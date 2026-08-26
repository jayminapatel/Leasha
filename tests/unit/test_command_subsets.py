r"""Which `/` commands each box offers, and why that is not a free choice.

**Every box offers every switch, and that is the change.** The rule has not
moved - a tab offers what it can honour, no more and no less - but the answer
to "what can this tab honour" has, because the tabs stopped each writing their
own filtering. `storage/filters.file_filter_sql` is now the single definition of
what a switch means, and each tab composes it, so there is no switch any tab has
to decline.

What that fixes is the owner's report: *"the switches Search should have all
switches, files should have all switches (files, Mail and Code), mail should
have mail switches, code same switches has files - this is not the case, and the
results should be same across but only applicable to the tab"*. Files offered
two of eleven; Code offered three and silently ignored two of those.

The half of the rule that still bites is the second one. A command offered and
not honoured is discovered one disappointment at a time, so the tests below
check each tab against the function that actually runs - not against the parser,
which was the gap that let `/name` and `/path` sit in the Code menu doing
nothing for as long as they did.
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
    """The rule itself: generic search can do anything a specific tab can."""
    union: set[str] = set()
    for names in SUBSETS.values():
        union |= set(names)
    assert union <= ALL_NAMES


def test_the_search_box_offers_the_repository_switches_too() -> None:
    r"""*"Search should have all switches"* - including the git ones.

    It used to pass no catalogue at all, so it got `COMMANDS` and stopped at
    eleven. It now gets the same list the Code box uses, and that is honest only
    because the box can answer them: `app/search/federate.py` runs the
    repository half on the full tier. Offering them before that existed would
    have been the failure this file is about, committed deliberately.
    """
    from app.search.gitquery import GIT_COMMANDS
    from app.ui.widgets.code_commands import ALL_CATALOGUE

    offered = {command.name for command in ALL_CATALOGUE}

    assert ALL_NAMES <= offered
    assert "history" in offered and "branch" in offered and "author" in offered
    # Every git switch whose spelling the index does not already claim.
    claimed = {spelling for command in COMMANDS for spelling in command.spellings}
    for command in GIT_COMMANDS:
        if not any(spelling in claimed for spelling in command.spellings):
            assert command.name in offered, f"/{command.name} is not offered anywhere"


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


def test_every_tab_offers_every_switch() -> None:
    r"""The rule, stated once.

    Not an aspiration: it holds because `file_filter_sql` defines each switch
    once and all three tabs compose it. If a future switch cannot be answered
    on some tab, this fails and the honest fix is to make that tab answer it or
    to say plainly why it cannot - which is the conversation the old subsets
    quietly avoided by shrinking the menu instead.
    """
    for tab, names in SUBSETS.items():
        assert set(names) == ALL_NAMES, f"{tab} is missing {sorted(ALL_NAMES - set(names))}"


def test_mail_still_uses_its_own_columns_for_the_mail_fields() -> None:
    r"""**Offering everything must not mean answering everything the same way.**

    `browse_messages` handles `from`, `to`, `subject` and `has` natively, on the
    trigram header index, and `after`/`before` against `m.sent_at`. The shared
    fragment must therefore arrive with those cleared - otherwise a date filter
    would land on the file's mtime, which for a PST is when the whole archive
    last changed and would match every message in it identically.
    """
    from app.ui.presenter import mail_filters

    filters = mail_filters(parse_query(expand_slashes(
        "/from dave /after 2024-01-01 /type msg /path 2024")))

    assert filters["sender"] == "dave"
    assert "after" in filters                     # on sent_at, natively
    where = filters.get("file_where", "")
    assert "f.ext" in where and "f.path" in where  # the file-level half
    assert "sender" not in where and "mtime_ns" not in where


def test_code_narrows_by_name_and_path_rather_than_merely_offering_them() -> None:
    r"""**The gap the old contract could not see.**

    `test_no_tab_offers_a_command_it_cannot_honour` asks whether the *parser*
    fills a field. It does, for `/name` and `/path`, and had for as long as the
    Code menu had offered them - while `CodeRoute` carried neither, so both
    narrowed nothing and looked healthy doing it. This asks the filter that
    actually runs.
    """
    from app.ui.presenter import git_rows_matching

    rows = [{"path": "src/OrderService.cs"}, {"path": "service/main.py"}]

    assert [r["path"] for r in git_rows_matching(rows, paths=("service",))] == [
        "service/main.py"]
    assert [r["path"] for r in git_rows_matching(rows, names=("service",))] == [
        "src/OrderService.cs"]


def test_the_popup_shows_only_what_it_was_restricted_to() -> None:
    """The restriction is applied to matches, not merely stored."""
    pytest.importorskip("PyQt6")
    from PyQt6.QtWidgets import QApplication, QLineEdit

    from app.ui.widgets.command_popup import CommandPopup

    # Held, not discarded: a QApplication nobody references is collected, and
    # the next widget built in this process aborts. See the session fixture in
    # `tests/conftest.py`.
    app = QApplication.instance() or QApplication([])
    assert app is not None
    box = QLineEdit()
    popup = CommandPopup(box, only=("repo",))
    popup.set_prefix("")
    assert [command.name for command in popup._matches] == ["repo"]

    unrestricted = CommandPopup(box)
    unrestricted.set_prefix("")
    assert len(unrestricted._matches) == len(COMMANDS)
