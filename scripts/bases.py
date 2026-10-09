"""Long-base identifier: how many daily bars each stock has spent in its base.

A base is the stretch of bars a stock has spent below a ceiling (pivot) without falling
more than 50% below it. Everything is measured on daily closes, so one wick does not
move the pivot.

  pivot   = a closing high made inside the base (the left-side high after an advance, or the
            top of a rally inside a range the stock fell into)
  start   = after an advance: the first close within 5% of the pivot (the left-side high);
            after a decline:  the bar after the last close above the pivot
  length  = trading days from start to today - the "base days" shown on the site
  depth   = pivot to lowest close in the base

The Base Score was built and validated on 20 years of NSE data in the Indian edition. On 20 years
of US data (scripts/backtest_us.py, config/base_evidence.json, shown on the website) the result
was different: 50-199-day bases in an uptrend did best, 400+ day bases lagged the S&P 500, and the
Base Score cut failed breakouts but did not sort returns. The code is kept identical to the Indian
edition so both sites measure bases the same way; the website shows the US evidence.

Input: cache/prices.csv.gz (base_history_calendar_days of daily candles).
"""
import json
import math
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from numba import njit
except ImportError:  # works without numba, just slower (~2 min instead of seconds)
    def njit(*a, **k):
        if a and callable(a[0]):
            return a[0]
        return lambda f: f

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config"
D_MAX = 0.50     # deepest base accepted (deeper is a recovery, not a base)
ZONE = 0.05      # "at the ceiling" = close within 5% of it
MIN_LEN = 25     # shortest base: 5 weeks (O'Neil flat base)
RECENT = 20      # a breakout in the last 20 sessions is shown as a breakout
BUY_RANGE = 0.05 # up to 5% above the pivot (O'Neil/IBD)
CHART_PTS = 130


# ─────────────────────────────────────────────── core (same code as the backtest)
@njit(cache=True)
def depth_start(c, dmax):
    """s[t] = first bar of the longest window ending at t whose lowest close is within dmax of its highest."""
    n = len(c)
    s = np.zeros(n, np.int64)
    qmax = np.empty(n, np.int64); hmax = 0; tmax = 0
    qmin = np.empty(n, np.int64); hmin = 0; tmin = 0
    lo = 0
    k = 1.0 - dmax
    for t in range(n):
        while tmax > hmax and c[qmax[tmax - 1]] <= c[t]:
            tmax -= 1
        qmax[tmax] = t; tmax += 1
        while tmin > hmin and c[qmin[tmin - 1]] >= c[t]:
            tmin -= 1
        qmin[tmin] = t; tmin += 1
        while c[qmin[hmin]] < k * c[qmax[hmax]]:
            lo += 1
            if qmax[hmax] < lo:
                hmax += 1
            if qmin[hmin] < lo:
                hmin += 1
        s[t] = lo
    return s


@njit(cache=True)
def prev_ge(c):
    n = len(c)
    out = np.full(n, -1, np.int64)
    st = np.empty(n, np.int64)
    top = 0
    for t in range(n):
        while top > 0 and c[st[top - 1]] < c[t]:
            top -= 1
        out[t] = st[top - 1] if top > 0 else -1
        st[top] = t
        top += 1
    return out


