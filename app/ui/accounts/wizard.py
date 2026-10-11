"""'Add account': choose a provider, say who you are, test the connection, save.

Layer: L5

Google and Microsoft sign in through the browser (the dialog waits on a worker,
rule 5, and shows the sign-in address in case the browser did not open). Apple,
other IMAP and other CardDAV take an app password; `add_password_account` tests
the connection first and saves nothing if it fails. An error is shown with its
fix (rule 2), including the administrator-consent sentence of 1e.
"""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QRadioButton,
    QSpinBox, QStackedWidget, QVBoxLayout, QWidget,
)

from app.ui.accounts import logic, parts
from app.ui.accounts.backend import Backend
from app.ui.accounts.parts import btn, hint, notice

__all__ = ["AddAccountDialog"]


def _add(factory: Any, spec: dict, on_progress: Any = None) -> Any:
    """Worker: sign in or test, then save. Rolls back by itself when the test fails."""
    import time  # noqa: PLC0415
    svc = factory()
    kind = spec["kind"]
    if kind in ("google", "microsoft"):
        acct = svc.add_oauth_account(kind, spec["name"], mail=spec["mail"], contacts=spec["contacts"],
                                     login_hint=spec["address"], on_url=on_progress)
    else:
        provider = "imap" if kind == "google-imap" else kind
        extra = dict(logic.GMAIL_IMAP) if kind == "google-imap" else {}
        options = {"addressbook_url": spec["addressbook"]} if spec.get("addressbook") else {}
        if spec.get("server"):
            extra["server"] = spec["server"]
        if spec.get("port"):
            extra["port"] = spec["port"]
        if spec.get("security"):
            extra["security"] = spec["security"]
        acct = svc.add_password_account(
            provider, spec["address"], spec["password"], spec["name"], mail=spec["mail"],
            contacts=spec["contacts"], options=options, **extra)
    svc.accounts.update(acct.id, options={"signed_in_at": int(time.time())})
    return acct


