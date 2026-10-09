"""Fundamental metrics, Fund rating and fundamental scanners (US).

Input (made by fetch_fundamentals.py):
  config/fund_results.csv   quarterly results from SEC XBRL filings ($ millions)
  config/fund_holdings.csv  monthly snapshots of % held by insiders / institutions (Yahoo)
  config/fund_info.csv      sector, industry, earnings dates
  config/insider_tx.csv     open-market insider purchases and sales (SEC Form 4)
Used by compute.py:

    F = fundamentals.build(U, ctx)  ->  dict with
        per_stock  {symbol: {...metrics...}}       merged into stocks.json rows
        detail     {symbol: {q: [...], h: [...], it: [...]}}  tables for the stock card (fund.json)
        industry   {symbol: industry}
        meta, hits, metric                        extra scanners (same format as scanners.py)
        cats                                       their categories

Conventions
  * Growth uses net income attributable to the company - robust to splits, unlike reported EPS.
    Growth is only computed when the base quarter is profitable.
  * P/E = today's market cap / trailing-4-quarter net income, so it moves with the price every day.
  * Operating margin = operating income / revenue (the US convention).
  * Banks, insurers, brokers and other financials: no operating margin, ROCE, debt or cash-flow
    tests (they don't mean the same thing for a lender).
  * The fiscal year is the company's own (fye = 1 on the quarter that closes it).
"""
import re
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config"

CATS = ["Earnings & growth", "Quality & value", "Ownership & insiders", "Fundamental red flags"]
TOP_EXEC = re.compile(r"\b(?:CEO|CFO|Chief Executive|Chief Financial|President|Chairman)\b", re.I)


def _num(x):
    return None if x is None or (isinstance(x, float) and not np.isfinite(x)) else x


def growth(now, base):
    """% growth, only when the base is positive and meaningful."""
    if now is None or base is None or not np.isfinite(now) or not np.isfinite(base) or base <= 0:
        return np.nan
    g = (now / base - 1) * 100
    return float(np.clip(g, -100, 999))


def load():
    rd = lambda f, **k: pd.read_csv(CFG / f, dtype={"symbol": str}, **k) if (CFG / f).exists() else pd.DataFrame()
    r, h, ins = rd("fund_results.csv"), rd("fund_holdings.csv"), rd("insider_tx.csv")
    i = rd("fund_info.csv")
    i = i.drop_duplicates("symbol").set_index("symbol") if len(i) else i
    return r, h, i, ins


