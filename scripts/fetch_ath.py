"""All-time-high reference for the Green Line Breakout scanner.

The daily price file holds about six years. For the all-time high we also need the older
history, which Yahoo serves as monthly candles back to listing (split-adjusted, no login).

Writes config/ath.csv:  symbol, ath_before, cutoff, built
  ath_before = highest monthly high in the months BEFORE `cutoff`
  cutoff     = first day of the month in which the file was built
compute.py combines it with the daily highs from `cutoff` onwards, so the file only needs
refreshing now and then; it is rebuilt when older than 7 days (that also picks up new splits).
"""
import json
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import yahoo as Y  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE, OUT = ROOT / "cache", ROOT / "config" / "ath.csv"
SETTINGS = json.loads((ROOT / "config" / "settings.json").read_text())
MAX_AGE_DAYS = 7


def main():
    force = "--force" in sys.argv
    today = Y.today_et()
    if OUT.exists() and not force:
        old = pd.read_csv(OUT)
        built = pd.to_datetime(old["built"].iloc[0]).date() if "built" in old and len(old) else None
        if built and (today - built).days < MAX_AGE_DAYS:
            print(f"ath.csv built {built}, still fresh - skipping")
            return
    u = pd.read_csv(CACHE / "universe.csv", dtype={"symbol": str}, keep_default_na=False)
    u = u[u.kind.isin(["stock", "etf"])]
    cutoff = today.replace(day=1)
    rl = Y.RateLimiter(SETTINGS.get("yahoo_requests_per_second", 15))

    def job(row):
        rl.wait()
        d = Y.chart(row.key, interval="1mo", rng="max")
        if d is None:
            return None
        d = d[d.date < cutoff.isoformat()]
        if not len(d):
            return None
        return pd.Series([float(d["high"].max())])  # 1-item Series (run_pool needs len)

    t0 = time.time()
    data, failed = Y.run_pool(list(u.itertuples()), job, 8, "monthly", key=lambda r: r.symbol)
    out = pd.DataFrame({"symbol": list(data), "ath_before": [round(float(v.iloc[0]), 4) for v in data.values()]})
    out["cutoff"], out["built"] = cutoff.isoformat(), today.isoformat()
    out.sort_values("symbol").to_csv(OUT, index=False)
    print(f"ath.csv: {len(out)} symbols, {len(failed)} without older history (new listings), {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()
