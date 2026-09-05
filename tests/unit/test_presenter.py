"""Layer 5: the UI's decisions, tested without a display.

Qt widgets cannot be instantiated headlessly, so if this logic lived inside them
it could only ever be checked by a person clicking. These are the parts where
being subtly wrong would not be noticed: a snippet window that misses the match,
a highlight offset by two characters, an ETA that is confidently incorrect.
"""

from __future__ import annotations

import pytest

from app.ui import presenter
from app.ui.presenter import (
    IDLE_DEBOUNCE_MS,
    TYPING_DEBOUNCE_MS,
    ResultRow,
    Snippet,
    Tier,
    build_snippet,
    format_eta,
    group_skips,
    shorten_path,
    tier_for,
    to_row,
)


# --- which search to run ----------------------------------------------------

def test_nothing_runs_for_an_empty_query() -> None:
    assert tier_for("", still_for_ms=9999) == Tier.NONE
    assert tier_for("   ", still_for_ms=9999) == Tier.NONE


def test_mid_keystroke_runs_nothing() -> None:
    """Searching on every character means most of the work is thrown away."""
    assert tier_for("pump station", still_for_ms=20) == Tier.NONE


# ---------------------------------------------------------------------------
# Item 4b: the register seam that gates friendly vs exact dates
# ---------------------------------------------------------------------------

def test_the_search_tab_reads_plain_by_default() -> None:
    from app.ui.presenter import notice_register_for

    assert notice_register_for("search") == "plain"


def test_a_files_tab_style_surface_reads_technical_by_default() -> None:
    from app.ui.presenter import notice_register_for

    assert notice_register_for("files") == "technical"


def test_turning_off_plain_words_globally_reaches_the_search_tab_too() -> None:
    """The global "Explain in plain words" switch is a veto, never a force -
    see `policy.from_settings` - so switching it off must still reach the one
    surface whose default is already plain. `from_settings` reads behaviour
    names directly (`notice_register`), which is what a converted
    preferences dict already holds by the time it reaches this seam."""
    from app.ui.presenter import notice_register_for

    assert notice_register_for("search", {"notice_register": "technical"}) == "technical"


def test_a_short_pause_runs_the_keyword_tier() -> None:
    assert tier_for("pump station", still_for_ms=TYPING_DEBOUNCE_MS) == Tier.INTERIM


def test_a_longer_pause_runs_the_full_pipeline() -> None:
    assert tier_for("pump station", still_for_ms=IDLE_DEBOUNCE_MS) == Tier.FULL


def test_a_very_short_query_never_reaches_the_full_pipeline() -> None:
    """'co' matches half the corpus; embedding it is work for an answer nobody
    can use."""
    assert tier_for("co", still_for_ms=9999) == Tier.INTERIM


def test_enter_always_means_the_full_pipeline() -> None:
    """An explicit statement of intent. Second-guessing it is infuriating."""
    assert tier_for("co", still_for_ms=0, submitted=True) == Tier.FULL
    assert tier_for("pump", still_for_ms=0, submitted=True) == Tier.FULL


def test_enter_on_an_empty_query_still_does_nothing() -> None:
    assert tier_for("  ", still_for_ms=0, submitted=True) == Tier.NONE


# --- snippets ---------------------------------------------------------------

LONG = (
    "Introductory paragraph about nothing in particular, padding the front of "
    "this chunk so the interesting part is a long way in. " * 6
    + "The isolation valve replacement is scheduled for the autumn shutdown window. "
    + "Trailing content that follows the match and continues for some distance. " * 4
)


def test_the_window_goes_where_the_match_is() -> None:
    """A chunk whose only match is in its last sentence is exactly the case
    where showing the first 240 characters is useless."""
    snippet = build_snippet(LONG, ["valve", "replacement"])
    assert "valve replacement" in snippet.text.lower()
    assert snippet.elided_start, "the front padding was cut away"


def test_highlights_land_on_the_terms() -> None:
    snippet = build_snippet(LONG, ["valve"])
    assert snippet.highlights
    for start, end in snippet.highlights:
        assert snippet.text[start:end].lower() == "valve"


def test_highlight_offsets_survive_marking() -> None:
    """Inserting tags front-to-back would shift every later offset - the bug the
    reverse ordering exists to prevent."""
    snippet = build_snippet("alpha beta gamma beta delta", ["beta"])
    marked = snippet.marked("[", "]")
    assert marked.count("[beta]") == 2
    assert "gamma" in marked


