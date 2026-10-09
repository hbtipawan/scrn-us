"""
data_sources.py  (US edition)
─────────────────────────────
Weekly OHLCV data layer for the VPCI screener on US stocks.

Sources, in order:
  1. CACHE  — the daily candles the dashboard already downloaded this evening
              (cache/prices.csv.gz, Yahoo split-adjusted). Instant, no network.
              Used automatically when the file exists (the GitHub Action run).
  2. YAHOO  — yfinance (Streamlit app / your PC).

Returns the SAME weekly DataFrame shape that vpci_engine expects — no
screening logic is touched here, this is purely data plumbing. The helpers the
Indian edition exposes for Upstox / Kite are kept as harmless stubs so app.py
imports keep working.
"""

from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

# Same bar minimum as vpci_engine.MIN_BARS — kept in sync, not imported, so
# this module also works standalone in scripts/tests.
MIN_BARS = 42

# ═══════════════════════════════════════════════════════════════════════
# LIVE / INTRAWEEK SUPPORT  (US regular session 09:30 → 16:00 New York)
# ═══════════════════════════════════════════════════════════════════════
ET           = ZoneInfo("America/New_York")
SESSION_MINS = 390
MIN_PROJ_SESSIONS = 0.50
MAX_PROJ_MULT     = 5.00
_OPEN        = (9, 30)
_CLOSE       = (16, 0)


def et_now() -> datetime:
    return datetime.now(ET)


ist_now = et_now          # name used by app.py (the Indian edition shows IST)


def session_fraction(now: datetime | None = None) -> float:
    """How much of TODAY'S US session has elapsed, 0.0 → 1.0."""
    now = now or et_now()
    o = now.replace(hour=_OPEN[0],  minute=_OPEN[1],  second=0, microsecond=0)
    c = now.replace(hour=_CLOSE[0], minute=_CLOSE[1], second=0, microsecond=0)
    if now <= o:
        return 0.0
    if now >= c:
        return 1.0
    return (now - o).total_seconds() / (SESSION_MINS * 60.0)


def market_is_open(now: datetime | None = None) -> bool:
    now = now or et_now()
    if now.weekday() >= 5:
        return False
    return 0.0 < session_fraction(now) < 1.0


def week_elapsed_sessions(daily: pd.DataFrame) -> float:
    """Sessions elapsed in the CURRENT (last) W-FRI bucket of a daily frame.
    Completed days count 1.0; today counts session_fraction()."""
    if daily is None or daily.empty:
        return 5.0
    idx = pd.DatetimeIndex(daily.index)
    week_end = idx[-1] + pd.offsets.Week(weekday=4) if idx[-1].weekday() != 4 else idx[-1]
    wk_start = week_end - pd.Timedelta(days=4)
    in_week = idx[idx >= wk_start.normalize()]
    if len(in_week) == 0:
        return 5.0
    n = float(len(in_week))
    today = pd.Timestamp(et_now().date())
    if in_week[-1].normalize() == today and market_is_open():
        n = n - 1.0 + max(session_fraction(), 0.02)
    elif today > week_end.normalize():
        n = 5.0          # the week is over (a 4-day holiday week counts as complete)
    return max(n, 0.02)


class _RateLimiter:
    def __init__(self, min_interval: float):
        self.min_interval = min_interval
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            sleep_for = self._last + self.min_interval - now
            if sleep_for > 0:
                time.sleep(sleep_for)
            self._last = time.monotonic()


_DIAG: list = []
_DIAG_LOCK = threading.Lock()


def _diag(symbol: str, source: str, reason: str) -> None:
    with _DIAG_LOCK:
        _DIAG.append({"symbol": symbol, "source": source, "reason": reason})


def get_diag() -> pd.DataFrame:
    with _DIAG_LOCK:
        return pd.DataFrame(list(_DIAG))


def clear_diag() -> None:
    with _DIAG_LOCK:
        _DIAG.clear()


_YAHOO_LIMITER = _RateLimiter(0.15)


