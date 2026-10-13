"""5e: details from documents - org charts, contracts, scanned business cards. Not built yet, on purpose.

Layer: L7

Order 1s section 5e says this source waits "only after section 9 shows the others hold their
precision": a title read out of a contract is wrong more often than one from a signature, and a wrong
title offered is worse than none (the order's "Precision before reach"). Until section 9's precision
check has passed for the signature, vCard, LinkedIn and calendar sources, this is the switch-gated
place it will live and it offers nothing. When it is built: proposals at most 0.6, never pre-ticked.

`AccountSettings.enrich_documents` is off by default; `run.enrich_contact` does not call this at all
while it is off.
"""

from __future__ import annotations

from typing import Any

from app.contacts.card import Card
from app.contacts.enrich.base import Proposal

__all__ = ["document_proposals", "MAX_CONFIDENCE"]

#: The order's ceiling for this source, kept here so the builder of 5e starts from it.
MAX_CONFIDENCE = 0.6


def document_proposals(store: Any, cid: int, card: Card) -> list[Proposal]:
    """Nothing, until section 9's precision check lets this source be built (see the module docstring)."""
    return []
