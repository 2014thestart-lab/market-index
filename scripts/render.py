#!/usr/bin/env python3
"""
Render the three market-index charts from the collected SQLite database.

Outputs (PNG, 1280x640 @ 2x):
  out/token_expenditure_index.png
  out/token_usage.png
  out/gpu_rental_index.png
"""

import sqlite3
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "market.db"
OUT_DIR = ROOT / "out"

FOOTER = "MARKET INDEX | Collected daily history"
MAX_UNIT_USD_PER_MTOK = 200.0

# ---- palette -------------------------------------------------------------
BG = "#0B1020"        # figure background
PANEL = "#111C36"     # plot area
GRID = "#1E2B4A"
FG = "#FFFFFF"
MUTED = "#8C9BB8"
CYAN = "#2DE2E6"
BLUE = "#4A6FDB"
A100_C = "#5B7CFA"
H100_C = "#2DD4BF"
B200_C = "#C084FC"

plt.rcParams.update({
    "font.family": "DejaVu Sans",
    "figure.facecolor": BG,
    "savefig.facecolor": BG,
    "axes.facecolor": PANEL,
    "text.color": FG,
})


def frame(ax, span_days=None):
    """Apply the shared dark styling to an axes object."""
    for side in ("top", "right", "bottom", "left"):
        ax.spines[side].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(False)
    # Month labels only make sense once there is more than a season of data.
    fmt = "%y.%m" if (span_days or 0) >= 90 else "%m-%d"
    ax.xaxis.set_major_formatter(mdates.DateFormatter(fmt))
    ax.xaxis.set_major_locator(mdates.AutoDateLocator(minticks=3, maxticks=10))


def decorate(fig, title, subtitle, badge, badge_color=CYAN):
    fig.text(0.022, 0.945, title, fontsize=17, fontweight="bold", color=FG,
             va="top")
    fig.text(0.022, 0.888, subtitle, fontsize=9.5, color=MUTED, va="top")
    fig.text(0.978, 0.928, badge, fontsize=10, fontweight="bold",
             color=badge_color, ha="right", va="top")
    fig.text(0.022, 0.035, FOOTER, fontsize=8, color=MUTED, va="bottom")


def wow(series):
    """Week-over-week percent change of the last observation."""
    if len(series) < 8:
        return None
    prev, last = series.iloc[-8], series.iloc[-1]
    if not prev:
        return None
    return (last / prev - 1) * 100


def badge_text(value, label=""):
    if value is None:
        return f"{label}n/a" if label else "WoW n/a"
    return f"{label}{value:+.2f}%"


def last_date(df):
    return pd.to_datetime(df["date"]).max().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
def load_expenditure(conn):
    """Usage-weighted blended price per million tokens, indexed to 1.0."""
    usage = pd.read_sql(
        "SELECT date, model_permaslug, prompt_tokens, completion_tokens "
        "FROM token_usage WHERE complete = 1", conn)
    price = pd.read_sql(
        "SELECT date, model_id, canonical_slug, prompt_usd, completion_usd "
        "FROM model_price", conn)
    if usage.empty or price.empty:
        return None

    # Build a slug -> price lookup from the most recent price snapshot.
    # (Historical list prices are not retrievable, so the newest snapshot is
    #  carried backwards. Once the pipeline has run for a while, switch to an
    #  as-of join on `date` for a fully faithful index.)
    latest = price["date"].max()
    snap = price[price["date"] == latest]
    lookup = {}
    for _, r in snap.iterrows():
        for key in (r["canonical_slug"], r["model_id"]):
            if key:
                lookup[key] = (r["prompt_usd"], r["completion_usd"])

    def resolve(slug):
        if slug in lookup:
            return lookup[slug]
        # permaslugs often carry a -YYYYMMDD suffix; try trimming it
        base = slug.rsplit("-", 1)[0]
        return lookup.get(base)

    usage["px"] = usage["model_permaslug"].map(resolve)
    matched = usage.dropna(subset=["px"]).copy()
    if matched.empty:
        return None

    # Media/rerank models can carry per-unit prices that are meaningless per
    # token; drop anything implausible before blending.
    matched = matched[matched["px"].map(
        lambda t: (t[0] + t[1]) * 1e6 < MAX_UNIT_USD_PER_MTOK)].copy()
    if matched.empty:
        return None

    matched["cost"] = matched.apply(
        lambda r: r["prompt_tokens"] * r["px"][0]
        + r["completion_tokens"] * r["px"][1], axis=1)
    matched["tokens"] = matched["prompt_tokens"] + matched["completion_tokens"]

    g = matched.groupby("date").agg(cost=("cost", "sum"),
                                    tokens=("tokens", "sum")).reset_index()
    g = g[g["tokens"] > 0].sort_values("date")
    g["usd_per_mtok"] = g["cost"] / g["tokens"] * 1e6
    g["index"] = g["usd_per_mtok"] / g["usd_per_mtok"].iloc[0]

    coverage = matched["tokens"].sum() / (
        usage["prompt_tokens"] + usage["completion_tokens"]).sum()
    print(f"  expenditure: price-matched {coverage:.1%} of token volume")
    return g


