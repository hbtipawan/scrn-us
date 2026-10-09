"""20-year US backtest behind the Long-base identifier and the Stage 2 phases.

Re-runs, on US stocks, the same tests the Indian edition ran on NSE data, with the same code
(bases.py / stage2.py) for detecting bases, breakouts, base counts, the Base Score and the Stage 2
Score. Writes the tables the website shows:
  config/base_evidence.json      breakouts from bases: 1-year return vs the S&P 500 by base length
                                 and Base Score, 2007-16 and 2017-26 separately
  config/stage2_evidence.json    Stage 2 snapshots every 10 sessions: early / mid / late phase,
                                 Stage 2 Score, base count, months in Stage 2, distance above the MAs

Universe: today's dashboard universe (stocks only) - so delisted companies are missing, which
flatters every bucket a little (said on the site). Point-in-time RS rating each day.
Liquidity: $1M+ median daily dollar volume at the time of each event.

Run once (takes ~15 minutes, downloads ~20 years of daily candles):
    python scripts/backtest_us.py
The results are filled into the findings by hand-written text in the JSON (see FINDINGS below).
"""
import json
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent))
import bases as BS  # noqa: E402
import stage2 as S2  # noqa: E402
import yahoo as Y  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE, CFG = ROOT / "cache", ROOT / "config"
START = "2005-01-01"
EVENT_FROM, SPLIT = "2007-01-01", "2017-01-01"
H, H6 = 252, 126
LIQ = 1.0          # $ millions median daily dollar volume


def download(symbols):
    f = CACHE / "bt_prices.csv.gz"
    if f.exists():
        p = pd.read_csv(f, dtype={"symbol": str, "date": str})
        have = set(p.symbol)
        need = [s for s in symbols if s not in have]
    else:
        p, need = None, list(symbols)
    if need:
        rl = Y.RateLimiter(12)

        def job(s):
            rl.wait()
            return Y.chart("^GSPC" if s == "S&P 500" else s, start=START)
        got, failed = Y.run_pool(need, job, 8, "20y history")
        for _ in range(2):
            again = [s for s in need if s not in got]
            if not again:
                break
            time.sleep(15)
            more, failed = Y.run_pool(again, job, 3, "retry")
            got.update(more)
        frames = [d.assign(symbol=s) for s, d in got.items()]
        new = pd.concat(frames, ignore_index=True)[["symbol", "date", "close", "volume"]] if frames else None
        p = pd.concat([x for x in (p, new) if x is not None], ignore_index=True)
        p.to_csv(f, index=False, compression="gzip")
        print(f"downloaded {len(got)} new symbols, {len(failed)} failed")
    return p


def trimmed(x):
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if len(x) < 20:
        return float(np.mean(x)) if len(x) else np.nan
    lo, hi = np.percentile(x, [0.5, 99.5])
    return float(np.mean(x[(x >= lo) & (x <= hi)]))


def summary(df, extra=()):
    if not len(df):
        return {"n": 0, "avg": 0.0, "med": 0.0, "win": 0.0, **{k: 0.0 for k in extra}}
    out = {"n": int(len(df)), "avg": round(trimmed(df.ex), 1), "med": round(float(df.ex.median()), 1),
           "win": round(float((df.ex > 0).mean() * 100), 1)}
    for k in extra:
        out[k] = round(float(df[k].median()), 1) if k == "mdd" else round(float(df[k].mean() * 100), 1)
    return out


def table(df, col, labels, extra):
    return [{"label": lab, **summary(df[df[col] == lab], extra)} for lab in labels]


