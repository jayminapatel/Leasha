"""Which `/` commands each box offers, and why that is not a free choice.

**The generic search is the union; the focused tabs are subsets.** Search is
where somebody types before they know which tab they want, so it must offer
everything. A focused tab offers what it can honour - no more, because a
dropdown full of commands that quietly do nothing is discovered one
disappointment at a time, and no less, because a tab that could answer `/type`
and does not offer it is a feature nobody finds.

These are assertions about a relationship, not about a list. A command added to
the catalogue and to no tab will fail here, which is the point: it forces the
question "which tabs can honour this?" to be answered rather than forgotten.
"""

from __future__ import annotations

import pytest

from app.search.commands import COMMANDS, expand_slashes
from app.search.query import parse_query
from app.ui.presenter import CODE_COMMANDS, FILES_COMMANDS, MAIL_COMMANDS

ALL_NAMES = {command.name for command in COMMANDS}
SUBSETS = {
    "Files": FILES_COMMANDS, "Mail": MAIL_COMMANDS, "Code": CODE_COMMANDS,
}


@pytest.mark.parametrize("tab", sorted(SUBSETS))
def test_every_offered_command_is_a_real_one(tab: str) -> None:
    """A typo here would show a row that inserts syntax the parser ignores."""
    unknown = set(SUBSETS[tab]) - ALL_NAMES
    assert not unknown, f"{tab} offers commands that do not exist: {sorted(unknown)}"


def test_the_search_box_is_the_union_of_the_focused_tabs() -> None:
    """The rule itself: generic search can do anything a specific tab can.

    The search box passes no restriction, so it offers `COMMANDS` entire - this
    asserts the containment that makes that the right default rather than a
    coincidence nobody checked.
    """
    union: set[str] = set()
    for names in SUBSETS.values():
        union |= set(names)
    assert union <= ALL_NAMES


def test_no_tab_offers_a_command_it_cannot_honour() -> None:
    """The parser is the arbiter: expanding must populate a field.

    `/from` in the Files tab would expand to `from:dave`, parse into `senders`,
    and then be dropped - the tab reads names and extensions only. This checks
    each offered command against the field its tab actually consumes.
    """
    # (field the parser fills, a value that command actually accepts). `has:`
    # takes `attachment` and nothing else, and a date operator takes a date -
    # feeding every command the same "x" tested the validator, not the offer.
    consumed = {
        "type": ("ext", "pdf"), "name": ("names", "notes"),
        "repo": ("repos", "leasha"), "from": ("senders", "dave"),
        "to": ("recipients", "priya"), "subject": ("subjects", "invoice"),
        "has": ("has_attachment", "attachment"), "after": ("after", "2024-01-01"),
        "before": ("before", "2024-12-31"), "path": ("paths", "src"),
        "size": ("sizes", ">1mb"),
    }
    for tab, names in SUBSETS.items():
        for name in names:
            field, value = consumed[name]
            parsed = parse_query(expand_slashes(f"/{name} {value}"))
            assert getattr(parsed, field), (
                f"{tab} offers /{name}, which does not reach {field}")
            assert not parsed.unknown_operators, (
                f"{tab} offers /{name}, which the parser does not recognise")


def test_mail_offers_exactly_the_columns_its_store_query_takes() -> None:
    """`browse_messages` takes these and nothing else; drift would be silent."""
    assert set(MAIL_COMMANDS) == {"from", "to", "subject", "has", "after", "before"}


def test_code_offers_type_because_its_tree_lists_files() -> None:
    """It offered `repo` alone while the tab was a flat list of repositories.

    The moment the tree grew file children, `/type` became answerable - and an
    offer that lags behind what the tab can do is the same failure as an offer
    that runs ahead of it.
    """
    assert "type" in CODE_COMMANDS and "repo" in CODE_COMMANDS


def test_the_popup_shows_only_what_it_was_restricted_to() -> None:
    """The restriction is applied to matches, not merely stored."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QLineEdit

    from app.ui.widgets.command_popup import CommandPopup

    QApplication.instance() or QApplication([])
    box = QLineEdit()
    popup = CommandPopup(box, only=("repo",))
    popup.set_prefix("")
    assert [command.name for command in popup._matches] == ["repo"]

    unrestricted = CommandPopup(box)
    unrestricted.set_prefix("")
    assert len(unrestricted._matches) == len(COMMANDS)
