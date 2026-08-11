"""Sensitive identifier redaction shared by every input and output path.

The feature intentionally favors privacy over perfect classification.  Citizen IDs,
new foreign-resident UI numbers, and old UI numbers are replaced with the same
number of ``*`` characters.  The original value is never returned to callers.
"""

from __future__ import annotations

import re
from typing import Any


# Taiwan citizen ID and new UI number: one ASCII letter and nine digits.
_ONE_LETTER_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][0-9]{9}(?![A-Za-z0-9])")
# Old UI number: two ASCII letters and eight digits.  This can overlap with an
# invoice track number, so structured invoice parsing must discard that field
# before calling the generic sanitizer.  Ambiguous free text is always redacted.
_TWO_LETTER_ID = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{2}[0-9]{8}(?![A-Za-z0-9])")


def redact_sensitive_identifiers(text: str) -> str:
    """Return *text* with supported identifiers replaced by equal-length stars."""
    if not text:
        return text

    def _mask(match: re.Match[str]) -> str:
        return "*" * len(match.group(0))

    redacted = _TWO_LETTER_ID.sub(_mask, str(text))
    return _ONE_LETTER_ID.sub(_mask, redacted)


def contains_sensitive_identifier(text: str) -> bool:
    """Return whether text contains an identifier that the sanitizer would mask."""
    if not text:
        return False
    return bool(_ONE_LETTER_ID.search(text) or _TWO_LETTER_ID.search(text))


def redact_structure(value: Any) -> Any:
    """Recursively redact strings in JSON-like data without mutating the input."""
    if isinstance(value, str):
        return redact_sensitive_identifiers(value)
    if isinstance(value, list):
        return [redact_structure(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_structure(item) for item in value)
    if isinstance(value, dict):
        return {key: redact_structure(item) for key, item in value.items()}
    return value

