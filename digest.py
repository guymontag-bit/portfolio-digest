import os
import json
import requests
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from anthropic import Anthropic

# ── Configuration ────────────────────────────────────────────────────────────

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
SHEET_TAB = "Investing Dashboard"
WATCHLIST_TAB = "Watchlist"
WATCHLIST_RANGE = "A2:A200"
TICKER_COLUMN = "B"
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]

ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]
POLYGON_API_KEY = os.environ["POLYGON_API_KEY"]

# ── Google Sheets ─────────────────────────────────────────────────────────────

def get_tickers_from_sheet():
    """Read ticker symbols from column B of the Investing Dashboard sheet."""
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    service = build("sheets", "v4", credentials=creds)

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{SHEET_TAB}!B2:B200"
    ).execute()
    values = result.get("values", [])
    tickers = [row[0].strip().upper() for row in values if row and row[0].strip()]
    print(f"Found {len(tickers)} tickers: {', '.join(tickers)}")
    return tickers
def get_watchlist_from_sheet():
    """Read ticker symbols from column A of the Watchlist sheet."""
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets.readonly"]
    )
    service = build("sheets", "v4", credentials=creds)

    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{WATCHLIST_TAB}!{WATCHLIST_RANGE}"
    ).execute()
    values = result.get("values", [])
    tickers = [row[0].strip().upper() for row in values if row and row[0].strip()]
    print(f"Found {len(tickers)} watchlist tickers: {', '.join(tickers)}")
    return tickers

# ── Market Data (Polygon.io) ──────────────────────────────────────────────────

def get_quote(ticker):
    """Fetch previous close price and daily change from Polygon.io."""
    url = f"https://api.polygon.io/v2/aggs/ticker/{ticker}/prev"
    params = {"adjusted": "true", "apiKey": POLYGON_API_KEY}
    try:
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultsCount", 0) > 0:
            result = data["results"][0]
            close = result["c"]
            open_price = result["o"]
            change_pct = ((close - open_price) / open_price) * 100
            return {
                "close": close,
                "open": open_price,
                "high": result["h"],
                "low": result["l"],
                "volume": result["v"],
                "change_pct": round(change_pct, 2)
            }
    except Exception as e:
        print(f"Warning: Could not fetch quote for {ticker}: {e}")
    return None

# ── News (Finnhub) ────────────────────────────────────────────────────────────

def get_news(ticker):
    """Fetch last 24 hours of news for a ticker from Finnhub."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    yesterday = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": ticker,
        "from": yesterday,
        "to": today,
        "token": FINNHUB_API_KEY
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        articles = r.json()
        # Return top 5 most recent headlines and summaries
        return [
            {"headline": a["headline"], "summary": a.get("summary", "")}
            for a in articles[:5]
            if a.get("headline")
        ]
    except Exception as e:
        print(f"Warning: Could not fetch news for {ticker}: {e}")
    return []

# ── Data Assembly ─────────────────────────────────────────────────────────────

def build_portfolio_data(tickers):
    """Fetch quotes and news for all tickers."""
    portfolio = []
    for ticker in tickers:
        print(f"Fetching data for {ticker}...")
        quote = get_quote(ticker)
        news = get_news(ticker)
        portfolio.append({
            "ticker": ticker,
            "quote": quote,
            "news": news
        })
    return portfolio

# ── Claude Summary ────────────────────────────────────────────────────────────

def generate_summary(portfolio):
    """Send portfolio data to Claude and get a detailed digest."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    # Build a readable data block for the prompt
    data_block = ""
    for holding in portfolio:
        ticker = holding["ticker"]
        quote = holding["quote"]
        news = holding["news"]

        data_block += f"\n## {ticker}\n"
        if quote:
            direction = "▲" if quote["change_pct"] >= 0 else "▼"
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
                data_block += f"- {article['headline']}\n"
                if article["summary"]:
                    data_block += f"  {article['summary'][:200]}\n"
        else:
            data_block += "No recent news found.\n"

    today_str = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor manage a small hobby portfolio of micro-cap and speculative stocks. The investor's strategy is short holding periods with small positions, looking to exit as quickly as possible when negative signals appear.

Today is {today_str}. Below is the portfolio data including yesterday's price action and recent news for each holding.

{data_block}

Write a focused daily portfolio briefing structured as follows:

1. EXIT FLAGS — This is the most important section. Lead with any holding that triggers one or more of the following:
   - Declined more than 7% in the prior session
   - Showing low volume or thin liquidity (flag if volume is unusually low)
   - Analyst downgrade or price target cut
   - Negative company-specific news: FDA rejection, clinical trial failure, earnings miss, insider selling, SEC filing concerns, or any other material negative catalyst
   For each flagged holding, state clearly: what the signal is, why it matters, and whether it suggests an exit should be considered.

2. HOLDING-BY-HOLDING BREAKDOWN — For each position not already flagged for exit:
   - Price action and volume summary
   - Any company-specific news and what it means for this holding specifically
   - One-line bottom line: hold, watch, or investigate further

3. POSITIVE CATALYSTS — Briefly note any holdings with meaningful positive news (analyst upgrades, FDA approvals, strong earnings, significant partnerships). Keep this section concise.

4. THINGS TO WATCH TODAY — 3-5 specific items relevant to holdings in this portfolio for today's session.

