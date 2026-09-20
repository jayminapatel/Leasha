r"""Order 202626270602 (0n) section 5 - the read-only guarantee, for every report path.

Layer: L4 + L5

"no write syscalls to user paths from any report path - the view-only invariant
extended." (Non-negotiable 10: the indexer opens and reads; it never modifies,
moves or deletes a document or an email - and a report is a reader, not a
second writer.)

Three independent proofs, because each one alone has a way to be quietly
vacuous:

1. **The database refuses writes.** The store's own connection is given a
   SQLite authorizer that denies every INSERT, UPDATE, DELETE and schema change,
   and then every report and timeline function is run. If any of them writes,
   it raises - so it cannot be quietly reading nothing.
2. **Nothing under the user's folders is opened for writing, created,
   renamed or removed.** Every write-shaped call (`open`, `os.open`, `os.remove`,
   `os.replace`, `shutil.copy`, `Path.write_text`...) is recorded while the
   command-line reports run against an index whose files are real, and the
   folder is compared byte for byte afterwards.
3. **The source says so** - a scan of every report module, in the style of
   `test_the_inheritance_module_never_writes_to_disk`.

Each guard is shown able to fail (`test_the_..._would_notice_...`).
"""

from __future__ import annotations

import builtins
import io
import os
import re
import shutil
import sqlite3
from pathlib import Path

import pytest

from app import cli
from app.storage.sqlite_store import SqliteStore
from tests.unit.test_reports_fixtures import family_index                  # noqa: F401  (fixture)
from tests.unit.timeline_env import add_file, add_mail, noon

ROOT = Path(__file__).resolve().parents[2]

#: Every module a report or the timeline is made of - **listed by glob, so a new
#: one is covered the day it is written.**
REPORT_MODULES = sorted(
    [*(ROOT / "app" / "reports").glob("*.py"),
     ROOT / "app" / "cli" / "timeline.py",
     ROOT / "app" / "ui" / "timeline_view.py",
     ROOT / "app" / "ui" / "presenter" / "timeline.py",
     ROOT / "app" / "ui" / "controllers" / "timeline_controller.py",
     *(ROOT / "app" / "ui" / "widgets").glob("timeline_*.py")])

WRITE_PATTERNS = (
    r"\bINSERT\b", r"\bUPDATE\s+\w+\s+SET\b", r"\bDELETE\s+FROM\b", r"\bDROP\b", r"\bALTER\s+TABLE\b",
    r"\bCREATE\s+(TABLE|INDEX)\b", r"\.write_text\(", r"\.write_bytes\(", r"\bopen\([^)]*['\"][wax+]",
    r"os\.remove", r"os\.unlink", r"os\.rename", r"os\.replace", r"shutil\.(copy|move|rmtree)",
    r"\.unlink\(", r"\.mkdir\(", r"\.touch\(", r"\.commit\(", r"store\.write\(", r"\.set_state\(",
)

#: The two deliberate exceptions, each a fact that has to stay true:
ALLOWED = {
    # `TimelinePicker` remembers *what to show* and *whether to group* in
    # `index_state` - Leasha's own settings row, never a user's file or a
    # document row.
    ("timeline_picker.py", r"\.set_state\("),
    # `reports_view.py` is not in this list (it writes the PDF *the person chose
    # the location of*, in `_write_pdf`); the timeline has no export.
}


