"""Run Pawan's VPCI Streamlit screener (vpci/ folder, US edition) headlessly every evening.

This script does what clicking "🚀 Run Market Scan" in app.py does, with the app's default settings:
  universe     : every stock that passes the dashboard's filters (written to vpci/Stock_List_US.csv)
  data source  : the evening's daily candles (cache/prices.csv.gz) resampled to weekly bars,
                 Yahoo as the fallback
  relaxed mode : off           parallel workers : 12
It runs the scan three times, one per combination of the app's sidebar toggles:
  completed   : "Screen the RUNNING week" OFF (the app's default after market close)
  running     : "Screen the RUNNING week" ON,  "Pro-rate partial-week volume" ON
  running_raw : "Screen the RUNNING week" ON,  "Pro-rate partial-week volume" OFF

The functions it calls (process_symbol, status_label, rank_stocks,
rank_g4_pending, market-cap enrichment ...) are read straight out of
vpci/app.py at run time, so edits to app.py carry over automatically.

Writes docs/data/vpci/{completed,running,running_raw}.json, plus a weekly sector-leadership
snapshot in docs/data/vpci/history/ (one file per week, the app's own format).

Run:  python scripts/vpci_app_run.py            (both modes)
      python scripts/vpci_app_run.py --limit 50 (quick test)
"""
import argparse
import ast
import json
import logging
import os
import sys
import time
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "vpci"
OUT = ROOT / "docs" / "data" / "vpci"
from zoneinfo import ZoneInfo  # noqa: E402
ET = ZoneInfo("America/New_York")

warnings.filterwarnings("ignore")
for name in ("streamlit", "streamlit.runtime", "streamlit.runtime.scriptrunner_utils",
             "streamlit.runtime.caching", "streamlit.runtime.state"):
    logging.getLogger(name).setLevel(logging.ERROR)

os.chdir(APP_DIR)                     # app.py reads Stock_List.csv etc. from its own folder
sys.path.insert(0, str(APP_DIR))

import numpy as np                    # noqa: E402
import pandas as pd                   # noqa: E402
import streamlit as st                # noqa: E402

from vpci_engine import analyze_stock_v3, DEFAULT_PARAMS, MIN_BARS            # noqa: E402
from data_sources import fetch_weekly_any, get_diag  # noqa: E402
from signal_history import classify_fresh_v2, analyze_young_stock, YOUNG_MIN_BARS  # noqa: E402
from sector_history import (load_sector_map, attach_sector_columns,          # noqa: E402
                            build_sector_leadership, build_rotation_view)

# ── Pull functions and constants out of app.py without running its UI ──────────
WANTED = {"parse_mcap_to_crore", "mcap_score", "rank_stocks", "rank_g4_pending",
          "get_universe", "process_symbol", "status_label", "fetch_mcap", "format_mcap"}
CONSTS = {"UNIVERSE_FILES", "MARKET_FLAG"}
_src = (APP_DIR / "app.py").read_text(encoding="utf-8")
_tree = ast.parse(_src)
NS = {"np": np, "pd": pd, "st": st, "re": __import__("re"), "datetime": datetime,
      "analyze_stock_v3": analyze_stock_v3, "DEFAULT_PARAMS": DEFAULT_PARAMS, "MIN_BARS": MIN_BARS,
      "fetch_weekly_any": fetch_weekly_any, "classify_fresh_v2": classify_fresh_v2,
      "analyze_young_stock": analyze_young_stock, "YOUNG_MIN_BARS": YOUNG_MIN_BARS}
_found = set()
for node in ast.walk(_tree):
    if isinstance(node, ast.FunctionDef) and node.name in WANTED and node.name not in _found:
        node.col_offset = 0
        exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), NS)
        _found.add(node.name)
    elif isinstance(node, ast.Assign) and any(getattr(t, "id", None) in CONSTS for t in node.targets):
        exec(compile(ast.Module(body=[node], type_ignores=[]), "app.py", "exec"), NS)
missing = WANTED - _found
if missing:
    sys.exit(f"app.py no longer defines: {', '.join(sorted(missing))} — update scripts/vpci_app_run.py")

process_symbol, status_label = NS["process_symbol"], NS["status_label"]
rank_stocks, rank_g4_pending = NS["rank_stocks"], NS["rank_g4_pending"]
fetch_mcap, format_mcap = NS["fetch_mcap"], NS["format_mcap"]
MARKET_FLAG = NS["MARKET_FLAG"]


