r"""A made-up corpus to index, identical every time it is made from the same seed.

Layer: L3 (tooling for the indexing benchmark - nothing in the app calls this)

**Why this exists.** Work order 0x item 5a asks for "a repeatable benchmark",
and 2d asks for before-and-after numbers "on the same synthetic corpus". Both
need a pile of files that is the same on the owner's Windows machine, on a Mac
and in a Linux sandbox, so that two numbers can be compared at all. Timing
somebody's real folder measures their folder, not the change being tested.

`app/index/index_bench.py` already has a tiny corpus of 240 `.txt` files. That
one exists to calibrate auto-tune in about a minute; it is too small and too
plain to show where a real run spends its time. This one has the shapes that
make real runs slow:

* ordinary documents - `.txt`, `.md`, `.html`, `.docx`;
* one **large `.mbox`** holding thousands of messages, some with attachments
  (one file on disk, thousands of documents inside - the shape that makes a
  progress bar sit still);
* one **`.zip` with hundreds of members, including a zip inside it** (the
  archive reader, its safety budget and its nesting);
* a folder of loose **`.eml`** messages.

**Deterministic, byte for byte.** Everything is drawn from `random.Random(seed)`
and every timestamp is a fixed date, including the ones hidden inside zip
entries and MIME boundaries (the standard library would otherwise pick a random
boundary and today's date). The same seed and sizes give the same bytes on every
operating system, and `CorpusManifest.digest` proves it.

**No network, nothing committed.** Every byte is generated on the caller's
machine. The `.docx` files are written by hand from the three XML parts Word
needs, rather than with python-docx, because python-docx stamps the current
time into every file and the result would differ on every run.

Nothing here reads or writes anywhere except the folder the caller names.
"""

from __future__ import annotations

import hashlib
import io
import json
import random
import zipfile
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from datetime import UTC
from email.generator import BytesGenerator
from email.message import EmailMessage
from email.policy import SMTP
from pathlib import Path
from typing import Any

__all__ = [
    "CORPUS_DIR",
    "MANIFEST_NAME",
    "SIZES",
    "CorpusManifest",
    "CorpusSpec",
    "generate",
    "load_manifest",
]

#: The sub-folder that holds the files to index. The manifest sits *beside* it,
#: not inside it, so the benchmark never indexes its own bookkeeping.
CORPUS_DIR = "corpus"

#: The file that records what was generated, next to `CORPUS_DIR`.
MANIFEST_NAME = "corpus-manifest.json"

#: Bumped whenever the generator changes what it writes. Two corpora made by
#: different versions of this file are different corpora even with the same
#: seed, and a report has to be able to say so.
GENERATOR_VERSION = 1

#: Every date in the corpus is this one plus a whole number of hours. A fixed
#: start is what keeps the bytes identical from run to run.
_EPOCH = (2024, 3, 1)

#: The date stamped on every entry inside a zip (year, month, day, h, m, s).
#: Zip entries carry a modification time; without a fixed one each run would
#: produce a different file.
_ZIP_TIME = (2024, 3, 1, 9, 0, 0)


@dataclass(frozen=True)
class CorpusSpec:
    """How big the corpus should be. Every field is a count, not a size in bytes.

    Counts rather than bytes because the pipeline's cost is mostly per
    document and per chunk; "200MB" could be three big files or thirty
    thousand small ones and those take wildly different times.
    """

    #: Loose documents, split evenly between .txt, .md, .html and .docx.
    documents: int = 400
    #: Roughly how many words each document holds. Varied +-50% per file so
    #: the chunker sees both short and long inputs.
    words_per_document: int = 600
    #: Messages in the one big `.mbox`.
    mbox_messages: int = 2000
    #: Every Nth mbox message carries an attachment (a small text file or a
    #: `.docx`). 0 means none do.
    attachment_every: int = 5
    #: Members in the outer `.zip`, not counting the nested zip itself.
    zip_members: int = 300
    #: Members in the zip *inside* the zip.
    nested_zip_members: int = 40
    #: Loose `.eml` files in their own folder.
    eml_files: int = 200
    #: The seed. Same seed and same counts, same bytes.
    seed: int = 1

    def as_dict(self) -> dict[str, Any]:
        """The spec as a plain dictionary, for JSON reports."""
        return asdict(self)


