"""
OSU P-Card Auditor — Part IV of the Analytics Mindset P-card case study.

Two tools in one site:
  1. Ask the database in plain English (Google Gemini writes the SQLite query).
  2. A prohibited-purchase dashboard with description and vendor keyword search.

The Gemini API key is read from the GEMINI_API_KEY environment variable only.
It is never written into the source, the templates, or the repository.
"""

import csv
import gzip
import io
import json
import os
import re
import shutil
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
DB_GZ = BASE_DIR / "data" / "pcards.db.gz"
DB_PATH = Path(os.environ.get("DB_PATH", BASE_DIR / "data" / "pcards.db"))

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Preference order. The first model the account can actually use is picked at
# runtime, so the app keeps working as Google retires and adds models.
MODEL_PREFERENCE = [
    os.environ.get("GEMINI_MODEL", "").strip(),
    "gemini-2.5-flash",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
    "gemini-2.5-flash-lite",
]

MAX_ROWS = 500          # hard ceiling on rows returned to the browser
QUERY_TIMEOUT_S = 25    # wall clock budget for one Gemini call

app = Flask(__name__)

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------

_db_lock = threading.Lock()

TABLE_SCHEMA = """
CREATE TABLE pcards (
  Year                   INTEGER,  -- 2010-2014
  Month                  INTEGER,  -- 1-12
  FullName               TEXT,     -- anonymised cardholder, e.g. 'Employee 5433B965'
  ID                     INTEGER,  -- row identifier
  AgencyNumber           INTEGER,
  AgencyName             TEXT,     -- always 'OKLAHOMA STATE UNIVERSITY'
  CardholderLastName     TEXT,
  CardholderFirstInitial TEXT,
  Description            TEXT,     -- merchant-supplied description, e.g. 'GENERAL PURCHASE'
  Amount                 REAL,     -- negative values are refunds/credits
  Vendor                 TEXT,     -- merchant name, upper case
  TransactionDate        TEXT,     -- original format 'M/D/YYYY 0:00:00'
  PostedDate             TEXT,     -- original format 'M/D/YYYY 0:00:00'
  MCC                    TEXT,     -- merchant category code description, e.g. 'HARDWARE STORES'
  TxnDateISO             TEXT      -- TransactionDate as 'YYYY-MM-DD' (added for easy date maths)
);
"""


def ensure_database() -> None:
    """Unpack the compressed database on first boot if it is not there yet."""
    if DB_PATH.exists():
        return
    if not DB_GZ.exists():
        raise FileNotFoundError(
            f"Neither {DB_PATH} nor {DB_GZ} exists. Run scripts/build_db.py first."
        )
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DB_PATH.with_suffix(".tmp")
    app.logger.info("Unpacking %s -> %s", DB_GZ.name, DB_PATH)
    with gzip.open(DB_GZ, "rb") as src, open(tmp, "wb") as dst:
        shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)
    tmp.replace(DB_PATH)


def connect():
    """Read-only connection. Nothing in this app ever needs to write."""
    ensure_database()
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def _authorizer(action, arg1, arg2, db_name, trigger):
    """Second line of defence: the SQLite engine itself refuses writes."""
    allowed = {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
        sqlite3.SQLITE_RECURSIVE,
    }
    return sqlite3.SQLITE_OK if action in allowed else sqlite3.SQLITE_DENY


def run_select(sql, params=(), limit=MAX_ROWS):
    con = connect()
    try:
        con.set_authorizer(_authorizer)
        cur = con.execute(sql, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(limit)
        truncated = len(cur.fetchmany(1)) > 0
        return cols, [list(r) for r in rows], truncated
    finally:
        con.close()


def available_years():
    cols, rows, _ = run_select("SELECT DISTINCT Year FROM pcards ORDER BY Year DESC")
    return [r[0] for r in rows]


# --------------------------------------------------------------------------
# SQL safety net for model-written queries
# --------------------------------------------------------------------------

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|detach|vacuum|"
    r"pragma|reindex|analyze|begin|commit|rollback)\b",
    re.IGNORECASE,
)


class UnsafeSQL(Exception):
    pass