@njit(cache=True)
def base_stats(c, a, e, from_above, zone):
    """start, ceiling, ceiling bar, floor, ceiling tests, floor tests, from_above for the base on c[a..e]."""
    if not from_above and a > 0:
        # window cut only by the 50% depth limit, but the bar before it was above the whole
        # window: the stock still came down into this base
        mx = -1.0
        for i in range(a, e + 1):
            if c[i] > mx:
                mx = c[i]
        if c[a - 1] > mx:
            from_above = True
    if from_above:
        start = a
        q = -1
        for i in range(a, e + 1):
            if c[i] < c[a] * (1 - zone):
                q = i
                break
        lo = q if q >= 0 else a
    else:
        lo = a
    ceil = -1.0
    ci = lo
    for i in range(lo, e + 1):
        if c[i] >= ceil:
            ceil = c[i]
            ci = i
    if not from_above:
        start = a
        for i in range(a, e + 1):
            if c[i] >= ceil * (1 - zone):
                start = i
                break
        lo = start
    fl = 1e18
    for i in range(start, e + 1):
        if c[i] < fl:
            fl = c[i]
    tc = 0; armed = True
    for i in range(lo, e + 1):
        if c[i] >= ceil * (1 - zone):
            if armed:
                tc += 1
                armed = False
        elif c[i] < ceil * (1 - 2 * zone):
            armed = True
    tf = 0; armf = True
    for i in range(start, e + 1):
        if c[i] <= fl * (1 + zone):
            if armf:
                tf += 1
                armf = False
        elif c[i] > fl * (1 + 2 * zone):
            armf = True
    return start, ceil, ci, fl, tc, tf, from_above


@njit(cache=True)
def recent_breakouts(c, first, dmax, zone, min_len):
    """Breakout bars t >= first (close above a whole base of >= min_len bars).
    Rows: t, start, ceiling, floor, from_above, prev_higher_idx, tests, floor_tests, ceiling_idx"""
    n = len(c)
    pg = prev_ge(c)
    s = depth_start(c, dmax)
    out = np.empty((max(n - first, 1), 9))
    k = 0
    for t in range(max(first, 2), n):
        e = t - 1
        p = pg[t]
        a = max(p + 1, s[e])
        if e - a + 1 < min_len:
            continue
        fa = p >= 0 and s[e] <= p + 1   # came down from a higher close (p = -1: never above it)
        st, ce, ci, fl, tc, tf, fa = base_stats(c, a, e, fa, zone)
        if c[t] <= ce or e - st + 1 < min_len:
            continue
        out[k, 0] = t; out[k, 1] = st; out[k, 2] = ce; out[k, 3] = fl
        out[k, 4] = 1.0 if fa else 0.0; out[k, 5] = p; out[k, 6] = tc; out[k, 7] = tf; out[k, 8] = ci
        k += 1
    return out[:k]


@njit(cache=True)
def current_bases(c, dmax, zone):
    """Bases a breakout from today would have to clear - one per possible pivot (each earlier
    closing high above today's close, nearest first). Only pivots that are the base's own
    ceiling are returned. Rows: start, pivot, floor, from_above, pivot_idx, prev_higher_idx, tests, floor_tests"""
    n = len(c)
    e = n - 1
    s = depth_start(c, dmax)[e]
    out = np.empty((64, 8))
    k = 0
    run = -1.0
    i = e
    while i >= 0 and k < 64:
        if c[i] > run:
            run = c[i]
            bj = i
            p = bj - 1
            while p >= 0 and c[p] <= run:
                p -= 1
            a = max(p + 1, s)
            if bj < a:          # fell more than dmax after this high
                break
            fa = p >= 0 and s <= p + 1
            st, ce, ci, fl, tc, tf, fa = base_stats(c, a, e, fa, zone)
            if ce == run:       # a real high made inside the base, not a bar of the decline into it
                out[k, 0] = st; out[k, 1] = run; out[k, 2] = fl; out[k, 3] = 1.0 if fa else 0.0
                out[k, 4] = bj; out[k, 5] = p; out[k, 6] = tc; out[k, 7] = tf
                k += 1
            i = p
            continue
        i -= 1
    return out[:k]