#: Named sizes, so a person can say "medium" instead of seven numbers. The
#: names are what goes into a report, which is why they are short and fixed.
#:
#: `tiny` is for tests (well under a second to make and index with the fake
#: embedder). `small` is the default: big enough to have a real mbox and zip,
#: small enough to index in a minute or two with the fake embedder. `large` is
#: for the owner's machine, where a run of tens of minutes is acceptable.
SIZES: dict[str, CorpusSpec] = {
    "tiny": CorpusSpec(documents=8, words_per_document=150, mbox_messages=12,
                       attachment_every=4, zip_members=10, nested_zip_members=3,
                       eml_files=4),
    "small": CorpusSpec(),
    "medium": CorpusSpec(documents=2000, mbox_messages=10_000,
                         zip_members=800, nested_zip_members=100,
                         eml_files=1000),
    "large": CorpusSpec(documents=8000, words_per_document=900,
                        mbox_messages=40_000, zip_members=2000,
                        nested_zip_members=200, eml_files=4000),
}


@dataclass
class CorpusManifest:
    """What `generate` actually wrote. Saved next to the corpus as JSON.

    The benchmark report copies this in whole, so every number in the report
    can be traced back to exactly which corpus produced it.
    """

    #: The spec it was made from.
    spec: dict[str, Any] = field(default_factory=dict)
    #: The size name (`small`, ...), or `custom` when counts were given.
    size_name: str = "custom"
    generator_version: int = GENERATOR_VERSION
    #: Files on disk under the corpus folder (the mbox and the zip count once).
    files: int = 0
    #: Total bytes of those files.
    bytes: int = 0
    #: How many documents the pipeline should report: every loose document,
    #: every message, every zip member that is itself a document. The report
    #: puts the pipeline's own count beside it, so a reader that silently
    #: dropped half the mbox shows up as a mismatch rather than as "fast".
    expected_documents: int = 0
    #: Messages that carry an attachment. **Not** counted in
    #: `expected_documents`: as of v0.3.3 the mail readers record an
    #: attachment's *name* on its message and do not read its contents as a
    #: separate document (`app/extract/email_files.py`).
    attachments: int = 0
    #: Files per extension, e.g. {"txt": 100, "mbox": 1}.
    by_extension: dict[str, int] = field(default_factory=dict)
    #: SHA-256 over every file's relative path and bytes, in sorted order. Two
    #: corpora with the same digest are the same corpus; this is the proof of
    #: determinism the tests check.
    digest: str = ""

    def as_dict(self) -> dict[str, Any]:
        """The manifest as a plain dictionary, for JSON."""
        return asdict(self)


# ---------------------------------------------------------------------------
# Words. Real English in the project's own domain, not lorem ipsum: the chunker
# splits on sentences and the tokeniser is English, so nonsense words would
# measure a code path no real corpus takes.
# ---------------------------------------------------------------------------

def _words(text: str) -> list[str]:
    """Split a line of space-separated words into a list.

    The word lists below are written as plain sentences-worth of text because
    that is far easier to read and edit than a list of quoted strings.
    """
    return text.split()


_NOUNS = _words(
    "pump station report valve manifold budget forecast schedule vessel "
    "certificate record drawing permit contractor handover invoice meeting "
    "project homework essay garden holiday recipe school teacher football "
    "kitchen roof boiler insurance mortgage passport appointment dentist "
    "spreadsheet presentation proposal contract warranty receipt ticket "
    "flight hotel booking survey inspection audit training induction policy"
)
_VERBS = _words(
    "reviewed approved updated sent attached signed scheduled cancelled "
    "checked replaced ordered finished started discussed agreed moved paid "
    "printed booked confirmed postponed measured calibrated submitted"
)
_ADJECTIVES = _words(
    "quarterly annual urgent final draft revised new old monthly weekly "
    "important routine overdue complete partial signed unsigned shared"
)
_NAMES = _words(
    "Amelia Oliver Isla George Ava Noah Mia Leo Ivy Arthur Grace Oscar "
    "Priya Ravi Sofia Mateo Hannah Yusuf Chloe Daniel"
)
_SURNAMES = _words(
    "Smith Patel Jones Taylor Brown Wilson Khan Evans Thomas Roberts "
    "Walker Wright Green Hall Wood"
)
_PLACES = _words(
    "Leeds Manchester Bristol London Glasgow Cardiff York Bath Oxford "
    "Cambridge Norwich Exeter"
)


