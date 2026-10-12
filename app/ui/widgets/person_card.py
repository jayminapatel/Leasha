"""A contact's compact card, and the strip that shows it above Search's results.

Layer: L5 widget

Order 1k 6f: "Searching a person's name in Search shows their contact card above the results".
`PersonCard` is the card (photo or initials, name, title and company, the main email and phone
with their icons, the accounts the contact is kept in as coloured dots, and "Open contact").
`PersonStrip` is the row of at most two cards between the notice bar and the results.

**It never gets in the way of searching.** The strip listens to the box's own `textChanged`
(as the filter chips do), waits `LOOKUP_DEBOUNCE_MS` of stillness, and reads on a worker
(rule 5) - it does not touch the search, its timers, its results or their order. A stale answer
is dropped by generation; no match, an empty book, a missing table or a failed read all leave
the strip hidden and the results exactly as they were. The decision of *who* a query names is
`app.contacts.activity.strong_matches`, so a test reads it without a window.

**It never moves rows already painted** (UI phase B, owner, 2026-10-13). The card used to
land a moment after the first rows and push them all down. Two things stop that: the pause is
shorter than the keyword tier's (`LOOKUP_DEBOUNCE_MS`), so the card is normally in place before
the first row is drawn; and an answer that lands after the rows for the same words is *held*
(`rows_moved`) and shown with the next set of rows, which move anyway, never on its own.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from PySide6.QtCore import QRectF, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QVBoxLayout, QWidget

from app.core.logging import logger
from app.ui.later import when_done
from app.ui.theme import theme_colours
from app.ui.widgets.buttons import action_button
from app.ui.widgets.icons import icon

__all__ = ["PersonInfo", "PersonCard", "PersonStrip", "lookup_people", "attach",
           "LOOKUP_DEBOUNCE_MS", "PHOTO_SIDE"]

_log = logger.bind(component="ui.person_card")

#: Still time after the last keystroke before the contacts are read. UI phase B: *under* the
#: keyword tier's 150 ms (it was 250, just after it), so the card normally lands before the
#: first row is painted instead of pushing it down. Still a pause, so a name being typed at
#: speed does not read the book every letter; the read is on a worker either way.
LOOKUP_DEBOUNCE_MS = 120
PHOTO_SIDE = 44
#: Cards shown at most. Two: a search for "Ann" that names three Anns is a search, not a card.
MAX_CARDS = 2


@dataclass
class PersonInfo:
    """One contact as the card shows it. Built on a worker; holds no widget."""

    id: int
    name: str
    title: str = ""
    company: str = ""
    email: str = ""
    phone: str = ""
    accounts: list[tuple[str, str]] = field(default_factory=list)       # (name, colour)
    photo: Optional[QImage] = None                                       # already scaled


def _hub_over(store: Any) -> Any:
    from app.contacts.hub import ContactHub

    try:
        from app.accounts.store import AccountStore

        region = AccountStore(store).settings().phone_region or "GB"
        return ContactHub(store, region=region)
    except Exception:                                  # noqa: BLE001 - the default region will do
        return ContactHub(store)


def lookup_people(store: Any, query: str, limit: int = MAX_CARDS) -> list[PersonInfo]:
    """The contacts `query` names, ready to draw. **Runs on a worker**: it reads SQLite and
    decodes a picture. `[]` for no store, no match, an empty book or any failure."""
    if store is None or not str(query or "").strip():
        return []
    try:
        from app.contacts.activity import strong_matches

        hub = _hub_over(store)
        rows = strong_matches(hub, query, limit)
        if not rows:
            return []
        try:
            from app.accounts.store import AccountStore

            names = {int(a.id): (str(a.name or a.address), str(a.colour or "")) for a in AccountStore(store).list()}
        except Exception:                              # noqa: BLE001 - dots are an extra
            names = {}
        out = []
        for row in rows:
            info = PersonInfo(row.id, row.display_name, row.title, row.company, row.email, row.phone,
                              [names[a] for a in row.accounts if a in names])
            if row.has_photo:
                found = hub.conn.execute("SELECT data FROM contact_photos WHERE contact_id = ?",
                                         (row.id,)).fetchone()
                image = QImage.fromData(bytes(found[0])) if found else QImage()
                if not image.isNull():
                    info.photo = image.scaled(PHOTO_SIDE * 2, PHOTO_SIDE * 2,
                                              Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                                              Qt.TransformationMode.SmoothTransformation)
            out.append(info)
        return out
    except Exception as exc:                           # noqa: BLE001 - the strip stays hidden, the log says why
        _log.debug("no contact card for this search: {}", exc)
        return []


class PersonCard(QFrame):
    """One contact: who they are at a glance, and a way into their page."""

    open_requested = Signal(int)

    def __init__(self, info: PersonInfo, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.info = info
        self.setObjectName("personCard")
        self.setAccessibleName(f"Contact card: {info.name}")
        colours = theme_colours()
        self.setStyleSheet(
            f"#personCard {{ background: {colours.get('surface', '#ffffff')}; "
            f"border: 1px solid {colours.get('border', '#cccccc')}; border-radius: 8px; }}")
        self.photo = QLabel()
        self.photo.setFixedSize(PHOTO_SIDE, PHOTO_SIDE)
        self.photo.setAccessibleName("Photo")
        self.photo.setPixmap(self._picture(colours))
        self.name = QLabel(info.name)
        self.name.setObjectName("contactName")
        self.name.setAccessibleName("Name")
        work = " · ".join(p for p in (info.title, info.company) if p)
        self.work = QLabel(work)
        self.work.setAccessibleName("Title and company")
        self.work.setVisible(bool(work))
        text = QVBoxLayout()
        text.setSpacing(2)
        text.addWidget(self.name)
        text.addWidget(self.work)
        self.lines: list[QLabel] = []
        reach = QHBoxLayout()
        reach.setSpacing(14)
        for glyph, value, words in (("mail", info.email, "Email"), ("phone", info.phone, "Phone")):
            if value:
                reach.addLayout(self._line(glyph, value, words, colours))
        reach.addStretch(1)
        text.addLayout(reach)
        self.dots = QLabel()
        self.dots.setAccessibleName("Kept in")
        self._dots(colours)
        text.addWidget(self.dots)
        self.open_button = action_button("Open contact", "contact", "secondary")
        self.open_button.setToolTip(f"Open {info.name} on the Contacts page")
        self.open_button.setAccessibleName(f"Open contact {info.name}")
        self.open_button.clicked.connect(lambda: self.open_requested.emit(self.info.id))
        row = QHBoxLayout(self)
        row.setContentsMargins(10, 8, 10, 8)
        row.setSpacing(10)
        row.addWidget(self.photo, 0, Qt.AlignmentFlag.AlignTop)
        row.addLayout(text, 1)
        row.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignVCenter)

    def _line(self, glyph: str, value: str, words: str, colours: dict) -> QHBoxLayout:
        box = QHBoxLayout()
        box.setSpacing(4)
        pic = QLabel()
        pic.setPixmap(icon(glyph, colours.get("text_dim", "#888888")).pixmap(14, 14))
        pic.setAccessibleName(words)
        label = QLabel(value)
        label.setAccessibleName(f"{words}: {value}")
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.lines.append(label)
        box.addWidget(pic)
        box.addWidget(label)
        return box

    def _dots(self, colours: dict) -> None:
        """The accounts it is kept in: a coloured dot and the account's name, side by side."""
        if not self.info.accounts:
            self.dots.hide()
            return
        faint = colours.get("text_dim", "#888888")
        self.dots.setTextFormat(Qt.TextFormat.RichText)
        self.dots.setText("&nbsp;&nbsp;".join(
            f'<span style="color:{colour or faint}">&#9679;</span> '
            f'<span style="color:{faint}">{_escape(name)}</span>' for name, colour in self.info.accounts))

    def _picture(self, colours: dict) -> QPixmap:
        side = PHOTO_SIDE
        ratio = 2
        canvas = QPixmap(side * ratio, side * ratio)
        canvas.setDevicePixelRatio(ratio)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        path = QPainterPath()
        path.addEllipse(QRectF(0, 0, side, side))
        if self.info.photo is not None:
            painter.setClipPath(path)
            scaled = QPixmap.fromImage(self.info.photo).scaled(
                side * ratio, side * ratio, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation)
            painter.drawPixmap(QRectF(0, 0, side, side), scaled, QRectF(
                (scaled.width() - side * ratio) / 2, (scaled.height() - side * ratio) / 2,
                side * ratio, side * ratio))
        else:
            from app.ui.contacts.logic import initials

            painter.fillPath(path, QColor(colours.get("accent_soft", "#dddddd")))
            font = QFont(painter.font())
            font.setBold(True)
            font.setPixelSize(int(side * 0.38))
            painter.setFont(font)
            painter.setPen(QColor(colours.get("accent_text", "#444444")))
            painter.drawText(QRectF(0, 0, side, side), Qt.AlignmentFlag.AlignCenter, initials(self.info.name))
        painter.end()
        return canvas