# ─────────────────────────────────────────────── score (validated in the backtest)
def base_score(length, rs, vs200, slope150, tight15, from_above, overhead, parts=False):
    """0-100. Points: base length 25 (best 200-399 days), RS 25, trend 20, right-side tightness 15,
    base built after an advance 10, little overhead supply 5."""
    pl = 25 if 200 <= length < 400 else 22 if 400 <= length < 800 else 14 if 100 <= length < 200 else \
        12 if length >= 800 else 6 if length >= 50 else 0
    rs = rs if rs is not None and np.isfinite(rs) else 0
    pr = 25 if rs >= 90 else 20 if rs >= 80 else 15 if rs >= 70 else 7 if rs >= 50 else 0
    pt = 0
    if vs200 is not None and np.isfinite(vs200) and vs200 > 0:
        pt = 20 if (slope150 is not None and np.isfinite(slope150) and slope150 > 0) else 8
    pg = 15 if tight15 <= .06 else 11 if tight15 <= .09 else 7 if tight15 <= .12 else 3 if tight15 <= .16 else 0
    py = 4 if from_above else 10
    po = 5 if overhead <= .05 else 3 if overhead <= .15 else 0
    p = [pl, pr, pt, pg, py, po]
    return p if parts else int(sum(p))


def _r(x, nd=2):
    if x is None or not np.isfinite(x):
        return None
    return round(float(x), nd)


def _px(v):
    return None if v is None or not np.isfinite(v) else (round(float(v)) if v >= 1000 else round(float(v), 1) if v >= 100 else round(float(v), 2))


def _series(p, U, bse):
    """symbol -> (dates, close, volume); an optional older history file can be put in front (Indian edition: BSE)."""
    p = p[p.symbol.isin(set(U))]
    out = {}
    for s, g in p.groupby("symbol", sort=False):
        g = g.sort_values("date")
        g = g[g.close > 0]
        src = 0
        if bse is not None and s in bse:
            b = bse[s]
            b = b[b.date < g.date.iloc[0]]
            if len(b) > 20:
                g = pd.concat([b, g], ignore_index=True)
                src = len(b)
        out[s] = (g.date.to_numpy(), g.close.to_numpy(float), g.volume.to_numpy(float), src)
    return out


def load_bse():
    f = ROOT / "cache" / "bse_backfill.csv.gz"
    if not f.exists():
        return None
    b = pd.read_csv(f, dtype={"symbol": str, "date": str})
    return {s: g.sort_values("date")[["date", "close", "volume"]] for s, g in b.groupby("symbol")}


