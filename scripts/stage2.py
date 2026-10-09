"""Stage 2 phase: EARLY, MID or LATE in the advance, plus a Stage 2 Score for strength.

Stage 2 is the dashboard's own definition (Weinstein, on daily bars): close above the 150-day
SMA (the 30-week line) and the SMA up more than 1% over 20 sessions.

The advance is dated from its start: the first Stage 2 day after the last confirmed Stage 4
(10 or more of 20 sessions below a falling 150-day SMA). Maturity points (0-9):

  O'Neil base count since the start   2 bases 1 · 3 bases 2 · 4 bases 3 · 5+ bases 4
     (a base counts when the stock gained 20%+ from the last counted pivot; undercutting the
      previous base's low starts the count again at 1 - IBD's rules)
  time in Stage 2                     6-12 months 1 · 12+ months 2
  gain from the low before the start  150-300% 1 · over 300% 2
  150-day slope slowing               1 (slope 40 sessions ago was 1.5+ points steeper)

  EARLY 0-1 · MID 2-4 · LATE 5+

Distance above the moving averages is NOT a maturity point: on NSE and US data alike, stocks
further above their 150-day line did better, as the academic moving-average studies found. More than 100%
above the 200-day is shown as a climax warning because drawdowns were deeper there.

Stage 2 Score (0-100): RS 25 · 150-day slope 20 · distance from the advance's peak 20 ·
extension above the 150-day 15 (30-100% best; over 100% scores low) · above the 50-day 10 ·
at a multi-year closing high 10.

Checked on 20 years of US data (2007-2026, 315,000 Stage 2 snapshots taken every 10 sessions,
2,500 stocks; scripts/backtest_us.py): see config/stage2_evidence.json, shown on the website.
In the US, Late meant more risk (more falls into Stage 4, deeper drops) but not lower returns;
the Stage 2 Score sorted outcomes in both halves.
"""
import json
from pathlib import Path

import numpy as np
import pandas as pd

import bases as BS

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config"


def maturity_points(bases, age, gain_cycle, slope_chg):
    pb = 4 if bases >= 5 else 3 if bases == 4 else 2 if bases == 3 else 1 if bases == 2 else 0
    pa = 2 if age >= 260 else 1 if age >= 130 else 0
    pg = 2 if gain_cycle > 3 else 1 if gain_cycle > 1.5 else 0
    pd_ = 1 if slope_chg < -0.015 else 0
    return [pb, pa, pg, pd_]


def phase_of(points):
    m = sum(points)
    return "E" if m <= 1 else "M" if m <= 4 else "L"


def score_parts(rs, slope, ext150, dd_peak, a50, ath):
    rs = rs if rs is not None and np.isfinite(rs) else 0
    p_rs = 25 if rs >= 90 else 20 if rs >= 80 else 14 if rs >= 70 else 7 if rs >= 50 else 0
    p_sl = 20 if slope > .09 else 15 if slope > .063 else 10 if slope > .046 else 6 if slope > .034 else 2
    p_ex = 5 if ext150 > 1 else 15 if ext150 > .3 else 11 if ext150 > .2 else 6 if ext150 > .1 else 2
    p_pk = 20 if dd_peak > -.03 else 15 if dd_peak > -.08 else 8 if dd_peak > -.15 else 3 if dd_peak > -.25 else 0
    return [p_rs, p_sl, p_pk, p_ex, 10 if a50 else 0, 10 if ath else 0]


def base_count(c, v, cs, t):
    """O'Neil base count between bar cs and t, on the first breakout of each base of 25+ days
    (same event rules as the backtest: liquid at the breakout, one event per base, retries
    after a failed breakout not counted)."""
    ev = BS.recent_breakouts(c, 60, BS.D_MAX, BS.ZONE, BS.MIN_LEN)
    if not len(ev):
        return 0, None
    to = pd.Series(c * v / 1e6).rolling(50).median().to_numpy()   # $ millions
    lt, lc = -1, None
    cnt, lastc, lastf, last_t = 0, None, None, None
    for r in ev:
        te, st, ce, fl, ci = int(r[0]), int(r[1]), r[2], r[3], int(r[8])
        if te > t:
            break
        if not (to[te - 1] >= 1.0):
            continue                                   # illiquid at the time (under $1M a day)
        if lt < 0 or st > lt or ci < lt:
            retry = False                              # a new base (or an older, bigger one)
        elif c[lt + 1:te].size and c[lt + 1:te].min() < lc * 0.95:
            retry = True                               # failed, back in the base, broke out again
        else:
            continue                                   # same breakout run
        lt, lc = te, ce
        if retry or te < cs:
            continue
        if lastc is None or fl < lastf:
            cnt = 1                                    # first base, or undercut the last base's low
        elif ce >= 1.2 * lastc:
            cnt += 1
        else:
            continue                                   # base-on-base: same stage
        lastc, lastf, last_t = ce, fl, te
    return cnt, last_t


