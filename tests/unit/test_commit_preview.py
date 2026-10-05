r"""Order 0y §3c: a commit opens as a commit.

Layer: L5

Selecting a history row shows, in the preview pane, the commit's message, its
author and date, the files it changed, and the diff with the searched text
highlighted. `git show` runs on the pane's worker, and only for the row that is
selected.

`gitsearch.show_commit` itself - the command, the parsing, a real merge - is in
`test_git_streaming.py`. Nothing here starts git.
"""

from __future__ import annotations

import ast
import inspect
import os
from pathlib import Path

import pytest

from app.search.gitsearch import CommitDetail, GitRow
from app.ui import commit_preview as module
from app.ui.commit_preview import commit_html, commit_preview, date_in_words
from app.ui.presenter import git_result_row
from app.ui.preview_loader import KIND_HTML, KIND_TEXT, load_preview_for

DETAIL = CommitDetail(
    commit="abc1234" + "0" * 33, author="Ada Lovelace", email="ada@example.com",
    date="2024-01-02T09:00:00+00:00", subject="Rename CustomerId <at last>",
    body="Because CustomerId was misleading.\n\n<script>alert(1)</script>",
    files=(("M", "src/Order.cs", ""), ("A", "src/New.cs", ""),
           ("R", "src/Moved.cs", "src/Old.cs")),
    diff="\n".join([
        "diff --git a/src/Order.cs b/src/Order.cs",
        "@@ -1 +1 @@",
        "-    var CustomerId = 1;  // <old>",
        "+    var ClientId = 1;",
        "     unchanged & kept",
    ]))


def history_row(**fields):
    base = dict(kind="commit", commit="abc1234" + "0" * 33, date="2024-01-02",
                author="Ada Lovelace", subject="Rename CustomerId <at last>",
                repo="leasha", root="D:/SearchProject")
    base.update(fields)
    return git_result_row(GitRow(**base), "", "CustomerId")


# --- the page ---------------------------------------------------------------------


def test_the_date_is_said_in_words() -> None:
    assert date_in_words("2024-01-02T09:00:00+00:00") == "Tuesday 2 January 2024, 09:00"
    assert date_in_words("2026-09-30T14:27:52+05:45") == "Wednesday 30 September 2026, 14:27"
    assert date_in_words("not a date") == "not a date"
    assert date_in_words("") == ""


def test_the_page_has_the_message_the_author_the_date_the_files_and_the_diff() -> None:
    page = commit_html(DETAIL, "CustomerId")
    assert "<h3>Rename " in page
    assert "was misleading." in page
    assert "<b>Ada Lovelace</b> &lt;ada@example.com&gt;" in page
    assert "Tuesday 2 January 2024, 09:00" in page
    assert DETAIL.commit in page
    assert "<b>3 files changed</b>" in page
    for piece in ("Modified", "src/Order.cs", "Added", "src/New.cs",
                  "Renamed", "src/Moved.cs (was src/Old.cs)"):
        assert piece in page, piece
    assert "var ClientId = 1;" in page and "unchanged &amp; kept" in page


def test_the_searched_text_is_highlighted_wherever_it_is() -> None:
    page = commit_html(DETAIL, "customerid")           # found whatever its capitals
    marked = '<span style="background-color:#ffe066; color:#1f2328">CustomerId</span>'
    assert page.count(marked) == 3, "the subject, the message and the removed line"
    assert "<h3>Rename " + marked in page
    assert "var " + marked + " = 1;" in page
    assert commit_html(DETAIL, "").count("background-color") == 0


def test_added_and_removed_lines_are_told_apart() -> None:
    page = commit_html(DETAIL, "")
    assert '<span style="color:#e5534b">-    var CustomerId = 1;' in page
    assert '<span style="color:#2da44e">+    var ClientId = 1;</span>' in page
    assert "<b>diff --git a/src/Order.cs b/src/Order.cs</b>" in page
    assert '<span style="color:#8a63d2">@@ -1 +1 @@</span>' in page


def test_nothing_from_the_repository_is_obeyed_as_markup() -> None:
    page = commit_html(DETAIL, "CustomerId")
    assert "<script>" not in page and "&lt;script&gt;alert(1)&lt;/script&gt;" in page
    assert "&lt;at last&gt;" in page and "// &lt;old&gt;" in page


def test_one_file_is_one_file() -> None:
    one = CommitDetail(commit="a" * 40, subject="s", files=(("D", "gone.py", ""),))
    page = commit_html(one)
    assert "<b>1 file changed</b>" in page and "Deleted" in page and "<pre>" not in page


# --- from a row to a preview -----------------------------------------------------------


