"""Telegram alerts for PKC Sector Radar US.

  --evening  after the daily run: market summary + your watchlist alerts (same rules as the
             website's "Needs your attention today") + today's best scans.
  --live     during US market hours (run every 15 min by .github/workflows/telegram.yml): live
             price check of your watchlist stocks (one Yahoo batch quote) - pivot crossed, stop
             broken, target hit, near pivot, big intraday move. Each alert is sent once per stock per day.
  --test     sends a test message (and prints your chat id if TELEGRAM_CHAT_ID isn't set yet).

Secrets (GitHub repo -> Settings -> Secrets and variables -> Actions):
  TELEGRAM_BOT_TOKEN  from @BotFather
  TELEGRAM_CHAT_ID    your chat id (the --test run prints it)
  GIST_TOKEN          the same ghp_ token used for "Sync devices" on the Watchlists tab;
                      the job reads your watchlists from that private gist
Optional repository variable SITE_URL (defaults to https://<owner>.github.io/<repo>/).
Without the secrets the script does nothing and exits cleanly.
"""
import argparse
import html
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parents[1]
DATA, CFG = ROOT / "docs" / "data", ROOT / "config"
from zoneinfo import ZoneInfo  # noqa: E402
ET = ZoneInfo("America/New_York")
sys.path.insert(0, str(Path(__file__).parent))
TG_API = os.environ.get("TELEGRAM_API", "https://api.telegram.org")
GH_API = os.environ.get("GITHUB_API", "https://api.github.com")
GFILE, SFILE = "pkc-radar-us-watchlists.json", "pkc-radar-us-alert-state.json"
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GTOK = os.environ.get("GIST_TOKEN", "").strip()
MOVE_PCT = float(os.environ.get("MOVE_PCT") or 5)
UA = {"User-Agent": "Mozilla/5.0 (PKC Sector Radar US alerts)"}


def site_url():
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"].rstrip("/") + "/"
    repo = os.environ.get("GITHUB_REPOSITORY", "")
    if "/" in repo:
        owner, name = repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{name}/"
    return ""


E = html.escape
inr = lambda v: "–" if v is None else (f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}")
sgn = lambda v, d=1, u="%": "–" if v is None else f"{'+' if v > 0 else '−' if v < 0 else ''}{abs(v):.{d}f}{u}"
tv = lambda s, ex=None: f"https://www.tradingview.com/chart/?symbol={quote(f'{ex}:{s}' if ex and ex != 'INDEX' else s)}"
sym = lambda s, ex=None: f'<a href="{tv(s, ex)}">{E(s)}</a>'


# ───────────────────────────────────────────── Telegram / GitHub
def tg(method, **params):
    r = requests.post(f"{TG_API}/bot{TOKEN}/{method}", json=params, timeout=30)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method} failed: {j.get('description') or r.status_code}")
    return j["result"]


def tg_send(text):
    """Send HTML text, split under Telegram's 4096-character limit at line breaks."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 3900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        tg("sendMessage", chat_id=CHAT, text=c, parse_mode="HTML", disable_web_page_preview=True)
        time.sleep(0.4)


def gh(path, method="GET", body=None):
    r = requests.request(method, GH_API + path, json=body, timeout=30,
                         headers={"Accept": "application/vnd.github+json", "Authorization": f"Bearer {GTOK}", **UA})
    if r.status_code >= 300:
        raise RuntimeError(f"GitHub {path}: HTTP {r.status_code}")
    return r.json()


def gist_load():
    """-> (gist_id, watchlists dict, alert-state dict). Watchlists come from the website's Sync."""
    if not GTOK:
        return None, None, {}
    g = next((x for x in gh("/gists?per_page=100") if GFILE in (x.get("files") or {})), None)
    if not g:
        return None, None, {}
    full = gh(f"/gists/{g['id']}")
    files = full.get("files", {})
    wl = json.loads(files[GFILE]["content"])
    state = json.loads(files[SFILE]["content"]) if SFILE in files and files[SFILE].get("content") else {}
    return g["id"], wl, state


def state_save(gid, state):
    gh(f"/gists/{gid}", "PATCH", {"files": {SFILE: {"content": json.dumps(state)}}})


def wl_symbols(wl):
    seen, out = set(), []
    for L in wl.get("lists", []):
        for it in L.get("items", []):
            if it["s"] not in seen:
                seen.add(it["s"])
                out.append(it["s"])
    return out


def lists_of(wl, s):
    return [L["name"] for L in wl.get("lists", []) if any(i["s"] == s for i in L.get("items", []))]


# ───────────────────────────────────────────── evening digest
BULL = ["combo3", "up4", "high52_dcr", "ath", "vcp", "pocket_pivot", "tight3w", "power3", "bgu", "episodic_pivot", "darvas",
        "stage2_bo", "bull_snort", "wedge_pop", "f_post", "f_accel", "lb_breakout", "lb_near", "s2_early"]
