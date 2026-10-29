"""Small helpers that turn numbers/months into human-friendly text."""

from __future__ import annotations

import calendar


def inr(amount) -> str:
    """Format a number as Indian rupees with lakh/crore grouping.

    >>> inr(1234567.5)
    '₹12,34,567.50'
    """
    if amount is None:
        return "n/a"
    negative = amount < 0
    paise = round(abs(float(amount)) * 100)
    whole, frac = divmod(paise, 100)
    digits = str(whole)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups + [tail])
    return f"{'-' if negative else ''}₹{digits}.{frac:02d}"


def month_label(month: str) -> str:
    """'2026-03' -> 'March 2026'."""
    year, mm = month.split("-")
    return f"{calendar.month_name[int(mm)]} {year}"


def pct(value) -> str:
    return "n/a" if value is None else f"{value:+.1f}%"
