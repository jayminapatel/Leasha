r"""Turning a result row into a preview, for every kind of row there is.

Layer: L5

The owner's instruction: *"preview pane should be in every search type"*. The
pane itself was already written and already worked - on one tab. What did not
exist was a rule for reading rows that are not search results, and the pane was
reaching into them with three `getattr` calls, which is a rule nobody could test
and which was silently wrong twice:

* a **Mail** row has no `preview_text`, so every message previewed as
  `ERR_FILE_MISSING` against a synthetic path nobody could have opened;
* a **Code** row's `path` is shortened to fit its column, so every repository
  file did the same - and "file missing" is plausible enough that nobody would
  have questioned it.

Both are the same failure: a preview pane that is confidently wrong looks
exactly like one that is right about a broken file. So the reading rule lives in
`preview_loader`, which imports no Qt, and this is where it is checked.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.ui.preview_loader import (
    KIND_TEXT,
    load_preview_for,
    stored_text,
)


@dataclass
class SearchRow:
    path: str = ""
    name: str = ""
    page: int = 0
    preview_text: str = ""


@dataclass
class CodeRow:
    """A `RepoFileRow`: `path` is shortened for the column, `full_path` is real."""

    name: str = ""
    path: str = ""
    full_path: str = ""


@dataclass
class MailRow:
    """No file of its own - a PST holds a hundred thousand of these."""

    file_id: int = 0
    name: str = ""
    path: str = ""


class FakeStore:
    def __init__(self, chunks, raises=False):
        self._chunks = chunks
        self._raises = raises
        self.asked = []

    def chunks_for_file(self, file_id):
        self.asked.append(file_id)
        if self._raises:
            raise RuntimeError("the database is locked")
        return self._chunks


class Chunk:
    def __init__(self, text):
        self.text = text


# --- reading the row --------------------------------------------------------

def test_a_file_is_previewed_from_its_path(tmp_path):
    target = tmp_path / "notes.txt"
    target.write_text("the contents", encoding="utf-8")

    preview = load_preview_for(SearchRow(path=str(target), name="notes.txt"))

    assert preview.kind == KIND_TEXT
    assert "the contents" in preview.body


def test_a_code_row_is_previewed_from_full_path_not_the_shortened_one(tmp_path):
    """**The trap.** `path` is what the column shows: `…\\src\\main.py`. Opening
    it finds nothing, and the pane says the file is missing - which is a
    plausible message about a file that is sitting right there."""
    target = tmp_path / "main.py"
    target.write_text("print('hello')", encoding="utf-8")

    preview = load_preview_for(CodeRow(
        name="main.py", path="…/src/main.py", full_path=str(target)))

    assert preview.error is None, "it read the shortened column value"
    assert "hello" in preview.body


def test_the_page_a_hit_knows_about_is_carried_through(tmp_path):
    target = tmp_path / "report.pdf"
    target.write_bytes(b"%PDF-1.4 not really")

    preview = load_preview_for(SearchRow(path=str(target), page=7))

    assert preview.page == 7


# --- rows that carry no file ------------------------------------------------

def test_a_body_the_row_already_has_is_used_without_asking_the_store():
    """A search result carries its own snippet. Going back to the store for
    text that is already in hand is a query per arrow key."""
    store = FakeStore([Chunk("from the store")])

    preview = load_preview_for(
        SearchRow(path="C:/mail.pst", preview_text="already here", name="Subject"),
        body_provider=lambda row: stored_text(store, 1),
    )

    assert "already here" in preview.body
    assert store.asked == [], "it queried for a body it had been given"


def test_a_mail_row_gets_its_text_from_the_provider():
    """The message has no file of its own; the text was extracted at index time
    and this is where it went."""
    store = FakeStore([Chunk("Dear Priya"), Chunk("Regards, Dave")])

    preview = load_preview_for(
        MailRow(file_id=42, name="Invoice", path="C:/mail.pst#42"),
        body_provider=lambda row: stored_text(store, row.file_id),
    )

    assert "Dear Priya" in preview.body and "Regards, Dave" in preview.body
    assert store.asked == [42]


def test_a_message_is_headed_with_its_subject():
    """`load_preview` is handed text and a synthetic path and calls it
    "Message". A heading that says nothing the pane has not already said is a
    heading worth spending on the subject instead."""
    preview = load_preview_for(MailRow(name="Q3 numbers", path="C:/mail.pst#1"),
                               body_provider=lambda _row: "the body")

    assert preview.title == "Q3 numbers"


def test_a_provider_that_fails_costs_the_body_and_nothing_else():
    """**It runs on the paint path.** A store that is locked, mid-index, or
    being closed must cost a preview of one row somebody arrowed past - never a
    traceback, and never the window."""
    store = FakeStore([], raises=True)

    preview = load_preview_for(
        MailRow(file_id=9, path="C:/mail.pst#9"),
        body_provider=lambda row: stored_text(store, row.file_id),
    )

    assert preview.body == ""
    assert preview.error is not None, "a preview with no body must say so"


def test_a_row_with_nothing_on_it_at_all_does_not_raise():
    """Defensive, for the same reason: whatever a future row looks like, the
    worst it may do here is fail to preview."""
    class Odd:
        pass

    assert load_preview_for(Odd()) is not None


# --- the store helper -------------------------------------------------------

def test_stored_text_joins_the_chunks_in_order():
    store = FakeStore([Chunk("one"), Chunk("two"), Chunk("three")])

    assert stored_text(store, 5) == "one\n\ntwo\n\nthree"


def test_stored_text_survives_a_store_that_raises():
    assert stored_text(FakeStore([], raises=True), 5) == ""


def test_stored_text_survives_a_row_with_no_id():
    assert stored_text(FakeStore([]), None) == ""