def source_offences(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    # docstrings and strings may *mention* writing; SQL strings are the real risk, so
    # only strip triple-quoted prose blocks before matching.
    code = re.sub(r'r?"""[\s\S]*?"""', "", code)
    return [f"{path.name}: {p}" for p in WRITE_PATTERNS
            if re.search(p, code, re.IGNORECASE) and (path.name, p) not in ALLOWED]


def test_there_are_report_modules_to_check():
    assert len(REPORT_MODULES) >= 12, [p.name for p in REPORT_MODULES]


@pytest.mark.parametrize("path", REPORT_MODULES, ids=lambda p: p.name)
def test_no_report_or_timeline_module_writes_anywhere(path):
    assert source_offences(path) == []


def test_the_source_scan_would_notice_a_write(tmp_path):
    bad = tmp_path / "bad.py"
    bad.write_text('def f(store):\n    store.conn.execute("DELETE FROM files")\n'
                   '    open("x.txt", "w").write("hi")\n', encoding="utf-8")
    found = source_offences(bad)
    assert any("DELETE" in f for f in found) and any("open" in f for f in found)


# ---------------------------------------------------------------------------
# 1. The database refuses writes while every report runs
# ---------------------------------------------------------------------------

def deny_writes(conn: sqlite3.Connection) -> list:
    """Install an authorizer that denies (and records) any write; returns the record."""
    denied: list = []
    writing = {sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE,
               sqlite3.SQLITE_CREATE_TABLE, sqlite3.SQLITE_CREATE_INDEX, sqlite3.SQLITE_DROP_TABLE,
               sqlite3.SQLITE_DROP_INDEX, sqlite3.SQLITE_ALTER_TABLE, sqlite3.SQLITE_CREATE_VIEW,
               sqlite3.SQLITE_CREATE_TRIGGER, sqlite3.SQLITE_DROP_VIEW, sqlite3.SQLITE_DROP_TRIGGER}

    def authorizer(action, arg1, arg2, _db, _source):
        if action in writing:
            denied.append((action, arg1, arg2))
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    conn.set_authorizer(authorizer)
    return denied


def test_the_authorizer_would_notice_a_write(tmp_path):
    with SqliteStore(tmp_path / "t.db") as store:
        denied = deny_writes(store.conn)
        with pytest.raises(sqlite3.DatabaseError):
            store.conn.execute("DELETE FROM files")
        assert denied


def test_every_report_and_every_timeline_function_runs_with_the_database_read_only(family_index, qtbot):
    from app.reports import space
    from app.reports.inheritance import catalogue_sources, render_inheritance_document, report_generated_at
    from app.reports.timeline import (
        Cursor, Period, date_of_file, refresh_overview, timeline_overview, timeline_page,
    )
    from app.ui.reports_view import _report_snapshot

    store, ids = family_index
    add_file(store, r"D:\Family\Photos\lake.jpg", mtime=noon(2019, 3, 1), taken=noon(2015, 6, 10),
             phash="00ff00ff00ff00ff")
    add_file(store, r"D:\Family\Photos\lake2.jpg", mtime=noon(2019, 3, 1), taken=noon(2015, 6, 10, 2),
             phash="00ff00ff00ff00fe")
    mail = add_mail(store, "Hello", sent=noon(2015, 6, 25), container_mtime=noon(2021, 1, 1))
    denied = deny_writes(store.conn)

    sources = catalogue_sources(store, roots=[r"D:\Family"])
    render_inheritance_document(sources, generated_at=report_generated_at(store))
    space.find_duplicate_groups(store)
    space.find_near_duplicate_photo_groups(store)
    space.find_source_duplicate_share(store)
    space.find_source_uniqueness(store)
    space.total_reclaimable_bytes(store)
    space.hash_coverage(store)
    assert _report_snapshot(store) is not None
    june = Period.month(2015, 6)
    for kind in ("everything", "photos", "videos", "documents", "mail", "code"):
        timeline_page(store, june, kind=kind, connected={})
        timeline_overview(store, kind)
    page = timeline_page(store, Period.between(None, None), limit=2, connected={})
    timeline_page(store, Period.between(None, None), limit=2, cursor=page.cursor, connected={})
    timeline_page(store, june, cursor=Cursor(noon(2015, 6, 10), 1, 1), connected={})
    assert refresh_overview(store, "everything", None) is not None
    assert date_of_file(store, mail) == noon(2015, 6, 25)
    assert denied == [], f"a report tried to write: {denied}"


# ---------------------------------------------------------------------------
# 2. Nothing under the user's folders is written, created, renamed or removed
# ---------------------------------------------------------------------------

class WriteRecorder:
    """Records every write-shaped filesystem call made through Python."""

    def __init__(self, monkeypatch):
        self.paths: list[str] = []
        real_open, real_os_open = builtins.open, os.open

        def opener(file, mode="r", *args, **kwargs):
            if isinstance(mode, str) and any(c in mode for c in "wax+"):
                self.paths.append(os.fspath(file) if not isinstance(file, int) else "")
            return real_open(file, mode, *args, **kwargs)

        def os_opener(path, flags, *args, **kwargs):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
                self.paths.append(os.fspath(path))
            return real_os_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", opener)
        monkeypatch.setattr(io, "open", opener)
        monkeypatch.setattr(os, "open", os_opener)
        for name in ("remove", "unlink", "rmdir", "mkdir", "makedirs", "rename", "replace"):
            real = getattr(os, name)
            monkeypatch.setattr(os, name, self._recording(real))
        for name in ("copy", "copy2", "copyfile", "move", "rmtree"):
            monkeypatch.setattr(shutil, name, self._recording(getattr(shutil, name)))

    def _recording(self, real):
        def wrapper(*args, **kwargs):
            for arg in args[:2]:
                if isinstance(arg, (str, os.PathLike)):
                    self.paths.append(os.fspath(arg))
            return real(*args, **kwargs)
        return wrapper

    def under(self, folder: Path) -> list[str]:
        root = os.path.normcase(str(folder))
        return [p for p in self.paths if os.path.normcase(os.path.abspath(p)).startswith(root)]


def snapshot(folder: Path) -> dict:
    return {str(p.relative_to(folder)): (p.stat().st_size, p.stat().st_mtime_ns, p.read_bytes())
            for p in sorted(folder.rglob("*")) if p.is_file()}


def test_the_recorder_would_notice_a_write_into_a_users_folder(tmp_path, monkeypatch):
    mine = tmp_path / "mine"
    mine.mkdir()
    recorder = WriteRecorder(monkeypatch)
    (mine / "note.txt").write_text("hi", encoding="utf-8")
    os.remove(mine / "note.txt")
    assert len(recorder.under(mine)) >= 2


def test_the_command_line_reports_never_touch_the_folders_they_describe(tmp_path, monkeypatch, capsys):
    from PIL import Image

    from tests.unit.test_timeline_cli import env_file, run

    family = tmp_path / "family"
    (family / "Photos").mkdir(parents=True)
    Image.new("RGB", (32, 32), (9, 9, 9)).save(family / "Photos" / "lake.jpg")
    (family / "Letters").mkdir()
    (family / "Letters" / "to-the-bank.txt").write_text("PRIVATE LETTER BODY", encoding="utf-8")
    env = env_file(tmp_path)
    cli.cmd_init(cli.build_parser().parse_args(["init", "--env", env]))
    with SqliteStore(tmp_path / "data" / "index.db") as store:
        for path in family.rglob("*.*"):
            add_file(store, str(path), mtime=path.stat().st_mtime_ns, content_hash=f"h-{path.name}")
        store.set_state("ui:roots", str(family))
    before = snapshot(family)
    capsys.readouterr()

    recorder = WriteRecorder(monkeypatch)
    for argv in (["report", "inheritance"], ["report", "space"], ["report", "space", "--json"],
                 ["timeline"], ["timeline", "--year", "2025"], ["timeline", "--json"]):
        assert run([*argv, "--env", env]) == cli.EXIT_OK
    monkeypatch.undo()

    assert recorder.under(family) == [], f"a report wrote into the user's folder: {recorder.under(family)}"
    assert snapshot(family) == before
    assert "PRIVATE LETTER BODY" not in capsys.readouterr().out


def test_the_timeline_window_never_touches_a_photograph_it_shows(qtbot, tmp_path, monkeypatch):
    from PIL import Image

    from app.reports.timeline import Period
    from app.ui.timeline_view import TimelineView

    folder = tmp_path / "pictures"
    folder.mkdir()
    for i in range(3):
        Image.new("RGB", (80, 60), (i * 50, 10, 10)).save(folder / f"p{i}.jpg")
    before = snapshot(folder)
    with SqliteStore(tmp_path / "t.db") as store:
        for i in range(3):
            add_file(store, str(folder / f"p{i}.jpg"), mtime=noon(2019, 1, 1), taken=noon(2015, 6, 10, i * 400))
        view = TimelineView(store)
        qtbot.addWidget(view)
        view.resize(900, 400)
        view.show()
        recorder = WriteRecorder(monkeypatch)
        view.browse(Period.month(2015, 6))
        qtbot.waitUntil(lambda: view.list.block_count() > 0 and not view._loading, timeout=8000)
        view.grab()
        qtbot.waitUntil(lambda: len(view.list._pictures) == 3, timeout=8000)
        from PyQt6.QtCore import QThreadPool
        QThreadPool.globalInstance().waitForDone(5000)
        monkeypatch.undo()
    assert recorder.under(folder) == []
    assert snapshot(folder) == before
