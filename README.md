# P-Card Auditor

Part IV of the Analytics Mindset P-card case study. A website internal auditors can use
to interrogate 489,178 Oklahoma State University purchasing-card transactions (2010–2014).

Two tabs:

- **Ask the database** — type an audit question in plain English. Google Gemini turns it
  into a SQLite query, the query runs against a read-only copy of the database, and the
  rows come back with the SQL shown and a short plain-English reading of the result.
- **Prohibited purchases** — a dashboard with instructions, a year selector, and two
  separate searches: one over the transaction description field, one over the vendor
  field. Fourteen buttons load ready-made keyword sets for each prohibited category.

## Protecting the API key

The key is read from the `GEMINI_API_KEY` environment variable and nothing else. It never
appears in the source, the templates, the page HTML, or the repository. `.env` is in
`.gitignore`. Copy `.env.example` to `.env` locally, and set the variable in the Render
dashboard for the deployed site.

## Running it locally (Windows Command Prompt)

```bat
cd path\to\pcard-auditor
py -3 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
set GEMINI_API_KEY=your-key-here
python app.py
```

Open http://127.0.0.1:5000. On the first run the app unpacks `data/pcards.db.gz` into
`data/pcards.db`, which takes a second or two and happens only once.

On macOS or Linux, use `export GEMINI_API_KEY=your-key-here` instead of `set`.

## Deploying to Render

1. Push this folder to GitHub. `data/pcards.db.gz` is about 23 MB, so it commits
   normally — no Git LFS needed.
2. On Render, create a **New Web Service** from the repository.
3. Runtime Python 3. Build command `pip install -r requirements.txt`. Start command:
   ```
   gunicorn app:app --bind 0.0.0.0:$PORT --workers 1 --threads 4 --timeout 120
   ```
4. Under **Environment**, add `GEMINI_API_KEY` with your key. Do not put it anywhere else.
5. Deploy. `/healthz` reports the row count and whether the key is present.

`render.yaml` holds the same settings if you prefer a blueprint deploy. The free tier
sleeps after inactivity, so the first request after a quiet spell takes 30–60 seconds.

## The database

`data/pcards.db.gz` is the case-study file with two changes, both made by
`scripts/build_db.py`:

- a `TxnDateISO` column holding `TransactionDate` as `YYYY-MM-DD`, because SQLite cannot
  sort or do date arithmetic on the original `M/D/YYYY 0:00:00` text;
- an index on `Year`.

Everything else is untouched, so queries written for the original schema still run. To
rebuild from the raw file:

```bat
python scripts\build_db.py path\to\pcards_1.db
```

## How the query tab stays safe

A model writing SQL against a live database needs guardrails. This app uses four:

1. The connection opens the file in read-only mode (`?mode=ro`).
2. A SQLite authorizer callback rejects every operation except reads.
3. Model output is parsed before it runs: markdown fences stripped, one statement only,
   must begin with `SELECT` or `WITH`, and any write or schema keyword is refused.
4. Results are capped at 500 rows.

If the generated query fails, the SQLite error is handed back to Gemini once for a
correction. If that also fails, the error and the attempted SQL are shown rather than
hidden.

## Model selection

The app asks the Gemini API which models the key can use and picks the first available
from its preference list, so it keeps working as Google retires models. Pin one with the
`GEMINI_MODEL` environment variable if you want a specific model.

## Files

```
app.py                 Flask app, Gemini calls, SQL guardrails, search endpoints
templates/index.html   Both tabs
static/style.css       Styling
static/app.js          Tabs, query flow, dashboard search, CSV export
scripts/build_db.py    Rebuilds data/pcards.db.gz from the raw case file
data/pcards.db.gz      Compressed database (unpacked on first boot)
render.yaml, Procfile  Deployment configuration
```

A flagged transaction is a lead for follow-up, not a finding. Merchant descriptions are
generic and many vendors sell both allowable and prohibited items, so every hit needs to
be confirmed against the receipt and the business purpose.
