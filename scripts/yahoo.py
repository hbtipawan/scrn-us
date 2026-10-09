"""Yahoo Finance helpers shared by the US scripts (no account, no API key).

  chart()         daily / monthly candles from the public chart API (split-adjusted, not
                  dividend-adjusted - the same convention as a TradingView chart)
  quotes()        batch quote: price, average volume, market cap, next earnings date ...
  summary()       per-symbol profile modules: sector/industry, holders, ETF category/holdings

quotes() and summary() need Yahoo's "crumb" (a cookie-bound token every browser gets);
it is fetched once per session and refreshed automatically if Yahoo rejects it.
"""
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
ET = ZoneInfo("America/New_York")
Q1 = "https://query1.finance.yahoo.com"
Q2 = "https://query2.finance.yahoo.com"


def now_et():
    return datetime.now(ET)


def today_et():
    return now_et().date()


def ysym(s):
    """Our symbol (exchange style, BRK.B) -> Yahoo symbol (BRK-B)."""
    return s if s.startswith("^") else s.replace(".", "-").replace("/", "-")


# ───────────────────────────────────────────────────────── session + crumb
_local = threading.local()
_crumb = {"v": None, "cookies": None, "lock": threading.Lock()}


def sess():
    if not hasattr(_local, "s"):
        s = requests.Session()
        s.headers.update({"User-Agent": UA, "Accept": "application/json,text/plain,*/*"})
        if _crumb["cookies"] is not None:
            s.cookies.update(_crumb["cookies"])
        _local.s = s
    return _local.s


def crumb(force=False):
    with _crumb["lock"]:
        if _crumb["v"] and not force:
            return _crumb["v"]
        s = requests.Session()
        s.headers.update({"User-Agent": UA})
        for url in ("https://fc.yahoo.com", "https://finance.yahoo.com/"):
            try:
                s.get(url, timeout=20, allow_redirects=True)
            except requests.RequestException:
                pass
            if s.cookies:
                break
        c = None
        for host in (Q1, Q2):
            try:
                r = s.get(f"{host}/v1/test/getcrumb", timeout=20)
                if r.status_code == 200 and r.text and "<" not in r.text and len(r.text) < 40:
                    c = r.text.strip()
                    break
            except requests.RequestException:
                pass
        if not c:
            raise RuntimeError("Yahoo crumb not available")
        _crumb["v"], _crumb["cookies"] = c, s.cookies
        _local.__dict__.pop("s", None)
        return c


class RateLimiter:
    def __init__(self, per_sec):
        self.gap, self.lock, self.next = 1.0 / per_sec, threading.Lock(), 0.0

    def wait(self):
        with self.lock:
            now = time.monotonic()
            t = max(now, self.next)
            self.next = t + self.gap
        time.sleep(max(0, t - now))


def get_json(url, params=None, tries=5, need_crumb=False):
    for attempt in range(tries):
        p = dict(params or {})
        if need_crumb:
            p["crumb"] = crumb(force=attempt > 0 and attempt % 2 == 0)
        try:
            r = sess().get(url, params=p, timeout=30)
        except requests.RequestException:
            time.sleep(1.5 * (attempt + 1))
            continue
        if r.status_code in (401, 403) and need_crumb:
            crumb(force=True)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(min(30, 3 * (attempt + 1)))
            continue
        if r.status_code == 404:
            return None
        if r.status_code != 200:
            return None
        try:
            return r.json()
        except ValueError:
            return None
    raise RuntimeError(f"Yahoo kept refusing {url.split('?')[0]}")


# ───────────────────────────────────────────────────────── candles
def chart(symbol, days=None, interval="1d", start=None, rng=None):
    """symbol -> DataFrame(date, open, high, low, close, volume) or None.
    Dates are the exchange's (US Eastern) calendar dates."""
    params = {"interval": interval, "events": "split", "includePrePost": "false"}
    if rng:
        params["range"] = rng
    else:
        p1 = int(time.time()) - int(days * 86400) if days else int(pd.Timestamp(start, tz="UTC").timestamp())
        params.update({"period1": p1, "period2": int(time.time()) + 86400})
    j = get_json(f"{Q1}/v8/finance/chart/{requests.utils.quote(ysym(symbol))}", params)
    try:
        res = j["chart"]["result"][0]
    except (TypeError, KeyError, IndexError):
        return None
    if not res or "timestamp" not in res or not res.get("indicators", {}).get("quote"):
        return None
    q = res["indicators"]["quote"][0]
    tz = res.get("meta", {}).get("exchangeTimezoneName") or "America/New_York"
    d = pd.DataFrame({"date": pd.to_datetime(res["timestamp"], unit="s", utc=True).tz_convert(tz).strftime("%Y-%m-%d"),
                      "open": q.get("open"), "high": q.get("high"), "low": q.get("low"),
                      "close": q.get("close"), "volume": q.get("volume")})
    d = d.dropna(subset=["close"])
    d = d[d.close > 0]
    if not len(d):
        return None
    for c in ("open", "high", "low"):
        d[c] = d[c].fillna(d["close"])
    d["volume"] = d["volume"].fillna(0)
    return d.drop_duplicates("date", keep="last").reset_index(drop=True)


