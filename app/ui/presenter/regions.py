"""The countries a phone number or an address can be from, as a drop-down shows them. Qt-free.

Layer: L5. "default country code should show number and flag" (the owner, 2026-10-11): each row reads
`flag  United Kingdom  +44`, the two-letter code is what is stored, and typing a code, a name or a
dialling code finds the row.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

__all__ = ["Region", "regions", "flag", "row_text", "code_of", "country_names"]

from typing import NamedTuple


class Region(NamedTuple):
    code: str        # "GB"
    name: str        # "United Kingdom"
    dial: str        # "+44"


def flag(code: str) -> str:
    """The regional-indicator pair that fonts with flags draw as a flag; others show the two letters."""
    code = (code or "").upper()
    if len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code)


@lru_cache(maxsize=1)
def regions() -> tuple[Region, ...]:
    """Every region phone numbers are known for, by name."""
    import phonenumbers
    from phonenumbers import geocoder

    out = []
    for code in phonenumbers.SUPPORTED_REGIONS:
        example = phonenumbers.example_number(code)
        name = (geocoder.country_name_for_number(example, "en") if example else "") or code
        out.append(Region(code, name, f"+{phonenumbers.country_code_for_region(code)}"))
    return tuple(sorted(out, key=lambda r: r.name.casefold()))


def country_names() -> list[str]:
    return [r.name for r in regions()]


def row_text(r: Region) -> str:
    return f"{flag(r.code)}  {r.name}  {r.dial}".strip()


def code_of(text: str) -> Optional[str]:
    """The region a typed or chosen text means: a row as `row_text` makes it, a code, a name or a dialling code."""
    raw = (text or "").strip()
    if not raw:
        return None
    for r in regions():
        if raw == row_text(r):
            return r.code
    low = raw.casefold().lstrip("+")
    for r in regions():
        if low == r.code.casefold() or low == r.name.casefold():
            return r.code
    digits = low if low.isdigit() else ""
    if digits:
        hits = [r for r in regions() if r.dial == f"+{digits}"]
        for preferred in ("GB", "US", "CA", "AU"):
            for r in hits:
                if r.code == preferred:
                    return r.code
        return hits[0].code if hits else None
    return None
