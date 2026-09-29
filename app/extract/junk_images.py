r"""Signature logos, social icons, tracking pixels and dividers in email: not read.

Layer: L2

Order 0z lane D (2026-09-29). The owner: *signature and junk images attached
to emails are being OCR'd, which is slow and useless.* A company logo repeated
in the signature of four thousand messages, a row of social-media icons, a
one-pixel tracking image and a 600x2 divider each cost an OCR call and give
nothing anybody searches for. This module decides which pictures attached to
a message are not worth reading, and remembers what it learnt so the same
picture is never read twice for nothing.

## The rules, and what they may never do

Each rule is one line of the order (`docs/WORKORDER-robust-indexing-and-
status.md`, lane D), and each is chosen so that a screenshot, a scan, a
receipt or a photograph is never among what it leaves out:

* **D2 - decorative.** An attachment the message itself marks as part of its
  layout (hidden, or referenced from the HTML body by `cid:`) *and* tiny or
  divider-shaped, by the pixel size in the image's header (no decode). A
  pasted screenshot is inline too - which is why "inline" alone is never
  enough - but a screenshot is not 100 pixels a side, and not 600x4.
* **D1 - repeated, with no words.** The same bytes seen `REPEAT_LIMIT` or more
  times, whose one reading gave fewer than `MIN_WORDS` words. The book
  (`ImageBook`) spans every archive and every run (`app/index/image_book.py`
  keeps it in SQLite). Identical bytes read the same way twice give the same
  words, so skipping the second read loses nothing the first did not.
* **D3 - fewer than three words after OCR.** The text is not indexed and the
  hash joins D1. `ocr` text only: a photograph that OCR found nothing in but
  Florence-2 described is a description, and it is kept.
* **D4 - near-identical logos.** The same logo re-encoded by a mail client
  (every byte different, so D1 cannot see it) is matched by perceptual hash,
  only among small images, only with the same pixel size give or take
  `NEAR_SIZE_SLACK`, and only against pictures that were read and gave no
  words `REPEAT_LIMIT` or more times between them.

**Not used, by the owner's decision** (the order's closing section): the file
name (`image001.png` is also what Outlook calls a pasted screenshot), the byte
size alone, the position in the email, and colour variance.

Every picture left unread still ends as one status word, `Skipped`, with one
of the `REASON_*` codes; the pipeline counts them per reason and shows the
count on the Indexing page.

**Pure, and Qt- and store-free.** The book is an in-memory map with a lock;
where it is loaded from and saved to is the index layer's business.
"""

from __future__ import annotations

import io
import re
import threading
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Optional

__all__ = [
    "BookEntry",
    "CHARS_PER_WORD",
    "DIVIDER_RATIO",
    "DIVIDER_SIDE",
    "FLAT_BITS",
    "ImageBook",
    "LOGO_MAX_PIXELS",
    "MIN_WORDS",
    "NEAR_BITS",
    "NEAR_SIZE_SLACK",
    "REASONS",
    "REASON_DECORATIVE",
    "REASON_FEW_WORDS",
    "REASON_REPEATED",
    "REASON_TEXT",
    "REPEAT_LIMIT",
    "Screened",
    "TINY_SIDE",
    "count_words",
    "decorative",
    "header_size",
    "is_inline",
    "perceptual_hash",
    "screen",
    "settle",
]

#: Fewer words than this after OCR and the text is not indexed (D3). The
#: order's number. A logo reads as its company name - one or two words - and
#: a social icon as a letter or nothing; a receipt, a screenshot or a scanned
#: page reads as dozens.
MIN_WORDS = 3

#: Seen this many times, across every archive and run, and read once with
#: fewer than `MIN_WORDS` words: not read again (D1). The order's number.
REPEAT_LIMIT = 5

#: D2: "tiny" means both sides at or under this many pixels. Social icons are
#: 16 to 48 pixels, drawn at twice that on a high-density screen; 100 covers
#: both. A pasted screenshot holding three words of 12-point text is already
#: wider than this.
TINY_SIDE = 100

#: D2: "divider-shaped" means the short side at or under `DIVIDER_SIDE` pixels
#: and the long side at least `DIVIDER_RATIO` times it. A rule line, a spacer,
#: a coloured bar. One line of the smallest legible text is taller than 12
#: pixels, so nothing this thin holds a word OCR could read.
DIVIDER_SIDE = 12
DIVIDER_RATIO = 8

#: D4 looks only at pictures this small (pixels). Signature logos and banners
#: are a few hundred pixels across; a photograph or a screenshot is millions,
#: and decoding it for a hash it will never match would cost time for nothing.
LOGO_MAX_PIXELS = 640 * 480

