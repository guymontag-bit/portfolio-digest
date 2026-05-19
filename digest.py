import os
import json
import requests
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from anthropic import Anthropic

# ── Configuration ────────────────────────────────────────────────────────────

SPREADSHEET_ID = "14M4QR3kLWd40T-N-iIPgw-utWgbfTluWbhP5-O4_n9A"
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
        range="B2:B200"
    ).execute()
    values = result.get("values", [])
    tickers = [row[0].strip().upper() for row in values if row and row[0].strip()]
    print(f"Found {len(tickers)} tickers: {', '.join(tickers)}")
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

    prompt = f"""You are a sharp, experienced financial analyst preparing a pre-market morning briefing for a portfolio investor.

Today is {today_str}. Below is the portfolio data including yesterday's price action and recent news for each holding.

{data_block}

Write a detailed daily portfolio digest with the following structure:

1. MARKET OVERVIEW — A 2-3 sentence big-picture summary of what's happening across the portfolio today.

2. HOLDING-BY-HOLDING BREAKDOWN — For each ticker:
   - Price action summary (what moved and by how much)
   - What the news means for this specific holding
   - Any risks, opportunities, or items to watch
   - A one-line "bottom line" assessment

3. CROSS-PORTFOLIO FLAGS — Note any themes, risks, or macro events that affect multiple holdings simultaneously (e.g. sector moves, interest rate news, geopolitical events).

4. THINGS TO WATCH TODAY — 3-5 bullet points of specific items to monitor during today's trading session.

Be direct, specific, and analytical. Avoid generic filler. If there is no meaningful news for a holding, say so briefly and move on. Use plain text formatting suitable for email."""

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

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Starting portfolio digest...")
    tickers = get_tickers_from_sheet()
    if not tickers:
        print("No tickers found in sheet. Exiting.")
        return
    portfolio = build_portfolio_data(tickers)
    print("Generating summary with Claude...")
    summary = generate_summary(portfolio)
    print("Sending email...")
    send_email(summary)
    print("Done.")

if __name__ == "__main__":
    main()