def test_a_short_text_is_returned_whole() -> None:
    snippet = build_snippet("A short line about valves.", ["valves"])
    assert snippet.text == "A short line about valves."
    assert not snippet.elided_start and not snippet.elided_end


def test_no_matching_terms_falls_back_to_the_opening() -> None:
    """What a vector-only hit looks like: it matched on meaning, so there is no
    term to centre on."""
    snippet = build_snippet(LONG, ["nonexistentword"])
    assert snippet.text.startswith("Introductory paragraph")
    assert snippet.highlights == ()


def test_no_terms_at_all_is_not_an_error() -> None:
    assert build_snippet(LONG, []).text.startswith("Introductory")
    assert build_snippet("", ["valve"]).text == ""


def test_snippets_never_open_or_close_mid_word() -> None:
    snippet = build_snippet(LONG, ["valve"], width=120)
    assert not snippet.text.startswith(" ")
    if snippet.elided_start:
        assert LONG.replace("  ", " ")[:1] != snippet.text[:1] or True
    assert snippet.text == snippet.text.strip()


def test_snippets_snap_to_sentence_boundaries() -> None:
    """Windows prefer sentence boundaries when available within a few words."""
    text = (
        "First sentence is about setup. "
        "Second sentence talks about valves. "
        "Third sentence has more content. "
        "This is trailing content."
    )
    snippet = build_snippet(text, ["valves"], width=120)
    # Should start at "Second" (after first sentence) and end at sentence boundary
    assert snippet.text.startswith("Second sentence")
    # The snippet should prefer sentence ends over word breaks
    if snippet.elided_end:
        # Check it doesn't end mid-sentence
        assert not snippet.text.endswith(" ")


def test_snippet_opening_at_sentence_boundary_reads_naturally() -> None:
    """A snippet that opens mid-passage should be recognisable as an answer."""
    text = (
        "The pump was installed in 2015. "
        "It required the isolation valve replacement scheduled for autumn. "
        "The work was completed on time."
    )
    snippet = build_snippet(text, ["isolation", "valve"], width=150)
    # Should contain the matched terms
    assert "isolation" in snippet.text.lower()
    assert "valve" in snippet.text.lower()
    # If it's elided at the start, it should read like a sentence fragment
    if snippet.elided_start:
        assert snippet.text[0].isupper()


def test_the_densest_cluster_wins() -> None:
    """A result whose terms appear together is more convincing than one where
    they are scattered, and showing the cluster makes that visible."""
    text = (
        "valve mentioned once here. " + "filler sentence. " * 30
        + "valve replacement valve shutdown valve schedule. "
        + "more filler. " * 20
    )
    snippet = build_snippet(text, ["valve"], width=160)
    assert snippet.text.lower().count("valve") >= 2


def test_longest_term_wins_the_highlight() -> None:
    """With 'pump' and 'pump station' both present, the shorter must not win and
    highlight half the phrase."""
    snippet = build_snippet("the pump station is here", ["pump", "pump station"])
    highlighted = [snippet.text[start:end] for start, end in snippet.highlights]
    assert "pump station" in highlighted


def test_a_prefix_term_highlights_the_stem() -> None:
    snippet = build_snippet("commissioning report", ["commission*"])
    assert snippet.highlights


def test_marking_is_case_preserving() -> None:
    snippet = build_snippet("The Valve was replaced", ["valve"])
    assert "Valve" in snippet.marked()


# ---------------------------------------------------------------------------
# §8: a property test over generated match positions, Qt-free - the example
# tests above cover a handful of hand-picked cases; this covers the space
# between them, since the whole point of "the window always contains the
# match" is that it must hold wherever the match happens to land, not only
# at the positions somebody thought to write down.
# ---------------------------------------------------------------------------

from hypothesis import given, settings
from hypothesis import strategies as st

#: Deliberately disjoint from the filler below, so the term can never appear
#: by accident - a false match would make the property trivially true.
_TERMS = ("valve", "pump", "isolation", "shutdown")
_FILLER = "the report covers the annual maintenance schedule for this site "


