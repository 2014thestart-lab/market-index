#!/usr/bin/env python3
"""Emit out/summary.json - a compact digest of the collected series."""
import json, sqlite3, sys
from datetime import datetime, timezone
from pathlib import Path
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "market.db"
OUT = ROOT / "out" / "summary.json"
sys.path.insert(0, str(Path(__file__).resolve().parent))
from render import load_expenditure


def pct(s, lag):
    if len(s) <= lag:
        return None
    prev, last = s.iloc[-1 - lag], s.iloc[-1]
    return None if not prev else round((last / prev - 1) * 100, 2)


def block(s, dates, unit=None):
    return {"latest": round(float(s.iloc[-1]), 6),
            "latest_date": str(dates.iloc[-1])[:10],
            "n_points": int(len(s)),
            "change_pct": {"dod": pct(s, 1), "wow": pct(s, 7), "mom": pct(s, 30)},
            **({"unit": unit} if unit else {})}


def main():
    if not DB_PATH.exists():
        print("no database; run collect.py first")
        return 1
    conn = sqlite3.connect(DB_PATH)
    out = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "schema_version": 1,
           "source_note": "LLM token data derived from OpenRouter "
                          "(openrouter.ai/rankings); GPU prices from the "
                          "Vast.ai marketplace."}

    d = pd.read_sql("SELECT DISTINCT date FROM token_usage WHERE complete=1 "
                    "ORDER BY date", conn)["date"]
    if d.empty:
        out["coverage"] = {"complete_days": 0}
        OUT.parent.mkdir(parents=True, exist_ok=True)
        OUT.write_text(json.dumps(out, indent=2))
        print("wrote summary (no complete days yet)")
        return 0

    have = pd.to_datetime(d)
    full = pd.date_range(have.min(), have.max(), freq="D")
    gaps = sorted(set(full) - set(have))
    out["coverage"] = {"complete_days": int(len(have)),
                       "first_date": have.min().strftime("%Y-%m-%d"),
                       "last_date": have.max().strftime("%Y-%m-%d"),
                       "gaps": [g.strftime("%Y-%m-%d") for g in gaps]}

    u = pd.read_sql("SELECT date, SUM(prompt_tokens + completion_tokens) AS total "
                    "FROM token_usage WHERE complete=1 GROUP BY date ORDER BY date", conn)
    if len(u) >= 1:
        out["token_usage"] = block(u["total"], u["date"], "tokens/day")

    g = load_expenditure(conn)
    if g is not None and len(g) >= 1:
        out["token_expenditure"] = {
            **block(g["index"], g["date"]),
            "blended_usd_per_mtok": round(float(g["usd_per_mtok"].iloc[-1]), 4),
            "basis": f"usage-weighted blended price, indexed to 1.0 at {g['date'].iloc[0]}"}

    gp = pd.read_sql("SELECT date, gpu_class, median_dph, min_dph, n_offers "
                     "FROM gpu_price ORDER BY date", conn)
    if not gp.empty:
        latest = gp[gp["date"] == gp["date"].max()]
        gp["family"] = gp["gpu_class"].str.split().str[0]
        fam = gp.groupby(["date", "family"])["median_dph"].mean().unstack()
        out["gpu_rental"] = {
            "latest_date": gp["date"].max(),
            "classes": {r.gpu_class: {"median_usd_per_gpu_hour": round(r.median_dph, 4),
                                      "min_usd_per_gpu_hour": round(r.min_dph, 4),
                                      "n_offers": int(r.n_offers)}
                        for r in latest.itertuples()},
            "index": {f: block(fam[f] / fam[f].iloc[0], pd.Series(fam.index))
                      for f in fam.columns if fam[f].notna().all()} if len(fam) >= 2 else {}}

    conn.close()
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
