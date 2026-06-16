import os
import time
import json
import requests
from datetime import datetime, timedelta
from google.oauth2 import service_account
from googleapiclient.discovery import build
import anthropic
import sendgrid
from sendgrid.helpers.mail import Mail

# ── Configuration ────────────────────────────────────────────────────────────

SPREADSHEET_ID = os.environ["SPREADSHEET_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
SENDGRID_API_KEY = os.environ["SENDGRID_API_KEY"]
SENDER_EMAIL = os.environ["SENDER_EMAIL"]
RECIPIENT_EMAIL = os.environ["RECIPIENT_EMAIL"]

EDGAR_HEADERS = {
    "User-Agent": "Benjamin Sweeney thetaindigo.bs@gmail.com",
    "Accept-Encoding": "gzip, deflate",
}

EDGAR_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"
EDGAR_TERMS = ["PDUFA", "target action date", "topline data", "advisory committee", "Phase 3 results"]

SOURCE_SHEET = "Investing Dashboard"
SOURCE_RANGE = f"{SOURCE_SHEET}!B:J"  # B = ticker, J = status

ACTIVE_STATUSES = ["Active", "Active - Watch", "Monitoring - Watch"]

EVENTS_SHEET = "Upcoming Events"
EVENTS_HEADERS = ["Ticker", "Event Type", "Event Date", "Notes", "Source Filing", "Date Added"]

LOOKBACK_DAYS = 8  # cover full week plus buffer

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


# ── EDGAR search ─────────────────────────────────────────────────────────────

def edgar_search(ticker, term, lookback_days=LOOKBACK_DAYS, max_retries=3):
    """Search EDGAR full-text for a ticker + term in recent 8-K filings."""
    since_date = (datetime.utcnow() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    params = {
        "q": f'"{term}"',
        "forms": "8-K",
        "ticker": ticker,
        "dateRange": "custom",
        "startdt": since_date,
    }
    for attempt in range(max_retries):
        try:
            response = requests.get(
                EDGAR_SEARCH_URL,
                params=params,
                headers=EDGAR_HEADERS,
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            elif response.status_code in [429, 503]:
                wait = 2 ** attempt
                print(f"  Rate limited on {ticker} / '{term}', waiting {wait}s...")
                time.sleep(wait)
            else:
                print(f"  EDGAR error {response.status_code} for {ticker} / '{term}'")
                return None
        except Exception as e:
            print(f"  EDGAR exception for {ticker} / '{term}': {e}")
            time.sleep(2 ** attempt)
    return None


def collect_snippets(ticker):
    """Run all search terms for a ticker and collect unique filing snippets."""
    snippets = {}
    for term in EDGAR_TERMS:
        result = edgar_search(ticker, term)
        if result and result.get("hits", {}).get("hits"):
            for hit in result["hits"]["hits"]:
                accession = hit.get("_id", "")
                if accession not in snippets:
                    entity = hit.get("_source", {})
                    snippet = entity.get("file_date", "") + " | " + entity.get("period_of_report", "") + " | " + str(entity.get("entity_name", ""))
                    # include any text excerpt if available
                    highlight = hit.get("highlight", {})
                    for key, val in highlight.items():
                        if val:
                            snippet += " | " + " ".join(val)
                    snippets[accession] = {
                        "accession": accession,
                        "snippet": snippet,
                        "filing_date": entity.get("file_date", "")
                    }
        time.sleep(1)  # polite crawl between term searches
    return list(snippets.values())


# ── Claude extraction ─────────────────────────────────────────────────────────

def extract_events_with_claude(ticker, snippets):
    """Pass filing snippets to Claude and extract structured future catalyst events."""
    if not snippets:
        return []

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    combined = "\n\n".join([
        f"Filing {i+1} (accession: {s['accession']}, filed: {s['filing_date']}):\n{s['snippet']}"
        for i, s in enumerate(snippets)
    ])

    prompt = f"""You are analyzing SEC 8-K filing excerpts for the stock ticker {ticker}.

Your task is to extract any FUTURE binary catalyst events mentioned in these filings.

ONLY extract events that:
- Have a specific date or date range in the future (after today {datetime.utcnow().strftime('%Y-%m-%d')})
- Are binary catalysts: PDUFA dates, FDA advisory committee meetings, Phase 2/3 topline data readouts, NDA/BLA submissions, or similar regulatory milestones
- Are clearly associated with ticker {ticker}

Do NOT extract:
- Past events or historical references
- Vague language without a date ("data expected eventually")
- Non-catalyst events (earnings, general updates)

Respond ONLY with a JSON array. Each element should have these exact keys:
- "ticker": string (always {ticker})
- "event_type": string (e.g. "PDUFA", "Phase 3 Readout", "FDA Advisory Committee", "NDA Submission")
- "event_date": string in YYYY-MM-DD format, or "Q1 2026" style if only quarter is mentioned
- "notes": string (brief description, max 100 chars)
- "source_filing": string (accession number)

If no qualifying future events are found, respond with an empty array: []

Do not include any text outside the JSON array.

Filing excerpts:
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
    # existing_rows[0] is headers, data starts at index 1
    data_rows = existing_rows[1:] if len(existing_rows) > 1 else []

    new_events = []
    changed_events = []

    for event in extracted_events:
        ticker = event.get("ticker", "")
        event_type = event.get("event_type", "")
        event_date = event.get("event_date", "")
        notes = event.get("notes", "")
        source = event.get("source_filing", "")

        matched = False
        for i, row in enumerate(data_rows):
            existing_ticker = row[0] if len(row) > 0 else ""
            existing_type = row[1] if len(row) > 1 else ""
            existing_date = row[2] if len(row) > 2 else ""

            if existing_ticker == ticker and existing_type == event_type:
                matched = True
                if existing_date != event_date:
                    # date has changed — flag as update
                    changed_events.append((i, existing_date, event_date, ticker, event_type))
                break  # found match, stop searching

        if not matched:
            # genuinely new event
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

    message = Mail(
        from_email=SENDER_EMAIL,
        to_emails=RECIPIENT_EMAIL,
        subject=f"Catalyst Calendar Update — {datetime.utcnow().strftime('%b %d, %Y')}",
        html_content=body
    )
    sg = sendgrid.SendGridAPIClient(api_key=SENDGRID_API_KEY)
    sg.send(message)
    print("Summary email sent.")


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
        snippets = collect_snippets(ticker)

        if not snippets:
            print(f"  No relevant filings found for {ticker}")
            time.sleep(1)
            continue

        print(f"  Found {len(snippets)} filing(s), extracting events...")
        extracted = extract_events_with_claude(ticker, snippets)

        if extracted:
            tickers_with_results.append(ticker)
            new_events, changed_events = check_against_existing(extracted, existing_rows)
            all_new_events.extend(new_events)
            all_changed_events.extend(changed_events)
            print(f"  {len(new_events)} new, {len(changed_events)} changed")
        else:
            print(f"  No qualifying future events extracted for {ticker}")

        time.sleep(2)  # polite delay between tickers

    # 4. Write new events to sheet
    if all_new_events:
        append_events(service, all_new_events)
        print(f"Appended {len(all_new_events)} new event(s) to Upcoming Events tab.")

    # 5. Update changed dates
    if all_changed_events:
        # re-fetch existing rows after appending new ones
        updated_existing = get_existing_events(service)
        for (row_index, old_date, new_date, ticker, event_type) in all_changed_events:
            update_event_date(service, updated_existing, row_index, new_date)
            print(f"Updated {ticker} {event_type}: {old_date} → {new_date}")

    # 6. Send summary email
    send_summary_email(all_new_events, all_changed_events, tickers, tickers_with_results)

    print("Weekly catalyst scraper complete.")


if __name__ == "__main__":
    main()
