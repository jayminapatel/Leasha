r"""The two settings that only matter once the corpus is large.

Layer: L6 (UI), driving L3

Both are answers to the same finding: **the design scales and the schedule does
not.** Indexing 600GB is roughly five days at the throughput measured here, and
1.5TB is nearly a fortnight - so what matters at that size is not making the
work faster but doing less of it, and doing the useful part first.

* **Images and scans** decides *when* pictures of text are read, not whether.
  OCR costs about 3.6 seconds a page against roughly 0.4 for embedding a
  passage, so a single pass means nothing is searchable until everything is.
* **Re-check archives** decides how long a folder declared static is trusted.
  The recurring cost of a settled corpus is the re-walk, not the indexing.

Its own widget because `indexing_settings.py` is under the 250-line guard, and
because these two belong together: neither is worth a thought at 100GB and both
are the difference between a workable schedule and an unworkable one at a
terabyte.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QLabel, QSpinBox, QVBoxLayout,
)

from app.ui.presenter.coverage import (
    ATTACHMENTS, CODE, EMAIL, FILES, MEDIA, PICTURES, PLACES, ZIPS, place_sentence,
)
from app.ui.tuning import cost_hint

__all__ = ["LongRunBox"]


def _cost(key: str) -> QLabel:
    """The one-line "what this costs" under a coverage control.

    **Under the control, never in a modal** - §4e's rule. A cost that
    interrupts you is a cost you dismiss without reading; a cost that sits
    beside the switch is one you weigh.

    The wording comes from `tuning.cost_hint`, which is pure and refuses to
    invent a figure it has not measured.
    """
    label = QLabel(cost_hint(key))
    label.setWordWrap(True)
    label.setObjectName("costHint")
    label.setEnabled(False)              # renders as the muted secondary text
    return label


def _elsewhere(text: str) -> QLabel:
    """Where a place's levers are set, when that is another page."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setEnabled(False)              # the muted secondary text, like a cost line
    return label


