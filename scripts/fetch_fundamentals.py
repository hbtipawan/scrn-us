"""Fundamentals for the US universe - all free, no login, no uploads.

  1. Quarterly results from SEC EDGAR XBRL (the numbers in every 10-Q and 10-K):
     SEC's nightly bulk file companyfacts.zip (~1.4 GB, about 30 seconds to download) holds one
     JSON per company. Only companies whose file changed since the last run are re-parsed.
     -> config/fund_results.csv  (about 12 quarters per stock: revenue, operating income, pre-tax
        profit, tax, net income, EPS, interest, D&A, diluted shares, balance sheet each quarter,
        operating cash flow and capex for each fiscal year)
  2. Insider buying and selling (SEC Form 4): SEC's quarterly insider-transaction data sets for
     the past year, then every Form 4 filed since, read from EDGAR's daily index.
     -> config/insider_tx.csv  (open-market purchases "P" and sales "S" only)
  3. Company profile, ownership and earnings dates from Yahoo Finance:
     sector, industry, % held by insiders and institutions (a monthly snapshot is kept, so the
     change builds up over time), next earnings date; for ETFs category, fund family, assets,
     expense ratio and top 10 holdings.
     -> config/fund_info.csv, config/fund_holdings.csv, config/etf_info.csv

SEC asks every program to identify itself with a contact e-mail in the User-Agent. Set the
repository variable SEC_CONTACT (e.g. "Your Name you@gmail.com") - see README.

Run:  python scripts/fetch_fundamentals.py            (normal incremental run)
      python scripts/fetch_fundamentals.py --full     (re-parse every company)
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ElementTree
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

sys.path.insert(0, str(Path(__file__).parent))
import yahoo as Y  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CACHE, CFG = ROOT / "cache", ROOT / "config"
S = json.loads((CFG / "settings.json").read_text())
STATE_F = CFG / "fund_meta.json"
SEC_UA = (os.environ.get("SEC_CONTACT") or S.get("sec_user_agent") or "").strip()
if SEC_UA and "@" in SEC_UA and not SEC_UA.lower().startswith("pkc"):
    SEC_UA = "PKC Sector Radar US " + SEC_UA
TODAY = Y.today_et()

# ────────────────────────────────────────────────────────── SEC plumbing
_sec_rl = Y.RateLimiter(8)            # SEC fair-access limit is 10 requests per second


def sec_get(url, binary=False, tries=4, stream_to=None):
    if not SEC_UA:
        raise RuntimeError("SEC_CONTACT is not set (SEC requires a contact e-mail in the User-Agent)")
    for a in range(tries):
        _sec_rl.wait()
        try:
            r = requests.get(url, headers={"User-Agent": SEC_UA, "Accept-Encoding": "gzip, deflate"},
                             timeout=120 if stream_to is None else 900, stream=stream_to is not None)
        except requests.RequestException:
            time.sleep(3 * (a + 1))
            continue
        if r.status_code == 200:
            if stream_to is not None:
                with open(stream_to, "wb") as f:
                    for chunk in r.iter_content(1 << 20):
                        f.write(chunk)
                return stream_to
            return r.content if binary else r.text
        if r.status_code == 404:
            return None
        if r.status_code in (403, 429) or r.status_code >= 500:
            time.sleep(5 * (a + 1))
            continue
        return None
    return None


def ticker_map():
    """SEC's ticker -> CIK list (includes the exchange)."""
    f = CACHE / "company_tickers_exchange.json"
    txt = sec_get("https://www.sec.gov/files/company_tickers_exchange.json")
    if txt:
        f.write_text(txt)
    elif f.exists():
        txt = f.read_text()
    else:
        return {}
    j = json.loads(txt)
    i_cik, i_t = j["fields"].index("cik"), j["fields"].index("ticker")
    out = {}
    for row in j["data"]:
        t = str(row[i_t]).upper().replace("-", ".")
        out.setdefault(t, int(row[i_cik]))
    return out


# ────────────────────────────────────────────────────────── XBRL -> quarters
REV = ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "RevenueFromContractWithCustomerIncludingAssessedTax",
       "SalesRevenueNet", "SalesRevenueGoodsNet", "SalesRevenueServicesNet", "RevenuesNetOfInterestExpense",
       "RealEstateRevenueNet", "OperatingLeasesIncomeStatementLeaseRevenue", "InterestAndDividendIncomeOperating"]
