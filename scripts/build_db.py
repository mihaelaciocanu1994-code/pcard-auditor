"""
Turn the raw case-study database into the file the site ships with.

    python scripts/build_db.py path/to/pcards_1.db

What it does:
  1. Copies the raw database.
  2. Adds TxnDateISO ('YYYY-MM-DD'), parsed from TransactionDate ('M/D/YYYY 0:00:00'),
     because SQLite cannot sort or subtract the original format.
  3. Indexes Year, which every dashboard query filters on.
  4. VACUUMs, then gzips to data/pcards.db.gz (about 24 MB, small enough to commit
     to GitHub without Git LFS).

app.py unpacks the .gz on first boot, so only the compressed file belongs in the repo.
"""

import gzip
import shutil
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_DB = ROOT / "data" / "pcards.db"
OUT_GZ = ROOT / "data" / "pcards.db.gz"


def to_iso(value):
    """'7/26/2014 0:00:00' -> '2014-07-26'"""
    if not value:
        return None
    try:
        month, day, year = value.split(" ")[0].split("/")
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    except (ValueError, AttributeError):
        return None


def main(source: Path) -> None:
    OUT_DB.parent.mkdir(parents=True, exist_ok=True)
    print(f"Copying {source} -> {OUT_DB}")
    shutil.copy(source, OUT_DB)

    con = sqlite3.connect(OUT_DB)
    cols = {r[1] for r in con.execute("PRAGMA table_info(pcards)")}

    if "TxnDateISO" not in cols:
        print("Adding TxnDateISO")
        con.execute("ALTER TABLE pcards ADD COLUMN TxnDateISO TEXT")
        rows = con.execute("SELECT rowid, TransactionDate FROM pcards").fetchall()
        con.executemany(
            "UPDATE pcards SET TxnDateISO = ? WHERE rowid = ?",
            [(to_iso(d), rid) for rid, d in rows],
        )
        con.commit()

    unparsed = con.execute("SELECT COUNT(*) FROM pcards WHERE TxnDateISO IS NULL").fetchone()[0]
    print(f"Dates that could not be parsed: {unparsed}")

    print("Indexing Year")
    con.execute("CREATE INDEX IF NOT EXISTS idx_year ON pcards(Year)")
    con.commit()
    con.execute("VACUUM")
    total = con.execute("SELECT COUNT(*) FROM pcards").fetchone()[0]
    con.close()

    print(f"Compressing {total:,} rows -> {OUT_GZ}")
    with open(OUT_DB, "rb") as src, gzip.open(OUT_GZ, "wb", compresslevel=9) as dst:
        shutil.copyfileobj(src, dst, length=8 * 1024 * 1024)

    print(f"Done. {OUT_GZ.stat().st_size / 1e6:.1f} MB compressed.")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("Usage: python scripts/build_db.py path/to/pcards_1.db")
    main(Path(sys.argv[1]))
