"""Technical scanner library for PKC Sector Radar US.

Every scanner works on wide DataFrames (rows = trading days, columns = symbols)
and returns a boolean Series for the latest day, plus an optional metric to
show next to each hit. Definitions follow the published versions of each setup
(sources noted in SCANS descriptions) and are deliberately strict: a scanner
that lists 300 stocks is noise.

Used by compute.py:  run_all(ctx) -> (meta list, hits dict, metrics dict)
"""
import numpy as np
import pandas as pd


# ───────────────────────────────────────────────────────────── indicators
def wilder(x, n):
    return x.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def rsi(C, n=14):
    d = C.diff()
    up, dn = d.clip(lower=0), (-d).clip(lower=0)
    rs = wilder(up, n) / wilder(dn, n)
    return 100 - 100 / (1 + rs)


def true_range(H, L, C):
    pc = C.shift(1)
    tr = np.fmax(np.fmax((H - L).to_numpy(), (H - pc).abs().to_numpy()), (L - pc).abs().to_numpy())
    return pd.DataFrame(tr, index=C.index, columns=C.columns)


def adx(H, L, C, n=14):
    up, dn = H.diff(), -L.diff()
    pdm = up.where((up > dn) & (up > 0), 0.0)
    ndm = dn.where((dn > up) & (dn > 0), 0.0)
    atr = wilder(true_range(H, L, C), n)
    pdi = 100 * wilder(pdm, n) / atr
    ndi = 100 * wilder(ndm, n) / atr
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi)
    return wilder(dx, n), pdi, ndi


def supertrend_dir(H, L, C, n=10, mult=3.0):
    """+1 = uptrend, -1 = downtrend (standard ATR(RMA) supertrend)."""
    atr = wilder(true_range(H, L, C), n).to_numpy()
    hl2 = ((H + L) / 2).to_numpy()
    c = C.to_numpy()
    ub, lb = hl2 + mult * atr, hl2 - mult * atr
    T, N = c.shape
    fu, fl = np.full((T, N), np.nan), np.full((T, N), np.nan)
    d = np.zeros((T, N))
    for t in range(T):
        if t == 0:
            fu[t], fl[t], d[t] = ub[t], lb[t], 1
            continue
        pfu, pfl, pc = fu[t - 1], fl[t - 1], c[t - 1]
        fu[t] = np.where(np.isnan(pfu) | (ub[t] < pfu) | (pc > pfu), ub[t], pfu)
        fl[t] = np.where(np.isnan(pfl) | (lb[t] > pfl) | (pc < pfl), lb[t], pfl)
        pd_ = d[t - 1]
        nd = np.where((pd_ <= 0) & (c[t] > pfu), 1, np.where((pd_ >= 0) & (c[t] < pfl), -1, pd_))
        nd = np.where(nd == 0, 1, nd)
        d[t] = np.where(np.isnan(atr[t]) | np.isnan(c[t]), pd_, nd)
    return pd.DataFrame(d, index=C.index, columns=C.columns)


def crossed_up(a, b, k=1):
    """a crossed above b within the last k sessions (b may be a frame or scalar)."""
    prev_a, prev_b = a.shift(1), (b.shift(1) if isinstance(b, pd.DataFrame) else b)
    cond = (a > b) & (prev_a <= prev_b)
    return cond.iloc[-k:].any()


def crossed_down(a, b, k=1):
    prev_a, prev_b = a.shift(1), (b.shift(1) if isinstance(b, pd.DataFrame) else b)
    cond = (a < b) & (prev_a >= prev_b)
    return cond.iloc[-k:].any()


# ───────────────────────────────────────────────────────────── scanner set
CATS = ["Confluence", "Momentum", "Breakouts", "Volume", "Trend & pullbacks", "Price action", "Oscillators",
        "Relative strength", "Weakness"]

# Broad "state" lists left out of the confluence count.
NOT_COUNTED = {"trend_template", "adx_strong", "leaders", "high_octane", "strong_3612"}