BEAR = ["down4", "leader_down", "below200", "ema_down", "death", "gap_down", "f_red"]
ICON = {"crit": "🛑", "warn": "⚠️", "good": "🚀", "info": "📅"}


def load(name):
    p = DATA / name
    return json.loads(p.read_text()) if p.exists() else None


def sma50(c, i):
    if i < 49:
        return None
    w = c[i - 49:i + 1]
    return None if any(v is None for v in w) else sum(w) / 50


def watch_alerts(wl, by, scans_of, scan_name, closes, vp, asof):
    """Python twin of wlAlerts() in docs/index.html."""
    out = []
    meta = wl.get("meta", {})
    for s in wl_symbols(wl):
        x, m = by.get(s), meta.get(s, {}) or {}
        px = (x or {}).get("px") or (vp.get(s) or {}).get("close")
        add = lambda lvl, txt, rank: out.append((lvl, rank, s, txt))
        r1 = ((x or {}).get("r") or {}).get("1D")
        prev = px / (1 + r1 / 100) if px and r1 is not None else None
        stop, piv, tgt = m.get("stop"), m.get("pivot"), m.get("target")
        if px and stop:
            if px < stop:
                add("crit", f"closed below your stop ${inr(stop)} today" if prev and prev >= stop else f"below your stop ${inr(stop)} ({sgn((px / stop - 1) * 100)})", 0)
            elif px / stop - 1 <= .03:
                add("warn", f"within {(px / stop - 1) * 100:.1f}% of your stop ${inr(stop)}", 2)
        if px and tgt and px >= tgt:
            add("good", f"reached your target ${inr(tgt)}", 3)
        if px and piv:
            fp = (px / piv - 1) * 100
            if prev and prev <= piv < px:
                vr = (x or {}).get("vr")
                add("good", f"broke out above your pivot ${inr(piv)}" + (f" on {vr:.1f}× volume" if vr else ""), 1)
            elif 0 <= fp <= 5:
                add("good", f"in the buy zone, {sgn(fp)} above pivot ${inr(piv)}", 4)
            elif -3 <= fp < 0:
                add("info", f"{abs(fp):.1f}% below your pivot ${inr(piv)} — get ready", 5)
        bad = [scan_name[i] for i in scans_of.get(s, []) if i in BEAR and i in scan_name]
        if bad:
            add("warn", "warning: " + ", ".join(bad), 2)
        good = [scan_name[i] for i in scans_of.get(s, []) if i in BULL and i in scan_name]
        if good:
            add("good", "in today's scans: " + ", ".join(good), 6)
        if x and x.get("rs") is not None and x.get("rs1w") is not None and x["rs"] < 70 <= x["rs1w"]:
            add("warn", f"RS rating fell to {x['rs']} (was {x['rs1w']})", 7)
        c = (closes or {}).get("c", {}).get(s)
        if c and len(c) > 51:
            i = len(c) - 1
            a, b = sma50(c, i), sma50(c, i - 1)
            if a and b and c[i] is not None and c[i - 1] is not None:
                if c[i] < a and c[i - 1] >= b:
                    add("warn", "closed below its 50-day average", 3)
                elif c[i] > a and c[i - 1] <= b:
                    add("info", "closed back above its 50-day average", 8)
        nr = (x or {}).get("next_results")
        if nr:
            dd = (datetime.strptime(nr, "%Y-%m-%d") - datetime.strptime(asof, "%Y-%m-%d")).days
            if 0 <= dd <= 7:
                add("info", f"earnings on {datetime.strptime(nr, '%Y-%m-%d'):%a %d %b} ({'today' if dd == 0 else 'tomorrow' if dd == 1 else f'in {dd} days'})", 9)
        if "FRESH" in str((vp.get(s) or {}).get("status", "")):
            add("good", "fresh VPCI buy signal this week", 6)
    order = {"crit": 0, "warn": 1, "good": 2, "info": 3}
    return sorted(out, key=lambda a: (order[a[0]], a[1], a[2]))


