"""Error types used across the app.

QuestionError  -> the *user* asked something we cannot (or should not) answer.
                  It carries an HTTP status and a machine-readable code, and is
                  shown to the user as a friendly message.
InvalidPlan    -> the *interpreter* (usually the LLM) produced a malformed or
                  unsafe plan. It is never shown to the user; the app falls back
                  to the rule-based interpreter instead.
"""

from __future__ import annotations

EXAMPLE_QUESTIONS = [
    "What is the total revenue?",
    "Which product generated the highest revenue?",
    "Show the monthly revenue trend",
    "Which region generated the highest sales?",
    "How did revenue change between January and February?",
]


class QuestionError(Exception):
    def __init__(self, message: str, code: str = "unsupported_question",
                 status: int = 422, suggestions: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.suggestions = suggestions if suggestions is not None else EXAMPLE_QUESTIONS


class InvalidPlan(Exception):
    """The interpreter's output failed validation."""