def write_universe():
    """vpci/Stock_List_US.csv (companyId, Name, Sector, Industry) from tonight's dashboard universe."""
    rows = json.loads((ROOT / "docs" / "data" / "stocks.json").read_text())
    st_ = pd.DataFrame([{"companyId": r["s"], "Name": r.get("n") or "", "Sector": r.get("sec") or "Unknown",
                         "Industry": r.get("ind") or "Unknown"} for r in rows if r.get("k") != "e"])
    st_.to_csv(APP_DIR / "Stock_List_US.csv", index=False)
    return {r["s"]: r.get("mc") for r in rows if r.get("k") != "e"}


def get_universe():
    syms, path = NS["get_universe"]()
    return syms, path or NS["UNIVERSE_FILES"][0]


def split(df):
    """DataFrame -> compact JSON-safe {columns, rows}."""
    if df is None or len(df) == 0:
        return {"columns": [], "rows": []}
    j = json.loads(df.to_json(orient="split", index=False, date_format="iso", force_ascii=False))
    return {"columns": j["columns"], "rows": j["data"]}


def run_scan(live, symbols, project_volume=True, workers=12):
    """Mirror of the `if run_scan:` block in app.py (checkpointing omitted)."""
    params = {**DEFAULT_PARAMS, "relaxed": False}
    results, young, failed = [], [], []
    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(process_symbol, s, params, ["Cache", "Yahoo"], live, project_volume): s
                for s in symbols}
        for n, f in enumerate(as_completed(futs), 1):
            sym = futs[f]
            try:
                kind, r = f.result()
                if kind == "full" and r:
                    results.append(r)
                elif kind == "young" and r:
                    young.append(r)
                else:
                    failed.append(sym)
            except Exception:
                failed.append(sym)
            if n % 400 == 0:
                print(f"  [{'running' if live else 'completed'}] {n}/{len(symbols)}  {time.time() - t0:.0f}s", flush=True)
    df = pd.DataFrame(results)
    if len(df):
        df["status"] = df.apply(status_label, axis=1)
        df = df.sort_values(["gate_count", "pct_near_52w"], ascending=[False, False])
    return df, pd.DataFrame(young), failed, round(time.time() - t0)


def bar_week(df, live):
    """Monday–Friday of the most common latest weekly bar, and whether it is unfinished."""
    if "week_ending" not in df:
        return None
    fri = pd.Timestamp(df["week_ending"].mode().iloc[0])        # W-FRI label is the Friday
    now = datetime.now(ET)
    today = pd.Timestamp(now.date())
    return {"monday": (fri - pd.Timedelta(days=4)).date().isoformat(), "friday": fri.date().isoformat(),
            "unfinished": bool(today < fri or (today == fri and now.hour * 60 + now.minute < 16 * 60 + 5))}


def enrich(df_sorted, mcap_dict, upath):
    """Market-cap + company-name enrichment, as in app.py."""
    df_sorted = df_sorted.copy()
    df_sorted["raw_mcap"] = df_sorted["symbol"].map(mcap_dict).fillna(0)
    df_sorted["Market Cap"] = df_sorted["raw_mcap"].apply(format_mcap)
    df_sorted = df_sorted.drop(columns=["raw_mcap"])
    try:
        uni = pd.read_csv(upath)
        uni.columns = [str(c).strip() for c in uni.columns]
        if {"companyId", "Name"}.issubset(uni.columns):
            name_map = dict(zip(uni["companyId"].astype(str).str.strip().str.upper(), uni["Name"]))
            df_sorted.insert(1, "Company Name", df_sorted["symbol"].str.upper().map(name_map).fillna(""))
    except Exception:
        pass
    return df_sorted


