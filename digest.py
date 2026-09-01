import os
import sys
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from anthropic import Anthropic
import resend

# ── Configuration ────────────────────────────────────────────────────────────

SPREADSHEET_ID           = os.environ["SPREADSHEET_ID"]

# Current Positions and Long Positions share the same column layout:
# B=Ticker, C=Qty, D=Entry Price, E=Entry Position, F=Current Price,
# G=Current Position, H=Price at Open, I=High, J=Low, K=Volume, L=Change %, M=Tradetime
PORTFOLIO_TAB             = "Current Positions"
LONG_POSITIONS_TAB        = "Long Positions"
POSITION_RANGE            = "B2:M200"

# Watchlist tabs are ticker-only plus price fallback columns:
# A=Ticker, B=Current Price, C=Price at Open, D=High, E=Low, F=Volume, G=Change %, H=Tradetime
ACTIVE_WATCHLIST_TAB      = "1 Active Watchlist"
MONITORING_WATCHLIST_TAB  = "2 Monitoring Watchlist"
REASSESS_WATCHLIST_TAB    = "3 Reassess Watchlist"
FAILURE_PATTERNS_TAB      = "Failure Patterns"
FAILURE_PATTERNS_RANGE    = "A2:C50"
TRADE_JOURNAL_TAB         = "Trade Journal"
TRADE_JOURNAL_RANGE       = "A2:D500"
WATCHLIST_RANGE           = "A2:H200"

PORTFOLIO_ENABLED            = os.environ.get("PORTFOLIO_ENABLED", "true").lower() == "true"
ACTIVE_WATCHLIST_ENABLED     = os.environ.get("ACTIVE_WATCHLIST_ENABLED", "true").lower() == "true"
MONITORING_WATCHLIST_ENABLED = os.environ.get("MONITORING_WATCHLIST_ENABLED", "true").lower() == "true"
REASSESS_ENABLED             = os.environ.get("REASSESS_ENABLED", "true").lower() == "true"
LONG_POSITIONS_ENABLED       = os.environ.get("LONG_POSITIONS_ENABLED", "true").lower() == "true"

# Signal Log — feedback-loop record of EXIT FLAG / ENTRY OPPORTUNITY / RISK FLAG
# signals, scoped to Current Positions and Active Watchlist only (see columns A-O)
SIGNAL_LOG_TAB     = "Signal Log"
SIGNAL_LOG_RANGE   = "A:O"
SIGNAL_LOG_ENABLED = os.environ.get("SIGNAL_LOG_ENABLED", "true").lower() == "true"

# Trade Journal — ticker-specific history injected into build_data_block, one
# most-recent entry per ticker (see build_trade_journal_index)
TRADE_JOURNAL_ENABLED = os.environ.get("TRADE_JOURNAL_ENABLED", "true").lower() == "true"

RECIPIENT_EMAIL   = os.environ["RECIPIENT_EMAIL"]
SENDER_EMAIL       = os.environ["SENDER_EMAIL"]
ANTHROPIC_API_KEY  = os.environ["ANTHROPIC_API_KEY"]
RESEND_API_KEY     = os.environ["RESEND_API_KEY"]
FINNHUB_API_KEY    = os.environ["FINNHUB_API_KEY"]
POLYGON_API_KEY    = os.environ["POLYGON_API_KEY"]

# Column offsets relative to each range's starting column
PORTFOLIO_FIELD_OFFSETS = {  # relative to B (B=0)
    "price":      4,  # F
    "open":       6,  # H
    "high":       7,  # I
    "low":        8,  # J
    "volume":     9,  # K
    "change_pct": 10, # L
    "tradetime":  11, # M
}
POSITION_FIELD_OFFSETS = {  # relative to B (B=0) — qty/cost basis fields
    "qty":          1,  # C
    "entry_price":  2,  # D
    "entry_value":  3,  # E
    "current_value":5,  # G
}
WATCHLIST_FIELD_OFFSETS = {  # relative to A (A=0)
    "price":      1,  # B
    "open":       2,  # C
    "high":       3,  # D
    "low":        4,  # E
    "volume":     5,  # F
    "change_pct": 6,  # G
    "tradetime":  7,  # H
}

# ── Google Sheets ─────────────────────────────────────────────────────────────

def _sheet_service():
    """
    Return an authenticated Google Sheets service.
    Uses read/write scope (not readonly) so this same service can also append
    rows to the Signal Log tab — the catalyst scraper already writes to this
    spreadsheet with this scope under the same service account, so no new
    sharing/permission setup should be needed.
    """
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)

def _safe_float(value):
    """Parse a sheet cell into a float, returning None for blank/#N/A/invalid values."""
    if value is None:
        return None
    s = str(value).strip().replace("$", "").replace(",", "")
    if s == "" or s.upper() in ("#N/A", "N/A", "#ERROR!", "#REF!", "#VALUE!"):
        return None
    try:
        return float(s)
    except ValueError:
        return None

def _read_tickers_with_fallback(tab, cell_range, ticker_col_offset, field_offsets,
                                 position_offsets=None):
    """
    Generic helper: read tickers plus optional Google Finance fallback price fields
    (and optionally position/cost-basis fields) from a named tab.

    Returns a deduplicated list of dicts:
      {"ticker": str, "fallback": {...} or None, "position": {...} or None}
    """
    service = _sheet_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{tab}!{cell_range}"
    ).execute()
    values = result.get("values", [])

    seen = set()
    rows = []
    for row in values:
        if len(row) <= ticker_col_offset:
            continue
        raw_ticker = row[ticker_col_offset].strip() if row[ticker_col_offset] else ""
        if not raw_ticker:
            continue
        ticker = raw_ticker.upper()
        if ticker in seen:
            continue
        seen.add(ticker)

        def cell(offset):
            return row[offset] if len(row) > offset else None

        fallback = {
            "price":      _safe_float(cell(field_offsets["price"])),
            "open":       _safe_float(cell(field_offsets["open"])),
            "high":       _safe_float(cell(field_offsets["high"])),
            "low":        _safe_float(cell(field_offsets["low"])),
            "volume":     _safe_float(cell(field_offsets["volume"])),
            "change_pct": _safe_float(cell(field_offsets["change_pct"])),
            "tradetime":  cell(field_offsets["tradetime"]),
        }
        if fallback["price"] is None:
            fallback = None

        position = None
        if position_offsets:
            qty           = _safe_float(cell(position_offsets["qty"]))
            entry_price   = _safe_float(cell(position_offsets["entry_price"]))
            entry_value   = _safe_float(cell(position_offsets["entry_value"]))
            current_value = _safe_float(cell(position_offsets["current_value"]))
            if any(v is not None for v in (qty, entry_price, entry_value, current_value)):
                position = {
                    "qty":           qty,
                    "entry_price":   entry_price,
                    "entry_value":   entry_value,
                    "current_value": current_value,
                }

        rows.append({"ticker": ticker, "fallback": fallback, "position": position})

    return rows