def chart_expenditure(conn):
    g = load_expenditure(conn)
    if g is None or len(g) < 2:
        print("  skip expenditure (insufficient data)")
        return
    x = pd.to_datetime(g["date"])

    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=100)
    fig.subplots_adjust(left=0.09, right=0.90, top=0.76, bottom=0.14)
    ax.plot(x, g["index"], color=CYAN, lw=1.8, solid_joinstyle="round")
    ax.set_ylabel("Index", color=MUTED, fontsize=10)
    frame(ax, (x.max() - x.min()).days)
    decorate(fig, "LLM Token Expenditure Index",
             f"Daily history through {last_date(g)}",
             badge_text(wow(g["index"])))
    out = OUT_DIR / "token_expenditure_index.png"
    fig.savefig(out, dpi=200)
    plt.close(fig)
    print(f"  wrote {out.name}")


def chart_usage(conn):
    g = pd.read_sql(
        "SELECT date, SUM(prompt_tokens + completion_tokens) AS total "
        "FROM token_usage WHERE complete = 1 GROUP BY date ORDER BY date", conn)
    if len(g) < 2:
        print("  skip usage (insufficient data)")
        return
    x = pd.to_datetime(g["date"])

    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=100)
    fig.subplots_adjust(left=0.11, right=0.90, top=0.76, bottom=0.14)
    ax.bar(x, g["total"], color=BLUE, width=0.78)
    ax.set_ylabel("Total tokens", color=MUTED, fontsize=10)
    ax.set_ylim(0, g["total"].max() * 1.08)
    frame(ax, (x.max() - x.min()).days)
    decorate(fig, "Token Usage (OpenRouter)",
             f"Daily total-token history through {last_date(g)}",
             badge_text(wow(g["total"])))
    out = OUT_DIR / "token_usage.png"
    fig.savefig(out, dpi=200)
    plt.close(fig)
    print(f"  wrote {out.name}")


def chart_gpu(conn):
    df = pd.read_sql(
        "SELECT date, gpu_class, median_dph FROM gpu_price ORDER BY date", conn)
    if df.empty:
        print("  skip gpu (no data)")
        return

    # Collapse SXM/PCIE variants into one series per silicon generation.
    df["family"] = df["gpu_class"].str.split().str[0]
    g = (df.groupby(["date", "family"])["median_dph"].mean()
           .unstack("family").sort_index())

    series = [(n, c) for n, c in
              [("A100", A100_C), ("H100", H100_C), ("B200", B200_C)]
              if n in g.columns]
    if not series:
        print("  skip gpu (no tracked families)")
        return
    if len(g) < 2:
        print(f"  skip gpu chart ({len(g)} day of data — needs 2+); "
              "keep collecting")
        return

    x = pd.to_datetime(g.index)
    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=100)
    fig.subplots_adjust(left=0.09, right=0.86, top=0.76, bottom=0.14)

    axes, handles, badges = [ax], [], []
    for i, (name, color) in enumerate(series):
        a = ax if i == 0 else ax.twinx()
        if i > 1:
            a.spines["right"].set_position(("axes", 1 + 0.085 * (i - 1)))
        if i > 0:
            a.set_facecolor("none")
        vals = g[name] / g[name].iloc[0]
        ln, = a.plot(x, vals, color=color, lw=1.6, label=name)
        a.set_ylabel(name, color=color, fontsize=10)
        a.tick_params(axis="y", colors=color, labelsize=9, length=0)
        for s in ("top", "left", "bottom"):
            a.spines[s].set_visible(False)
        a.spines["right"].set_color(color if i else "none")
        handles.append(ln)
        badges.append(badge_text(wow(vals), f"{name} "))
        axes.append(a)

    frame(ax, (x.max() - x.min()).days)
    ax.legend(handles=handles, loc="upper left", frameon=False,
              labelcolor=MUTED, fontsize=10, ncol=len(handles),
              bbox_to_anchor=(0.02, 1.10))
    decorate(fig, "GPU Rental Index",
             f"Daily normalized scores through {g.index.max()} "
             "| independent Y-axes",
             "  |  ".join(badges))
    out = OUT_DIR / "gpu_rental_index.png"
    fig.savefig(out, dpi=200)
    plt.close(fig)
    print(f"  wrote {out.name}")


def seed_demo(conn, days=180):
    """Populate a throwaway in-memory history so the chart styling can be
    reviewed before the real series has accumulated. Clearly watermarked."""
    import random
    random.seed(7)
    end = pd.Timestamp(pd.read_sql(
        "SELECT MAX(date) d FROM token_usage WHERE complete=1", conn).d[0])
    dates = pd.date_range(end - pd.Timedelta(days=days - 1), end, freq="D")

    base = pd.read_sql("SELECT gpu_class, median_dph FROM gpu_price", conn)
    rows_u, rows_g = [], []
    lvl, vol = 1.0, 6e12
    for i, d in enumerate(dates):
        lvl *= 1 + random.gauss(0, 0.03)
        vol *= 1 + random.gauss(0.012, 0.05)
        rows_u.append((d.date().isoformat(), lvl, vol))
        for _, r in base.iterrows():
            drift = 1 + 0.25 * (i / len(dates)) + random.gauss(0, 0.02)
            rows_g.append((d.date().isoformat(), r.gpu_class,
                           r.median_dph * drift))
    return (pd.DataFrame(rows_u, columns=["date", "index", "total"]),
            pd.DataFrame(rows_g, columns=["date", "gpu_class", "median_dph"]))