DUR = {
    "rev": REV,
    "opi": ["OperatingIncomeLoss"],
    "pbt": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
    "tax": ["IncomeTaxExpenseBenefit"],
    "pat": ["NetIncomeLoss", "NetIncomeLossAvailableToCommonStockholdersBasic", "ProfitLoss"],
    "pat_all": ["ProfitLoss", "NetIncomeLoss"],
    "fin": ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestExpenseNet"],
    "dep": ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization", "DepreciationAmortizationAndAccretionNet", "Depreciation"],
    "oi": ["NonoperatingIncomeExpense", "OtherNonoperatingIncomeExpense"],
    "prov": ["ProvisionForLoanLeaseAndOtherLosses", "ProvisionForLoanAndLeaseLosses", "ProvisionForCreditLosses"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex": ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets", "PaymentsForCapitalImprovements"],
    "shares": ["WeightedAverageNumberOfDilutedSharesOutstanding", "WeightedAverageNumberOfShareOutstandingBasicAndDiluted",
               "WeightedAverageNumberOfSharesOutstandingBasic"],
}
LAST_RESORT = {"InterestAndDividendIncomeOperating"}     # a lender's gross interest income: only if nothing else
EPS = ["EarningsPerShareDiluted", "EarningsPerShareBasicAndDiluted", "EarningsPerShareBasic"]
INST = {
    "equity": ["StockholdersEquity", "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest"],
    "assets": ["Assets"],
    "cash_eq": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
    "cash_sti": ["CashCashEquivalentsAndShortTermInvestments"],
    "sti": ["ShortTermInvestments", "MarketableSecuritiesCurrent", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "ltd_total": ["LongTermDebt", "LongTermDebtAndCapitalLeaseObligations", "DebtLongtermAndShorttermCombinedAmount"],
    "ltd_nc": ["LongTermDebtNoncurrent", "LongTermDebtAndCapitalLeaseObligationsNoncurrent", "LongTermNotesPayable"],
    "ltd_c": ["LongTermDebtCurrent", "DebtCurrent", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    "std": ["ShortTermBorrowings", "CommercialPaper"],
    "inv": ["InventoryNet"],
    "recv": ["AccountsReceivableNetCurrent", "ReceivablesNetCurrent"],
    "cwip": ["ConstructionInProgressGross"],
}
QUARTER_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "10-KT", "10-KT/A"}


def _days(a, b):
    return (date.fromisoformat(b) - date.fromisoformat(a)).days


def _facts(gaap, concept, unit="USD"):
    x = gaap.get(concept)
    if not x:
        return []
    return x.get("units", {}).get(unit, [])


def durations(gaap, concepts, unit="USD", biggest=False):
    """-> {(start, end): (val, filed, form)} using, for every period, the most recent filing (restatements win).
    One concept is primary (the one reported most recently); others only fill older periods."""
    best = None
    per_concept = []
    for k, cpt in enumerate(concepts):
        d = {}
        for f in _facts(gaap, cpt, unit):
            if "start" not in f or f.get("form") not in QUARTER_FORMS:
                continue
            key = (f["start"], f["end"])
            cur = d.get(key)
            if cur is None:
                d[key] = [f["val"], f["filed"], f["form"], f["filed"]]
            else:
                cur[3] = min(cur[3], f["filed"])          # first time this period was reported
                if f["filed"] > cur[1]:                    # restated later: the newest value wins
                    cur[0], cur[1], cur[2] = f["val"], f["filed"], f["form"]
        if d:
            latest = max(e for (_, e) in d)
            recent = sorted(d.items(), key=lambda kv: kv[0][1])[-4:]
            if all(v[0] == 0 for _, v in recent) and len(concepts) > 1:
                continue                                   # a tag reported as 0 (e.g. an unused total): skip it
            size = max(abs(v[0]) for (_, e), v in d.items() if e == latest) if biggest and cpt not in LAST_RESORT else 0
            per_concept.append((latest, size, -k, d))
    if not per_concept:
        return {}
    per_concept.sort(key=lambda x: x[:3], reverse=True)
    out = dict(per_concept[0][3])
    first_end = min(e for (_, e) in out)
    for _, _, _, d in per_concept[1:]:
        for key, v in d.items():
            if key[1] < first_end and key not in out:
                out[key] = v
    return out


QMIN, QMAX = 80, 115          # a fiscal quarter: 12-16 weeks (Costco's Q4 is 16 weeks)


def quarterize(dur, fy_ends, diff=True):
    """{(start,end): [val, filed, form, first_filed]} -> {end: (val, first_filed)} for fiscal quarters.
    Direct quarter values win; otherwise (diff=True) quarter = year-to-date minus the previous
    year-to-date, or for Q4 the full year minus the three quarters inside it."""
    q = {}
    for (s, e), (v, fl, fm, first) in dur.items():
        if QMIN <= _days(s, e) <= QMAX:
            if e not in q or _days(s, e) < 100:
                q[e] = (v, first)
    if not diff:
        return q
    # A 10-K that tags the full-year figure with the fourth quarter's dates (it happens): when a "quarter"
    # is larger than the nine-month total reported just before it, it is the year - take the quarter out.
    for (s, e), (v, fl, fm, first) in dur.items():
        n = _days(s, e)
        if 250 <= n <= 290 and v > 0:
            for e4, (qv, qf) in list(q.items()):
                if QMIN <= _days(e, e4) <= QMAX and qv > v:
                    q[e4] = (qv - v, qf)
    by_start = {}
    for (s, e), val in dur.items():
        by_start.setdefault(s, []).append((e, val))
    for s, lst in by_start.items():
        lst.sort()
        for i, (e, (v, fl, fm, first)) in enumerate(lst):
            n = _days(s, e)
            if n < 150 or e in q or n > 380:
                continue
            prev = [(e2, v2) for e2, v2 in lst[:i] if QMIN <= _days(e2, e) <= QMAX]
            if prev:
                e2, v2 = prev[-1]
                q[e] = (v - v2[0], first)
            elif e in fy_ends:
                ins = sorted(k for k in q if s < k < e)
                if len(ins) == 3 and _days(s, ins[0]) <= QMAX:
                    q[e] = (v - sum(q[k][0] for k in ins), first)
    return q


def annual(dur, fy_ends):
    return {e: v for (s, e), (v, *_r) in dur.items() if 340 <= _days(s, e) <= 380 and e in fy_ends}


def fiscal_year_ends(gaap):
    """Ends of the 12-month periods reported in 10-K filings (10-Q 'trailing twelve months' columns excluded)."""
    out = set()
    for cpt in REV + DUR["pat"]:
        for f in _facts(gaap, cpt):
            if "start" in f and str(f.get("form", "")).startswith("10-K") and 340 <= _days(f["start"], f["end"]) <= 380:
                out.add(f["end"])
    return out


def instants(gaap, concepts, unit="USD"):
    for cpt in concepts:
        d = {}
        for f in _facts(gaap, cpt, unit):
            if "start" in f or f.get("form") not in QUARTER_FORMS:
                continue
            if f["end"] not in d or f["filed"] > d[f["end"]][1]:
                d[f["end"]] = (f["val"], f["filed"])
        if d:
            yield cpt, {e: v for e, (v, _) in d.items()}


def first_instant(gaap, concepts):
    for _, d in instants(gaap, concepts):
        return d
    return {}


def norm_qe(e):
    """Fiscal periods that end in the first week of a month belong to the month before (52/53-week years)."""
    d = date.fromisoformat(e)
    if d.day <= 7:
        d = d.replace(day=1) - timedelta(days=1)
    return d.isoformat()


def parse_company(j, sym, min_year):
    gaap = (j.get("facts") or {}).get("us-gaap") or {}
    if not gaap:
        return []
    fy_ends = fiscal_year_ends(gaap)
    Q = {}
    first_filed = {}
    for col, cps in DUR.items():
        unit = "shares" if col == "shares" else "USD"
        d = durations(gaap, cps, unit, biggest=(col == "rev"))
        if col in ("ocf", "capex"):
            Q[col] = annual(d, fy_ends)
            continue
        if col == "shares":
            # weighted-average share counts can't be differenced: direct quarters, else the year's figure
            qq = quarterize(d, fy_ends, diff=False)
            for e, v in annual(d, fy_ends).items():
                qq.setdefault(e, (v, None))
            Q[col] = {e: v[0] for e, v in qq.items()}
            continue
        qq = quarterize(d, fy_ends)
        Q[col] = {e: v[0] for e, v in qq.items()}
        if col in ("rev", "pat"):
            for e, v in qq.items():
                if v[1]:
                    first_filed[e] = min(first_filed.get(e, "9999"), v[1])
        if col == "pat":
            Q["_ytd_pat"] = annual(d, fy_ends)
        if col == "rev":
            Q["_ytd_rev"] = annual(d, fy_ends)
    eps_d = durations(gaap, EPS, "USD/shares")
    Q["eps"] = {e: v for (s, e), (v, *_r) in eps_d.items() if QMIN <= _days(s, e) <= QMAX}
    B = {k: first_instant(gaap, v) for k, v in INST.items()}
    ends = sorted(set(Q["rev"]) | set(Q["pat"]))
    ends = [e for e in ends if e[:4] >= str(min_year)]
    rows = []
    for e in ends:
        def g(col):
            return Q.get(col, {}).get(e)
        def b(col):
            return B.get(col, {}).get(e)
        rev, pat = g("rev"), g("pat")
        if rev is None and pat is None:
            continue
        cash = b("cash_sti")
        if cash is None:
            cash = (b("cash_eq") or 0) + (b("sti") or 0) if (b("cash_eq") is not None or b("sti") is not None) else None
        if b("ltd_total") is not None:
            debt = b("ltd_total") + (b("std") or 0)
        elif any(b(k) is not None for k in ("ltd_nc", "ltd_c", "std")):
            debt = (b("ltd_nc") or 0) + (b("ltd_c") or 0) + (b("std") or 0)
        else:
            debt = 0.0 if b("equity") is not None else None
        fye = e in fy_ends
        eps = g("eps")
        if eps is None and pat is not None and g("shares"):
            eps = round(pat / g("shares"), 2)            # Q4: not reported for the quarter alone
        M = lambda v: None if v is None else round(v / 1e6, 3)
        rows.append({
            "symbol": sym, "qe": norm_qe(e), "pe": e, "basis": "C", "aud": "A" if fye else "U",
            "filed": first_filed.get(e) if first_filed.get(e, "9999") != "9999" else None, "bank": 0,
            "rev": M(rev), "oi": M(g("oi")), "opi": M(g("opi")), "fin": M(g("fin")), "dep": M(g("dep")), "exc": 0.0,
            "pbt": M(g("pbt")), "tax": M(g("tax")), "pat": M(pat), "pat_own": M(pat), "pat_all": M(g("pat_all")),
            "eps": eps, "prov": M(g("prov")), "shares": M(g("shares")),
            "equity": M(b("equity")), "borrow": M(debt), "cash": M(cash), "cwip": M(b("cwip")), "assets": M(b("assets")),
            "inv": M(b("inv")), "recv": M(b("recv")),
            "ocf": M(Q["ocf"].get(e)) if fye else None, "capex": M(Q["capex"].get(e)) if fye else None,
            "ytd_rev": M(Q["_ytd_rev"].get(e)) if fye else None, "ytd_pat": M(Q["_ytd_pat"].get(e)) if fye else None,
            "fye": 1 if fye else 0,
        })
    # drop a quarter that duplicates its neighbour after normalising the date
    seen, out = set(), []
    for r in sorted(rows, key=lambda r: r["pe"], reverse=True):
        if r["qe"] in seen:
            continue
        seen.add(r["qe"])
        out.append(r)
    return sorted(out, key=lambda r: r["qe"])


RESULT_COLS = ["symbol", "qe", "pe", "basis", "aud", "filed", "bank", "rev", "oi", "opi", "fin", "dep", "exc", "pbt", "tax",
               "pat", "pat_own", "pat_all", "eps", "prov", "shares", "equity", "borrow", "cash", "cwip", "assets", "inv", "recv",
               "ocf", "capex", "ytd_rev", "ytd_pat", "fye"]


def update_results(stocks, cikmap, full=False):
    """Re-parse companies whose SEC file changed since the last run."""
    zpath = CACHE / "companyfacts.zip"
    state = json.loads(STATE_F.read_text()) if STATE_F.exists() else {}
    done = state.get("facts", {})                  # cik -> member timestamp already parsed
    t0 = time.time()
    fresh = zpath.exists() and (time.time() - zpath.stat().st_mtime) < 20 * 3600
    if not fresh:
        print("Downloading SEC companyfacts.zip ...")
        if not sec_get("https://www.sec.gov/Archives/edgar/daily-index/xbrl/companyfacts.zip", stream_to=zpath):
            print("WARN companyfacts.zip not downloaded - results not refreshed")
            return 0
        print(f"  {zpath.stat().st_size / 1e9:.2f} GB in {time.time() - t0:.0f}s")
    z = zipfile.ZipFile(zpath)
    members = {int(i.filename[3:13]): i for i in z.infolist() if i.filename.startswith("CIK")}
    old = pd.read_csv(CFG / "fund_results.csv", dtype={"symbol": str}) if (CFG / "fund_results.csv").exists() and not full else pd.DataFrame(columns=RESULT_COLS)
    have = set(old.symbol) if len(old) else set()
    todo = []
    for s in stocks:
        cik = cikmap.get(s)
        if not cik or cik not in members:
            continue
        stamp = "%04d%02d%02d%02d%02d" % members[cik].date_time[:5]
        if full or s not in have or done.get(str(cik)) != stamp:
            todo.append((s, cik, stamp))
    min_year = TODAY.year - 4
    new_rows, parsed = [], 0
    for s, cik, stamp in todo:
        try:
            j = json.loads(z.read(members[cik]))
            rows = parse_company(j, s, min_year)
        except Exception as e:
            print(f"  {s}: {type(e).__name__} {e}")
            continue
        new_rows += rows
        done[str(cik)] = stamp
        parsed += 1
        if parsed % 500 == 0:
            print(f"  parsed {parsed}/{len(todo)}  {time.time() - t0:.0f}s", flush=True)
    upd = {r[0] for r in todo}
    keep = old[~old.symbol.isin(upd) & old.symbol.isin(set(stocks) | have)] if len(old) else old
    res = pd.concat([keep, pd.DataFrame(new_rows, columns=RESULT_COLS)], ignore_index=True)
    res = res[res.symbol.isin(set(stocks))].sort_values(["symbol", "qe"])
    res = res.groupby("symbol").tail(14)
    res.to_csv(CFG / "fund_results.csv", index=False)
    state["facts"] = done
    STATE_F.write_text(json.dumps(state, indent=0))
    print(f"fund_results.csv: {res.symbol.nunique()} stocks, {len(res)} quarters ({parsed} companies re-parsed, {time.time() - t0:.0f}s)")
    return parsed


# ────────────────────────────────────────────────────────── insider transactions (Form 4)
INS_COLS = ["acc", "filed", "tdate", "cik", "symbol", "owner", "rel", "title", "code", "shares", "price", "value"]


def _rel(director, officer, ten):
    return ("D" if director else "") + ("O" if officer else "") + ("T" if ten else "")


def insider_datasets(ciks, since):
    """Quarterly SEC insider-transaction data sets (one zip per quarter, ~10 MB)."""
    out = []
    q = (since.year, (since.month - 1) // 3 + 1)
    last = (TODAY.year, (TODAY.month - 1) // 3 + 1)
    got = []
    while q < last:
        y, n = q
        name = f"{y}q{n}_form345.zip"
        z = None
        for base in ("https://www.sec.gov/files/datastandardsinnovation/data/insider-transactions-data-sets/",
                     "https://www.sec.gov/files/structureddata/data/insider-transactions-data-sets/"):
            b = sec_get(base + name, binary=True)
            if b and b[:2] == b"PK":
                z = zipfile.ZipFile(io.BytesIO(b))
                break
        q = (y + (n == 4), 1 if n == 4 else n + 1)
        if z is None:
            continue
        rd = lambda f: pd.read_csv(z.open(f), sep="\t", dtype=str, quoting=csv.QUOTE_NONE, on_bad_lines="skip")
        sub = rd("SUBMISSION.tsv")
        sub = sub[sub.DOCUMENT_TYPE.isin(["4", "4/A"])]
        sub["cik"] = pd.to_numeric(sub.ISSUERCIK, errors="coerce")
        sub = sub[sub.cik.isin(ciks)]
        own = rd("REPORTINGOWNER.tsv")
        own = own[own.ACCESSION_NUMBER.isin(set(sub.ACCESSION_NUMBER))]
        own = own.groupby("ACCESSION_NUMBER").agg({"RPTOWNERNAME": "first", "RPTOWNER_RELATIONSHIP": lambda x: ",".join(x.dropna()),
                                                   "RPTOWNER_TITLE": "first"})
        tr = rd("NONDERIV_TRANS.tsv")
        tr = tr[tr.ACCESSION_NUMBER.isin(set(sub.ACCESSION_NUMBER)) & tr.TRANS_CODE.isin(["P", "S"])]
        tr = tr.merge(sub[["ACCESSION_NUMBER", "FILING_DATE", "cik", "ISSUERTRADINGSYMBOL"]], on="ACCESSION_NUMBER")
        tr = tr.join(own, on="ACCESSION_NUMBER")
        for r in tr.itertuples():
            rel = str(r.RPTOWNER_RELATIONSHIP or "")
            sh = pd.to_numeric(r.TRANS_SHARES, errors="coerce")
            px = pd.to_numeric(r.TRANS_PRICEPERSHARE, errors="coerce")
            out.append([r.ACCESSION_NUMBER, _d(r.FILING_DATE), _d(r.TRANS_DATE), int(r.cik), str(r.ISSUERTRADINGSYMBOL or "").upper(),
                        str(r.RPTOWNERNAME or "")[:40], _rel("Director" in rel, "Officer" in rel, "TenPercent" in rel),
                        str(r.RPTOWNER_TITLE or "")[:40] if str(r.RPTOWNER_TITLE) != "nan" else "", r.TRANS_CODE,
                        sh, px, sh * px if np.isfinite(sh) and np.isfinite(px) else np.nan])
        got.append(name)
    print(f"insider data sets: {', '.join(got) or 'none'} -> {len(out)} transactions")
    return pd.DataFrame(out, columns=INS_COLS), got


def _d(s):
    try:
        return datetime.strptime(str(s), "%d-%b-%Y").date().isoformat()
    except ValueError:
        return str(s)[:10]


def form4_daily(ciks, start, end, cap_seconds=900):
    """Every Form 4 for our companies filed on EDGAR between start and end (daily index)."""
    rows, t0, days_done = [], time.time(), []
    d = start
    while d <= end:
        if d.weekday() >= 5:
            d += timedelta(days=1)
            continue
        if time.time() - t0 > cap_seconds:
            print(f"  Form 4: time budget used, continuing from {d} next run")
            break
        qtr = (d.month - 1) // 3 + 1
        idx = sec_get(f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{qtr}/form.{d:%Y%m%d}.idx")
        if idx is None:
            d += timedelta(days=1)
            if d < end:
                days_done.append(d - timedelta(days=1))   # holiday: no index
            continue
        paths = []
        for line in idx.splitlines():
            if line[:4] not in ("4   ", "4/A "):
                continue
            m = re.search(r"\s(\d{3,10})\s+(\d{8})\s+(edgar/\S+\.txt)\s*$", line)
            if m and int(m.group(1)) in ciks:
                paths.append(m.group(3))
        def job(p):
            t = sec_get("https://www.sec.gov/Archives/" + p)
            return parse_form4(t, p) if t else []
        with ThreadPoolExecutor(6) as ex:
            for f in as_completed([ex.submit(job, p) for p in paths]):
                try:
                    rows += f.result()
                except Exception:
                    pass
        days_done.append(d)
        d += timedelta(days=1)
    print(f"Form 4 daily: {len(days_done)} days, {len(rows)} transactions, {time.time() - t0:.0f}s")
    return pd.DataFrame(rows, columns=INS_COLS), (max(days_done) if days_done else None)


def _x(node, path):
    e = node.find(path)
    return (e.text or "").strip() if e is not None and e.text else ""


def parse_form4(text, path):
    m = re.search(r"<ownershipDocument>.*?</ownershipDocument>", text, re.S)
    if not m:
        return []
    doc = ElementTree.fromstring(m.group(0))
    acc = path.rsplit("/", 1)[-1].replace(".txt", "")
    filed = re.search(r"FILED AS OF DATE:\s+(\d{8})", text)
    filed = f"{filed.group(1)[:4]}-{filed.group(1)[4:6]}-{filed.group(1)[6:]}" if filed else ""
    cik = int(_x(doc, "issuer/issuerCik") or 0)
    sym = _x(doc, "issuer/issuerTradingSymbol").upper()
    ro = doc.find("reportingOwner")
    name = _x(ro, "reportingOwnerId/rptOwnerName") if ro is not None else ""
    rel = ro.find("reportingOwnerRelationship") if ro is not None else None
    flag = lambda k: rel is not None and _x(rel, k) in ("1", "true", "True")
    title = _x(rel, "officerTitle") if rel is not None else ""
    out = []
    for t in doc.findall("nonDerivativeTable/nonDerivativeTransaction"):
        code = _x(t, "transactionCoding/transactionCode")
        if code not in ("P", "S"):
            continue
        sh = pd.to_numeric(_x(t, "transactionAmounts/transactionShares/value"), errors="coerce")
        px = pd.to_numeric(_x(t, "transactionAmounts/transactionPricePerShare/value"), errors="coerce")
        out.append([acc, filed, _x(t, "transactionDate/value")[:10], cik, sym, name[:40],
                    _rel(flag("isDirector"), flag("isOfficer"), flag("isTenPercentOwner")), title[:40], code,
                    sh, px, sh * px if np.isfinite(sh) and np.isfinite(px) else np.nan])
    return out


def update_insiders(cik_of):
    f = CFG / "insider_tx.csv"
    state = json.loads(STATE_F.read_text()) if STATE_F.exists() else {}
    ciks = set(cik_of.values())
    sym_of = {v: k for k, v in cik_of.items()}
    old = pd.read_csv(f, dtype={"symbol": str, "acc": str}) if f.exists() else pd.DataFrame(columns=INS_COLS)
    keep_from = (TODAY - timedelta(days=400)).isoformat()
    frames = [old]
    if not len(old) or not state.get("form4_done"):
        ds, got = insider_datasets(ciks, TODAY - timedelta(days=370))
        frames.append(ds)
        # daily Form 4s from the start of the current quarter
        start = date(TODAY.year, 3 * ((TODAY.month - 1) // 3) + 1, 1)
    else:
        start = date.fromisoformat(state["form4_done"]) + timedelta(days=1)
    end = TODAY - timedelta(days=1)      # today's index is published late in the evening
    if start <= end:
        dl, last = form4_daily(ciks, start, end)
        frames.append(dl)
        if last:
            state = json.loads(STATE_F.read_text()) if STATE_F.exists() else {}
            state["form4_done"] = last.isoformat()
            STATE_F.write_text(json.dumps(state, indent=0))
    tx = pd.concat([x for x in frames if len(x)], ignore_index=True) if any(len(x) for x in frames) else pd.DataFrame(columns=INS_COLS)
    if not len(tx):
        return
    tx["cik"] = pd.to_numeric(tx["cik"], errors="coerce")
    tx["symbol"] = tx["cik"].map(sym_of).fillna(tx["symbol"])     # today's ticker for the company
    tx = tx[tx.tdate.astype(str) >= keep_from].drop_duplicates(["acc", "tdate", "owner", "code", "shares", "price"])
    tx.sort_values(["symbol", "tdate"]).to_csv(f, index=False)
    print(f"insider_tx.csv: {len(tx)} open-market transactions, {tx.symbol.nunique()} companies")


# ────────────────────────────────────────────────────────── Yahoo: profiles, holders, ETFs, earnings dates
BANK_IND = re.compile(r"^Banks|Savings|Mortgage Finance", re.I)


def update_profiles(stocks, etfs, cikmap, max_new=4000, max_refresh=500):
    """Sector/industry and ownership snapshot for stocks, category/holdings for ETFs.
    Missing ones are fetched first; the rest are refreshed when older than 30 days."""
    fi_f, fh_f, ei_f = CFG / "fund_info.csv", CFG / "fund_holdings.csv", CFG / "etf_info.csv"
    info = pd.read_csv(fi_f, dtype={"symbol": str}).set_index("symbol") if fi_f.exists() else pd.DataFrame()
    hold = pd.read_csv(fh_f, dtype={"symbol": str}) if fh_f.exists() else pd.DataFrame(columns=["symbol", "date", "insiders", "inst", "inst_float", "inst_count"])
    einfo = pd.read_csv(ei_f, dtype={"symbol": str}).set_index("symbol") if ei_f.exists() else pd.DataFrame()
    old_date = lambda df, s: str(df.at[s, "ind_date"]) if len(df) and s in df.index and "ind_date" in df else ""
    cut = (TODAY - timedelta(days=30)).isoformat()
    need_s = [s for s in stocks if old_date(info, s) == ""]
    need_s += sorted((s for s in stocks if "" < old_date(info, s) < cut), key=lambda s: old_date(info, s))[:max_refresh]
    need_e = [s for s in etfs if old_date(einfo, s) == ""]
    need_e += sorted((s for s in etfs if "" < old_date(einfo, s) < cut), key=lambda s: old_date(einfo, s))[:max_refresh // 2]
    need_s, need_e = need_s[:max_new], need_e[:max_new]
    rl = Y.RateLimiter(10)
    t0 = time.time()

    def job_s(s):
        rl.wait()
        r = Y.summary(s, ["assetProfile", "majorHoldersBreakdown", "calendarEvents"])
        if not r:
            return None
        ap, mh = r.get("assetProfile") or {}, r.get("majorHoldersBreakdown") or {}
        return {"sector": ap.get("sector"), "industry": ap.get("industry"), "country": ap.get("country"),
                "employees": ap.get("fullTimeEmployees"),
                "insiders": Y.raw(mh.get("insidersPercentHeld")), "inst": Y.raw(mh.get("institutionsPercentHeld")),
                "inst_float": Y.raw(mh.get("institutionsFloatPercentHeld")), "inst_count": Y.raw(mh.get("institutionsCount"))}

    def job_e(s):
        rl.wait()
        r = Y.summary(s, ["fundProfile", "topHoldings", "summaryDetail"])
        if not r:
            return None
        fp, th, sd = r.get("fundProfile") or {}, r.get("topHoldings") or {}, r.get("summaryDetail") or {}
        fees = fp.get("feesExpensesInvestment") or {}
        hl = [f"{h.get('symbol')}:{round((Y.raw(h.get('holdingPercent')) or 0) * 100, 2)}" for h in th.get("holdings") or [] if h.get("symbol")]
        return {"category": fp.get("categoryName"), "family": fp.get("family"),
                "aum_m": round((Y.raw(sd.get("totalAssets")) or 0) / 1e6, 1) or None,
                "expense": Y.raw(fees.get("annualReportExpenseRatio")), "holdings": " ".join(hl)}

    got_s, fail_s = Y.run_pool(need_s, job_s, 6, "profiles")
    got_e, fail_e = Y.run_pool(need_e, job_e, 6, "ETF profiles")
    print(f"Yahoo profiles: {len(got_s)} stocks, {len(got_e)} ETFs ({len(fail_s) + len(fail_e)} failed) in {time.time() - t0:.0f}s")
    # stock info
    rows = info.reset_index().to_dict("records") if len(info) else []
    byk = {r["symbol"]: r for r in rows}
    for s, v in got_s.items():
        r = byk.setdefault(s, {"symbol": s})
        r.update({k: v[k] for k in ("sector", "industry", "country", "employees")})
        r["ind_date"] = TODAY.isoformat()
    # ownership snapshot: one row per stock per month
    snaps = []
    for s, v in got_s.items():
        if v.get("inst") is not None or v.get("insiders") is not None:
            snaps.append({"symbol": s, "date": TODAY.isoformat(), **{k: v.get(k) for k in ("insiders", "inst", "inst_float", "inst_count")}})
    if snaps:
        hold = pd.concat([hold, pd.DataFrame(snaps)], ignore_index=True)
        hold["ym"] = hold["date"].astype(str).str[:7]
        hold = hold.drop_duplicates(["symbol", "ym"], keep="last").drop(columns="ym")
        hold = hold[hold.date.astype(str) >= (TODAY - timedelta(days=800)).isoformat()]
        hold.sort_values(["symbol", "date"]).to_csv(fh_f, index=False)
    # earnings dates + CIK from the quote snapshot
    q = pd.read_csv(CACHE / "quotes.csv", index_col=0) if (CACHE / "quotes.csv").exists() else pd.DataFrame()
    for s in stocks:
        r = byk.setdefault(s, {"symbol": s})
        r["cik"] = cikmap.get(s)
        if len(q) and s in q.index:
            nxt = [Y.ts_to_date(q.at[s, c]) for c in ("earningsTimestampStart", "earningsTimestamp") if c in q]
            nxt = [d for d in nxt if d]
            fut = [d for d in nxt if d >= TODAY.isoformat()]
            past = [d for d in nxt if d < TODAY.isoformat()]
            if fut:
                r["next_results"] = min(fut)
                r["next_est"] = bool(q.at[s, "isEarningsDateEstimate"]) if "isEarningsDateEstimate" in q and pd.notna(q.at[s, "isEarningsDateEstimate"]) else None
            elif str(r.get("next_results") or "") < TODAY.isoformat():
                r["next_results"] = None
            if past:
                r["last_results"] = max(past + [str(r["last_results"])] if isinstance(r.get("last_results"), str) else past)
            prev = r.get("prev_next")
            if isinstance(prev, str) and prev < TODAY.isoformat() and str(r.get("last_results") or "") < prev:
                r["last_results"] = prev                   # the date we had on file has now passed
            r["prev_next"] = r.get("next_results") if isinstance(r.get("next_results"), str) else prev
        r["bank"] = 1 if BANK_IND.search(str(r.get("industry") or "")) else 0
    info = pd.DataFrame(list(byk.values()))
    cols = ["symbol", "sector", "industry", "country", "employees", "ind_date", "next_results", "next_est", "last_results", "prev_next", "cik", "bank"]
    for c_ in cols:
        if c_ not in info:
            info[c_] = None
    info[cols].sort_values("symbol").to_csv(fi_f, index=False)
    # ETF info
    erows = einfo.reset_index().to_dict("records") if len(einfo) else []
    ebyk = {r["symbol"]: r for r in erows}
    for s, v in got_e.items():
        r = ebyk.setdefault(s, {"symbol": s})
        r.update(v)
        r["ind_date"] = TODAY.isoformat()
    if ebyk:
        e = pd.DataFrame(list(ebyk.values()))
        for c_ in ["category", "family", "aum_m", "expense", "holdings", "ind_date"]:
            if c_ not in e:
                e[c_] = None
        e[["symbol", "category", "family", "aum_m", "expense", "holdings", "ind_date"]].sort_values("symbol").to_csv(ei_f, index=False)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--skip", default="", help="comma list of steps to skip: results,insiders,profiles")
    a = ap.parse_args()
    skip = set(a.skip.split(","))
    u = pd.read_csv(CACHE / "universe.csv", dtype={"symbol": str}, keep_default_na=False)
    q = pd.read_csv(CACHE / "quotes.csv", index_col=0) if (CACHE / "quotes.csv").exists() else pd.DataFrame()
    # the same liquidity floor as the site (a little looser, so names near the line keep their data)
    px = pd.to_numeric(q.get("regularMarketPrice"), errors="coerce") if len(q) else pd.Series(dtype=float)
    adv = pd.to_numeric(q.get("averageDailyVolume3Month"), errors="coerce") * px / 1e6 if len(q) else pd.Series(dtype=float)
    mc = pd.to_numeric(q.get("marketCap"), errors="coerce") / 1e6 if len(q) else pd.Series(dtype=float)
    st = u[u.kind == "stock"].symbol
    liquid = lambda s: (adv.get(s, 0) or 0) >= 0.6 * S["min_median_turnover_m"] and (mc.get(s, 1e9) or 1e9) >= 0.6 * S["min_market_cap_m"] and (px.get(s, 0) or 0) >= 0.8 * S["min_price"]
    stocks = [s for s in st if liquid(s)] if len(q) else list(st)
    etfs = list(u[u.kind == "etf"].symbol)
    print(f"Fundamentals universe: {len(stocks)} stocks, {len(etfs)} ETFs")
    cikmap = {}
    try:
        cikmap = ticker_map()
    except Exception as e:
        print(f"WARN SEC ticker list: {e}")
    cik_of = {s: cikmap[s] for s in stocks if s in cikmap}
    print(f"SEC CIK found for {len(cik_of)} of {len(stocks)} stocks")
    if "profiles" not in skip:
        try:
            update_profiles(stocks, etfs, cikmap)
        except Exception as e:
            print(f"WARN profiles: {type(e).__name__} {e}")
    if "results" not in skip and SEC_UA:
        try:
            update_results(stocks, cikmap, a.full)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"WARN results: {e}")
    if "insiders" not in skip and SEC_UA:
        try:
            update_insiders(cik_of)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"WARN insiders: {e}")
    if not SEC_UA:
        print("WARN SEC_CONTACT not set - SEC results and insider data skipped (see README)")


if __name__ == "__main__":
    main()
