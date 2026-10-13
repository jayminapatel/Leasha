"""The contact editor: every field of a card, edited in one panel.

Layer: L5

Order 1k 6d, 6k, 6m, D4, D6. It holds the photo, the name parts, company, department and title,
a list for every repeatable kind of `card.KINDS` (any number each, a label combo, drag to
reorder, a main email and phone), dates, related people, notes, the "Kept in" account chips,
groups, tags and the favourite star. Every field says where it came from on hover. Nothing is
written here: Save emits a `SaveRequest`, and the page runs it on a worker. The two things that
act at once are D4's: adding an account to "Kept in" is immediate, and removing one asks first,
then emits with `confirmed=True`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtCore import QRect, QRectF, Qt, Signal
from PySide6.QtGui import QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (QFileDialog, QFrame, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                             QMessageBox, QPlainTextEdit, QToolButton, QVBoxLayout, QWidget)

from app.contacts.card import KINDS, SCALARS, Card, Field, Photo, display_name, field_key
from app.ui.contacts.enrich_panel import EnrichPanel
from app.ui.contacts.fields import FieldList
from app.ui.contacts.index_panel import IndexPanel
from app.ui.contacts.loader import Detail, read_photo, run_task
from app.ui.contacts.logic import KIND_INFO, initials, merge_order, source_words
from app.ui.contacts.parts import TokenEdit, retint_tools, tool_button
from app.ui.contacts.review import ReviewBox
from app.ui.theme import theme_colours
from app.ui.widgets.buttons import action_button, button_row
from app.ui.widgets.flow_layout import FlowLayout
from app.ui.widgets.icons import icon
from app.ui.widgets.scroll import scrollable

__all__ = ["ContactEditor", "SaveRequest", "PHOTO_SIDE"]

PHOTO_SIDE = 84

_NAME_PARTS = (("prefix", "Title (Dr, Ms)"), ("given", "First name"), ("additional", "Middle name"),
               ("family", "Last name"), ("suffix", "Suffix"), ("nickname", "Nickname"))
_WORK = (("company", "Company", "building-2"), ("department", "Department", "building"),
         ("title", "Job title", "briefcase"))


@dataclass
class SaveRequest:
    """What Save asks the page to write."""

    cid: Optional[int]
    card: Card
    tags: list[str]
    original_tags: list[str] = field(default_factory=list)
    kept_in: list[int] = field(default_factory=list)


class ContactEditor(QWidget):
    save_requested = Signal(object)
    cancel_requested = Signal()
    acknowledge_requested = Signal(int)
    #: (contact id, the accounts it should be kept in, removals confirmed). Immediate (D4).
    kept_in_requested = Signal(int, list, bool)
    person_photo_requested = Signal(int)
    #: D7.1 and D6/10k, passed on from the review box: the page does the work on a worker.
    delete_here_requested = Signal(int)
    put_back_requested = Signal(int, int)
    resolve_requested = Signal(int, int, str)
    dirty_changed = Signal(bool)
    error = Signal(object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.cid: Optional[int] = None
        self.region = "GB"
        self._accounts: dict[int, Any] = {}
        self._kept: set[int] = set()
        self._orig_kept: set[int] = set()
        self._photo: Optional[Photo] = None
        self._orig: Optional[Card] = None
        self._orig_tags: list[str] = []
        self._tips: dict[str, list] = {}
        self._loading = False
        self._dirty = False
        self.host_can_pick_photo = False

        # -- the banner (D6)
        self.banner = QFrame()
        self.banner.setObjectName("contactBanner")
        self.banner_icon = QLabel()
        self._banner_default = "Changed in two places. The newest edit won; the other is kept in History."
        self.banner_text = QLabel(self._banner_default)
        self.banner_text.setWordWrap(True)
        self.acknowledge = action_button("Acknowledge", "check", "secondary")
        self.acknowledge.setToolTip("I have looked: take the 'changed in two places' marker off this contact")
        self.acknowledge.clicked.connect(self._acknowledge)
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(8, 6, 8, 6)
        bl.addWidget(self.banner_icon)
        bl.addWidget(self.banner_text, 1)
        bl.addWidget(self.acknowledge)
        self.banner.hide()
        self.banner_detail = QLabel("")
        self.banner_detail.setWordWrap(True)
        self.banner_detail.setObjectName("resultsSummary")
        self.banner_detail.setAccessibleName("Which value won")
        self.banner_detail.hide()
        self.review = ReviewBox()
        self.review.delete_here_requested.connect(self.delete_here_requested)
        self.review.put_back_requested.connect(self.put_back_requested)
        self.review.resolve_requested.connect(self.resolve_requested)
        self._names: dict[int, str] = {}

        # -- the head: photo, name, favourite
        self.photo_label = QLabel()
        self.photo_label.setFixedSize(PHOTO_SIDE, PHOTO_SIDE)
        self.photo_label.setAccessibleName("Photo")
        self.heading = QLabel("")
        self.heading.setObjectName("contactName")
        self.heading.setWordWrap(True)
        self.favourite = tool_button("star", "Favourite", "Mark this contact as a favourite", checkable=True)
        self.favourite.toggled.connect(self._touch)
        self.choose_photo = tool_button("image-plus", "Choose photo", "Choose a picture file for this contact")
        self.choose_photo.clicked.connect(self._choose_photo)
        self.person_photo = tool_button("image", "From my photos",
                                        "Pick one of your own photos of this person")
        self.person_photo.clicked.connect(self._from_my_photos)
        self.remove_photo = tool_button("trash-2", "Remove photo", "Take this contact's picture away")
        self.remove_photo.clicked.connect(lambda: self.set_photo(None))
        photo_buttons = QHBoxLayout()
        photo_buttons.setContentsMargins(0, 0, 0, 0)
        for b in (self.choose_photo, self.person_photo, self.remove_photo):
            photo_buttons.addWidget(b)
        photo_buttons.addStretch(1)
        side = QVBoxLayout()
        side.addWidget(self.heading)
        side.addLayout(photo_buttons)
        side.addStretch(1)
        head = QHBoxLayout()
        head.addWidget(self.photo_label)
        head.addLayout(side, 1)
        head.addWidget(self.favourite, 0, Qt.AlignmentFlag.AlignTop)

        # -- name and work
        self.edits: dict[str, QLineEdit] = {}
        # Two columns, each box with its caption above it: six name parts and three work lines took
        # fifteen rows of a narrow pane; this takes six, and a filled box still says what it is.
        form = QGridLayout()
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(4)
        for i, (key, words) in enumerate(_NAME_PARTS):
            form.addWidget(self._cell(key, words), i // 2, i % 2)
        work = QGridLayout()
        work.setContentsMargins(0, 0, 0, 0)
        work.setHorizontalSpacing(8)
        work.setVerticalSpacing(4)
        for i, (key, words, _glyph) in enumerate(_WORK):
            work.addWidget(self._cell(key, words), i // 2, i % 2, 1, 2 if i == len(_WORK) - 1 and i % 2 == 0 else 1)
        for grid in (form, work):
            grid.setColumnStretch(0, 1)
            grid.setColumnStretch(1, 1)

        # -- repeatable kinds
        self.lists: dict[str, FieldList] = {}
        lists_box = QVBoxLayout()
        lists_box.setSpacing(10)
        for kind in KINDS:
            fl = FieldList(kind)
            fl.changed.connect(self._touch)
            self.lists[kind] = fl
            lists_box.addWidget(fl)

        # -- notes, kept in, groups, tags
        self.notes = QPlainTextEdit()
        self.notes.setPlaceholderText("Notes")
        self.notes.setAccessibleName("Notes")
        self.notes.setToolTip("Anything you want to remember about this person")
        self.notes.setFixedHeight(84)
        self.notes.textChanged.connect(self._touch)
        self.kept_row = FlowLayout(spacing=6)
        self.kept_box = QWidget()
        self.kept_box.setLayout(self.kept_row)
        self.kept_buttons: dict[int, QToolButton] = {}
        self.groups = TokenEdit("group", "users")
        self.groups.changed.connect(self._touch)
        self.tags = TokenEdit("tag", "tag")
        self.tags.changed.connect(self._touch)

        body = QWidget()
        col = QVBoxLayout(body)
        col.setContentsMargins(10, 8, 10, 12)
        col.setSpacing(10)
        col.addWidget(self.review)
        col.addWidget(self.banner)
        col.addWidget(self.banner_detail)
        col.addLayout(head)
        col.addLayout(form)
        col.addLayout(work)
        col.addLayout(lists_box)
        col.addWidget(self._section("sticky-note", "Notes"))
        col.addWidget(self.notes)
        col.addWidget(self._section("cloud", "Kept in"))
        hint = QLabel("Where this contact is stored. Adding an account puts it there at the next "
                      "sync (or Sync now); taking one away asks first.")
        hint.setWordWrap(True)
        hint.setObjectName("resultsSummary")
        col.addWidget(hint)
        col.addWidget(self.kept_box)
        col.addWidget(self._section("users", "Groups"))
        col.addWidget(self.groups)
        col.addWidget(self._section("tag", "Tags"))
        col.addWidget(self.tags)
        # Order 1s 7c, 7e: enrich this contact, and the suggestions turned down for it.
        self.enrich_panel = EnrichPanel()
        self.enrich_panel.error.connect(self.error)
        col.addWidget(self.enrich_panel)
        # 6f: the mail, files and photos the index holds about this person.
        self.index_panel = IndexPanel()
        self.index_panel.error.connect(self.error)
        col.addWidget(self.index_panel)
        col.addStretch(1)
        self.scroll = scrollable(body, min_width=300)

        self.save_button = action_button("Save", "save", "primary")
        self.save_button.setToolTip("Save this contact (Ctrl+S)")
        self.save_button.clicked.connect(self._save)
        self.cancel_button = action_button("Cancel", "x", "secondary")
        self.cancel_button.setToolTip("Put back what was there and stop editing")
        self.cancel_button.clicked.connect(self._cancel)
        self.save_button.setEnabled(False)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.scroll, 1)
        buttons = button_row(self.save_button, self.cancel_button)
        buttons.setContentsMargins(10, 4, 10, 8)
        outer.addLayout(buttons)
        self.setAccessibleName("Contact editor")
        self.retint(theme_colours())
        self.clear()

    # ------------------------------------------------------------------ build
    def _line(self, key: str, words: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(words)
        edit.setAccessibleName(words)
        edit.setToolTip(words)
        edit.textChanged.connect(self._touch)
        self.edits[key] = edit
        return edit

    def _cell(self, key: str, words: str) -> QWidget:
        """One box with its caption above it."""
        box = QWidget()
        col = QVBoxLayout(box)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(1)
        caption = QLabel(words)
        caption.setObjectName("resultsSummary")
        col.addWidget(caption)
        col.addWidget(self._line(key, words))
        return box

    @staticmethod
    def _icon_label(glyph: str, words: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        pic = QLabel()
        pic.setPixmap(icon(glyph, theme_colours().get("text_dim", "#888888")).pixmap(16, 16))
        row.addWidget(pic)
        row.addWidget(QLabel(words))
        return box

    @staticmethod
    def _section(glyph: str, words: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        pic = QLabel()
        pic.setPixmap(icon(glyph, theme_colours().get("text_dim", "#888888")).pixmap(16, 16))
        title = QLabel(words)
        title.setObjectName("contactSectionTitle")
        row.addWidget(pic)
        row.addWidget(title)
        row.addStretch(1)
        return box

    def retint(self, colours: dict[str, str]) -> None:
        retint_tools(self, colours)
        self.banner_icon.setPixmap(icon("triangle-alert", colours.get("warning", "#b06000")).pixmap(18, 18))
        self.banner.setStyleSheet(
            f"#contactBanner {{ border: 1px solid {colours.get('warning', '#b06000')}; border-radius: 6px; }}")
        for fl in self.lists.values():
            fl.retint()
        self.groups.retint()
        self.tags.retint()
        self._draw_photo()
        self._paint_kept()

    # --------------------------------------------------------------- loading
    def configure(self, accounts: list, groups: list[str], tags: list[tuple], region: str,
                  can_pick_photo: bool) -> None:
        """What the editor offers: the accounts to keep a contact in, known groups and tags."""
        self.region = region
        self._accounts = {a.id: a for a in accounts if getattr(a, "syncs_contacts", True)}
        self.groups.set_known(groups)
        self.tags.set_known([t[1] for t in tags], {t[1]: t[2] for t in tags})
        self.host_can_pick_photo = can_pick_photo
        self.person_photo.setEnabled(can_pick_photo)
        self.person_photo.setToolTip("Pick one of your own photos of this person" if can_pick_photo else
                                     "Not available: this needs Leasha's photo index, which is not connected here")
        self._build_kept_buttons()

    def set_account_names(self, names: dict[int, str]) -> None:
        """Every account's name (also those that no longer sync contacts), for the review bars."""
        self._names = dict(names)

    def clear(self) -> None:
        self.cid = None
        self.review.clear()
        self.banner_detail.hide()
        self._load(Card(), [], [], {}, {}, "")
        self.setEnabled(False)
        self.heading.setText("")
        self.index_panel.clear()
        self.enrich_panel.clear()

    def start_new(self, default_account: Optional[int]) -> None:
        """Ctrl+N: an empty card, kept in the default account (D5)."""
        self.setEnabled(True)
        self.cid = None
        kept = [default_account] if default_account in self._accounts else []
        self._load(Card(), kept, [], {}, {}, "")
        self.index_panel.clear()
        self.enrich_panel.clear()
        for kind in ("email", "phone"):                 # blank rows are not saved; they invite typing
            self.lists[kind].add_row(None, emit=False)
        self.heading.setText("New contact")
        self._draw_photo()
        self._set_dirty(False)
        self.edits["given"].setFocus()

    def show_detail(self, d: Detail) -> None:
        self.setEnabled(True)
        self.cid = d.id
        flagged = bool((d.meta or {}).get("conflict_at")) or bool(d.conflicts)
        self._load(d.card, d.kept_in, d.tags, d.sources, d.meta, display_name(d.card))
        self.banner.setVisible(flagged)
        names = {**{i: (a.name or a.address) for i, a in self._accounts.items()}, **self._names}
        self.review.show_for(d.id, list(d.links), list(d.conflicts), names)
        # Under "Ask me every time" the choice is the review box's; the newest-edit banner keeps its
        # words and its Acknowledge, and says which value won and where the other one is kept.
        held = self.review.has_held
        self.acknowledge.setVisible(not held)
        if held:
            self.banner_text.setText("Changed in two places. You chose to be asked: pick which value to keep "
                                     "in the box above.")
        else:
            self.banner_text.setText(self._banner_default)
        self.banner_detail.setText("\n".join(self.review.decided))
        self.banner_detail.setVisible(flagged and bool(self.review.decided))
        self.index_panel.show_for(d.id, [f.value for f in d.card.values("email")])
        self.enrich_panel.show_for(d.id)

    def _load(self, card: Card, kept: list[int], tags: list[str], sources: dict, meta: dict, name: str) -> None:
        self._loading = True
        try:
            self._orig = card.copy()
            self._orig.photo = card.photo
            self._orig_tags = list(tags)
            self._tips = sources
            names = {i: (a.name or a.address) for i, a in self._accounts.items()}
            for key in SCALARS:
                if key == "notes":
                    self.notes.setPlainText(card.get("notes"))
                    self.notes.setToolTip(source_words(sources.get("s:notes", []), names) or
                                          "Anything you want to remember about this person")
                else:
                    edit = self.edits[key]
                    edit.setText(card.get(key))
                    base = edit.accessibleName()
                    tip = source_words(sources.get(f"s:{key}", []), names)
                    edit.setToolTip(f"{base}\n{tip}" if tip else base)
            for kind, fl in self.lists.items():
                fl.set_fields(card.values(kind), lambda f: source_words(
                    sources.get("f:" + field_key(f, self.region), []), names))
            self.groups.set_items(card.groups)
            self.tags.set_items(tags)
            self.favourite.setChecked(card.favourite)
            self._photo = card.photo
            self._kept = set(kept)
            self._orig_kept = set(kept)
            self.banner.hide()
            self.banner_detail.hide()
            self.review.clear()
            self.heading.setText(name)
            self._draw_photo()
            self._paint_kept()
        finally:
            self._loading = False
        self._set_dirty(False)

    # ------------------------------------------------------------- collecting
    def collect(self) -> Card:
        """The card as the controls now hold it."""
        card = Card()
        for key in SCALARS:
            text = (self.notes.toPlainText() if key == "notes" else self.edits[key].text()).strip()
            if text:
                card.scalars[key] = text
        per_kind = {k: fl.fields() for k, fl in self.lists.items()}
        card.fields = merge_order(self._orig.fields if self._orig else [], per_kind)
        card.groups = self.groups.items()
        card.favourite = self.favourite.isChecked()
        card.photo = self._photo
        return card

    def kept_in(self) -> list[int]:
        return sorted(self._kept)

    def request(self) -> SaveRequest:
        return SaveRequest(self.cid, self.collect(), self.tags.items(), list(self._orig_tags),
                           self.kept_in() if self.cid is None else [])

    def is_dirty(self) -> bool:
        return self._dirty

    # ------------------------------------------------------------------ state
    def _touch(self, *_a: object) -> None:
        if self._loading:
            return
        name = display_name(self.collect())
        self.heading.setText(name or ("New contact" if self.cid is None else ""))
        self._draw_photo()
        self._set_dirty(self._differs())

    def _differs(self) -> bool:
        if self.cid is None:
            return True
        now = self.collect()
        was = self._orig
        if was is None:
            return True
        same_photo = (now.photo.sha if now.photo else "") == (was.photo.sha if was.photo else "")
        return not (now.to_json() == was.to_json() and same_photo and
                    sorted(self.tags.items()) == sorted(self._orig_tags))

    def _set_dirty(self, on: bool) -> None:
        if on != self._dirty:
            self._dirty = on
            self.dirty_changed.emit(on)
        self.save_button.setEnabled(on and self.isEnabled())

    def mark_saved(self) -> None:
        self._set_dirty(False)

    # ------------------------------------------------------------------ photo
    def set_photo(self, photo: Optional[Photo]) -> None:
        self._photo = photo
        self._draw_photo()
        if not self._loading:
            self._set_dirty(self._differs())

    def _draw_photo(self) -> None:
        c = theme_colours()
        side = PHOTO_SIDE
        canvas = QPixmap(side, side)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, side, side))
        loaded = QPixmap()
        if self._photo is not None and loaded.loadFromData(self._photo.data):
            painter.setClipPath(path)
            scaled = loaded.scaled(side, side, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                   Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap((side - scaled.width()) // 2, (side - scaled.height()) // 2, scaled)
        else:
            from PySide6.QtGui import QColor, QFont

            painter.fillPath(path, QColor(c.get("accent_soft", "#dddddd")))
            font = QFont(painter.font())
            font.setBold(True)
            font.setPixelSize(int(side * 0.38))
            painter.setFont(font)
            painter.setPen(QColor(c.get("accent_text", "#444444")))
            name = self.heading.text() if self.heading.text() != "New contact" else ""
            painter.drawText(QRect(0, 0, side, side), Qt.AlignmentFlag.AlignCenter, initials(name) if name else "?")
        painter.end()
        self.photo_label.setPixmap(canvas)
        self.remove_photo.setEnabled(self._photo is not None)

    def _choose_photo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose a picture", "",
                                              "Pictures (*.jpg *.jpeg *.png *.gif *.bmp *.webp)")
        if path:
            run_task(self, lambda: read_photo(path), lambda r: self.set_photo(Photo(r[0], r[1])))

    def _from_my_photos(self) -> None:
        # The stub of 6d.1 / 2c: the host's own picker, if it has one.
        if self.host_can_pick_photo:
            self.person_photo_requested.emit(self.cid if self.cid is not None else -1)

    # ---------------------------------------------------------------- kept in
    def _build_kept_buttons(self) -> None:
        while self.kept_row.count():
            item = self.kept_row.takeAt(0)
            if item is not None and item.widget() is not None:
                item.widget().deleteLater()
        self.kept_buttons = {}
        from app.ui.contacts.logic import account_glyph

        for aid, a in self._accounts.items():
            b = QToolButton()
            b.setText(a.name or a.address)
            b.setCheckable(True)
            b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            b.setToolTip(f"Keep this contact in {a.name or a.address} ({a.address})")
            b.setAccessibleName(f"Kept in {a.name or a.address}")
            b.setProperty("account", aid)
            b.setProperty("glyph_name", account_glyph(a))
            b.clicked.connect(lambda checked, i=aid: self._kept_clicked(i, checked))
            self.kept_buttons[aid] = b
            self.kept_row.addWidget(b)
        self._paint_kept()

    def _paint_kept(self) -> None:
        c = theme_colours()
        for aid, b in self.kept_buttons.items():
            a = self._accounts[aid]
            on = aid in self._kept
            b.blockSignals(True)
            b.setChecked(on)
            b.blockSignals(False)
            b.setIcon(icon(str(b.property("glyph_name")), a.colour))
            border = a.colour if on else c.get("border", "#888888")
            fill = c.get("accent_soft", "#eeeeee") if on else "transparent"
            b.setStyleSheet(f"QToolButton {{ border: 2px solid {border}; border-radius: 11px; "
                            f"padding: 2px 8px; background: {fill}; }}")

    def confirm_removal(self, account_name: str) -> bool:
        """D4: taking a contact out of an account deletes it from that account. Asks first."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Remove from this account?")
        box.setText(f"Remove this contact from {account_name}?")
        box.setInformativeText(f"It will be deleted from {account_name} the next time that account syncs. "
                               "It stays in Leasha and in the other accounts it is kept in.")
        yes = box.addButton("Remove it", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton("Keep it", QMessageBox.ButtonRole.RejectRole)
        box.exec()
        return box.clickedButton() is yes

    def _kept_clicked(self, aid: int, checked: bool) -> None:
        name = self._accounts[aid].name or self._accounts[aid].address
        if checked:
            self._kept.add(aid)
            self._paint_kept()
            if self.cid is not None:
                self.kept_in_requested.emit(self.cid, sorted(self._kept), False)
            else:
                self._touch()
            return
        if self.cid is not None and aid in self._orig_kept and not self.confirm_removal(name):
            self._paint_kept()
            return
        self._kept.discard(aid)
        self._paint_kept()
        if self.cid is not None:
            self.kept_in_requested.emit(self.cid, sorted(self._kept), aid in self._orig_kept)
        else:
            self._touch()

    def set_kept(self, kept: list[int]) -> None:
        """After the page wrote a change to where it is kept."""
        self._kept = set(kept)
        self._orig_kept = set(kept)
        self._paint_kept()

    # ----------------------------------------------------------------- actions
    def _save(self) -> None:
        if self.isEnabled():
            self.save_requested.emit(self.request())

    def save_if_dirty(self) -> None:
        if self._dirty and self.isEnabled():
            self._save()

    def _cancel(self) -> None:
        self.cancel_requested.emit()

    def _acknowledge(self) -> None:
        if self.cid is not None:
            self.acknowledge_requested.emit(self.cid)

    def focus_first(self) -> None:
        self.edits["given"].setFocus()
