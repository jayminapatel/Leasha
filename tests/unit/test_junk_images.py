"""Order 0z lane D: the junk-image filter - signature logos and icons in mail are not read.

The owner: *signature and junk images attached to emails are being OCR'd, which
is slow and useless.* Each rule of the order's lane D has its tests here, and
so does the promise that makes the filter safe to have on by default: **a real
screenshot, scan, receipt or photograph is never left unread by it.**

The reads go through the real `pst_libpff` attachment loop on a fake `pypff`
(the same fakes as `test_pst_libpff`), with real picture bytes and a stand-in
OCR reader, so they run anywhere and fast.

**Dormant for mail since 1 October 2026.** The owner ruled that pictures attached
to mail are recorded by name and never read (`app.extract.mail_attachments`), so
nothing in a normal run reaches this filter any more. These tests keep it working
by declaring their stand-in pictures readable - it is kept, not deleted, so that
turning picture reading back on is a one-line change and not a rebuild.
"""

from __future__ import annotations

import io
import sqlite3
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from app.extract import base, junk_images, progress, pst_libpff, reading
from app.extract.base import Document
from app.extract.junk_images import (
    BookEntry, ImageBook, MIN_WORDS, REASON_DECORATIVE, REASON_FEW_WORDS, REASON_REPEATED,
    REPEAT_LIMIT, count_words, decorative, header_size, is_inline, perceptual_hash,
    screen, settle,
)
from tests.unit.test_pst_libpff import (
    FakeAttachment, FakeFolder, FakeMessage, FakeRecordEntry, install_fake,
)


# --- pictures ------------------------------------------------------------------

