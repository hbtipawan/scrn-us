"""Download daily candles for every US stock and ETF + benchmark indices.

Universe (no login, no API key):
  1. Nasdaq Trader's daily symbol directory (nasdaqtraded.txt) - every security traded on a
     US exchange (NYSE, Nasdaq, NYSE American, NYSE Arca, Cboe), with an ETF flag.
     Warrants, units, rights, preferreds, notes, test issues, SPAC shells, closed-end funds
     and leveraged / inverse ETFs are dropped.
  2. A Yahoo batch quote for all of them (about 90 requests) gives price, 3-month average
     volume, market cap and the next earnings date. Only names with price >= prefilter_min_price
     and average daily dollar volume >= prefilter_min_dollar_volume_m are downloaded.

Prices: Yahoo Finance chart API, split-adjusted (not dividend-adjusted), US Eastern dates.
  --source yfinance   uses the yfinance library instead (backup path, same data vendor).

Output: cache/prices.csv.gz  (symbol,date,open,high,low,close,volume)
        cache/universe.csv   (symbol,name,kind,exch,key)    kind = stock / etf / index
        cache/quotes.csv     Yahoo quote snapshot (market cap, earnings date ...)

Run:  python scripts/fetch_prices.py
      python scripts/fetch_prices.py --limit 50     (quick test)
"""
import argparse
import io
import json
import os
import re
import sys
import time
from datetime import timedelta
from pathlib import Path

import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
import yahoo as Y  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE, CFG = ROOT / "cache", ROOT / "config"
SETTINGS = json.loads((CFG / "settings.json").read_text())

# Display name -> Yahoo index symbol
INDEX_KEYS = {
    "S&P 500": "^GSPC",
    "Nasdaq 100": "^NDX",
    "Dow Jones": "^DJI",
    "Russell 2000": "^RUT",
    "S&P 400 Mid": "^SP400",
    "S&P 600 Small": "^SP600",
}

EXCH = {"Q": "NASDAQ", "N": "NYSE", "A": "AMEX", "P": "AMEX", "Z": "CBOE", "V": "IEX"}  # TradingView prefixes (NYSE Arca = AMEX, Cboe BZX = CBOE)

BAD_NAME = re.compile(
    r"warrant|\brights?\b|(?<!common )(?<!partnership )\bunits?\b(?! represent)|preferred|preference|"
    r"\bnotes?\b|debenture|subordinated|%|\bescrow\b|"
    r"acquisition (?:corp|corporation|company|co\b|holdings|inc|ltd|limited)|"
    r"\bclosed[- ]end\b|municipal|\bfund\b|\bincome trust\b|\btrust units\b|\bcontingent value\b", re.I)
BAD_ETF = re.compile(
    r"\b(?:1\.25|1\.5|1\.75|2|3|4|5)x\b|-[1-3]x\b|\bultra\b|ultrapro|leveraged|inverse|\bbear\b|\bbull\b|"
    r"\bshort (?!term|duration|maturity|treasury|bond|-term)|daily target|\b2x\b|\bdouble\b|\btriple\b|"
    r"\bvix\b|volatility (?:index )?futures|option income strategy|yieldmax|weekly ?pay|\bsingle[- ]stock\b", re.I)
BAD_SYM = re.compile(r"[$^#]|\.(?:U|W|WS|R|RT|WI|V)$|\.(?:PR|P)[A-Z]?$")


def build_universe():
    """All US-listed common stocks and plain ETFs from nasdaqtraded.txt."""
    cached = CACHE / "nasdaqtraded.txt"
    try:
        r = requests.get("https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqtraded.txt",
                         headers={"User-Agent": Y.UA}, timeout=90)
        r.raise_for_status()
        text = r.text
        cached.write_text(text)
    except Exception as e:
        if not cached.exists():
            raise
        print(f"WARN symbol directory not downloaded ({e}); using cached copy")
        text = cached.read_text()
    d = pd.read_csv(io.StringIO(text), sep="|", dtype=str, keep_default_na=False)
    d = d[(d["Nasdaq Traded"] == "Y") & (d["Test Issue"] == "N") & (d["Symbol"] != "")]
    d = d[~d["Symbol"].str.contains(BAD_SYM)]
    d = d[d["NextShares"] != "Y"] if "NextShares" in d else d
    etf = d["ETF"] == "Y"
    name = d["Security Name"]
    keep = (~etf & ~name.str.contains(BAD_NAME)) | (etf & ~name.str.contains(BAD_ETF))
    d = d[keep]
    # one-letter suffix share classes (BRK.B, BF.B) are kept; drop odd suffixes
    d = d[~d["Symbol"].str.contains(r"\.[A-Z]{2,}$")]
    rows = [(r.Symbol, clean_name(r["Security Name"]), "etf" if r.ETF == "Y" else "stock",
             EXCH.get(r["Listing Exchange"], "NYSE"), Y.ysym(r.Symbol)) for _, r in d.iterrows()]
    u = pd.DataFrame(rows, columns=["symbol", "name", "kind", "exch", "key"]).drop_duplicates("symbol")
    return u


