"""Turn a natural-language question into a validated plan.

Pipeline (in order):
  1. Basic input checks            (empty / too long)            -> 400
  2. Read-only guard               (delete/drop/update ...)      -> 403
  3. Unsupported-topic guard       (profit, forecast, units ...) -> 422
  4. LLM interpretation (optional) -> validate_plan
        if the LLM is down / returns garbage -> fall back to step 5
  5. Rule-based interpretation     -> validate_plan

Steps 1-3 run BEFORE the LLM, so dangerous or unsupported questions never
depend on the AI behaving correctly.
"""

from __future__ import annotations

import re

from .analytics import DIMENSIONS
from .errors import EXAMPLE_QUESTIONS, InvalidPlan, QuestionError
from .formatting import month_label
from .llm import LLMError
from .planner import validate_plan

MAX_QUESTION_LENGTH = 500

DESTRUCTIVE_RE = re.compile(
    r"\b(delete|drop|truncate|insert|alter|remove|erase|modify|update|overwrite|wipe)\b", re.I)

# Things the dataset/app cannot answer. Refusing is better than giving a wrong number.
UNSUPPORTED_RE = re.compile(
    r"\b(profit|profits|margin|margins|cost|costs|customer|customers|forecast|predict|prediction|"
    r"projection|average|avg|mean|median|inventory|stock|employee|employees|salary|tax|supplier|"
    r"units?|quantity|quantities|pieces|volume|how many|number of|count|"
    r"top\s+(?:\d+|two|three|four|five|ten)|bottom\s+(?:\d+|two|three|four|five|ten)|"
    r"next (month|year|quarter|week))\b", re.I)

BUSINESS_RE = re.compile(r"\b(revenue|sales|sold|selling|sell|income|earn\w*|turnover|money)\b", re.I)
SUPERLATIVE_RE = re.compile(
    r"\b(highest|top|best|most|largest|biggest|max|maximum|leading|greatest|"
    r"lowest|least|worst|smallest|minimum|weakest|bottom|fewest)\b", re.I)
BREAKDOWN_RE = re.compile(r"\b(by|each|every|per|wise|breakdown|split)\b", re.I)
ASCENDING_RE = re.compile(r"\b(lowest|least|worst|smallest|minimum|weakest|bottom|fewest)\b", re.I)
TREND_RE = re.compile(
    r"\b(trend|trends|monthly|month[- ]by[- ]month|month[- ]wise|per month|each month|"
    r"by month|over time|month over month|mom)\b", re.I)
COMPARE_RE = re.compile(
    r"\b(compare|compared|comparison|change|changed|difference|vs|versus|growth|grew|"
    r"increase|increased|decrease|decreased|between)\b", re.I)

DIMENSION_WORDS = {
    "product": re.compile(r"\bproducts?\b", re.I),
    "region": re.compile(r"\bregions?\b", re.I),
    "category": re.compile(r"\bcategor(?:y|ies)\b", re.I),
    "channel": re.compile(r"\bchannels?\b", re.I),
}

MONTH_NAMES = {
    "january": 1, "jan": 1, "february": 2, "feb": 2, "march": 3, "mar": 3, "april": 4, "apr": 4,
    "may": 5, "june": 6, "jun": 6, "july": 7, "jul": 7, "august": 8, "aug": 8,
    "september": 9, "sept": 9, "sep": 9, "october": 10, "oct": 10,
    "november": 11, "nov": 11, "december": 12, "dec": 12,
}
MONTH_RE = re.compile(r"\b(" + "|".join(sorted(MONTH_NAMES, key=len, reverse=True)) + r")\b(?:\s+(20\d{2}))?", re.I)
ISO_MONTH_RE = re.compile(r"\b(20\d{2})-(0[1-9]|1[0-2])\b")



