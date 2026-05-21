# portfolio-digest
I wanted to build a tool that would automatically pull my stock holdings from a spreadsheet and deliver a daily email summary of relevant news and price action before market open — I worked through the entire project collaboratively with Claude, using it not just for code generation but as a technical advisor throughout the process.

Architecture

The final pipeline has five components working together:

Google Sheets — serves as the input layer, where my ticker symbols live

Python orchestrator script — fetches data, calls APIs, and ties everything together

Massive (formerly Polygon.io) + Finnhub — provide daily price data and per-ticker news headlines

Claude API — receives all the raw data and generates a detailed, analytical digest

SendGrid — delivers the finished email each morning

GitHub Actions serves as the scheduler and host, running the script automatically on weekdays.

The Build Process

The project started with a feasibility conversation — Claude walked me through the architecture options before writing a single line of code, which helped me understand what I was committing to. Once I decided on the approach, Claude guided me through setting up five separate accounts and credential sets (Google Cloud, GitHub, Anthropic Console, SendGrid, and Finnhub/Massive), explaining each step as we went.

The core script came in at around 200 lines of Python across three files: the main script, a requirements file, and a GitHub Actions workflow YAML. Claude wrote all three, with the script designed around my specific sheet structure — column B of a tab called "Investing Dashboard," with an existing header row.

Troubleshooting

This is where the project got interesting. Several issues came up that required real debugging:

403 authentication error — the script was trying to use the Google Drive API to locate the spreadsheet by name, but the service account only had Sheets API scope. Fixed by hardcoding the spreadsheet ID directly and removing the Drive API dependency entirely.

Wrong data being read — the script was reading dollar values and dates instead of tickers, because it wasn't specifying the correct sheet tab. Fixed by adding a `SHEET_TAB` variable to the configuration and updating the range reference to `"Investing Dashboard!B2:B200"`.

GitHub Actions timing delays — the scheduled 7am delivery arrived at 10:40am on the first run, which turned out to be a well-documented platform limitation rather than a bug. Addressed by moving the scheduled time to 5am ET to build in a buffer.

Polygon.io rebrand — mid-project I noticed references to a service called Massive instead of Polygon.io. A quick check confirmed it was simply a rebrand with no API changes required.

What Claude's Role Actually Looked Like

Claude functioned less like an autocomplete tool and more like a senior developer sitting alongside me. It designed the architecture, wrote all the code, explained what each component was doing and why, diagnosed errors from raw log output, and suggested fixes. When I asked questions — like whether Editor vs. Viewer access mattered for the service account, or whether the API billed separately from my Claude.ai subscription — it answered them directly rather than just generating more code.

That said, the human judgment calls were mine: which approach to take, when to try a simpler fix before a more complex one, and how to structure my existing spreadsheet. The debugging loop in particular required back-and-forth — I'd share an error or a screenshot, Claude would interpret it and propose a fix, and I'd implement and report back.

End Result

A fully automated pipeline that reads my portfolio from Google Sheets every weekday morning and delivers a detailed pre-market briefing to my inbox — covering price action, news summaries, risk flags, and cross-portfolio themes — for roughly $1-3/month in API costs.

The most useful takeaway from a vibe coding perspective: Claude is most effective when you treat it as a collaborator you're explaining your actual situation to, rather than a search engine you're querying for code snippets. The more context I gave — showing screenshots, pasting exact error messages, describing my sheet structure — the faster and more accurately it could help.