def clean_name(n):
    n = re.split(r"\s+-\s+|\s+(Common Stock|Common Shares|Ordinary Shares|Class [A-Z] (Common|Ordinary)|American Depositary)", str(n))[0]
    return n.strip()[:60]


def prefilter(u):
    """Batch-quote everything; keep names liquid enough to matter (plus curated ETFs and theme stocks)."""
    t0 = time.time()
    q = Y.quotes(u.symbol.tolist())
    print(f"Yahoo quotes: {len(q)} of {len(u)} symbols in {time.time() - t0:.0f}s")
    if len(q) < 0.5 * len(u):
        raise RuntimeError("Yahoo quote service returned too little - try again later")
    q.to_csv(CACHE / "quotes.csv")
    px = pd.to_numeric(q["regularMarketPrice"], errors="coerce")
    adv = pd.to_numeric(q["averageDailyVolume3Month"], errors="coerce") * px / 1e6
    qt = q["quoteType"].astype(str)
    ok = (px >= SETTINGS["prefilter_min_price"]) & (adv >= SETTINGS["prefilter_min_dollar_volume_m"]) & qt.isin(["EQUITY", "ETF"])
    must = set()
    for f, col in ((CFG / "etf_groups.csv", "ETF"), (CFG / "themes.csv", "Symbols")):
        if f.exists():
            for v in pd.read_csv(f)[col].dropna():
                must.update(str(v).split())
    keep = set(ok.index[ok]) | (must & set(q.index))
    u = u[u.symbol.isin(keep)].copy()
    # Yahoo's own type wins (some "ETF" rows in the directory are ETNs or trusts and vice versa)
    u["kind"] = u.symbol.map(lambda s: "etf" if q.at[s, "quoteType"] == "ETF" else "stock")
    nm = q["longName"].fillna(q["shortName"])
    u["name"] = [clean_name(nm.get(s)) if isinstance(nm.get(s), str) else n for s, n in zip(u.symbol, u.name)]
    return u


def yf_download(rows, days):
    """Backup path: yfinance in batches."""
    import yfinance as yf
    out, failed = {}, []
    syms = [r.key for r in rows]
    back = {r.key: r.symbol for r in rows}
    start = (Y.today_et() - timedelta(days=days)).isoformat()
    for i in range(0, len(syms), 200):
        ch = syms[i:i + 200]
        try:
            df = yf.download(ch, start=start, interval="1d", auto_adjust=False, actions=False, threads=True,
                             group_by="ticker", progress=False)
        except Exception as e:
            print(f"  yfinance batch failed: {e}")
            failed += [back[s] for s in ch]
            continue
        for s in ch:
            try:
                g = df[s] if isinstance(df.columns, pd.MultiIndex) else df
                g = g.dropna(subset=["Close"])
                if not len(g):
                    failed.append(back[s])
                    continue
                d = pd.DataFrame({"date": pd.DatetimeIndex(g.index).strftime("%Y-%m-%d"), "open": g["Open"].values,
                                  "high": g["High"].values, "low": g["Low"].values, "close": g["Close"].values,
                                  "volume": g["Volume"].fillna(0).values})
                out[back[s]] = d
            except Exception:
                failed.append(back[s])
        print(f"  yfinance {min(i + 200, len(syms))}/{len(syms)}", flush=True)
    return out, failed