# ═══════════════════════════════════════════════════════════════════════
# SHARED WEEKLY-DF CLEANUP — mirrors vpci_engine.fetch_weekly_data's
# post-processing exactly (drop incomplete week, positive rows, MIN_BARS).
# ═══════════════════════════════════════════════════════════════════════
def _clean_weekly(df: pd.DataFrame | None, min_bars: int = MIN_BARS,
                  keep_partial: bool = False) -> pd.DataFrame | None:
    if df is None or df.empty or len(df) < min_bars:
        return None
    df = df.copy()
    df.columns = [str(c).strip().capitalize() for c in df.columns]
    for col in ["Close", "High", "Low", "Volume", "Open"]:
        if col not in df.columns:
            return None
    if not keep_partial and len(df) > 2:
        last_vol = df["Volume"].iloc[-1]
        prev_vol = df["Volume"].iloc[-2]
        if prev_vol > 0 and last_vol < (prev_vol * 0.25):
            df = df.iloc[:-1]
    df = df[(df["Close"] > 0) & (df["Volume"] > 0)].dropna(subset=["Close", "Volume"])
    return df if len(df) >= min_bars else None


def _finalise_live(weekly: pd.DataFrame, daily: pd.DataFrame,
                   project_volume: bool) -> pd.DataFrame:
    weekly = weekly.copy()
    weekly["Volume"] = weekly["Volume"].astype("float64")
    elapsed = week_elapsed_sessions(daily)
    raw_vol = float(weekly["Volume"].iloc[-1])
    mult = 1.0
    do_proj = project_volume and MIN_PROJ_SESSIONS <= elapsed < 5
    if do_proj:
        mult = min(5.0 / elapsed, MAX_PROJ_MULT)
        weekly.iloc[-1, weekly.columns.get_loc("Volume")] = raw_vol * mult
    weekly.attrs["partial_week"]       = elapsed < 5
    weekly.attrs["elapsed_sessions"]   = round(elapsed, 2)
    weekly.attrs["raw_partial_volume"] = raw_vol
    weekly.attrs["volume_projected"]   = bool(do_proj)
    weekly.attrs["projection_mult"]    = round(mult, 2)
    weekly.attrs["week_confidence"]    = round(min(elapsed / 5.0, 1.0), 2)
    weekly.attrs["asof"]               = et_now().strftime("%Y-%m-%d %H:%M ET")
    return weekly


def _daily_to_weekly(daily: pd.DataFrame) -> pd.DataFrame:
    """Resample daily OHLCV to weekly bars (Friday-ending, matching Yahoo)."""
    daily = daily.sort_index()
    wk = pd.DataFrame({
        "Open":   daily["Open"].resample("W-FRI").first(),
        "High":   daily["High"].resample("W-FRI").max(),
        "Low":    daily["Low"].resample("W-FRI").min(),
        "Close":  daily["Close"].resample("W-FRI").last(),
        "Volume": daily["Volume"].resample("W-FRI").sum(),
    }).dropna(subset=["Close"])
    return wk


def _drop_unfinished_week(weekly: pd.DataFrame) -> pd.DataFrame:
    """Completed-weeks mode: drop this week's bar until Friday's close."""
    if weekly is None or weekly.empty:
        return weekly
    fri = weekly.index[-1].normalize()
    now = et_now()
    today = pd.Timestamp(now.date())
    if today < fri or (today == fri and now.hour * 60 + now.minute < 16 * 60 + 5):
        return weekly.iloc[:-1]
    return weekly


# ═══════════════════════════════════════════════════════════════════════
# 1. CACHE  (the evening download: cache/prices.csv.gz)
# ═══════════════════════════════════════════════════════════════════════
_CACHE = {"df": None, "lock": threading.Lock()}


def _cache_path() -> Path | None:
    p = os.environ.get("PKC_PRICE_CACHE")
    if p and Path(p).exists():
        return Path(p)
    here = Path(__file__).resolve().parent.parent / "cache" / "prices.csv.gz"
    return here if here.exists() else None