def evening():
    meta, stocks, scans, breadth, groups = (load(n) for n in ("meta.json", "stocks.json", "scans.json", "breadth.json", "groups.json"))
    if not meta or not stocks:
        print("No dashboard data - run compute.py first")
        return
    closes = load("closes.json")
    vpj = load("vpci/completed.json")
    vp = {}
    if vpj:
        cols = vpj["results"]["columns"]
        vp = {r[cols.index("symbol")]: dict(zip(cols, r)) for r in vpj["results"]["rows"]}
    by = {r["s"]: r for r in stocks}
    scans_of, scan_name = {}, {m["id"]: m["name"] for m in scans["meta"]}
    for i, syms in scans["hits"].items():
        for s in syms:
            scans_of.setdefault(s, []).append(i)
    asof = meta["asof"]
    bname = breadth.get("bench_name", "S&P 500")
    bi = meta["indices"].get(bname, {})
    L = len(breadth["dates"]) - 1
    a50, a50w = breadth["a50"][L], breadth["a50"][max(0, L - 5)]
    verdict = "Strong" if a50 >= 60 else "Mixed" if a50 >= 40 else "Weak"
    lines = [f"<b>🇺🇸 PKC Sector Radar US · {datetime.strptime(asof, '%Y-%m-%d'):%d %b %Y}</b>",
             f"{E(bname)} {inr(bi.get('px'))} ({sgn((bi.get('r') or {}).get('1D'), 2)}) · Breadth <b>{verdict}</b>: {a50:.1f}% above 50 DMA ({sgn(a50 - a50w, 1, ' pts')} in a week)",
             f"Advances/declines {breadth['adv'][L]}/{breadth['dec'][L]} · 52W highs/lows {breadth['nh'][L]}/{breadth['nl'][L]}"]
    th = []
    for name, syms in (groups or {}).get("Themes", {}).items():
        v = [by[s]["r"]["1M"] for s in syms if s in by and by[s]["r"].get("1M") is not None]
        if len(v) >= (meta.get("min_group_size") or 3):
            th.append((sum(v) / len(v), name))
    th.sort(reverse=True)
    if th:
        lines.append("Leading themes (1M): " + " · ".join(f"{E(n)} {sgn(v, 1)}" for v, n in th[:3]))

    gid, wl, _ = (None, None, {})
    try:
        gid, wl, _ = gist_load()
    except Exception as e:
        lines += ["", f"⚠️ Couldn't read your watchlists: {E(str(e))}"]
    if wl:
        syms = wl_symbols(wl)
        rets = []
        for L_ in wl.get("lists", []):
            for it in L_.get("items", []):
                px = (by.get(it["s"]) or {}).get("px")
                if px and it.get("p"):
                    rets.append((px / it["p"] - 1) * 100)
        al = watch_alerts(wl, by, scans_of, scan_name, closes, vp, asof)
        lines += ["", f"<b>⭐ Your watchlists</b> — {len(syms)} stocks" + (f" · average since added {sgn(sum(rets) / len(rets))}" if rets else "")]
        if al:
            for lvl, _, s, txt in al[:40]:
                lines.append(f"{ICON[lvl]} {sym(s)} — {E(txt)}")
            if len(al) > 40:
                lines.append(f"…and {len(al) - 40} more on the dashboard")
        else:
            lines.append("No alerts on your watchlists today.")
    elif GTOK:
        lines += ["", "⭐ No synced watchlists found — turn on <i>Sync devices</i> on the Watchlists tab."]

    top = lambda ids, n=8: sorted(ids, key=lambda s: -((by.get(s) or {}).get("rs") or 0))[:n]
    lines += ["", "<b>🔎 Today's scans</b>"]
    for sid, label in (("combo3", "Confluence (4+ signal groups)"), ("lb_breakout", "Long-base breakouts (100+ day bases)"), ("s2_early", "Early Stage 2, score 70+"), ("f_post", "Post-earnings breakouts"), ("f_insider", "Insider buying clusters"), ("high52_dcr", "52-week highs, strong close"), ("vcp", "VCP setups"), ("etf_leaders", "ETF leaders near highs")):
        h = scans["hits"].get(sid) or []
        if h:
            lines.append(f"{label}: <b>{len(h)}</b> — " + ", ".join(sym(s) for s in top(h)))
    fresh = [s for s, r in vp.items() if "FRESH" in str(r.get("status", ""))]
    if fresh:
        lines.append(f"Fresh VPCI buys: <b>{len(fresh)}</b> — " + ", ".join(sym(s) for s in fresh[:10]))
    if site_url():
        lines += ["", f'<a href="{site_url()}">Open the dashboard</a>']
    tg_send("\n".join(lines))
    print(f"Evening digest sent ({len(lines)} lines)")


