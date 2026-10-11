"""Answer a question about a person in the contact book, from the book - no model, no guess.

Layer: L7. "Does Search and Chat include contacts?" (the owner, 2026-10-12). A question such as "what is Zane's
number" or "who is Hiren Shethna" is answered from the contact card when the name is a contact; a question that
is not about a person in the book is left alone for the usual search. The words say where it came from.
"""

from __future__ import annotations

import re
from typing import Any, Optional

__all__ = ["contact_answer"]

#: What the question asks of a person: only then is the book consulted.
_INTENT = re.compile(
    r"\b(phone|number|mobile|cell|call|email|e-mail|address|contact|company|works?|job|title|"
    r"who is|who's|details|birthday|website|linkedin)\b", re.IGNORECASE)
_STOP = frozenset("""what whats what's is are the a an of for to me my his her their number phone mobile cell email
e-mail address contact company work works job title details who whos who's does do you have get give tell show find
please can could where how which and or in at on birthday website linkedin call""".split())


def contact_answer(store: Any, question: str, *, limit: int = 3) -> Optional[str]:
    """The card details of the people the question names, or None when it names nobody in the book."""
    text = " ".join(str(question or "").split())
    if not text or not _INTENT.search(text):
        return None
    words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", text) if w.lower().strip("'s") not in _STOP
             and w.lower() not in _STOP]
    names = [w.strip("'s") if w.lower().endswith("'s") else w for w in words]
    if not names:
        return None
    try:
        from app.contacts.hub import ContactHub  # noqa: PLC0415

        hub = ContactHub(store)
        hits = hub.search(" ".join(names), limit=10)
        asked = {w.lower().strip("'s") if w.lower().endswith("'s") else w.lower()
                 for w in re.findall(r"[A-Za-z][A-Za-z'\-]+", text)}
        # A name in the book counts when one of its words (a first name, a surname) is a word of the question.
        hits = [h for h in hits if any(p.lower().strip(",.") in asked for p in h.display_name.split()
                                       if len(p) > 2)][:limit]
        if not hits:
            return None
        lines = []
        for hit in hits:
            card = hub.get_card(hit.id)
            if card is None:
                continue
            from app.contacts.card import display_name  # noqa: PLC0415

            head = display_name(card)
            work = ", ".join(x for x in (card.get("title"), card.get("company")) if x)
            lines.append(f"**{head}**" + (f" - {work}" if work else ""))
            for kind, word in (("phone", "Phone"), ("email", "Email"), ("address", "Address"), ("url", "Web")):
                for f in card.values(kind)[:4]:
                    lines.append(f"- {word}{' (' + f.label + ')' if f.label else ''}: {f.value}")
        if not lines:
            return None
        return "From your contacts:\n\n" + "\n".join(lines)
    except Exception:                                    # noqa: BLE001 - a contact lookup never breaks a chat
        return None
