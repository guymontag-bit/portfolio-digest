import os
import json
import time
import requests
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from anthropic import Anthropic

# ── Configuration ────────────────────────────────────────────────────────────

SPREADSHEET_ID          = os.environ["SPREADSHEET_ID"]
PORTFOLIO_TAB           = "Current Positions"
PORTFOLIO_RANGE         = "B2:B200"
ACTIVE_WATCHLIST_TAB    = "1 Active Watchlist"
MONITORING_WATCHLIST_TAB= "2 Monitoring Watchlist"
REASSESS_WATCHLIST_TAB  = "3 Reassess Watchlist"
WATCHLIST_RANGE         = "A2:A200"

RECIPIENT_EMAIL         = os.environ["RECIPIENT_EMAIL"]
SENDER_EMAIL            = os.environ["SENDER_EMAIL"]
ANTHROPIC_API_KEY       = os.environ["ANTHROPIC_API_KEY"]
SENDGRID_API_KEY        = os.environ["SENDGRID_API_KEY"]
FINNHUB_API_KEY         = os.environ["FINNHUB_API_KEY"]
POLYGON_API_KEY         = os.environ["POLYGON_API_KEY"]
REASSESS_ENABLED        = os.environ.get("REASSESS_ENABLED", "true").lower() == "true"

# ── Google Sheets ─────────────────────────────────────────────────────────────

def _sheet_service():
    """Return an authenticated Google Sheets service."""
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    return build("sheets", "v4", credentials=creds)

def _read_tickers(tab, cell_range):
    """Generic helper: read a deduplicated column of tickers from a named tab."""
    service = _sheet_service()
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{tab}!{cell_range}"
    ).execute()
    values = result.get("values", [])
    seen = set()
    tickers = []
    for row in values:
        if row and row[0].strip():
            t = row[0].strip().upper()
            if t not in seen:
                seen.add(t)
                tickers.append(t)
    return tickers

def get_tickers_from_sheet():
    """Read unique portfolio tickers from Current Positions tab, column B."""
    tickers = _read_tickers(PORTFOLIO_TAB, PORTFOLIO_RANGE)
    print(f"Found {len(tickers)} portfolio tickers: {', '.join(tickers)}")
    return tickers

def get_active_watchlist_from_sheet():
    """Read unique tickers from 1 Active Watchlist tab, column A."""
    tickers = _read_tickers(ACTIVE_WATCHLIST_TAB, WATCHLIST_RANGE)
    print(f"Found {len(tickers)} active watchlist tickers: {', '.join(tickers)}")
    return tickers

def get_monitoring_watchlist_from_sheet():
    """Read unique tickers from 2 Monitoring Watchlist tab, column A."""
    tickers = _read_tickers(MONITORING_WATCHLIST_TAB, WATCHLIST_RANGE)
    print(f"Found {len(tickers)} monitoring watchlist tickers: {', '.join(tickers)}")
    return tickers

def get_reassess_watchlist_from_sheet():
    """Read unique tickers from 3 Reassess Watchlist tab, column A."""
    tickers = _read_tickers(REASSESS_WATCHLIST_TAB, WATCHLIST_RANGE)
    print(f"Found {len(tickers)} reassess watchlist tickers: {', '.join(tickers)}")
    return tickers

# ── Market Data (Massive / Polygon) ──────────────────────────────────────────

def get_quote(ticker):
    """Fetch previous close price and daily change from Massive/Polygon."""
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
                "change_pct": round(change_pct, 2)
            }
    except Exception as e:
        print(f"Warning: Could not fetch quote for {ticker}: {e}")
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

def build_portfolio_data(tickers):
    """Fetch quotes, news, and EDGAR filings for all tickers."""
    portfolio = []
    for ticker in tickers:
        print(f"Fetching data for {ticker}...")
        quote   = get_quote(ticker)
        news    = get_news(ticker)
        filings = get_edgar_filings(ticker)
        time.sleep(0.25)  # gentle rate limiting
        portfolio.append({
            "ticker":  ticker,
            "quote":   quote,
            "news":    news,
            "filings": filings
        })
    return portfolio