# ───────────────────────────────────────────── live (market hours)
def live(force=False):
    import yahoo as Y
    now = datetime.now(ET)
    open_, close_ = now.replace(hour=9, minute=30, second=0), now.replace(hour=16, minute=5, second=0)
    if not force and (now.weekday() >= 5 or not open_ <= now <= close_):
        print(f"{now:%a %H:%M} New York - market closed, nothing to do")
        return
    gid, wl, state = gist_load()
    if not wl:
        print("No synced watchlists (GIST_TOKEN missing or Sync not turned on)")
        return
    today = now.strftime("%Y-%m-%d")
    if state.get("date") != today:
        state = {"date": today, "sent": []}
    meta = wl.get("meta", {})
    syms = wl_symbols(wl)
    q = Y.quotes(syms) if syms else None
    frac = max(0.05, min(1.0, ((now - open_).total_seconds() / 60) / 390))
    alerts = []
    for s in syms:
        if q is None or s not in q.index:
            continue
        r = q.loc[s]
        ltp, prev = r.get("regularMarketPrice"), r.get("regularMarketPreviousClose")
        if ltp is None or prev is None or not (ltp == ltp) or not (prev == prev) or prev <= 0:
            continue
        if Y.ts_to_date(r.get("regularMarketTime")) != today:      # holiday / no trade yet today
            continue
        m = meta.get(s) or {}
        vol, avg = r.get("regularMarketVolume"), r.get("averageDailyVolume3Month")
        pace = vol / (avg * frac) if vol and avg and avg == avg and avg > 0 else None
        chg = (ltp / prev - 1) * 100
        vtxt = f", volume pace {pace:.1f}× normal" if pace else ""
        lists = ", ".join(lists_of(wl, s))

        def add(kind, txt):
            key = f"{s}:{kind}"
            if key not in state["sent"]:
                state["sent"].append(key)
                alerts.append(f"{txt} <i>({E(lists)})</i>")
        piv, stop, tgt = m.get("pivot"), m.get("stop"), m.get("target")
        if stop and ltp < stop:
            add("stop", f"🛑 {sym(s)} below your stop ${inr(stop)} — now ${inr(ltp)} ({sgn(chg, 2)} today)")
        if tgt and ltp >= tgt:
            add("target", f"🎯 {sym(s)} hit your target ${inr(tgt)} — now ${inr(ltp)}")
        if piv:
            if prev <= piv < ltp:
                ext = (ltp / piv - 1) * 100
                add("pivot", f"🚀 {sym(s)} crossed your pivot ${inr(piv)} — now ${inr(ltp)} ({sgn(ext)} above{', extended' if ext > 5 else ''}){vtxt}")
            elif 0 < (piv / ltp - 1) * 100 <= 1:
                add("near", f"👀 {sym(s)} within {(piv / ltp - 1) * 100:.1f}% of your pivot ${inr(piv)} — now ${inr(ltp)}{vtxt}")
        if abs(chg) >= MOVE_PCT:
            add("up" if chg > 0 else "down", f"{'📈' if chg > 0 else '📉'} {sym(s)} {sgn(chg, 1)} today at ${inr(ltp)}{vtxt}")
    if alerts:
        tg_send(f"<b>⏰ {now:%H:%M} New York · live watchlist alerts</b>\n" + "\n".join(alerts))
        state_save(gid, state)
    print(f"{now:%H:%M} New York - {len(alerts)} new alerts")


# ───────────────────────────────────────────── test
def test():
    global CHAT
    if not CHAT:
        ups = tg("getUpdates")
        chats = {u["message"]["chat"]["id"]: u["message"]["chat"].get("first_name") or u["message"]["chat"].get("title")
                 for u in ups if "message" in u}
        if not chats:
            print("No chat found. Open your bot in Telegram, press Start (or send it 'hi'), then run this again.")
            return
        CHAT = str(list(chats)[-1])
        print(f"YOUR TELEGRAM_CHAT_ID IS: {CHAT}  ({list(chats.values())[-1]}) - save it as a repository secret")
    lines = ["<b>✅ PKC Sector Radar US is connected to Telegram</b>"]
    try:
        gid, wl, _ = gist_load()
        if wl:
            n = len(wl_symbols(wl))
            lv = sum(1 for s, m in (wl.get("meta") or {}).items() if m and (m.get("pivot") or m.get("stop") or m.get("target")))
            lines.append(f"Found your synced watchlists: {len(wl.get('lists', []))} lists, {n} stocks, {lv} with pivot/stop/target set.")
        else:
            lines.append("Watchlists not found yet — add the GIST_TOKEN secret and turn on Sync devices on the Watchlists tab.")
    except Exception as e:
        lines.append(f"Couldn't read watchlists: {E(str(e))}")
    lines.append("You'll get a digest after each daily update (after the US close), and live alerts every 15 minutes while the US market is open.")
    tg_send("\n".join(lines))
    print("Test message sent")


def main():
    ap = argparse.ArgumentParser()
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--evening", action="store_true")
    g.add_argument("--live", action="store_true")
    g.add_argument("--test", action="store_true")
    ap.add_argument("--force", action="store_true", help="live: run even outside market hours")
    a = ap.parse_args()
    if not TOKEN:
        print("TELEGRAM_BOT_TOKEN not set - Telegram alerts are off")
        return 0
    if not CHAT and not a.test:
        print("TELEGRAM_CHAT_ID not set - run the Telegram workflow once with mode 'test' to find it")
        return 0
    if a.test:
        test()
    elif a.evening:
        evening()
    else:
        live(a.force)
    return 0


if __name__ == "__main__":
    sys.exit(main())