class LongRunBox(QGroupBox):
    """Which pass to run, and how often to re-check an archive."""

    changed = pyqtSignal()

    def __init__(self, parent: Optional[Any] = None) -> None:
        # **Renamed rather than rebuilt.** §4c-3 asks for a Coverage group and
        # this widget already *was* one: what gets read, and therefore how long
        # a run takes. The controls, their object names and their tooltips are
        # untouched; it moved screen and gained a cost line per control.
        super().__init__("Coverage - what gets read", parent)

        # **Three choices, not two checkboxes.** "Skip OCR" and "Only OCR" as
        # separate switches have a fourth state that means nothing, and neither
        # label says the thing that matters - that this is about *when* images
        # are read.
        self.ocr_mode = QComboBox()
        self.ocr_mode.setObjectName("INDEX_OCR_MODE")
        self.ocr_mode.addItem("Text and images together", "both")
        self.ocr_mode.addItem("Text first, images queued", "text")
        self.ocr_mode.addItem("Images only - the second pass", "images")
        self.ocr_mode.setToolTip(
            "Reading text out of an image takes about 3.6 seconds a page - roughly\n"
            "eight times what everything else costs - so on a large corpus it is what\n"
            "decides how long a run takes.\n\n"
            "Text first indexes everything readable without OCR and queues the images,\n"
            "so search works in a day or two rather than a fortnight. The queued files\n"
            "appear on the Indexing page as held, not failed, and the Images pass fills\n"
            "them in afterwards."
        )
        self.ocr_mode.currentIndexChanged.connect(lambda _i: self.changed.emit())

        self.archive_recheck_days = QSpinBox()
        self.archive_recheck_days.setObjectName("ARCHIVE_RECHECK_DAYS")
        self.archive_recheck_days.setRange(0, 365)
        self.archive_recheck_days.setSuffix(" days")
        self.archive_recheck_days.setSpecialValueText("Only when it changes")
        self.archive_recheck_days.setKeyboardTracking(False)
        self.archive_recheck_days.setToolTip(
            "A folder marked as an archive in Folders to index is walked once and\n"
            "then left alone - which is what makes an incremental run over a settled\n"
            "terabyte take seconds instead of hours.\n\n"
            "It is still re-walked when the folder itself changes, when you press\n"
            "Rescan, and after this many days. The interval is the backstop for a\n"
            "change the folder's own timestamp cannot show - a file edited in place,\n"
            "deep inside."
        )
        self.archive_recheck_days.valueChanged.connect(lambda _v: self.changed.emit())

        self.name_only = QCheckBox("Index every file by name")
        self.name_only.setObjectName("INDEX_NAME_ONLY")
        self.name_only.setToolTip(
            "Record a row for every file, including the ones nothing can read -\n"
            ".zip, .mp4, .exe, .iso. They become findable by name; their contents\n"
            "are not searchable, and nothing is opened.\n\n"
            "Before this they produced no row at all: not a name, not a skip, and\n"
            "nothing anywhere saying they had been passed over.\n\n"
            "Costs one row per file - tens of bytes, no text and no vectors."
        )
        self.name_only.stateChanged.connect(lambda _s: self.changed.emit())

        # **A switch beside a number, because neither default is safe alone.**
        # Off by default leaves the contents of every archive unsearchable on a
        # corpus that is largely archives; on by default with no ceiling lets
        # one 20GB zip decide how long a run takes.
        self.archive_read_inside = QCheckBox("Read inside .zip archives")
        self.archive_read_inside.setObjectName("ARCHIVE_READ_INSIDE")
        self.archive_read_inside.setToolTip(
            "Index the documents inside .zip files as well as the archive's own\n"
            "name, so a report in a backup is findable by its contents.\n\n"
            "Members are extracted one at a time and deleted immediately - the\n"
            "whole archive is never unpacked. Encrypted members are never opened\n"
            "and never prompt."
        )
        self.archive_read_inside.stateChanged.connect(lambda _s: self.changed.emit())

        self.archive_max_mb = QSpinBox()
        self.archive_max_mb.setObjectName("ARCHIVE_MAX_MB")
        self.archive_max_mb.setRange(1, 10_000)
        self.archive_max_mb.setSingleStep(10)
        self.archive_max_mb.setSuffix(" MB")
        self.archive_max_mb.setKeyboardTracking(False)
        self.archive_max_mb.setToolTip(
            "The most one archive may expand to. Shared with anything nested\n"
            "inside it, so three levels cost this once rather than three times.\n\n"
            "An archive that reaches it is still indexed by name, and the members\n"
            "read before the budget ran out are kept."
        )
        self.archive_max_mb.valueChanged.connect(lambda _v: self.changed.emit())

        # Order 0z lane D. One switch, on by default: the filter only ever
        # leaves out pictures that are decoration, never a screenshot or scan.
        self.junk_images = QCheckBox("Leave out signature logos and icons in email")
        self.junk_images.setObjectName("INDEX_JUNK_IMAGE_FILTER")
        self.junk_images.setToolTip(
            "Pictures attached to email that are only decoration are not read:\n"
            "a signature logo repeated in every message, social-media icons,\n"
            "tracking pixels and divider lines. Reading text out of each one\n"
            "takes time and finds nothing worth searching for.\n\n"
            "They are still findable by name. Screenshots, scans, receipts and\n"
            "photos are read as before. Switch it off to read every picture."
        )
        self.junk_images.stateChanged.connect(lambda _s: self.changed.emit())
        # **1 October 2026: dormant, so it is shown switched off from use.**
        # Pictures attached to mail are now kept by name and never read
        # (`app/extract/mail_attachments.py`), so this switch changes nothing;
        # a control that does nothing is a quiet lie. Its label and tooltip are
        # released text and stay as they are - the note below says why.
        self.junk_images.setEnabled(False)
        self.junk_images_note = QLabel(
            "Pictures attached to email are now kept by name only and never read, "
            "so this switch has no effect.")
        self.junk_images_note.setObjectName("junkImagesDormantNote")
        self.junk_images_note.setWordWrap(True)

        # **The lever the owner asked for** (1 October 2026): what is read out
        # of a file attached to an email - `app/extract/mail_attachments.py`.
        # The logo switch above is live only while pictures are being read.
        self.mail_attachments = QComboBox()
        self.mail_attachments.setObjectName("MAIL_ATTACHMENTS")
        self.mail_attachments.addItem("Names only - nothing is opened", "names")
        self.mail_attachments.addItem("Documents - Office, PDF and text", "documents")
        self.mail_attachments.addItem("Documents and pictures - slow", "pictures")
        self.mail_attachments.addItem("Everything Leasha can read - slowest", "everything")
        # The default before anything is loaded - not the first item, "Names only".
        self.mail_attachments.setCurrentIndex(self.mail_attachments.findData("documents"))
        self.mail_attachments.setToolTip(
            "Which files attached to an email are opened and read.\n\n"
            "Every attachment is listed on the Files tab and found by its name\n"
            "whatever this says; it decides only whether its words are read too.\n"
            "Applies to mail read after the change - re-index a mailbox to\n"
            "apply it to mail already indexed."
        )
        self.mail_attachments.currentIndexChanged.connect(lambda _i: self._sync_junk())
        self.mail_attachments.currentIndexChanged.connect(lambda _i: self.changed.emit())

        # **A budget, not a switch.** At ~3.6s a page, twenty pages is about a
        # minute a document and covers the title, contents and introduction.
        # All-or-nothing is the sixty-hour column.
        self.pdf_ocr_pages = QSpinBox()
        self.pdf_ocr_pages.setObjectName("PDF_OCR_PAGES")
        self.pdf_ocr_pages.setRange(0, 500)
        self.pdf_ocr_pages.setSuffix(" pages")
        self.pdf_ocr_pages.setSpecialValueText("Do not read scanned PDFs")
        self.pdf_ocr_pages.setKeyboardTracking(False)
        self.pdf_ocr_pages.setToolTip(
            "A scanned PDF has no text layer, so the only way to read one is to\n"
            "render it and run OCR over the pages - about 3.6 seconds each.\n\n"
            "This is how many pages of each are worth that. The first few carry\n"
            "the title, contents and introduction, which is what makes a document\n"
            "findable at all."
        )
        self.pdf_ocr_pages.valueChanged.connect(lambda _v: self.changed.emit())

        # **Rung 1's own threshold, promoted from a hardcoded literal.**
        # `app/extract/ocr_ladder.py` decides an image is a document worth
        # reading in full once this much of its thumbnail is plain white -
        # this is what lets that number be found and changed rather than
        # edited in a file (non-negotiable 11).
        self.ocr_white_page_percent = QSpinBox()
        self.ocr_white_page_percent.setObjectName("OCR_WHITE_PAGE_PERCENT")
        self.ocr_white_page_percent.setRange(1, 100)
        self.ocr_white_page_percent.setSuffix(" %")
        self.ocr_white_page_percent.setKeyboardTracking(False)
        self.ocr_white_page_percent.setToolTip(
            "Above this percentage of plain white, Leasha treats the image as\n"
            "a document and reads it in full, skipping the quicker check it\n"
            "would otherwise run first.\n\n"
            "Lower it to catch scanned pages with shading or colour; raise it\n"
            "if ordinary photos of pale backgrounds - snow, whiteboards, plain\n"
            "walls - are being read as documents unnecessarily."
        )
        self.ocr_white_page_percent.valueChanged.connect(
            lambda _v: self.changed.emit())

        # Work order 0z lane B: how long one file may hold a reader
        # (`app/index/file_watch.py`). Coverage because a file that runs past
        # it is not read - it is recorded as skipped, like a damaged one.
        self.file_time_limit = QSpinBox()
        self.file_time_limit.setObjectName("INDEX_FILE_TIME_LIMIT_S")
        self.file_time_limit.setRange(0, 3600)
        self.file_time_limit.setSingleStep(30)
        self.file_time_limit.setSuffix(" s")
        self.file_time_limit.setSpecialValueText("No limit")
        self.file_time_limit.setKeyboardTracking(False)
        self.file_time_limit.setToolTip(
            "How long one text or code file may take to read before it is\n"
            "skipped, so a damaged file cannot hold a reader for the rest of the\n"
            "run. PDFs, Office files and other documents get ten times this.\n\n"
            "Mailboxes and zip archives have no time limit - see the next\n"
            "setting. Videos and recordings have none either. Any file can\n"
            "also be skipped by hand with Force skip while it is being read."
        )
        self.file_time_limit.valueChanged.connect(lambda _v: self.changed.emit())

        self.stall_limit = QSpinBox()
        self.stall_limit.setObjectName("INDEX_STALL_LIMIT_S")
        self.stall_limit.setRange(0, 7200)
        self.stall_limit.setSingleStep(60)
        self.stall_limit.setSuffix(" s")
        self.stall_limit.setSpecialValueText("Never")
        self.stall_limit.setKeyboardTracking(False)
        self.stall_limit.setToolTip(
            "A large mailbox (.pst, .mbox, .olm) or zip can rightly take hours,\n"
            "so it is never cut off for taking long. It is skipped only when\n"
            "nothing new has been read from it for this long. The messages\n"
            "already read are kept and stay searchable."
        )
        self.stall_limit.valueChanged.connect(lambda _v: self.changed.emit())

        # **Place by place** (owner, 1 October 2026): one block per place, its
        # levers, and a sentence saying what they do there - recomputed as a
        # lever moves (`presenter/coverage.py`). The controls and the labels
        # beside them are the ones this box always had; only the grouping is new.
        rows = {
            FILES: [(None, self.name_only), (None, _cost("INDEX_NAME_ONLY")),
                    ("Re-check archives every", self.archive_recheck_days),
                    ("Time limit per file", self.file_time_limit)],
            EMAIL: [("Skip a mailbox or archive after no progress for", self.stall_limit)],
            ATTACHMENTS: [("What to read from email attachments", self.mail_attachments),
                          (None, self.junk_images_note), (None, self.junk_images)],
            ZIPS: [(None, self.archive_read_inside), (None, _cost("ARCHIVE_READ_INSIDE")),
                   ("Largest archive to read", self.archive_max_mb)],
            PICTURES: [("Images and scans", self.ocr_mode), (None, _cost("INDEX_OCR_MODE")),
                       ("Pages of a scanned PDF", self.pdf_ocr_pages),
                       ("How white a photo must be to read as a page",
                        self.ocr_white_page_percent)],
            MEDIA: [(None, _elsewhere("Switched on and off in Settings, Models & AI."))],
            CODE: [],
        }
        self._levers: dict = {}
        self.sentences: dict[str, QLabel] = {}
        layout = QVBoxLayout(self)
        for place in PLACES:
            block = QGroupBox(place)
            block.setObjectName("place")
            form = QFormLayout(block)
            sentence = QLabel()
            sentence.setObjectName("placeSentence")
            sentence.setWordWrap(True)
            self.sentences[place] = sentence
            form.addRow(sentence)
            for label, widget in rows[place]:
                if label is None:
                    form.addRow(widget)
                else:
                    form.addRow(label, widget)
            layout.addWidget(block)
        self.changed.connect(self._refresh)
        self._sync_junk()
        self._refresh()

    def _sync_junk(self) -> None:
        """The logo switch is live only while pictures in mail are read."""
        reads_pictures = self.mail_attachments.currentData() in ("pictures", "everything")
        self.junk_images.setEnabled(reads_pictures)
        self.junk_images_note.setVisible(not reads_pictures)

    def _refresh(self) -> None:
        """Every place's sentence, from the levers as they stand right now."""
        levers = {**self._levers, **self.values()}
        for place, label in self.sentences.items():
            label.setText(place_sentence(place, levers))

    def load(self, settings: Any) -> None:
        """Fill from Settings without emitting - see `IndexingSettings.load_indexing`."""
        # The levers that live on other pages, for the sentences that use them.
        self._levers = {name: getattr(settings, name) for name in (
            "index_ocr_pass", "index_watch_folders", "video_indexing_enabled",
            "audio_transcription_enabled", "caption_trickle_enabled",
            "people_recognition_enabled") if hasattr(settings, name)}
        widgets = (self.ocr_mode, self.archive_recheck_days, self.name_only,
                   self.archive_read_inside, self.archive_max_mb,
                   self.pdf_ocr_pages, self.ocr_white_page_percent,
                   self.file_time_limit, self.stall_limit, self.junk_images,
                   self.mail_attachments)
        for widget in widgets:
            widget.blockSignals(True)
        try:
            mode = str(getattr(settings, "index_ocr_mode", "both"))
            found = self.ocr_mode.findData(mode)
            self.ocr_mode.setCurrentIndex(found if found >= 0 else 0)
            self.archive_recheck_days.setValue(
                int(getattr(settings, "archive_recheck_days", 30)))
            self.name_only.setChecked(
                bool(getattr(settings, "index_name_only", True)))
            self.archive_read_inside.setChecked(
                bool(getattr(settings, "archive_read_inside", True)))
            self.archive_max_mb.setValue(
                int(getattr(settings, "archive_max_mb", 100)))
            self.pdf_ocr_pages.setValue(
                int(getattr(settings, "pdf_ocr_pages", 0)))
            self.ocr_white_page_percent.setValue(
                int(getattr(settings, "ocr_white_page_percent", 70)))
            self.file_time_limit.setValue(
                int(getattr(settings, "index_file_time_limit_s", 120)))
            self.stall_limit.setValue(
                int(getattr(settings, "index_stall_limit_s", 600)))
            self.junk_images.setChecked(
                bool(getattr(settings, "index_junk_image_filter", True)))
            found = self.mail_attachments.findData(
                str(getattr(settings, "mail_attachments", "documents")))
            self.mail_attachments.setCurrentIndex(found if found >= 0 else 1)
        finally:
            for widget in widgets:
                widget.blockSignals(False)
        self._sync_junk()
        self._refresh()

    def values(self) -> dict:
        """Keyed by `Settings` field name, like `IndexingSettings.current_limits`."""
        return {
            "index_ocr_mode": str(self.ocr_mode.currentData() or "both"),
            "archive_recheck_days": int(self.archive_recheck_days.value()),
            "index_name_only": bool(self.name_only.isChecked()),
            "archive_read_inside": bool(self.archive_read_inside.isChecked()),
            "archive_max_mb": int(self.archive_max_mb.value()),
            "pdf_ocr_pages": int(self.pdf_ocr_pages.value()),
            "ocr_white_page_percent": int(self.ocr_white_page_percent.value()),
            "index_file_time_limit_s": int(self.file_time_limit.value()),
            "index_stall_limit_s": int(self.stall_limit.value()),
            "index_junk_image_filter": bool(self.junk_images.isChecked()),
            "mail_attachments": str(self.mail_attachments.currentData() or "documents"),
        }