class _Writer:
    """Draws words, sentences and whole files from one seeded random stream.

    One object per corpus, so the order things are generated in fixes every
    byte. Nothing here may call `random` without going through `self.rng`, or
    two runs with the same seed would stop matching.
    """

    def __init__(self, seed: int) -> None:
        """Start a fresh random stream from `seed`."""
        self.rng = random.Random(seed)

    # -- text ---------------------------------------------------------------

    def person(self) -> tuple[str, str]:
        """A made-up person: (display name, email address)."""
        first = self.rng.choice(_NAMES)
        last = self.rng.choice(_SURNAMES)
        return f"{first} {last}", f"{first.lower()}.{last.lower()}@example.org"

    def sentence(self) -> str:
        """One plain sentence, 8-18 words, ending in a full stop.

        The chunker looks for sentence breaks, so real-looking sentences make
        it take the same path it takes on a real document.
        """
        rng = self.rng
        words = [
            "The", rng.choice(_ADJECTIVES), rng.choice(_NOUNS), "for",
            rng.choice(_PLACES), "was", rng.choice(_VERBS), "by",
            rng.choice(_NAMES),
        ]
        for _ in range(rng.randint(0, 9)):
            words.append(rng.choice(_NOUNS + _ADJECTIVES + _VERBS))
        return " ".join(words) + "."

    def paragraphs(self, words: int) -> list[str]:
        """Roughly `words` words, as a list of paragraphs of 3-7 sentences."""
        out: list[str] = []
        count = 0
        while count < words:
            para = " ".join(self.sentence() for _ in range(self.rng.randint(3, 7)))
            count += len(para.split())
            out.append(para)
        return out

    def title(self) -> str:
        """A short title such as 'Revised boiler report Leeds'."""
        rng = self.rng
        return (f"{rng.choice(_ADJECTIVES).title()} {rng.choice(_NOUNS)} "
                f"{rng.choice(_NOUNS)} {rng.choice(_PLACES)}")

    def document_words(self, spec: CorpusSpec) -> int:
        """How long the next document is: the spec's length, +-50%."""
        base = max(20, spec.words_per_document)
        return self.rng.randint(base // 2, base + base // 2)

    # -- file bodies --------------------------------------------------------

    def txt(self, words: int) -> bytes:
        """A plain-text document."""
        title = self.title()
        body = "\n\n".join(self.paragraphs(words))
        return f"{title}\n\n{body}\n".encode()

    def md(self, words: int) -> bytes:
        """A Markdown document with a heading per few paragraphs."""
        paras = self.paragraphs(words)
        lines = [f"# {self.title()}", ""]
        for index, para in enumerate(paras):
            if index % 3 == 0:
                lines += [f"## {self.title()}", ""]
            lines += [para, ""]
        return "\n".join(lines).encode("utf-8")

    def html(self, words: int) -> bytes:
        """A small HTML page: a title, headings and paragraphs."""
        title = self.title()
        body = "".join(f"<h2>{self.title()}</h2><p>{p}</p>\n"
                       for p in self.paragraphs(words))
        return (f"<!doctype html>\n<html><head><meta charset=\"utf-8\">"
                f"<title>{title}</title></head>\n<body><h1>{title}</h1>\n"
                f"{body}</body></html>\n").encode()

    def docx(self, words: int) -> bytes:
        """A minimal but valid Word document, with fixed timestamps.

        A `.docx` is a zip of XML files. Word (and every reader in this app)
        needs only three parts: the content-types list, the package
        relationships, and the document body itself. Written by hand so the
        bytes do not depend on the clock - see the module docstring.
        """
        from xml.sax.saxutils import escape

        paras = [self.title(), *self.paragraphs(words)]
        body = "".join(
            f"<w:p><w:r><w:t xml:space=\"preserve\">{escape(p)}</w:t></w:r></w:p>"
            for p in paras)
        parts = {
            "[Content_Types].xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                '<Default Extension="xml" ContentType="application/xml"/>'
                '<Override PartName="/word/document.xml" ContentType="application/'
                'vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
                '</Types>'),
            "_rels/.rels": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/'
                'officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>'
                '</Relationships>'),
            "word/document.xml": (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/'
                'wordprocessingml/2006/main"><w:body>' + body + '</w:body></w:document>'),
        }
        return _zip_bytes((name, text.encode("utf-8")) for name, text in parts.items())

    def document(self, kind: str, words: int) -> bytes:
        """The body of one document of the given kind (txt, md, html, docx)."""
        return {"txt": self.txt, "md": self.md, "html": self.html,
                "docx": self.docx}[kind](words)

    # -- mail ---------------------------------------------------------------

    def message(self, number: int, *, attach: bool) -> EmailMessage:
        """One email. Every header and MIME boundary is fixed by `number`.

        `attach=True` adds either a small text attachment or a `.docx`, so
        the attachment path through the reader is exercised too.
        """
        rng = self.rng
        sender_name, sender = self.person()
        to_name, to = self.person()
        msg = EmailMessage()
        msg["From"] = f"{sender_name} <{sender}>"
        msg["To"] = f"{to_name} <{to}>"
        msg["Subject"] = f"{self.title()} ({number})"
        hours = number * 3 + rng.randint(0, 2)
        msg["Date"] = _mail_date(hours)
        msg["Message-ID"] = f"<bench-{number}@example.org>"
        body = "\n\n".join(self.paragraphs(rng.randint(60, 260)))
        msg.set_content(f"Hi {to_name.split()[0]},\n\n{body}\n\nThanks,\n{sender_name}\n")
        if attach:
            if number % 2:
                msg.add_attachment(self.txt(rng.randint(80, 300)), maintype="text",
                                   subtype="plain", filename=f"notes-{number}.txt")
            else:
                msg.add_attachment(
                    self.docx(rng.randint(80, 300)), maintype="application",
                    subtype="vnd.openxmlformats-officedocument.wordprocessingml.document",
                    filename=f"report-{number}.docx")
            # The standard library invents a random boundary when it writes a
            # multipart message. A fixed one keeps the bytes identical.
            msg.set_boundary(f"==bench-boundary-{number}==")
        return msg


