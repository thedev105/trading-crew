from __future__ import annotations

import re

_MARKER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "instruction_like_text",
        re.compile(
            r"(?:ignore\s+(?:all\s+)?previous\s+instructions|"
            r"(?:assistant|system|developer)\s*:|call\s+the\s+tool|"
            r"follow\s+these\s+instructions)",
            re.IGNORECASE,
        ),
    ),
    ("active_markup", re.compile(r"<\s*script\b|\bon[a-z]+\s*=", re.IGNORECASE)),
    ("unicode_control", re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069]")),
    # Mirrors corpus.canonicalize_rule_text's confusable-letter check (Cyrillic and Greek).
    ("unicode_confusable", re.compile(r"[\u0400-\u04ff\u0370-\u03ff\u1f00-\u1fff]")),
    ("non_http_url", re.compile(r"\b(?:javascript|data|file):", re.IGNORECASE)),
    ("code_fence", re.compile(r"```")),
    (
        "sql_fragment",
        re.compile(
            r"\b(?:drop\s+table|delete\s+from|insert\s+into|select\b.+\bfrom|"
            r"update\b.+\bset)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shell_fragment",
        re.compile(
            r"\b(?:rm\s+-rf|curl\s+|wget\s+|(?:ba|z|k)?sh\s+-c|sudo\s+)"
            r"|\$\(",
            re.IGNORECASE,
        ),
    ),
)


def find_untrusted_text_markers(text: str) -> tuple[str, ...]:
    """Classify inert text markers without interpreting or acting on the input."""

    return tuple(name for name, pattern in _MARKER_PATTERNS if pattern.search(text) is not None)