# Words that may follow "for/of/in..." without naming a specific product/region/etc.
ENTITY_TRIGGER_RE = re.compile(r"\b(?:for|of|about|on|in|from)\s+(?:the\s+)?([a-z0-9][a-z0-9\-]*)", re.I)
ENTITY_IGNORE = {
    "the", "all", "each", "every", "our", "my", "this", "that", "total", "overall", "whole", "entire",
    "company", "business", "us", "we", "everything", "year", "month", "months", "data", "sales", "revenue",
    "product", "products", "region", "regions", "category", "categories", "channel", "channels", "a", "an",
}


def find_unknown_entity(text: str, catalog: dict) -> str | None:
    """Spot 'revenue for Smartphone'-style requests naming something that is not in the data.

    The rule-based interpreter would otherwise ignore the unknown word and silently answer
    a different question (the overall total) - a wrong answer. Better to say so.
    """
    known_words = set()
    for values in catalog["values"].values():
        for value in values:
            known_words.update(re.split(r"\s+", value.lower()))
            known_words.add(value.lower())
    for match in ENTITY_TRIGGER_RE.finditer(text.lower()):
        token = match.group(1).rstrip("s") if match.group(1).endswith("s") and len(match.group(1)) > 3 else match.group(1)
        word = match.group(1)
        if (word in ENTITY_IGNORE or word in MONTH_NAMES or re.fullmatch(r"\d{4}(-\d{2})?", word)
                or word in known_words or token in known_words):
            continue
        return word
    return None

# --------------------------------------------------------------------------- #
# Rule-based interpreter
# --------------------------------------------------------------------------- #
def _default_year(catalog: dict) -> str:
    return catalog["months"][-1][:4]


def extract_months(text: str, catalog: dict) -> list[str]:
    """Find months mentioned in the question, in order, as 'YYYY-MM'."""
    found: list[tuple[int, str]] = []
    for match in ISO_MONTH_RE.finditer(text):
        found.append((match.start(), f"{match.group(1)}-{match.group(2)}"))
    spans = [(m.start(), m.end()) for m in ISO_MONTH_RE.finditer(text)]
    for match in MONTH_RE.finditer(text):
        if any(s <= match.start() < e for s, e in spans):
            continue
        year = match.group(2) or _default_year(catalog)
        found.append((match.start(), f"{year}-{MONTH_NAMES[match.group(1).lower()]:02d}"))
    ordered: list[str] = []
    for _, month in sorted(found):
        if month not in ordered:
            ordered.append(month)
    return ordered


def extract_filters(text: str, catalog: dict) -> dict:
    """Find product/region/category/channel values mentioned in the question."""
    filters: dict = {}
    for dim, values in catalog["values"].items():
        hits = [v for v in values if re.search(r"\b" + re.escape(v.lower()) + r"s?\b", text)]
        if len(hits) > 1:
            raise QuestionError(f"Please ask about one {dim} at a time (you mentioned: {', '.join(hits)}).",
                                code="unsupported_question")
        if hits:
            filters[dim] = hits[0]
    return filters