def _mail_date(hours: int) -> str:
    """An RFC 2822 date `hours` after the fixed start, always in UTC."""
    from datetime import datetime, timedelta
    from email.utils import format_datetime

    start = datetime(*_EPOCH, tzinfo=UTC)
    return format_datetime(start + timedelta(hours=hours))


def _zip_bytes(members: Iterator[tuple[str, bytes]]) -> bytes:
    """A zip file, in memory, whose every entry has the same fixed date.

    `ZipFile.writestr` with a bare name stamps today's date on the entry;
    passing a `ZipInfo` with `_ZIP_TIME` is what makes the output repeatable.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, data in members:
            info = zipfile.ZipInfo(name, date_time=_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            # Normal file permissions (rw-r--r--), fixed so the bytes do not
            # depend on the caller's umask.
            info.external_attr = 0o644 << 16
            zf.writestr(info, data)
    return buffer.getvalue()


def _message_bytes(msg: EmailMessage) -> bytes:
    """An email as bytes with `\\n` line ends on every platform.

    `SMTP` policy would use `\\r\\n`; the mbox format and the `.eml` files are
    written with `\\n` so the bytes are the same on Windows and elsewhere.
    """
    buffer = io.BytesIO()
    BytesGenerator(buffer, policy=SMTP.clone(linesep="\n")).flatten(msg)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# The public entry points
# ---------------------------------------------------------------------------

def generate(folder: Path, spec: CorpusSpec | None = None, *,
             size_name: str = "custom") -> CorpusManifest:
    """Write a corpus into `folder/corpus/` and its manifest beside it.

    `folder` is created if needed. If `folder/corpus/` already exists it is
    **refused** rather than overwritten, unless it is empty: deleting
    somebody's files because they named the wrong folder is not a risk a
    benchmark is allowed to take. Callers that want a fresh corpus pass a
    fresh folder (or use `load_manifest` to reuse one).

    Returns the manifest, which is also saved as `folder/corpus-manifest.json`.
    """
    spec = spec or SIZES["small"]
    folder = Path(folder)
    root = folder / CORPUS_DIR
    if root.exists() and any(root.iterdir()):
        raise FileExistsError(
            f"{root} already has files in it. Pick an empty folder - this "
            "never deletes anything it did not just create.")
    root.mkdir(parents=True, exist_ok=True)

    writer = _Writer(spec.seed)
    expected = 0
    attachments = 0

    # 1. Loose documents, spread over a few sub-folders the way real ones are.
    kinds = ("txt", "md", "html", "docx")
    for number in range(spec.documents):
        kind = kinds[number % len(kinds)]
        sub = root / "documents" / f"folder{number % 7}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"doc{number:05d}.{kind}").write_bytes(
            writer.document(kind, writer.document_words(spec)))
        expected += 1

    # 2. One big mbox, written by hand rather than through `mailbox.mbox`.
    # The standard library class writes the platform's line ending (`\r\n` on
    # Windows), which would make the Windows corpus a different corpus. The
    # format is simple enough to write directly: a "From " separator line per
    # message, the message itself, a blank line, and any body line that starts
    # with "From " escaped as ">From " so it is not mistaken for a separator.
    mail_dir = root / "mail"
    mail_dir.mkdir(parents=True, exist_ok=True)
    with (mail_dir / "archive.mbox").open("wb") as out:
        for number in range(spec.mbox_messages):
            attach = bool(spec.attachment_every) and number % spec.attachment_every == 0
            msg = writer.message(number, attach=attach)
            out.write(b"From bench@example.org " + _from_line_time(number) + b"\n")
            for line in _message_bytes(msg).split(b"\n"):
                if line.startswith(b"From "):
                    line = b">" + line
                out.write(line + b"\n")
            out.write(b"\n")
            expected += 1
            attachments += int(attach)

    # 3. Loose .eml files.
    eml_dir = root / "mail" / "eml"
    eml_dir.mkdir(parents=True, exist_ok=True)
    for number in range(spec.eml_files):
        # Numbered after the mbox so no two messages share a Message-ID.
        ident = spec.mbox_messages + number
        attach = bool(spec.attachment_every) and number % spec.attachment_every == 0
        (eml_dir / f"message{number:05d}.eml").write_bytes(
            _message_bytes(writer.message(ident, attach=attach)))
        expected += 1
        attachments += int(attach)

    # 4. The zip, with a zip inside it.
    nested = []
    for number in range(spec.nested_zip_members):
        kind = kinds[number % len(kinds)]
        nested.append((f"inner/doc{number:04d}.{kind}",
                       writer.document(kind, writer.document_words(spec) // 2)))
        expected += 1
    members = []
    for number in range(spec.zip_members):
        kind = kinds[number % len(kinds)]
        members.append((f"backup/section{number % 5}/item{number:04d}.{kind}",
                        writer.document(kind, writer.document_words(spec) // 2)))
        expected += 1
    if nested:
        members.append(("backup/nested.zip", _zip_bytes(iter(nested))))
    archive_dir = root / "archives"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / "backup.zip").write_bytes(_zip_bytes(iter(members)))

    manifest = _survey(root, spec, size_name, expected)
    manifest.attachments = attachments
    (folder / MANIFEST_NAME).write_text(
        json.dumps(manifest.as_dict(), indent=2), encoding="utf-8")
    return manifest


def _from_line_time(number: int) -> bytes:
    """The fixed date on message `number`'s mbox "From " line, in the
    `asctime` shape the format uses (e.g. `Fri Mar  1 09:00:00 2024`)."""
    import time

    stamp = time.gmtime(1_709_283_600 + number * 3 * 3600)   # 2024-03-01 09:00 UTC
    return time.asctime(stamp).encode("ascii")


def _survey(root: Path, spec: CorpusSpec, size_name: str,
            expected: int) -> CorpusManifest:
    """Count, size and fingerprint what is now on disk under `root`.

    The digest covers each file's path *relative to root*, written with
    forward slashes, so the same corpus gives the same digest on Windows
    (which uses backslashes) and everywhere else.
    """
    digest = hashlib.sha256()
    manifest = CorpusManifest(spec=spec.as_dict(), size_name=size_name,
                              expected_documents=expected)
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        relative = path.relative_to(root).as_posix()
        data = path.read_bytes()
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(hashlib.sha256(data).digest())
        manifest.files += 1
        manifest.bytes += len(data)
        ext = path.suffix.lower().lstrip(".") or "(none)"
        manifest.by_extension[ext] = manifest.by_extension.get(ext, 0) + 1
    manifest.digest = digest.hexdigest()
    return manifest


def load_manifest(folder: Path) -> CorpusManifest | None:
    """The manifest saved in `folder`, or None if there is none or it is unreadable.

    Lets the runner reuse a corpus made earlier instead of generating it again
    - useful for a large corpus that takes a while to write.
    """
    path = Path(folder) / MANIFEST_NAME
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        return CorpusManifest(**raw)
    except (OSError, ValueError, TypeError):
        return None