def get_tickers_from_sheet():
    """Read unique portfolio tickers, price fallback data, and position data
    from Current Positions."""
    rows = _read_tickers_with_fallback(
        PORTFOLIO_TAB, POSITION_RANGE, ticker_col_offset=0,
        field_offsets=PORTFOLIO_FIELD_OFFSETS,
        position_offsets=POSITION_FIELD_OFFSETS
    )
    print(f"Found {len(rows)} portfolio tickers: {', '.join(r['ticker'] for r in rows)}")
    return rows

def get_active_watchlist_from_sheet():
    rows = _read_tickers_with_fallback(
        ACTIVE_WATCHLIST_TAB, WATCHLIST_RANGE, ticker_col_offset=0,
        field_offsets=WATCHLIST_FIELD_OFFSETS
    )
    print(f"Found {len(rows)} active watchlist tickers: {', '.join(r['ticker'] for r in rows)}")
    return rows

def get_monitoring_watchlist_from_sheet():
    rows = _read_tickers_with_fallback(
        MONITORING_WATCHLIST_TAB, WATCHLIST_RANGE, ticker_col_offset=0,
        field_offsets=WATCHLIST_FIELD_OFFSETS
    )
    print(f"Found {len(rows)} monitoring watchlist tickers: {', '.join(r['ticker'] for r in rows)}")
    return rows

def get_reassess_watchlist_from_sheet():
    rows = _read_tickers_with_fallback(
        REASSESS_WATCHLIST_TAB, WATCHLIST_RANGE, ticker_col_offset=0,
        field_offsets=WATCHLIST_FIELD_OFFSETS
    )
    print(f"Found {len(rows)} reassess watchlist tickers: {', '.join(r['ticker'] for r in rows)}")
    return rows

def get_failure_patterns():
    """Read general, ticker-agnostic failure patterns from the Failure Patterns tab."""
    service = _sheet_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{FAILURE_PATTERNS_TAB}!{FAILURE_PATTERNS_RANGE}"
    ).execute()
    values = result.get("values", [])

    patterns = []
    for row in values:
        if not row or not row[0].strip():
            continue
        name        = row[0].strip()
        description = row[1].strip() if len(row) > 1 and row[1] else ""
        trigger     = row[2].strip() if len(row) > 2 and row[2] else ""
        patterns.append({"name": name, "description": description, "trigger": trigger})

    print(f"Found {len(patterns)} failure patterns")
    return patterns

def build_failure_patterns_block():
    """Format failure patterns into a text block for prompt insertion."""
    patterns = get_failure_patterns()
    if not patterns:
        return ""

    block = "\n\nKNOWN FAILURE PATTERNS — the investor has learned these lessons from past trades. Actively screen today's data against each one and call it out explicitly if a holding or watchlist ticker matches a pattern:\n"
    for p in patterns:
        block += f"\n- {p['name']}"
        if p["description"]:
            block += f": {p['description']}"
        if p["trigger"]:
            block += f" (Watch for: {p['trigger']})"
    return block

def get_trade_journal_entries():
    """Read raw Trade Journal rows: Ticker | Date | Outcome | Lesson."""
    service = _sheet_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{TRADE_JOURNAL_TAB}!{TRADE_JOURNAL_RANGE}"
    ).execute()
    values = result.get("values", [])

    entries = []
    for row in values:
        if not row or not row[0].strip():
            continue
        entries.append({
            "ticker":  row[0].strip().upper(),
            "date":    row[1].strip() if len(row) > 1 and row[1] else "",
            "outcome": row[2].strip() if len(row) > 2 and row[2] else "",
            "lesson":  row[3].strip() if len(row) > 3 and row[3] else "",
        })

    print(f"Found {len(entries)} trade journal entries")
    return entries

def build_trade_journal_index():
    """
    Build a ticker -> most recent entry lookup from the Trade Journal tab.
    Assumes rows are entered in roughly chronological order (same convention
    as the append-only Signal Log) — for a given ticker, the last matching row
    read wins. If the journal isn't in date order, sort the sheet by Date
    before running the digest.
    """
    if not TRADE_JOURNAL_ENABLED:
        return {}
    try:
        entries = get_trade_journal_entries()
    except Exception as e:
        print(f"Warning: could not read Trade Journal, skipping: {e}")
        return {}

    index = {}
    for entry in entries:
        if entry["ticker"]:
            index[entry["ticker"]] = entry  # last one read wins — most recent
    return index

def get_long_positions_from_sheet():
    """Read unique long-position tickers, price fallback data, and position data
    from Long Positions (same column layout as Current Positions)."""
    rows = _read_tickers_with_fallback(
        LONG_POSITIONS_TAB, POSITION_RANGE, ticker_col_offset=0,
        field_offsets=PORTFOLIO_FIELD_OFFSETS,
        position_offsets=POSITION_FIELD_OFFSETS
    )
    print(f"Found {len(rows)} long position tickers: {', '.join(r['ticker'] for r in rows)}")
    return rows

def compute_total_value(ticker_rows):
    """Sum current_value across all rows that have it, for % of portfolio calculations."""
    total = 0.0
    have_any = False
    for row in ticker_rows:
        pos = row.get("position")
        if pos and pos.get("current_value") is not None:
            total += pos["current_value"]
            have_any = True
    return total if have_any else None

# ── Market Data (Massive / Polygon) with sheet fallback ──────────────────────