def rules_plan(question: str, catalog: dict) -> dict:
    """Keyword-based interpretation. Deterministic, no network needed."""
    text = question.lower()
    filters = extract_filters(text, catalog)
    months = extract_months(text, catalog)
    has_business = bool(BUSINESS_RE.search(text))
    has_superlative = bool(SUPERLATIVE_RE.search(text))
    has_trend = bool(TREND_RE.search(text))
    wants_compare = bool(COMPARE_RE.search(text))

    # Which dimension is the user asking us to rank? (ignore ones they already filtered on)
    ranked_dim = None
    positions = []
    for dim, pattern in DIMENSION_WORDS.items():
        match = pattern.search(text)
        if match and dim not in filters:
            positions.append((match.start(), dim))
    if positions:
        ranked_dim = min(positions)[1]

    if len(months) > 2:
        raise QuestionError("Please mention at most two months (compare two months, or ask for the full trend).")

    # --- compare two months -------------------------------------------------
    if len(months) == 2 and not wants_compare and not has_trend:
        raise QuestionError(
            f"Did you want to compare {month_label(months[0])} and {month_label(months[1])}? "
            "Try 'How did revenue change between "
            f"{month_label(months[0]).split()[0]} and {month_label(months[1]).split()[0]}?' "
            "(month ranges / sums over several months are not supported).")
    if len(months) == 2 and not has_trend:
        return {"intent": "month_comparison", "months": months, "filters": filters}
    if len(months) == 1 and wants_compare and not has_trend and not ranked_dim and has_business:
        year, mm = months[0].split("-")
        previous = f"{year}-{int(mm) - 1:02d}" if int(mm) > 1 else None
        if previous is None or previous not in catalog["months"]:
            raise QuestionError(
                f"To measure change I need two months (e.g. 'between January and February'); "
                f"there is no month before {month_label(months[0])} in the data.")
        return {"intent": "month_comparison", "months": [previous, months[0]], "filters": filters}

    # --- trend ----------------------------------------------------------------
    if has_trend and has_business:
        if months:
            raise QuestionError("Month ranges are not supported. Ask for the full monthly trend "
                                "(optionally for one region/product/category/channel).")
        return {"intent": "monthly_trend", "filters": filters}

    # --- ranking ----------------------------------------------------------------
    if ranked_dim and (has_business or has_superlative) and (has_superlative or BREAKDOWN_RE.search(text)):
        if months:
            filters["month"] = months[0]
        order = "asc" if ASCENDING_RE.search(text) else "desc"
        return {"intent": "ranking", "dimension": ranked_dim, "order": order, "filters": filters}

    # --- total revenue ----------------------------------------------------------
    if has_business:
        if months:
            filters["month"] = months[0]
        return {"intent": "total_revenue", "filters": filters}

    raise QuestionError("I can only answer questions about revenue in the sales data.")


# --------------------------------------------------------------------------- #
# Orchestrator
# --------------------------------------------------------------------------- #
def interpret(question, catalog: dict, llm_func=None) -> dict:
    """Return {"plan": validated_plan, "interpreted_by": "llm"|"rules"|"rules_fallback", "notes": [...]}."""
    if not isinstance(question, str) or not question.strip():
        raise QuestionError("Please type a question.", code="invalid_request", status=400)
    question = " ".join(question.split())
    if len(question) > MAX_QUESTION_LENGTH:
        raise QuestionError(f"Question is too long (max {MAX_QUESTION_LENGTH} characters).",
                            code="invalid_request", status=400)

    if DESTRUCTIVE_RE.search(question):
        raise QuestionError("This assistant is read-only. It can answer questions about the sales data "
                            "but cannot change, delete or insert anything.",
                            code="read_only", status=403)

    topic = UNSUPPORTED_RE.search(question)
    if topic:
        raise QuestionError(
            f"Sorry, '{topic.group(0)}' is not supported. The data only allows revenue questions "
            "(revenue = quantity × unit price − discount), so I won't guess.")

    unknown = find_unknown_entity(question, catalog)
    if unknown:
        options = "; ".join(f"{dim}s: {', '.join(vals)}" for dim, vals in catalog["values"].items())
        raise QuestionError(f"I couldn't find '{unknown}' in the data. Valid values are - {options}.",
                            code="data_not_available")

    notes: list[str] = []
    if llm_func is not None:
        try:
            plan = validate_plan(llm_func(question, catalog), catalog)
            return {"plan": plan, "interpreted_by": "llm", "notes": notes + plan.pop("notes")}
        except (LLMError, InvalidPlan) as exc:
            notes.append(f"LLM output was not usable ({exc}); used the rule-based interpreter instead.")
        # QuestionError (user-facing) is deliberately NOT caught: it goes straight to the user.

    try:
        plan = validate_plan(rules_plan(question, catalog), catalog)
    except InvalidPlan as exc:  # pragma: no cover - rules should always produce valid plans
        raise QuestionError("I could not understand that question.") from exc
    return {"plan": plan, "interpreted_by": "rules_fallback" if notes else "rules",
            "notes": notes + plan.pop("notes")}


__all__ = ["interpret", "rules_plan", "extract_months", "extract_filters", "EXAMPLE_QUESTIONS", "DIMENSIONS"]
