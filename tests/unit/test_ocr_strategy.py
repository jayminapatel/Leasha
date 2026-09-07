r"""OCR where it pays, and the wiring that meant it never ran at all.

Layer: L2/L3

From `docs/WORKORDER-202626081052-ocr-strategy.md`. The decision it records is
about **value density** rather than capability - both jobs are technically the
same, and one is worth doing:

| Work | Volume | Cost |
|---|---|---|
| Scanned PDFs, 20-page budget | ~200 docs x 20 pages | **~4 hours** |
| Office embedded images | ~5,000 decks x 30 images | **~150 hours** |

A scanned manual is a document whose *entire* contents are unreachable. An
embedded picture in a deck is almost always a logo, an icon, or a chart whose
labels are already in the slide text beside it.

**§4 names the open piece of wiring, and it is the substance of this file:**
whether a PDF needs OCR is *not knowable from its extension*, so `reads_by_ocr`
is False for every `.pdf`, the images pass narrows the walk to `.png`/`.jpg`,
and a scanned manual is never revisited however many times somebody runs
`--only-ocr`. The text pass records exactly the right rows; the OCR pass has to
read them back.

§3's measurement - one folder, timed, on the owner's machine - is not something
a unit test can stand in for, and the work order says so: *"nothing else in this
document is worth doing first"*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.errors import make_error
from app.index.pipeline import IndexStats, Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.storage.sqlite_store import FileStatus, SqliteStore


class _NoVectors:
    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return 0

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _pipeline(store, root, *, ocr_mode="both"):
    import math

    from app.index.embedder import Embedder, l2_normalise

    def encode(texts):
        return [l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)])
                for t in texts]

    return Pipeline(store, _NoVectors(), Embedder(dim=8, encoder=encode),
                    PipelineConfig(
                        walk=WalkConfig(roots=[root],
                                        extensions=frozenset({".png", ".txt"})),
                        workers=1, min_free_gb=1, ocr_mode=ocr_mode))


@pytest.fixture()
def ledger(tmp_path):
    """A store in the state the *text* pass leaves behind: one scanned PDF
    recorded as having no text layer, and one deck that is mostly pictures."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    # **Outside the walked folder on purpose.** Since §1a the walk yields every
    # file it sees, readable or not, so a scanned PDF sitting in the corpus
    # would arrive from the walk and prove nothing about the ledger. Elsewhere,
    # the only way it can reach the pass is the route under test.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    scanned = elsewhere / "manual.pdf"
    scanned.write_bytes(b"%PDF-1.4 not really a pdf")
    deck = elsewhere / "infographic.pptx"
    deck.write_bytes(b"PK not really a deck")

    with SqliteStore(tmp_path / "index.db") as store:
        for path, code in ((scanned, "ERR_NO_TEXT_LAYER"),
                           (deck, "ERR_MOSTLY_PICTURES")):
            file_id = store.upsert_file(
                str(path), size_bytes=path.stat().st_size, mtime_ns=1,
                ext=path.suffix.lstrip("."), status=FileStatus.PENDING,
                source_kind="file")
            store.mark_skipped(file_id, make_error(
                code, "extract", path=str(path)))
        yield store, corpus


# --- §4: the images pass reads the ledger, not the walk ---------------------

def test_the_images_pass_retries_a_scanned_pdf(ledger):
    r"""**The open wiring.** `reads_by_ocr` is False for every PDF - correctly,
    because most have a text layer - so narrowing the walk to image extensions
    skips every scanned manual in the corpus, for ever."""
    store, corpus = ledger

    retried = [p.path.name
               for p in _pipeline(store, corpus, ocr_mode="images")
               ._no_text_layer_candidates()]

    assert retried == ["manual.pdf"]


def test_a_picture_heavy_deck_is_not_retried(ledger):
    r"""**They looked like one problem and are not.** Sharing
    `ERR_NO_TEXT_LAYER` between a scanned PDF and a mostly-pictures deck made
    the second indistinguishable from the first, so `--only-ocr` would have
    queued every infographic in the corpus - the 150-hour column, entered by
    accident."""
    store, corpus = ledger

    retried = [p.path.name
               for p in _pipeline(store, corpus, ocr_mode="images")
               ._no_text_layer_candidates()]

    assert "infographic.pptx" not in retried


def test_the_text_pass_does_not_read_the_ledger(ledger):
    """Only `--only-ocr` retries them. The text pass has just written those
    rows and re-queueing them would be an infinite loop wearing a retry."""
    store, corpus = ledger

    candidates = [c.path.name for c in
                  _pipeline(store, corpus, ocr_mode="text")._candidates()]

    assert "manual.pdf" not in candidates


def test_the_images_pass_reaches_a_file_the_walk_cannot_see(ledger):
    r"""**The whole point of reading the ledger back.** The scanned PDF is not
    under the walked root at all here, so the only way it can reach the pass is
    the `ERR_NO_TEXT_LAYER` row the text pass wrote - which is exactly the
    situation on a real corpus, where the images walk is narrowed to
    `.png`/`.jpg` and a manual is invisible to it."""
    store, corpus = ledger

    candidates = [c.path.name for c in
                  _pipeline(store, corpus, ocr_mode="images")._candidates()]

    assert "manual.pdf" in candidates


