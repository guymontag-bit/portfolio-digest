# Portfolio Digest

An automated system that reads stock positions and watchlists from a Google Sheet, gathers current pricing, news, and regulatory filings for each ticker, and uses Claude to generate tailored analytical briefings delivered by email — all running on a schedule via GitHub Actions.

## What it does

Each weekday morning (and on a separate Mon/Wed/Fri schedule for long-term holdings), the system:

1. Reads ticker lists from designated tabs in a Google Sheet
2. Fetches price data, recent news, and SEC filings for each ticker
3. Sends that data to Claude with a purpose-built prompt for each portfolio segment, grounded in the investor's own accumulated trading lessons
4. Emails the resulting briefing
5. For Current Positions and Active Watchlist, extracts the briefing's own flagged signals into a Signal Log for future accuracy tracking

There is no manual step in the daily flow — everything runs automatically once configured.

## Architecture

```
Google Sheets ──▶ Python script ──▶ Data sources ──▶ Claude API ──▶ Resend ──▶ Email
                  (GitHub Actions)   (price/news/filings)              │
                                                                        ▼
                                                              Signal Log (sheet)
```

| Component | Role |
|---|---|
| Google Sheets | Source of truth for tickers, positions, failure patterns, and (as a fallback) live prices; also the destination for the Signal Log and Upcoming Events |
| GitHub Actions | Scheduler and host — runs the scripts on cron triggers, no server required |
| Massive (formerly Polygon.io) | Primary price/quote data |
| Google Finance (via sheet formulas) | Fallback price data for tickers Massive doesn't cover |
| Finnhub | Primary news source |
| Google News RSS | Fallback news source |
| SEC EDGAR | 8-K filing lookups (material events: FDA decisions, trial results, etc.) — metadata only, not filing content |
| Claude API (Sonnet) | Generates the analytical briefing for each segment; also runs a secondary extraction pass for the Signal Log |
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
| Current positions | `Current Positions` | Exit-focused: flags declines >7%, low liquidity, downgrades, adverse news/filings; weighs unrealized P&L and portfolio weight | Weekdays |
| Active watchlist | `1 Active Watchlist` | Entry-focused: highest-conviction near-term candidates | Weekdays |
| Monitoring watchlist | `2 Monitoring Watchlist` | Signal-watching: flags tickers ready for promotion to Active | Weekdays |
| Reassess watchlist | `3 Reassess Watchlist` | Keep-or-cut review of underperforming watchlist names | Weekdays (toggle-able) |
| Long-term holdings | `Long Positions` | Thesis-level review; explicitly ignores daily price noise in favor of multi-month fundamentals; also position-aware (unrealized P&L, concentration risk) | Mon/Wed/Fri |
| Upcoming catalysts | `Upcoming Events` (written to, not read from) | Auto-populated calendar of future binary events (PDUFA dates, trial readouts, etc.) extracted from news | Weekly (Tuesdays) |

All five daily-flow segments (Portfolio, Active, Monitoring, Reassess, Long Positions) can be independently toggled off via GitHub Secrets without touching code — useful for reducing noise during vacations or when hobby capital is reallocated elsewhere.

**Note:** the weekly catalyst scraper sources its active ticker list differently from the rest of the pipeline — it reads from the `Investing Dashboard` tab filtered by a status column, not from `Current Positions`. These two lists are not guaranteed to stay in sync; see Known Limitations.

## Data flow per ticker

For every ticker in a given segment, the script gathers:

1. **Price/quote data** — tries Massive/Polygon first; if unavailable, falls back to Google Finance formulas already maintained in the sheet (price, open, high, low, volume, % change, trade time)
2. **News** — tries Finnhub first (last 24 hours); falls back to Google News RSS if empty
3. **SEC filings** — checks EDGAR for 8-K filings in the past 48 hours (filing metadata only — type, entity, filed date — not the filing's actual content)
4. **Position data** (Current Positions and Long Positions only) — share count and entry price from the sheet; unrealized gain/loss % and % of total portfolio are self-computed from quantity and live/current price rather than trusted from static sheet dollar columns, to avoid drift when a position is added to after initial entry

All of this is compiled into a structured data block, together with the investor's own known failure patterns, and passed to Claude along with a segment-specific prompt. Claude is explicitly instructed to only use the data provided — not to infer or fill gaps from general knowledge — to flag missing data rather than guess, and to output plain text rather than Markdown, since emails render the response as-is.

## Signal Log (feedback loop)

Scoped to **Current Positions and Active Watchlist only** (deliberately excluding Monitoring, Reassess, and Long Positions to avoid diluting the log's signal with lower-conviction tiers). After each of those two emails sends, a separate, cheap Claude call reads the digest's own prose and extracts the tickers it already flagged — EXIT FLAG (Current Positions) or ENTRY OPPORTUNITY / RISK FLAG (Active Watchlist) — into structured rows appended to a `Signal Log` tab (columns A–O: Log ID, Date Flagged, Ticker, Segment, Signal Type, Price at Flag, Reasoning Snippet, Trigger Basis, Position Context, Resolved, Price +5D/+10D/+20D, Outcome, Notes).

The log deliberately does not ask Claude to grade or score its own flags at write time — it only records what was flagged and why. Judging whether a flag was correct is intended as a future, separate scoring pass over the +5D/+10D/+20D price columns. Wrapped in its own try/except so a Signal Log failure can never block email delivery. Toggle-able via `SIGNAL_LOG_ENABLED`.

## Known Failure Patterns

A `Failure Patterns` tab (Lesson Name, Pattern Description, Trigger Signal) holds the investor's own accumulated, ticker-agnostic trading lessons. Read once per script run and injected into every digest prompt, with Claude explicitly instructed to actively screen each day's data against every listed pattern. Ticker-specific trade history (as opposed to general patterns) is not yet integrated — tracked as a future enhancement.

## Sheet column layout

**Current Positions and Long Positions** (same layout across both tabs):
| Column | B | C | D | E | F | G | H | I | J | K | L | M |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Field | Ticker | Qty | Entry Price | Entry Position | Current Price | Current Position | Price at Open | High | Low | Volume | Change % | Tradetime |

**Active, Monitoring, and Reassess Watchlist tabs** (ticker + price fallback only, no position data):
| Column | A | B | C | D | E | F | G | H |
|---|---|---|---|---|---|---|---|---|
| Field | Ticker | Current Price | Price at Open | High | Low | Volume | Change % | Tradetime |

**Failure Patterns:**
| Column | A | B | C |
|---|---|---|---|
| Field | Lesson Name | Pattern Description | Trigger Signal |

**Signal Log** (append-only, written by the pipeline, columns A–O as described above).

Columns beyond Ticker/position fields are populated with `GOOGLEFINANCE()` formulas and serve purely as the price fallback source.

## Configuration (GitHub Secrets)

| Secret | Purpose |
|---|---|
| `GOOGLE_CREDENTIALS` | Service account JSON for Sheets API access — needs read/write scope (not readonly), since the pipeline now writes to Signal Log and Upcoming Events |
| `SPREADSHEET_ID` | Target spreadsheet ID |
| `ANTHROPIC_API_KEY` | Claude API access |
| `RESEND_API_KEY` | Email delivery |
| `SENDER_EMAIL` / `RECIPIENT_EMAIL` | Email addresses (sends from `onboarding@resend.dev`, to your own verified address) |
| `FINNHUB_API_KEY` | News data |
| `POLYGON_API_KEY` | Price data (Massive/Polygon) |
| `PORTFOLIO_ENABLED` | `true`/`false` — toggles the Portfolio (Current Positions) email on/off |
| `ACTIVE_WATCHLIST_ENABLED` | `true`/`false` — toggles the Active Watchlist email on/off |
| `MONITORING_WATCHLIST_ENABLED` | `true`/`false` — toggles the Monitoring Watchlist email on/off |
| `REASSESS_ENABLED` | `true`/`false` — toggles the Reassess Watchlist email on/off |
| `LONG_POSITIONS_ENABLED` | `true`/`false` — toggles the Long Positions email on/off |
| `SIGNAL_LOG_ENABLED` | `true`/`false` — toggles Signal Log writes on/off |

All toggles default to enabled if the secret is unset.

## Schedule

Defined in `.github/workflows/daily_digest.yml`, all times in UTC (ET + buffer to absorb GitHub Actions scheduling delays). Each schedule uses a distinct trigger time to avoid a GitHub Actions limitation where multiple cron expressions matching the same timestamp on the same workflow collapse into a single event, silently skipping any job whose `if:` condition doesn't match whichever schedule string GitHub reports:

- **Daily digest** (`0 10 * * 1-5`) — Portfolio, Active, Monitoring, and Reassess emails, weekdays, 10:00 UTC
- **Weekly catalyst scraper** (`5 10 * * 2`) — Tuesdays, 10:05 UTC, scans news for future binary events and writes them to the `Upcoming Events` tab
- **Long positions digest** (`10 10 * * 1,3,5`) — Monday/Wednesday/Friday, 10:10 UTC

All jobs also support manual triggering via `workflow_dispatch` in the GitHub Actions UI, which is the recommended way to test changes before merging. Note that `workflow_dispatch` bypasses the schedule-collision issue above, so a manual run succeeding doesn't guarantee the scheduled trigger will.

## Design principles

- **Segment-appropriate prompts** — each portfolio segment gets language matched to its actual purpose (exit discipline for active trades, thesis review for long-term holds, promotion/demotion logic for watchlist tiers) rather than one generic summary format
- **Grounding over fabrication** — prompts explicitly instruct Claude to only use provided data and to state "insufficient data" rather than guess, given the risk of hallucinated analysis on sparse micro-cap news coverage
- **Graceful degradation** — every data source (price, news) has a fallback, and missing data is surfaced both in the prompt and via a per-email data coverage summary line, rather than silently dropped or invented
- **Toggle over delete** — every segment, plus Signal Log, can be turned off via secrets without touching code
- **Deduplication by default** — ticker lists are deduplicated on read, since source sheets are populated by formulas/queries that can produce duplicates
- **Learn from experience, not just live data** — known failure patterns and (eventually) flag-accuracy history feed back into the prompts, rather than each run reasoning from a blank slate

## Known Limitations

- Massive/Polygon's free tier has coverage gaps on micro-cap and OTC tickers, partially mitigated by the Google Finance sheet fallback
- Google News RSS is an unofficial/unsupported feed and may occasionally require maintenance if its format changes
- SEC EDGAR integration surfaces filing metadata (type, entity, date) only — not filing content — so Claude cannot currently distinguish a material adverse 8-K from a routine one
- GitHub Actions scheduled triggers are not time-precise (can run late) and, as noted above, cannot fire two cron matches at the same timestamp as separate events — schedules are staggered and set earlier than the target delivery time to compensate
- Resend's free tier sends from a shared address (`onboarding@resend.dev`) and can only send to verified recipient addresses — appropriate for personal use, not for sending to third parties
- The weekly catalyst scraper's active-ticker source (`Investing Dashboard` status column) is independent of `Current Positions`, so the two lists can drift out of sync
- No feedback loop yet scores the Signal Log's own flag accuracy, and ticker-specific trade history is not yet integrated into prompts (only general failure patterns are)
- Full-file rewrites carry real risk of silently dropping features built in earlier, out-of-context sessions — see project conventions for mitigations (attach current file before rewriting, prefer targeted edits, read the diff's removed lines)