def test_a_history_row_previews_as_its_commit() -> None:
    asked: list = []

    def show(repo, sha):
        asked.append((repo, sha))
        return DETAIL

    preview = commit_preview(history_row(), show=show)
    assert asked == [(Path("D:/SearchProject"), "abc1234" + "0" * 33)]
    assert preview.kind == KIND_HTML and preview.error is None
    assert preview.title == "Rename CustomerId <at last>"
    assert preview.subtitle == ("Commit abc12340  ·  Ada Lovelace  ·  "
                                "Tuesday 2 January 2024, 09:00")
    assert "background-color:#ffe066" in preview.body
    assert preview.notice == ""


def test_a_diff_that_was_cut_says_so() -> None:
    cut = CommitDetail(commit="a" * 40, subject="s", diff="+x", truncated=True)
    preview = commit_preview(history_row(), show=lambda _r, _s: cut)
    assert "longer than the preview shows" in preview.notice and "3,000" in preview.notice


def test_a_commit_git_cannot_show_still_shows_what_the_row_knew() -> None:
    failed = CommitDetail(ok=False, error="fatal: bad object\nabc1234")
    preview = commit_preview(history_row(), show=lambda _r, _s: failed)
    assert preview.kind == KIND_TEXT and preview.error is None
    assert "Rename CustomerId" in preview.body and "Ada Lovelace" in preview.body
    assert preview.notice == "git could not show this commit: fatal: bad object abc1234"


def test_the_pane_loader_sends_a_history_row_to_git_show(monkeypatch) -> None:
    from app.search import gitsearch

    asked: list = []
    monkeypatch.setattr(gitsearch, "show_commit",
                        lambda repo, sha: asked.append(sha) or DETAIL)
    preview = load_preview_for(history_row())
    assert asked == ["abc1234" + "0" * 33] and preview.kind == KIND_HTML

    changed = history_row(kind="change", path="src/Order.cs", status="M")
    assert load_preview_for(changed).kind == KIND_HTML, "a file's history row is a commit too"
    removed = history_row(kind="content", path="src/Order.cs", status="-", text="var x;")
    assert load_preview_for(removed).kind == KIND_HTML, "and so is a line a commit removed"


def test_a_file_in_the_checkout_is_still_read_from_disk(monkeypatch, tmp_path) -> None:
    from app.search import gitsearch

    monkeypatch.setattr(gitsearch, "show_commit", lambda *_a: pytest.fail("git was started"))
    source = tmp_path / "a.py"
    source.write_text("CustomerId = 1\n", encoding="utf-8")
    hit = git_result_row(GitRow(kind="content", path="a.py", line_no=1, text="CustomerId = 1",
                                repo="leasha", root=str(tmp_path)), "", "CustomerId")
    preview = load_preview_for(hit)
    assert preview.kind == KIND_TEXT and "CustomerId = 1" in preview.body


def test_git_show_is_only_ever_run_by_the_loader() -> None:
    """Only when selected, and on a worker: the window's own modules never call it.

    `preview_loader` is a worker-only module (`test_ui_never_blocks.WORKER_ONLY`),
    and `commit_preview` is reached only from it."""
    ui = Path(module.__file__).parent
    callers = []
    for path in sorted(ui.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", getattr(node.func, "attr", ""))
                if name in ("commit_preview", "show_commit"):
                    callers.append(path.name)
    assert sorted(set(callers)) == ["preview_loader.py"], callers
    assert "PySide6" not in inspect.getsource(module)


# --- in the pane ---------------------------------------------------------------------------

pytest.importorskip("PySide6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_the_pane_draws_the_commit(qapp, monkeypatch) -> None:
    from app.search import gitsearch
    from app.ui.widgets import preview as preview_module
    from app.ui.widgets.preview import PreviewPane

    monkeypatch.setattr(gitsearch, "show_commit", lambda repo, sha: DETAIL)
    monkeypatch.setattr(preview_module, "run", lambda _pool, worker: worker.run())
    pane = PreviewPane()
    started: list = []
    real_start = pane._start
    pane._start = lambda: (started.append(1), real_start())

    pane.show_row(history_row())
    assert started == [], "git must wait for the pane's pause, not run on every arrow key"
    assert pane.title.text() == "Rename CustomerId <at last>"
    pane._timer.stop()
    pane._start()

    text = pane.text.toPlainText()
    for piece in ("Rename CustomerId <at last>", "Because CustomerId was misleading.",
                  "<script>alert(1)</script>", "Ada Lovelace <ada@example.com>",
                  "Tuesday 2 January 2024, 09:00", "3 files changed", "src/Moved.cs (was src/Old.cs)",
                  "-    var CustomerId = 1;  // <old>", "+    var ClientId = 1;"):
        assert piece in text, piece
    assert "ffe066" in pane.text.toHtml().lower(), "the searched text is not highlighted"
    assert pane.subtitle.text().startswith("Commit abc12340")
    assert pane.stack.currentWidget() is pane.text