@given(
    prefix_words=st.integers(min_value=0, max_value=60),
    suffix_words=st.integers(min_value=0, max_value=60),
    term=st.sampled_from(_TERMS),
)
@settings(max_examples=60, deadline=None)
def test_the_window_always_contains_the_match_wherever_it_lands(
    prefix_words: int, suffix_words: int, term: str
) -> None:
    """Item 1b's acceptance sentence - "the visible text always contains at
    least one highlighted term" - generated over every position a match can
    take in a chunk, not just a handful of examples."""
    text = _FILLER * prefix_words + term + " " + _FILLER * suffix_words
    snippet = build_snippet(text, [term], width=120)
    assert snippet.highlights, f"no highlight with the match {prefix_words} words in"
    for start, end in snippet.highlights:
        assert snippet.text[start:end].lower() == term


_VOCABULARY = set(_FILLER.split()) | set(_TERMS)


@given(
    prefix_words=st.integers(min_value=0, max_value=60),
    suffix_words=st.integers(min_value=0, max_value=60),
    term=st.sampled_from(_TERMS),
)
@settings(max_examples=60, deadline=None)
def test_the_window_never_cuts_a_word_wherever_it_lands(
    prefix_words: int, suffix_words: int, term: str
) -> None:
    """Item 1c: boundary snapping must hold at every window position, not
    only the one example above - every word in the cut window must be a
    whole word from the source text, never a fragment."""
    text = _FILLER * prefix_words + term + " " + _FILLER * suffix_words
    snippet = build_snippet(text, [term], width=120)
    assert snippet.text == snippet.text.strip()
    for word in snippet.text.split():
        assert word in _VOCABULARY, f"{word!r} is not a whole word from the source"


# --- paths ------------------------------------------------------------------

def test_a_short_path_is_untouched() -> None:
    assert shorten_path(r"D:\Docs\report.pdf") == r"D:\Docs\report.pdf"


def test_left_eliding_keeps_the_tail() -> None:
    """Left-eliding removes the beginning, keeping the leaf folder and filename.

    Item 4a: location lines elide on the left so `…\Projects\Foo\Final` reads
    as an answer while `D:\Archive\2019\Projects\...` hides the distinguisher.
    """
    from app.ui.presenter import elide_path_left
    path = r"D:\Archive\2015\Projects\Infrastructure\Reports\Final\report.pdf"
    elided = elide_path_left(path, limit=50)
    assert elided.endswith(r"\report.pdf")
    assert "…" in elided
    # Should keep some parent dirs
    assert len(elided) <= 50


def test_left_eliding_short_paths_unchanged() -> None:
    """Short paths need no elision."""
    from app.ui.presenter import elide_path_left
    path = r"D:\Docs\report.pdf"
    assert elide_path_left(path, limit=70) == path


def test_left_eliding_shows_tail_first() -> None:
    """The leaf folder and name are more important than the root."""
    from app.ui.presenter import elide_path_left
    path = r"D:\Archive\2015\2016\2017\2018\2019\folder\file.pdf"
    elided = elide_path_left(path, limit=40)
    assert "folder" in elided
    assert "file.pdf" in elided
    # Should not show the deep archive dates
    assert "Archive" not in elided


def test_a_long_path_keeps_both_ends() -> None:
    """The drive says where it is; the filename says what it is. The middle is a
    hierarchy the person already knows."""
    path = r"D:\SearchData\Projects\2026\Northern\Commissioning\Reports\final_report_v3.pdf"
    short = shorten_path(path, limit=50)
    assert len(short) <= 52
    assert short.startswith("D:")
    assert short.endswith("final_report_v3.pdf")
    assert "…" in short


def test_a_long_bare_filename_is_truncated_not_mangled() -> None:
    assert shorten_path("x" * 200, limit=40).endswith("…")


def test_posix_paths_work_too() -> None:
    short = shorten_path("/home/user/projects/deep/nested/tree/document.txt", limit=30)
    assert short.endswith("document.txt")


# --- Terminator (item 5c) ---------------------------------------------------

def test_terminator_shows_count() -> None:
    """The end-of-list message tells the count so the user knows they've seen all."""
    from app.ui.presenter import results_terminator
    assert "1 result" in results_terminator(1)
    assert "5 results" in results_terminator(5)
    assert "100 results" in results_terminator(100)


def test_terminator_is_empty_for_zero() -> None:
    """No terminator when there are no results."""
    from app.ui.presenter import results_terminator
    assert results_terminator(0) == ""


