r"""Every switch changes the search, and every notice has a plain sentence.

Layer: L4. The two §7 items that are guards rather than scenarios.

**The anti-P1 wiring rule.** A setting that is declared, shown in Settings and
consumed by nothing is this codebase's most-repeated defect: it was found in
seven tuning settings at once, and twice more during this order — a spelling
correction that reached the notice and not the query, and a translator asking
the store for kinds it does not have. `test_settings_are_used` catches a key
nobody reads; **this catches a field that is read and changes nothing.**

So each of the six behaviours is flipped, and the *response* has to differ.
Not the policy object, not a call count — the answer a person would see.
"""

from __future__ import annotations

import pathlib
import tempfile
import time

import pytest

from app.search.engine import (
    NOTICE_NO_VECTORS, NOTICE_RELAXED, NOTICE_SPELLING, SearchEngine,
)
from app.search.policy import BEHAVIOURS, SEARCH, for_surface

_DAY = 86_400 * 1_000_000_000


class _NoVectors:
    def search(self, *_args, **_kwargs):
        return []


class _NoModel:
    def embed(self, _text):
        raise RuntimeError("no embedding model in this test")

    def embed_all(self, _texts):
        raise RuntimeError("no embedding model in this test")


@pytest.fixture(scope="module")
def engine():
    """A corpus shaped so that every one of the six has something to do."""
    from app.storage.sqlite_store import SqliteStore

    store = SqliteStore(pathlib.Path(tempfile.mkdtemp()) / "policy.db").connect()
    now = time.time_ns()
    for name, text, days, digest in (
        # for typo_correction and relax_on_empty
        ("volcanoes.docx", "My homework about volcanoes and magma", 500, "a"),
        # for version_folding: same family, one with a marker
        ("report.docx", "The safety report for the Leeds site", 900, "b"),
        ("report v2.docx", "The safety report for the Leeds site, revised",
         10, "c"),
        # for recency_blend: two near-equal matches, different ages
        ("old-notes.txt", "Notes about magma and lava flows", 3000, "d"),
        ("new-notes.txt", "Notes about magma and lava flows", 1, "e"),
    ):
        file_id = store.upsert_file(
            f"C:/work/{name}", parent_dir="C:/work", ext=name.split(".")[-1],
            size_bytes=1, mtime_ns=int(now - days * _DAY), content_hash=digest,
            status="INDEXED", source_kind="file")
        store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])

    built = SearchEngine(store, _NoVectors(), _NoModel())
    yield built, store
    built.close()


def _with(**changes):
    return for_surface(SEARCH).with_overrides(**changes)


# --------------------------------------------------------------------------
# Each of the six, proved against the response
# --------------------------------------------------------------------------

def test_typo_correction_changes_the_query_that_runs(engine):
    """**And the query, not only the notice.** That distinction is the bug
    the scenario tests found: the correction reached the message and left the
    search as typed."""
    built, _store = engine
    on = built.search("volcanno", policy=_with(typo_correction="auto"),
                      use_cache=False)
    off = built.search("volcanno", policy=_with(typo_correction="off"),
                       use_cache=False)
    assert on.parsed.fts_match() == '"volcano"'
    assert off.parsed.fts_match() == '"volcanno"'
    assert on.results and not off.results


def test_relax_on_empty_changes_whether_anything_comes_back(engine):
    built, _store = engine
    on = built.search("volcanoes type:pdf", policy=_with(relax_on_empty=True),
                      use_cache=False)
    off = built.search("volcanoes type:pdf", policy=_with(relax_on_empty=False),
                       use_cache=False)
    assert on.results and not off.results
    assert any(n.code == NOTICE_RELAXED for n in on.notices)
    assert not any(n.code == NOTICE_RELAXED for n in off.notices)


def test_recency_blend_changes_the_order(engine):
    """Two documents with the same words and three thousand days between
    them."""
    built, _store = engine
    on = built.search("lava flows", policy=_with(recency_blend=True),
                      use_cache=False)
    off = built.search("lava flows", policy=_with(recency_blend=False),
                       use_cache=False)
    assert on.results[0].path.endswith("new-notes.txt")
    assert [r.path for r in on.results] != [r.path for r in off.results]


def test_version_folding_changes_how_many_rows_are_drawn(engine):
    built, _store = engine
    on = built.search("safety report Leeds", policy=_with(version_folding=True),
                      use_cache=False)
    off = built.search("safety report Leeds", policy=_with(version_folding=False),
                       use_cache=False)
    assert len(on.folds) < len(off.folds)
    assert any(fold.folded for fold in on.folds)
    assert not any(fold.folded for fold in off.folds)