def run_all(x):
    """x: dict of wide frames / series prepared in compute.py.

    Required keys: O,H,L,C,V (wide, current universe), bench (Series), sma (dict n->frame),
    ema20, ema50, rs_now, rs_prev, hi52, lo52, avgv50, vr (Series)
    """
    O, H, L, C, V = x["O"], x["H"], x["L"], x["C"], x["V"]
    sma, ema20, ema50 = x["sma"], x["ema20"], x["ema50"]
    c, o, h, l, v = C.iloc[-1], O.iloc[-1], H.iloc[-1], L.iloc[-1], V.iloc[-1]
    pc, ph, pl, pv = C.iloc[-2], H.iloc[-2], L.iloc[-2], V.iloc[-2]
    d1 = c / pc - 1
    vr = x["vr"]
    rs_now, rs_prev = x["rs_now"], x["rs_prev"]
    hi52, lo52 = x["hi52"].iloc[-1], x["lo52"].iloc[-1]
    s10 = C.rolling(10, min_periods=10).mean()
    s20, s50, s150, s200 = (sma[n] for n in (20, 50, 150, 200))
    s50l, s150l, s200l = s50.iloc[-1], s150.iloc[-1], s200.iloc[-1]
    bars = C.notna().sum()
    uptrend = (c > s50l) & (s50l > s150l) & (s150l > s200l)   # Stage-2 stack
    pct_from_hi = (c / hi52 - 1) * 100

    # indicators
    R = rsi(C)
    rsi_l, rsi_p = R.iloc[-1], R.iloc[-2]
    TR = true_range(H, L, C)
    atr14 = wilder(TR, 14)
    atrp = atr14.iloc[-1] / c * 100
    A, PDI, NDI = adx(H, L, C)
    adx_l = A.iloc[-1]
    macd = C.ewm(span=12, adjust=False).mean() - C.ewm(span=26, adjust=False).mean()
    sig = macd.ewm(span=9, adjust=False).mean()
    bb_mid, bb_sd = s20, C.rolling(20, min_periods=20).std()
    bb_up = bb_mid + 2 * bb_sd
    bb_w = (4 * bb_sd) / bb_mid
    st = supertrend_dir(H, L, C)
    rng = (H - L)
    rsl = C.div(x["bench"], axis=0)                       # RS line vs benchmark
    e10, e21 = C.ewm(span=10, adjust=False).mean(), C.ewm(span=21, adjust=False).mean()
    e10l, e21l = e10.iloc[-1], e21.iloc[-1]
    rng_l = (h - l)
    dcr = ((c - l) / rng_l.where(rng_l > 0) * 100).fillna(50)   # daily closing range, 0 = low, 100 = high
    adr = ((H / L - 1) * 100).iloc[-20:].mean()                  # average daily range %, 20 sessions
    atr_prev = atr14.iloc[-2]
    first_bar = C.apply(lambda s_: s_.first_valid_index())
    pos = {d: i for i, d in enumerate(C.index)}
    age = pd.Series({k: (len(C) - 1 - pos[d]) if d is not None else np.nan for k, d in first_bar.items()})
    young = age < 252                                            # listed within the last year

    hits, metric, meta = {}, {}, []

    def add(id_, name, cat, desc, mask, m_label=None, m_vals=None, m_fmt="num", sort="rs"):
        mask = mask.fillna(False).astype(bool)
        syms = sorted(mask.index[mask])
        hits[id_] = syms
        if m_label is not None:
            mv = m_vals.reindex(syms)
            metric[id_] = {s: (None if pd.isna(mv[s]) else round(float(mv[s]), 2)) for s in syms}
        meta.append({"id": id_, "name": name, "cat": cat, "desc": desc, "metric": m_label,
                     "fmt": m_fmt, "sort": sort})

    # ── Momentum
    add("up4", "4% breakout day", "Momentum",
        "Up 4% or more today on volume above yesterday's — Pradeep Bonde's momentum-burst trigger.",
        (d1 >= 0.04) & (v > pv), "Day %", d1 * 100, "pct", "metric")
    add("burst20", "Momentum burst: +20% in 5 days", "Momentum",
        "Gained 20% or more over the last 5 sessions. Fast movers; check for news before chasing.",
        (C.iloc[-1] / C.iloc[-6] - 1) >= 0.20, "5-day %", (C.iloc[-1] / C.iloc[-6] - 1) * 100, "pct", "metric")
    gap = (o / pc - 1)
    add("gap_up", "Gap up held", "Momentum",
        "Opened 2%+ above yesterday's close, never filled yesterday's high, and closed above the open.",
        (gap >= 0.02) & (l > ph) & (c > o), "Gap %", gap * 100, "pct", "metric")
    add("rs_jump", "RS rating jump", "Momentum",
        "RS rating up 15+ points in a week to 70 or higher — fresh relative-strength surge.",
        (rs_now - rs_prev >= 15) & (rs_now >= 70), "RS Δ1W", rs_now - rs_prev, "num", "metric")
    add("leaders", "Leaders near highs", "Momentum",
        "RS 90+ and within 5% of the 52-week high.", (rs_now >= 90) & (c >= 0.95 * hi52))
    lowp = L.iloc[-10:].gt(e21.iloc[-10:]).all()
    add("power_trend", "Power trend", "Momentum",
        "Low above the 21 EMA for 10 straight sessions, 21 EMA above the 50 DMA, 50 DMA rising for 5 sessions, "
        "and an up close today (Morales & Kacher). The strongest kind of trend — buy pullbacks, don't short it.",
        lowp & (e21l > s50l) & (s50l > s50.iloc[-6]) & (c > pc), "ADR %", adr, "pct")
    r15 = C.iloc[-1] / C.iloc[-16] - 1
    ups15 = (C.diff() > 0).iloc[-15:].sum()
    v15 = V.iloc[-15:].mean() / V.iloc[-65:-15].mean()
    add("ants", "Ants (steady accumulation)", "Momentum",
        "Up on 12 or more of the last 15 sessions, +20% over those 15 sessions, with average volume 20%+ above "
        "the prior 50 sessions (Morales & Kacher) — institutions buying day after day.",
        (ups15 >= 12) & (r15 >= 0.20) & (v15 >= 1.2), "15-day %", r15 * 100, "pct", "metric")
    add("high_octane", "High-octane leaders", "Momentum",
        "Average daily range 5% or more, RS 90+, above the 21 EMA and 50 DMA and within 10% of the 52-week high — "
        "the fast movers swing traders want (Deepvue 'High Octane RS'; same thresholds).",
        (adr >= 5) & (rs_now >= 90) & (c > e21l) & (c > s50l) & (pct_from_hi >= -10), "ADR %", adr, "pct", "metric")
    rr3, rr6, rr12 = (C.iloc[-1] / C.iloc[-n - 1] - 1 for n in (63, 126, 252))
    top = lambda r_: r_.rank(pct=True) >= 0.90
    add("strong_3612", "Swing leaders: 3, 6 and 12 months", "Momentum",
        "In the top 10% of the universe on 3-month, 6-month AND 12-month return, and above the 21 EMA "
        "(Mike Webster's swing-trading list).",
        top(rr3) & top(rr6) & top(rr12) & (c > e21l), "3M %", rr3 * 100, "pct", "metric")
    ep_gap = o / pc - 1
    add("episodic_pivot", "Episodic pivot", "Momentum",
        "Gapped up 10% or more on at least 3× average volume and held the gap (close no more than 2% under the "
        "open), after a quiet quarter (not up more than 30% in the prior 3 months). Pradeep Bonde's EP — "
        "usually news-driven; check the news.",
        (ep_gap >= 0.10) & (vr >= 3) & (c >= o * 0.98) & ((C.iloc[-2] / C.iloc[-65] - 1) <= 0.30),
        "Gap %", ep_gap * 100, "pct", "metric")
    gap_atr = (o - ph) / atr_prev
    add("bgu", "Buyable gap up", "Momentum",
        "Opened above yesterday's high by at least 0.75 × ATR, volume 1.5× average or more, closed in the upper half "
        "of the day's range and not below the open by more than 1% (Gil Morales). Stop just under the gap-day low.",
        (gap_atr >= 0.75) & (vr >= 1.5) & (dcr >= 50) & (c >= o * 0.99), "Gap / ATR", gap_atr, "x", "metric")

    # ── Breakouts
    prior_hi = C.iloc[-253:-1].max()
    add("high52", "New 52-week closing high", "Breakouts",
        "Closed above the highest close of the previous 252 sessions.",
        (c > prior_hi) & (bars >= 200), "Vol ×", vr, "x")
    add("high2y", "Highest close in 2 years", "Breakouts",
        "Today's close is the highest of the full ~2-year history held for the site.",
        (c >= C.max()) & (bars >= 400), "Vol ×", vr, "x")
    add("high52_dcr", "52-week high, strong close", "Breakouts",
        "New 52-week closing high with the close in the top third of the day's range — buyers held it into the "
        "bell (Deepvue's DCR rule).",
        (c > prior_hi) & (bars >= 200) & (dcr >= 67), "Close range %", dcr, "num")
    ath_prior = x.get("ath_prior")
    if ath_prior is not None:
        add("ath", "Green line breakout (all-time high)", "Breakouts",
            "Closed above the highest price in the stock's entire listed history, on volume at least 1.2× average "
            "and a close in the upper half of the day's range. No overhead supply at all.",
            (c > ath_prior.reindex(c.index)) & (vr >= 1.2) & (dcr >= 50), "Vol ×", vr, "x")
    Wk = C.copy()
    Wk.index = pd.to_datetime(Wk.index)
    wk = Wk.resample("W-FRI").last().iloc[-3:]
    w3 = (wk.max() / wk.min() - 1) * 100
    add("tight3w", "3 weeks tight", "Breakouts",
        "The last three weekly closes (this week's so far included) within 1.5% of each other, in a Stage-2 "
        "uptrend (William O'Neil). Buy point: above the high of the tight area.",
        (w3 <= 1.5) & uptrend, "3-week spread %", w3, "pct", "metric_asc")
    mx = np.fmax(np.fmax(e10l, e21l), s50l)
    mn = np.fmin(np.fmin(e10l, e21l), s50l)
    spread = (mx / mn - 1) * 100
    add("launchpad", "Launch pad", "Breakouts",
        "10 EMA, 21 EMA and 50 DMA bunched within 2% of each other with price above the 21 EMA, 50 DMA above the "
        "200 DMA and within 10% of the 52-week high — energy stored before a move (Deepvue).",
        (spread <= 2) & (c > e21l) & (s50l > s200l) & (pct_from_hi >= -10), "MA spread %", spread, "pct", "metric_asc")
    ipo_hi = pd.Series({k: H[k].iloc[-int(age[k]) - 1:-1].max() if young.get(k) and age[k] >= 10 else np.nan
                        for k in C.columns})
    add("ipo_bo", "IPO high breakout", "Breakouts",
        "Listed within the last year (10+ sessions ago) and closed above its highest price since listing, on 1.5× "
        "average volume — new leaders often start here.",
        young & (c > ipo_hi) & (vr >= 1.5), "Days listed", age, "int")
    box_top, box_bot = H.iloc[-21:-1].max(), L.iloc[-21:-1].min()
    add("darvas", "Darvas box breakout", "Breakouts",
        "Prior 20-day range no wider than 15%, closed above the box top on 1.5× average volume.",
        ((box_top / box_bot - 1) <= 0.15) & (c > box_top) & (vr >= 1.5), "Box %", (box_top / box_bot - 1) * 100, "pct")
    add("bb_break", "Bollinger band breakout", "Breakouts",
        "Closed above the upper Bollinger band (20, 2) on 1.5× average volume.",
        (c > bb_up.iloc[-1]) & (vr >= 1.5), "Vol ×", vr, "x")
    cross150 = crossed_up(C, s150, 5)
    s150_flat_up = s150l >= s150.iloc[-21] * 0.995
    add("stage2_bo", "Stage 2 breakout (Weinstein)", "Breakouts",
        "Crossed above the 30-week (150-day) average in the last 5 sessions, average flat or rising, close above 50 DMA.",
        cross150 & s150_flat_up & (c > s50l), "Vol ×", vr, "x")
    def win_range(a, b):
        hh = H.iloc[-a:-b] if b else H.iloc[-a:]
        ll = L.iloc[-a:-b] if b else L.iloc[-a:]
        return (hh.max() / ll.min() - 1) * 100
    r1, r2_, r3 = win_range(10, 0), win_range(20, 10), win_range(40, 20)
    vdry = V.iloc[-10:].mean() / V.iloc[-60:-10].mean()
    add("vcp", "Volatility contraction (VCP)", "Breakouts",
        "Stage-2 trend within 15% of the 52-week high, with three successively tighter ranges "
        "(sessions 40–20, 20–10, last 10), the last one 10% or less, and volume drying up. "
        "Minervini's VCP — watch for the breakout above the tight range.",
        uptrend & (pct_from_hi >= -15) & (r3 > r2_) & (r2_ > r1) & (r1 <= 10) & (vdry < 1),
        "Last 10d range %", r1, "pct")

    # ── Volume
    add("vol_spike", "Volume spike up", "Volume",
        "Volume at least 3× the 50-day average and price up 2% or more today.",
        (vr >= 3) & (d1 >= 0.02), "Vol ×", vr, "x", "metric")
    down_vol10 = V.where(C < C.shift(1)).iloc[-11:-1].max()
    add("pocket_pivot", "Pocket pivot", "Volume",
        "Up day whose volume beats the largest down-day volume of the prior 10 sessions, closing above the 10 and 50 DMA "
        "within 15% of the 52-week high (Morales & Kacher).",
        (c > pc) & (v > down_vol10) & (c > s10.iloc[-1]) & (c > s50l) & (pct_from_hi >= -15), "Vol ×", vr, "x")
    add("vol_high", "Highest volume in a year (up day)", "Volume",
        "Today's volume is the largest of the past 252 sessions and the stock closed up.",
        (v >= V.iloc[-252:].max()) & (d1 > 0) & (bars >= 200), "Vol ×", vr, "x", "metric")
    v5 = V.iloc[-5:].mean() / x["avgv50"].iloc[-1]
    add("dryup", "Volume dry-up near highs", "Volume",
        "5-day average volume ≤ 50% of the 50-day average while the stock holds within 10% of its high above the 50 DMA — supply exhausted.",
        (v5 <= 0.5) & (pct_from_hi >= -10) & (c > s50l), "5d vol ×", v5, "x")

    # ── Trend & pullbacks
    add("trend_template", "Trend template (Minervini)", "Trend & pullbacks",
        "Passes all 8 Minervini trend conditions with RS ≥ 70.", x["tt"])
    add("golden", "Golden cross", "Trend & pullbacks",
        "50 DMA crossed above the 200 DMA in the last 5 sessions.", crossed_up(s50, s200, 5))
    add("ema_up", "20 EMA crossed above 50 EMA", "Trend & pullbacks",
        "Bullish crossover in the last 3 sessions, price above the 20 EMA.",
        crossed_up(ema20, ema50, 3) & (c > ema20.iloc[-1]))
    e20 = ema20.iloc[-1]
    add("pb20", "Pullback to 20 EMA in uptrend", "Trend & pullbacks",
        "Stage-2 stock with RS ≥ 70 whose low tagged the 20 EMA (within 1%) today and closed above it.",
        uptrend & (rs_now >= 70) & (l <= e20 * 1.01) & (c >= e20), "Dist to 20EMA %", (c / e20 - 1) * 100, "pct")
    add("pb50", "Pullback to 50 DMA in uptrend", "Trend & pullbacks",
        "Stage-2 stock with RS ≥ 70 whose low tagged the 50 DMA (within 1.5%) and closed above it.",
        uptrend & (rs_now >= 70) & (l <= s50l * 1.015) & (c > s50l), "Dist to 50DMA %", (c / s50l - 1) * 100, "pct")
    add("supertrend", "Supertrend turned bullish", "Trend & pullbacks",
        "Supertrend (10, 3) flipped from sell to buy in the last 2 sessions.",
        (st.iloc[-1] > 0) & (st.iloc[-3:-1] < 0).any())
    add("adx_strong", "Strong trend (ADX)", "Trend & pullbacks",
        "ADX(14) ≥ 25 and rising over 5 sessions with +DI above −DI — a trend with real strength.",
        (adx_l >= 25) & (adx_l > A.iloc[-6]) & (PDI.iloc[-1] > NDI.iloc[-1]), "ADX", adx_l, "num", "metric")
    nr7 = rng.iloc[-1] <= rng.iloc[-7:].min()
    add("nr7", "NR7 in uptrend", "Trend & pullbacks",
        "Narrowest daily range of the last 7 sessions in a Stage-2 stock (Crabel) — a volatility pause that often precedes expansion.",
        nr7 & uptrend, "Range %", rng.iloc[-1] / c * 100, "pct")
    add("inside", "Inside day in uptrend", "Trend & pullbacks",
        "Today's high and low sit inside yesterday's range, in a Stage-2 stock with RS ≥ 70.",
        (h <= ph) & (l >= pl) & uptrend & (rs_now >= 70))
    add("power3", "Power of 3", "Trend & pullbacks",
        "In one up session the stock started below the 10 EMA, 21 EMA and 50 DMA (low under all three) and closed "
        "above all three — a decisive reclaim (Deepvue).",
        (l <= mn) & (c > mx) & (c > pc), "Vol ×", vr, "x")
    below21 = C.iloc[-6:-1].lt(e21.iloc[-6:-1]).sum()
    add("wedge_pop", "EMA reclaim (wedge pop)", "Trend & pullbacks",
        "Closed back above the 10 and 21 EMA after at least 3 of the previous 5 closes below the 21 EMA, on 1.5× "
        "average volume, with the 50 DMA above the 200 DMA (Oliver Kell's wedge pop / EMA crossback).",
        (below21 >= 3) & (c > e10l) & (c > e21l) & (vr >= 1.5) & (s50l > s200l), "Vol ×", vr, "x", "metric")

    # ── Price action (single- and two-bar signals)
    add("outside_day", "Bullish outside day", "Price action",
        "Today's range engulfs yesterday's (higher high and lower low), closed up, in the top 30% of the range, "
        "on above-average volume — a reversal of control to buyers.",
        (h > ph) & (l < pl) & (c > pc) & (dcr >= 70) & (vr >= 1), "Close range %", dcr, "num")
    add("open_low", "Open = low, strong close", "Price action",
        "Opened at the day's low (no dip below the open) and closed 2%+ above it on above-average volume — "
        "buyers in control from the first trade.",
        ((o - l).abs() <= o * 0.0005) & (c >= o * 1.02) & (vr >= 1), "Day %", d1 * 100, "pct", "metric")
    body = (c - o).abs()
    lw, uw = np.fmin(o, c) - l, h - np.fmax(o, c)
    add("hammer50", "Hammer at the 50 DMA", "Price action",
        "Hammer candle (lower wick at least twice the body, small upper wick) whose low tested the 50 DMA and "
        "closed above it, in a Stage-2 uptrend — support confirmed.",
        (lw >= 2 * body) & (uw <= body) & (l <= s50l * 1.01) & (c > s50l) & uptrend,
        "Dist to 50DMA %", (c / s50l - 1) * 100, "pct")
    ins = (H < H.shift(1)) & (L > L.shift(1))
    add("inside2", "Double inside day", "Price action",
        "Two inside days in a row in a Stage-2 uptrend — a tighter coil than a single inside day. Buy the break "
        "of the mother bar's high.", ins.iloc[-1] & ins.iloc[-2] & uptrend, "Range %", rng.iloc[-1] / c * 100, "pct")
    dn3 = (C.diff() < 0).iloc[-4:-1].sum()
    add("bull_snort", "Bull snort", "Price action",
        "After 2+ down closes in the prior 3 sessions, a 3%+ up day on at least 2× average volume closing in the "
        "top quarter of its range, above the 200 DMA (Oliver Kell) — a forceful turn.",
        (dn3 >= 2) & (d1 >= 0.03) & (vr >= 2) & (dcr >= 75) & (c > s200l), "Vol ×", vr, "x", "metric")

    # ── Oscillators
    add("rsi60", "RSI crossed above 60", "Oscillators",
        "RSI(14) crossed up through 60 today with price above the 50 DMA — momentum regime shift (Andrew Cardwell's range rules).",
        (rsi_l > 60) & (rsi_p <= 60) & (c > s50l), "RSI", rsi_l, "num")
    add("rsi30", "RSI oversold bounce", "Oscillators",
        "RSI(14) crossed back above 30 in the last 2 sessions while the stock is still above its 200 DMA — "
        "a pullback in a long-term uptrend, not a falling knife.",
        crossed_up(R, 30, 2) & (c > s200l), "RSI", rsi_l, "num")
    add("macd_bull", "MACD bullish cross above zero", "Oscillators",
        "MACD (12, 26) crossed above its 9-day signal in the last 2 sessions while above the zero line.",
        crossed_up(macd, sig, 2) & (macd.iloc[-1] > 0))
    add("squeeze", "Bollinger squeeze", "Oscillators",
        "Bollinger band width at its narrowest in 120 sessions with price above the 50 DMA — coiled for a move (John Bollinger).",
        (bb_w.iloc[-1] <= bb_w.iloc[-120:].min() * 1.0001) & (c > s50l) & (bars >= 150), "BB width %", bb_w.iloc[-1] * 100, "pct")

    # ── Relative strength
    rsl_hi = rsl.iloc[-1] >= rsl.iloc[-252:].max()
    for n_, lab, nm in ((21, "1m", "1-month"), (63, "3m", "3-month"), (126, "6m", "6-month")):
        add(f"rsnh_{lab}", f"RS line new {nm} high before price", "Relative strength",
            f"RS line vs the S&P 500 at a new {nm} high while price is still 2%+ below its own {nm} closing high, "
            "RS rating 70+ — strength showing before the breakout (Deepvue 'RS New Highs Before Price').",
            (rsl.iloc[-1] >= rsl.iloc[-n_:].max()) & (c < 0.98 * C.iloc[-n_:].max()) & (rs_now >= 70),
            "From 52wH %", pct_from_hi, "pct")
    add("rsline_lead", "RS line new 12-month high before price", "Relative strength",
        "The stock's relative-strength line vs the S&P 500 hit a 52-week high while price is still below its own high — "
        "a classic leadership tell (O'Neil's 'blue dot').",
        rsl_hi & (c < 0.98 * hi52) & (bars >= 200), "From 52wH %", pct_from_hi, "pct")
    add("rs_up_mkt_down", "Up while market fell", "Relative strength",
        "Up 1% or more today while the S&P 500 fell, RS ≥ 80 and volume above average — buying under pressure.",
        (d1 >= 0.01) & (x["bench"].iloc[-1] < x["bench"].iloc[-2]) & (rs_now >= 80) & (vr >= 1),
        "Day %", d1 * 100, "pct", "metric")

    # ── Weakness
    add("down4", "4% breakdown day", "Weakness",
        "Down 4% or more today on volume above yesterday's. Exit check on holdings.",
        (d1 <= -0.04) & (v > pv), "Day %", d1 * 100, "pct", "metric_asc")
    add("leader_down", "Leader down on volume", "Weakness",
        "RS 80+ stock down 2% or more on at least 1.5× average volume — distribution in a leader. Check if you hold it.",
        (d1 <= -0.02) & (vr >= 1.5) & (rs_now >= 80), "Day %", d1 * 100, "pct", "metric_asc")
    add("ema_down", "20 EMA crossed below 50 EMA", "Weakness",
        "Bearish crossover in the last 3 sessions, price below the 20 EMA.",
        crossed_down(ema20, ema50, 3) & (c < e20))
    add("below200", "Broke below 200 DMA on volume", "Weakness",
        "Crossed below the 200 DMA in the last 3 sessions, today's volume ≥ 1.5× average.",
        crossed_down(C, s200, 3) & (c < s200l) & (vr >= 1.5), "Vol ×", vr, "x")
    add("death", "Death cross", "Weakness",
        "50 DMA crossed below the 200 DMA in the last 5 sessions.", crossed_down(s50, s200, 5))
    add("low52", "New 52-week closing low", "Weakness",
        "Closed below the lowest close of the previous 252 sessions.",
        (c < C.iloc[-253:-1].min()) & (bars >= 200))
    add("gap_down", "Gap down not filled", "Weakness",
        "Opened 2%+ below yesterday's close, stayed below yesterday's low, and closed below the open.",
        (gap <= -0.02) & (h < pl) & (c < o), "Gap %", gap * 100, "pct", "metric_asc")

    # ── Confluence: in how many different signal GROUPS (categories) is each stock today?
    # Counting categories rather than scans stops one big up-day (which trips 4% day, volume spike,
    # pocket pivot, Bollinger, RSI…) from looking like broad confirmation.
    cat_of = {m["id"]: m["cat"] for m in meta}
    groups_hit = {}
    for i, syms_ in hits.items():
        if cat_of[i] in ("Weakness", "Confluence") or i in NOT_COUNTED:
            continue
        for sym in syms_:
            groups_hit.setdefault(sym, set()).add(cat_of[i])
    cnt = pd.Series({k: float(len(groups_hit.get(k, ()))) for k in c.index})
    n_groups = len([k for k in CATS if k not in ("Weakness", "Confluence")])
    add("combo3", "Confluence: 4+ signal groups", "Confluence",
        f"Stocks firing bullish scans in at least 4 of the {n_groups} groups (momentum, breakouts, volume, trend, "
        "price action, oscillators, relative strength) today — independent confirmation, the day's strongest "
        "shortlist. Broad lists (trend template, ADX, leaders near highs, high-octane, swing leaders) don't count. "
        "Open a stock to see every scan it is in.",
        cnt >= 4, "Groups", cnt, "int", "metric")
    meta.insert(0, meta.pop())        # show it first

    per_stock = {"rsi": rsi_l, "adx": adx_l, "atrp": atrp, "dcr": dcr, "adr": adr, "nsc": cnt}
    return meta, hits, metric, per_stock