def get_quote(ticker, fallback=None):
    """
    Fetch previous close price and daily change from Massive/Polygon.
    If the API returns nothing and a fallback dict (from Google Finance formulas
    in the sheet) is provided, use that instead and mark the source accordingly.
    """
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev"
    params = {"adjusted": "true", "apiKey": POLYGON_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultsCount", 0) > 0:
            result = data["results"][0]
            close      = result["c"]
            open_price = result["o"]
            change_pct = ((close - open_price) / open_price) * 100
            return {
                "close":      close,
                "open":       open_price,
                "high":       result["h"],
                "low":        result["l"],
                "volume":     result["v"],
                "change_pct": round(change_pct, 2),
                "source":     "Massive/Polygon",
                "tradetime":  None,
            }
    except Exception as e:
        print(f"Warning: Could not fetch quote for {ticker} from Massive/Polygon: {e}")

    if fallback and fallback.get("price") is not None:
        print(f"  Using sheet fallback price data for {ticker}")
        return {
            "close":      fallback.get("price"),
            "open":       fallback.get("open"),
            "high":       fallback.get("high"),
            "low":        fallback.get("low"),
            "volume":     fallback.get("volume"),
            "change_pct": fallback.get("change_pct"),
            "source":     "Google Finance (sheet)",
            "tradetime":  fallback.get("tradetime"),
        }

    return None

# ── News (Finnhub) ────────────────────────────────────────────────────────────

def get_finnhub_news(ticker):
    """Fetch last 24 hours of news for a ticker from Finnhub."""
    today     = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    url    = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": ticker,
        "from":   yesterday,
        "to":     today,
        "token":  FINNHUB_API_KEY
    }
    try:
        r        = requests.get(url, params=params, timeout=10)
        articles = r.json()
        return [
            {"headline": a["headline"], "summary": a.get("summary", ""), "source": "Finnhub"}
            for a in articles[:5]
            if a.get("headline")
        ]
    except Exception as e:
        print(f"Warning: Could not fetch Finnhub news for {ticker}: {e}")
    return []

# ── News Fallback (Google News RSS) ──────────────────────────────────────────

def get_google_news(ticker):
    """Fallback: fetch recent headlines from Google News RSS for a ticker."""
    url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
    try:
        r    = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:5]
        articles = []
        for item in items:
            headline = item.findtext("title", "").strip()
            if headline:
                articles.append({
                    "headline": headline,
                    "summary":  "",
                    "source":   "Google News"
                })
        return articles
    except Exception as e:
        print(f"Warning: Could not fetch Google News for {ticker}: {e}")
    return []

def get_news(ticker):
    """Return Finnhub news; fall back to Google News RSS if Finnhub returns nothing."""
    articles = get_finnhub_news(ticker)
    if not articles:
        print(f"  No Finnhub news for {ticker}, trying Google News RSS...")
        articles = get_google_news(ticker)
    return articles

# ── SEC EDGAR 8-K Filings ─────────────────────────────────────────────────────

def get_edgar_filings(ticker):
    """Fetch recent 8-K filings from SEC EDGAR for a ticker (past 48 hours)."""
    try:
        search_url = (
            f"https://efts.sec.gov/LATEST/search-index?q=%22{ticker}%22"
            f"&forms=8-K"
            f"&dateRange=custom"
            f"&startdt={(datetime.utcnow()-timedelta(days=2)).strftime('%Y-%m-%d')}"
            f"&enddt={datetime.utcnow().strftime('%Y-%m-%d')}"
        )
        headers = {"User-Agent": "portfolio-digest contact@example.com"}
        r    = requests.get(search_url, headers=headers, timeout=10)
        data = r.json()
        hits = data.get("hits", {}).get("hits", [])

        filings = []
        for hit in hits[:3]:
            src         = hit.get("_source", {})
            filed_at    = src.get("file_date", "")
            form_type   = src.get("form_type", "8-K")
            entity      = src.get("entity_name", ticker)
            description = src.get("period_of_report", "")
            filings.append({
                "form":        form_type,
                "entity":      entity,
                "filed":       filed_at,
                "description": description,
            })
        return filings
    except Exception as e:
        print(f"Warning: Could not fetch EDGAR filings for {ticker}: {e}")
    return []

# ── Data Assembly ─────────────────────────────────────────────────────────────

def build_portfolio_data(ticker_rows, total_value=None):
    """
    Fetch quotes (with sheet fallback), news, and EDGAR filings for all tickers.
    ticker_rows is a list of {"ticker", "fallback", "position"}.
    total_value, if provided, is used to compute % of portfolio per holding.
    """
    portfolio = []
    for row in ticker_rows:
        ticker   = row["ticker"]
        fallback = row.get("fallback")
        position = row.get("position")
        print(f"Fetching data for {ticker}...")
        quote   = get_quote(ticker, fallback=fallback)
        news    = get_news(ticker)
        filings = get_edgar_filings(ticker)
        time.sleep(0.25)  # gentle rate limiting

        position_metrics = None
        if position:
            unrealized_pct = None
            entry_value   = position.get("entry_value")
            current_value = position.get("current_value")
            entry_price   = position.get("entry_price")

            if entry_value is not None and entry_value != 0 and current_value is not None:
                unrealized_pct = round((current_value - entry_value) / entry_value * 100, 2)
            elif entry_price is not None and entry_price != 0 and quote and quote.get("close") is not None:
                unrealized_pct = round((quote["close"] - entry_price) / entry_price * 100, 2)

            pct_of_portfolio = None
            if total_value and current_value is not None and total_value != 0:
                pct_of_portfolio = round((current_value / total_value) * 100, 2)

            position_metrics = {
                "qty":               position.get("qty"),
                "entry_price":       entry_price,
                "current_value":     current_value,
                "unrealized_pct":    unrealized_pct,
                "pct_of_portfolio":  pct_of_portfolio,
            }

        portfolio.append({
            "ticker":   ticker,
            "quote":    quote,
            "news":     news,
            "filings":  filings,
            "position": position_metrics
        })
    return portfolio

# ── Data Block Builder ────────────────────────────────────────────────────────