# ───────────────────────────────────────────────────────── quotes / profiles
QUOTE_FIELDS = ["symbol", "quoteType", "longName", "shortName", "regularMarketPrice", "regularMarketPreviousClose",
                "regularMarketVolume", "averageDailyVolume3Month", "averageDailyVolume10Day", "marketCap",
                "sharesOutstanding", "exchange", "fullExchangeName", "earningsTimestamp", "earningsTimestampStart",
                "earningsTimestampEnd", "isEarningsDateEstimate", "firstTradeDateMilliseconds", "regularMarketTime",
                "regularMarketOpen", "regularMarketDayHigh", "regularMarketDayLow", "marketState", "trailingPE",
                "forwardPE", "epsTrailingTwelveMonths", "epsForward", "dividendYield", "typeDisp"]


def quotes(symbols, batch=150, workers=4):
    """Batch quotes. Returns DataFrame indexed by our symbol."""
    syms = list(dict.fromkeys(symbols))
    back = {ysym(s): s for s in syms}
    chunks = [syms[i:i + batch] for i in range(0, len(syms), batch)]
    rows = []

    def job(ch):
        j = get_json(f"{Q1}/v7/finance/quote", {"symbols": ",".join(ysym(s) for s in ch)}, need_crumb=True)
        return ((j or {}).get("quoteResponse") or {}).get("result") or []

    with ThreadPoolExecutor(workers) as ex:
        for f in as_completed([ex.submit(job, c) for c in chunks]):
            try:
                for q in f.result():
                    q = {k: q.get(k) for k in QUOTE_FIELDS}
                    q["symbol"] = back.get(q["symbol"], q["symbol"])
                    rows.append(q)
            except Exception as e:
                print(f"  quote batch failed: {e}")
    if not rows:
        return pd.DataFrame(columns=QUOTE_FIELDS).set_index("symbol")
    return pd.DataFrame(rows).drop_duplicates("symbol").set_index("symbol")


def summary(symbol, modules):
    j = get_json(f"{Q2}/v10/finance/quoteSummary/{requests.utils.quote(ysym(symbol))}",
                 {"modules": ",".join(modules)}, need_crumb=True)
    try:
        return j["quoteSummary"]["result"][0]
    except (TypeError, KeyError, IndexError):
        return None


def raw(x):
    """Yahoo's {'raw': .., 'fmt': ..} -> raw value."""
    if isinstance(x, dict):
        return x.get("raw")
    return x


def run_pool(items, job, workers, label, key=lambda r: r):
    out, failed, t0 = {}, [], time.time()
    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(job, r): r for r in items}
        for n, f in enumerate(as_completed(futs), 1):
            r = futs[f]
            k = key(r)
            try:
                d = f.result()
                if d is not None and (not hasattr(d, "__len__") or len(d)):
                    out[k] = d
                else:
                    failed.append(k)
            except Exception as e:
                failed.append(f"{k}:{type(e).__name__}")
            if n % 1000 == 0:
                print(f"  {label} {n}/{len(items)}  {time.time() - t0:.0f}s", flush=True)
    return out, failed


def ts_to_date(ts):
    if ts is None or (isinstance(ts, float) and pd.isna(ts)):
        return None
    try:
        return datetime.fromtimestamp(int(ts), tz=timezone.utc).astimezone(ET).date().isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def session_done(d=None):
    """True once today's regular session (9:30-16:00 ET) has closed, or on a weekend."""
    n = now_et()
    if n.weekday() >= 5:
        return True
    return n.hour * 60 + n.minute >= 16 * 60 + 5


def last_complete_day():
    n = now_et()
    d = n.date()
    if not session_done():
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d