Be direct and actionable. Skip generic market commentary. If there is no news for a holding say so in one line and move on. Focus on company-specific developments over macro themes. The investor wants to know: should I exit anything today, and is there anything I need to act on?"""
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

def generate_watchlist_summary(portfolio):
    """Send watchlist data to Claude and get an entry-focused digest."""
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    data_block = ""
    for holding in portfolio:
        ticker = holding["ticker"]
        quote = holding["quote"]
        news = holding["news"]

        data_block += f"\n## {ticker}\n"
        if quote:
            direction = "▲" if quote["change_pct"] >= 0 else "▼"
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
                data_block += f"- {article['headline']}\n"
                if article["summary"]:
                    data_block += f"  {article['summary'][:200]}\n"
        else:
            data_block += "No recent news found.\n"

    today_str = datetime.utcnow().strftime("%A, %B %d, %Y")

    prompt = f"""You are a trading assistant helping a retail investor monitor a watchlist of speculative micro-cap stocks for potential entry opportunities. The investor takes small positions and looks for short-term catalysts.

Today is {today_str}. Below is watchlist data including yesterday's price action and recent news for each ticker.

{data_block}

Write a focused watchlist briefing structured as follows:

1. ENTRY OPPORTUNITIES — Lead with any ticker showing a compelling near-term entry signal:
   - Positive catalyst: FDA approval or positive ruling, strong earnings, analyst upgrade or new coverage, significant partnership or licensing deal
   - Unusual volume spike suggesting accumulation
   - Sharp pullback on no news that may represent a buying opportunity
   For each opportunity, state clearly: what the signal is, why it might represent a good entry, and what the key risk is.

2. TICKER-BY-TICKER BREAKDOWN — For each ticker not flagged as an entry opportunity:
   - Price action and volume summary
   - Any company-specific news and what it means
   - One-line bottom line: watching, not yet, or avoid

3. RISK FLAGS — Note any watchlist tickers showing warning signs that should be removed from consideration:
   - Negative catalysts, deteriorating fundamentals, FDA rejections, insider selling
   - Tickers that appear to have no near-term catalyst worth waiting for

4. THINGS TO WATCH TODAY — 3-5 specific upcoming catalysts or events relevant to watchlist tickers.

Be direct and actionable. Focus entirely on company-specific developments. The investor wants to know: is anything on my watchlist worth entering today?"""

    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": prompt}]
    )
    return message.content[0].text

# ── Email Delivery (SendGrid) ─────────────────────────────────────────────────

def send_email(summary):
    """Send the digest via SendGrid."""
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    subject = f"Portfolio Digest — {today_str}"

    # Convert plain text to simple HTML for email readability
    html_body = "<pre style='font-family: Georgia, serif; font-size: 15px; line-height: 1.6; max-width: 700px;'>"
    html_body += summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_body += "</pre>"

    payload = {
        "personalizations": [{"to": [{"email": RECIPIENT_EMAIL}]}],
        "from": {"email": SENDER_EMAIL},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": summary},
            {"type": "text/html", "value": html_body}
        ]
    }

    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json"
    }

    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers=headers,
        json=payload,
        timeout=15
    )

    if r.status_code in (200, 202):
        print(f"Email sent successfully to {RECIPIENT_EMAIL}")
    else:
        raise RuntimeError(f"SendGrid error {r.status_code}: {r.text}")

def send_watchlist_email(summary):
    """Send the watchlist digest via SendGrid."""
    today_str = datetime.utcnow().strftime("%B %d, %Y")
    subject = f"Watchlist Digest — {today_str}"

    html_body = "<pre style='font-family: Georgia, serif; font-size: 15px; line-height: 1.6; max-width: 700px;'>"
    html_body += summary.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    html_body += "</pre>"

    payload = {
        "personalizations": [{"to": [{"email": RECIPIENT_EMAIL}]}],
        "from": {"email": SENDER_EMAIL},
        "subject": subject,
        "content": [
            {"type": "text/plain", "value": summary},
            {"type": "text/html", "value": html_body}
        ]
    }

    headers = {
        "Authorization": f"Bearer {SENDGRID_API_KEY}",
        "Content-Type": "application/json"
    }

    r = requests.post(
        "https://api.sendgrid.com/v3/mail/send",
        headers=headers,
        json=payload,
        timeout=15
    )

    if r.status_code in (200, 202):
        print(f"Watchlist email sent successfully to {RECIPIENT_EMAIL}")
    else:
        raise RuntimeError(f"SendGrid error {r.status_code}: {r.text}")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Starting portfolio digest...")
    
    # Portfolio digest
    tickers = get_tickers_from_sheet()
    if tickers:
        portfolio = build_portfolio_data(tickers)
        print("Generating portfolio summary with Claude...")
        summary = generate_summary(portfolio)
        print("Sending portfolio email...")
        send_email(summary)
    else:
        print("No portfolio tickers found, skipping.")

    # Watchlist digest
    watchlist_tickers = get_watchlist_from_sheet()
    if watchlist_tickers:
        watchlist_portfolio = build_portfolio_data(watchlist_tickers)
        print("Generating watchlist summary with Claude...")
        watchlist_summary = generate_watchlist_summary(watchlist_portfolio)
        print("Sending watchlist email...")
        send_watchlist_email(watchlist_summary)
    else:
        print("No watchlist tickers found, skipping.")

    print("Done.")

if __name__ == "__main__":
    main()