def test_notice_register_changes_the_words(engine):
    built, _store = engine
    plain = built.search("volcanoes", policy=_with(notice_register="plain"),
                         use_cache=False)
    technical = built.search("volcanoes",
                             policy=_with(notice_register="technical"),
                             use_cache=False)

    def message(response):
        return next((n.message for n in response.notices
                     if n.code == NOTICE_NO_VECTORS), "")

    assert message(plain) and message(technical)
    assert message(plain) != message(technical)


def test_auto_chips_changes_whether_filters_are_offered(engine):
    """**Read on the presenter side**, because the retrieval path may not
    know translation exists - but the *policy* still decides, which is what
    keeps the rule in one place."""
    from app.ui.presenter import chips_for

    _built, store = engine
    assert chips_for(store, "the report from 2024", _with(auto_chips=True))
    assert chips_for(store, "the report from 2024", _with(auto_chips=False)) == ()


def test_explain_results_changes_whether_a_row_can_say_why(engine):
    """**The seventh behaviour, and the only one that changes nothing about
    the search.** It adds an explanation beside a result, so what it must
    provably change is whether that explanation exists at all."""
    from app.ui.presenter import explain_for

    built, _store = engine
    response = built.search("volcanoes", policy=_with(explain_results=True),
                            use_cache=False)
    result = response.results[0]

    # **Through `explain_for`, not `why_result`.** The first version of this
    # test branched on the policy in its own helper and then asserted the
    # branch - a tautology that would have passed with the switch wired to
    # nothing at all, which is the exact defect this file exists to catch.
    assert explain_for(result, response.parsed, _with(explain_results=True))
    assert explain_for(result, response.parsed,
                       _with(explain_results=False)) == ()


def test_every_behaviour_in_the_table_is_proved_above():
    r"""**The guard on the guard.** A seventh behaviour added to the policy
    with no test here would be a switch nobody proved does anything - which
    is the defect this whole file exists to prevent, arriving by the back
    door.
    """
    proved = {
        "typo_correction", "relax_on_empty", "recency_blend",
        "version_folding", "notice_register", "auto_chips",
        "explain_results",
    }
    assert {name for name, _label, _help in BEHAVIOURS} == proved


# --------------------------------------------------------------------------
# Notices: every code has a plain sentence
# --------------------------------------------------------------------------

def test_every_notice_code_has_a_plain_form():
    r"""**A notice with no plain form falls through to the technical one**,
    which is the failure this table exists to remove: the everyday tab would
    quietly start naming commands at somebody who has never seen one.
    """
    import app.search.engine as engine_module
    from app.search.plain_notices import PLAIN

    codes = {value for name, value in vars(engine_module).items()
             if name.startswith("NOTICE_") and isinstance(value, str)}
    missing = sorted(codes - set(PLAIN))
    assert not missing, (
        f"These notice codes have no plain-register sentence: {missing}. "
        f"Add one to `plain_notices.PLAIN` - the everyday tab shows whatever "
        f"is there, and what is there today is written for a developer.")


def test_the_table_names_no_code_that_no_longer_exists():
    """Dead weight in the other direction: an entry for a notice nothing
    emits is a sentence nobody will ever read, and it hides the fact that the
    code was removed."""
    import app.search.engine as engine_module
    from app.search.plain_notices import PLAIN

    codes = {value for name, value in vars(engine_module).items()
             if name.startswith("NOTICE_") and isinstance(value, str)}
    assert not sorted(set(PLAIN) - codes)


@pytest.mark.parametrize("code", sorted(
    value for name, value in vars(__import__(
        "app.search.engine", fromlist=["x"])).items()
    if name.startswith("NOTICE_") and isinstance(value, str)))
def test_the_plain_form_speaks_english(code):
    """No operator syntax, no command names, no `snake_case`. The register is
    the whole point of the table."""
    from app.search.plain_notices import PLAIN, plain_message

    entry = PLAIN[code]
    sentence = entry("a test message") if callable(entry) else entry
    assert sentence and isinstance(sentence, str)
    for jargon in ("app.cli", "--", "()", "_"):
        assert jargon not in sentence, f"{code} says {jargon!r} to somebody"
    assert plain_message(code, "a test message")