#: D4: two perceptual hashes this many bits apart or fewer are the same logo.
#: Far tighter than the photo-folding threshold (`PHASH_NEAR_THRESHOLD`, 16):
#: a folded photo is still shown, a matched logo is not read at all. Measured
#: on the lane D corpus (2026-09-29, 68 small pictures): one logo re-saved as
#: JPEG at six qualities, 0 bits from the original every time; the closest two
#: *different* pictures, 14 bits; the same glyph drawn at another size, 2-6
#: (and never compared, because the sizes differ).
NEAR_BITS = 6

#: D4 ignores a hash with fewer set bits than this, or more than 64 minus it.
#: `phash` sets each bit by comparing a coefficient with the median, so a
#: picture with any detail lands at or near 32. A flat one - a blank, a
#: divider, a one-pixel image - has nothing to compare and hashes to all
#: zeros: measured, a blank 300x60 and a 600x2 divider were 0 bits apart.
#: Such a hash says nothing about what the picture is, so it never matches.
FLAT_BITS = 16

#: D4: the two pictures' widths and heights may differ by this fraction.
NEAR_SIZE_SLACK = 0.10

#: Why a picture was not read. Codes, not sentences: the words live in the
#: presenter (`app/ui/presenter/activity.py`, `app/ui/presenter/indexing.py`).
REASON_DECORATIVE = "decorative"      # D2
REASON_REPEATED = "repeated"          # D1 and D4
REASON_FEW_WORDS = "few_words"        # D3
REASONS = (REASON_DECORATIVE, REASON_REPEATED, REASON_FEW_WORDS)

#: For a log line: the reason as the order words it.
REASON_TEXT = {
    REASON_DECORATIVE: "decorative image, not read",
    REASON_REPEATED: "repeated image with no words, not read again",
    REASON_FEW_WORDS: "fewer than three words, not indexed",
}

# MAPI attachment properties, per [MS-OXPROPS]. Read from the attachment's own
# record set, the same way `pst_libpff._attachment_name` reads its file name.
_PROP_ATTACH_CONTENT_ID = 0x3712
_PROP_ATTACH_CONTENT_LOCATION = 0x3713
_PROP_ATTACH_FLAGS = 0x3714
_PROP_ATTACHMENT_HIDDEN = 0x7FFE
#: `PR_ATTACH_FLAGS` bit: the attachment is referenced from the HTML body.
_ATT_MHTML_REF = 0x4

#: `count_words`: letters and digits per word when OCR has dropped the spaces.
#: English averages under five letters a word; five errs towards counting a
#: line of real text as enough words.
CHARS_PER_WORD = 5

# Letters in the scripts written without spaces: each character counts as a
# word, or a Chinese receipt would read as "two words" and be dropped.
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")
_TOKEN = re.compile(r"[^\W_]+")