def build_data_block(portfolio, trade_journal_index=None):
    """Build a readable text block from portfolio data for Claude prompts."""
    trade_journal_index = trade_journal_index or {}
    data_block = ""
    for holding in portfolio:
        ticker   = holding["ticker"]
        quote    = holding["quote"]
        news     = holding["news"]
        filings  = holding.get("filings", [])
        position = holding.get("position")

        data_block += f"\n## {ticker}\n"

        if quote:
            missing = []
            direction = "▲" if (quote.get("change_pct") or 0) >= 0 else "▼"

            price_line = f"Price: ${quote['close']:.2f}" if quote.get("close") is not None else "Price: unavailable"
            if quote.get("change_pct") is not None:
                price_line += f" {direction} {abs(quote['change_pct'])}% yesterday"
            else:
                missing.append("change %")
            data_block += price_line + "\n"

            high = f"${quote['high']:.2f}" if quote.get("high") is not None else "n/a"
            if quote.get("high") is None:
                missing.append("high")
            low = f"${quote['low']:.2f}" if quote.get("low") is not None else "n/a"
            if quote.get("low") is None:
                missing.append("low")
            vol = f"{int(quote['volume']):,}" if quote.get("volume") is not None else "n/a"
            if quote.get("volume") is None:
                missing.append("volume")
            data_block += f"High: {high} | Low: {low} | Volume: {vol}\n"

            source = quote.get("source", "unknown source")
            data_block += f"Price data source: {source}"
            if quote.get("tradetime"):
                data_block += f" (as of {quote['tradetime']})"
            data_block += "\n"

            if missing:
                data_block += f"Note: missing fields for this quote: {', '.join(missing)}\n"
        else:
            data_block += "Price data unavailable from all sources (API and sheet fallback)\n"

        if position:
            pos_parts = []
            if position.get("qty") is not None:
                pos_parts.append(f"{position['qty']:g} shares")
            if position.get("entry_price") is not None:
                pos_parts.append(f"entry ${position['entry_price']:.2f}")
            if position.get("unrealized_pct") is not None:
                sign = "+" if position["unrealized_pct"] >= 0 else ""
                pos_parts.append(f"{sign}{position['unrealized_pct']}% unrealized")
            if position.get("pct_of_portfolio") is not None:
                pos_parts.append(f"{position['pct_of_portfolio']}% of portfolio")
            if pos_parts:
                data_block += "Position: " + " | ".join(pos_parts) + "\n"
            else:
                data_block += "Position: held, but size/cost-basis data unavailable\n"

        journal_entry = trade_journal_index.get(ticker)
        if journal_entry:
            line = f"Most recent past trade: {journal_entry['date'] or 'date unknown'} — {journal_entry['outcome'] or 'outcome not recorded'}"
            if journal_entry["lesson"]:
                line += f" — {journal_entry['lesson']}"
            data_block += line + "\n"

        if news:
            data_block += "Recent news:\n"
            for article in news:
                source = f" [{article.get('source', '')}]" if article.get("source") else ""
                data_block += f"- {article['headline']}{source}\n"
                if article.get("summary"):
                    data_block += f"  {article['summary'][:200]}\n"
        else:
            data_block += "No recent news found.\n"

        if filings:
            data_block += "SEC EDGAR 8-K filings (past 48h):\n"
            for f in filings:
                data_block += f"- {f['form']} filed {f['filed']} — {f['entity']}"
                if f.get("description"):
                    data_block += f" ({f['description']})"
                data_block += "\n"
        else:
            data_block += "No recent EDGAR filings.\n"

    return data_block

# ── Data Coverage Tracking ────────────────────────────────────────────────────

def classify_quote_completeness(quote):
    """Classify a quote's completeness: 'none', 'partial', or 'full'."""
    if not quote:
        return "none"
    fields = ["close", "high", "low", "volume", "change_pct"]
    missing = [f for f in fields if quote.get(f) is None]
    return "partial" if missing else "full"

def compute_coverage_summary(portfolio):
    """Build a short data-coverage line summarizing quote completeness across a run."""
    full = partial = none = 0
    partial_tickers = []
    none_tickers = []

    for holding in portfolio:
        status = classify_quote_completeness(holding.get("quote"))
        if status == "full":
            full += 1
        elif status == "partial":
            partial += 1
            partial_tickers.append(holding["ticker"])
        else:
            none += 1
            none_tickers.append(holding["ticker"])

    total = full + partial + none
    line = f"Data coverage: {full}/{total} complete"
    if partial:
        line += f", {partial} partial ({', '.join(partial_tickers)})"
    if none:
        line += f", {none} no data ({', '.join(none_tickers)})"
    return line

# ── Signal Log (feedback loop) ────────────────────────────────────────────────
#
# Scope: Current Positions and Active Watchlist only. Monitoring, Reassess, and
# Long Positions are intentionally not logged — see project notes on avoiding
# diluted hit-rate numbers from lower-conviction tiers.
#
# Design: the digest prompts above are left untouched. A separate, cheap Claude
# call reads the digest's own prose after it's generated and extracts only the
# already-flagged tickers into structured form. This mirrors the extraction
# pattern already used in weekly_catalyst_scraper.py (extract_events_with_claude)
# rather than asking the main prompt to also emit JSON, which would risk
# leaking structured output into the emailed briefing.
#
# The log deliberately does not ask Claude to grade or score its own flags —
# it only records what was flagged and why, in Claude's own words. Judgment on
# whether a flag was "correct" belongs to a future scoring pass, not this one.