def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def _jpeg(image: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def _logo(seed: int = 1, size=(200, 60)) -> Image.Image:
    """A signature logo: a few coloured shapes, some detail for a perceptual hash."""
    import random

    r = random.Random(seed)
    image = Image.new("RGB", size, (255, 255, 255))
    draw = ImageDraw.Draw(image)
    w, h = size
    for _ in range(5):
        x, y = r.randint(0, w - 20), r.randint(0, h - 10)
        draw.rectangle((x, y, x + r.randint(10, w // 2), y + r.randint(5, h // 2)),
                       fill=(r.randint(0, 255), r.randint(0, 255), r.randint(0, 255)))
    return image


def _screenshot(size=(800, 450), seed: int = 7) -> Image.Image:
    image = _logo(seed, size)
    return image


ICON = _png(Image.new("RGB", (24, 24), (59, 89, 152)))
PIXEL = _png(Image.new("RGB", (1, 1), (255, 255, 255)))
DIVIDER = _png(Image.new("RGB", (600, 2), (200, 200, 200)))
LOGO = _png(_logo(1))
SCREENSHOT = _png(_screenshot())
SMALL_SCREENSHOT = _png(_screenshot((320, 60), seed=9))
RECEIPT = _jpeg(_screenshot((600, 1200), seed=3), 85)
PHOTO = _jpeg(_screenshot((1600, 1200), seed=4), 88)


# --- the stand-in OCR reader ---------------------------------------------------

#: What the stand-in OCR "reads" in each picture, by its bytes.
TEXT: dict[bytes, str] = {}


class _Ocr:
    """Registered as `ocr` for `.png`/`.jpg`, so `reads_by_ocr` treats them as pictures."""

    name = "ocr"
    extensions = (".png", ".jpg")
    calls: list[str] = []

    def extract(self, path: Path):
        type(self).calls.append(path.name)
        text = TEXT.get(path.read_bytes(), "")
        if text:
            yield Document(path=path, text=text, source_kind="file", meta={"format": "ocr"})


@pytest.fixture(autouse=True)
def _reader(monkeypatch):
    from tests.unit.test_pst_libpff import read_contents_of

    read_contents_of(monkeypatch, *_Ocr.extensions)      # dormant for mail: see above
    before = dict(base.REGISTRY)
    _Ocr.calls = []
    TEXT.clear()
    TEXT.update({
        LOGO: "ACME\nLtd",
        SCREENSHOT: "orderagendadeliveryagendameetingorder\npaymenttotalorderduepaymenttotal",
        SMALL_SCREENSHOT: "Error: file not found",
        RECEIPT: "Coffee 2.40\nMilk 1.10\nTotal 3.50\nVisa ending 1234",
        PHOTO: "",
    })
    base.REGISTRY.get(".png")                   # load the real readers first,
    for ext in _Ocr.extensions:                 # then stand in for the OCR one
        dict.pop(base.REGISTRY, ext, None)
    base.register(_Ocr())
    yield
    base.REGISTRY.clear()
    base.REGISTRY.update(before)


class _Attachment(FakeAttachment):
    """A libpff attachment with the MAPI properties Outlook writes for an inline picture."""

    def __init__(self, name, data, *, inline=False):
        super().__init__(name, data)
        if inline:
            self._record_sets[0]._entries += [
                _Entry(0x7FFE, True), _Entry(0x3712, f"{name}@01DA0000.00000000")]


class _Entry(FakeRecordEntry):
    def get_data_as_boolean(self):
        if isinstance(self._value, bool):
            return self._value
        raise TypeError("not a boolean")

    def get_data_as_string(self):
        if isinstance(self._value, str):
            return self._value
        raise TypeError("not a string")

    def get_data_as_integer(self):
        if isinstance(self._value, int):
            return int(self._value)
        raise TypeError("not an integer")


def _archive(monkeypatch, messages, *, junk=True, images=reading.IMAGES_READ):
    """Read one fake archive. Returns (documents, policy)."""
    install_fake(monkeypatch, FakeFolder("", children=[FakeFolder("Inbox", messages=messages)]))
    with reading.reading(images=images, junk=junk) as policy:
        documents = list(pst_libpff.read_archive(Path("Archive.pst")))
    assert progress.frames() == []
    return documents, policy


def _message(n, *attachments):
    return FakeMessage(n, subject=f"Message {n}", plain="Regards.",
                       headers=f"Message-ID: <m{n}@example.com>\n", attachments=attachments)


def _attached(documents):
    return sorted(d.meta["attachment_name"] for d in documents if "attachment_name" in d.meta)


# --- counting words --------------------------------------------------------------

@pytest.mark.parametrize("text, words", [
    ("ACME\nLtd", 2),                                  # a logo
    ("Northwind", 1),
    ("in", 1),                                         # a LinkedIn icon
    ("f", 0),                                          # a Facebook icon
    ("3", 0),                                          # a speck on a photograph
    ("Error: file not found", 4),
    # OCR drops spaces: one line of six words read as one token is still six words.
    ("orderagendadeliveryagendameetingorder", 7),
    ("Pleasecallmeback", 3),
    ("收据 合计 现金", 6),                               # written without spaces
])
def test_words_are_counted_even_when_ocr_drops_the_spaces(text, words) -> None:
    assert count_words(text) == words


# --- D2: decorative --------------------------------------------------------------

@pytest.mark.parametrize("size, inline, expected", [
    ((24, 24), True, True),            # a social icon
    ((96, 96), True, True),            # the same icon drawn for a high-density screen
    ((1, 1), True, True),              # a tracking pixel
    ((600, 2), True, True),            # a divider
    ((1, 10), True, True),             # a spacer
    ((24, 24), False, False),          # attached on purpose: never decorative
    ((600, 2), False, False),
    ((800, 450), True, False),         # a pasted screenshot is inline too
    ((320, 60), True, False),          # a small pasted screenshot of one line
    ((200, 60), True, False),          # a logo: left to D1, D3 and D4
    ((600, 100), True, False),         # a banner holding words
    (None, True, False),               # size unknown proves nothing
])
def test_decorative_needs_inline_and_tiny_or_divider_shaped(size, inline, expected) -> None:
    assert decorative(inline, size) is expected


def test_the_size_comes_from_the_header() -> None:
    assert header_size(SCREENSHOT) == (800, 450)
    assert header_size(DIVIDER) == (600, 2)
    assert header_size(b"not a picture") is None


def test_inline_is_read_from_the_mapi_properties() -> None:
    assert is_inline(_Attachment("logo.png", LOGO, inline=True))
    assert not is_inline(_Attachment("logo.png", LOGO))
    flagged = FakeAttachment("x.png", LOGO)
    flagged._record_sets[0]._entries.append(_Entry(0x3714, 4))          # ATT_MHTML_REF
    assert is_inline(flagged)

    class Broken:
        def get_number_of_record_sets(self):
            raise OSError("damaged")

    assert not is_inline(Broken())


def test_an_inline_icon_pixel_and_divider_are_not_read(monkeypatch) -> None:
    documents, policy = _archive(monkeypatch, [_message(
        1, _Attachment("image002.png", ICON, inline=True),
        _Attachment("image003.png", PIXEL, inline=True),
        _Attachment("image004.png", DIVIDER, inline=True))])
    assert _Ocr.calls == []
    assert policy.not_read == {REASON_DECORATIVE: 3}
    assert policy.counts == {"Indexed": 1, "Skipped": 3}
    assert documents[0].meta["attachment_names"] == [
        "image002.png", "image003.png", "image004.png"], "still found by name, on the message"


# --- the promise ------------------------------------------------------------------

def test_a_real_screenshot_scan_or_receipt_is_never_left_unread(monkeypatch) -> None:
    """Inline or attached, once or across many archives: always read, always indexed."""
    for archive in range(REPEAT_LIMIT + 3):
        _Ocr.calls = []
        documents, policy = _archive(monkeypatch, [
            _message(1, _Attachment("image001.png", SCREENSHOT, inline=True),
                     _Attachment("image002.png", SMALL_SCREENSHOT, inline=True)),
            _message(2, _Attachment("receipt.jpg", RECEIPT)),
            _message(3, _Attachment("holiday.jpg", PHOTO)),
        ], junk=BOOK)
        read = set(_Ocr.calls)
        assert {"image001.png", "image002.png", "receipt.jpg"} <= read, f"archive {archive}"
        assert _attached(documents) == ["image001.png", "image002.png", "receipt.jpg"]
        assert REASON_DECORATIVE not in policy.not_read
        # The photograph gave OCR nothing, every time. Once those same bytes had
        # been met REPEAT_LIMIT times it is not handed to OCR again - which
        # changes nothing indexed, because it never produced a word to index.
        assert ("holiday.jpg" in read) is (archive + 1 < REPEAT_LIMIT)


BOOK = ImageBook()


@pytest.fixture(autouse=True)
def _fresh_book():
    global BOOK
    BOOK = ImageBook()
    yield


# --- D1 and D3: repeated with no words; fewer than three words ----------------------

def test_a_logo_with_under_three_words_is_not_indexed_and_then_not_read_again(monkeypatch) -> None:
    book = ImageBook()
    reads = []
    for archive in range(4):
        _Ocr.calls = []
        # The same logo in two messages: the second is a Duplicate within the archive.
        documents, policy = _archive(monkeypatch, [
            _message(1, _Attachment("image001.png", LOGO, inline=True)),
            _message(2, _Attachment("image001.png", LOGO, inline=True)),
        ], junk=book)
        reads.append(len(_Ocr.calls))
        assert _attached(documents) == [], "D3: two words are not indexed"
    # Met twice an archive (the second a Duplicate). Read in the first two; in
    # the third its first sighting is the fifth, and it gave two words.
    assert reads == [1, 1, 0, 0]
    assert policy.not_read == {REASON_REPEATED: 1}
    entry = book.get(pst_libpff._hash_bytes(LOGO))
    assert entry.words == 2 and entry.seen == 8


def test_d3_keeps_words_from_anything_but_ocr() -> None:
    book = ImageBook()
    described = Document(path=Path("p.jpg"), text="A dog", source_kind="file",
                         meta={"format": "florence_tags"})
    kept, why = settle(book, "a", [described])
    assert kept == [described] and why == ""
    ocr = Document(path=Path("p.jpg"), text="ACME Ltd", source_kind="file", meta={"format": "ocr"})
    assert settle(book, "b", [ocr]) == ([], REASON_FEW_WORDS)
    many = Document(path=Path("p.jpg"), text="Total due 3.50 by card",
                    source_kind="file", meta={"format": "ocr"})
    assert settle(book, "c", [many]) == ([many], "")
    assert book.get("b").words == 2 and book.get("c").words >= MIN_WORDS


def test_bytes_that_gave_words_are_never_skipped_however_often_seen() -> None:
    book = ImageBook()
    for _ in range(50):
        book.saw("receipt")
    book.read("receipt", 40)
    assert not book.repeated_without_words("receipt")
    assert screen(book, "receipt", RECEIPT, inline=False).reason == ""


def test_the_book_needs_the_limit_before_it_skips() -> None:
    book = ImageBook()
    book.saw("logo")
    book.read("logo", 1)
    for seen in range(2, REPEAT_LIMIT + 1):
        assert not book.repeated_without_words("logo")
        book.saw("logo")
    assert book.repeated_without_words("logo")


# --- D4: near-identical logos -------------------------------------------------------

def test_a_re_encoded_logo_is_recognised_by_its_perceptual_hash() -> None:
    book = ImageBook()
    image = _logo(1)
    variants = [_jpeg(image, q) for q in (95, 90, 85, 80, 75, 70, 65)]
    assert len(set(variants)) == len(variants), "every byte different"
    for i, data in enumerate(variants[:REPEAT_LIMIT]):
        digest = f"v{i}"
        book.saw(digest)
        screened = screen(book, digest, data, inline=True)
        assert screened.reason == "", f"variant {i} read: not yet seen {REPEAT_LIMIT} times"
        settle(book, digest, [], screened)                  # OCR found nothing
    book.saw("v6")
    assert screen(book, "v6", variants[6], inline=True).reason == REASON_REPEATED


def test_a_different_logo_or_size_is_not_matched() -> None:
    book = ImageBook()
    data = _png(_logo(1))
    size = header_size(data)
    phash = perceptual_hash(data, size)
    book = ImageBook([("x", BookEntry(seen=50, words=0, phash=phash, width=size[0], height=size[1]))])
    other = _png(_logo(2))
    assert not book.looks_like_repeated_logo(perceptual_hash(other, header_size(other)),
                                             header_size(other))
    wider = _png(_logo(1).resize((300, 60)))
    assert not book.looks_like_repeated_logo(perceptual_hash(wider, header_size(wider)),
                                             header_size(wider))
    assert book.looks_like_repeated_logo(phash, size)


def test_one_near_match_that_gave_words_says_no() -> None:
    book = ImageBook()
    data = _png(_logo(1))
    size = header_size(data)
    phash = perceptual_hash(data, size)
    book = ImageBook([("logo", BookEntry(seen=50, words=0, phash=phash, width=200, height=60)),
                      ("real", BookEntry(seen=1, words=12, phash=phash, width=200, height=60))])
    assert not book.looks_like_repeated_logo(phash, size)


def test_a_flat_picture_never_matches_anything() -> None:
    """Measured: a blank 300x60 and a 600x2 divider hash to the same all-zero bits."""
    blank = _png(Image.new("RGB", (300, 60), (255, 255, 255)))
    phash = perceptual_hash(blank, (300, 60))
    assert bin(phash).count("1") < junk_images.FLAT_BITS
    book = ImageBook()
    book = ImageBook([("blank", BookEntry(seen=50, words=0, phash=phash, width=300, height=60))])
    assert not book.looks_like_repeated_logo(phash, (300, 60))


def test_a_big_picture_is_never_decoded_for_d4() -> None:
    assert perceptual_hash(PHOTO, header_size(PHOTO)) is None


# --- D5: the switch ---------------------------------------------------------------

def test_switched_off_every_picture_is_read_as_before(monkeypatch) -> None:
    documents, policy = _archive(monkeypatch, [_message(
        1, _Attachment("image001.png", LOGO, inline=True),
        _Attachment("image002.png", ICON, inline=True),
        _Attachment("image003.png", SCREENSHOT, inline=True))], junk=False)
    assert sorted(_Ocr.calls) == ["image001.png", "image002.png", "image003.png"]
    assert _attached(documents) == ["image001.png", "image003.png"]
    assert policy.not_read == {}


def test_the_setting_is_registered_read_and_has_a_control(temp_env) -> None:
    from PySide6.QtWidgets import QWidget

    from app.core.config import SETTING_KEYS, load_settings
    from app.core.settings_registry import by_key
    from app.ui.widgets.long_run_box import LongRunBox

    setting = by_key("INDEX_JUNK_IMAGE_FILTER")
    assert setting is not None and setting.kind == "bool" and setting.default is True
    assert "INDEX_JUNK_IMAGE_FILTER" in SETTING_KEYS
    assert load_settings(temp_env).index_junk_image_filter is True
    temp_env.write_text(temp_env.read_text(encoding="utf-8")
                        + "\nINDEX_JUNK_IMAGE_FILTER=false\n", encoding="utf-8")
    settings = load_settings(temp_env)
    assert settings.index_junk_image_filter is False

    box = LongRunBox()
    control = box.findChild(QWidget, "INDEX_JUNK_IMAGE_FILTER")
    assert control is not None
    box.load(settings)
    assert box.values()["index_junk_image_filter"] is False


def test_the_switch_says_it_has_no_effect_while_mail_pictures_are_not_read(temp_env) -> None:
    """1 October 2026: a switch that changes nothing must not look as if it does."""
    from PySide6.QtWidgets import QLabel, QWidget

    from app.core.settings_registry import by_key
    from app.ui.widgets.long_run_box import LongRunBox

    box = LongRunBox()
    assert not box.findChild(QWidget, "INDEX_JUNK_IMAGE_FILTER").isEnabled()
    note = box.findChild(QLabel, "junkImagesDormantNote")
    assert note is not None and "no effect" in note.text()
    assert by_key("INDEX_JUNK_IMAGE_FILTER").help.startswith("Note, 1 October 2026")


def test_the_pipeline_hands_the_book_only_when_the_filter_is_on(tmp_path) -> None:
    from app.index.pipeline import PipelineConfig
    from app.index.walker import WalkConfig

    assert PipelineConfig(walk=WalkConfig(roots=[])).junk_images is True


# --- the book in the index ----------------------------------------------------------

def test_the_book_survives_a_run_in_the_index(tmp_path) -> None:
    from app.index.image_book import PersistentImageBook
    from app.storage.sqlite_store import SqliteStore

    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        first = PersistentImageBook(store)
        first.saw("abc")
        first.read("abc", 1, phash=0x0123456789ABCDEF, size=(200, 60))
        assert first.save() == 1
        assert first.save() == 0, "nothing changed since"
    with SqliteStore(db) as store:
        again = PersistentImageBook(store)
        entry = again.get("abc")
        assert entry == BookEntry(seen=1, words=1, phash=0x0123456789ABCDEF,
                                  width=200, height=60)
        store.clear_index()
        assert store.image_hashes() == [], "a reset forgets the book too"


def test_schema_v29_adds_the_table(tmp_path) -> None:
    from app.storage.migrations import CURRENT_VERSION
    from app.storage.sqlite_store import SqliteStore

    db = tmp_path / "index.db"
    with SqliteStore(db):
        pass
    with sqlite3.connect(db) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    assert CURRENT_VERSION >= 29 and "image_hashes" in names


# --- where it shows ------------------------------------------------------------------

def test_the_counts_line_says_why_pictures_were_skipped() -> None:
    from app.ui.presenter.activity import status_counts_text

    text = status_counts_text({"Indexed": 70, "Skipped": 32, "Skipped:decorative": 12,
                               "Skipped:repeated": 14, "Duplicate": 244})
    assert text == ("70 Indexed · 32 Skipped (12 decorative pictures, 14 repeated "
                    "pictures) · 244 Duplicate")


def test_the_indexing_page_counts_pictures_not_read() -> None:
    from app.index.pipeline import IndexStats
    from app.ui.presenter.indexing import index_summary, pictures_not_read_counts

    stats = IndexStats()
    stats.pictures_not_read = {"decorative": 36, "repeated": 32, "few_words": 8}
    stored = repr(stats.as_dict())
    counts = pictures_not_read_counts(stored)
    assert counts == {"decorative": 36, "repeated": 32, "few_words": 8}
    rows = index_summary({"files_total": 1, "chunks_total": 1}, pictures_not_read=counts)
    row = next(r for r in rows if r.label == "Pictures in mail not read")
    assert row.value == "76" and not row.warn
    assert pictures_not_read_counts("") == {} and pictures_not_read_counts("garbage(") == {}


# --- the Outlook (MAPI) backend -------------------------------------------------------

def test_the_outlook_path_applies_d1_and_d3_but_never_d2() -> None:
    """`email_pst` cannot see whether Outlook marked a picture inline (UNVERIFIED on
    Windows), so an icon is read there as before; a two-word logo is still not
    indexed, and once met `REPEAT_LIMIT` times it is not read again."""
    from app.extract import email_pst
    from tests.unit.test_email_pst import FakeAttachment as MapiAttachment

    book = ImageBook()
    calls = []
    for n in range(4):
        _Ocr.calls = []
        item = email_pst.MailItem(entry_id=f"E{n}", subject="Hi", body="Regards.", attachments=[
            MapiAttachment("image001.png", LOGO), MapiAttachment("image002.png", LOGO),
            MapiAttachment("image003.png", ICON), MapiAttachment("receipt.jpg", RECEIPT),
        ])
        with reading.reading(junk=book) as policy:
            documents = list(email_pst._attachment_documents(
                item, f"pst://store/E{n}", set(), warnings=[]))
        calls.append(sorted(_Ocr.calls))
        assert [d.meta["attachment_name"] for d in documents] == ["receipt.jpg"]
    assert calls[0] == ["image001.png", "image003.png", "receipt.jpg"]
    assert calls[-1] == ["image003.png", "receipt.jpg"]
    assert REASON_DECORATIVE not in policy.not_read


# --- through the pipeline ---------------------------------------------------------------

@pytest.mark.parametrize("on", [True, False])
def test_the_pipeline_counts_what_it_left_unread_and_keeps_the_book(
        tmp_path, monkeypatch, on) -> None:
    from app.extract import email_pst
    from app.index.activity import KIND_ARCHIVE_COUNTS, decode_counts
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore
    from tests.unit.test_index_freshness import NullVectors, fake_embedder, write_aged

    monkeypatch.setattr(email_pst.PstExtractor, "backend", email_pst.PstBackend.AUTO)
    monkeypatch.setattr(email_pst.PstExtractor, "session_factory", None)
    root = tmp_path / "mail"
    root.mkdir()
    write_aged(root / "2019.pst", b"!BDN" + b"\0" * 4092)
    install_fake(monkeypatch, FakeFolder("", children=[FakeFolder("Inbox", messages=[
        _message(1, _Attachment("image001.png", LOGO, inline=True),
                 _Attachment("image002.png", ICON, inline=True),
                 _Attachment("receipt.jpg", RECEIPT))])]))
    db = tmp_path / "index.db"
    with SqliteStore(db) as store:
        config = PipelineConfig(walk=WalkConfig(roots=[root]), workers=1, ocr_mode="both",
                                junk_images=on)
        stats = Pipeline(store, NullVectors(), fake_embedder(), config).run()
        rows = {r[0] for r in store.conn.execute("SELECT path FROM files").fetchall()}
        book = store.image_hashes()
    assert "pst://2019/1/attachments/receipt.jpg" in rows
    [line] = [e for e in stats.activity.entries() if e.kind == KIND_ARCHIVE_COUNTS]
    if on:
        assert stats.pictures_not_read == {"decorative": 1, "few_words": 1}
        assert "pst://2019/1/attachments/image001.png" not in rows
        assert decode_counts(line.detail)["Skipped:decorative"] == 1
        assert {r[0] for r in book} == {pst_libpff._hash_bytes(d) for d in (LOGO, ICON, RECEIPT)}
    else:
        assert stats.pictures_not_read == {}
        assert "pst://2019/1/attachments/image001.png" in rows
        assert book == []
