"""Data management: CSV -> cleaned rows -> SQLite.

Design choices (worth explaining in the interview):
  * Every CSV row is validated one by one. Bad rows are NOT silently dropped:
    they are stored in a `rejected_records` table together with the reason.
  * 'Returned' orders are kept in the `sales` table (so no data is lost) but
    analytics only count status = 'Completed'.
  * `revenue` is computed once at load time:  quantity * unit_price - discount.
  * The web app opens the database in READ-ONLY mode, so even a bug (or a
    malicious question) cannot modify the data.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path

import pandas as pd

REQUIRED_COLUMNS = [
    "Transaction_Id", "Order_date", "Product", "Category", "Region",
    "Sales_channel", "Quantity", "Unit_price", "Discount_amount", "Status",
]
TEXT_COLUMNS = ["Transaction_Id", "Product", "Category", "Region", "Sales_channel", "Status"]
DATE_FORMAT = "%d-%m-%Y"  # the CSV uses DD-MM-YYYY

SCHEMA = """
CREATE TABLE sales (
    transaction_id  TEXT PRIMARY KEY,
    order_date      TEXT NOT NULL,          -- ISO format YYYY-MM-DD
    month           TEXT NOT NULL,          -- YYYY-MM (handy for GROUP BY)
    product         TEXT NOT NULL,
    category        TEXT NOT NULL,
    region          TEXT NOT NULL,
    sales_channel   TEXT NOT NULL,
    quantity        INTEGER NOT NULL CHECK (quantity > 0),
    unit_price      REAL NOT NULL CHECK (unit_price > 0),
    discount_amount REAL NOT NULL CHECK (discount_amount >= 0),
    status          TEXT NOT NULL,
    revenue         REAL NOT NULL           -- quantity * unit_price - discount_amount
);
CREATE INDEX idx_sales_month  ON sales(month);
CREATE INDEX idx_sales_region ON sales(region);
CREATE INDEX idx_sales_product ON sales(product);

