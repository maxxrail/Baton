"""Mask PII before it reaches logs or traces: emails, phone numbers and card-like numbers.

Deliberately conservative: it's better to over-mask an order number than to log a phone.
Order numbers in Northwind's `NW-12345` format are left alone because support needs them.

Known limits (pattern-based, not a PII classifier):
- Phones are matched in North American shapes (10 digits, optional country code). Other
  international formats such as `+44 20 7946 0958` are not masked.
- Any 13-19 digit run is treated as a card, so long numeric IDs (e.g. epoch milliseconds)
  get masked too. That errs on the safe side.
"""

import re

from pydantic import BaseModel

_EMAIL = re.compile(r"\b([A-Za-z0-9._%+-])[A-Za-z0-9._%+-]*@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
# 13-19 digits, optionally separated by spaces or dashes (credit/debit card shapes).
_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
# Phone-like: optional +country, then 10+ digits with common separators.
_PHONE = re.compile(r"(?<![\w-])(?:\+?\d{1,3}[ .-]?)?\(?\d{3}\)?[ .-]?\d{3}[ .-]?\d{4}\b")


def redact(text: str) -> str:
    text = _EMAIL.sub(lambda m: f"{m.group(1)}***@{m.group(2)}", text)
    text = _CARD.sub(lambda m: f"[card ****{re.sub(r'\D', '', m.group(0))[-4:]}]", text)
    text = _PHONE.sub("[phone]", text)
    return text


def redact_value(value: object) -> object:
    """Recursively redact a structured value (log event fields, trace payloads).

    Strings are redacted; dict keys too; Pydantic models via `model_dump`; numbers, bools and
    None pass through; anything else is rendered with `str()` and redacted, so an object's
    repr can't smuggle PII past the filter.
    """
    if isinstance(value, str):
        return redact(value)
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, BaseModel):
        return redact_value(value.model_dump(mode="json"))
    if isinstance(value, dict):
        return {redact(str(k)): redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [redact_value(v) for v in value]
    return redact(str(value))


# Public names for the patterns, so guardrails can find (not just mask) contact details.
EMAIL_RE = _EMAIL
CARD_RE = _CARD
PHONE_RE = _PHONE