def main():
    rows = json.loads((ROOT / "docs" / "data" / "stocks.json").read_text())
    U = sorted(r["s"] for r in rows if r.get("k") != "e")
    print(f"backtest universe: {len(U)} stocks")
    p = download(U + ["S&P 500"])
    p = p[p.close > 0]
    bench = p[p.symbol == "S&P 500"].set_index("date")["close"].sort_index()
    cal = list(bench.index)
    C = p.pivot_table(index="date", columns="symbol", values="close", aggfunc="last").reindex(cal)
    V = p.pivot_table(index="date", columns="symbol", values="volume", aggfunc="last").reindex(cal).fillna(0)
    C = C[[s for s in U if s in C.columns]]
    V = V[C.columns]
    print(f"{C.shape[1]} stocks, {len(cal)} sessions {cal[0]} - {cal[-1]}")
    # point-in-time RS rating (same formula as compute.py)
    Cf = C.ffill(limit=5)
    parts, w = [], [0.4, 0.2, 0.2, 0.2]
    for n in (63, 126, 189, 252):
        parts.append(Cf / Cf.shift(n))
    sc = sum(wi * pi for wi, pi in zip(w, parts))
    avail = sum(wi * pi.notna() for wi, pi in zip(w, parts))
    part_sum = sum(wi * pi.fillna(0) for wi, pi in zip(w, parts))
    sc = sc.fillna(part_sum / avail.replace(0, np.nan))
    sc = sc.where(Cf.notna() & (Cf.notna().cumsum() >= 63))
    RS = (sc.rank(axis=1, pct=True) * 98 + 1).round()
    TO = (C * V / 1e6).rolling(50, min_periods=20).median()
    bvals = bench.to_numpy(float)
    dates = np.array(cal)
    ev_rows, s2_rows = [], []
    t0 = time.time()
    for k, s in enumerate(C.columns):
        cs_ = C[s]
        first = cs_.first_valid_index()
        if first is None:
            continue
        i0 = cal.index(first)
        c = cs_.iloc[i0:].ffill().to_numpy(float)
        v = V[s].iloc[i0:].to_numpy(float)
        n = len(c)
        if n < 400:
            continue
        b = bvals[i0:]
        d = dates[i0:]
        rs = RS[s].iloc[i0:].to_numpy(float)
        to = TO[s].iloc[i0:].to_numpy(float)
        sma150 = pd.Series(c).rolling(150).mean().to_numpy()
        sma200 = pd.Series(c).rolling(200).mean().to_numpy()
        sma50 = pd.Series(c).rolling(50).mean().to_numpy()
        slope = sma150 / np.roll(sma150, 20) - 1
        slope[:170] = np.nan
        # forward outcomes
        idx = np.arange(n)
        fwd = np.where(idx + H < n, np.roll(c, -H) / c - 1, np.nan)
        bfwd = np.where(idx + H < n, np.roll(b, -H) / b - 1, np.nan)
        ex = (fwd - bfwd) * 100

        # ── base breakouts: first breakout of each base (bases.py event code)
        evs = BS.recent_breakouts(c, 2, BS.D_MAX, BS.ZONE, BS.MIN_LEN)
        lt, lc = -1, None
        for r in evs:
            te, st, ce, fl, fa, pg, tc, tf, ci = int(r[0]), int(r[1]), r[2], r[3], int(r[4]), int(r[5]), r[6], r[7], int(r[8])
            if lt < 0 or st > lt or ci < lt:
                new = True
            elif c[lt + 1:te].size and c[lt + 1:te].min() < lc * 0.95:
                new = False                               # failed and broke out again: not a first breakout
            else:
                continue                                  # same breakout run
            lt, lc = te, ce
            if not new or not (to[te - 1] >= LIQ) or te + H >= n or d[te] < EVENT_FROM:
                continue
            length = te - st
            w15 = c[max(st, te - 15):te]
            tight15 = w15.max() / w15.min() - 1
            overhead = (c[max(0, pg - 500):pg + 1].max() / ce - 1) if pg >= 0 else 0.0
            vs200 = c[te] / sma200[te] - 1 if np.isfinite(sma200[te]) else np.nan
            sl = slope[te]
            score = BS.base_score(length, rs[te], vs200, sl, tight15, bool(fa), overhead)
            up = np.isfinite(sma200[te]) and np.isfinite(sl) and c[te] > sma150[te] and sl > 0 and c[te] > sma200[te]
            ev_rows.append((s, d[te], length, rs[te], up, score, ex[te],
                            c[te + 1:te + 21].min() < ce * 0.95, c[te:te + H + 1].max() / c[te] >= 2.0))

        # ── Stage 2 snapshots every 10th session
        stage = np.where((c > sma150) & (slope > .01), 2, np.where((c < sma150) & (slope < -.01), 4, 0))
        conf4 = pd.Series((stage == 4).astype(int)).rolling(20).sum().to_numpy() >= 10
        c4 = np.flatnonzero(conf4)
        fell = np.zeros(n, bool)                          # confirmed Stage 4 within the next 6 months
        if len(c4):
            nxt = np.searchsorted(c4, idx, side="right")
            ok = nxt < len(c4)
            fell[ok] = c4[nxt[ok]] <= idx[ok] + H6
        mdd = np.full(n, np.nan)
        lowf = pd.Series(c[::-1]).rolling(H6, min_periods=H6).min().to_numpy()[::-1]   # min of c[t..t+125]
        mdd[:n - 1] = np.roll(lowf, -1)[:n - 1] / c[:n - 1] - 1
        bigf = pd.Series(c[::-1]).rolling(H + 1, min_periods=H + 1).max().to_numpy()[::-1] / c >= 2.0
        g0 = cal.index(EVENT_FROM) if EVENT_FROM in cal else int(np.searchsorted(dates, EVENT_FROM))
        for t in range(171, n - H):
            if (i0 + t) % 10 or stage[t] != 2 or not (to[t] >= LIQ) or i0 + t < g0:
                continue
            l4 = c4[c4 <= t]
            lo_i = int(l4[-1]) + 1 if len(l4) else 170
            s2i = np.flatnonzero(stage[lo_i:t + 1] == 2)
            if not len(s2i):
                continue
            cs = lo_i + int(s2i[0])
            age = t - cs
            low = c[max(0, cs - 120):cs + 1].min()
            peak = c[cs:t + 1].max()
            sl = slope[t] if np.isfinite(slope[t]) else 0.0
            slope_chg = sl - slope[t - 40] if t >= 210 and np.isfinite(slope[t - 40]) else 0.0
            ext150 = c[t] / sma150[t] - 1
            ext200 = c[t] / sma200[t] - 1 if np.isfinite(sma200[t]) else np.nan
            dd_peak = c[t] / peak - 1
            a50 = bool(c[t] > sma50[t])
            ath = bool(c[t] >= c[max(0, t - 1500):t + 1].max() * 0.999)
            nb = base_count(evs, c, to, cs, t)
            mp = S2.maturity_points(nb, age, c[t] / low - 1, slope_chg)
            sp = S2.score_parts(rs[t], sl, ext150, dd_peak, a50, ath)
            s2_rows.append((s, d[t], S2.phase_of(mp), int(sum(sp)), nb, age, ext150, ext200, ex[t], fell[t], mdd[t] * 100, bigf[t]))
        if (k + 1) % 250 == 0:
            print(f"  {k + 1}/{C.shape[1]} stocks  {len(ev_rows)} breakouts  {len(s2_rows)} Stage 2 snapshots  {time.time() - t0:.0f}s", flush=True)

    E = pd.DataFrame(ev_rows, columns=["s", "date", "len", "rs", "up", "score", "ex", "fail", "big"])
    Z = pd.DataFrame(s2_rows, columns=["s", "date", "ph", "sc", "bases", "age", "e150", "e200", "ex", "end", "mdd", "big"])
    E.to_csv(CACHE / "bt_breakouts.csv.gz", index=False, compression="gzip")
    Z.to_csv(CACHE / "bt_stage2.csv.gz", index=False, compression="gzip")
    write_evidence(E, Z, C.shape[1], cal)


