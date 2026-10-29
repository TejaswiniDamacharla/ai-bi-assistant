"""Analytics: every number shown to the user comes from here.

The AI never calculates anything. It only decides WHICH of these functions to
call and with WHAT parameters. SQL text is built from fixed templates; only
whitelisted column names are ever interpolated, and all values are passed as
bound parameters (`?`), so SQL injection is not possible.
"""

from __future__ import annotations

import sqlite3

from .errors import QuestionError
from .formatting import inr, month_label, pct

# user-facing dimension name -> real column name (the ONLY columns we allow)
DIMENSIONS = {
    "product": "product",
    "region": "region",
    "category": "category",
    "channel": "sales_channel",
}
PLURALS = {"product": "products", "region": "regions", "category": "categories", "channel": "channels"}
ORDERS = {"desc": "DESC", "asc": "ASC"}

BASE_ASSUMPTIONS = [
    "Revenue = Quantity × Unit_price − Discount_amount.",
    "Only orders with status 'Completed' are counted; 'Returned' orders are excluded.",
    "Amounts are shown in Indian rupees (₹); the CSV does not state a currency.",
]


def _where(filters: dict) -> tuple[str, list]:
    clauses = ["status = 'Completed'"]
    params: list = []
    for key, value in filters.items():
        if key == "month":
            clauses.append("month = ?")
        elif key in DIMENSIONS:
            clauses.append(f"{DIMENSIONS[key]} = ?")  # key comes from the whitelist above
        else:
            raise QuestionError(f"Unsupported filter '{key}'.", code="invalid_plan", status=400)
        params.append(value)
    return " AND ".join(clauses), params


def _scope(filters: dict) -> str:
    if not filters:
        return ""
    parts = []
    for key, value in filters.items():
        parts.append(f"Month: {month_label(value)}" if key == "month" else f"{key.capitalize()}: {value}")
    return " (" + ", ".join(parts) + ")"


def _query(conn: sqlite3.Connection, sql: str, params: list) -> tuple[list[str], list[list]]:
    cursor = conn.execute(sql, params)
    columns = [d[0] for d in cursor.description]
    return columns, [list(r) for r in cursor.fetchall()]


def _no_data(filters: dict) -> QuestionError:
    return QuestionError(f"No completed sales match that request{_scope(filters)}.",
                         code="data_not_available")


# --------------------------------------------------------------------------- #
# 1. Total revenue
# --------------------------------------------------------------------------- #
def total_revenue(conn, plan):
    filters = plan["filters"]
    where, params = _where(filters)
    sql = (
        "SELECT COUNT(*) AS orders, SUM(quantity) AS units_sold, "
        "SUM(quantity * unit_price) AS gross_sales, SUM(discount_amount) AS discounts, "
        "SUM(revenue) AS net_revenue FROM sales WHERE " + where
    )
    columns, rows = _query(conn, sql, params)
    orders, units, gross, discounts, net = rows[0]
    if not orders:
        raise _no_data(filters)
    answer = (f"Total revenue{_scope(filters)} is {inr(net)}, from {orders} completed orders "
              f"({units} units; gross {inr(gross)} minus discounts {inr(discounts)}).")
    return answer, columns, rows, sql, params, {"net_revenue": "inr", "gross_sales": "inr", "discounts": "inr"}


# --------------------------------------------------------------------------- #
# 2/4. Ranking by product / region / category / channel
# --------------------------------------------------------------------------- #
def ranking(conn, plan):
    dim, order, filters = plan["dimension"], plan["order"], plan["filters"]
    col = DIMENSIONS[dim]
    where, params = _where(filters)
    sql = (
        f"SELECT {col} AS {dim}, SUM(revenue) AS revenue, SUM(quantity) AS units_sold, "
        f"COUNT(*) AS orders FROM sales WHERE {where} GROUP BY {col} "
        f"ORDER BY revenue {ORDERS[order]}, {col} ASC"
    )
    columns, rows = _query(conn, sql, params)
    if not rows:
        raise _no_data(filters)
    total = sum(r[1] for r in rows)
    for r in rows:
        r.append(round(100 * r[1] / total, 1) if total else None)
    columns.append("share_pct")

    name, revenue = rows[0][0], rows[0][1]
    word = "highest" if order == "desc" else "lowest"
    answer = (f"{name} has the {word} revenue among {PLURALS[dim]}{_scope(filters)}: "
              f"{inr(revenue)} ({rows[0][4]}% of the {inr(total)} total).")
    if len(rows) > 1 and rows[1][1] == revenue:
        answer += f" Note: {rows[1][0]} is tied with the same revenue."
    return answer, columns, rows, sql, params, {"revenue": "inr", "share_pct": "pct"}


