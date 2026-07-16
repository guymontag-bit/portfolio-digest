import os
import time
import json
import requests
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
from anthropic import Anthropic
import resend

# ── Configuration ────────────────────────────────────────────────────────────

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]
FINNHUB_API_KEY = os.environ["FINNHUB_API_KEY"]

SOURCE_SHEET = "Investing Dashboard"
SOURCE_RANGE = f"{SOURCE_SHEET}!B:J"  # B = ticker, J = status

ACTIVE_STATUSES = ["Active", "Active - Watch", "Monitoring - Watch"]

EVENTS_SHEET = "Upcoming Events"
EVENTS_HEADERS = ["Ticker", "Event Type", "Event Date", "Notes", "Source", "Date Added"]

LOOKBACK_DAYS = 15  # generous overlap between weekly runs to reduce coverage gaps

# ── Google Sheets helpers ─────────────────────────────────────────────────────

def get_sheets_service():
    creds_json = json.loads(os.environ["GOOGLE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(
        creds_json,
        scopes=["https://www.googleapis.com/auth/spreadsheets"]
    )
    return build("sheets", "v4", credentials=creds)


def get_active_tickers(service):
    """Pull tickers from Investing Dashboard where column J matches active statuses."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=SOURCE_RANGE
    ).execute()

    rows = result.get("values", [])
    tickers = []
    for row in rows[1:]:  # skip header
        if len(row) >= 9:  # need at least columns B through J
            ticker = row[0].strip().upper() if row[0] else ""
            status = row[8].strip() if row[8] else ""  # column J is index 8 relative to B
            if ticker and status in ACTIVE_STATUSES:
                tickers.append(ticker)

    return list(dict.fromkeys(tickers))  # deduplicate, preserve order


def get_existing_events(service):
    """Pull existing rows from Upcoming Events tab for dedup checking."""
    result = service.spreadsheets().values().get(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{EVENTS_SHEET}!A:F"
    ).execute()
    return result.get("values", [])


def ensure_events_headers(service, existing_rows):
    """Write headers to Upcoming Events tab if not already present."""
    if not existing_rows or existing_rows[0] != EVENTS_HEADERS:
        service.spreadsheets().values().update(
            spreadsheetId=SPREADSHEET_ID,
            range=f"{EVENTS_SHEET}!A1",
            valueInputOption="RAW",
            body={"values": [EVENTS_HEADERS]}
        ).execute()


def append_events(service, new_rows):
    """Append new event rows to the Upcoming Events tab."""
    if not new_rows:
        return
    service.spreadsheets().values().append(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{EVENTS_SHEET}!A:F",
        valueInputOption="RAW",
        insertDataOption="INSERT_ROWS",
        body={"values": new_rows}
    ).execute()


def update_event_date(service, existing_rows, row_index, new_date):
    """Update the Event Date cell for a changed event (1-indexed, accounting for header)."""
    sheet_row = row_index + 2  # +1 for header, +1 for 1-indexing
    service.spreadsheets().values().update(
        spreadsheetId=SPREADSHEET_ID,
        range=f"{EVENTS_SHEET}!C{sheet_row}",
        valueInputOption="RAW",
        body={"values": [[new_date]]}
    ).execute()


# ── News sources (Finnhub primary, Google News RSS fallback) ─────────────────

def get_finnhub_news(ticker, lookback_days=LOOKBACK_DAYS):
    """Fetch recent news for a ticker from Finnhub, scoped to the lookback window."""
    today = datetime.utcnow().strftime("%Y-%m-%d")
    since = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    url = "https://finnhub.io/api/v1/company-news"
    params = {
        "symbol": ticker,
        "from": since,
        "to": today,
        "token": FINNHUB_API_KEY
    }
    try:
        r = requests.get(url, params=params, timeout=10)
        articles = r.json()
        if not isinstance(articles, list):
            return []
        return [
            {
                "headline": a.get("headline", ""),
                "summary": a.get("summary", ""),
                "source": a.get("source", "Finnhub"),
                "url": a.get("url", ""),
                "datetime": a.get("datetime", "")
            }
            for a in articles[:10]
            if a.get("headline")
        ]
    except Exception as e:
        print(f"  Warning: Finnhub news fetch failed for {ticker}: {e}")
        return []


def get_google_news(ticker):
    """Fallback: fetch recent headlines from Google News RSS for a ticker."""
    import xml.etree.ElementTree as ET
    url = f"https://news.google.com/rss/search?q={ticker}+stock&hl=en-US&gl=US&ceid=US:en"
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        root = ET.fromstring(r.content)
        items = root.findall(".//item")[:10]
        articles = []
        for item in items:
            headline = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            pub_date = item.findtext("pubDate", "").strip()
            if headline:
                articles.append({
                    "headline": headline,
                    "summary": "",
                    "source": "Google News",
                    "url": link,
                    "datetime": pub_date
                })
        return articles
    except Exception as e:
        print(f"  Warning: Google News fetch failed for {ticker}: {e}")
        return []


def get_news_for_ticker(ticker):
    """Return Finnhub news; fall back to Google News RSS if Finnhub returns nothing."""
    articles = get_finnhub_news(ticker)
    if not articles:
        print(f"  No Finnhub news for {ticker}, trying Google News RSS...")
        articles = get_google_news(ticker)
    return articles


# ── Claude extraction ─────────────────────────────────────────────────────────

def extract_events_with_claude(ticker, articles):
    """Pass news articles to Claude and extract structured future catalyst events."""
    if not articles:
        return []

    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    combined = "\n\n".join([
        f"Article {i+1} (source: {a['source']}, published: {a.get('datetime', 'unknown')}):\n"
        f"Headline: {a['headline']}\n"
        f"Summary: {a.get('summary', '(none)')}"
        for i, a in enumerate(articles)
    ])

    today_str = datetime.utcnow().strftime("%Y-%m-%d")

    prompt = f"""You are analyzing recent news articles about the stock ticker {ticker}.

Your task is to extract any FUTURE binary catalyst events mentioned in these articles.

ONLY extract events that:
- Have a specific date, date range, or quarter mentioned that is in the future (after today {today_str})
- Are binary catalysts: PDUFA dates, FDA advisory committee meetings, Phase 2/3 topline data readouts, NDA/BLA submissions or decisions, earnings with material guidance, or similar regulatory/clinical milestones
- Are clearly associated with ticker {ticker}

Do NOT extract:
- Past events or historical references (e.g. "following last month's approval...")
- Vague language without any date or timeframe ("data expected eventually")
- Routine news with no specific future date attached
- Speculation or analyst opinion pieces with no company-disclosed date

Respond ONLY with a JSON array. Each element should have these exact keys:
- "ticker": string (always {ticker})
- "event_type": string (e.g. "PDUFA", "Phase 3 Readout", "FDA Advisory Committee", "NDA Submission", "Earnings")
- "event_date": string in YYYY-MM-DD format if a specific date is given, otherwise "Q1 2026" style if only a quarter/timeframe is mentioned
- "notes": string (brief description, max 100 chars)
- "source": string (the news source name, e.g. "Finnhub" or the publication name)

If no qualifying future events are found, respond with an empty array: []

Do not include any text outside the JSON array.

News articles:
{combined}"""

    try:
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = response.content[0].text.strip()
        raw = raw.replace("```json", "").replace("```", "").strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  Claude extraction error for {ticker}: {e}")
        return []


# ── Dedup logic ───────────────────────────────────────────────────────────────

def check_against_existing(extracted_events, existing_rows):
    """
    Compare extracted events against existing Upcoming Events rows.
    Returns:
      - new_events: list of rows to append
      - changed_events: list of (row_index, old_date, new_date, ticker, event_type) tuples
    """
    data_rows = existing_rows[1:] if len(existing_rows) > 1 else []

    new_events = []
    changed_events = []

    for event in extracted_events:
        ticker = event.get("ticker", "")
        event_type = event.get("event_type", "")
        event_date = event.get("event_date", "")
        notes = event.get("notes", "")
        source = event.get("source", "")

        matched = False
        for i, row in enumerate(data_rows):
            existing_ticker = row[0] if len(row) > 0 else ""
            existing_type = row[1] if len(row) > 1 else ""
            existing_date = row[2] if len(row) > 2 else ""

            if existing_ticker == ticker and existing_type == event_type:
                matched = True
                if existing_date != event_date:
                    changed_events.append((i, existing_date, event_date, ticker, event_type))
                break

        if not matched:
            new_events.append([
                ticker,
                event_type,
                event_date,
                notes,
                source,
                datetime.utcnow().strftime("%Y-%m-%d")
            ])

    return new_events, changed_events


# ── Email summary ─────────────────────────────────────────────────────────────

def send_summary_email(new_events, changed_events, tickers_processed, tickers_with_results):
    """Send a brief summary email of what changed in the Upcoming Events tab."""
    if not new_events and not changed_events:
        body = f"""<h2>Weekly Catalyst Calendar Update</h2>
<p>Run completed: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
<p>Tickers scanned: {len(tickers_processed)} ({', '.join(tickers_processed)})</p>
<p>No new or changed catalyst events detected this week.</p>"""
    else:
        new_section = ""
        if new_events:
            rows = "".join([
                f"<tr><td>{r[0]}</td><td>{r[1]}</td><td>{r[2]}</td><td>{r[3]}</td></tr>"
                for r in new_events
            ])
            new_section = f"""<h3>New Events Added ({len(new_events)})</h3>
<table border='1' cellpadding='5' style='border-collapse:collapse'>
<tr><th>Ticker</th><th>Event Type</th><th>Date</th><th>Notes</th></tr>
{rows}
</table>"""

        changed_section = ""
        if changed_events:
            rows = "".join([
                f"<tr><td>{e[3]}</td><td>{e[4]}</td><td>{e[1]}</td><td>{e[2]}</td></tr>"
                for e in changed_events
            ])
            changed_section = f"""<h3>Date Changes Detected ({len(changed_events)})</h3>
<table border='1' cellpadding='5' style='border-collapse:collapse'>
<tr><th>Ticker</th><th>Event Type</th><th>Old Date</th><th>New Date</th></tr>
{rows}
</table>"""

        body = f"""<h2>Weekly Catalyst Calendar Update</h2>
<p>Run completed: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
<p>Tickers scanned: {len(tickers_processed)} | With results: {len(tickers_with_results)}</p>
{new_section}
{changed_section}
<p><em>All events written directly to the Upcoming Events tab in your Investing Dashboard.</em></p>"""

    resend.api_key = RESEND_API_KEY
    try:
        resend.Emails.send({
            "from": "Portfolio Digest <onboarding@resend.dev>",
            "to": [RECIPIENT_EMAIL],
            "subject": f"Catalyst Calendar Update — {datetime.utcnow().strftime('%b %d, %Y')}",
            "html": body
        })
        print("Summary email sent.")
    except Exception as e:
        raise RuntimeError(f"Resend error: {e}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"Weekly catalyst scraper starting — {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")

    service = get_sheets_service()

    # 1. Get active tickers
    tickers = get_active_tickers(service)
    print(f"Active tickers found: {tickers}")

    # 2. Get existing events for dedup
    existing_rows = get_existing_events(service)
    ensure_events_headers(service, existing_rows)

    all_new_events = []
    all_changed_events = []
    tickers_with_results = []

    # 3. Process each ticker
    for ticker in tickers:
        print(f"Processing {ticker}...")
        articles = get_news_for_ticker(ticker)

        if not articles:
            print(f"  No recent news found for {ticker}")
            time.sleep(0.5)
            continue

        print(f"  Found {len(articles)} article(s), extracting events...")
        extracted = extract_events_with_claude(ticker, articles)

        if extracted:
            tickers_with_results.append(ticker)
            new_events, changed_events = check_against_existing(extracted, existing_rows)
            all_new_events.extend(new_events)
            all_changed_events.extend(changed_events)
            print(f"  {len(new_events)} new, {len(changed_events)} changed")
        else:
            print(f"  No qualifying future events extracted for {ticker}")

        time.sleep(0.5)  # light delay between tickers - news APIs are less rate-sensitive than EDGAR

    # 4. Write new events to sheet
    if all_new_events:
        append_events(service, all_new_events)
        print(f"Appended {len(all_new_events)} new event(s) to Upcoming Events tab.")

    # 5. Update changed dates
    if all_changed_events:
        updated_existing = get_existing_events(service)
        for (row_index, old_date, new_date, ticker, event_type) in all_changed_events:
            update_event_date(service, updated_existing, row_index, new_date)
            print(f"Updated {ticker} {event_type}: {old_date} → {new_date}")

    # 6. Send summary email
    send_summary_email(all_new_events, all_changed_events, tickers, tickers_with_results)

    print("Weekly catalyst scraper complete.")


if __name__ == "__main__":
    main()