def with_sector(df, sector_map):
    """Display-only: add Sector / Industry from Stock_List.csv to each result row."""
    if sector_map is None or sector_map.empty or "symbol" not in df:
        return df
    m = sector_map.set_index("symbol")
    out = df.copy()
    key = out["symbol"].astype(str).str.strip().str.upper()
    out["Sector"] = key.map(m["sector"]).fillna("")
    out["Industry"] = key.map(m["industry"]).fillna("")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--modes", default="completed,running,running_raw")
    a = ap.parse_args()

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "history").mkdir(exist_ok=True)
    os.environ.setdefault("PKC_PRICE_CACHE", str(ROOT / "cache" / "prices.csv.gz"))
    mc_site = write_universe()
    symbols, upath = get_universe()
    if a.limit:
        symbols = symbols[:a.limit]
    print(f"Universe {len(symbols)} symbols from {upath}")

    scans = {}
    for mode in a.modes.split(","):
        # running     = "Screen the RUNNING week" ON, "Pro-rate partial-week volume" ON
        # running_raw = "Screen the RUNNING week" ON, "Pro-rate partial-week volume" OFF
        df, young, failed, secs = run_scan(mode.startswith("running"), symbols,
                                           project_volume=(mode != "running_raw"))
        scans[mode] = (df, young, failed, secs)
        print(f"{mode}: {len(df)} analysed, {len(young)} young, {len(failed)} failed, {secs}s")

    # Market cap: the dashboard's figure (Yahoo quote, rolled to today's close) instead of one
    # yfinance call per stock as in app.py - same number, no extra requests.
    mcap_dict = {s_: (v * 1e6 if v else 0) for s_, v in mc_site.items()}

    sector_map = load_sector_map(upath)
    now = datetime.now(ET)
    for mode, (df, young, failed, secs) in scans.items():
        out = {"mode": mode, "live": mode.startswith("running"), "project_volume": mode != "running_raw",
               "scan_time": now.strftime("%Y-%m-%d %H:%M ET"), "seconds": secs,
               "universe_file": Path(upath).name, "total": len(df) + len(failed), "failed": sorted(failed)}
        if len(df) == 0:
            out.update({"results": split(None), "young": split(young), "ranked": split(None),
                        "g4": split(None), "sector": {}})
        else:
            df = enrich(df, mcap_dict, upath)
            ranked = rank_stocks(df, include_relaxed=False)
            g4 = rank_g4_pending(df)
            lead = build_sector_leadership(attach_sector_columns(df, sector_map)) if not sector_map.empty else None
            out.update({
                "week_ending": str(df["week_ending"].mode().iloc[0]) if "week_ending" in df else None,
                "bar_week": bar_week(df, mode.startswith("running")),
                "source_counts": df["source"].value_counts().to_dict() if "source" in df else {},
                "exchange_counts": df["exchange"].value_counts().to_dict() if "exchange" in df else {},
                "results": split(with_sector(df, sector_map)),
                "young": split(young.sort_values(["accumulating", "vpci"], ascending=[False, False])
                               if len(young) else young),
                "ranked": split(ranked), "g4": split(g4),
                "sector": {k: split(v) for k, v in lead.items()} if lead else {},
            })
            # Weekly snapshot (completed-week scan only), app's own schema,
            # one file per week: re-runs during the week overwrite it.
            if mode == "completed" and lead is not None and lead["sector_both"]["stocks_passing"].sum() > 0:
                wk = pd.Timestamp(out["week_ending"]) if out.get("week_ending") else pd.Timestamp(now.date())
                friday = (wk + pd.offsets.Week(weekday=4)) if wk.weekday() != 4 else wk
                snap = {"scan_date": friday.date().isoformat(),
                        "scan_timestamp": now.isoformat(timespec="seconds"), "market": MARKET_FLAG,
                        **{k: lead[k].to_dict(orient="records")
                           for k in ("sector_both", "industry_both", "sector_either", "industry_either")}}
                (OUT / "history" / f"sector_{snap['scan_date']}_{MARKET_FLAG}.json").write_text(
                    json.dumps(snap, indent=1, default=str))
        (OUT / f"{mode}.json").write_text(json.dumps(out, ensure_ascii=False, separators=(",", ":"), default=str))

    # Rotation view across saved weeks (app's build_rotation_view)
    hist = []
    for f in sorted((OUT / "history").glob("sector_*.json")):
        try:
            hist.append(json.loads(f.read_text()))
        except Exception:
            pass
    rot = build_rotation_view(hist, top_n=12)
    rotation = {"weeks": len(hist), "table": split(rot.reset_index()) if not rot.empty else split(None)}
    if not rot.empty and rot.shape[1] >= 2:
        delta = (rot.iloc[:, -1] - rot.iloc[:, -2]).sort_values(ascending=False)
        rotation["rot_in"] = [[k, int(v)] for k, v in delta[delta > 0].head(8).items()]
        rotation["rot_out"] = [[k, int(v)] for k, v in delta[delta < 0].head(8).items()]
    (OUT / "rotation.json").write_text(json.dumps(rotation, separators=(",", ":"), default=str))
    diag = get_diag()
    if len(diag):
        print(f"Fetch diagnostics: {len(diag)} entries; reasons: {diag['reason'].value_counts().head(3).to_dict()}")


if __name__ == "__main__":
    main()