def extract_signals_with_claude(portfolio, summary_text, segment):
    """
    Extract structured signals from a digest's own prose for Signal Log purposes.
    segment must be "Current Position" or "Active".
    Returns a list of dicts: {"ticker", "signal_type", "trigger_basis", "reasoning_snippet"}
    """
    if not summary_text or not summary_text.strip():
        return []

    tickers = [h["ticker"] for h in portfolio]
    if not tickers:
        return []

    if segment == "Current Position":
        section_guidance = (
            'Only extract signals from the "EXIT FLAGS" section. Every extracted '
            'signal must use "signal_type": "EXIT FLAG". If that section says '
            'nothing was flagged, return [].'
        )
    else:  # "Active"
        section_guidance = (
            'Extract signals from two sections only. From "ENTRY OPPORTUNITIES", '
            'use "signal_type": "ENTRY OPPORTUNITY". From "RISK FLAGS", use '
            '"signal_type": "RISK FLAG". Ignore all other sections. If neither '
            'section flags anything, return [].'
        )

    client = Anthropic(api_key=ANTHROPIC_API_KEY)
    prompt = f"""Below is a daily briefing you just wrote for a retail investor's {segment} tickers: {', '.join(tickers)}.

{section_guidance}

For each qualifying flagged ticker, extract:
- "ticker": string, must be exactly one of: {', '.join(tickers)}
- "signal_type": string, exactly as specified above
- "trigger_basis": string, max 40 chars — the specific fact that tripped the flag (e.g. "8-K filed", "7% decline", "analyst upgrade"), not a restatement of the signal type
- "reasoning_snippet": string, max 150 chars — one-line summary in your own words of why this was flagged

Respond ONLY with a JSON array, no other text. If nothing qualifies, respond with [].

Briefing:
{summary_text}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        signals = json.loads(raw)
        if not isinstance(signals, list):
            return []
        valid_tickers = set(tickers)
        return [s for s in signals if isinstance(s, dict) and s.get("ticker") in valid_tickers]
    except Exception as e:
        print(f"  Warning: signal extraction failed for {segment}: {e}")
        return []

def _position_context_string(position):
    """Build the Position Context column string from position metrics, or '' if none."""
    if not position:
        return ""
    parts = []
    if position.get("unrealized_pct") is not None:
        sign = "+" if position["unrealized_pct"] >= 0 else ""
        parts.append(f"{sign}{position['unrealized_pct']}% unrealized")
    if position.get("pct_of_portfolio") is not None:
        parts.append(f"{position['pct_of_portfolio']}% of portfolio")
    return " | ".join(parts)

def build_signal_log_rows(portfolio, signals, segment, log_date, id_counters):
    """
    Convert extracted signals + portfolio data into Signal Log rows (columns A-O).
    id_counters is a dict shared across the whole run, used to keep Log IDs
    unique if the same ticker is flagged more than once on the same day.
    """
    quote_by_ticker    = {h["ticker"]: h.get("quote") for h in portfolio}
    position_by_ticker = {h["ticker"]: h.get("position") for h in portfolio}
    date_str = log_date.strftime("%Y-%m-%d")
    stamp    = log_date.strftime("%Y%m%d")

    rows = []
    for sig in signals:
        ticker = sig.get("ticker", "").strip().upper()
        if not ticker:
            continue

        n = id_counters.get(ticker, 0) + 1
        id_counters[ticker] = n
        log_id = f"{ticker}-{stamp}-{n}"

        quote = quote_by_ticker.get(ticker)
        price_at_flag = quote.get("close") if quote and quote.get("close") is not None else ""

        position_context = _position_context_string(position_by_ticker.get(ticker))

        rows.append([
            log_id,                                    # A Log ID
            date_str,                                   # B Date Flagged
            ticker,                                      # C Ticker
            segment,                                     # D Segment
            sig.get("signal_type", ""),                  # E Signal Type
            price_at_flag,                                # F Price at Flag
            sig.get("reasoning_snippet", "")[:150],       # G Reasoning Snippet
            sig.get("trigger_basis", "")[:40],            # H Trigger Basis
            position_context,                             # I Position Context
            False,                                        # J Resolved
            "", "", "",                                   # K/L/M Price +5D/+10D/+20D
            "",                                            # N Outcome
            "",                                            # O Notes
        ])
    return rows

def append_signal_log_rows(service, rows):
    """Append rows to the Signal Log tab. Append-only — never mutates existing rows."""
    if not rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SIGNAL_LOG_TAB}!{SIGNAL_LOG_RANGE}",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": rows}
    ).execute()

def log_signals_to_sheet(service, portfolio, summary_text, segment, id_counters):
    """Extract and append signals for one segment's digest run. Safe to call even if empty."""
    signals = extract_signals_with_claude(portfolio, summary_text, segment)
    if not signals:
        print(f"  No {segment} signals to log.")
        return
    rows = build_signal_log_rows(portfolio, signals, segment, datetime.utcnow(), id_counters)
    append_signal_log_rows(service, rows)
    print(f"  Logged {len(rows)} {segment} signal(s) to Signal Log.")

# ── Shared grounding instruction ──────────────────────────────────────────────

GROUNDING_INSTRUCTION = (
    "IMPORTANT: Only reference facts explicitly present in the data below. "
    "Do not infer, estimate, or generate any price, percentage, date, or news "
    "detail that is not directly stated. If data for a ticker is missing, "
    "incomplete, or unclear, say so plainly rather than filling the gap with "
    "a plausible-sounding assumption. It is always better to say \"insufficient "
    "data\" than to guess.\n\n"
    "Some price data comes directly from a live market data API (labeled "
    "'Massive/Polygon'), while other price data is sourced from the investor's "
    "own spreadsheet, which uses Google Finance formulas (labeled 'Google "
    "Finance (sheet)'). Both are legitimate current data sources. Where a "
    "tradetime is given for sheet-sourced data, treat that as the actual "
    "freshness indicator for that quote rather than assuming it is as current "
    "as an API-sourced quote. If a quote has missing fields noted, do not "
    "speculate about the missing values.\n\n"
      "Where a 'Position:' line is present, this is factual position data from "
    "the investor's own records: share count, entry price, unrealized gain/loss "
    "%, and % of total portfolio value. Treat this as ground truth and weigh it "
    "directly in your analysis — do not estimate or infer position size or cost "
    "basis for a holding where this line is absent.\n\n"
    "Where a 'Most recent past trade:' line is present, this is factual history "
    "from the investor's own trade journal for that exact ticker. Present it as "
    "neutral context alongside today's data — state what the outcome and lesson "
    "were, but do not speculate beyond what's written, and do not treat it as an "
    "implicit instruction to act a certain way today."
)

# Portfolio concentration threshold, shared across Portfolio and Long Positions
# prompts so "large %" / "outsized" mean the same thing in both emails and
# don't drift apart from separate edits over time.
CONCENTRATION_THRESHOLD_PCT = 20

OUTPUT_FORMAT_INSTRUCTION = (
    "FORMATTING: This response will be sent as a plain-text email and rendered "
    "exactly as written — it is NOT parsed as Markdown. Do not use Markdown "
    "syntax of any kind: no ## or # headers, no ** for bold, no _ for italics, "
    "no [text](link) formatting. For section headers, use ALL CAPS on their own "
    "line. For lists, use a plain hyphen (-) followed by a space. Keep formatting "
    "simple and readable as raw text."
)

