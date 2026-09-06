#!/usr/bin/env python3
"""
Daily collector for the LLM/GPU market index.

Sources (all keyless):
  1. OpenRouter frontend rankings  -> daily per-model token volume (30d rolling)
  2. OpenRouter /api/v1/models     -> per-model list price snapshot
  3. Vast.ai marketplace bundles   -> live GPU rental offers

Everything lands in SQLite with UPSERT semantics, so re-running is safe and
the 30-day rolling window self-heals any missed days.
"""

import json
import sqlite3
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "market.db"

UA = {"User-Agent": "market-index-collector/1.0 (research)"}
TIMEOUT = 60

COMPLETE_MIN_MODELS = 100

# Vast.ai GPU classes tracked. Add rows here to widen coverage.
GPU_CLASSES = ["A100 SXM4", "A100 PCIE", "H100 SXM", "H100 PCIE", "B200"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS token_usage (
    date              TEXT NOT NULL,
    model_permaslug   TEXT NOT NULL,
    variant           TEXT NOT NULL,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    reasoning_tokens  INTEGER,
    request_count     INTEGER,
    complete          INTEGER DEFAULT 0,
    collected_at      TEXT,
    PRIMARY KEY (date, model_permaslug, variant)
);

CREATE TABLE IF NOT EXISTS model_price (
    date            TEXT NOT NULL,
    model_id        TEXT NOT NULL,
    canonical_slug  TEXT,
    prompt_usd      REAL,   -- USD per token
    completion_usd  REAL,
    collected_at    TEXT,
    PRIMARY KEY (date, model_id)
);

CREATE TABLE IF NOT EXISTS gpu_price (
    date          TEXT NOT NULL,
    gpu_class     TEXT NOT NULL,
    n_offers      INTEGER,
    min_dph       REAL,   -- USD per GPU-hour
    p25_dph       REAL,
    median_dph    REAL,
    mean_dph      REAL,
    collected_at  TEXT,
    PRIMARY KEY (date, gpu_class)
);
"""


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def get_json(url, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return json.loads(r.read().decode())
        except Exception as exc:  # noqa: BLE001
            last = exc
            log(f"  retry {attempt + 1}/{retries} after {exc}")
            time.sleep(2 ** attempt)
    raise RuntimeError(f"GET failed: {url} ({last})")


def quantile(sorted_vals, q):
    if not sorted_vals:
        return None
    idx = (len(sorted_vals) - 1) * q
    lo, hi = int(idx), min(int(idx) + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)


# --------------------------------------------------------------------------
# 1. Token usage
# --------------------------------------------------------------------------
def collect_token_usage(conn, now):
    url = "https://openrouter.ai/api/frontend/v1/rankings/models?view=month"
    rows = get_json(url)["data"]

    # The keyless endpoint only reports ONE finalised day in full; earlier
    # dates come back as sparse late-arriving fragments. Counting models per
    # date separates the two, so downstream code never blends a complete day
    # with a 2-model fragment.
    per_day = {}
    for r in rows:
        per_day[r["date"][:10]] = per_day.get(r["date"][:10], 0) + 1
    threshold = max(COMPLETE_MIN_MODELS, 0.5 * max(per_day.values(), default=0))
    complete_days = {d for d, n in per_day.items() if n >= threshold}

    payload = [
        (
            r["date"][:10],
            r["model_permaslug"],
            r.get("variant") or "standard",
            r.get("total_prompt_tokens") or 0,
            r.get("total_completion_tokens") or 0,
            r.get("total_native_tokens_reasoning") or 0,
            r.get("count") or 0,
            1 if r["date"][:10] in complete_days else 0,
            now,
        )
        for r in rows
    ]
    conn.executemany(
        """INSERT INTO token_usage VALUES (?,?,?,?,?,?,?,?,?)
           ON CONFLICT(date, model_permaslug, variant) DO UPDATE SET
             prompt_tokens=excluded.prompt_tokens,
             completion_tokens=excluded.completion_tokens,
             reasoning_tokens=excluded.reasoning_tokens,
             request_count=excluded.request_count,
             complete=MAX(token_usage.complete, excluded.complete),
             collected_at=excluded.collected_at""",
        payload,
    )
    log(f"token_usage   : {len(payload)} rows over {len(per_day)} dates; "
        f"complete={sorted(complete_days)}")


# --------------------------------------------------------------------------
# 2. Model list prices
# --------------------------------------------------------------------------
def collect_model_prices(conn, today, now):
    models = get_json("https://openrouter.ai/api/v1/models")["data"]
    payload = []
    for m in models:
        pr = m.get("pricing") or {}
        try:
            prompt = float(pr.get("prompt", 0) or 0)
            completion = float(pr.get("completion", 0) or 0)
        except (TypeError, ValueError):
            continue
        payload.append(
            (today, m["id"], m.get("canonical_slug"), prompt, completion, now)
        )
    conn.executemany(
        """INSERT INTO model_price VALUES (?,?,?,?,?,?)
           ON CONFLICT(date, model_id) DO UPDATE SET
             prompt_usd=excluded.prompt_usd,
             completion_usd=excluded.completion_usd,
             collected_at=excluded.collected_at""",
        payload,
    )
    log(f"model_price   : {len(payload)} models priced")


# --------------------------------------------------------------------------
# 3. GPU rental prices
# --------------------------------------------------------------------------
def collect_gpu_prices(conn, today, now):
    payload = []
    for gpu in GPU_CLASSES:
        q = {
            "gpu_name": {"eq": gpu},
            "rentable": {"eq": True},
            "type": "on-demand",
            "num_gpus": {"gte": 1},
            "order": [["dph_total", "asc"]],
            "limit": 500,
        }
        url = ("https://console.vast.ai/api/v0/bundles/?q="
               + urllib.parse.quote(json.dumps(q)))
        try:
            offers = get_json(url).get("offers", [])
        except RuntimeError as exc:
            log(f"  {gpu}: skipped ({exc})")
            continue

        prices = sorted(
            o["dph_total"] / max(o.get("num_gpus") or 1, 1)
            for o in offers
            if o.get("dph_total")
        )
        if not prices:
            log(f"  {gpu}: no rentable offers")
            continue

        payload.append((
            today, gpu, len(prices),
            prices[0],
            quantile(prices, 0.25),
            quantile(prices, 0.50),
            sum(prices) / len(prices),
            now,
        ))
        log(f"  {gpu:11s}: n={len(prices):3d} "
            f"min=${prices[0]:.3f} med=${quantile(prices, 0.5):.3f}")
        time.sleep(1)  # be polite to the marketplace API

    conn.executemany(
        """INSERT INTO gpu_price VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(date, gpu_class) DO UPDATE SET
             n_offers=excluded.n_offers, min_dph=excluded.min_dph,
             p25_dph=excluded.p25_dph, median_dph=excluded.median_dph,
             mean_dph=excluded.mean_dph, collected_at=excluded.collected_at""",
        payload,
    )
    log(f"gpu_price     : {len(payload)} GPU classes")


def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    conn = sqlite3.connect(DB_PATH)
    conn.executescript(SCHEMA)

    failures = []
    for name, fn in [
        ("token_usage", lambda: collect_token_usage(conn, now)),
        ("model_price", lambda: collect_model_prices(conn, today, now)),
        ("gpu_price", lambda: collect_gpu_prices(conn, today, now)),
    ]:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{name}: {exc}")
            log(f"!! {name} FAILED: {exc}")

    conn.commit()
    conn.close()

    if failures:
        log(f"completed with {len(failures)} failure(s)")
        # Partial success is still worth committing; only hard-fail if all died.
        return 1 if len(failures) == 3 else 0
    log(f"done -> {DB_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