CREATE TABLE rejected_records (
    csv_line       INTEGER,
    transaction_id TEXT,
    reason         TEXT NOT NULL,
    raw_row        TEXT NOT NULL
);
"""


def _blank(value) -> bool:
    return pd.isna(value) or str(value).strip() == ""


def _number(value) -> float | None:
    """Parse a numeric cell; return None if blank/invalid."""
    if _blank(value):
        return None
    try:
        number = float(str(value).replace(",", "").strip())
    except ValueError:
        return None
    return None if (math.isnan(number) or math.isinf(number)) else number


def parse_row(row) -> tuple[dict | None, list[str]]:
    """Validate one CSV row. Returns (clean_record, problems).

    If `problems` is non-empty the record is None and the row is rejected.
    """
    problems: list[str] = []
    record: dict = {}

    for col in TEXT_COLUMNS:
        if _blank(row[col]):
            problems.append(f"missing {col}")
        else:
            record[col] = str(row[col]).strip()

    parsed_date = None
    if _blank(row["Order_date"]):
        problems.append("missing Order_date")
    else:
        parsed_date = pd.to_datetime(str(row["Order_date"]).strip(), format=DATE_FORMAT, errors="coerce")
        if pd.isna(parsed_date):
            problems.append(f"invalid Order_date '{row['Order_date']}' (expected DD-MM-YYYY)")

    quantity = _number(row["Quantity"])
    if quantity is None:
        problems.append("missing or non-numeric Quantity")
    elif quantity <= 0 or quantity != int(quantity):
        problems.append(f"invalid Quantity ({quantity:g}); must be a positive whole number")

    unit_price = _number(row["Unit_price"])
    if unit_price is None:
        problems.append("missing or non-numeric Unit_price")
    elif unit_price <= 0:
        problems.append(f"invalid Unit_price ({unit_price:g}); must be > 0")

    # A blank discount is treated as 0 (documented assumption).
    discount = 0.0 if _blank(row["Discount_amount"]) else _number(row["Discount_amount"])
    if discount is None:
        problems.append("non-numeric Discount_amount")
    elif discount < 0:
        problems.append(f"negative Discount_amount ({discount:g})")

    if not problems and discount > quantity * unit_price:
        problems.append("Discount_amount is larger than the gross amount")

    if problems:
        return None, problems

    record.update(
        order_date=parsed_date.strftime("%Y-%m-%d"),
        month=parsed_date.strftime("%Y-%m"),
        quantity=int(quantity),
        unit_price=unit_price,
        discount_amount=discount,
        revenue=quantity * unit_price - discount,
    )
    return record, []


def load_and_clean(csv_path: str | Path) -> tuple[list[dict], list[dict], int]:
    """Read the CSV and split it into (clean_records, rejected_records, total_rows)."""
    raw = pd.read_csv(csv_path, dtype=str)
    raw.columns = [str(c).strip() for c in raw.columns]
    missing = [c for c in REQUIRED_COLUMNS if c not in raw.columns]
    if missing:
        raise ValueError(f"CSV is missing required columns: {missing}")

    clean: list[dict] = []
    rejected: list[dict] = []
    seen_ids: set[str] = set()

    for index, row in raw.iterrows():
        line = int(index) + 2  # +1 for header, +1 because humans count from 1
        record, problems = parse_row(row)
        if record is not None and record["Transaction_Id"] in seen_ids:
            record, problems = None, [f"duplicate Transaction_Id {row['Transaction_Id']}"]
        if record is None:
            rejected.append({
                "csv_line": line,
                "transaction_id": None if _blank(row["Transaction_Id"]) else str(row["Transaction_Id"]).strip(),
                "reason": "; ".join(problems),
                "raw_row": json.dumps({k: (None if pd.isna(v) else str(v)) for k, v in row.items()}),
            })
        else:
            seen_ids.add(record["Transaction_Id"])
            clean.append(record)
    return clean, rejected, len(raw)


def build_database(csv_path: str | Path, db_path: str | Path) -> dict:
    """(Re)create the SQLite database from the CSV. Returns a load summary."""
    clean, rejected, total = load_and_clean(csv_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.unlink(missing_ok=True)

    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO sales VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            [(r["Transaction_Id"], r["order_date"], r["month"], r["Product"], r["Category"],
              r["Region"], r["Sales_channel"], r["quantity"], r["unit_price"],
              r["discount_amount"], r["Status"], r["revenue"]) for r in clean],
        )
        conn.executemany(
            "INSERT INTO rejected_records VALUES (?,?,?,?)",
            [(r["csv_line"], r["transaction_id"], r["reason"], r["raw_row"]) for r in rejected],
        )
        conn.commit()
    finally:
        conn.close()
    return {"total_rows": total, "loaded": len(clean), "rejected": len(rejected)}


def get_readonly_connection(db_path: str | Path) -> sqlite3.Connection:
    """Open the DB so that INSERT/UPDATE/DELETE/DROP are impossible."""
    uri = Path(db_path).resolve().as_uri() + "?mode=ro"
    return sqlite3.connect(uri, uri=True)


def get_catalog(conn: sqlite3.Connection) -> dict:
    """Valid values the interpreter may refer to (used to validate questions)."""
    months = [r[0] for r in conn.execute(
        "SELECT DISTINCT month FROM sales WHERE status = 'Completed' ORDER BY month")]
    values = {}
    for dim, col in (("product", "product"), ("region", "region"),
                     ("category", "category"), ("channel", "sales_channel")):
        values[dim] = [r[0] for r in conn.execute(f"SELECT DISTINCT {col} FROM sales ORDER BY {col}")]
    return {"months": months, "values": values}


def get_data_quality(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        "SELECT csv_line, transaction_id, reason FROM rejected_records ORDER BY csv_line").fetchall()
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM sales GROUP BY status").fetchall())
    return {
        "rejected_records": [{"csv_line": r[0], "transaction_id": r[1], "reason": r[2]} for r in rows],
        "loaded_by_status": counts,
    }