def drop_bad_ticks(p):
    """Remove one-day price spikes that reverse the next day (a bad print or a split applied a day late):
    close more than 2.5x or less than 0.4x the day before, and the next close back within 1.5x of the day before."""
    p = p.sort_values(["symbol", "date"]).reset_index(drop=True)
    g = p.groupby("symbol", sort=False)["close"]
    prev, nxt = g.shift(1), g.shift(-1)
    r = p["close"] / prev
    back = nxt / prev
    bad = ((r > 2.5) | (r < 0.4)) & (back > 1 / 1.5) & (back < 1.5)
    if bad.any():
        print(f"Dropped {int(bad.sum())} one-day bad ticks: {', '.join(sorted(set(p.loc[bad, 'symbol'])))[:300]}")
    return p[~bad]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["yahoo", "yfinance"], default=os.environ.get("PRICE_SOURCE", "yahoo"))
    ap.add_argument("--limit", type=int, default=0, help="only first N stocks (testing)")
    a = ap.parse_args()

    CACHE.mkdir(exist_ok=True)
    u = build_universe()
    print(f"Symbol directory: {(u.kind == 'stock').sum()} stocks, {(u.kind == 'etf').sum()} ETFs")
    if a.limit:
        prio = set()
        for f, col in ((CFG / "etf_groups.csv", "ETF"),):
            if f.exists():
                for v in pd.read_csv(f)[col].dropna():
                    prio.update(str(v).split())
        u = pd.concat([u[u.symbol.isin(prio)], u[(u.kind == "stock")].head(a.limit)]).drop_duplicates("symbol")
    u = prefilter(u)
    idx = pd.DataFrame([(name, name, "index", "INDEX", key) for name, key in INDEX_KEYS.items()
                        if name in [SETTINGS["benchmark"]] + SETTINGS["extra_indices"]],
                       columns=["symbol", "name", "kind", "exch", "key"])
    u = pd.concat([u, idx], ignore_index=True)
    u.to_csv(CACHE / "universe.csv", index=False)
    print(f"Universe: {(u.kind == 'stock').sum()} stocks + {(u.kind == 'etf').sum()} ETFs + "
          f"{(u.kind == 'index').sum()} indices; source={a.source}")

    # the long-base identifier (bases.py) needs ~6 years; everything else uses history_calendar_days
    days = max(SETTINGS["history_calendar_days"], SETTINGS.get("base_history_calendar_days", 0))
    rows = list(u.itertuples())
    t0 = time.time()
    if a.source == "yahoo":
        rl = Y.RateLimiter(SETTINGS.get("yahoo_requests_per_second", 15))

        def job(row):
            rl.wait()
            return Y.chart(row.key, days=days)
        data, failed = Y.run_pool(rows, job, 8, "history", key=lambda r: r.symbol)
        # Yahoo sometimes refuses a short burst of requests: give those symbols a slower second try
        for attempt in range(2):
            if not failed:
                break
            time.sleep(20)
            again = [r for r in rows if r.symbol not in data]
            print(f"  retrying {len(again)} symbols")
            rl2 = Y.RateLimiter(4)

            def job2(row):
                rl2.wait()
                return Y.chart(row.key, days=days)
            more, failed = Y.run_pool(again, job2, 3, "retry", key=lambda r: r.symbol)
            data.update(more)
    else:
        data, failed = yf_download(rows, days)

    if not data:
        sys.exit("No data downloaded")
    # A bar for today is only final after the close: drop it if the job runs during the session.
    today = Y.today_et().isoformat()
    partial = not Y.session_done()
    out = []
    for s, d in data.items():
        d = d.drop_duplicates("date", keep="last").copy()
        if partial:
            d = d[d.date < today]
        d.insert(0, "symbol", s)
        out.append(d)
    p = pd.concat(out, ignore_index=True)
    p = drop_bad_ticks(p)
    p.to_csv(CACHE / "prices.csv.gz", index=False, compression="gzip")
    (CACHE / "fetch_meta.json").write_text(json.dumps({
        "source": a.source, "symbols": p.symbol.nunique(), "rows": len(p),
        "last_date": p.date.max(), "failed": failed[:200], "failed_count": len(failed),
        "seconds": round(time.time() - t0)}, indent=1))
    print(f"Saved {p.symbol.nunique()} symbols, {len(p)} rows, last date {p.date.max()}, "
          f"failed {len(failed)}, {time.time() - t0:.0f}s")
    # Guard: a broken run must never overwrite the website.
    if len(failed) > 0.3 * len(rows):
        sys.exit("Too many failures - not publishing")


if __name__ == "__main__":
    main()