def build(p_long, U, rs_now):
    """p_long: long daily history (symbol,date,close,volume). U: universe. rs_now: Series 1-99.

    Each stock gets ONE row: its longest base. If it also broke out of a shorter base inside
    it in the last 20 sessions, that breakout is attached as `ib` (inner breakout)."""
    import time
    t0 = time.time()
    data = _series(p_long, U, load_bse())
    window_start = str(p_long.date.min())
    rows, charts, per = [], {}, {}
    for s in U:
        if s not in data:
            continue
        d, c, v, nbse = data[s]
        n = len(c)
        if n < MIN_LEN + 2:
            continue
        e = n - 1
        sma150 = pd.Series(c).rolling(150).mean().to_numpy()
        sma200 = pd.Series(c).rolling(200).mean().to_numpy()
        vs200 = c[e] / sma200[e] - 1 if n >= 200 else np.nan
        slope = sma150[e] / sma150[e - 20] - 1 if n >= 170 else np.nan
        rs = rs_now.get(s, np.nan)

        def measure(kind, t, st, piv, fl, fa, pg, tc, tf):
            length = t - st                           # bars inside the base (breakout bar excluded)
            overhead = (c[max(0, pg - 500):pg + 1].max() / piv - 1) if pg >= 0 else 0.0
            endb = t if kind == "bo" else n           # right side measured before the breakout
            w = c[max(st, endb - 15):endb]
            tight15 = w.max() / w.min() - 1
            vb = v[st:endb]
            dry = v[max(st, endb - 10):endb].mean() / vb.mean() if vb.size and vb.mean() > 0 else np.nan
            ud = np.nan                               # up/down volume, last 50 sessions
            if endb >= 51:
                u = np.diff(c[endb - 51:endb]) > 0
                vv = v[endb - 50:endb]
                if vv[~u].sum() > 0:
                    ud = vv[u].sum() / vv[~u].sum()
            return dict(kind=kind, t=t, st=st, len=int(length), piv=piv, fl=fl, fa=fa, tc=tc, tf=tf,
                        over=overhead, tight=tight15, dry=dry, ud=ud,
                        scp=base_score(length, rs, vs200, slope, tight15, fa, overhead, parts=True))

        bo = None
        ev = recent_breakouts(c, max(n - RECENT, 2), D_MAX, ZONE, MIN_LEN)
        if len(ev):
            j = int(np.argmax(ev[:, 0] - ev[:, 1]))  # most significant: the longest base
            same = np.flatnonzero(ev[:, 1] == ev[j, 1])
            j = int(same[np.argmin(ev[same, 0])])    # ...measured on its first breakout day
            t1, st = int(ev[j, 0]), int(ev[j, 1])
            if c[t1:].min() >= ev[j, 2] * (1 - ZONE):  # not failed (no close 5%+ back inside)
                bo = measure("bo", t1, st, ev[j, 2], ev[j, 3], int(ev[j, 4]), int(ev[j, 5]), int(ev[j, 6]), int(ev[j, 7]))
        cur, near = None, None
        cb = current_bases(c, D_MAX, ZONE)
        if len(cb):
            L = e - cb[:, 0] + 1
            ok = (L >= MIN_LEN) & (cb[:, 1] >= c[e])
            if ok.any():
                j = int(np.argmax(np.where(ok, L, -1)))
                cur = measure("in", n, int(cb[j, 0]), cb[j, 1], cb[j, 2], int(cb[j, 3]), int(cb[j, 5]), int(cb[j, 6]), int(cb[j, 7]))
                ok2 = ok & (cb[:, 1] > c[e])          # next pivot above today's close
                if ok2.any():
                    k = int(np.argmin(np.where(ok2, cb[:, 1], np.inf)))
                    if k != j:
                        near = (cb[k, 1], int(L[k]))
        if bo is None and cur is None:
            continue
        for m_ in (bo, cur):
            if m_ is not None:
                m_["sc"] = int(sum(m_["scp"]))
        prim = bo if (cur is None or (bo is not None and bo["len"] >= cur["len"])) else cur
        st, piv, fl = prim["st"], prim["piv"], prim["fl"]
        dist = piv / c[e] - 1
        if prim["kind"] == "bo":
            ago, ext = e - prim["t"], c[e] / piv - 1
            status = "bo0" if ago == 0 else ("bo" if ext <= BUY_RANGE else "ext")
        else:
            ago, ext = None, None
            status = "near" if dist <= BUY_RANGE else "w15" if dist <= 0.15 else "in"
        capped = st == 0 and str(d[0]) <= window_start
        rec = {"s": s, "len": prim["len"], "start": str(d[st]), "piv": _px(piv), "fl": _px(fl),
               "depth": _r((1 - fl / piv) * 100, 1), "dist": _r(dist * 100, 1), "st": status, "ago": ago,
               "ext": _r(ext * 100, 1) if ext is not None else None,
               "type": "S" if prim["fa"] else "C", "tc": prim["tc"], "tf": prim["tf"],
               "tight": _r(prim["tight"] * 100, 1), "dry": _r(prim["dry"], 2), "ud": _r(prim["ud"], 2),
               "over": _r(prim["over"] * 100, 0), "sc": prim["sc"], "scp": prim["scp"], "tgt": _px(piv + 0.85 * (piv - fl)),
               "cap": bool(capped), "lst": bool(st == 0 and not capped), "bse": bool(nbse and st < nbse)}
        if near is not None:
            rec["np"], rec["nd"], rec["nlen"] = _px(near[0]), _r((near[0] / c[e] - 1) * 100, 1), near[1]
        if bo is not None and prim is not bo:
            rec["ib"] = {"len": bo["len"], "piv": _px(bo["piv"]), "ago": e - bo["t"], "ext": _r((c[e] / bo["piv"] - 1) * 100, 1),
                         "sc": bo["sc"], "start": str(d[bo["st"]])}
        rows.append(rec)
        per[s] = {"bl": prim["len"], "bsc": prim["sc"]}
        # chart: some bars before the base, the base, and anything since
        a0 = max(0, st - max(20, int(prim["len"] * 0.3)))
        seg = c[a0:]
        step = max(1, math.ceil(len(seg) / CHART_PTS))
        idx = list(range(len(seg) - 1, -1, -step))[::-1]
        ch = {"c": [_px(seg[i]) for i in idx], "s": int(np.searchsorted(idx, st - a0)), "d0": str(d[a0]), "k": step}
        if prim["kind"] == "bo":
            ch["b"] = int(np.searchsorted(idx, prim["t"] - a0))
        elif bo is not None:
            ch["ib"] = int(np.searchsorted(idx, bo["t"] - a0))
            ch["ibs"] = int(np.searchsorted(idx, bo["st"] - a0))
            ch["ibp"] = _px(bo["piv"])
        charts[s] = ch
    ev_file = CFG / "base_evidence.json"
    evidence = json.loads(ev_file.read_text()) if ev_file.exists() else None
    print(f"bases: {len(rows)} stocks in a base or fresh breakout, {time.time() - t0:.0f}s")
    return {"rows": rows, "charts": charts, "per_stock": per, "evidence": evidence}