def count_words(text: str) -> int:
    """How many words OCR found, allowing for the spaces it drops.

    Runs of two or more letters or digits, **or** the letters and digits
    divided by `CHARS_PER_WORD`, whichever is more. Measured on this corpus:
    RapidOCR read a pasted screenshot of twelve six-word lines as
    `orderagendadeliveryagendameetingorder` - one "word" a line - and a banner
    as `Savethedate:AnnualConference2026`. Counting tokens alone would have
    called both "fewer than three words" and dropped real text; counted by
    length, they are 7 and 6 words a line. A logo stays short either way:
    `ACME Ltd` is 2, `Northwind` 1, a LinkedIn icon's `in` 1.

    Single characters are not tokens - OCR turns a social icon into "f" and
    a speck into "l" - except in the scripts written without spaces, where
    every character counts, or a Chinese receipt would read as "two words".
    """
    tokens = chars = 0
    for token in _TOKEN.findall(str(text or "")):
        cjk = len(_CJK.findall(token))
        if cjk:
            tokens += cjk + (1 if len(token) - cjk >= 2 else 0)
            chars += len(token) - cjk
        else:
            if len(token) >= 2:
                tokens += 1
            chars += len(token)
    return max(tokens, chars // CHARS_PER_WORD)


def header_size(data: bytes) -> Optional[tuple[int, int]]:
    """`(width, height)` from the image's header, without decoding the pixels.

    `PIL.Image.open` is lazy: it reads the header and stops. `None` when
    Pillow is missing or cannot tell - the caller then reads the picture as
    it always did, because an unknown size proves nothing.
    """
    try:
        from PIL import Image

        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
        return int(width), int(height)
    except Exception:                            # noqa: BLE001 - unknown, not junk
        return None


def decorative(inline: bool, size: Optional[tuple[int, int]]) -> bool:
    """D2: inline or hidden, *and* tiny or divider-shaped. See the module docstring."""
    if not inline or size is None:
        return False
    width, height = size
    if width <= 0 or height <= 0:
        return False
    if width <= TINY_SIDE and height <= TINY_SIDE:
        return True
    short, long_ = min(width, height), max(width, height)
    return short <= DIVIDER_SIDE and long_ >= DIVIDER_RATIO * short


def is_inline(attachment: Any) -> bool:
    """Is this libpff attachment part of the message's layout? Never raises.

    Hidden (`PR_ATTACHMENT_HIDDEN`), carrying a content id or content location
    that the HTML body points at, or flagged `ATT_MHTML_REF`. Outlook marks a
    signature logo all three ways; a file somebody attached on purpose,
    none of them. **A pasted screenshot is also inline**, which is why D2
    never acts on this alone.
    """
    try:
        for set_index in range(attachment.get_number_of_record_sets()):
            record_set = attachment.get_record_set(set_index)
            for entry_index in range(record_set.get_number_of_entries()):
                entry = record_set.get_entry(entry_index)
                try:
                    tag = entry.get_entry_type()
                except Exception:                # noqa: BLE001
                    continue
                if tag == _PROP_ATTACHMENT_HIDDEN:
                    if _as_bool(entry):
                        return True
                elif tag in (_PROP_ATTACH_CONTENT_ID, _PROP_ATTACH_CONTENT_LOCATION):
                    try:
                        if str(entry.get_data_as_string() or "").strip():
                            return True
                    except Exception:            # noqa: BLE001 - absent or not text
                        continue
                elif tag == _PROP_ATTACH_FLAGS:
                    try:
                        if int(entry.get_data_as_integer()) & _ATT_MHTML_REF:
                            return True
                    except Exception:            # noqa: BLE001
                        continue
    except Exception:                            # noqa: BLE001 - one bad attachment
        return False
    return False


def _as_bool(entry: Any) -> bool:
    try:
        return bool(entry.get_data_as_boolean())
    except Exception:                            # noqa: BLE001
        try:
            return bool(entry.get_data_as_integer())
        except Exception:                        # noqa: BLE001
            return False


def perceptual_hash(data: bytes, size: Optional[tuple[int, int]]) -> Optional[int]:
    """D4's fingerprint for a small picture, as a 64-bit integer, or `None`.

    `None` for anything over `LOGO_MAX_PIXELS` (never decoded), without
    Pillow or `imagehash`, or for a picture that will not decode. The same
    `imagehash.phash` the photo folding uses (`app/index/phash.py`).
    """
    if size is None or size[0] * size[1] > LOGO_MAX_PIXELS:
        return None
    try:
        import imagehash
        from PIL import Image

        from app.index.phash import PHASH_HASH_SIZE

        with Image.open(io.BytesIO(data)) as image:
            return int(str(imagehash.phash(image, hash_size=PHASH_HASH_SIZE)), 16)
    except Exception:                            # noqa: BLE001 - no D4 for this one
        return None


@dataclass
class BookEntry:
    """What is known about one picture's bytes.

    * `seen` - how many times these bytes were met, read or not, in every
      archive and run.
    * `words` - how many words its one reading gave (`count_words`), or
      `None` if it has never been read to the end.
    * `phash`, `width`, `height` - for D4; `None`/0 when not computed.
    """

    seen: int = 0
    words: Optional[int] = None
    phash: Optional[int] = None
    width: int = 0
    height: int = 0

    @property
    def wordless(self) -> bool:
        return self.words is not None and self.words < MIN_WORDS


class ImageBook:
    """Every picture's hash, how often it was met and what reading it gave.

    One per run, shared by every extraction thread, so every method takes the
    lock. `dirty()` hands the index layer what changed since it last saved.
    A reader outside the pipeline gets a fresh, unsaved one per read.
    """

    def __init__(self, entries: Optional[Iterable[tuple[str, BookEntry]]] = None) -> None:
        self._lock = threading.Lock()
        self._entries: dict[str, BookEntry] = dict(entries or ())
        self._dirty: set[str] = set()
        # D4 looks only at pictures with a perceptual hash - a few logos among
        # every photograph ever attached - so it walks this, not the whole book.
        self._hashed: dict[str, BookEntry] = {
            digest: entry for digest, entry in self._entries.items() if entry.phash is not None}

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def get(self, digest: str) -> Optional[BookEntry]:
        with self._lock:
            entry = self._entries.get(digest)
            return None if entry is None else BookEntry(**vars(entry))

    def saw(self, digest: str) -> None:
        """These bytes were met once more."""
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                entry = self._entries[digest] = BookEntry()
            entry.seen += 1
            self._dirty.add(digest)

    def repeated_without_words(self, digest: str) -> bool:
        """D1: met `REPEAT_LIMIT` times or more, and its reading gave too few words."""
        with self._lock:
            entry = self._entries.get(digest)
            return bool(entry is not None and entry.wordless and entry.seen >= REPEAT_LIMIT)

    def read(self, digest: str, words: int, *, phash: Optional[int] = None,
             size: Optional[tuple[int, int]] = None) -> None:
        """Record what reading these bytes gave."""
        with self._lock:
            entry = self._entries.get(digest)
            if entry is None:
                entry = self._entries[digest] = BookEntry(seen=1)
            entry.words = max(0, int(words))
            if phash is not None:
                entry.phash = int(phash)
                self._hashed[digest] = entry
            if size is not None:
                entry.width, entry.height = int(size[0]), int(size[1])
            self._dirty.add(digest)

    def looks_like_repeated_logo(self, phash: Optional[int],
                                 size: Optional[tuple[int, int]]) -> bool:
        """D4: near-identical pictures of the same size, together met `REPEAT_LIMIT`
        times, every one of which gave too few words when read.

        One near-identical picture that *did* give words is enough to say no:
        a match that is sometimes real text is not a logo.
        """
        if phash is None or size is None or not _informative(phash):
            return False
        width, height = size
        total = 0
        with self._lock:
            for entry in self._hashed.values():
                if entry.phash is None or entry.words is None:
                    continue
                if not _same_size(entry.width, width) or not _same_size(entry.height, height):
                    continue
                if bin(entry.phash ^ phash).count("1") > NEAR_BITS:
                    continue
                if not entry.wordless:
                    return False
                total += entry.seen
        return total >= REPEAT_LIMIT

    def dirty(self) -> list[tuple[str, BookEntry]]:
        """What changed since the last `clean`, as copies."""
        with self._lock:
            return [(digest, BookEntry(**vars(self._entries[digest])))
                    for digest in sorted(self._dirty) if digest in self._entries]

    def clean(self, digests: Iterable[str]) -> None:
        """These were saved. A digest changed again meanwhile is saved next time."""
        with self._lock:
            self._dirty.difference_update(digests)

    def entries(self) -> Iterator[tuple[str, BookEntry]]:
        with self._lock:
            items = [(digest, BookEntry(**vars(entry)))
                     for digest, entry in self._entries.items()]
        return iter(items)


@dataclass
class Screened:
    """What `screen` found out about one picture before it was read.

    `reason` is a `REASON_*` code when the picture should not be read, else
    `""`. `size` and `phash` are kept for `settle`, so nothing is worked out
    twice.
    """

    reason: str = ""
    size: Optional[tuple[int, int]] = None
    phash: Optional[int] = None


def screen(book: Optional[ImageBook], digest: str, data: bytes, *,
           inline: bool) -> Screened:
    """D2, then D1, then D4, cheapest first. Never raises; `book=None` is the filter off.

    Call it once per picture attachment *after* `book.saw(digest)`, so this
    sighting counts towards `REPEAT_LIMIT`.
    """
    if book is None:
        return Screened()
    try:
        size = header_size(data)
        if decorative(inline, size):
            return Screened(REASON_DECORATIVE, size)
        if book.repeated_without_words(digest):
            return Screened(REASON_REPEATED, size)
        phash = perceptual_hash(data, size)
        if book.looks_like_repeated_logo(phash, size):
            return Screened(REASON_REPEATED, size, phash)
        return Screened("", size, phash)
    except Exception:                            # noqa: BLE001 - read it, as before
        return Screened()


def settle(book: Optional[ImageBook], digest: str, documents: list[Any],
           screened: Optional[Screened] = None) -> tuple[list[Any], str]:
    """D3, after a picture was read: what to keep, and why not when nothing is.

    Returns `(documents to index, reason)`. Every document came from OCR
    (`meta["format"] == "ocr"`) and together they hold fewer than `MIN_WORDS`
    words: nothing is kept and the reason is `REASON_FEW_WORDS`. Anything else
    - a Florence-2 description, a reader that is not OCR - is kept as it is.
    The words are written into the book either way (D1 needs them), and an
    empty `documents` (OCR found nothing) is recorded as no words.
    """
    if book is None:
        return documents, ""
    screened = screened or Screened()
    try:
        ocr_only = all((getattr(d, "meta", None) or {}).get("format") == "ocr"
                       for d in documents)
        words = sum(count_words(getattr(d, "text", "")) for d in documents)
        book.read(digest, words, phash=screened.phash, size=screened.size)
        if documents and ocr_only and words < MIN_WORDS:
            return [], REASON_FEW_WORDS
    except Exception:                            # noqa: BLE001 - keep what was read
        return documents, ""
    return documents, ""


def _informative(phash: int) -> bool:
    """Does this hash describe a picture with some detail? See `FLAT_BITS`."""
    bits = bin(phash).count("1")
    return FLAT_BITS <= bits <= 64 - FLAT_BITS


def _same_size(a: int, b: int) -> bool:
    if a <= 0 or b <= 0:
        return False
    return abs(a - b) <= NEAR_SIZE_SLACK * max(a, b)