def render_demo(conn):
    global FOOTER
    FOOTER = "PREVIEW | SYNTHETIC DATA - styling check only, not research output"
    u, gp = seed_demo(conn)
    x = pd.to_datetime(u["date"])
    span = (x.max() - x.min()).days

    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=100)
    fig.subplots_adjust(left=0.09, right=0.90, top=0.76, bottom=0.14)
    ax.plot(x, u["index"], color=CYAN, lw=1.8)
    ax.set_ylabel("Index", color=MUTED, fontsize=10)
    frame(ax, span)
    decorate(fig, "LLM Token Expenditure Index",
             f"Daily history through {u.date.max()}", badge_text(wow(u["index"])))
    fig.savefig(OUT_DIR / "preview_token_expenditure_index.png", dpi=200)
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=100)
    fig.subplots_adjust(left=0.11, right=0.90, top=0.76, bottom=0.14)
    ax.bar(x, u["total"], color=BLUE, width=0.9)
    ax.set_ylabel("Total tokens", color=MUTED, fontsize=10)
    ax.set_ylim(0, u["total"].max() * 1.08)
    frame(ax, span)
    decorate(fig, "Token Usage (OpenRouter)",
             f"Daily total-token history through {u.date.max()}",
             badge_text(wow(u["total"])))
    fig.savefig(OUT_DIR / "preview_token_usage.png", dpi=200)
    plt.close(fig)

    gp["family"] = gp["gpu_class"].str.split().str[0]
    g = gp.groupby(["date", "family"])["median_dph"].mean().unstack("family")
    xg = pd.to_datetime(g.index)
    fig, ax = plt.subplots(figsize=(12.8, 6.4), dpi=100)
    fig.subplots_adjust(left=0.09, right=0.86, top=0.76, bottom=0.14)
    handles, badges = [], []
    for i, (name, color) in enumerate(
            [("A100", A100_C), ("H100", H100_C), ("B200", B200_C)]):
        a = ax if i == 0 else ax.twinx()
        if i > 1:
            a.spines["right"].set_position(("axes", 1 + 0.085 * (i - 1)))
        if i > 0:
            a.set_facecolor("none")
        vals = g[name] / g[name].iloc[0]
        ln, = a.plot(xg, vals, color=color, lw=1.6, label=name)
        a.set_ylabel(name, color=color, fontsize=10)
        a.tick_params(axis="y", colors=color, labelsize=9, length=0)
        for sp in ("top", "left", "bottom"):
            a.spines[sp].set_visible(False)
        a.spines["right"].set_color(color if i else "none")
        handles.append(ln)
        badges.append(badge_text(wow(vals), f"{name} "))
    frame(ax, span)
    ax.legend(handles=handles, loc="upper left", frameon=False,
              labelcolor=MUTED, fontsize=10, ncol=3, bbox_to_anchor=(0.02, 1.10))
    decorate(fig, "GPU Rental Index",
             f"Daily normalized scores through {g.index.max()} "
             "| independent Y-axes", "  |  ".join(badges))
    fig.savefig(OUT_DIR / "preview_gpu_rental_index.png", dpi=200)
    plt.close(fig)
    print("  wrote 3 preview_*.png (synthetic)")


def coverage_report(conn):
    """Print collected span and any missing days. A gap here is permanent --
    the upstream source cannot be re-queried for a day that was missed."""
    d = pd.read_sql(
        "SELECT DISTINCT date FROM token_usage WHERE complete=1 ORDER BY date",
        conn)["date"]
    if d.empty:
        print("coverage: no complete days yet")
        return
    have = pd.to_datetime(d)
    full = pd.date_range(have.min(), have.max(), freq="D")
    missing = sorted(set(full) - set(have))
    print(f"coverage: {len(have)} complete days "
          f"({have.min():%Y-%m-%d} -> {have.max():%Y-%m-%d})")
    if missing:
        print(f"  GAPS ({len(missing)}): "
              + ", ".join(m.strftime("%Y-%m-%d") for m in missing[:10])
              + (" ..." if len(missing) > 10 else ""))


def main():
    if not DB_PATH.exists():
        print(f"No database at {DB_PATH}. Run scripts/collect.py first.")
        return 1
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    if "--demo" in sys.argv:
        print("rendering PREVIEW (synthetic):")
        render_demo(conn)
        conn.close()
        return 0
    coverage_report(conn)
    print("rendering:")
    chart_expenditure(conn)
    chart_usage(conn)
    chart_gpu(conn)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