def sanitise_sql(sql: str) -> str:
    """Accept a single read-only SELECT/WITH statement and nothing else."""
    sql = sql.strip()
    # Strip markdown fences the model sometimes adds despite instructions.
    fence = re.match(r"^```(?:sql|sqlite)?\s*(.*?)\s*```$", sql, re.DOTALL | re.IGNORECASE)
    if fence:
        sql = fence.group(1).strip()
    sql = sql.rstrip().rstrip(";").strip()

    if not sql:
        raise UnsafeSQL("The model returned an empty query.")
    if ";" in sql:
        raise UnsafeSQL("Only one statement can be run at a time.")
    if not re.match(r"^(select|with)\b", sql, re.IGNORECASE):
        raise UnsafeSQL("Only SELECT queries are allowed. This one starts with something else.")
    if FORBIDDEN.search(sql):
        raise UnsafeSQL("The query contains a keyword that could change the data, so it was blocked.")

    # Cap the result size unless the model already set a tighter limit.
    if not re.search(r"\blimit\s+\d+\s*$", sql, re.IGNORECASE):
        sql = f"{sql}\nLIMIT {MAX_ROWS}"
    return sql


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------

_model_cache = {"name": None}


class GeminiError(Exception):
    pass


def _http_json(url, payload=None, timeout=QUERY_TIMEOUT_S):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST" if data else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:400]
        if exc.code in (401, 403):
            raise GeminiError("Gemini rejected the API key. Check GEMINI_API_KEY.") from exc
        if exc.code == 429:
            raise GeminiError("Gemini rate limit reached. Wait a moment and ask again.") from exc
        raise GeminiError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise GeminiError(f"Could not reach Gemini: {exc.reason}") from exc


def pick_model() -> str:
    """Choose a model this API key can actually call, then remember it."""
    if _model_cache["name"]:
        return _model_cache["name"]
    wanted = [m for m in MODEL_PREFERENCE if m]
    try:
        listing = _http_json(f"{GEMINI_API_BASE}/models?pageSize=200", timeout=15)
        usable = {
            m["name"].split("/")[-1]
            for m in listing.get("models", [])
            if "generateContent" in m.get("supportedGenerationMethods", [])
        }
    except GeminiError:
        usable = set()
    for name in wanted:
        if not usable or name in usable:
            _model_cache["name"] = name
            return name
    flash = sorted(n for n in usable if "flash" in n and "tts" not in n and "image" not in n)
    if flash:
        _model_cache["name"] = flash[0]
        return flash[0]
    raise GeminiError("No Gemini model on this key supports generateContent.")