# ── Data Block Builder ────────────────────────────────────────────────────────

def build_data_block(portfolio):
    """Build a readable text block from portfolio data for Claude prompts."""
    data_block = ""
    for holding in portfolio:
        ticker  = holding["ticker"]
        quote   = holding["quote"]
        news    = holding["news"]
        filings = holding.get("filings", [])

        data_block += f"\n## {ticker}\n"

        if quote:
            direction   = "▲" if quote["change_pct"] >= 0 else "▼"
            data_block += (
                f"Price: ${quote['close']:.2f} "
                f"{direction} {abs(quote['change_pct'])}% yesterday\n"
                f"High: ${quote['high']:.2f} | Low: ${quote['low']:.2f} | "
                f"Volume: {int(quote['volume']):,}\n"
            )
        else:
            data_block += "Price data unavailable\n"

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

# ── Claude Summary (Portfolio) ────────────────────────────────────────────────

def generate_summary(portfolio):
    """Generate exit-focused portfolio digest via Claude."""
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor manage a small hobby portfolio of micro-cap and speculative stocks. The investor's strategy is short holding periods with small positions, looking to exit as quickly as possible when negative signals appear.

Today is {today_str}. Below is the portfolio data including yesterday's price action, recent news, and any SEC EDGAR 8-K filings from the past 48 hours for each holding.

{data_block}

Write a focused daily portfolio briefing structured as follows:

1. EXIT FLAGS — This is the most important section. Lead with any holding that triggers one or more of the following:
   - Declined more than 7% in the prior session
   - Showing low volume or thin liquidity
   - Analyst downgrade or price target cut
   - Negative company-specific news: FDA rejection, clinical trial failure, earnings miss, insider selling, SEC filing concerns
   - Any material negative 8-K filing (e.g. going concern, restatement, adverse event, CRL)
   For each flagged holding, state clearly: what the signal is, why it matters, and whether it suggests an exit should be considered.

2. HOLDING-BY-HOLDING BREAKDOWN — For each position not already flagged for exit:
   - Price action and volume summary
   - Any company-specific news or EDGAR filings and what they mean for this holding
   - One-line bottom line: hold, watch, or investigate further

3. POSITIVE CATALYSTS — Briefly note any holdings with meaningful positive news or filings (analyst upgrades, FDA approvals, strong earnings, significant 8-K disclosures). Keep this section concise.

4. THINGS TO WATCH TODAY — 3-5 specific items relevant to holdings in this portfolio for today's session.

Be direct and actionable. Skip generic market commentary. If there is no news or filing for a holding say so in one line and move on. Focus on company-specific developments. The investor wants to know: should I exit anything today, and is there anything I need to act on?"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Claude Summary (Active Watchlist) ────────────────────────────────────────

def generate_active_watchlist_summary(portfolio):
    """Generate entry-focused digest for Tier 1 active watchlist via Claude."""
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor monitor their active watchlist of speculative micro-cap stocks. These are the investor's highest-conviction watchlist names — tickers they are actively considering entering in the near term. The investor takes small positions and looks for short-term catalysts.

Today is {today_str}. Below is data including yesterday's price action, recent news, and any SEC EDGAR 8-K filings from the past 48 hours for each ticker.

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
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Claude Summary (Monitoring Watchlist) ────────────────────────────────────

def generate_monitoring_watchlist_summary(portfolio):
    """Generate signal-watching digest for Tier 2 monitoring watchlist via Claude."""
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor monitor their secondary watchlist of speculative micro-cap stocks. These tickers are being watched for developing signals — they are not yet ready for entry but could be elevated to the active watchlist if the right catalyst appears.

Today is {today_str}. Below is data including yesterday's price action, recent news, and any SEC EDGAR 8-K filings from the past 48 hours for each ticker.

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
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Claude Summary (Reassess Watchlist) ──────────────────────────────────────

