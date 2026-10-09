"""Turn cache/prices.csv.gz into the small JSON files the website reads.

Writes docs/data/:
  meta.json     date, source, universe counts
  stocks.json   one row per stock AND per ETF (k:"e"): returns for every period (now and 1 week
                ago), RS rating, 52W distance, DMA flags, trend template, stage, turnover, volume ratio
  groups.json   membership lists for Themes / Sector / Industry / Index, and the ETF sets
  breadth.json  daily market-breadth history (stocks only)
  scans.json    today's scanner hits

Theme/sector averages are computed in the browser from stocks.json, so every
period x grouping x (mean/median) combination is available without extra files.

Run:  python scripts/compute.py
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import scanners as SC  # noqa: E402
from yahoo import ET  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE, CFG, OUT = ROOT / "cache", ROOT / "config", ROOT / "docs" / "data"
S = json.loads((CFG / "settings.json").read_text())
PERIODS = {"1D": 1, "1W": 5, "1M": 21, "3M": 63, "6M": 126, "YTD": None, "1Y": 252}
LAG = 5  # "1 week ago" snapshot for rank-change arrows


def r2(x, nd=2):
    if x is None or (isinstance(x, float) and not np.isfinite(x)):
        return None
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, nd) if np.isfinite(x) else None


LONG = None  # full download (about 6 years) for the long-base identifier


def load():
    global LONG
    p = pd.read_csv(CACHE / "prices.csv.gz", dtype={"symbol": str, "date": str})
    u = pd.read_csv(CACHE / "universe.csv", dtype={"symbol": str}, keep_default_na=False)
    LONG = p[["symbol", "date", "close", "volume"]]
    # everything except bases.py works on the usual window, exactly as before the longer download
    cut = (datetime.now(ET).date() - timedelta(days=S["history_calendar_days"])).isoformat()
    p = p[p.date >= cut]
    bench = S["benchmark"]
    cal = sorted(p.loc[p.symbol == bench, "date"].unique()) or sorted(p.date.unique())
    # Use the benchmark's trading calendar; drop stray non-session dates.
    p = p[p.date.isin(set(cal))]
    wide = {f: p.pivot_table(index="date", columns="symbol", values=f, aggfunc="last").reindex(cal)
            for f in ["open", "high", "low", "close", "volume"]}
    return wide, u


def period_return(C, n, end=-1):
    """Return over n sessions ending at row `end` (negative index)."""
    if len(C) < n + 1 - end:
        return pd.Series(np.nan, index=C.columns)
    return C.iloc[end] / C.iloc[end - n] - 1


def ytd_return(C, end=-1):
    d = C.index[end]
    prev = C.index[C.index < f"{d[:4]}-01-01"]
    if not len(prev):
        return pd.Series(np.nan, index=C.columns)
    return C.iloc[end] / C.loc[prev[-1]] - 1


def rs_score(C, end=-1):
    """IBD-style: 40% weight on last quarter, 20% each on the three before."""
    parts, w = [], [0.4, 0.2, 0.2, 0.2]
    for n in (63, 126, 189, 252):
        parts.append(period_return(C, n, end) + 1)
    sc = sum(wi * pi for wi, pi in zip(w, parts))
    # Young listings (<1y): use what exists, renormalised.
    avail = sum(wi * pi.notna() for wi, pi in zip(w, parts))
    part_sum = sum(wi * pi.fillna(0) for wi, pi in zip(w, parts))
    sc = sc.fillna(part_sum / avail.replace(0, np.nan))
    return sc


def pct_rank(s):
    return (s.rank(pct=True) * 98 + 1).round()


def rank_against(score, ref):
    """1-99 rating of `score` measured against the distribution of `ref` (used to rate ETFs on the
    same scale as stocks: an ETF with RS 80 has out-performed 80% of stocks)."""
    r = np.sort(ref.dropna().to_numpy())
    if not len(r):
        return pd.Series(np.nan, index=score.index)
    p = np.searchsorted(r, score.to_numpy(), side="right") / len(r)
    out = pd.Series(np.round(p * 98 + 1), index=score.index)
    return out.where(score.notna())


def market_caps(c):
    """Market cap in $ millions from the Yahoo quote snapshot, rolled forward to today's close.
    The last good copy is kept in config/mcap_us.csv for days the quote service is unavailable."""
    saved = CFG / "mcap_us.csv"
    m = None
    qf = CACHE / "quotes.csv"
    if qf.exists():
        q = pd.read_csv(qf, index_col=0)
        px = pd.to_numeric(q.get("regularMarketPrice"), errors="coerce")
        mc = pd.to_numeric(q.get("marketCap"), errors="coerce") / 1e6
        m = pd.DataFrame({"Symbol": q.index, "MarketCapM": mc.round(1), "Close": px}).dropna()
        m = m[m.MarketCapM > 0]
        if len(m) > 1000:
            m.assign(Date=datetime.now(ET).date().isoformat()).to_csv(saved, index=False)
        m = m.set_index("Symbol")
    if (m is None or len(m) < 1000) and saved.exists():
        print("market cap: using saved copy")
        m = pd.read_csv(saved).set_index("Symbol")
    if m is None:
        return pd.Series(dtype=float)
    return (m["MarketCapM"] * (c.reindex(m.index) / m["Close"]).fillna(1.0)).dropna()


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    W, u = load()
    bench = S["benchmark"]
    stocks = u[u.kind == "stock"].set_index("symbol")
    etfs_u = u[u.kind == "etf"].set_index("symbol")
    syms = [s for s in stocks.index if s in W["close"].columns]
    esyms = [s for s in etfs_u.index if s in W["close"].columns]
    allsyms = syms + esyms
    C_all = W["close"][allsyms].ffill(limit=3)
    raw_all = W["close"][allsyms]
    V_all = W["volume"][allsyms].fillna(0)
    dates = C_all.index
    last = dates[-1]

    # ---------------- universe filter (as of today)
    bars = raw_all.notna().sum()
    turnover = raw_all * V_all / 1e6                    # $ millions
    med_to = turnover.iloc[-50:].median()
    traded_recently = raw_all.iloc[-3:].notna().any()
    c_all = C_all.iloc[-1]
    mcap = market_caps(c_all)
    base_ok = (bars >= S["min_bars"]) & traded_recently & (c_all >= S["min_price"])
    keep = base_ok & (med_to >= S["min_median_turnover_m"])
    # stocks with a known market cap below the floor are dropped; unknown (new listings) are kept
    keep_s = keep.reindex(syms) & (mcap.reindex(syms).fillna(1e12) >= S["min_market_cap_m"])
    U = [s for s in syms if keep_s.get(s, False)]
    etf_sets = pd.read_csv(CFG / "etf_groups.csv") if (CFG / "etf_groups.csv").exists() else pd.DataFrame(columns=["Set", "ETF", "Label"])
    curated = set(etf_sets["ETF"])
    keep_e = (bars >= S["min_bars"]) & traded_recently & ((med_to >= S["etf_min_median_turnover_m"]) | bars.index.isin(curated))
    E = [s for s in esyms if keep_e.get(s, False)]
    print(f"{len(syms)} stocks with data -> {len(U)} pass filters; {len(esyms)} ETFs -> {len(E)} (as of {last})")

    C, H, L, V = C_all[U], W["high"][U], W["low"][U], V_all[U]
    raw_close = raw_all[U]
    Cu, Hu, Lu, Vu = C, H, L, V
    Ou = W["open"][U].fillna(C)

    # ---------------- indicators (stocks)
    def indicators(Cx, Hx, Lx, Vx):
        sma = {n: Cx.rolling(n, min_periods=n).mean() for n in (20, 50, 150, 200)}
        return (sma, Cx.ewm(span=20, adjust=False).mean(), Cx.ewm(span=50, adjust=False).mean(),
                Hx.rolling(252, min_periods=60).max(), Lx.rolling(252, min_periods=60).min(),
                Vx.rolling(50, min_periods=20).mean().shift(1))

    sma, ema20, ema50, hi52, lo52, avgv50 = indicators(Cu, Hu, Lu, Vu)
    rets = {k: (ytd_return(Cu) if n is None else period_return(Cu, n)) for k, n in PERIODS.items()}
    rets_prev = {k: (ytd_return(Cu, -1 - LAG) if n is None else period_return(Cu, n, -1 - LAG))
                 for k, n in PERIODS.items()}
    score_now, score_prev = rs_score(Cu), rs_score(Cu, -1 - LAG)
    rs_now, rs_prev = pct_rank(score_now), pct_rank(score_prev)

    def trend_flags(Cx, smax, lo, hi, rs):
        sma200_up = smax[200].iloc[-1] > smax[200].iloc[-22]
        cx = Cx.iloc[-1]
        return ((cx > smax[150].iloc[-1]) & (cx > smax[200].iloc[-1]) & (smax[150].iloc[-1] > smax[200].iloc[-1])
                & sma200_up & (smax[50].iloc[-1] > smax[150].iloc[-1]) & (smax[50].iloc[-1] > smax[200].iloc[-1])
                & (cx > smax[50].iloc[-1]) & (cx >= 1.3 * lo.iloc[-1]) & (cx >= 0.75 * hi.iloc[-1]) & (rs >= 70))

    tt = trend_flags(Cu, sma, lo52, hi52, rs_now)
    slope150 = sma[150].iloc[-1] / sma[150].iloc[-21] - 1
    c = Cu.iloc[-1]

    def stage_of(sl, px, m):
        if pd.isna(sl) or pd.isna(m):
            return None
        if px > m and sl > 0.01:
            return 2
        if px < m and sl < -0.01:
            return 4
        return 3 if px >= m else 1

    def stage(s):
        return stage_of(slope150.get(s), c.get(s), sma[150].iloc[-1].get(s))

    # sectors / industries: Yahoo profile (fetch_fundamentals.py), GICS from the S&P lists as a fallback
    sec, ind = pd.Series(dtype=object), pd.Series(dtype=object)
    if (CFG / "fund_info.csv").exists():
        fi = pd.read_csv(CFG / "fund_info.csv", dtype={"symbol": str}).drop_duplicates("symbol").set_index("symbol")
        sec = fi["sector"].dropna() if "sector" in fi else sec
        ind = fi["industry"].dropna() if "industry" in fi else ind
    if (CFG / "gics.csv").exists():
        g = pd.read_csv(CFG / "gics.csv").drop_duplicates("Symbol").set_index("Symbol")
        sec = sec.combine_first(g["Sector"])
    etf_cat = pd.Series(dtype=object)
    if (CFG / "etf_info.csv").exists():
        ei = pd.read_csv(CFG / "etf_info.csv", dtype={"symbol": str}).drop_duplicates("symbol").set_index("symbol")
        etf_cat = ei["category"].dropna() if "category" in ei else etf_cat

    rows = []
    vr = Vu.iloc[-1] / avgv50.iloc[-1]
    names = u.set_index("symbol")["name"]
    for s in U:
        rows.append({
            "s": s, "n": str(names.get(s, s))[:40], "sec": sec.get(s), "ind": ind.get(s), "ex": stocks.at[s, "exch"],
            "px": r2(c[s]), "r": {k: r2(v[s] * 100) for k, v in rets.items()},
            "rp": {k: r2(v[s] * 100) for k, v in rets_prev.items()},
            "rs": None if pd.isna(rs_now[s]) else int(rs_now[s]),
            "rs1w": None if pd.isna(rs_prev.get(s)) else int(rs_prev[s]),
            "h52": r2((c[s] / hi52.iloc[-1][s] - 1) * 100, 1),
            "l52": r2((c[s] / lo52.iloc[-1][s] - 1) * 100, 1),
            "a20": bool(c[s] > sma[20].iloc[-1][s]) if pd.notna(sma[20].iloc[-1][s]) else None,
            "a50": bool(c[s] > sma[50].iloc[-1][s]) if pd.notna(sma[50].iloc[-1][s]) else None,
            "a200": bool(c[s] > sma[200].iloc[-1][s]) if pd.notna(sma[200].iloc[-1][s]) else None,
            "tt": bool(tt[s]), "st": stage(s),
            "to": r2(med_to[s], 1), "vr": r2(vr[s], 1),
            "mc": r2(mcap.get(s), 0),
        })

    # ---------------- ETFs: same columns, RS measured against the stock universe
    Ce, He, Le, Ve = C_all[E], W["high"][E], W["low"][E], V_all[E]
    Oe = W["open"][E].fillna(Ce)
    esma, eema20, eema50, ehi52, elo52, eavgv50 = indicators(Ce, He, Le, Ve)
    erets = {k: (ytd_return(Ce) if n is None else period_return(Ce, n)) for k, n in PERIODS.items()}
    erets_prev = {k: (ytd_return(Ce, -1 - LAG) if n is None else period_return(Ce, n, -1 - LAG)) for k, n in PERIODS.items()}
    ers_now, ers_prev = rank_against(rs_score(Ce), score_now), rank_against(rs_score(Ce, -1 - LAG), score_prev)
    ett = trend_flags(Ce, esma, elo52, ehi52, ers_now)
    eslope = esma[150].iloc[-1] / esma[150].iloc[-21] - 1
    ec = Ce.iloc[-1]
    evr = Ve.iloc[-1] / eavgv50.iloc[-1]
    set_of = dict(zip(etf_sets["ETF"], etf_sets["Set"]))
    label_of = dict(zip(etf_sets["ETF"], etf_sets["Label"]))
    for s in E:
        rows.append({
            "s": s, "n": str(names.get(s, s))[:48], "k": "e", "ex": etfs_u.at[s, "exch"],
            "cat": etf_cat.get(s), "set": set_of.get(s), "lbl": label_of.get(s),
            "px": r2(ec[s]), "r": {k: r2(v[s] * 100) for k, v in erets.items()},
            "rp": {k: r2(v[s] * 100) for k, v in erets_prev.items()},
            "rs": None if pd.isna(ers_now[s]) else int(ers_now[s]),
            "rs1w": None if pd.isna(ers_prev.get(s)) else int(ers_prev[s]),
            "h52": r2((ec[s] / ehi52.iloc[-1][s] - 1) * 100, 1),
            "l52": r2((ec[s] / elo52.iloc[-1][s] - 1) * 100, 1),
            "a20": bool(ec[s] > esma[20].iloc[-1][s]) if pd.notna(esma[20].iloc[-1][s]) else None,
            "a50": bool(ec[s] > esma[50].iloc[-1][s]) if pd.notna(esma[50].iloc[-1][s]) else None,
            "a200": bool(ec[s] > esma[200].iloc[-1][s]) if pd.notna(esma[200].iloc[-1][s]) else None,
            "tt": bool(ett[s]), "st": stage_of(eslope.get(s), ec.get(s), esma[150].iloc[-1].get(s)),
            "to": r2(med_to[s], 1), "vr": r2(evr[s], 1),
        })

    # ---------------- groups (membership only; maths happens in the browser)
    uset = set(U)

    def load_groups(path, key):
        if not path.exists():
            return {}
        g = pd.read_csv(path)
        return {r[key]: [x for x in str(r["Symbols"]).split() if x in uset] for _, r in g.iterrows()}

    secu, indu = sec[sec.index.isin(uset)], ind[ind.index.isin(uset)]
    eset = set(E)
    etf_groups = {}
    for st_name, g in etf_sets.groupby("Set", sort=False):
        d = {f"{r.ETF} · {r.Label}": [r.ETF] for r in g.itertuples() if r.ETF in eset}
        if d:
            etf_groups[st_name] = d
    groups = {
        "Themes": load_groups(CFG / "themes.csv", "Theme"),
        "Sector": {k: sorted(secu.index[secu == k]) for k in sorted(secu.unique())},
        "Industry": {k: sorted(indu.index[indu == k]) for k in sorted(indu.unique())},
        "Index": load_groups(CFG / "indices.csv", "Index"),
        "ETFs": etf_groups,
    }
    if not groups["Industry"]:
        groups.pop("Industry")

    # ---------------- breadth history (current stock universe)
    nb = S["breadth_history_days"]
    dchg = Cu.pct_change(fill_method=None)
    valid = raw_close.notna()
    hiC = Cu.rolling(252, min_periods=200).max()
    loC = Cu.rolling(252, min_periods=200).min()

    def pct(mask, base):
        return (mask.sum(axis=1) / base.sum(axis=1).replace(0, np.nan) * 100)

    b = pd.DataFrame({
        "a20": pct((Cu > sma[20]) & valid, sma[20].notna() & valid),
        "a50": pct((Cu > sma[50]) & valid, sma[50].notna() & valid),
        "a200": pct((Cu > sma[200]) & valid, sma[200].notna() & valid),
        "adv": ((dchg > 0) & valid).sum(axis=1), "dec": ((dchg < 0) & valid).sum(axis=1),
        "nh": ((Cu >= hiC) & hiC.notna() & valid).sum(axis=1),
        "nl": ((Cu <= loC) & loC.notna() & valid).sum(axis=1),
        "up4": ((dchg >= 0.04) & valid).sum(axis=1), "dn4": ((dchg <= -0.04) & valid).sum(axis=1),
    }).iloc[-nb:]
    bench_close = W["close"][bench].reindex(b.index) if bench in W["close"] else pd.Series(np.nan, index=b.index)
    breadth = {"dates": list(b.index), "bench": [r2(x) for x in bench_close], "bench_name": bench}
    for col in b.columns:
        breadth[col] = [r2(x, 1) for x in b[col]] if col in ("a20", "a50", "a200") else [int(x) for x in b[col]]

    indices = {}
    for name in [bench] + S["extra_indices"]:
        if name in W["close"]:
            ci = W["close"][name].ffill()
            indices[name] = {"px": r2(ci.iloc[-1]),
                             "r": {k: r2(((ytd_return(ci.to_frame()) if n is None else period_return(ci.to_frame(), n)).iloc[0]) * 100)
                                   for k, n in PERIODS.items()}}

    # ---------------- scanners (library in scanners.py)
    bench_s = W["close"][bench].reindex(Cu.index).ffill() if bench in W["close"] else Cu.mean(axis=1)
    # All-time high before today: monthly history (config/ath.csv, from fetch_ath.py) + daily highs since.
    ath_all = None
    first_all = raw_all.apply(lambda s_: s_.first_valid_index())
    if (CFG / "ath.csv").exists():
        a = pd.read_csv(CFG / "ath.csv", dtype={"symbol": str}).drop_duplicates("symbol").set_index("symbol")
        cutoff = str(a["cutoff"].iloc[0]) if len(a) else last
        Hall = W["high"][allsyms]
        since = Hall.loc[(Hall.index >= cutoff) & (Hall.index < last)].max()
        ath_all = pd.Series(np.fmax(a["ath_before"].reindex(allsyms).to_numpy(dtype=float),
                                    since.reindex(allsyms).to_numpy(dtype=float)), index=allsyms)
        # symbols missing from the file: only trust the daily data if the whole listed life is inside it
        inside = first_all > dates[5]
        miss = ath_all.index[a["ath_before"].reindex(allsyms).isna()]
        ath_all[miss] = np.where(inside[miss], Hall[miss].iloc[:-1].max(), np.nan)
        print(f"ATH reference: {int(ath_all.reindex(U).notna().sum())} of {len(U)} stocks")
    ath_prior = ath_all.reindex(U) if ath_all is not None else None
    sc_meta, sc_hits, sc_metric, per_stock = SC.run_all({
        "O": Ou, "H": Hu, "L": Lu, "C": Cu, "V": Vu, "bench": bench_s, "sma": sma,
        "ema20": ema20, "ema50": ema50, "rs_now": rs_now, "rs_prev": rs_prev.reindex(U),
        "hi52": hi52, "lo52": lo52, "avgv50": avgv50, "vr": vr, "tt": tt, "ath_prior": ath_prior})
    by_row = {r["s"]: r for r in rows}
    for s in U:
        for k, ser in per_stock.items():
            by_row[s][k] = r2(ser.get(s), 1)
    cats = list(SC.CATS)

    # ---------------- ETF scanners: the same library run on the ETF universe, a few lists kept
    if len(E) >= 20:
        ebench = W["close"][bench].reindex(Ce.index).ffill() if bench in W["close"] else Ce.mean(axis=1)
        em, eh, emt, eper = SC.run_all({
            "O": Oe, "H": He, "L": Le, "C": Ce, "V": Ve, "bench": ebench, "sma": esma,
            "ema20": eema20, "ema50": eema50, "rs_now": ers_now, "rs_prev": ers_prev.reindex(E),
            "hi52": ehi52, "lo52": elo52, "avgv50": eavgv50, "vr": evr, "tt": ett,
            "ath_prior": ath_all.reindex(E) if ath_all is not None else None})
        for s in E:
            for k, ser in eper.items():
                by_row[s][k] = r2(ser.get(s), 1)
        # bond, cash and currency ETFs move too little for chart patterns - keep them out of the ETF scans
        fixed = pd.Series([bool(re.search(r"bond|treasur|money market|ultrashort|muni|government|corporate|inflation|"
                                          r"bank loan|credit|preferred|currency|t-bill|mortgage|securitized|fixed income",
                                          str(etf_cat.get(s_, "")) + " " + str(label_of.get(s_, "")) + " " + str(names.get(s_, "")), re.I))
                           for s_ in E], index=E)
        eh = {k: [s_ for s_ in v if not fixed.get(s_, False)] for k, v in eh.items()}
        # broad index clones are always "tight": keep the coil list to ETFs with leadership (RS 70+)
        eh["tight3w"] = [s_ for s_ in eh.get("tight3w", []) if ers_now.get(s_, 0) >= 70]
        top_rs = (ers_now >= ers_now[~fixed].quantile(0.90)) & ~fixed
        pick = {"high52": ("ETF: new 52-week high", None), "ath": ("ETF: all-time high", None),
                "stage2_bo": ("ETF: Stage 2 breakout", None), "golden": ("ETF: golden cross", None),
                "pb50": ("ETF: pullback to the 50 DMA in an uptrend", None), "tight3w": ("ETF: 3 weeks tight", None),
                "rsnh_3m": ("ETF: RS line at a 3-month high before price", None),
                "below200": ("ETF: broke below the 200 DMA", None), "death": ("ETF: death cross", None)}
        emeta = {m["id"]: m for m in em}
        for sid, (nm, _) in pick.items():
            if sid not in emeta:
                continue
            m = dict(emeta[sid])
            m.update({"id": "etf_" + sid, "name": nm, "cat": "ETFs",
                      "desc": m["desc"].replace("stock", "ETF").replace("Stage-2 stock", "Stage-2 ETF")
                      + (" ETFs with RS 70+ only." if sid == "tight3w" else "") + " Bond, cash and currency ETFs are left out."})
            sc_meta.append(m)
            sc_hits["etf_" + sid] = eh.get(sid, [])
            if sid in emt:
                sc_metric["etf_" + sid] = emt[sid]
        lead = [s for s in E if top_rs.get(s, False) and ec[s] >= 0.95 * ehi52.iloc[-1][s]]
        sc_meta.append({"id": "etf_leaders", "name": "ETF: RS leaders near highs", "cat": "ETFs",
                        "desc": "ETFs in the top 10% of all ETFs by RS rating and within 5% of their 52-week high — "
                                "where the strongest money is going, sector by sector and country by country.",
                        "metric": "RS", "fmt": "int", "sort": "metric"})
        sc_hits["etf_leaders"] = sorted(lead)
        sc_metric["etf_leaders"] = {s: float(ers_now[s]) for s in lead}
        cats = cats + ["ETFs"]

    # ---------------- fundamentals (SEC filings, from fetch_fundamentals.py)
    FR = None
    try:
        import fundamentals as FD
        st_series = pd.Series({s: stage(s) for s in U})
        a_of = lambda n: pd.Series({s: (bool(c[s] > sma[n].iloc[-1][s]) if pd.notna(sma[n].iloc[-1][s]) else None) for s in U})
        FR = FD.build(U, {"mcap": mcap.reindex(U), "c": c, "rs": rs_now, "st": st_series, "tt": tt, "a200": a_of(200),
                          "a50": a_of(50), "h52": (c / hi52.iloc[-1] - 1) * 100, "vr": vr, "C": Cu, "dates": list(dates),
                          "sector": sec.reindex(U), "industry": ind.reindex(U)})
    except Exception as e:  # fundamentals are optional - never break the daily run
        import traceback
        traceback.print_exc()
        print(f"fundamentals skipped: {e}")
    if FR:
        for s in U:
            r = by_row[s]
            r.update(FR["per_stock"].get(s, {}))
            if not r.get("ind") and FR["industry"].get(s):
                r["ind"] = FR["industry"][s]
        sc_meta += FR["meta"]
        sc_hits.update(FR["hits"])
        sc_metric.update(FR["metric"])
        base = [k for k in cats if k not in ("Weakness", "ETFs")]
        cats = base + FR["cats"][:3] + ["Weakness", FR["cats"][3]] + (["ETFs"] if "ETFs" in cats else [])
        dump_later = {"fund.json": FR["detail"]}
        filed = [v.get("filed") for v in FR["per_stock"].values() if v.get("filed")]
        fund_meta = {"stocks": sum(1 for v in FR["per_stock"].values() if "fr" in v), "latest_filing": max(filed) if filed else None}
        print(f"fundamentals: {fund_meta['stocks']} stocks rated, latest filing {fund_meta['latest_filing']}")
    else:
        dump_later, fund_meta = {}, None
    # ---------------- long-base identifier (bases.py)
    base_meta = None
    try:
        import bases as BS
        BR = BS.build(LONG, U, rs_now)
        for r in rows:
            r.update(BR["per_stock"].get(r["s"], {}))
        bm, bh, bmet = BS.scans(BR)
        sc_meta += bm
        sc_hits.update(bh)
        sc_metric.update(bmet)
        cats = cats[:cats.index("Breakouts") + 1] + ["Bases"] + cats[cats.index("Breakouts") + 1:] if "Breakouts" in cats else cats + ["Bases"]
        dump_later["bases.json"] = {"asof": last, "rows": BR["rows"], "charts": BR["charts"], "evidence": BR["evidence"],
                                    "params": {"d_max": BS.D_MAX, "zone": BS.ZONE, "min_len": BS.MIN_LEN, "recent": BS.RECENT}}
        base_meta = {"stocks": len(BR["rows"]), "history_from": str(LONG.date.min())}
    except Exception as e:  # never break the daily run
        import traceback
        traceback.print_exc()
        print(f"bases skipped: {e}")

    # ---------------- Stage 2 phase: early / mid / late (stage2.py)
    s2_meta = None
    try:
        import stage2 as S2
        SR = S2.build(LONG, [r["s"] for r in rows if r.get("st") == 2 and r.get("k") != "e"], rs_now)
        for r in rows:
            r.update(SR["per_stock"].get(r["s"], {}))
        sm, sh, smet = S2.scans(SR)
        sc_meta += sm
        sc_hits.update(sh)
        sc_metric.update(smet)
        at = cats.index("Bases") + 1 if "Bases" in cats else len(cats)
        cats = cats[:at] + ["Stage 2"] + cats[at:]
        dump_later["stage2.json"] = {"asof": last, "rows": SR["rows"], "evidence": SR["evidence"]}
        s2_meta = {"stocks": len(SR["rows"])}
    except Exception as e:  # never break the daily run
        import traceback
        traceback.print_exc()
        print(f"stage 2 phases skipped: {e}")
    scans = {"cats": cats, "meta": sc_meta, "hits": sc_hits, "metric": sc_metric}

    # earnings dates for ETFs are meaningless; for stocks fundamentals.py adds next_results
    fm = json.loads((CACHE / "fetch_meta.json").read_text()) if (CACHE / "fetch_meta.json").exists() else {}
    meta = {"asof": last, "generated": datetime.now(ET).strftime("%Y-%m-%d %H:%M ET"),
            "source": fm.get("source"), "stocks_with_data": len(syms), "universe": len(U), "etfs": len(E),
            "failed": fm.get("failed_count"), "indices": indices,
            "filters": {"min_price": S["min_price"], "min_median_turnover_m": S["min_median_turnover_m"],
                        "min_market_cap_m": S["min_market_cap_m"], "etf_min_median_turnover_m": S["etf_min_median_turnover_m"]},
            "min_group_size": S["min_group_size"], "fund": fund_meta, "bases": base_meta, "stage2": s2_meta,
            "repo": __import__("os").environ.get("GITHUB_REPOSITORY")}

    def clean(o):
        if isinstance(o, float):
            return o if np.isfinite(o) else None
        if isinstance(o, dict):
            return {k: clean(v) for k, v in o.items()}
        if isinstance(o, list):
            return [clean(v) for v in o]
        return o

    def dump(name, obj):
        (OUT / name).write_text(json.dumps(clean(obj), separators=(",", ":"), allow_nan=False))
    dump("meta.json", meta)
    dump("stocks.json", rows)
    dump("groups.json", groups)
    dump("breadth.json", breadth)
    dump("scans.json", scans)
    for name, obj in dump_later.items():
        dump(name, obj)
    # ETF facts + top holdings for the stock card and the Rotation tab
    if (CFG / "etf_info.csv").exists():
        ei = pd.read_csv(CFG / "etf_info.csv", dtype={"symbol": str}).drop_duplicates("symbol").set_index("symbol")
        ej = {}
        for s in E:
            if s not in ei.index:
                continue
            r = ei.loc[s]
            h = []
            for tok in str(r.get("holdings") or "").split():
                sym_, _, w = tok.rpartition(":")
                try:
                    if np.isfinite(float(w)):
                        h.append([sym_.replace("-", "."), round(float(w), 2)])
                except ValueError:
                    pass
            ej[s] = {k: v for k, v in {"cat": r.get("category"), "fam": r.get("family"), "aum": r2(r.get("aum_m"), 0),
                                        "exp": r2(r.get("expense"), 5), "h": h}.items()
                     if v is not None and not (isinstance(v, float) and not np.isfinite(v)) and v != []}
        dump("etf.json", ej)

    # ---------------- last ~1 year of closes for watchlists (sparklines, since-added, stock-card chart)
    def rd(v):
        if v is None or not np.isfinite(v):
            return None
        v = round(float(v), 0 if v >= 1000 else 1 if v >= 100 else 2)
        return int(v) if v == int(v) else v
    tail = C_all.iloc[-260:]
    closes = {"dates": list(tail.index), "c": {s: [rd(v) for v in tail[s].tolist()] for s in U + E}}
    if bench in W["close"]:
        closes["c"][bench] = [rd(v) for v in W["close"][bench].reindex(tail.index).ffill().tolist()]
    dump("closes.json", closes)
    print("scans: " + ", ".join(f"{k}={len(v)}" for k, v in scans["hits"].items()))
    print(f"breadth today: >50DMA {breadth['a50'][-1]}%  >200DMA {breadth['a200'][-1]}%  "
          f"A/D {breadth['adv'][-1]}/{breadth['dec'][-1]}")


if __name__ == "__main__":
    main()