def _escape(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


class PersonStrip(QWidget):
    """At most two `PersonCard`s above the results, for a query that names a contact."""

    open_requested = Signal(int)

    def __init__(self, text_of: Any, store: Any, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._text_of = text_of
        self._store = store
        self._generation = 0
        self._shown_for = ""
        #: Rows have been painted since the last keystroke; a change now would push them.
        self._painted = False
        #: `(people, query)` that landed after the rows, waiting for them to change.
        self._held: Optional[tuple] = None
        self._rows_key: Any = None
        self.cards: list[PersonCard] = []
        self.person_timer = QTimer(self)
        self.person_timer.setSingleShot(True)
        self.person_timer.setInterval(LOOKUP_DEBOUNCE_MS)
        self.person_timer.timeout.connect(self.lookup_now)
        self._column = QVBoxLayout(self)
        self._column.setContentsMargins(0, 0, 0, 0)
        self._column.setSpacing(6)
        self.setAccessibleName("People matching your search")
        self.hide()

    def typed(self, _text: str = "") -> None:
        """The box changed: look again after the person pauses."""
        self._generation += 1                    # whatever is out there is stale now
        self._painted, self._held = False, None
        if not self._query():
            self.person_timer.stop()
            self.clear()
            return
        self.person_timer.start()

    def _query(self) -> str:
        text = " ".join(str(self._text_of() or "").split())
        # Filters ("type:pdf"), slash commands and one-letter boxes are searches, not names.
        if len(text) < 2 or ":" in text or text.startswith("/"):
            return ""
        return text

    def lookup_now(self) -> None:
        """Read the contacts for the box's text on a worker; the answer lands in `_landed`."""
        query = self._query()
        if not query or self._store is None:
            self.clear()
            return
        from app.ui.workers import CallableWorker, run

        generation = self._generation
        worker = CallableWorker(lookup_people, self._store, query, component="ui.person_card")
        when_done(self, worker, finished=lambda people: self._landed(people, generation, query),
                  failed=lambda _error: self.clear())
        run(QThreadPool.globalInstance(), worker)

    def _landed(self, people: Any, generation: int, query: str) -> None:
        if generation != self._generation:
            return
        people = list(people or ())[:MAX_CARDS]
        if [c.info.id for c in self.cards] == [p.id for p in people]:
            self._shown_for = query if people else ""
            return                               # nothing on screen would change
        if self._painted:
            self._held = (people, query)         # see `rows_moved`
            return
        self._apply(people, query)

    def rows_moved(self, rows: Any) -> None:
        """The results list redrew. If its rows changed, a held card may move with them.

        A redraw of the same rows (the mail details arriving, a preference) is not a
        change: showing the card then would be the push this exists to prevent.
        """
        key = tuple(getattr(row, "chunk_id", id(row)) for row in rows or ())
        changed, self._rows_key = key != self._rows_key, key
        if changed and self._held is not None:
            held, self._held = self._held, None
            self._apply(*held)
        self._painted = bool(key)

    def _apply(self, people: list, query: str) -> None:
        if not people:
            self.clear()
            return
        self.clear()
        self._shown_for = query
        for info in people[:MAX_CARDS]:
            card = PersonCard(info, self)
            card.open_requested.connect(self.open_requested)
            self._column.addWidget(card)
            self.cards.append(card)
        self.show()

    def clear(self) -> None:
        for card in self.cards:
            self._column.removeWidget(card)
            card.hide()
            card.deleteLater()
        self.cards = []
        self._shown_for = ""
        self.hide()


def attach(view: Any) -> PersonStrip:
    """Make the strip for a `SearchView`, wired to its box. The caller places the widget.

    `view.person_timer` is the strip's timer, so `workers.stop_timers` finds it by its name
    when the window closes. The strip is also the view's `person_strip`, and its
    `open_requested(contact id)` is what the window connects to the Contacts page.
    """
    store = getattr(getattr(view, "_engine", None), "store", None)
    strip = PersonStrip(view.input.text, store, view)
    view.input.textChanged.connect(strip.typed)
    results = getattr(view, "results", None)
    if results is not None and hasattr(results, "rows_changed"):
        results.rows_changed.connect(strip.rows_moved)
    view.person_strip = strip
    view.person_timer = strip.person_timer
    return strip
