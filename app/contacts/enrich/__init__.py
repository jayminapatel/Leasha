"""Enrichment: proposals for a contact from the owner's own mail and, if switched on, the internet.

Layer: L7

Order 1k section 7. Every source returns `Proposal` objects and changes nothing;
`apply_proposals` writes the accepted ones. See `run.py` for the entry points.
"""

from __future__ import annotations

from app.contacts.enrich.base import ApplyResult, GuardedTransport, Proposal, apply_proposals
from app.contacts.enrich.history import History, contact_history, refresh_last_contacted
from app.contacts.enrich.run import (enrich_contact, enrich_selection, history_for, proposals_for,
                                     suggestions_for)
from app.contacts.enrich.suggest import SuggestedContact, accept_suggestions, suggest_contacts

__all__ = [
    "Proposal", "ApplyResult", "apply_proposals", "GuardedTransport", "enrich_contact", "enrich_selection",
    "proposals_for", "history_for", "suggestions_for", "History", "contact_history", "refresh_last_contacted",
    "SuggestedContact", "suggest_contacts", "accept_suggestions",
]