def gemini(prompt: str, system: str = "", temperature: float = 0.0) -> str:
    if not GEMINI_API_KEY:
        raise GeminiError(
            "No Gemini API key is configured. Set GEMINI_API_KEY in the environment "
            "and restart the app."
        )
    model = pick_model()
    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 1200},
    }
    if system:
        payload["systemInstruction"] = {"parts": [{"text": system}]}
    data = _http_json(f"{GEMINI_API_BASE}/models/{model}:generateContent", payload)

    candidates = data.get("candidates") or []
    if not candidates:
        blocked = (data.get("promptFeedback") or {}).get("blockReason")
        raise GeminiError(f"Gemini returned no answer{f' ({blocked})' if blocked else ''}.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    text = "".join(p.get("text", "") for p in parts).strip()
    if not text:
        raise GeminiError("Gemini returned an empty answer. Try rewording the question.")
    return text


SQL_SYSTEM = f"""You translate an internal auditor's question into ONE SQLite SELECT query.

The database has exactly one table:
{TABLE_SCHEMA}

Rules:
- Return ONLY the SQL. No markdown fences, no commentary, no trailing semicolon.
- Read-only: SELECT or WITH ... SELECT. Never INSERT/UPDATE/DELETE/DROP/PRAGMA/ATTACH.
- Use TxnDateISO ('YYYY-MM-DD') for anything involving dates, day grouping or
  date arithmetic. Use Year and Month for calendar filters.
- The audit covers calendar year 2014 unless the auditor names another year, so
  add `WHERE Year = 2014` when no year is given.
- Amount is REAL; negative values are refunds or credits. Exclude them with
  `Amount > 0` for spending tests unless the question is about refunds.
- Keyword matching on Description, Vendor and MCC must be case-insensitive:
  use `UPPER(col) LIKE '%WORD%'`.
- Round money with ROUND(SUM(Amount), 2) and alias output columns in plain
  English, e.g. `AS "Total spent"`.
- Always add an ORDER BY that matches the question, and a LIMIT of at most {MAX_ROWS}.
- Cardholder identity is FullName. AgencyName is the same for every row, so
  never filter on it.
"""

EXPLAIN_SYSTEM = """You are an internal audit assistant summarising a query result for an
auditor at Oklahoma State University.

Write 2-4 short sentences in plain English. Say what the result shows and what the
auditor should look at next. Flagged rows are potential exceptions that need
follow-up, never proof of fraud or of a violation, so never state that a violation
or fraud occurred. No markdown headings, no bullet lists, no preamble.
"""


# --------------------------------------------------------------------------
# Prohibited-purchase presets (Part IV)
# --------------------------------------------------------------------------

PROHIBITED = [
    {"label": "Alcohol", "description": ["ALCOHOL", "LIQUOR", "WINE", "BEER", "SPIRITS", "BAR "],
     "vendor": ["LIQUOR", "WINE", "SPIRITS", "BREWER", "PACKAGE STORE"]},
    {"label": "Cash, cash advances and ATM", "description": ["CASH", "ATM", "ADVANCE", "WITHDRAWAL"],
     "vendor": ["ATM", "CASH ADVANCE", "MONEY"]},
    {"label": "Decorations", "description": ["DECOR", "DECORATION", "BALLOON", "PARTY", "ORNAMENT"],
     "vendor": ["PARTY", "HOBBY LOBBY", "FLORAL", "FLOWER"]},
    {"label": "Donations and sponsorships", "description": ["DONAT", "SPONSOR", "CONTRIBUTION", "CHARIT"],
     "vendor": ["FOUNDATION", "CHARIT", "UNITED WAY"]},
    {"label": "Gasoline", "description": ["GAS", "FUEL", "GASOLINE", "DIESEL"],
     "vendor": ["SHELL", "EXXON", "PHILLIPS 66", "QUIKTRIP", "LOVE'S", "CONOCO", "CIRCLE K"]},
    {"label": "Gifts, gift cards and certificates", "description": ["GIFT", "GIFT CARD", "CERTIFICATE"],
     "vendor": ["GIFT", "HALLMARK"]},
    {"label": "Insurance", "description": ["INSURANCE", "PREMIUM", "COVERAGE"],
     "vendor": ["INSURANCE", "ASSURANCE", "UNDERWRIT"]},
    {"label": "Late fees", "description": ["LATE FEE", "LATE CHARGE", "FINANCE CHARGE", "PENALTY", "INTEREST"],
     "vendor": ["COLLECTION"]},
    {"label": "Mail and postage", "description": ["POSTAGE", "STAMPS", "SHIPPING", "MAILING", "COURIER"],
     "vendor": ["USPS", "POST OFFICE", "POSTAL", "FEDEX", "UPS ", "STAMPS.COM", "PITNEY"]},
    {"label": "Moving expenses", "description": ["MOVING", "RELOCATION", "MOVER", "VAN LINES"],
     "vendor": ["MOVING", "VAN LINES", "U-HAUL", "PENSKE", "TWO MEN AND A TRUCK"]},
    {"label": "Personal purchases", "description": ["PERSONAL", "CLOTHING", "GROCER", "COSMETIC", "JEWEL"],
     "vendor": ["WAL-MART", "TARGET", "AMAZON", "COSTCO", "SAM'S CLUB", "BEST BUY"]},
    {"label": "Personal and individual memberships", "description": ["MEMBERSHIP", "DUES", "SUBSCRIPTION", "RENEWAL"],
     "vendor": ["CLUB", "GYM", "FITNESS", "ASSOCIATION", "SOCIETY"]},
    {"label": "Salaries, wages and benefits", "description": ["SALARY", "WAGES", "PAYROLL", "BENEFIT", "STIPEND", "BONUS"],
     "vendor": ["PAYROLL", "STAFFING", "TEMP"]},
    {"label": "Service and incentive awards", "description": ["AWARD", "PLAQUE", "TROPHY", "ENGRAV", "RECOGNITION", "INCENTIVE"],
     "vendor": ["TROPHY", "AWARDS", "ENGRAV", "PROMOTIONAL"]},
]

RESULT_COLUMNS = [
    "TransactionDate", "PostedDate", "FullName", "Vendor",
    "Description", "MCC", "Amount", "Year", "Month",
]


# --------------------------------------------------------------------------
# Routes
# --------------------------------------------------------------------------

@app.route("/")
def index():
    return render_template(
        "index.html",
        years=available_years(),
        categories=PROHIBITED,
        has_key=bool(GEMINI_API_KEY),
    )


@app.route("/api/ask", methods=["POST"])
def api_ask():
    question = (request.json or {}).get("question", "").strip()
    if not question:
        return jsonify(error="Type a question first."), 400
    if len(question) > 800:
        return jsonify(error="That question is too long. Keep it under 800 characters."), 400

    started = time.time()
    try:
        raw_sql = gemini(f"Auditor's question: {question}\n\nSQLite query:", SQL_SYSTEM)
        sql = sanitise_sql(raw_sql)
    except (GeminiError, UnsafeSQL) as exc:
        return jsonify(error=str(exc)), 502

    try:
        cols, rows, truncated = run_select(sql)
    except sqlite3.Error as exc:
        # One repair attempt: hand the engine's own error back to the model.
        try:
            fixed = gemini(
                f"This SQLite query failed.\n\nQuery:\n{sql}\n\nSQLite error: {exc}\n\n"
                f"Auditor's question: {question}\n\nReturn the corrected query only:",
                SQL_SYSTEM,
            )
            sql = sanitise_sql(fixed)
            cols, rows, truncated = run_select(sql)
        except (GeminiError, UnsafeSQL, sqlite3.Error) as exc2:
            return jsonify(error=f"The query could not be run: {exc2}", sql=sql), 400

    summary = None
    try:
        preview = json.dumps({"columns": cols, "rows": rows[:15]}, default=str)[:4000]
        summary = gemini(
            f"Auditor's question: {question}\n\nQuery that was run:\n{sql}\n\n"
            f"Rows returned: {len(rows)}{'+ (truncated)' if truncated else ''}\n"
            f"First rows as JSON:\n{preview}\n\nSummary:",
            EXPLAIN_SYSTEM,
            temperature=0.2,
        )
    except GeminiError:
        pass  # the table is the answer; the summary is a bonus

    return jsonify(
        sql=sql,
        columns=cols,
        rows=rows,
        summary=summary,
        truncated=truncated,
        elapsed=round(time.time() - started, 1),
        model=_model_cache["name"],
    )


@app.route("/api/search", methods=["POST"])
def api_search():
    body = request.json or {}
    field = body.get("field")
    year = body.get("year")
    keywords = [k.strip().upper() for k in (body.get("keywords") or []) if k and k.strip()]

    if field not in ("Description", "Vendor"):
        return jsonify(error="Choose either the description search or the vendor search."), 400
    if not keywords:
        return jsonify(error="Enter a keyword to search for."), 400

    where, params = [], []
    if year and str(year).lower() != "all":
        where.append("Year = ?")
        params.append(int(year))
    clause = " OR ".join([f"UPPER({field}) LIKE ?"] * len(keywords))
    where.append(f"({clause})")
    params.extend(f"%{k}%" for k in keywords)

    sql = (
        f"SELECT {', '.join(RESULT_COLUMNS)} FROM pcards "
        f"WHERE {' AND '.join(where)} "
        f"ORDER BY Amount DESC, TxnDateISO"
    )
    cols, rows, truncated = run_select(sql, params)

    total_sql = f"SELECT COUNT(*), ROUND(SUM(Amount), 2) FROM pcards WHERE {' AND '.join(where)}"
    _, totals, _ = run_select(total_sql, params)
    count, total = (totals[0] if totals else (0, 0))

    return jsonify(
        columns=cols, rows=rows, truncated=truncated,
        count=count or 0, total=total or 0, sql=sql, keywords=keywords,
    )


@app.route("/api/export", methods=["POST"])
def api_export():
    body = request.json or {}
    cols = body.get("columns") or []
    rows = body.get("rows") or []
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(cols)
    writer.writerows(rows)
    return (
        buf.getvalue(),
        200,
        {
            "Content-Type": "text/csv; charset=utf-8",
            "Content-Disposition": 'attachment; filename="pcard-audit-results.csv"',
        },
    )


@app.route("/healthz")
def healthz():
    try:
        _, rows, _ = run_select("SELECT COUNT(*) FROM pcards")
        return jsonify(status="ok", rows=rows[0][0], gemini_key=bool(GEMINI_API_KEY))
    except Exception as exc:  # noqa: BLE001 - health check reports whatever broke
        return jsonify(status="error", detail=str(exc)), 500


if __name__ == "__main__":
    ensure_database()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)