# --------------------------------------------------------------------------- #
# 3. Monthly trend
# --------------------------------------------------------------------------- #
def monthly_trend(conn, plan):
    filters = plan["filters"]
    where, params = _where(filters)
    sql = ("SELECT month, SUM(revenue) AS revenue, COUNT(*) AS orders FROM sales "
           f"WHERE {where} GROUP BY month ORDER BY month")
    columns, rows = _query(conn, sql, params)
    if not rows:
        raise _no_data(filters)
    previous = None
    for r in rows:
        r.append(None if previous in (None, 0) else round(100 * (r[1] - previous) / previous, 1))
        previous = r[1]
    columns.append("mom_change_pct")

    first, last = rows[0], rows[-1]
    peak = max(rows, key=lambda r: r[1])
    low = min(rows, key=lambda r: r[1])
    overall = None if first[1] == 0 else round(100 * (last[1] - first[1]) / first[1], 1)
    answer = (f"Monthly revenue{_scope(filters)} went from {inr(first[1])} in {month_label(first[0])} "
              f"to {inr(last[1])} in {month_label(last[0])} ({pct(overall)}). "
              f"Peak: {month_label(peak[0])} ({inr(peak[1])}); lowest: {month_label(low[0])} ({inr(low[1])}).")
    return answer, columns, rows, sql, params, {"revenue": "inr", "mom_change_pct": "pct"}


# --------------------------------------------------------------------------- #
# 5. Compare two months
# --------------------------------------------------------------------------- #
def month_comparison(conn, plan):
    filters, (m1, m2) = plan["filters"], plan["months"]
    where, params = _where(filters)
    sql = ("SELECT month, SUM(revenue) AS revenue, COUNT(*) AS orders, SUM(quantity) AS units_sold "
           f"FROM sales WHERE {where} AND month IN (?, ?) GROUP BY month ORDER BY month")
    params = params + [m1, m2]
    columns, rows = _query(conn, sql, params)
    by_month = {r[0]: r for r in rows}
    if not by_month:
        raise _no_data(filters)
    rows = [by_month.get(m, [m, 0, 0, 0]) for m in (m1, m2)]  # show 0 for a month with no sales
    a, b = rows[0][1], rows[1][1]
    diff = b - a
    change = None if a == 0 else round(100 * diff / a, 1)
    direction = "rose" if diff > 0 else "fell" if diff < 0 else "did not change"
    if diff == 0:
        answer = f"Revenue{_scope(filters)} did not change between {month_label(m1)} and {month_label(m2)} ({inr(a)})."
    else:
        pct_text = f" ({pct(change)})" if change is not None else " (percentage not defined: first month had no revenue)"
        answer = (f"Revenue{_scope(filters)} {direction} by {inr(abs(diff))}{pct_text} from "
                  f"{month_label(m1)} ({inr(a)}) to {month_label(m2)} ({inr(b)}).")
    return answer, columns, rows, sql, params, {"revenue": "inr"}


INTENT_HANDLERS = {
    "total_revenue": total_revenue,
    "ranking": ranking,
    "monthly_trend": monthly_trend,
    "month_comparison": month_comparison,
}


def run_plan(conn: sqlite3.Connection, plan: dict) -> dict:
    """Execute a *validated* plan and return the structured result."""
    handler = INTENT_HANDLERS.get(plan["intent"])
    if handler is None:
        raise QuestionError("Unknown intent.", code="invalid_plan", status=400)
    answer, columns, rows, sql, params, formats = handler(conn, plan)
    return {
        "answer": answer,
        "supporting_data": {"columns": columns, "rows": rows, "formats": formats},
        "sql": sql,
        "sql_params": params,
        "assumptions": list(BASE_ASSUMPTIONS),
    }
