"""From your index: the mail, files and photos Leasha holds about one contact.

Layer: L5

Order 1k 6f. Under a contact in the editor: the latest mail with this person (subject, date,
whether it came from them or went to them, and which account for mail fetched from one), the
files exchanged with them as attachments, photos of them, and the first and last time they were
in touch. **Every item opens**: it goes through `workers.open_row_async`, the route every page's
Open takes (a message in Outlook or its account, an attachment from a copy, a photo in its
viewer).

Photos are those of the Photo Tagger person the contact is *accepted* as (`people.py`, D9's
rule): a name that only matches is a suggestion, and the panel offers "Suggest link" for a
contact with none, then "Yes, link them" or "Not them" for the suggestion.

IA-6 (UX review 2026-10-14): **and onward.** "See all photos" opens the Photos page narrowed
to the linked person (`who:`), "See mail from them" the Mail page narrowed to the contact's
address (`from:`) - the words in `presenter.photo_people`, the pages opened by the window
(`photos_requested`, `mail_requested`; `ContactsPage` hands them to its host).

Rule 5: everything is read, and every link written, on a worker (`loader.run_task`); the facts
themselves are `app.contacts.activity.gather`, the same reading the MCP tool `contact_activity`
gives. Nothing here writes to mail or files.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QToolButton, QVBoxLayout, QWidget

from app.ui.contacts import loader
from app.ui.theme import SPACE_XS, theme_colours
from app.ui.widgets.buttons import action_button
from app.ui.widgets.icons import icon

__all__ = ["IndexPanel", "load_index", "SHOWN"]

#: How many of each kind the panel lists. The MCP tool's default is the same ten; eight fits.
SHOWN = 8


def day(seconds: Any) -> str:
    try:
        return time.strftime("%d %b %Y", time.localtime(float(seconds))) if seconds else ""
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def load_index(hub: Any, cid: int, emails: list[str]) -> dict:
    """Everything the panel shows for contact `cid`. **Runs on a worker.**"""
    from app.contacts import people
    from app.contacts.activity import gather

    store = hub.store
    try:
        from app.accounts.store import AccountStore

        names = {int(a.id): str(a.name or a.address) for a in AccountStore(store).list()}
    except Exception:                                   # noqa: BLE001 - an account name is an extra
        names = {}
    found = gather(store, cid, [e for e in emails if e], SHOWN, names)
    link = people.pile_link(hub, cid)
    found["link_state"] = link.state if link is not None else "none"
    found["link_name"] = link.pile_name if link is not None else ""
    return found


class IndexPanel(QWidget):
    """The "From your index" body of the contact editor."""

    error = Signal(object)
    #: Something was written (a link accepted); the page may want to reload its photo cache.
    changed = Signal(int)
    #: IA-6: "See all photos" / "See mail from them" - the box the other page should read.
    photos_requested = Signal(str)
    mail_requested = Signal(str)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.hub: Any = None
        self.cid: Optional[int] = None
        self._emails: list[str] = []
        self._gen = 0
        self._note = ""
        #: How an item opens: `(path)`. Replaced in tests; the window's own route otherwise.
        self.opener: Callable[[str], Any] = self._open_path
        self.setAccessibleName("From your index")
        self.heading = QWidget()
        head = QHBoxLayout(self.heading)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(SPACE_XS)
        pic = QLabel()
        pic.setPixmap(icon("search", theme_colours().get("text_dim", "#888888")).pixmap(16, 16))
        title = QLabel("From your index")
        title.setObjectName("contactSectionTitle")
        head.addWidget(pic)
        head.addWidget(title)
        head.addStretch(1)
        self.summary = QLabel("")
        self.summary.setObjectName("resultsSummary")
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("First and last contact")
        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(SPACE_XS)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(SPACE_XS)
        outer.addWidget(self.heading)
        outer.addWidget(self.summary)
        outer.addLayout(self.body)
        self.items: list[QToolButton] = []
        self.link_buttons: dict[str, Any] = {}
        #: IA-6: "See all photos" / "See mail from them", by "photos" / "mail".
        self.see_buttons: dict[str, Any] = {}
        self.clear()

    # ----------------------------------------------------------------- loading
    def configure(self, hub: Any) -> None:
        self.hub = hub

    def clear(self) -> None:
        self._gen += 1
        self.cid = None
        self._emails = []
        self._wipe()
        self.summary.setText("")
        self.setVisible(False)

    def show_for(self, cid: int, emails: list[str]) -> None:
        """Read the index for this contact on a worker and draw the answer."""
        if self.hub is None:
            return
        self._gen += 1
        gen = self._gen
        self.cid, self._emails = cid, [e.strip() for e in emails if e and e.strip()]
        self.setVisible(True)
        self._wipe()
        self.summary.setText("Looking through your index…")
        hub, mails = self.hub, list(self._emails)
        loader.run_task(self, lambda: load_index(hub, cid, mails),
                        lambda found: self._landed(found, gen, cid),
                        lambda err: (self.summary.setText("Could not read your index for this contact."),
                                     self.error.emit(err)) if gen == self._gen else None,
                        component="ui.contacts.index")

    def _landed(self, found: dict, gen: int, cid: int) -> None:
        if gen != self._gen or cid != self.cid:
            return
        self._wipe()
        first, last = day(found.get("first_at")), day(found.get("last_at"))
        if not self._emails:
            self.summary.setText("This contact has no email address, so no mail can be matched.")
        elif first or last:
            self.summary.setText(f"First in touch {first or '-'}  ·  last in touch {last or '-'}")
        else:
            self.summary.setText("No mail with this person is in your index yet.")
        if self._note:
            self.summary.setText(f"{self._note}  {self.summary.text()}")
            self._note = ""
        self._section("Latest mail", [self._mail_item(m) for m in found.get("messages", [])])
        self._section("Files exchanged", [self._file_item(f) for f in found.get("files_exchanged", [])])
        self._section("Photos", [self._photo_item(p) for p in found.get("photos", [])])
        self._link_row(found.get("link_state", "none"), found.get("link_name", ""),
                       bool(found.get("photos")))
        self._see_row(found.get("link_state", "none"), found.get("link_name", ""))

    # ------------------------------------------------------------------- items
    def _wipe(self) -> None:
        while self.body.count():
            item = self.body.takeAt(0)
            widget = item.widget() if item is not None else None
            if widget is not None:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
            elif item is not None and item.layout() is not None:
                self._drop_layout(item.layout())
        self.items = []
        self.link_buttons = {}
        self.see_buttons = {}

    @staticmethod
    def _drop_layout(layout: Any) -> None:
        while layout.count():
            item = layout.takeAt(0)
            if item is not None and item.widget() is not None:
                w = item.widget()
                w.hide()
                w.setParent(None)
                w.deleteLater()

    def _section(self, words: str, items: list) -> None:
        if not items:
            return
        title = QLabel(words)
        title.setObjectName("contactSectionTitle")
        title.setAccessibleName(words)
        self.body.addWidget(title)
        for button in items:
            self.body.addWidget(button)
            self.items.append(button)

    def _item(self, glyph: str, text: str, tip: str, path: str, name: str) -> QToolButton:
        button = QToolButton()
        button.setIcon(icon(glyph, theme_colours().get("text_dim", "#888888")))
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        button.setAutoRaise(True)
        button.setToolTip(tip)
        button.setAccessibleName(name)
        button.setProperty("path", path)
        button.setStyleSheet("QToolButton { text-align: left; padding: 3px 4px; }")
        button.clicked.connect(lambda _c=False, p=path: self.opener(p))
        return button

    def _mail_item(self, m: dict) -> QToolButton:
        mine = m.get("direction") == "to them"
        subject = m.get("subject") or "(no subject)"
        bits = [day(m.get("sent_at")) or m.get("sent", "")[:10], "to them" if mine else "from them"]
        if m.get("account"):
            bits.append(m["account"])
        text = f"{subject}  -  {'  ·  '.join(b for b in bits if b)}"
        return self._item("send" if mine else "inbox", text,
                          f"Open this message ({m.get('path', '')})", m.get("path", ""),
                          f"Mail: {subject}")

    def _file_item(self, f: dict) -> QToolButton:
        message = f.get("message") or {}
        sent = day(message.get("sent_at")) or str(message.get("sent", ""))[:10]
        text = f"{f.get('name') or f.get('path', '')}  -  {'on ' + (message.get('subject') or 'a message') + ', ' + sent}"
        return self._item("file-text", text, f"Open this attachment ({f.get('path', '')})",
                          f.get("path", ""), f"File: {f.get('name', '')}")

    def _photo_item(self, p: dict) -> QToolButton:
        path = str(p.get("path", ""))
        name = path.replace("\\", "/").rsplit("/", 1)[-1]
        text = f"{name}  -  {str(p.get('taken', ''))[:10]}" if p.get("taken") else name
        return self._item("image", text, f"Open this photo ({path})", path, f"Photo: {name}")

    # --------------------------------------------------------------- the link
    def _link_row(self, state: str, pile_name: str, has_photos: bool) -> None:
        """2c in the panel: accept or refuse a suggested person, or ask for a suggestion."""
        row = QHBoxLayout()
        row.setContentsMargins(0, SPACE_XS, 0, 0)
        label = QLabel("")
        label.setObjectName("resultsSummary")
        label.setWordWrap(True)
        label.setAccessibleName("Photos of this person")
        if state == "accepted":
            label.setText(f"Photos are of {pile_name}." if has_photos else
                          f"Linked to {pile_name}, who has no photos in your index yet.")
            row.addWidget(label, 1)
        elif state == "suggested":
            label.setText(f"Is this {pile_name} in your photos?")
            row.addWidget(label, 1)
            yes = action_button("Yes, link them", "check", "primary")
            yes.setToolTip(f"Show the photos of {pile_name} here")
            yes.clicked.connect(lambda: self._decide(True))
            no = action_button("Not them", "x", "secondary")
            no.setToolTip("Do not suggest this person for this contact again")
            no.clicked.connect(lambda: self._decide(False))
            row.addWidget(yes)
            row.addWidget(no)
            self.link_buttons = {"accept": yes, "decline": no}
        elif state == "declined":
            label.setText("You said the person in your photos is not this contact.")
            row.addWidget(label, 1)
        else:
            label.setText("Photos of this person appear once they are linked to a named person.")
            row.addWidget(label, 1)
            suggest = action_button("Suggest link", "user-plus", "secondary")
            suggest.setToolTip("Look for a person you have named in Photos with this contact's name")
            suggest.clicked.connect(self.suggest)
            row.addWidget(suggest)
            self.link_buttons = {"suggest": suggest}
        self.body.addLayout(row)

    def _see_row(self, state: str, pile_name: str) -> None:
        """IA-6: everything, on its own page - the person's photos, the mail from them."""
        from app.ui.presenter.photo_people import mail_box, photos_box

        photos = photos_box(pile_name) if state == "accepted" else ""
        mail = mail_box(self._emails[0]) if self._emails else ""
        if not photos and not mail:
            return
        row = QHBoxLayout()
        row.setContentsMargins(0, SPACE_XS, 0, 0)
        if photos:
            see = action_button("See all photos", "image", "secondary")
            see.setToolTip(f"Open Photos on every photo of {pile_name}")
            see.clicked.connect(lambda _c=False, box=photos: self.photos_requested.emit(box))
            row.addWidget(see)
            self.see_buttons["photos"] = see
        if mail:
            see = action_button("See mail from them", "mail", "secondary")
            see.setToolTip(f"Open Mail on the messages from {self._emails[0]}")
            see.clicked.connect(lambda _c=False, box=mail: self.mail_requested.emit(box))
            row.addWidget(see)
            self.see_buttons["mail"] = see
        row.addStretch(1)
        self.body.addLayout(row)

    def suggest(self) -> None:
        """Ask `people.suggest_pile_links` for a match, then read this contact again."""
        self._write(self._suggest_work, then="No person you have named in Photos has this name.")

    def _suggest_work(self) -> int:
        from app.contacts import people

        people.suggest_pile_links(self.hub)
        link = people.pile_link(self.hub, self.cid) if self.cid is not None else None
        return 1 if link is not None and link.state == "suggested" else 0

    def _decide(self, accept: bool) -> None:
        cid, hub = self.cid, self.hub
        if cid is None:
            return

        def work() -> int:
            from app.contacts import people

            (people.accept_pile_link if accept else people.decline_pile_link)(hub, cid)
            return 1

        self._write(work, then="")

    def _write(self, work: Callable[[], int], *, then: str) -> None:
        cid = self.cid
        if cid is None or self.hub is None:
            return

        def done(found: int) -> None:
            if cid != self.cid:
                return
            self.changed.emit(cid)
            self.show_for(cid, self._emails)
            self._note = "" if found else then

        loader.run_task(self, work, done, component="ui.contacts.index")

    # -------------------------------------------------------------------- open
    def _open_path(self, path: str) -> None:
        if not path:
            return
        from app.ui.presenter.opening import Place
        from app.ui.workers import open_row_async

        open_row_async(getattr(self.hub, "store", None), Place(path))
