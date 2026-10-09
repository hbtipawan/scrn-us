"""Refresh index memberships for the US dashboard.

Sources (free, reachable from cloud servers):
  S&P 500 / 400 / 600 - Wikipedia's constituent tables (editors update them within a day or two
                        of S&P's announcements; GICS sector and sub-industry included)
  Nasdaq-100          - German Wikipedia's constituent table (the English page no longer lists them)
  Dow Jones 30        - holdings file of the SPDR Dow Jones ETF (DIA), published daily by State Street
Writes:
  config/indices.csv  -> Index, Symbols   S&P 500 (and its 11 GICS sectors), S&P MidCap 400,
                                          S&P SmallCap 600, Nasdaq-100, Dow Jones 30
  config/gics.csv     -> Symbol, Company, Sector, SubIndustry, Index   (S&P 1500 members)

If a download fails, the existing rows are kept, so the daily job never breaks on this step.
Run:  python scripts/update_reference.py
"""
import io
import sys
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / "config"
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126 Safari/537.36"}
WIKI = "https://en.wikipedia.org/wiki/"

PAGES = {
    "S&P 500": "List_of_S%26P_500_companies",
    "S&P MidCap 400": "List_of_S%26P_400_companies",
    "S&P SmallCap 600": "List_of_S%26P_600_companies",
    "Nasdaq-100": "https://de.wikipedia.org/wiki/NASDAQ-100",
    "Dow Jones 30": "DIA",
}
SSGA = "https://www.ssga.com/us/en/intermediary/library-content/products/fund-data/etfs/us/holdings-daily-us-en-{}.xlsx"
SYMBOL_COLS = ("Symbol", "Ticker", "Ticker symbol")


def norm(s):
    return str(s).strip().upper().replace("-", ".").replace("​", "")


def spdr_holdings(etf):
    """Holdings of a State Street SPDR ETF (daily xlsx)."""
    r = requests.get(SSGA.format(etf.lower()), headers=UA, timeout=60)
    r.raise_for_status()
    raw = pd.read_excel(io.BytesIO(r.content), header=None)
    hdr = next(i for i in range(min(15, len(raw))) if "Ticker" in [str(x).strip() for x in raw.iloc[i]])
    t = pd.read_excel(io.BytesIO(r.content), skiprows=hdr)
    t.columns = [str(c).strip() for c in t.columns]
    t = t.rename(columns={"Ticker": "Symbol", "Name": "Company"}).dropna(subset=["Symbol"])
    t["Symbol"] = t["Symbol"].map(norm)
    t = t[t["Symbol"].str.match(r"^[A-Z][A-Z0-9.]{0,6}$") & (t["Symbol"] != "CASH")]
    if len(t) < 25:
        raise ValueError(f"{etf}: only {len(t)} holdings")
    return t


def members(page):
    """The first table on the page with a ticker column and 25+ rows."""
    if page.isupper() and len(page) <= 5:
        return spdr_holdings(page)
    r = requests.get(page if page.startswith("http") else WIKI + page, headers=UA, timeout=60)
    r.raise_for_status()
    for t in pd.read_html(io.StringIO(r.text)):
        if isinstance(t.columns, pd.MultiIndex):
            t.columns = [c[-1] for c in t.columns]
        t.columns = [str(c).split("[")[0].strip() for c in t.columns]
        col = next((c for c in SYMBOL_COLS if c in t.columns), None)
        if col and len(t) >= 25:
            t = t.rename(columns={col: "Symbol"})
            t["Symbol"] = t["Symbol"].map(norm)
            return t[t["Symbol"].str.match(r"^[A-Z][A-Z0-9.]{0,6}$")]
    raise ValueError(f"{page}: no constituent table found")


def main():
    CFG.mkdir(exist_ok=True)
    old = pd.read_csv(CFG / "indices.csv") if (CFG / "indices.csv").exists() else pd.DataFrame(columns=["Index", "Symbols"])
    prev = dict(zip(old["Index"], old["Symbols"]))
    rows, failed, gics = {}, [], []
    for name, page in PAGES.items():
        try:
            t = members(page)
            rows[name] = " ".join(sorted(set(t["Symbol"])))
            if "GICS Sector" in t.columns:
                sub = t.get("GICS Sub-Industry", pd.Series([""] * len(t), index=t.index))
                co = t.get("Security", t.get("Company", pd.Series([""] * len(t), index=t.index)))
                gics.append(pd.DataFrame({"Symbol": t["Symbol"], "Company": co, "Sector": t["GICS Sector"],
                                          "SubIndustry": sub, "Index": name}))
                if name == "S&P 500":
                    for sec, g in t.groupby("GICS Sector"):
                        rows[f"S&P 500 {sec}"] = " ".join(sorted(set(g["Symbol"])))
            print(f"{name}: {len(t)} members")
        except Exception as e:
            failed.append(f"{name} ({e})")
    for k, v in prev.items():                 # keep previous membership for anything that failed
        if k not in rows and (k in PAGES or k.startswith("S&P 500 ")):
            rows[k] = v
    if rows:
        order = list(PAGES) + sorted(k for k in rows if k not in PAGES)
        pd.DataFrame([(k, rows[k]) for k in order if k in rows], columns=["Index", "Symbols"]).to_csv(CFG / "indices.csv", index=False)
        print(f"indices.csv: {len(rows)} indices")
    if gics:
        g = pd.concat(gics).drop_duplicates("Symbol")
        g.sort_values("Symbol").to_csv(CFG / "gics.csv", index=False)
        print(f"gics.csv: {len(g)} stocks")
    if failed:
        print("WARN failed: " + "; ".join(failed), file=sys.stderr)


if __name__ == "__main__":
    main()