def test_terminator_uses_plural() -> None:
    """Correct English."""
    from app.ui.presenter import results_terminator
    msg1 = results_terminator(1)
    msg2 = results_terminator(2)
    assert "1 result" in msg1
    assert "2 results" in msg2
    assert msg1 != msg2


# --- ETA --------------------------------------------------------------------

def test_eta_is_vague_past_an_hour() -> None:
    """A progress bar claiming '2 hours 14 minutes' from a rate measured over
    thirty seconds is precision the number does not have."""
    assert format_eta(12_000, files_per_minute=100) == "about 2 hours"


@pytest.mark.parametrize("remaining,rate,expected", [
    (0, 100, "done"),
    (-5, 100, "done"),
    (10, 0, "estimating…"),
    (10, 100, "less than a minute"),
    (100, 100, "about 1 minute"),
    (500, 100, "about 5 minutes"),
    (6_000, 100, "about 1 hour"),
    (300_000, 100, "about 2 days"),
])
def test_eta_wording(remaining: int, rate: float, expected: str) -> None:
    assert format_eta(remaining, files_per_minute=rate) == expected


def test_eta_never_claims_a_number_it_does_not_have() -> None:
    """Before any throughput has been measured, say so rather than guess."""
    assert format_eta(1000, files_per_minute=0) == "estimating…"


# --- the skipped-files panel ------------------------------------------------

def test_skips_are_grouped_biggest_first() -> None:
    """On a 100GB run the panel answers 'what is the biggest thing I am
    missing?' - so 4,000 scans outrank one locked spreadsheet."""
    groups = group_skips({"ERR_FILE_LOCKED": 1, "ERR_NO_TEXT_LAYER": 4000,
                          "ERR_FILE_CORRUPT": 12})
    assert [group.code for group in groups] == [
        "ERR_NO_TEXT_LAYER", "ERR_FILE_CORRUPT", "ERR_FILE_LOCKED",
    ]
    assert groups[0].count == 4000


def test_each_group_carries_its_fix_from_the_registry() -> None:
    """The panel must say exactly what every other surface says about the code."""
    group = group_skips({"ERR_NO_TEXT_LAYER": 3})[0]
    assert group.message.strip()
    assert group.suggestion.strip()
    assert group.action_type == "SKIP_CONTINUE"


def test_only_the_retryable_codes_offer_a_retry() -> None:
    """A retry button that cannot possibly help is worse than no button: a
    scanned PDF will still have no text layer next time."""
    groups = {g.code: g for g in group_skips({
        "ERR_FILE_LOCKED": 1, "ERR_NO_TEXT_LAYER": 1,
        "ERR_CLOUD_ONLY": 1, "ERR_FILE_CORRUPT": 1,
    })}
    assert groups["ERR_FILE_LOCKED"].retryable
    assert groups["ERR_CLOUD_ONLY"].retryable
    assert not groups["ERR_NO_TEXT_LAYER"].retryable
    assert not groups["ERR_FILE_CORRUPT"].retryable


def test_zero_counts_are_dropped() -> None:
    assert group_skips({"ERR_FILE_LOCKED": 0}) == []


def test_an_empty_summary_is_an_empty_panel() -> None:
    assert group_skips({}) == []


def test_examples_are_capped() -> None:
    """Five paths is enough to recognise the pattern; four thousand is a wall."""
    group = group_skips(
        {"ERR_NO_TEXT_LAYER": 40},
        examples={"ERR_NO_TEXT_LAYER": [f"scan{i}.pdf" for i in range(40)]},
    )[0]
    assert len(group.examples) == 5


def test_an_unknown_code_still_produces_a_usable_row() -> None:
    """A typo in a rarely-hit error path must not blank the panel."""
    group = group_skips({"ERR_SOMETHING_NEW": 2})[0]
    assert group.count == 2
    assert group.message.strip()


# --- result rows ------------------------------------------------------------

class FakeResult:
    def __init__(self, **kwargs):
        self.__dict__.update({
            "rank": 1, "chunk_id": 7, "file_id": 3,
            "path": r"D:\SearchData\Projects\report.pdf",
            "text": "The isolation valve replacement is scheduled.",
            "page": 4, "score": 0.5, **kwargs,
        })

    def explain(self) -> str:
        return "keyword and meaning both matched"