def _cache_frames() -> dict:
    with _CACHE["lock"]:
        if _CACHE["df"] is None:
            p = _cache_path()
            if p is None:
                _CACHE["df"] = {}
            else:
                d = pd.read_csv(p, dtype={"symbol": str, "date": str})
                d["date"] = pd.to_datetime(d["date"])
                _CACHE["df"] = {s: g.set_index("date")[["open", "high", "low", "close", "volume"]]
                                .rename(columns=str.capitalize).sort_index()
                                for s, g in d.groupby("symbol", sort=False)}
        return _CACHE["df"]


def fetch_cache_weekly(symbol: str, min_bars: int = MIN_BARS, live: bool = False,
                       project_volume: bool = True) -> tuple[pd.DataFrame | None, str]:
    daily = _cache_frames().get(symbol.strip().upper())
    if daily is None or daily.empty:
        return None, ""
    daily = daily[daily.index >= daily.index[-1] - pd.Timedelta(days=760)]
    if live:
        df = _clean_weekly(_daily_to_weekly(daily), min_bars, keep_partial=True)
        return (_finalise_live(df, daily, project_volume), "US") if df is not None else (None, "")
    df = _clean_weekly(_drop_unfinished_week(_daily_to_weekly(daily)), min_bars)
    return (df, "US") if df is not None else (None, "")


# ═══════════════════════════════════════════════════════════════════════
# 2. YAHOO  (yfinance)
# ═══════════════════════════════════════════════════════════════════════
def fetch_yahoo_weekly(symbol: str, min_bars: int = MIN_BARS,
                       live: bool = False, project_volume: bool = True
                       ) -> tuple[pd.DataFrame | None, str]:
    import yfinance as yf
    sym = symbol.strip().upper().replace(".", "-")
    last = "empty"
    for attempt in range(3):
        _YAHOO_LIMITER.wait()
        try:
            daily = yf.Ticker(sym).history(period="2y", interval="1d", auto_adjust=False)
            if daily is not None and not daily.empty:
                break
        except Exception as e:
            last = f"{type(e).__name__}: {str(e)[:80]}"
        time.sleep(0.5 * (2 ** attempt))
    else:
        _diag(symbol, "Yahoo", last)
        return None, ""
    try:
        daily.index = pd.DatetimeIndex(daily.index).tz_localize(None).normalize()
    except (TypeError, ValueError):
        daily.index = pd.DatetimeIndex(daily.index).normalize()
    daily = daily[["Open", "High", "Low", "Close", "Volume"]]
    if live:
        df = _clean_weekly(_daily_to_weekly(daily), min_bars, keep_partial=True)
        return (_finalise_live(df, daily, project_volume), "US") if df is not None else (None, "")
    df = _clean_weekly(_drop_unfinished_week(_daily_to_weekly(daily)), min_bars)
    return (df, "US") if df is not None else (None, "")


SOURCE_FETCHERS = {
    "Cache": fetch_cache_weekly,
    "Yahoo": fetch_yahoo_weekly,
}


def fetch_weekly_any(symbol: str, source_order: list[str],
                     min_bars: int = MIN_BARS,
                     live: bool = False, project_volume: bool = True
                     ) -> tuple[pd.DataFrame | None, str, str]:
    """Fetch weekly OHLCV for one symbol, trying each source in order. Yahoo is
    always the final safety net. Returns (df, source_used, exchange_used)."""
    order = [s for s in source_order if s in SOURCE_FETCHERS]
    if "Cache" not in order and _cache_path() is not None:
        order.insert(0, "Cache")
    if "Yahoo" not in order:
        order.append("Yahoo")
    for src in order:
        try:
            df, exch = SOURCE_FETCHERS[src](symbol, min_bars, live=live, project_volume=project_volume)
        except Exception as e:
            _diag(symbol, src, f"EXC {type(e).__name__}: {str(e)[:100]}")
            df, exch = None, ""
        if df is not None:
            return df, src, exch
    _diag(symbol, "ALL", "not found on any source")
    return None, "", ""


# ── stubs for names the Indian edition's app.py imports ───────────────────────
def load_upstox_instruments() -> dict:
    return {"NSE": {}, "BSE": {}}


def kite_totp_login():
    return None, "Kite is not used for US stocks."


def kite_access_token():
    return None


def set_kite_manual_token(token: str) -> None:
    return None