def scans(B):
    """Three scanners for the Scanners tab (same format as scanners.py)."""
    if not B["rows"]:
        return [], {}, {}
    R = pd.DataFrame(B["rows"]).set_index("s")
    # breakout from a long base: the row itself, or an inner breakout of a still longer base
    ib = R["ib"] if "ib" in R else pd.Series(None, index=R.index)
    bo_len = pd.Series({s: (R.at[s, "len"] if R.at[s, "st"] in ("bo0", "bo") else
                            (ib[s]["len"] if isinstance(ib[s], dict) and ib[s]["ext"] <= BUY_RANGE * 100 else 0)) for s in R.index})
    hits, metric, meta = {}, {}, []

    def add(id_, name, desc, mask, m_label, m_vals, fmt="int", sort="metric"):
        syms = sorted(R.index[mask.fillna(False)])
        hits[id_] = syms
        metric[id_] = {s: (None if pd.isna(m_vals[s]) else float(m_vals[s])) for s in syms}
        meta.append({"id": id_, "name": name, "cat": "Bases", "desc": desc, "metric": m_label, "fmt": fmt, "sort": sort})

    add("lb_breakout", "Long-base breakout",
        "Closed above the pivot of a base of 100+ trading days in the last 20 sessions and is still within 5% of it "
        "(the buy range). In 20 years of US data, bases of 50-199 days in an uptrend did best; 400+ day bases lagged "
        "the S&P 500 even though fewer of them failed - favour RS 90+ breakouts.",
        bo_len >= 100, "Base days", bo_len)
    add("lb_near", "Long base near pivot",
        "In a base of 200+ trading days and within 5% below its pivot - watch for the breakout.",
        (R.st == "near") & (R.len >= 200), "Base days", R.len)
    add("lb_best", "Big base, top Base Score",
        "Base of 200+ trading days with a Base Score of 70+ (uptrend, RS, tight right side, base built after an advance) and "
        "no more than 15% below its pivot. In the US backtest a Base Score of 70+ cut failed breakouts (30% vs 39%) but "
        "did not add return - use it as a reliability check alongside RS.",
        (R.len >= 200) & (R.sc >= 70) & (R.st != "ext") & (R.dist <= 15), "Base Score", R.sc)
    return meta, hits, metric