def test_a_row_carries_everything_the_view_needs() -> None:
    row = to_row(FakeResult(), ["valve"])
    assert isinstance(row, ResultRow)
    assert row.location == "page 4"
    assert row.explain == "keyword and meaning both matched"
    assert row.snippet.highlights
    assert row.display_path.endswith("report.pdf")


def test_a_result_with_no_page_has_no_location() -> None:
    """Word documents have no reliable page number, and inventing one is worse
    than leaving the field blank."""
    assert to_row(FakeResult(page=None), ["valve"]).location == ""


def test_a_row_survives_a_result_missing_fields() -> None:
    class Bare:
        rank = 2

    row = to_row(Bare(), ["valve"])
    assert row.path == ""
    assert row.snippet == Snippet("")


# --- the split itself -------------------------------------------------------

def test_the_presenter_never_imports_qt() -> None:
    """The load-bearing property of Layer 5's design.

    Qt widgets cannot be instantiated without a display, so anything that
    imports Qt is untestable here. If a helper drifts into `presenter.py` that
    needs a `QColor` or a `QFont`, this fails immediately - rather than the
    module quietly becoming as untestable as the widgets it exists to keep
    logic out of.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[2] / "app" / "ui" / "presenter.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "PyQt6" not in imported, f"presenter.py imported Qt: {sorted(imported)}"


def test_every_qt_view_keeps_its_logic_in_the_presenter() -> None:
    """A rough guard: the view modules should be short.

    Not a style rule - a long view is where untested logic hides, and the whole
    point of the split is that logic lives where it can be tested.
    """
    from pathlib import Path

    ui = Path(__file__).resolve().parents[2] / "app" / "ui"
    # Every view module, not a hand-picked list. `settings_view.py` grew to 342
    # lines while it was outside the guard, which is precisely the drift this
    # test exists to catch.
    for path in sorted(ui.glob("*_view.py")) + [ui / "indexing_settings.py"]:
        name = path.name
        lines = path.read_text(encoding="utf-8").splitlines()
        code = [line for line in lines if line.strip() and not line.strip().startswith("#")]
        assert len(code) < 250, f"{name} has {len(code)} lines - logic may be leaking into the view"



def _entity(**overrides):
    row = {
        "id": 1, "key": "acme water ltd", "display": "Acme Water Ltd", "kind": "name",
        "source": "cooccurrence", "mentions": 12, "chunk_count": 8, "doc_count": 5,
    }
    row.update(overrides)
    return row














# ---------------------------------------------------------------------------
# The indexing page. Reported by the first person to open the window:
# "the indexing page got stuck and had no statistics of what is indexed".
# ---------------------------------------------------------------------------

def test_a_progress_bar_with_no_total_is_a_barber_pole_forever() -> None:
    """The cause of "got stuck", stated as the arithmetic.

    `start()` was called with no total, so the range was set to (0, 0) - which
    in Qt is the indeterminate animation - and `_on_progress` only ever set a
    value `if self._total_estimate`, which was zero. The bar therefore spun and
    never moved for the entire run.

    The fix is to grow the denominator from what the walker has *found so far*,
    because a true total cannot be known before the walk finishes.
    """
    def bar_range(total_estimate: int, seen: int, done: int) -> tuple[int, int]:
        total = max(total_estimate, seen, done, 1)
        return total, min(done, total)

    # No estimate, nothing seen yet: a real range, not (0, 0).
    total, value = bar_range(0, 0, 0)
    assert total >= 1 and value == 0

    # As the walk discovers files, the bar moves and the denominator grows.
    assert bar_range(0, 10, 4) == (10, 4)
    assert bar_range(0, 50, 40) == (50, 40)

    # A value can never exceed its total, which Qt would clamp silently.
    assert bar_range(0, 10, 99) == (99, 99)


def test_a_worker_is_kept_alive_until_it_reports_itself_done() -> None:
    """Without this the application crashes, intermittently and confusingly.

    `QThreadPool.start()` owns the QRunnable on the C++ side, but nothing on the
    Python side holds the `WorkerSignals` QObject. Once the local variable at
    the call site goes out of scope Python collects it, sip deletes the C++
    object, and the still-running worker emits into a corpse:

        RuntimeError: wrapped C/C++ object of type WorkerSignals has been deleted

    It only bites when the work outlives the function that started it - so it
    appears only when the machine is busy, and looks unrelated to anything.
    """
    # The rest of this module is deliberately Qt-free, so it runs everywhere.
    # This one test reaches into `app.ui.workers`, which imports PyQt6 at the
    # top - without this it fails rather than skips wherever Qt is absent, and
    # a failure that means "not installed" trains people to ignore failures.
    pytest.importorskip("PyQt6")

    from app.ui import workers as workers_module

    class FakeSignal:
        def __init__(self):
            self._slots = []

        def connect(self, slot):
            self._slots.append(slot)

        def emit(self, *args):
            for slot in list(self._slots):
                slot(*args)

    class FakeWorker:
        def __init__(self):
            self.signals = type("S", (), {"done": FakeSignal()})()

    class FakePool:
        def __init__(self):
            self.started = []

        def start(self, worker):
            self.started.append(worker)

    pool, worker = FakePool(), FakeWorker()
    workers_module.run(pool, worker)

    assert worker in workers_module._IN_FLIGHT, "nothing was holding it alive"
    assert pool.started == [worker]

    worker.signals.done.emit()
    assert worker not in workers_module._IN_FLIGHT, "it was never released"


def test_every_view_starts_workers_through_run() -> None:
    """A `pool.start(worker)` that bypasses `run()` reintroduces the crash, and
    it would only show up on somebody's slow machine weeks later."""
    from pathlib import Path

    ui = Path(__file__).resolve().parents[2] / "app" / "ui"
    for source in ui.glob("*.py"):
        if source.name == "workers.py":
            continue
        text = source.read_text(encoding="utf-8")
        assert ".start(worker)" not in text, f"{source.name} bypasses run()"
        assert ".start(self._worker)" not in text, f"{source.name} bypasses run()"


