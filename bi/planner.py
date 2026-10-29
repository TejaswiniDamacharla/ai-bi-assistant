"""Plan validation.

A "plan" is a tiny JSON-like dict describing what to compute, e.g.

    {"intent": "ranking", "dimension": "region", "order": "desc",
     "filters": {"month": "2026-03"}}

Both the LLM and the rule-based interpreter produce plans. Nothing is executed
until `validate_plan` has checked it. This is the main guardrail against wrong
or unsafe answers:

  * intent must be one of a small whitelist (so the AI can never ask for
    "DROP TABLE" or any other behaviour we did not build),
  * dimensions / filter keys must be whitelisted,
  * filter values are matched (case-insensitively) against values that really
    exist in the database, so hallucinated products/regions are rejected,
  * months must look like YYYY-MM and exist in the data.
"""

from __future__ import annotations

import re

from .analytics import DIMENSIONS
from .errors import EXAMPLE_QUESTIONS, InvalidPlan, QuestionError
from .formatting import month_label

INTENTS = {"total_revenue", "ranking", "monthly_trend", "month_comparison"}
MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


def _canonical(value, options: list[str]) -> str | None:
    wanted = str(value).strip().lower()
    for option in options:
        if option.lower() == wanted:
            return option
    return None


def check_month(value, catalog: dict) -> str:
    text = str(value).strip()
    if not MONTH_RE.match(text):
        raise InvalidPlan(f"'{value}' is not a YYYY-MM month")
    months = catalog["months"]
    if text not in months:
        raise QuestionError(
            f"There is no sales data for {month_label(text)}. "
            f"The data covers {month_label(months[0])} to {month_label(months[-1])}.",
            code="data_not_available")
    return text


def validate_plan(raw, catalog: dict) -> dict:
    """Return a clean, safe plan or raise InvalidPlan / QuestionError."""
    if not isinstance(raw, dict):
        raise InvalidPlan("plan is not an object")

    intent = raw.get("intent")
    if intent == "unsupported":
        reason = raw.get("reason")
        raise QuestionError(
            str(reason) if reason else "I can only answer questions about revenue in the sales data.",
            suggestions=EXAMPLE_QUESTIONS)
    if intent not in INTENTS:
        raise InvalidPlan(f"unknown intent {intent!r}")

    plan = {"intent": intent, "dimension": None, "order": "desc", "months": [], "filters": {}, "notes": []}

    if intent == "ranking":
        dimension = raw.get("dimension")
        if dimension not in DIMENSIONS:
            raise InvalidPlan(f"invalid dimension {dimension!r}")
        order = str(raw.get("order") or "desc").lower()
        if order not in ("asc", "desc"):
            raise InvalidPlan(f"invalid order {order!r}")
        plan["dimension"], plan["order"] = dimension, order

    filters = raw.get("filters") or {}
    if not isinstance(filters, dict):
        raise InvalidPlan("filters is not an object")
    for key, value in filters.items():
        if value in (None, ""):
            continue
        if key == "month":
            plan["filters"]["month"] = check_month(value, catalog)
        elif key in DIMENSIONS:
            canonical = _canonical(value, catalog["values"][key])
            if canonical is None:
                options = ", ".join(catalog["values"][key])
                raise QuestionError(f"There is no {key} called '{value}' in the data. Available {key}s: {options}.",
                                    code="data_not_available")
            plan["filters"][key] = canonical
        else:
            raise InvalidPlan(f"unknown filter {key!r}")

    if intent == "month_comparison":
        months = raw.get("months")
        if not isinstance(months, list) or len(months) != 2:
            raise InvalidPlan("month_comparison needs exactly two months")
        checked = sorted({check_month(m, catalog) for m in months})
        if len(checked) != 2:
            raise QuestionError("Please give two different months to compare.", code="invalid_request", status=400)
        plan["months"] = checked

    # A month filter makes no sense when the question is already about months.
    if intent in ("monthly_trend", "month_comparison") and "month" in plan["filters"]:
        del plan["filters"]["month"]
        plan["notes"].append("Ignored the single-month filter because the question is already about months.")

    return plan