# ── Claude Summary (Portfolio) ────────────────────────────────────────────────

def generate_summary(portfolio, failure_patterns_block="", trade_journal_index=None):
    """Generate exit-focused portfolio digest via Claude, weighing cost basis and position size."""
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio, trade_journal_index)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor manage a small hobby portfolio of micro-cap and speculative stocks. The investor's strategy is short holding periods with small positions, looking to exit as quickly as possible when negative signals appear.

Today is {today_str}. Below is the portfolio data including yesterday's price action, recent news, any SEC EDGAR 8-K filings from the past 48 hours, and position data (share count, entry price, unrealized gain/loss, and % of total portfolio) for each holding.

{GROUNDING_INSTRUCTION}
{OUTPUT_FORMAT_INSTRUCTION}
{failure_patterns_block}

{data_block}

POSITION-AWARE ANALYSIS: When evaluating exit signals, explicitly weigh position context, not just price/news signals in isolation:
- A decline on a position already at an unrealized loss is more urgent than the same decline on a position with a cushion of unrealized gains
- A decline on a holding that represents more than {CONCENTRATION_THRESHOLD_PCT}% of total portfolio value deserves more attention than the same decline on a smaller position
- Where position data is available, reference it directly (e.g. "this position is already down X% from entry" or "this is Y% of the total portfolio, the largest/one of the larger holdings")
- Where position data is missing for a holding, do not assume a size or cost basis — just proceed with signal-based analysis for that ticker

Write a focused daily portfolio briefing structured as follows:

1. EXIT FLAGS — This is the most important section. Lead with any holding that triggers one or more of the following:
   - Declined more than 7% in the prior session
   - Showing low volume or thin liquidity
   - Analyst downgrade or price target cut
   - Negative company-specific news: FDA rejection, clinical trial failure, earnings miss, insider selling, SEC filing concerns
   - Any material negative 8-K filing (e.g. going concern, restatement, adverse event, CRL)
   For each flagged holding, state clearly: what the signal is, why it matters, whether position context (unrealized P&L, portfolio weight) raises or lowers urgency, and whether an exit should be considered.

2. HOLDING-BY-HOLDING BREAKDOWN — For each position not already flagged for exit:
   - Price action and volume summary
   - Position context (unrealized P&L, % of portfolio) where available
   - Any company-specific news or EDGAR filings and what they mean for this holding
   - One-line bottom line: hold, watch, or investigate further

3. POSITIVE CATALYSTS — Briefly note any holdings with meaningful positive news or filings (analyst upgrades, FDA approvals, strong earnings, significant 8-K disclosures). Keep this section concise.

4. THINGS TO WATCH TODAY — 3-5 specific items relevant to holdings in this portfolio for today's session.

Be direct and actionable. Skip generic market commentary. If there is no news or filing for a holding say so in one line and move on. Focus on company-specific developments. The investor wants to know: should I exit anything today, and is there anything I need to act on?"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=5000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Claude Summary (Active Watchlist) ────────────────────────────────────────

def generate_active_watchlist_summary(portfolio, failure_patterns_block="", trade_journal_index=None):
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio, trade_journal_index)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor monitor their active watchlist of speculative micro-cap stocks. These are the investor's highest-conviction watchlist names — tickers they are actively considering entering in the near term. The investor takes small positions and looks for short-term catalysts.

Today is {today_str}. Below is data including yesterday's price action, recent news, and any SEC EDGAR 8-K filings from the past 48 hours for each ticker.

{GROUNDING_INSTRUCTION}
{OUTPUT_FORMAT_INSTRUCTION}
{failure_patterns_block}

{data_block}

Write a focused active watchlist briefing structured as follows:

1. ENTRY OPPORTUNITIES — Lead with any ticker showing a compelling near-term entry signal:
   - Positive catalyst: FDA approval or positive ruling, strong earnings, analyst upgrade or new coverage, significant partnership or licensing deal
   - Material positive 8-K filing (e.g. trial success, NDA acceptance, licensing agreement)
   - Unusual volume spike suggesting accumulation
   - Sharp pullback on no news that may represent a buying opportunity
   For each opportunity, state clearly: what the signal is, why it might represent a good entry, and what the key risk is.

2. TICKER-BY-TICKER BREAKDOWN — For each ticker not flagged as an entry opportunity:
   - Price action and volume summary
   - Any company-specific news or EDGAR filings and what they mean
   - One-line bottom line: enter, wait for better price, or avoid today

3. RISK FLAGS — Note any tickers showing warning signs that should be reconsidered or dropped:
   - Negative catalysts, FDA rejections, adverse 8-K filings, insider selling, deteriorating fundamentals

4. THINGS TO WATCH TODAY — 3-5 specific upcoming catalysts or events relevant to these tickers.

Be direct and actionable. Focus entirely on company-specific developments. The investor wants to know: is anything on this list worth entering today?"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=5000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Claude Summary (Monitoring Watchlist) ────────────────────────────────────

def generate_monitoring_watchlist_summary(portfolio, failure_patterns_block="", trade_journal_index=None):
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio, trade_journal_index)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor monitor their secondary watchlist of speculative micro-cap stocks. These tickers are being watched for developing signals — they are not yet ready for entry but could be elevated to the active watchlist if the right catalyst appears.

Today is {today_str}. Below is data including yesterday's price action, recent news, and any SEC EDGAR 8-K filings from the past 48 hours for each ticker.

{GROUNDING_INSTRUCTION}
{OUTPUT_FORMAT_INSTRUCTION}
{failure_patterns_block}

{data_block}

Write a focused monitoring watchlist briefing structured as follows:

1. ELEVATE TO ACTIVE — Flag any ticker that now shows a signal strong enough to warrant moving to the active watchlist for near-term entry consideration:
   - A clear near-term catalyst has emerged (FDA decision, trial readout, earnings, analyst initiation)
   - A positive 8-K filing with material implications
   - Unusual volume or price action suggesting something is developing
   For each flagged ticker, explain what changed and why it warrants closer attention.

2. TICKER-BY-TICKER BREAKDOWN — For each ticker not flagged for elevation:
   - Price action and volume summary
   - Any news or EDGAR filings and what they suggest about timing
   - One-line bottom line: developing, no change, or losing conviction