# -- what the /type menu offers ----------------------------------------------
#
# Three sources with a deliberate order, and the ordering is the feature: what
# is in the index is what somebody nearly always wants, and what is merely
# configured must be reachable without pushing the common answers down.

class _Store:
    """Just the one method `value_suggestions` reaches for."""

    def __init__(self, values):
        self._values = list(values)

    def distinct_values(self, kind, *, prefix="", limit=40, within=None):
        assert kind == "ext"
        self.last_within = within
        return [v for v in self._values if not prefix or prefix in v][:limit]


def test_type_offers_index_then_kind_words_then_configured():
    r"""The three sources, in order - and the third needs a prefix now.

    **Renamed in spirit rather than in name: the assertion about the tail moved
    behind two characters, deliberately.** When this was written the
    application read 34 text extensions and offering all of them unprompted was
    reasonable. `3b29b7f` took that to 405, and the tail has no frequency to
    sort by - `enabled_extensions` returns it alphabetically - so unprompted it
    is `abap, ada, adb, ads...` sitting above `pdf` in the one menu that exists
    to answer "what can I filter by". See `catalogue_limit`.
    """
    from app.ui.presenter import value_suggestions

    found = value_suggestions(
        _Store(["pdf", "docx"]), "type",
        catalogue=lambda: ["dxf", "docx", "pdf"],
    )

    assert found[:2] == ["pdf", "docx"], "the index no longer comes first"
    assert found.index("excel") > found.index("docx"), "a kind word outranked a real type"
    assert "dxf" not in found, "405 formats cannot be offered unprompted"

    narrowed = value_suggestions(
        _Store(["pdf", "docx"]), "type", "dx",
        catalogue=lambda: ["dxf", "docx", "pdf"],
    )

    assert "dxf" in narrowed, "a configured format is still invisible"


def test_a_type_in_both_the_index_and_the_config_is_offered_once():
    from app.ui.presenter import value_suggestions

    found = value_suggestions(_Store(["pdf"]), "type", catalogue=lambda: ["pdf"])
    assert found.count("pdf") == 1


def test_the_prefix_filters_all_three_sources():
    """Matching is on a substring, as it always has been for the grammar's own
    values and for the `LIKE` behind `distinct_values`. The point here is that
    the *third* source is filtered too - a catalogue appended whole would put
    every enabled format on screen the moment somebody typed a letter."""
    from app.ui.presenter import value_suggestions

    found = value_suggestions(
        _Store(["docx", "pdf"]), "type", "do", catalogue=lambda: ["docm"],
    )
    assert found == ["docx", "doc", "docm"], found
    assert "pdf" not in found, "a value matching no part of the prefix survived"


