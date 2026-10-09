"""Walk-forward test of the technical scanner library on US stocks.

Runs scanners.run_all exactly as the website does, on every 5th session of the downloaded daily
history (cache/prices.csv.gz, about 6 years), using only data up to that day. For every hit it
records the return over the next 5, 21 and 63 sessions (entry at the signal day's close) and
compares it with the S&P 500 and with the average stock in the universe on the same day.

Writes config/scan_evidence.json - shown on each scan's card on the website.

Caveats (also printed on the site): one market regime (2021-2026), today's universe (stocks that
were delisted are missing), close-to-close returns with no stops or costs. It answers "did this
list of stocks do better than the average stock afterwards?", not "is this a trading system?".

Run (about 15-25 minutes):  python scripts/backtest_scans.py
"""
import json
import sys
import warnings
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import compute as CP  # noqa: E402
import scanners as SC  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE, CFG = ROOT / "cache", ROOT / "config"
WIN = 420          # rows of history handed to the scanners each day
STEP = 5           # test every 5th session
HZ = (5, 21, 63)


def trimmed(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return float(np.mean(x)) if len(x) else np.nan
    lo, hi = np.percentile(x, [1, 99])
    return float(np.mean(x[(x >= lo) & (x <= hi)]))


def main():
    CP.S["history_calendar_days"] = 4000          # use the whole download, not the website's 2-year window
    W, u = CP.load()
    rows = json.loads((ROOT / "docs" / "data" / "stocks.json").read_text())
    U = [r["s"] for r in rows if r.get("k") != "e" and r["s"] in W["close"].columns]
    bench = CP.S["benchmark"]
    C_all = W["close"][U].ffill(limit=3)
    O_all, H_all, L_all = W["open"][U].fillna(C_all), W["high"][U], W["low"][U]
    V_all = W["volume"][U].fillna(0)
    B_all = W["close"][bench].ffill()
    n = len(C_all)
    fwd = {h: (C_all.shift(-h) / C_all - 1) for h in HZ}
    bfwd = {h: (B_all.shift(-h) / B_all - 1) for h in HZ}
    recs, base = [], []
    t0 = time.time()
    ts = list(range(WIN, n - HZ[0], STEP))
    for k, t in enumerate(ts):
        sl = slice(t - WIN + 1, t + 1)
        C, O, H, L, V = (x.iloc[sl] for x in (C_all, O_all, H_all, L_all, V_all))
        sma = {m: C.rolling(m, min_periods=m).mean() for m in (20, 50, 150, 200)}
        hi52, lo52 = H.rolling(252, min_periods=60).max(), L.rolling(252, min_periods=60).min()
        avgv50 = V.rolling(50, min_periods=20).mean().shift(1)
        rs_now = CP.pct_rank(CP.rs_score(C))
        rs_prev = CP.pct_rank(CP.rs_score(C, -1 - CP.LAG))
        cx = C.iloc[-1]
        tt = ((cx > sma[150].iloc[-1]) & (cx > sma[200].iloc[-1]) & (sma[150].iloc[-1] > sma[200].iloc[-1])
              & (sma[200].iloc[-1] > sma[200].iloc[-22]) & (sma[50].iloc[-1] > sma[150].iloc[-1])
              & (sma[50].iloc[-1] > sma[200].iloc[-1]) & (cx > sma[50].iloc[-1]) & (cx >= 1.3 * lo52.iloc[-1])
              & (cx >= 0.75 * hi52.iloc[-1]) & (rs_now >= 70))
        live = C.iloc[-1].notna() & (C.notna().sum() >= 260)
        cols = live[live].index
        x = {"O": O[cols], "H": H[cols], "L": L[cols], "C": C[cols], "V": V[cols], "bench": B_all.iloc[sl],
             "sma": {m: f[cols] for m, f in sma.items()}, "ema20": C[cols].ewm(span=20, adjust=False).mean(),
             "ema50": C[cols].ewm(span=50, adjust=False).mean(), "rs_now": rs_now[cols], "rs_prev": rs_prev[cols],
             "hi52": hi52[cols], "lo52": lo52[cols], "avgv50": avgv50[cols],
             "vr": V[cols].iloc[-1] / avgv50[cols].iloc[-1], "tt": tt[cols], "ath_prior": None}
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            meta, hits, _, _ = SC.run_all(x)
        d = C_all.index[t]
        ex = {h: (fwd[h].iloc[t][cols] - bfwd[h].iloc[t]) * 100 for h in HZ}
        avg = {h: trimmed(ex[h]) if t + h < n else np.nan for h in HZ}
        base.append((d, *[avg[h] for h in HZ]))
        for sid, syms in hits.items():
            for s in syms:
                recs.append((sid, d, s, *[ex[h].get(s, np.nan) if t + h < n else np.nan for h in HZ],
                             *[ex[h].get(s, np.nan) - avg[h] if t + h < n else np.nan for h in HZ]))
        if (k + 1) % 20 == 0:
            print(f"  {k + 1}/{len(ts)} days  {len(recs):,} hits  {time.time() - t0:.0f}s", flush=True)
    R = pd.DataFrame(recs, columns=["id", "date", "s", "x5", "x21", "x63", "e5", "e21", "e63"])
    R.to_csv(CACHE / "bt_scans.csv.gz", index=False, compression="gzip")
    Bz = pd.DataFrame(base, columns=["date", "b5", "b21", "b63"])
    names = {m["id"]: m["name"] for m in meta}
    out = {}
    for sid, g in R.groupby("id"):
        days = g.date.nunique()
        r = {"n": int(len(g)), "days": int(days)}
        for h in HZ:
            e, xx = g[f"e{h}"].dropna(), g[f"x{h}"].dropna()
            r[f"e{h}"] = round(trimmed(e), 2) if len(e) else None          # vs the average stock that day
            r[f"x{h}"] = round(trimmed(xx), 2) if len(xx) else None        # vs the S&P 500
            r[f"w{h}"] = round(float((e > 0).mean() * 100), 1) if len(e) else None
        # consistency: share of calendar years in which the 21-day edge was positive
        yr = g.dropna(subset=["e21"]).assign(y=g.date.str[:4]).groupby("y")["e21"].apply(trimmed)
        r["yrs_pos"], r["yrs"] = int((yr > 0).sum()), int(len(yr))
        out[sid] = r
    first, last = R.date.min(), R.date.max()
    ev = {"from": first, "to": last, "step": STEP, "universe": len(U),
          "base": {f"b{h}": round(trimmed(Bz[f"b{h}"]), 2) for h in HZ},
          "note": (f"Every {STEP}th session from {first} to {last}: each hit's return over the next 5, 21 and 63 sessions "
                   f"(from the signal day's close) compared with the average of all {len(U):,} stocks on the same day. "
                   "Today's universe only (no delisted stocks), one market regime, no stops or costs. Evidence, not a promise."),
          "scans": out}
    (CFG / "scan_evidence.json").write_text(json.dumps(ev, indent=1))
    tab = pd.DataFrame(out).T
    tab["name"] = [names.get(i, i) for i in tab.index]
    print(tab.sort_values("e21", ascending=False)[["name", "n", "days", "e5", "e21", "e63", "w21", "x21", "yrs_pos", "yrs"]].to_string())


if __name__ == "__main__":
    main()