def test_a_file_that_has_gone_is_skipped_silently(ledger):
    """The ordinary prune deals with the row; this pass is not the place."""
    store, corpus = ledger
    (corpus.parent / "elsewhere" / "manual.pdf").unlink()

    retried = list(_pipeline(store, corpus, ocr_mode="images")
                   ._no_text_layer_candidates())

    assert retried == []


# --- §5: the count that decides the Office question -------------------------

def test_a_warning_on_an_indexed_document_is_counted(tmp_path):
    r"""**"List the affected documents rather than reading them."**

    A warning lives on a document that indexed *successfully*, so it never
    reached the skip ledger and the only record was a log line. That made
    *"412 decks are mostly images"* - the input to the whole Office decision -
    a question answerable only by grepping.
    """
    stats = IndexStats()
    stats.warned_by_code["ERR_MOSTLY_PICTURES"] = 412

    assert stats.as_dict()["warned_by_code"] == {"ERR_MOSTLY_PICTURES": 412}


def test_the_two_kinds_of_unreadable_have_different_codes():
    r"""They read the same to a person and mean opposite things to the indexer:
    one is work to retry, the other is work deliberately declined."""
    scanned = make_error("ERR_NO_TEXT_LAYER", "extract.pdf", path="a.pdf")
    deck = make_error("ERR_MOSTLY_PICTURES", "extract.pptx", path="b.pptx")

    assert scanned.code != deck.code
    assert not scanned.is_fatal and not deck.is_fatal


# --- §4: a run that looks stalled gets killed -------------------------------

def test_the_progress_line_says_when_it_is_ocr():
    r"""A text run moves at hundreds of files a minute; OCR moves at seconds
    *per page*, so the same line reads as a stall - and a run that looks
    stalled gets killed, which is how four hours of work is thrown away."""
    from app.ui.presenter import progress_text

    ordinary = IndexStats(ocr_mode="both")
    ordinary.current = "manual.pdf"
    images = IndexStats(ocr_mode="images")
    images.current = "manual.pdf"

    assert "reading manual.pdf" in progress_text(ordinary)[1]
    assert "reading with OCR manual.pdf" in progress_text(images)[1]


# --- §6: non-negotiable 11 --------------------------------------------------

def test_the_page_budget_has_a_control_and_not_only_an_environment_variable():
    r"""`LEASHA_PDF_OCR_PAGES` was the whole interface, which is a tunable with
    no control. The variable still wins, so one run can be given a different
    budget without touching anybody's configuration."""
    from app.core.settings_registry import SURFACES, by_key

    setting = by_key("PDF_OCR_PAGES")

    assert setting is not None
    assert setting.default == 0                  # off unless asked for
    # It moved from Indexing to the Index Tuning screen's Coverage group with
    # the rest of "what gets read" - index-tuning §4c-3. The point of this
    # assertion is that it has *a* surface somebody can reach, not which one,
    # so the surface it names is checked against the registry's own list.
    assert setting.surface in SURFACES


def test_the_environment_variable_still_overrides(monkeypatch):
    from app.extract.pdf import PDF_OCR_PAGES_VAR, _pdf_ocr_pages

    monkeypatch.setenv(PDF_OCR_PAGES_VAR, "20")

    assert _pdf_ocr_pages() == 20


def test_a_typo_in_the_variable_is_off_rather_than_a_job_that_takes_days(monkeypatch):
    from app.extract.pdf import PDF_OCR_PAGES_VAR, _pdf_ocr_pages

    monkeypatch.setenv(PDF_OCR_PAGES_VAR, "twenty")

    assert _pdf_ocr_pages() == 0


def test_a_budget_is_a_budget_rather_than_a_switch():
    r"""The arithmetic is the argument: at ~3.6s a page, twenty pages is about
    a minute a document and covers the title, contents and introduction - most
    of what makes a manual findable. All-or-nothing is the sixty-hour column."""
    from app.core.settings_registry import by_key

    setting = by_key("PDF_OCR_PAGES")

    assert setting.kind == "int" and setting.unit == "pages"
    assert setting.minimum == 0 and setting.maximum >= 20


def test_the_white_page_threshold_has_a_control_and_a_sensible_default():
    r"""§2e of `WORKORDER-202626270508-media-by-default-and-ocr-ladder.md`:
    the OCR ladder's rung 1 threshold was a bare `if white_fraction > 0.7:`
    literal in `app/extract/ocr_ladder.py`, which is exactly the shape
    non-negotiable 11 forbids - a tunable with no control. This is that
    literal, promoted."""
    from app.core.settings_registry import SURFACES, by_key
    from app.extract.ocr_ladder import WHITE_FRACTION_THRESHOLD_DEFAULT

    setting = by_key("OCR_WHITE_PAGE_PERCENT")

    assert setting is not None
    # Stored as a whole-number percentage - the registry has no float kind -
    # and it must equal the literal the ladder used before this setting
    # existed, or a fresh install would not reproduce old behaviour.
    assert setting.default == round(WHITE_FRACTION_THRESHOLD_DEFAULT * 100)
    assert setting.kind == "int" and setting.unit == "%"
    assert setting.minimum is not None and setting.maximum == 100
    assert setting.surface in SURFACES
    # It lives with the rest of "what gets read" - index-tuning's Coverage
    # group - the same surface PDF_OCR_PAGES and INDEX_OCR_MODE moved to.
    assert setting.surface == by_key("PDF_OCR_PAGES").surface
    assert setting.group == by_key("PDF_OCR_PAGES").group