def stock_metrics(q, hold, ins, asof):
    """q: this stock's quarterly rows sorted by quarter end; hold: ownership snapshots; ins: insider trades."""
    out = {}
    q = q.copy()
    q["qe"] = pd.to_datetime(q["qe"])
    q = q.drop_duplicates("qe", keep="last").set_index("qe").sort_index()
    bank = bool(q["bank"].fillna(0).iloc[-1]) if len(q) else False
    out["bank"] = bank
    if len(q):
        last = q.index[-1]
        qn = (q.index.year * 4 + (q.index.month - 1) // 3).tolist()
        pos = {n: i for i, n in enumerate(qn)}
        n0 = qn[-1]
        cols = {c: pd.to_numeric(q[c], errors="coerce").to_numpy(dtype=float) for c in q.columns
                if c not in ("symbol", "basis", "aud", "filed", "pe")}

        def at(months_back, col):
            i = pos.get(n0 - months_back // 3)
            arr = cols.get(col)
            return float(arr[i]) if i is not None and arr is not None else np.nan

        L = q.iloc[-1]
        num = lambda v: float(v) if v is not None and pd.notna(v) else np.nan
        out["qe"] = last.strftime("%Y-%m-%d")
        out["filed"] = L.get("filed") if isinstance(L.get("filed"), str) else None
        out["rev_q"], out["pat_q"] = _num(num(L["rev"])), _num(num(L["pat"]))
        out["rev_yoy"], out["pat_yoy"] = growth(num(L["rev"]), at(12, "rev")), growth(num(L["pat"]), at(12, "pat"))
        out["rev_qoq"], out["pat_qoq"] = growth(num(L["rev"]), at(3, "rev")), growth(num(L["pat"]), at(3, "pat"))
        out["pat_yoy1"] = growth(at(3, "pat"), at(15, "pat"))
        out["pat_yoy2"] = growth(at(6, "pat"), at(18, "pat"))
        out["rev_yoy1"] = growth(at(3, "rev"), at(15, "rev"))
        out["eps_yoy"] = growth(num(L.get("eps")), at(12, "eps"))
        # Code 33 inputs: year-on-year growth for the last four quarters (EPS, else net income) and revenue
        for k in range(4):
            ge = growth(at(3 * k, "eps"), at(3 * k + 12, "eps"))
            out[f"eg{k}"] = ge if np.isfinite(ge) else growth(at(3 * k, "pat"), at(3 * k + 12, "pat"))
            out[f"rg{k}"] = growth(at(3 * k, "rev"), at(3 * k + 12, "rev"))
        out["pat_q1"], out["pat_q4"] = at(3, "pat"), at(12, "pat")
        streak = 0
        for k in range(6):
            g = growth(at(3 * k, "rev"), at(3 * k + 12, "rev"))
            if np.isfinite(g) and g > 10:
                streak += 1
            else:
                break
        out["rev_streak"] = streak

        def ttm(col, off=0):
            vals = [at(3 * k + off, col) for k in range(4)]
            return float(np.sum(vals)) if all(np.isfinite(v) for v in vals) else np.nan
        out["rev_ttm"], out["pat_ttm"] = ttm("rev"), ttm("pat")
        out["rev_ttm_g"] = growth(out["rev_ttm"], ttm("rev", 12))
        out["pat_ttm_g"] = growth(out["pat_ttm"], ttm("pat", 12))
        # share count (diluted, weighted) - buybacks shrink it, dilution grows it
        sh0, sh4 = num(L.get("shares")), at(12, "shares")
        out["sh_yoy"] = float((sh0 / sh4 - 1) * 100) if np.isfinite(sh0) and np.isfinite(sh4) and sh4 > 0 else np.nan
        rev = pd.to_numeric(q["rev"], errors="coerce")
        # operating margin: reported operating income, else pre-tax profit + interest - other income
        opi = pd.to_numeric(q.get("opi"), errors="coerce") if "opi" in q else pd.Series(np.nan, index=q.index)
        alt = pd.to_numeric(q["pbt"], errors="coerce") + pd.to_numeric(q["fin"], errors="coerce").fillna(0) - pd.to_numeric(q["oi"], errors="coerce").fillna(0)
        op = opi.where(opi.notna(), alt)
        if not bank:
            opm = (op / rev.where(rev > 0) * 100).clip(-200, 100)
            q["opm"] = opm
            cols["opm"] = opm.to_numpy(dtype=float)
            out["opm"] = _num(float(opm.iloc[-1])) if np.isfinite(opm.iloc[-1]) else None
            ago = at(12, "opm")
            out["opm_chg"] = float(opm.iloc[-1] - ago) if np.isfinite(opm.iloc[-1]) and np.isfinite(ago) else np.nan
            op_ttm = sum(at(3 * k, "opm") * at(3 * k, "rev") for k in range(4)) if np.isfinite(out["rev_ttm"]) else np.nan
            op_ttm0 = sum(at(3 * k + 12, "opm") * at(3 * k + 12, "rev") for k in range(4))
            r0 = ttm("rev", 12)
            out["opm_ttm"] = float(op_ttm / out["rev_ttm"]) if np.isfinite(op_ttm) and out["rev_ttm"] > 0 else np.nan
            out["opm_ttm_chg"] = float(out["opm_ttm"] - op_ttm0 / r0) if np.isfinite(out["opm_ttm"]) and np.isfinite(op_ttm0) and r0 and r0 > 0 else np.nan
        npm = pd.to_numeric(q["pat"], errors="coerce") / rev.where(rev > 0) * 100
        q["npm"] = npm
        cols["npm"] = npm.to_numpy(dtype=float)
        for k in range(3):                                   # net margin vs the same quarter a year earlier
            out[f"mg{k}"] = at(3 * k, "npm") - at(3 * k + 12, "npm")
        out["npm"] = float(npm.iloc[-1]) if np.isfinite(npm.iloc[-1]) else np.nan
        # balance sheet: latest quarter-end
        eqs = pd.to_numeric(q["equity"], errors="coerce")
        bs = q[eqs.notna()]
        fye = pd.to_numeric(q.get("fye"), errors="coerce").fillna(0) == 1
        if len(bs):
            b = bs.iloc[-1]
            eq = float(b["equity"])
            out["equity"] = _num(eq)
            out["bs_date"] = bs.index[-1].strftime("%Y-%m-%d")
            fyb = bs[fye.reindex(bs.index).fillna(False).to_numpy()]
            fy_end = fyb.index[-1] if len(fyb) else None
            prev = bs[bs.index <= bs.index[-1] - pd.DateOffset(months=11)]
            if fy_end is not None:
                back = (last.year * 4 + (last.month - 1) // 3) - (fy_end.year * 4 + (fy_end.month - 1) // 3)
                fy_pat = sum(at(3 * (back + k), "pat") for k in range(4))
                fy_ebit = sum(at(3 * (back + k), "pbt") + (at(3 * (back + k), "fin") if np.isfinite(at(3 * (back + k), "fin")) else 0) for k in range(4))
                if not np.isfinite(fy_pat):
                    fy_pat = num(fyb["ytd_pat"].iloc[-1])
                e_fy = float(fyb["equity"].iloc[-1])
                out["roe_fy"] = fy_end.strftime("%y")
                out["roe"] = float(np.clip(fy_pat / e_fy * 100, -200, 200)) if np.isfinite(fy_pat) and e_fy > 0 else np.nan
            else:
                fy_ebit, e_fy = np.nan, np.nan
                eq0 = float(prev["equity"].iloc[-1]) if len(prev) else np.nan
                avg_eq = np.nanmean([eq, eq0]) if np.isfinite(eq0) else eq
                out["roe"] = float(np.clip(out["pat_ttm"] / avg_eq * 100, -200, 200)) if np.isfinite(out["pat_ttm"]) and avg_eq and avg_eq > 0 else np.nan
            if not bank:
                debt = num(b["borrow"])
                out["debt"] = _num(debt)
                out["cash"] = _num(num(b["cash"]))
                out["de"] = float(debt / eq) if np.isfinite(debt) and eq > 0 else np.nan
                if fy_end is not None:
                    d_fy = num(fyb["borrow"].iloc[-1])
                    cap = e_fy + (d_fy if np.isfinite(d_fy) else 0)
                    out["roce"] = float(np.clip(fy_ebit / cap * 100, -200, 200)) if np.isfinite(fy_ebit) and cap > 0 else np.nan
                cw, ta = num(b.get("cwip")), num(b.get("assets"))
                out["cwip_pct"] = float(cw / ta * 100) if np.isfinite(cw) and np.isfinite(ta) and ta > 0 else np.nan
                prev_d = num(prev["borrow"].iloc[-1]) if len(prev) else np.nan
                out["debt_chg"] = float(debt - prev_d) if np.isfinite(debt) and np.isfinite(prev_d) else np.nan
        # cash flow: latest fiscal year
        fy = q[fye.to_numpy() & pd.to_numeric(q["ocf"], errors="coerce").notna().to_numpy()]
        if len(fy) and not bank:
            f = fy.iloc[-1]
            ocf, capex, ytd_pat, ytd_rev = (num(f[k]) for k in ("ocf", "capex", "ytd_pat", "ytd_rev"))
            out["fy"] = fy.index[-1].strftime("%Y")
            out["ocf"] = _num(ocf)
            out["fcf"] = float(ocf - (capex if np.isfinite(capex) else 0))
            out["ocf_pat"] = float(ocf / ytd_pat) if np.isfinite(ytd_pat) and ytd_pat > 0 else np.nan
            out["capex_rev"] = float(capex / ytd_rev * 100) if np.isfinite(capex) and np.isfinite(ytd_rev) and ytd_rev > 0 else np.nan
            if len(fy) >= 2:
                c0 = num(fy.iloc[-2]["capex"])
                out["capex_g"] = growth(capex, c0)
    # ownership snapshots (monthly)
    if hold is not None and len(hold):
        hh = hold.copy()
        hh["date"] = pd.to_datetime(hh["date"])
        hh = hh.drop_duplicates("date", keep="last").set_index("date").sort_index()
        for c in ("insiders", "inst", "inst_float"):
            hh[c] = pd.to_numeric(hh[c], errors="coerce") * 100
        H = hh.iloc[-1]
        out["shp_q"] = hh.index[-1].strftime("%Y-%m-%d")
        out["insiders"], out["inst"] = _num(float(H["insiders"])), _num(float(H["inst"]))
        out["inst_count"] = int(H["inst_count"]) if pd.notna(H["inst_count"]) else None

        def chg(c, months):
            t = hh.index[-1] - pd.DateOffset(months=months)
            m = hh.index[(hh.index >= t - pd.Timedelta(days=25)) & (hh.index <= t + pd.Timedelta(days=25))]
            return float(H[c] - hh.loc[m[0], c]) if len(m) and np.isfinite(H[c]) and np.isfinite(hh.loc[m[0], c]) else np.nan
        out["inst_1q"], out["inst_1y"] = chg("inst", 3), chg("inst", 12)
        out["insiders_1y"] = chg("insiders", 12)
    # insider trades (SEC Form 4, open market only)
    if ins is not None and len(ins):
        t = ins.copy()
        t["tdate"] = pd.to_datetime(t["tdate"], errors="coerce")
        t["value"] = pd.to_numeric(t["value"], errors="coerce")
        # officers and directors only: 10% owners are often funds or parent companies trading blocks
        t = t[t["rel"].fillna("").astype(str).str.contains("D|O")]
        d90 = t[t.tdate >= asof - pd.Timedelta(days=90)]
        d365 = t[t.tdate >= asof - pd.Timedelta(days=365)]
        buy, sell = d90[d90.code == "P"], d90[d90.code == "S"]
        out["ins_buy90"] = float(buy.value.sum() / 1e6) if len(buy) else 0.0
        out["ins_sell90"] = float(sell.value.sum() / 1e6) if len(sell) else 0.0
        out["ins_buyers90"] = int(buy.owner.nunique())
        out["ins_sellers90"] = int(sell.owner.nunique())
        out["ins_exec_buy90"] = float(buy[buy.title.fillna("").str.contains(TOP_EXEC)].value.sum() / 1e6) if len(buy) else 0.0
        b365, s365 = d365[d365.code == "P"].value.sum(), d365[d365.code == "S"].value.sum()
        out["ins_net365"] = float((b365 - s365) / 1e6)
        last_buy = t[t.code == "P"].tdate.max()
        out["ins_last_buy"] = last_buy.strftime("%Y-%m-%d") if pd.notna(last_buy) else None
    # tables for the stock card
    detail = {}
    if len(q):
        t = q.tail(10)
        detail["q"] = [[d.strftime("%b %y"), _r(r.rev), _r(r.pat), _r(r.eps, 2), _r(r.get("opm"), 1), _r(r.npm, 1)] for d, r in t.iterrows()]
        detail["basis"] = "Consolidated, SEC filings"
    if hold is not None and len(hold):
        detail["h"] = [[d.strftime("%d %b %y"), _r(r.insiders, 2), _r(r.inst, 2), None if pd.isna(r.inst_count) else int(r.inst_count)]
                       for d, r in hh.tail(8).iterrows()]
    if ins is not None and len(ins):
        tt = ins.sort_values("tdate").tail(12)
        detail["it"] = [[str(r.tdate)[:10], str(r.owner), str(r.title if isinstance(r.title, str) and r.title else r.rel or ""),
                         r.code, _r(pd.to_numeric(r.shares, errors="coerce"), 0), _r(pd.to_numeric(r.price, errors="coerce"), 2),
                         _r(pd.to_numeric(r.value, errors="coerce") / 1e6, 3)] for r in tt.itertuples()][::-1]
    return out, detail


def _r(v, nd=1):
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if np.isfinite(v) else None


def pct(s):
    return s.rank(pct=True)


def build(U, ctx):
    """U: list of symbols in today's universe. ctx: dict with mcap (Series, $ millions), c (close), rs, st (stage),
    tt (trend template), a200 / a50 (bool Series), h52 (% from high), C (closes frame), dates, vr, sector, industry."""
    res, hold, info, ins = load()
    if not len(res):
        return None
    asof = pd.Timestamp(ctx["dates"][-1])
    res = res[res.symbol.isin(U)]
    hg = dict(tuple(hold[hold.symbol.isin(U)].groupby("symbol"))) if len(hold) else {}
    ig = {}
    if len(ins):
        # insider filings belong to the company (CIK): every share class of it gets them (GOOGL and GOOG)
        if len(info) and "cik" in info:
            cik_syms = {}
            for s_, k_ in info["cik"].reindex(U).dropna().items():
                cik_syms.setdefault(int(k_), []).append(s_)
            for k_, g_ in ins.groupby(pd.to_numeric(ins["cik"], errors="coerce")):
                for s_ in cik_syms.get(int(k_), []) if pd.notna(k_) else []:
                    ig[s_] = g_
        for s_, g_ in ins[ins.symbol.isin(U)].groupby("symbol"):
            ig.setdefault(s_, g_)
    if len(info) and "bank" in info:
        res = res.copy()
        res["bank"] = res.symbol.map(info["bank"]).fillna(0)
    M, detail = {}, {}
    for sym, g in res.groupby("symbol"):
        m, d = stock_metrics(g.sort_values("qe"), hg.get(sym), ig.get(sym), asof)
        M[sym], detail[sym] = m, d
    for sym in set(hg) | set(ig):            # ownership / insider data only
        if sym not in M:
            m, d = stock_metrics(pd.DataFrame(columns=res.columns), hg.get(sym), ig.get(sym), asof)
            M[sym], detail[sym] = m, d
    df = pd.DataFrame.from_dict(M, orient="index").reindex(U)
    ALL = ["bank", "qe", "filed", "rev_q", "pat_q", "pat_q1", "pat_q4", "rev_yoy", "pat_yoy", "rev_qoq", "pat_qoq", "pat_yoy1", "pat_yoy2", "rev_yoy1", "eps_yoy",
           "rev_streak", "rev_ttm", "pat_ttm", "rev_ttm_g", "pat_ttm_g", "sh_yoy", "opm", "opm_chg", "opm_ttm", "opm_ttm_chg", "npm", "equity",
           "bs_date", "roe", "roe_fy", "debt", "cash", "de", "roce", "cwip_pct", "debt_chg", "fy", "ocf", "fcf", "ocf_pat", "capex_rev", "capex_g",
           "shp_q", "insiders", "inst", "inst_count", "inst_1q", "inst_1y", "insiders_1y", "ins_buy90", "ins_sell90", "ins_buyers90",
           "ins_sellers90", "ins_exec_buy90", "ins_net365", "ins_last_buy"]
    for col in ALL:
        if col not in df:
            df[col] = np.nan
    mc = ctx["mcap"].reindex(U)
    df["pe"] = np.where(df["pat_ttm"] > 0, mc / df["pat_ttm"], np.nan)
    df["ps"] = np.where(df["rev_ttm"] > 0, mc / df["rev_ttm"], np.nan)
    df["peg"] = np.where((df["pe"] > 0) & (df["pat_ttm_g"] > 0), df["pe"] / df["pat_ttm_g"], np.nan)
    df["pb"] = np.where(df["equity"] > 0, mc / df["equity"], np.nan)
    df["fcf_yield"] = np.where(mc > 0, df["fcf"] / mc * 100, np.nan)

    # ── Fund rating 1-99: growth (70%) + quality (30%), percentile across the universe
    comp = {
        "pat_yoy": (df["pat_yoy"].clip(-100, 300), .25), "pat_yoy1": (df["pat_yoy1"].clip(-100, 300), .15),
        "pat_ttm_g": (df["pat_ttm_g"].clip(-100, 300), .20), "rev_yoy": (df["rev_yoy"].clip(-100, 200), .15),
        "roe": (df["roe"].clip(-50, 60), .15),
        "margin": (df["opm_chg"].fillna(df["npm"] * 0).clip(-20, 20), .10),
    }
    num, den = pd.Series(0.0, index=df.index), pd.Series(0.0, index=df.index)
    for k, (s, w) in comp.items():
        p = pct(s)
        num += p.fillna(0) * w
        den += p.notna() * w
    raw = num / den.where(den >= .5)
    raw = raw.where(~(df["pat_ttm"] <= 0), raw.clip(upper=.15))
    lossy = raw.isna() & df["pat_q"].notna() & ((df["pat_ttm"] <= 0) | (df["pat_q"] <= 0))
    raw = raw.where(~lossy, 0.05)
    df["fr"] = (raw.rank(pct=True) * 98 + 1).round()

    # ── industry and earnings dates (Yahoo)
    industry = {}
    if len(info):
        ind = info["industry"] if "industry" in info else pd.Series(dtype=object)
        industry = {s: ind[s] for s in U if s in ind.index and isinstance(ind[s], str)}
        for c_ in ("next_results", "last_results", "next_est"):
            df[c_] = info[c_].reindex(U) if c_ in info else None

    # ── scanners
    c, rs, st, tt = ctx["c"].reindex(U), ctx["rs"].reindex(U), ctx["st"].reindex(U), ctx["tt"].reindex(U)
    a200, h52, vr = ctx["a200"].reindex(U), ctx["h52"].reindex(U), ctx["vr"].reindex(U)
    fin_sec = ctx.get("sector", pd.Series(dtype=object)).reindex(U).astype(str).str.contains("Financial", case=False, na=False)
    if len(info) and "sector" in info:
        fin_sec |= info["sector"].reindex(U).astype(str).str.contains("Financial", case=False, na=False)
    nb = ~(df["bank"].fillna(False).astype(bool) | fin_sec)
    df["fin"] = ~nb
    hits, metric, meta = {}, {}, []

    def add(id_, name, cat, desc, mask, m_label=None, m_vals=None, m_fmt="pct", sort="metric"):
        mask = pd.Series(mask, index=df.index).fillna(False).astype(bool)
        syms = sorted(mask.index[mask])
        hits[id_] = syms
        if m_label:
            mv = pd.Series(m_vals, index=df.index).reindex(syms)
            metric[id_] = {s: (None if pd.isna(mv[s]) else round(float(mv[s]), 2)) for s in syms}
        meta.append({"id": id_, "name": name, "cat": cat, "desc": desc, "metric": m_label, "fmt": m_fmt, "sort": sort})

    E, Q, O, R = CATS
    add("f_growth", "Growth leaders 30/30", E,
        "Revenue and net income both up 30%+ on the same quarter last year, profitable over the last 4 quarters, RS 80+ and "
        "in Stage 2 (Deepvue's 30/30 screen).",
        (df.rev_yoy >= 30) & (df.pat_yoy >= 30) & (df.pat_ttm > 0) & (rs >= 80) & (st == 2), "Profit YoY %", df.pat_yoy, "pct", "rs")
    add("f_accel", "Earnings acceleration", E,
        "Year-on-year profit growth faster in each of the last three quarters (e.g. +15% → +30% → +50%), latest at least +20%, "
        "revenue up 10%+, RS 60+. O'Neil's strongest earnings signal — from the company's 10-Q and 10-K filings.",
        (df.pat_yoy2 < df.pat_yoy1) & (df.pat_yoy1 < df.pat_yoy) & (df.pat_yoy >= 20) & (df.pat_yoy2 > 0) & (df.rev_yoy >= 10) & (rs >= 60),
        "Profit YoY %", df.pat_yoy)
    inst_up = (df.inst_1y > 0) | (df.inst_1y.isna() & (df.inst >= 40))
    add("f_canslim", "CANSLIM-style leaders", E,
        "C: quarterly profit +25% on sales +20% · A: trailing-year profit +20% · N: within 15% of the 52-week high · "
        "L: RS 80+ · I: institutional ownership rising over the year (until a year of snapshots exists: institutions own 40%+) "
        "(William O'Neil). Check the market (M) on the Breadth tab.",
        (df.pat_yoy >= 25) & (df.rev_yoy >= 20) & (df.pat_ttm_g >= 20) & (h52 >= -15) & (rs >= 80) & inst_up,
        "Profit YoY %", df.pat_yoy, "pct", "rs")
    acc = lambda a, b: a > b
    c33 = (acc(df.eg0, df.eg1) & acc(df.eg1, df.eg2) & acc(df.eg2, df.eg3) & acc(df.rg0, df.rg1) & acc(df.rg1, df.rg2)
           & acc(df.rg2, df.rg3) & (df.mg0 > 0) & (df.mg1 > 0) & (df.mg2 > 0) & (df.eg0 > 0) & (df.rg0 > 0))
    add("f_code33", "Code 33 (Minervini)", E,
        "Three quarters in a row of acceleration in EPS growth AND revenue growth (each quarter's year-on-year growth "
        "faster than the quarter before), with net margin above the year-ago quarter in each of those three quarters — "
        "Mark Minervini's 'Code 33', from the company's SEC filings.",
        c33, "EPS YoY %", df.eg0, "pct", "rs")
    mcap_ = ctx["mcap"].reindex(U)
    add("f_kell", "Kell growth universe", E,
        "Price over $20, market cap $2B–25B, and quarterly EPS and revenue both up more than 30% on last year — the "
        "list Oliver Kell starts from (as set up in Deepvue) before looking for his price-cycle setups.",
        (ctx["c"].reindex(U) > 20) & (mcap_ >= 2000) & (mcap_ <= 25000) & (df.eg0 > 30) & (df.rg0 > 30),
        "EPS YoY %", df.eg0, "pct", "rs")
    add("f_sepa", "SEPA: trend template + earnings", E,
        "Passes Minervini's trend template and the business backs it: profit +20% and revenue +15% on last year, ROE 15%+.",
        tt & (df.pat_yoy >= 20) & (df.rev_yoy >= 15) & (df.roe >= 15), "Profit YoY %", df.pat_yoy, "pct", "rs")
    add("f_margin", "Margin expansion", E,
        "Operating margin up 3+ points on the same quarter last year and up on a trailing-year basis, revenue growing 15%+, "
        "RS 75+ — operating leverage kicking in (not for financials).",
        nb & (df.opm_chg >= 3) & (df.opm_ttm_chg > 0) & (df.rev_yoy >= 15) & (df.pat_ttm > 0) & (rs >= 75), "Margin change (pts)", df.opm_chg)
    add("f_streak", "Revenue growth streak", E,
        "Revenue up 10%+ year on year for 5+ quarters in a row, trailing-year profit up 20%+, RS 70+ and above the "
        "200 DMA — consistent growers the market is rewarding.",
        (df.rev_streak >= 5) & (df.pat_ttm_g >= 20) & (rs >= 70) & (a200 == True), "Quarters in a row", df.rev_streak, "int")
    add("f_turn", "Turnaround", E,
        "Profitable in the latest quarter and the one before, after a loss in the same quarter last year — and the stock is "
        "above its 50 DMA.",
        (df.pat_q > 0) & (df.pat_q1 > 0) & (df.pat_q4 < 0) & (ctx["a50"].reindex(U) == True),
        "Quarter profit $M", df.pat_q, "num")
    # results day: the earnings date (Yahoo) if known, else the 10-Q / 10-K filing date
    lr = pd.to_datetime(df["last_results"], errors="coerce") if "last_results" in df else pd.Series(pd.NaT, index=U)
    fl = pd.to_datetime(df["filed"], errors="coerce")
    rec = lr.where(lr.notna() & ((fl.isna()) | (lr >= fl - pd.Timedelta(days=45))), fl)
    days = (asof - rec).dt.days
    C = ctx["C"]
    pre = pd.Series(np.nan, index=U)
    for s in U:
        f = rec.get(s)
        if pd.notna(f) and s in C and 0 <= days.get(s, 999) <= 20:
            before = C[s][C.index < f.strftime("%Y-%m-%d")]
            if len(before):
                pre[s] = before.iloc[-1]
    react = (c / pre - 1) * 100
    df["results_day"] = rec.dt.strftime("%Y-%m-%d")
    add("f_post", "Post-earnings breakout", E,
        "Earnings reported in the last 20 sessions with profit up 20%+, and the stock is 5%+ above its close before the "
        "report — the market is buying the numbers (earnings episodic pivot / post-earnings drift).",
        (days <= 20) & (days >= 0) & (df.pat_yoy >= 20) & (react >= 5), "Since earnings %", react)
    nr = pd.to_datetime(df["next_results"], errors="coerce") if "next_results" in df else pd.Series(pd.NaT, index=U)
    dleft = (nr - asof).dt.days
    add("f_upcoming", "Earnings in the next 7 days", E,
        "Earnings date within a week (Yahoo Finance calendar). RS 60+ only — the strong stocks worth watching into the report.",
        (dleft >= 0) & (dleft <= 7) & (rs >= 60), "Days to earnings", dleft, "int", "metric_asc")

    add("f_quality", "Quality compounders", Q,
        "ROCE 20%+, ROE 18%+, debt/equity 0.5 or less, positive free cash flow last year, operating margin 15%+, revenue up 12%+ "
        "on a trailing basis, price above the 200 DMA (not for financials).",
        nb & (df.roce >= 20) & (df.roe >= 18) & (df.de <= .5) & (df.fcf > 0) & (df.opm_ttm >= 15)
        & (df.rev_ttm_g >= 12) & (a200 == True), "ROCE %", df.roce)
    add("f_garp", "Growth at a reasonable price", Q,
        "PEG 0.8 or less (P/E ÷ trailing profit growth), profit growth 20%+, ROE 15%+, P/E under 35, RS 60+ and above the "
        "200 DMA.",
        (df.peg <= .8) & (df.pat_ttm_g >= 20) & (df.roe >= 15) & (df.pe < 35) & (rs >= 60) & (a200 == True), "PEG", df.peg, "num", "metric_asc")
    add("f_capex", "Capex cycle", Q,
        "Capital spending up 50%+ last fiscal year and at least 5% of revenue — new capacity being built that isn't in the "
        "numbers yet — with debt/equity ≤ 1 and revenue growing.",
        nb & (df.capex_g >= 50) & (df.capex_rev >= 5) & (df.de <= 1) & (df.rev_ttm_g > 0), "Capex growth %", df.capex_g)
    add("f_netcash", "Net-cash small caps", Q,
        "Cash above total debt, market cap under $2 billion, positive free cash flow and P/E under 25.",
        nb & (df.cash > df.debt.fillna(0)) & (mc < 2000) & (df.fcf > 0) & (df.pe > 0) & (df.pe < 25), "P/E", df.pe, "num", "metric_asc")

    add("f_insider", "Insider buying cluster", O,
        "Two or more insiders (officers, directors) bought shares on the open market in the last 90 days, $250k+ in total — "
        "people who know the business putting in their own money (SEC Form 4, purchases only).",
        (df.ins_buyers90 >= 2) & (df.ins_buy90 >= .25), "Bought $M (90d)", df.ins_buy90, "num")
    add("f_exec_buy", "CEO / CFO buying", O,
        "The CEO, CFO, president or chairman bought $100k+ of stock on the open market in the last 90 days (SEC Form 4).",
        df.ins_exec_buy90 >= .1, "Bought $M (90d)", df.ins_exec_buy90, "num")
    add("f_buyback", "Shrinking share count", O,
        "Diluted share count down 3%+ in a year (buybacks), positive free cash flow, profitable and RS 60+ — every share "
        "owns more of the company each quarter, and the market is noticing.",
        (df.sh_yoy <= -3) & (df.fcf > 0) & (df.pat_ttm > 0) & (rs >= 60), "Shares Δ1Y %", df.sh_yoy, "pct", "metric_asc")
    add("f_inst", "Institutions moving in", O,
        "Institutional ownership up 3+ points over about a year and still rising, RS 60+ (Yahoo ownership snapshots; this "
        "list fills in as the monthly history builds up).",
        (df.inst_1y >= 3) & (df.inst_1q > 0) & (rs >= 60), "Inst. Δ1Y (pts)", df.inst_1y)
    add("f_under", "Under-owned leaders", O,
        "Institutions own less than 40%, profit up 25%+ on revenue up 15%+, Fund rating 70+ and RS 80+ — strong businesses "
        "the big funds haven't loaded up on yet.",
        (df.inst < 40) & (df.pat_yoy >= 25) & (df.rev_yoy >= 15) & (df.fr >= 70) & (rs >= 80), "Inst. holding %", df.inst, "pct", "rs")

    flags = pd.DataFrame(index=df.index)
    flags["Heavy insider selling (over 1% of the company in 90 days)"] = (df.ins_sell90 > 0.01 * mc) & (df.ins_sellers90 >= 2)
    flags["Share count up 10%+ in a year (dilution)"] = df.sh_yoy >= 10
    flags["Negative operating cash flow last year despite a profit"] = nb & (df.ocf < 0) & (df.ocf_pat.notna())
    flags["Debt/equity above 1.5 and rising"] = nb & (df.de > 1.5) & (df.debt_chg > 0)
    flags["Profit down 25%+ with revenue falling"] = (df.pat_yoy <= -25) & (df.rev_yoy < 0)
    nflags = flags.sum(axis=1)
    add("f_red", "Fundamental red flags", R,
        "Two or more different warning signs: heavy insider selling, share dilution, rising high debt, negative operating cash "
        "flow despite a profit, or profit and revenue both falling. An exit check for holdings — open the stock card for which ones.",
        nflags >= 2, "Flags", nflags, "int")

    # ── per-stock fields for stocks.json
    keep = ["fr", "pe", "ps", "peg", "pb", "roe", "roce", "de", "opm", "opm_chg", "npm", "rev_yoy", "pat_yoy", "pat_yoy1", "pat_yoy2",
            "eps_yoy", "rev_qoq", "pat_qoq", "rev_ttm", "pat_ttm", "rev_ttm_g", "pat_ttm_g", "rev_streak", "fcf", "fcf_yield", "ocf_pat",
            "capex_g", "sh_yoy", "insiders", "inst", "inst_count", "inst_1q", "inst_1y", "ins_buy90", "ins_sell90", "ins_buyers90",
            "ins_net365", "ins_last_buy", "qe", "filed", "next_results", "next_est", "results_day", "shp_q", "bank", "fin", "fy", "roe_fy"]
    per = {}
    fl_ = flags.apply(lambda r: [k for k, v in r.items() if v is True or v == True], axis=1)
    for s in U:
        if s not in df.index or pd.isna(df.loc[s, "fr"]) and s not in M:
            continue
        row = {}
        for k in keep:
            if k not in df:
                continue
            v = df.loc[s, k]
            if isinstance(v, (bool, np.bool_)):
                row[k] = bool(v)
            elif isinstance(v, (float, np.floating)):
                if np.isfinite(v):
                    row[k] = round(float(v), 2)
            elif isinstance(v, (int, np.integer)):
                row[k] = int(v)
            elif isinstance(v, str):
                row[k] = v
        if fl_.get(s):
            row["flags"] = fl_[s]
        per[s] = row
    return {"per_stock": per, "detail": detail, "industry": industry, "meta": meta, "hits": hits, "metric": metric, "cats": CATS}