class AddAccountDialog(QDialog):
    def __init__(self, backend: Backend, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.backend = backend
        self.setWindowTitle("Add account")
        self.account: Any = None
        self._busy = False
        self.kind = "google"
        outer = QVBoxLayout(self)
        self.stack = QStackedWidget()
        outer.addWidget(self.stack)

        # ---- step 1: choose
        choose = QWidget()
        cl = QVBoxLayout(choose)
        cl.addWidget(QLabel("What kind of account?"))
        self.choices: dict[str, QRadioButton] = {}
        for key, (title, _g, line, _help) in logic.PROVIDER_TEXT.items():
            r = QRadioButton(title)
            r.setAccessibleName(title)
            r.setToolTip(line)
            self.choices[key] = r
            cl.addWidget(r)
            cl.addWidget(hint(line))
            r.toggled.connect(lambda on, k=key: on and self._picked(k))
        self.choices["google"].setChecked(True)
        self.stack.addWidget(choose)

        # ---- step 2: details
        details = QWidget()
        dl = QVBoxLayout(details)
        self.help = hint("")
        dl.addWidget(self.help)
        self.guide = btn("Show me how", "external-link", tip="Opens the step-by-step guide")
        dl.addWidget(self.guide)
        self.form = QFormLayout()
        dl.addLayout(self.form)
        self.name = QLineEdit()
        self.name.setAccessibleName("Name for this account")
        self.name.setPlaceholderText("Work, Home...")
        self.address = QLineEdit()
        self.address.setAccessibleName("Address or login")
        self.address.setPlaceholderText("you@example.com")
        self.password = QLineEdit()
        self.password.setAccessibleName("App password")
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.password.setPlaceholderText("App password")
        self.server = QLineEdit()
        self.server.setAccessibleName("Server")
        self.server.setPlaceholderText("imap.example.com")
        self.port = QSpinBox()
        self.port.setRange(0, 65535)
        self.port.setSpecialValueText("Automatic")
        self.port.setAccessibleName("Port")
        self.security = QComboBox()
        self.security.setAccessibleName("Connection security")
        for k, t in (("ssl", "SSL/TLS"), ("starttls", "STARTTLS"), ("none", "None")):
            self.security.addItem(t, k)
        self.addressbook = QLineEdit()
        self.addressbook.setAccessibleName("Address book address")
        self.addressbook.setPlaceholderText("Only if it cannot be found by itself")
        self.port.setToolTip("The server's port. Automatic uses the usual one for the security chosen.")
        self.security.setToolTip("How the connection to the server is protected. SSL/TLS is the usual choice.")
        self.contacts = QCheckBox("Sync contacts")
        self.contacts.setChecked(True)
        self.contacts.setToolTip("Bring this account's contacts into Leasha and keep them in step.")
        self.mail = QCheckBox("Fetch mail (read only)")
        self.mail.setToolTip("Fetch this account's mail so it can be searched. Leasha never changes the mailbox.")
        self._rows = {
            "name": ("Name", self.name), "address": ("Address", self.address),
            "password": ("App password", self.password), "server": ("Server", self.server),
            "port": ("Port", self.port), "security": ("Security", self.security),
            "addressbook": ("Address book", self.addressbook),
        }
        self.row_widgets: dict[str, tuple[QLabel, QWidget]] = {}
        for key, (label, w) in self._rows.items():
            lab = QLabel(label)
            self.form.addRow(lab, w)
            self.row_widgets[key] = (lab, w)
        dl.addWidget(self.contacts)
        dl.addWidget(self.mail)
        dl.addWidget(hint("Mail is only read: Leasha never changes the mailbox."))
        self.notice = notice()
        dl.addWidget(self.notice)
        self.stack.addWidget(details)

        # ---- buttons
        row = QHBoxLayout()
        self.back = btn("Back", "undo-2", tip="Choose another kind of account")
        self.next = btn("Next", "chevron-right", "primary", tip="Enter the account's details")
        self.add = btn("Test connection and add", "link", "primary",
                       tip="Checks the connection first; nothing is saved if it fails")
        self.cancel = btn("Cancel", "x")
        for b in (self.back, self.add, self.next):
            row.addWidget(b)
        row.addStretch(1)
        row.addWidget(self.cancel)
        outer.addLayout(row)
        self.back.clicked.connect(lambda: self._go(0))
        self.next.clicked.connect(lambda: self._go(1))
        self.add.clicked.connect(self.start)
        self.cancel.clicked.connect(self.reject)
        self.guide.clicked.connect(lambda: parts.open_guide(logic.GUIDE_ANCHORS[self._base()]))
        self._go(0)

    # -- steps
    def _base(self) -> str:
        return "google" if self.kind == "google-imap" else self.kind

    def _picked(self, key: str) -> None:
        self.kind = key

    def _go(self, step: int) -> None:
        self.stack.setCurrentIndex(step)
        self.back.setVisible(step == 1)
        self.add.setVisible(step == 1)
        self.next.setVisible(step == 0)
        if step == 1:
            self._layout_for(self.kind)

    def _layout_for(self, kind: str) -> None:
        title, _g, _line, help_ = logic.PROVIDER_TEXT[kind]
        oauth = kind in ("google", "microsoft")
        if kind == "apple":
            from app.accounts.service import IMAP_PRESETS  # noqa: PLC0415
            help_ = IMAP_PRESETS["apple"]["help"]
        self.help.setText(help_)
        shown = {"name", "address"}
        if not oauth:
            shown |= {"password"}
        if kind in ("imap", "carddav"):
            shown |= {"server", "port", "security"}
        if kind == "carddav":
            shown |= {"addressbook"}
        for key, (lab, w) in self.row_widgets.items():
            lab.setVisible(key in shown)
            w.setVisible(key in shown)
        if kind == "google-imap":
            self.contacts.setChecked(False)
            self.mail.setChecked(True)
        elif kind in ("imap",):
            self.contacts.setChecked(False)
            self.mail.setChecked(True)
        elif kind == "carddav":
            self.contacts.setChecked(True)
            self.mail.setChecked(False)
        self.contacts.setVisible(kind not in ("google-imap", "imap"))
        self.mail.setVisible(kind not in ("carddav",))
        self.address.setPlaceholderText("Optional - helps pick the right account in the browser" if oauth
                                        else "you@example.com")
        self.add.setText("Sign in and add" if oauth else "Test connection and add")

    # -- the work
    def spec(self) -> dict:
        return {"kind": self.kind, "name": self.name.text().strip(), "address": self.address.text().strip(),
                "password": self.password.text(), "server": self.server.text().strip(),
                "port": int(self.port.value()) or None, "security": str(self.security.currentData()),
                "addressbook": self.addressbook.text().strip(),
                "mail": self.mail.isChecked() and self.kind != "carddav",
                "contacts": self.contacts.isChecked() and self.kind not in ("google-imap", "imap")}

    def start(self) -> None:
        if self._busy:
            return
        spec = self.spec()
        oauth = spec["kind"] in ("google", "microsoft")
        if not oauth and (not spec["address"] or not spec["password"]):
            self._say("Enter the address and the app password first.")
            return
        if not spec["mail"] and not spec["contacts"]:
            self._say("Choose at least one of contacts and mail.")
            return
        self._busy = True
        self.add.setEnabled(False)
        self._say("Your browser is opening so you can sign in. Leasha waits here." if oauth
                  else "Testing the connection…")
        self.backend.call(_add, self.backend.factory, spec, progress=self._url, done=self._added,
                          fail=self._failed)

    def _url(self, url: Any) -> None:
        self._say(f"If your browser did not open, go to: {url}")

    def _added(self, account: Any) -> None:
        self._busy = False
        self.account = account
        self.accept()

    def _failed(self, err: Any) -> None:
        self._busy = False
        self.add.setEnabled(True)
        # Plainly: what happened, then what to do (rule 2; 1e for an administrator's refusal).
        self._say(f"{getattr(err, 'message', err)} {getattr(err, 'suggestion', '')}".strip())

    def _say(self, text: str) -> None:
        self.notice.setText(text)
        self.notice.setVisible(bool(text))