def generate_reassess_watchlist_summary(portfolio):
    """Generate keep-or-cut digest for Tier 3 reassess watchlist via Claude."""
    client     = Anthropic(api_key=ANTHROPIC_API_KEY)
    data_block = build_data_block(portfolio)
    today_str  = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor review their reassess watchlist — a list of speculative micro-cap stocks that have been flagged for reconsideration. These tickers have either underperformed expectations, lost a catalyst, or simply haven't moved. The investor needs to decide whether to keep watching, move them up, or drop them entirely.

Today is {today_str}. Below is data including yesterday's price action, recent news, and any SEC EDGAR 8-K filings from the past 48 hours for each ticker.

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
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Email Delivery (SendGrid) ─────────────────────────────────────────────────

def _send_sendgrid_email(subject, summary):
    """Send an email via SendGrid."""
    html_body  = "<pre style='font-family: Georgia, serif; font-size: 15px; line-height: 1.6; max-width: 700px;'>"
    html_body += summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_body += "</pre>"

    payload = {
        "personalizations": [{"to": [{"email": RECIPIENT_EMAIL}]}],
        "from":    {"email": SENDER_EMAIL},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": summary},
            {"type": "text/html",  "value": html_body}
        ]
    }
    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type":  "application/json"
    }
    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers=headers,
        json=payload,
        timeout=15
    )
    if r.status_code in (200, 202):
        print(f"Email '{subject}' sent successfully to {RECIPIENT_EMAIL}")
    else:
        raise RuntimeError(f"SendGrid error {r.status_code}: {r.text}")

def send_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_sendgrid_email(f"Portfolio Digest — {today_str}", summary)

def send_active_watchlist_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_sendgrid_email(f"Active Watchlist Digest — {today_str}", summary)

def send_monitoring_watchlist_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_sendgrid_email(f"Monitoring Watchlist Digest — {today_str}", summary)

def send_reassess_watchlist_email(summary):
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    _send_sendgrid_email(f"Reassess Watchlist Digest — {today_str}", summary)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Starting portfolio digest...")

    # Portfolio
    tickers = get_tickers_from_sheet()
    if tickers:
        portfolio = build_portfolio_data(tickers)
        print("Generating portfolio summary with Claude...")
        summary = generate_summary(portfolio)
        print("Sending portfolio email...")
        send_email(summary)
    else:
        print("No portfolio tickers found, skipping.")

    # Tier 1 — Active Watchlist
    active_tickers = get_active_watchlist_from_sheet()
    if active_tickers:
        active_portfolio = build_portfolio_data(active_tickers)
        print("Generating active watchlist summary with Claude...")
        active_summary = generate_active_watchlist_summary(active_portfolio)
        print("Sending active watchlist email...")
        send_active_watchlist_email(active_summary)
    else:
        print("No active watchlist tickers found, skipping.")

    # Tier 2 — Monitoring Watchlist
    monitoring_tickers = get_monitoring_watchlist_from_sheet()
    if monitoring_tickers:
        monitoring_portfolio = build_portfolio_data(monitoring_tickers)
        print("Generating monitoring watchlist summary with Claude...")
        monitoring_summary = generate_monitoring_watchlist_summary(monitoring_portfolio)
        print("Sending monitoring watchlist email...")
        send_monitoring_watchlist_email(monitoring_summary)
    else:
        print("No monitoring watchlist tickers found, skipping.")

    # Tier 3 — Reassess Watchlist
    if REASSESS_ENABLED:
    reassess_tickers = get_reassess_watchlist_from_sheet()
        if reassess_tickers:
            reassess_portfolio = build_portfolio_data(reassess_tickers)
            print("Generating reassess watchlist summary with Claude...")
            reassess_summary = generate_reassess_watchlist_summary(reassess_portfolio)
            print("Sending reassess watchlist email...")
            send_reassess_watchlist_email(reassess_summary)
        else:
            print("No reassess watchlist tickers found, skipping.")
    else:
        print("Reassess watchlist disabled via REASSESS_ENABLED flag, skipping.")

    print("Done.")

if __name__ == "__main__":
    main()