def base_count(evs, c, to, cs, t):
    """stage2.base_count on pre-computed breakout events, using only events up to bar t."""
    lt, lc = -1, None
    cnt, lastc, lastf = 0, None, None
    for r in evs:
        te, st, ce, fl, ci = int(r[0]), int(r[1]), r[2], r[3], int(r[8])
        if te > t:
            break
        if not (to[te - 1] >= LIQ):
            continue
        if lt < 0 or st > lt or ci < lt:
            retry = False
        elif c[lt + 1:te].size and c[lt + 1:te].min() < lc * 0.95:
            retry = True
        else:
            continue
        lt, lc = te, ce
        if retry or te < cs:
            continue
        if lastc is None or fl < lastf:
            cnt = 1
        elif ce >= 1.2 * lastc:
            cnt += 1
        else:
            continue
        lastc, lastf = ce, fl
    return cnt


def write_evidence(E, Z, nstocks, cal):
    E["lb"] = pd.cut(E["len"], [24, 49, 99, 199, 399, 799, 1e9], labels=["25–49", "50–99", "100–199", "200–399", "400–799", "800+"]).astype(str)
    E["sb"] = pd.cut(E["score"], [-1, 39, 54, 69, 79, 101], labels=["under 40", "40–54", "55–69", "70–79", "80+"]).astype(str)
    q = E["up"] & (E["rs"] >= 70)
    L = ["25–49", "50–99", "100–199", "200–399", "400–799", "800+"]
    SB = ["under 40", "40–54", "55–69", "70–79", "80+"]
    early, late = E[E.date < SPLIT], E[E.date >= SPLIT]
    ex = ("fail", "big")
    span = f"{pd.Timestamp(EVENT_FROM):%b %Y} – {pd.Timestamp(cal[-1]):%b %Y}"
    base = {
        "title": "What 20 years of US data say", "p1": "2007–16", "p2": "2017–26",
        "data": (f"Daily closes {span}, {nstocks:,} US stocks listed today, {len(E):,} first breakouts from bases of 25+ days "
                 f"with $1M+ daily dollar volume and a full year of data after. Returns are 1-year excess over the S&P 500 "
                 f"from the breakout close; 'Failed' = closed 5%+ back inside the base within 20 sessions; 'Doubled' = rose "
                 f"100%+ at some point within the year."),
        "len_quality": table(E[q], "lb", L, ex), "len_other": table(E[~q], "lb", L, ex),
        "score_all": table(E, "sb", SB, ex), "score_early": table(early, "sb", SB, ex), "score_late": table(late, "sb", SB, ex),
        "caveats": ("Only stocks still listed today (companies that were delisted or acquired are missing, which flatters all "
                    "returns a little, but affects every bucket). Averages are trimmed at the 0.5% tails. Past results are "
                    "evidence, not a promise."),
    }
    Z["band5"] = pd.cut(Z["sc"], [-1, 39, 54, 69, 84, 101], labels=["under 40", "40–54", "55–69", "70–84", "85+"]).astype(str)
    Z["band3"] = pd.cut(Z["sc"], [-1, 54, 69, 101], labels=["under 55", "55–69", "70+"]).astype(str)
    Z["phl"] = Z.ph.map({"E": "Early", "M": "Mid", "L": "Late"})
    Z["bl"] = Z.bases.clip(upper=5).map({0: "0 (first leg)", 1: "1", 2: "2", 3: "3", 4: "4", 5: "5+"})
    Z["ml"] = pd.cut(Z.age / 21, [-1, 3, 6, 9, 12, 18, 24, 1e9], labels=["under 3", "3–6", "6–9", "9–12", "12–18", "18–24", "24+"]).astype(str)
    Z["x150"] = pd.cut(Z.e150 * 100, [-1e9, 5, 10, 20, 30, 45, 65, 100, 1e9], labels=["0–5%", "5–10%", "10–20%", "20–30%", "30–45%", "45–65%", "65–100%", "over 100%"]).astype(str)
    Z["x200"] = pd.cut(Z.e200 * 100, [-1e9, 50, 100, 1e9], labels=["under 50%", "50–100%", "over 100%"]).astype(str)
    ex2 = ("end", "mdd", "big")
    ze, zl = Z[Z.date < SPLIT], Z[Z.date >= SPLIT]
    PH = ["Early", "Mid", "Late"]
    grid = [{"phase": ph, "band": bd, **summary(Z[(Z.phl == ph) & (Z.band3 == bd)], ex2)} for ph in PH for bd in ["under 55", "55–69", "70+"]]
    s2 = {
        "title": "What 20 years of US data say about Stage 2", "p1": "2007–16", "p2": "2017–26",
        "data": (f"Daily closes {span}. Every 10th session a stock was in Stage 2 (close above a 150-day average that rose "
                 f"1%+ in 20 sessions) with $1M+ daily dollar volume: {len(Z):,} snapshots from {Z.s.nunique():,} stocks still "
                 f"listed today. Returns are excess over the S&P 500 for the next year; 'Fell to Stage 4' = 10 of 20 sessions "
                 f"below a falling 150-day average within the next 6 months; 'Worst drop' = median lowest close in the next 6 months."),
        "phase_all": table(Z, "phl", PH, ex2), "phase_early": table(ze, "phl", PH, ex2), "phase_late": table(zl, "phl", PH, ex2),
        "score_early": table(ze, "band5", ["under 40", "40–54", "55–69", "70–84", "85+"], ex2),
        "score_late": table(zl, "band5", ["under 40", "40–54", "55–69", "70–84", "85+"], ex2),
        "score_all": table(Z, "band5", ["under 40", "40–54", "55–69", "70–84", "85+"], ex2),
        "grid": grid,
        "bases": table(Z, "bl", ["0 (first leg)", "1", "2", "3", "4", "5+"], ex2),
        "months": table(Z, "ml", ["under 3", "3–6", "6–9", "9–12", "12–18", "18–24", "24+"], ex2),
        "ext150": table(Z, "x150", ["0–5%", "5–10%", "10–20%", "20–30%", "30–45%", "45–65%", "65–100%", "over 100%"], ex2),
        "ext200": table(Z, "x200", ["under 50%", "50–100%", "over 100%"], ex2),
        "caveats": ("Only stocks still listed today (delisted and acquired companies are missing). Snapshots every 10 sessions "
                    "overlap, so these are averages, not independent trades. Averages are trimmed at the 0.5% tails. Past "
                    "results are evidence, not a promise."),
        "rules": json.loads((CFG / "stage2_evidence.json").read_text()).get("rules", "") if (CFG / "stage2_evidence.json").exists() else "",
    }
    old_b = json.loads((CFG / "base_evidence.json").read_text()) if (CFG / "base_evidence.json").exists() else {}
    old_s = json.loads((CFG / "stage2_evidence.json").read_text()) if (CFG / "stage2_evidence.json").exists() else {}
    base["findings"] = old_b.get("findings", [])
    s2["findings"] = old_s.get("findings", [])
    (CFG / "base_evidence.json").write_text(json.dumps(base, indent=1, ensure_ascii=False))
    (CFG / "stage2_evidence.json").write_text(json.dumps(s2, indent=1, ensure_ascii=False))
    print(json.dumps({k: base[k] for k in ("len_quality", "len_other", "score_all", "score_early", "score_late")}, indent=0, ensure_ascii=False)[:6000])
    print(json.dumps({k: s2[k] for k in ("phase_all", "phase_early", "phase_late", "score_all", "score_early", "score_late", "grid", "bases", "months", "ext150", "ext200")}, ensure_ascii=False)[:9000])


if __name__ == "__main__":
    main()
