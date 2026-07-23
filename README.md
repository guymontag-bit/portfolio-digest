# Portfolio Digest

An automated system that reads stock positions and watchlists from a Google Sheet, gathers current pricing, news, and regulatory filings for each ticker, and uses Claude to generate tailored analytical briefings delivered by email — all running on a schedule via GitHub Actions.

## What it does

Each weekday morning (and on a separate Mon/Wed/Fri schedule for long-term holdings), the system:

1. Reads ticker lists from designated tabs in a Google Sheet
2. Fetches price data, recent news, and SEC filings for each ticker
3. Sends that data to Claude with a purpose-built prompt for each portfolio segment
4. Emails the resulting briefing

There is no manual step in the daily flow — everything runs automatically once configured.

## Architecture

```
Google Sheets ──▶ Python script ──▶ Data sources ──▶ Claude API ──▶ Resend ──▶ Email
                  (GitHub Actions)   (price/news/filings)
```

| Component | Role |
|---|---|
| Google Sheets | Source of truth for tickers, positions, and (as a fallback) live prices |
| GitHub Actions | Scheduler and host — runs the scripts on cron triggers, no server required |
| Massive (formerly Polygon.io) | Primary price/quote data |
| Google Finance (via sheet formulas) | Fallback price data for tickers Massive doesn't cover |
| Finnhub | Primary news source |
| Google News RSS | Fallback news source |
| SEC EDGAR | 8-K filing lookups (material events: FDA decisions, trial results, etc.) |
| Claude API (Sonnet) | Generates the analytical briefing for each segment |
| Resend | Email delivery |

## Repository structure

```
.
├── digest.py                      # Main script: daily + long-positions flows
├── weekly_catalyst_scraper.py     # Weekly script: extracts future catalyst dates into the sheet
├── requirements.txt
└── .github/workflows/
    └── daily_digest.yml           # Defines all three scheduled jobs
```

## Portfolio segments

The system covers six distinct segments, each with its own Google Sheet tab, its own Claude prompt tuned to that segment's purpose, and its own email:

| Segment | Sheet tab | Purpose | Schedule |
|---|---|---|---|
| Current positions | `Current Positions` | Exit-focused: flags declines >7%, low liquidity, downgrades, adverse news/filings | Weekdays |
| Active watchlist | `1 Active Watchlist` | Entry-focused: highest-conviction near-term candidates | Weekdays |
| Monitoring watchlist | `2 Monitoring Watchlist` | Signal-watching: flags tickers ready for promotion to Active | Weekdays |
| Reassess watchlist | `3 Reassess Watchlist` | Keep-or-cut review of underperforming watchlist names | Weekdays (toggle-able) |
| Long-term holdings | `Long Positions` | Thesis-level review; explicitly ignores daily price noise in favor of multi-month fundamentals | Mon/Wed/Fri |
| Upcoming catalysts | `Upcoming Events` (written to, not read from) | Auto-populated calendar of future binary events (PDUFA dates, trial readouts, etc.) extracted from news | Weekly (Tuesdays) |

Each portfolio/watchlist tab expects tickers in a fixed column with a header row (see column layout below). Tabs can be toggled on/off independently via GitHub Secrets without any code changes.

## Data flow per ticker

For every ticker in a given segment, the script gathers:

1. **Price/quote data** — tries Massive/Polygon first; if unavailable, falls back to Google Finance formulas already maintained in the sheet (price, open, high, low, volume, % change, trade time)
2. **News** — tries Finnhub first (last 24 hours); falls back to Google News RSS if empty
3. **SEC filings** — checks EDGAR for 8-K filings in the past 48 hours

All of this is compiled into a structured data block and passed to Claude along with a segment-specific prompt. Claude is explicitly instructed to only use the data provided — not to infer or fill gaps from general knowledge — and to flag missing data rather than guess.

## Sheet column layout

**Current Positions** (`Current Positions` tab):
| Column | B | C | D | E | F | G | H | I | J | K | L | M |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Field | Ticker | Qty | Entry Price | Entry Position | Current Price | Current Position | Price at Open | High | Low | Volume | Change % | Tradetime |

**Watchlist tiers and Long Positions** (same layout across all four tabs):
| Column | A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|---|
| Field | Ticker | Current Price | Price at Open | High | Low | Volume | Change % | Tradetime |

Columns beyond Ticker on watchlist/long-position tabs are populated with `GOOGLEFINANCE()` formulas and serve purely as the price fallback source.

## Configuration (GitHub Secrets)

| Secret | Purpose |
|---|---|
| `GOOGLE_CREDENTIALS` | Service account JSON for Sheets API access |
| `SPREADSHEET_ID` | Target spreadsheet ID |
| `ANTHROPIC_API_KEY` | Claude API access |
| `RESEND_API_KEY` | Email delivery |
| `SENDER_EMAIL` / `RECIPIENT_EMAIL` | Email addresses (sends from `onboarding@resend.dev`, to your own verified address) |
| `FINNHUB_API_KEY` | News data |
| `POLYGON_API_KEY` | Price data (Massive/Polygon) |
| `REASSESS_ENABLED` | `true`/`false` — toggles the Reassess Watchlist email on/off |
| `LONG_POSITIONS_ENABLED` | `true`/`false` — toggles the Long Positions email on/off |

## Schedule

Defined in `.github/workflows/daily_digest.yml`, all times in UTC (ET + buffer to absorb GitHub Actions scheduling delays):

- **Daily digest** (`0 10 * * 1-5`) — Portfolio, Active, Monitoring, and Reassess emails, weekdays
- **Weekly catalyst scraper** (`0 10 * * 2`) — Tuesdays, scans news for future binary events and writes them to the `Upcoming Events` tab
- **Long positions digest** (`0 10 * * 1,3,5`) — Monday/Wednesday/Friday

All jobs also support manual triggering via `workflow_dispatch` in the GitHub Actions UI, which is the recommended way to test changes before merging.

## Design principles

- **Segment-appropriate prompts** — each portfolio segment gets language matched to its actual purpose (exit discipline for active trades, thesis review for long-term holds, promotion/demotion logic for watchlist tiers) rather than one generic summary format
- **Grounding over fabrication** — prompts explicitly instruct Claude to only use provided data and to state "insufficient data" rather than guess, given the risk of hallucinated analysis on sparse micro-cap news coverage
- **Graceful degradation** — every data source (price, news) has a fallback, and missing data is surfaced to the reader rather than silently dropped or invented
- **Toggle over delete** — segments can be turned off via secrets without touching code, useful for tiers that are empty or temporarily irrelevant
- **Deduplication by default** — ticker lists are deduplicated on read, since source sheets are populated by formulas/queries that can produce duplicates

## Known limitations

- Massive/Polygon's free tier has coverage gaps on micro-cap and OTC tickers, partially mitigated by the Google Finance sheet fallback
- Google News RSS is an unofficial/unsupported feed and may occasionally require maintenance if its format changes
- GitHub Actions scheduled triggers are not time-precise (can run late); schedules are set earlier than the target delivery time to compensate
- Resend's free tier sends from a shared address (`onboarding@resend.dev`) and can only send to verified recipient addresses — appropriate for personal use, not for sending to third parties