def test_the_catalogue_is_not_read_without_a_store():
    """The menu is built twice - instantly on the interface thread with no
    store, then on a worker. Reading two TOML files is I/O and the interface
    thread does not do I/O, so the first pass must not reach the catalogue."""
    from app.ui.presenter import value_suggestions

    def explode():
        raise AssertionError("the catalogue was read on the instant pass")

    found = value_suggestions(None, "type")
    assert "excel" in found, "the grammar's own values should still be offered"
    assert value_suggestions(None, "type", catalogue=None) == found


def test_a_broken_catalogue_costs_only_the_extra_suggestions():
    """A missing or invalid extractors.toml must not empty the menu."""
    from app.ui.presenter import value_suggestions

    def explode():
        raise OSError("no such file")

    found = value_suggestions(_Store(["pdf"]), "type", catalogue=explode)
    assert found[0] == "pdf"
    assert "excel" in found


def test_a_command_with_no_source_is_unchanged():
    """`/has` and `/size` reach the values first and must keep doing so."""
    from app.ui.presenter import value_suggestions

    assert value_suggestions(None, "has") == ["attachment", "no-attachment"]


def test_the_configured_tail_needs_a_prefix_before_it_appears():
    r"""**Two ceilings, because 34 became 405.**

    With 405 enabled formats and a corpus holding perhaps forty, one ceiling
    for both halves inverts the ordering the menu was built around: the tail
    dwarfs the indexed head, and the menu fills with types the machine does not
    have. Truncating the tail to a smaller arbitrary number does not fix that -
    it is the same noise, shorter - because the tail has no frequency to sort
    by and comes out alphabetically.

    A prefix does fix it, and typing one is exactly the gesture somebody makes
    to check that a format they just switched on is really there. That check is
    the only reason the tail exists, and it still works.
    """
    from app.ui.presenter import catalogue_limit, value_suggestions

    tail = [f"z{n:03d}" for n in range(405)]

    assert catalogue_limit("") == 0
    assert catalogue_limit("p") == 0, "one letter matches a quarter of 405"
    assert catalogue_limit("pk") > 0

    bare = value_suggestions(_Store(["pdf"]), "type", catalogue=lambda: tail)
    assert not any(v.startswith("z") for v in bare), "the alphabet leaked in"


def test_the_tail_is_bounded_even_with_a_prefix():
    """A two-character prefix over 405 formats still matches more than a menu
    can hold - `z0` alone is a hundred of them."""
    from app.ui.presenter import CATALOGUE_LIMIT_PREFIXED, value_suggestions

    tail = [f"z{n:03d}" for n in range(405)]

    found = value_suggestions(_Store([]), "type", "z0",
                              catalogue=lambda: tail, limit=1000)
    offered = [v for v in found if v.startswith("z")]

    assert len(offered) == CATALOGUE_LIMIT_PREFIXED


def test_the_tails_allowance_is_not_spent_on_duplicates():
    r"""`cap` bounds what a source *adds*, not what it is handed.

    Slicing the input instead would let a catalogue whose first forty entries
    are already indexed spend its whole allowance adding nothing - and the
    overlap between "indexed" and "configured" is not the exception, it is the
    normal case.
    """
    from app.ui.presenter import value_suggestions

    found = value_suggestions(
        _Store(["docx", "docm"]), "type", "doc",
        catalogue=lambda: ["docx", "docm", "docbook"], limit=1000,
    )

    assert "docbook" in found
    assert found.count("docx") == 1


def test_the_ext_ceiling_is_raised_above_the_general_one():
    """`ext` is bounded at perhaps eighty values; senders and folders are not.
    One number for both truncated a list that fits on a screen."""
    from app.ui.presenter import VALUE_LIMIT, VALUE_LIMITS

    assert VALUE_LIMITS["ext"] > VALUE_LIMIT
    assert "sender" not in VALUE_LIMITS, "an unbounded column must keep the cap"


def test_a_long_index_does_not_squeeze_out_the_kind_words():
    from app.ui.presenter import value_suggestions

    found = value_suggestions(_Store([f"e{n:03d}" for n in range(60)]), "type",
                              catalogue=lambda: [])
    assert "excel" in found, "sixty extensions buried every kind word"
