"""Phone numbers: stored as typed, matched as E.164.

Layer: L7

Order 1k 2d. `phonenumbers` (a wheel, rule 12) does the parsing. A number the
library cannot make sense of keeps no E.164 form and is matched by its digits
only when it is long enough to mean something, so "123" never links two people.

The default region is needed only for numbers typed without a "+"; it is the
person's setting (Settings > Accounts), because nothing else can know it.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = ["DEFAULT_REGION", "normalise", "format_number", "digits_key", "same_number"]

#: Used for numbers typed without a country code until the person sets their own.
#: The owner is in the United Kingdom (order 1k, 2026-10-11).
DEFAULT_REGION = "GB"

_MIN_DIGITS = 7


def normalise(raw: str, region: str = DEFAULT_REGION) -> str:
    """E.164 (`+447700900123`) for `raw`, or `""` when it is not a real number."""
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        import phonenumbers  # noqa: PLC0415 - heavy import, only when a number is seen
        parsed = phonenumbers.parse(text, region or None)
        if phonenumbers.is_possible_number(parsed):
            return phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    except Exception:  # noqa: BLE001 - NumberParseException and a missing wheel alike
        pass
    return ""


def is_mobile(raw: str, region: str = DEFAULT_REGION) -> bool:
    """True only for a number the phone library places as a mobile (or a number that may be one,
    where the country does not tell mobile from fixed). A landline, a toll-free or an unplaceable
    number is False: those are shared by a whole office or household."""
    text = (raw or "").strip()
    if not text:
        return False
    try:
        import phonenumbers  # noqa: PLC0415
        parsed = phonenumbers.parse(text, region or None)
        if not phonenumbers.is_possible_number(parsed):
            return False
        kind = phonenumbers.number_type(parsed)
        return kind in (phonenumbers.PhoneNumberType.MOBILE,
                        phonenumbers.PhoneNumberType.FIXED_LINE_OR_MOBILE)
    except Exception:  # noqa: BLE001
        return False


def digits_key(raw: str) -> str:
    """The digits of `raw` when it is long enough to match on, else `""`."""
    digits = re.sub(r"\D", "", raw or "")
    return digits if len(digits) >= _MIN_DIGITS else ""


def match_key(raw: str, region: str = DEFAULT_REGION) -> str:
    """What two numbers are compared by: E.164 when there is one, else the digits."""
    return normalise(raw, region) or digits_key(raw)


def format_number(raw: str, style: str = "international", region: str = DEFAULT_REGION) -> Optional[str]:
    """`raw` in one format (`international` or `national`), or None when it will not parse."""
    text = (raw or "").strip()
    if not text:
        return None
    try:
        import phonenumbers  # noqa: PLC0415
        parsed = phonenumbers.parse(text, region or None)
        if not phonenumbers.is_possible_number(parsed):
            return None
        fmt = (phonenumbers.PhoneNumberFormat.NATIONAL if style == "national"
               else phonenumbers.PhoneNumberFormat.INTERNATIONAL)
        return phonenumbers.format_number(parsed, fmt)
    except Exception:  # noqa: BLE001
        return None


def same_number(a: str, b: str, region: str = DEFAULT_REGION) -> bool:
    ka, kb = match_key(a, region), match_key(b, region)
    return bool(ka) and ka == kb