3. REMOVE FROM LIST — Note any tickers that have deteriorated to the point of no longer being worth monitoring:
   - Adverse events, failed trials, negative 8-K filings, or no meaningful catalyst on the horizon

4. THINGS TO WATCH — 3-5 upcoming events or filings that could change the status of tickers on this list.

Be concise and signal-focused. The investor wants to know: has anything here earned a promotion to the active watchlist?"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Claude Summary (Reassess Watchlist) ──────────────────────────────────────

def generate_reassess_watchlist_summary(portfolio, failure_patterns_block="", trade_journal_index=None):
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio, trade_journal_index)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor review their reassess watchlist — a list of speculative micro-cap stocks that have been flagged for reconsideration. These tickers have either underperformed expectations, lost a catalyst, or simply haven't moved. The investor needs to decide whether to keep watching, move them up, or drop them entirely.

Today is {today_str}. Below is data including yesterday's price action, recent news, and any SEC EDGAR 8-K filings from the past 48 hours for each ticker.

{GROUNDING_INSTRUCTION}
{OUTPUT_FORMAT_INSTRUCTION}
{failure_patterns_block}

{data_block}

Write a focused reassess watchlist briefing structured as follows:

1. DROP — Lead with any ticker that should be removed from all watchlists entirely:
   - No remaining near-term catalyst
   - Adverse news, failed trial, FDA rejection, or negative 8-K
   - Prolonged low volume with no signs of life
   For each, give a one-line rationale for dropping it.

2. KEEP WATCHING — Tickers that still have a reason to remain on the reassess list:
   - A pending catalyst that hasn't resolved yet
   - Price action or volume suggesting something may still develop
   For each, state what would need to happen to elevate it or drop it.

3. ELEVATE — Flag any ticker that has unexpectedly shown a positive signal worth promoting to the monitoring or active watchlist:
   - Surprise positive catalyst, strong 8-K, or analyst attention
   For each, explain what changed.

4. SUMMARY — A brief 2-3 sentence overall assessment of this tier: is the list getting cleaner or are there still names worth holding onto?

Be direct and unsentimental. The purpose of this list is to cut underperformers and recycle attention toward better opportunities."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Claude Summary (Long Positions) ──────────────────────────────────────────

def generate_long_positions_summary(portfolio, failure_patterns_block="", trade_journal_index=None):
    """Generate a thesis-level digest for long-term core holdings via Claude, weighing cost basis and position size."""
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio, trade_journal_index)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor manage a small core of long-term positions held for stability alongside a much more active, short-term speculative portfolio. These are NOT short-term trades. The investor's intended holding period for these names is months, potentially many months to years, not days or weeks.

Today is {today_str}. Below is data including yesterday's price action, recent news, any SEC EDGAR 8-K filings from the past 48 hours, and position data (share count, entry price, unrealized gain/loss, and % of total long-term portfolio) for each holding.

{GROUNDING_INSTRUCTION}
{OUTPUT_FORMAT_INSTRUCTION}
{failure_patterns_block}

CRITICAL FRAMING: Evaluate everything through a multi-month time horizon. Daily price movement, single-session volume spikes, and short-term volatility are NOT relevant to this briefing and should not be treated as action signals — mention price action only briefly for context, not as a headline concern. Do not suggest exiting a position due to a single bad day or a temporary dip. Instead, focus on developments that would change the long-term investment thesis:
- Material shifts in fundamentals (earnings trends, revenue/margin trajectory, guidance changes)
- Pipeline or product developments with multi-quarter implications
- Changes in competitive positioning or market share
- Balance sheet health, dilution risk, or capital structure changes
- Leadership or strategic direction changes
- Regulatory developments with long-term implications (not just near-term binary catalysts)

POSITION-AWARE ANALYSIS: Where position data is available, incorporate it into the thesis-level view — not as a trading signal, but as long-term risk context:
- Note unrealized gain/loss as an indicator of how the position has performed against the original long-term thesis, not as a reason to act on short-term movement
- Flag concentration risk explicitly if any single holding exceeds {CONCENTRATION_THRESHOLD_PCT}% of total long-term portfolio value
- Where position data is missing for a holding, do not assume a size or cost basis — proceed with thesis-level analysis for that ticker regardless

Write a focused long-term holdings briefing structured as follows:

1. THESIS-CHANGING DEVELOPMENTS — Lead with any holding where something has emerged that could meaningfully alter the long-term investment case, positive or negative. For each, explain what changed and why it matters for the multi-month-to-year outlook, not for tomorrow's trading session.

2. HOLDING-BY-HOLDING BREAKDOWN — For each position not already covered above:
   - Brief price context (one line, de-emphasized)
   - Position context (unrealized P&L, % of long-term portfolio) where available
   - Any fundamental or strategic news and what it means for the long-term thesis
   - One-line bottom line: thesis intact, thesis strengthening, or thesis warrants review

3. CONCENTRATION CHECK — Flag any single holding that exceeds {CONCENTRATION_THRESHOLD_PCT}% of the long-term portfolio as a concentration risk, based on the position data provided.

4. THINGS TO MONITOR — 3-5 items relevant to the long-term thesis of these holdings that may develop over the coming weeks to months (e.g. upcoming earnings, expected data readouts, sector trends). This is not a daily action list.

