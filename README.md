# PKC Sector Radar · US

The US edition of [PKC Sector Radar](https://github.com/hbtipawan/scrn): the same ScreeningMantis-style dashboard, built for US stocks **and ETFs**.

- **Rotation** – theme / sector / industry / index returns for Today, 1W, 1M, 3M, 6M, YTD, 1Y, with mean or median, rank change vs last week, and a click-through list of the stocks in each group. Group by **ETFs** to rank the ETF sets side by side: SPDR sectors, equal-weight sectors, tech & innovation, cyclicals & financials, energy & resources, health care, size & style factors, countries, commodities & crypto, bonds & rates. Every ETF row opens its category, issuer, assets, expense ratio and top 10 holdings.
- **Breadth** – % of stocks above 20/50/200 DMA, advance/decline, 52-week highs/lows, 4% movers, one year of history (stocks only), plus **market direction by IBD's rules** (distribution-day count and last follow-through day on the S&P 500 and Nasdaq 100) and **Pradeep Bonde's Stockbee Market Monitor** (4% up/down counts with 5- and 10-day ratios, 25% up/down in a quarter and a month, 50% in a month, 13% in 34 days, T2108) with the McClellan oscillator and Zweig breadth thrust.
- **RS ranking** – every stock ranked 1–99 by relative strength, filterable by sector, industry, theme, Stage 2, Minervini trend template. Switch to **ETFs** to rank ~1,000 liquid ETFs on the same scale (an ETF with RS 80 has beaten 80% of stocks).
- **Scanners** – 74 technical scans (`scripts/scanners.py`) led by the **Confluence** list, including the published screens of **Pradeep Bonde (Stockbee)**, **Kristjan Kullamägi (Qullamaggie)**, **Mark Minervini**, **William O'Neil**, **Deepvue's presets**, **Mike Webster**, **Gil Morales & Chris Kacher**, **Oliver Kell**, **Leif Soreide**, **Linda Raschke** and **Larry Connors** — switch the list to **By trader** to see them grouped. Plus long bases, Stage 2 phases, 21 fundamental scans from SEC filings (incl. Minervini's **Code 33**, **insider buying clusters** and **CEO/CFO buying** from Form 4), and 11 **ETF scans**. Every technical scan shows how it did in a **walk-forward test** on 2022–2026 data.
- **Watchlists** – multiple lists, return since added vs the S&P 500, pivot/stop/target with buy-zone alerts, notes, flags, track record, import/export, phone ↔ PC sync. ETFs can be added too.
- **VPCI Screener** – your VPCI v3 screener (`vpci/` folder, US edition) run every evening on every stock in the universe: Fresh Signals, Buyable, Watchlist, All Results, Ranked, G4 Pending, New Listings, Sector Leadership, Sector Rotation — for completed weeks and the running week.

**No broker login, no API key, no password.** Prices come from Yahoo Finance's public chart service. Fundamentals come from the companies' own 10-Q / 10-K filings on SEC EDGAR, insider trades from SEC Form 4.

```
GitHub Actions (6:30 PM New York = 4:00 AM IST; daily, weekly or on demand - you choose; free)
   ├─ update_reference.py   S&P 500/400/600, Nasdaq-100, Dow 30 members      (Wikipedia, State Street)
   ├─ fetch_prices.py       ~5,600 symbols: 6 years of daily candles          (Yahoo Finance)
   ├─ fetch_fundamentals.py SEC XBRL results, Form 4 insider trades, Yahoo profiles (new data only)
   ├─ fetch_ath.py          all-time highs (weekly)
   ├─ compute.py            returns, RS, breadth, scans, bases, Stage 2       → docs/data/*.json
   ├─ vpci_app_run.py       VPCI screener on every stock
   └─ publish docs/ to Pages → website updates itself
```

Cost: $0. Setup: about 20 minutes, once.

---

## Step 1 — Try it on your PC (optional, 15 minutes)

1. Install Python 3.12 from python.org (tick "Add Python to PATH").
2. Unzip this folder, e.g. to `D:\scrn-us`.
3. Open Command Prompt in the folder and tell SEC who you are (SEC requires a contact e-mail from every program that reads EDGAR):
   ```
   set SEC_CONTACT=Your Name your@email.com
   ```
4. Run `run_local.bat` from that same window. It downloads all US stocks and ETFs (~10 minutes), the SEC data (~10 minutes the first time), computes everything and opens the site at http://localhost:8000.

Quick 50-stock test instead:
```
pip install -r requirements.txt
python scripts\fetch_prices.py --limit 50
python scripts\compute.py
```

## Step 2 — Put it on GitHub

1. On github.com click **New repository** → name it `scrn-us` → **Public** (see Step 3) → Create.
2. On the empty repo page click **uploading an existing file** and drag in everything from the folder. Make sure the `.github` folder goes in too (on Windows, turn on "show hidden items" if you can't see it). Commit.
3. **Settings → Secrets and variables → Actions → Variables tab → New repository variable**:
   `SEC_CONTACT` = `Your Name your@email.com` (any working e-mail; SEC refuses programs that don't identify themselves, and only uses it to contact you if your program misbehaves).
4. **Actions** → enable workflows if asked → **Daily update** → **Run workflow**. The first run takes about 35 minutes (it backfills a year of insider trades and every company's filings); after that about 15–20 minutes. A green tick means the site is published.

From then on it runs by itself every weekday after the US close — or weekly, or only when you ask (see "Automatic or manual updates" below). A public repo has unlimited free Action minutes.

## Automatic or manual updates (your choice)

You don't have to update every day. Set the repository variable **`UPDATE_MODE`** (**Settings → Secrets and variables → Actions → Variables → New repository variable**):

| `UPDATE_MODE` | What happens |
|---|---|
| `daily` (or no variable) | Updates by itself every weekday after the US close (≈ 4 AM IST) |
| `weekly` | Updates by itself on Fridays only — the week's close (≈ 4 AM IST Saturday) |
| `manual` | Never updates on its own |

Whatever the mode, you can update any time: **Actions → Daily update → Run workflow**, or the **Update now ↗** link at the bottom of the site (it opens that page; the site refreshes about 20 minutes later). When the data is more than a trading day old, a banner at the top of the site says how old it is, with the same button. Change the variable whenever you like — no code edits.

The Telegram live checks have their own switch: set **`LIVE_ALERTS`** = `off` to stop them (the test run still works). On your PC, `run_local.bat` is always a manual update.

Note: GitHub pauses scheduled workflows in a public repository after 60 days with no commits. In `daily` or `weekly` mode the nightly data commit keeps it alive; in `manual` mode there is no schedule to pause.

## Step 3 — Host the website (free)

**Public, simplest (2 minutes).** **Settings → Pages** → Source: **GitHub Actions**. The workflow publishes `docs/` itself. Your site is at `https://<your-username>.github.io/scrn-us/`.

**Private, only you.** Use Cloudflare Pages exactly as in the Indian edition's README (Connect to Git → build output directory `docs`, then Zero Trust → Access to lock it to your e-mail). Note: the data files are published by the workflow, not committed, so for Cloudflare change the "Save data to the repository" step to also `git add -f docs/data`.

## Step 4 — Edit themes and ETF sets

Themes live in `config/themes.csv`: one theme per row, symbols separated by spaces (67 themes to start: AI chips, semicap, AI networking & optics, data-center power, nuclear, neoclouds, quantum, space, drones, cybersecurity, GLP-1, regional banks, homebuilders, China ADRs, India ADRs…).

```
Theme,Symbols
AI Chips,NVDA AMD AVGO MRVL ARM ALAB CRDO TSM QCOM INTC MU
```

ETF sets live in `config/etf_groups.csv`: one ETF per row with its set and a short label.

```
Set,ETF,Label
US sectors,XLK,Technology
```

Edit on GitHub (open the file → pencil icon → commit). A theme appears once 3 of its stocks pass the filters; tickers that no longer trade are simply ignored. **Sector** and **Industry** come from Yahoo Finance's classification (11 sectors, ~145 industries) for every stock; **Index** groups are equal-weighted members of the S&P 500 and its 11 GICS sectors, S&P 400, S&P 600, Nasdaq-100 and Dow 30.

## Universe

- **Stocks:** every common stock, ADR and LP listed on NYSE, Nasdaq, NYSE American or Cboe (from Nasdaq Trader's daily symbol directory) with price ≥ $5, median daily dollar volume ≥ $5M over 50 days and market cap ≥ $300M — about 2,700 stocks. Warrants, units, rights, preferreds, notes, SPAC shells and closed-end funds are dropped.
- **ETFs:** every ETF with median dollar volume ≥ $5M (about 1,000), plus every ETF in `config/etf_groups.csv`. Leveraged, inverse and volatility ETFs are dropped. ETFs never count in breadth or stock RS percentiles.

## Watchlists

Same as the Indian edition: tap **☆** next to any stock or ETF, track since-added returns against the S&P 500, set pivot/stop/target, flags and notes, import TradingView lists (`NASDAQ:` / `NYSE:` prefixes are fine), export, share links, track record.

Both sites live on `<you>.github.io`, so they share the browser's storage — the US site uses its own keys (`radarus.*`) and its own gist file (`pkc-radar-us-watchlists.json`), so your Indian and US lists never mix. **Sync devices** works with the same `ghp_` gist token as the Indian site.

## Trader screens (exact published rules)

Each one follows the trader's own published numbers (sources are in the scan's description on the site). Where a rule needs judgement, the site says how it was turned into numbers.

| Trader | Screens |
|---|---|
| Pradeep Bonde (Stockbee) | 4% breakout (c/c1 ≥ 1.04, v > v1, v ≥ 100K) · 4% breakdown · Young TI65 (avgc7/avgc65 ≥ 1.05, not 25 days ago) · Anticipation (TI65 > 1.04, ±0.4% and ±$0.20 day, 100K+ shares 3 days) · $ breakout (c − o ≥ $0.90) · Episodic pivot (4% on 3× the 50-day volume, 300K+) · 9 million volume EP · the full **Market Monitor** on the Breadth tab |
| Kristjan Kullamägi (Qullamaggie) | Episodic pivot (10%+ gap on 3× volume after a quiet quarter) · Breakout setup (top 2% on 1/3/6-month gain, resting on rising 10/20-day averages) · Continuation base and Big-volume gainer (Deepvue's Qullamaggie presets) |
| Mark Minervini | Trend template · VCP · Power play (100%+ in 8 weeks, then ≤20% pullback over 2–6 weeks) · SEPA · **Code 33** (3 quarters of accelerating EPS and revenue growth, expanding margin) |
| William O'Neil / IBD | 3 weeks tight · RS line new high before price · CANSLIM-style · earnings acceleration · distribution days and follow-through days |
| Deepvue presets | High-octane RS · Launch pad (price within 3% of the 21 EMA, 50-day and 65 EMA, stacked and rising) · Power of 3 · RS new highs before price · 52-week high with strong close · 30/30 growth |
| Mike Webster | Swing leaders 3/6/12 months · Cross Fire · Hard Tape |
| Morales & Kacher | Power trend · Ants · Pocket pivot · Buyable gap up |
| Oliver Kell | Wedge pop · Bull snort (3%+ on 3× the 20-day volume, close in top 30%) · Kell growth universe |
| Leif Soreide | Low tight flag · Rocket base |
| Others | Green line breakout (Dr. Eric Wish) · Holy Grail (Linda Raschke) · RSI(2) pullback (Larry Connors) · Darvas box · NR7 (Crabel) · Weinstein Stage 2 |

**Walk-forward test** (`scripts/backtest_scans.py`, results in `config/scan_evidence.json`): every 5th session from Feb 2022 to Sep 2026, the scanners were run on the data available that day and each hit's return over the next 5, 21 and 63 sessions was compared with the average stock that day. Most scans' edges were small (under 1% a month). The strongest were the **power play** (+4.2% a month and +9.8% over 3 months vs the average stock, ahead in 4 of 5 years), **Qullamaggie's breakout setup** (+2.0% / +4.5%), **bull snort** (+1.5%, ahead in 5 of 5 years), **high-octane leaders** (+1.3%), **Bonde's EP** (+1.2%) and his **4% breakout** (+1.1%, ahead in 5 of 5 years). Several popular lists — 3 weeks tight, launch pad, RS line new highs before price, Bollinger squeeze, MACD and golden crosses — trailed the average stock in this period. Caveats: one market regime, today's universe (delisted losers are missing, which flatters fast movers), no stops or costs. Re-run it any time with `python scripts/backtest_scans.py` (about 10 minutes).

## Long-base identifier

The Indian edition's base detection (`scripts/bases.py`): base length, start, pivot, depth, pivot tests, tightness, volume dry-up, overhead supply, measured move, inner breakouts and mini charts — with a **US Base Score** fitted on US data (below).

**Checked on 20 years of US data** (`scripts/backtest_us.py`: Jan 2007 – Oct 2026, 2,672 stocks, 199,000 first breakouts, 1-year return after the breakout against the S&P 500). The full tables are in the tab and in `config/base_evidence.json`. **The US result is different from India's:**

- **A longer base did not mean a bigger move.** In an uptrend with RS 70+, 50–99-day bases beat the S&P 500 by about 4% a year; 200–399 days lagged by about 1%, 400–799 by about 3%, 800+ by about 6% — in both 2007–16 and 2017–26.
- **Long bases were steadier, not stronger:** fewer failed breakouts (27–31% vs 37%), but far fewer doubles.
- **RS carried the edge in the last decade:** RS 95+ breakouts beat the index by about 14% a year in 2017–26 and doubled 24% of the time.
- **The Indian Base Score did not sort US returns,** so the US edition has its own (0–100): RS rating 30, trend strength 35 (distance above the 200-day average, rising 150-day), base length 15 (50–99 days best), breakout-day volume 15, built after an advance 5. It was adopted only because it sorted returns in the same order in **both** 2007–16 and 2017–26: a score of 75+ beat the S&P 500 by 8.4% and 12.2% a year against +1.8% and −2.6% under 30, and doubled 14% and 25% of the time against 3% and 7%.
- **What it does not do:** the median breakout lagged the index in every band, and high scores failed more often (39–49%). It finds the big winners; it doesn't make the typical breakout safer, so use a stop under the pivot.
- **Tight right sides failed least but did worst** (slow, low-volatility stocks), so tightness is shown but not scored.

## Stage 2: early, mid or late

Exactly the Indian edition's code (`scripts/stage2.py`): phase from base count, months in Stage 2, gain from the low and slope slowing; Stage 2 Score from RS, slope, distance from the peak, extension, 50-day and 6-year high.

**Checked on 20 years of US data** (315,000 Stage 2 snapshots, `config/stage2_evidence.json`):

- **Late = more risk** in both halves: 25–29% fell into Stage 4 within 6 months against 19–21% for Early, with deeper drops.
- **Late did not mean lower returns** in the US (unlike India) — tighten stops, don't sell on phase alone.
- **The Stage 2 Score sorted outcomes in both halves:** from about −0.6% a year under 40 to +6.7% at 85+, and falls into Stage 4 from 31% to 11%. Best: Early or Mid with a score of 70+. Worst: Late under 55 (36% fell into Stage 4).
- **Distance above the 150-day line was bullish,** as in India; over 100% above it is a climax warning (median worst 6-month drop about −30%).

## Fundamentals (automatic, from SEC filings)

`scripts/fetch_fundamentals.py`, every evening, no login:

- **Quarterly results** from SEC's nightly XBRL bulk file (the numbers in every 10-Q and 10-K): revenue, operating income, pre-tax profit, tax, net income, EPS, interest, D&A, diluted shares, balance sheet every quarter, operating cash flow and capex for each fiscal year. Q4 is the full year minus the first three quarters. Companies' own fiscal years are respected (NVIDIA's ends in January, Apple's in September). Only companies whose filings changed are re-parsed, so a normal run takes a minute or two.
- **Insider trades** (SEC Form 4): open-market purchases and sales by officers and directors, a rolling year — SEC's quarterly insider data sets for the backfill, then every Form 4 filed since from EDGAR's daily index.
- **Profile, ownership, earnings dates** (Yahoo Finance): sector, industry, % held by institutions and insiders (a monthly snapshot is kept, so the "Institutions moving in" scan fills in as history builds up), next earnings date; for ETFs category, issuer, assets, expense ratio and top 10 holdings.

`scripts/fundamentals.py` turns this into profit and revenue growth (YoY, QoQ, trailing four quarters), three-quarter earnings acceleration, operating margin and its change, P/E, P/S, PEG, P/B and FCF yield on today's market cap, ROE, ROCE, debt/equity, free cash flow, capex growth, share-count change (buybacks/dilution), institutional ownership and insider activity. A **Fund rating 1–99** (70% growth, 30% quality) sits next to the RS rating, and every scanner can be limited to Fund 60+ or 80+.

Fundamental scanners: growth leaders 30/30, earnings acceleration, CANSLIM-style, SEPA, margin expansion, revenue streak, turnaround, post-earnings breakout, earnings in the next 7 days, quality compounders, GARP, capex cycle, net-cash small caps, **insider buying cluster**, **CEO/CFO buying**, **shrinking share count**, institutions moving in, under-owned leaders, and fundamental red flags (heavy insider selling, dilution, rising high debt, negative cash flow despite profit, profit and revenue both falling).

**Checked against Yahoo Finance** on 200 random stocks (704 quarters): revenue within 5% for 93.5% of quarters and net income within 5% for 90%. The rest are differences of definition, not errors: net income here is what the company reports as attributable to its shareholders (Yahoo sometimes deducts preferred dividends or minority interests); restated quarters after a divestiture use the restated figure; a bank's revenue is net interest income plus non-interest income (its own "revenue" tag is often only fee income, which the script ignores); revenue net of excise tax is used where both are reported.

Banks, insurers, brokers and other financials skip margin, ROCE, debt and cash-flow tests. Companies that file 20-F (most foreign ADRs such as TSM or ASML) publish no quarterly XBRL, so they get technicals and ownership only.

## VPCI Screener (US edition)

`vpci/` holds your screener with the engine files unchanged (`vpci_engine.py`, `signal_history.py`, `sector_history.py`). What changed for the US:

- `data_sources.py` builds the weekly bars from the evening's daily candles (`cache/prices.csv.gz`) — no second download — with yfinance as the fallback when you run the Streamlit app on its own. US session times (9:30–4:00 New York) for the running-week pro-rating.
- `app.py`: the universe is `Stock_List_US.csv` (written each evening from the dashboard's universe), Upstox/Kite panels removed, market cap shown in dollars, and the ranking's market-cap sweet spot is **$2B–$10B** (US mid caps) instead of ₹5,000–15,000 crore. Everything else is your code.
- Completed-weeks mode drops this week's bar until Friday's close; running-week mode keeps it and pro-rates the volume exactly as before.

Run the Streamlit app locally with `cd vpci` → `streamlit run app.py`.

## Telegram alerts

Same bot setup as the Indian edition (BotFather token, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `GIST_TOKEN` secrets — you can reuse the same bot and chat):

- **Evening digest** after each daily run (~6:45 PM New York / ~4:15 AM IST): S&P 500 and breadth verdict, top themes, every watchlist alert, today's key scanner hits: confluence, base breakouts with a US Base Score of 60+, power plays, Qullamaggie setups, early Stage 2, post-earnings breakouts, insider buying and ETF leaders.
- **Live alerts** every 15 minutes while the US market is open: one Yahoo quote request checks every watchlist symbol against your pivot, stop and target, plus ±5% moves with volume pace. Each alert is sent once per stock per day.

## Settings

`config/settings.json`:

| Setting | Default | Meaning |
|---|---|---|
| `min_price` | 5 | Ignore stocks below this price ($) |
| `min_median_turnover_m` | 5.0 | Median 50-day dollar volume, $ millions |
| `min_market_cap_m` | 300 | Market cap floor, $ millions |
| `etf_min_median_turnover_m` | 5.0 | ETF dollar-volume floor (ETFs in `etf_groups.csv` are always kept) |
| `prefilter_min_price` / `prefilter_min_dollar_volume_m` | 3 / 1.0 | Looser floor used to decide what to download |
| `base_history_calendar_days` | 2300 | Days of daily candles downloaded for the long-base identifier |
| `min_group_size` | 3 | Smallest group shown on the Rotation tab |
| `yahoo_requests_per_second` | 15 | Download speed; lower it if Yahoo refuses requests |

## How the numbers are calculated

- **Prices** are Yahoo's split-adjusted daily candles (not dividend-adjusted — the same as a TradingView chart). Dates are New York trading days; a run during market hours drops today's unfinished bar.
- **Group return** = equal-weighted mean (or median) of member stocks' returns. Rank arrows compare with the same calculation 5 sessions ago.
- **RS rating** = percentile (1–99) of 0.4 × 3-month + 0.2 × 6-month + 0.2 × 9-month + 0.2 × 12-month performance across all stocks; ETFs are placed on the stock distribution.
- **Stage** = price vs the 150-day SMA (30-week) and its 20-day slope: above and rising = Stage 2, below and falling = Stage 4.
- **Trend template** = Minervini's 8 conditions with RS ≥ 70.
- **Market cap** = Yahoo's figure, rolled forward to today's close.
- **Breadth** uses today's stock universe for the whole history.

## Troubleshooting

| Problem | Fix |
|---|---|
| Actions run is red | Open it and read the step that failed. If the Yahoo download failed, the yfinance step should have run instead. |
| No fundamentals / "SEC_CONTACT not set" in the log | Add the `SEC_CONTACT` repository variable (Step 2.3) and run the workflow again. |
| Site says "Data files could not be loaded" | The workflow hasn't succeeded yet — check the Actions tab. |
| Want data now | Actions → Daily update → Run workflow. |
| Insider or institution scans empty | Insider data fills on the first run; institutional changes need a few months of monthly snapshots. |

## A note on the data sources

Yahoo Finance's chart and quote services are free and widely used but unofficial; if they change, the job tries yfinance and the site header shows the source. SEC EDGAR is official and free; SEC asks for no more than 10 requests a second (the scripts use 8) and a contact e-mail in the User-Agent. Keep the dashboard for personal use; don't resell the data.