def build(p_long, stage2_syms, rs_now):
    import time
    t0 = time.time()
    data = BS._series(p_long, list(stage2_syms), BS.load_bse())
    window_start = str(p_long.date.min())
    rows, per = [], {}
    for s in stage2_syms:
        if s not in data:
            continue
        d, c, v, nbse = data[s]
        n = len(c)
        if n < 171:                                    # needs a 150-day SMA and its 20-day slope
            continue
        e = n - 1
        cs_ = pd.Series(c)
        sma150 = cs_.rolling(150).mean().to_numpy()
        sma50 = cs_.rolling(50).mean().to_numpy()
        sma200 = cs_.rolling(200).mean().to_numpy()
        slope = sma150 / np.roll(sma150, 20) - 1
        slope[:170] = np.nan
        stage = np.where((c > sma150) & (slope > .01), 2, np.where((c < sma150) & (slope < -.01), 4, 0))
        conf4 = pd.Series((stage == 4).astype(int)).rolling(20).sum().to_numpy() >= 10
        l4 = np.flatnonzero(conf4[:e + 1])
        lo_i = int(l4[-1]) + 1 if len(l4) else 170
        s2 = np.flatnonzero(stage[lo_i:e + 1] == 2)
        if not len(s2):
            continue
        cs = lo_i + int(s2[0])
        censored = not len(l4)
        age = e - cs
        low = c[max(0, cs - 120):cs + 1].min()
        peak = c[cs:e + 1].max()
        gain_cycle = c[e] / low - 1
        sl = slope[e] if np.isfinite(slope[e]) else 0.0
        slope_chg = sl - slope[e - 40] if e >= 210 and np.isfinite(slope[e - 40]) else 0.0
        ext150 = c[e] / sma150[e] - 1
        ext200 = c[e] / sma200[e] - 1 if np.isfinite(sma200[e]) else np.nan   # under 200 sessions listed
        dd_peak = c[e] / peak - 1
        a50 = bool(c[e] > sma50[e])
        ath = bool(c[e] >= c[:e + 1].max() * 0.999)
        nb, last_bo = base_count(c, v, cs, e)
        mp = maturity_points(nb, age, gain_cycle, slope_chg)
        sp = score_parts(rs_now.get(s, np.nan), sl, ext150, dd_peak, a50, ath)
        warn = []
        if not a50:
            warn.append("below50")
        if dd_peak <= -0.15:
            warn.append("offpeak")
        if slope_chg < -0.015:
            warn.append("slowing")
        if np.isfinite(ext200) and ext200 > 1.0:
            warn.append("climax")
        rec = {"s": s, "ph": phase_of(mp), "mat": int(sum(mp)), "mp": mp, "sc": int(sum(sp)), "sp": sp,
               "start": str(d[cs]), "age": int(age), "cap": bool(censored and str(d[0]) <= window_start),
               "young": bool(censored and str(d[0]) > window_start and cs <= 175),   # clock starts once a 150-day SMA exists
               "bases": nb, "lastbo": str(d[last_bo]) if last_bo is not None else None,
               "gain": BS._r(gain_cycle * 100, 0), "gstart": BS._r((c[e] / c[cs] - 1) * 100, 0),
               "e150": BS._r(ext150 * 100, 1), "e200": BS._r(ext200 * 100, 1), "pk": BS._r(dd_peak * 100, 1),
               "slope": BS._r(sl * 100, 1), "slc": BS._r(slope_chg * 100, 1), "a50": a50, "ath": ath, "warn": warn,
               "low": BS._px(low), "bse": bool(nbse and cs < nbse)}
        rows.append(rec)
        per[s] = {"s2p": rec["ph"], "s2s": rec["sc"]}
    ev_file = CFG / "stage2_evidence.json"
    evidence = json.loads(ev_file.read_text()) if ev_file.exists() else None
    cnt = pd.Series([r["ph"] for r in rows]).value_counts().to_dict() if rows else {}
    print(f"stage 2: {len(rows)} stocks (early {cnt.get('E', 0)}, mid {cnt.get('M', 0)}, late {cnt.get('L', 0)}), {time.time() - t0:.0f}s")
    return {"rows": rows, "per_stock": per, "evidence": evidence}


def scans(B):
    if not B["rows"]:
        return [], {}, {}
    R = pd.DataFrame(B["rows"]).set_index("s")
    hits, metric, meta = {}, {}, []

    def add(id_, name, desc, mask, m_label, m_vals, sort="metric"):
        syms = sorted(R.index[mask.fillna(False)])
        hits[id_] = syms
        metric[id_] = {s: float(m_vals[s]) for s in syms}
        meta.append({"id": id_, "name": name, "cat": "Stage 2", "desc": desc, "metric": m_label, "fmt": "int", "sort": sort})

    add("s2_early", "Early Stage 2, score 70+",
        "Early in a Stage 2 advance (first base or two, under 6-12 months, not yet far from the low) with a Stage 2 Score of 70+. "
        "In 20 years of US data this group beat the S&P 500 by about 6.6% on average over the next year, and 11% fell into Stage 4 "
        "within 6 months (against 21% for all Stage 2 stocks).",
        (R.ph == "E") & (R.sc >= 70), "Stage 2 Score", R.sc)
    add("s2_late_weak", "Late Stage 2, weakening",
        "Late in the advance (4+ bases, a year or more, a very large gain, a slowing 150-day line or a climax run) with a Stage 2 Score under 55. "
        "In 20 years of US data 36% of this group fell into Stage 4 within 6 months (against 21% for all Stage 2 stocks) - "
        "time to tighten stops.",
        (R.ph == "L") & (R.sc < 55), "Maturity points", R.mat, "metric")
    return meta, hits, metric