Be calm, measured, and long-horizon in tone — this briefing should read differently from a short-term trading digest. If there is nothing thesis-relevant for a holding, say so briefly and move on rather than manufacturing urgency."""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=3000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Email Delivery (Resend) ───────────────────────────────────────────────────

def _send_resend_email(subject, summary):
    """Send an email via Resend."""
    resend.api_key = RESEND_API_KEY

    html_body  = "<pre style='font-family: Georgia, serif; font-size: 15px; line-height: 1.6; max-width: 700px;'>"
    html_body += summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_body += "</pre>"

    try:
        resend.Emails.send({
            "from": "Portfolio Digest <onboarding@resend.dev>",
            "to": [RECIPIENT_EMAIL],
            "subject": subject,
            "html": html_body,
            "text": summary
        })
        print(f"Email '{subject}' sent successfully to {RECIPIENT_EMAIL}")
    except Exception as e:
        raise RuntimeError(f"Resend error: {e}")

def send_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_resend_email(f"Portfolio Digest — {today_str}", summary)

def send_active_watchlist_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_resend_email(f"Active Watchlist Digest — {today_str}", summary)

def send_monitoring_watchlist_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_resend_email(f"Monitoring Watchlist Digest — {today_str}", summary)

def send_reassess_watchlist_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_resend_email(f"Reassess Watchlist Digest — {today_str}", summary)

def send_long_positions_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_resend_email(f"Long-Term Holdings Digest — {today_str}", summary)

# ── Flow runners ───────────────────────────────────────────────────────────────

def run_daily_digest():
    """Run the standard weekday flow: portfolio + active + monitoring + reassess."""
    print("Starting portfolio digest...")
    failure_patterns_block = build_failure_patterns_block()

    # Shared across this run so a ticker flagged twice in one day still gets a
    # unique Log ID (e.g. TICKER-20260806-1, TICKER-20260806-2).
    signal_log_ids = {}
    sheet_service = _sheet_service() if SIGNAL_LOG_ENABLED else None

    # Built once per run — same read-once-then-lookup pattern as failure patterns
    trade_journal_index = build_trade_journal_index()

    # Portfolio
    if PORTFOLIO_ENABLED:
        ticker_rows = get_tickers_from_sheet()
        if ticker_rows:
            portfolio = build_portfolio_data(ticker_rows)
            print("Generating portfolio summary with Claude...")
            summary = generate_summary(portfolio, failure_patterns_block, trade_journal_index)
            coverage_line = compute_coverage_summary(portfolio)
            summary = f"{coverage_line}\n\n{summary}"
            print(f"  {coverage_line}")
            print("Sending portfolio email...")
            send_email(summary)
            if SIGNAL_LOG_ENABLED:
                try:
                    log_signals_to_sheet(sheet_service, portfolio, summary, "Current Position", signal_log_ids)
                except Exception as e:
                    print(f"  Warning: Signal Log write failed for Current Position: {e}")
        else:
            print("No portfolio tickers found, skipping.")
    else:
        print("Portfolio digest disabled via PORTFOLIO_ENABLED flag, skipping.")

    # Tier 1 — Active Watchlist
    if ACTIVE_WATCHLIST_ENABLED:
        active_rows = get_active_watchlist_from_sheet()
        if active_rows:
            active_portfolio = build_portfolio_data(active_rows)
            print("Generating active watchlist summary with Claude...")
            active_summary = generate_active_watchlist_summary(active_portfolio, failure_patterns_block, trade_journal_index)
            coverage_line = compute_coverage_summary(active_portfolio)
            active_summary = f"{coverage_line}\n\n{active_summary}"
            print(f"  {coverage_line}")
            print("Sending active watchlist email...")
            send_active_watchlist_email(active_summary)
            if SIGNAL_LOG_ENABLED:
                try:
                    log_signals_to_sheet(sheet_service, active_portfolio, active_summary, "Active", signal_log_ids)
                except Exception as e:
                    print(f"  Warning: Signal Log write failed for Active: {e}")
        else:
            print("No active watchlist tickers found, skipping.")
    else:
        print("Active watchlist digest disabled via ACTIVE_WATCHLIST_ENABLED flag, skipping.")

    # Tier 2 — Monitoring Watchlist
    if MONITORING_WATCHLIST_ENABLED:
        monitoring_rows = get_monitoring_watchlist_from_sheet()
        if monitoring_rows:
            monitoring_portfolio = build_portfolio_data(monitoring_rows)
            print("Generating monitoring watchlist summary with Claude...")
            monitoring_summary = generate_monitoring_watchlist_summary(monitoring_portfolio, failure_patterns_block, trade_journal_index)
            coverage_line = compute_coverage_summary(monitoring_portfolio)
            monitoring_summary = f"{coverage_line}\n\n{monitoring_summary}"
            print(f"  {coverage_line}")
            print("Sending monitoring watchlist email...")
            send_monitoring_watchlist_email(monitoring_summary)
        else:
            print("No monitoring watchlist tickers found, skipping.")
    else:
        print("Monitoring watchlist digest disabled via MONITORING_WATCHLIST_ENABLED flag, skipping.")

    # Tier 3 — Reassess Watchlist
    if REASSESS_ENABLED:
        reassess_rows = get_reassess_watchlist_from_sheet()
        if reassess_rows:
            reassess_portfolio = build_portfolio_data(reassess_rows)
            print("Generating reassess watchlist summary with Claude...")
            reassess_summary = generate_reassess_watchlist_summary(reassess_portfolio, failure_patterns_block, trade_journal_index)
            coverage_line = compute_coverage_summary(reassess_portfolio)
            reassess_summary = f"{coverage_line}\n\n{reassess_summary}"
            print(f"  {coverage_line}")
            print("Sending reassess watchlist email...")
            send_reassess_watchlist_email(reassess_summary)
        else:
            print("No reassess watchlist tickers found, skipping.")
    else:
        print("Reassess watchlist disabled via REASSESS_ENABLED flag, skipping.")

    print("Done.")

def run_long_positions_digest():
    """Run the Mon/Wed/Fri long-term holdings flow, independent of the daily flow."""
    print("Starting long positions digest...")

    if not LONG_POSITIONS_ENABLED:
        print("Long positions digest disabled via LONG_POSITIONS_ENABLED flag, skipping.")
        return
        
    failure_patterns_block = build_failure_patterns_block()
    trade_journal_index    = build_trade_journal_index()
    long_rows = get_long_positions_from_sheet()
    if long_rows:
        total_value = compute_total_value(long_rows)
        long_portfolio = build_portfolio_data(long_rows, total_value=total_value)
        print("Generating long positions summary with Claude...")
        long_summary = generate_long_positions_summary(long_portfolio, failure_patterns_block, trade_journal_index)
        coverage_line = compute_coverage_summary(long_portfolio)
        long_summary = f"{coverage_line}\n\n{long_summary}"
        print(f"  {coverage_line}")
        print("Sending long positions email...")
        send_long_positions_email(long_summary)
    else:
        print("No long position tickers found, skipping.")

    print("Done.")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if "--long-positions-only" in sys.argv:
        run_long_positions_digest()
    else:
        run_daily_digest()

if __name__ == "__main__":
    main()
