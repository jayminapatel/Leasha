r"""Order 0y §2: search inside the code, as you type.

Layer: L4 / L5

* **2a** - definitions first, then files whose name matched, then mentions;
  only code (files in a repository) is searched.
* **2b** - the line number is the line in the *file*, not in the indexed text
  (which folds blank lines), and a line the file no longer holds has none.
* **2d** - the summary counts each kind.
* The typing budget: one keystroke's query well inside 40 ms on a store of
  thousands of passages.
"""

from __future__ import annotations

import os
import statistics
import time
from pathlib import Path

import pytest

from app.extract import chunk_document, extract
from app.search.code_search import (
    KIND_DEFINITION,
    KIND_MENTION,
    CodeMatch,
    code_matches,
    resolve_line,
    resolve_lines,
)
from app.search.commands import expand_slashes
from app.search.query import parse_query
from app.storage.sqlite_store import FileStatus, SqliteStore
from app.ui.presenter import code_list, code_match_rows, match_counts, repo_file_rows

SERVICE = '''"""Orders."""


import os



class OrderService:
    """Places orders."""

    def place(self, order):
        return OrderService.check(order)


def check_order(order):
    service = OrderService()
    return service
'''


def _index(store: SqliteStore, path: Path, *, repo_id=None) -> None:
    document = next(iter(extract(path)))
    file_id = store.upsert_file(
        str(path), size_bytes=path.stat().st_size, mtime_ns=1, ext=path.suffix.lstrip("."),
        status=FileStatus.INDEXED, source_kind="file", repo_id=repo_id)
    store.replace_chunks(file_id, [
        {"ordinal": n, "text": c.text, "page": c.page, "char_start": c.char_start,
         "char_end": c.char_end, "label": c.label}
        for n, c in enumerate(chunk_document(document))])


@pytest.fixture()
def store(tmp_path):
    repo = tmp_path / "shop"
    repo.mkdir()
    (repo / "service.py").write_text(SERVICE, encoding="utf-8")
    (repo / "orderservice_notes.txt").write_text("nothing about it here", encoding="utf-8")
    loose = tmp_path / "loose"
    loose.mkdir()
    (loose / "letter.txt").write_text("Dear OrderService team", encoding="utf-8")
    with SqliteStore(tmp_path / "index.db") as s:
        repo_id = s.upsert_repo(str(repo), name="shop", kind="work")
        _index(s, repo / "service.py", repo_id=repo_id)
        _index(s, loose / "letter.txt")                   # not in a repository
        s.repo = repo
        yield s


def _matches(store, text):
    return code_matches(store, parse_query(expand_slashes(text)))


def test_the_definition_comes_first_then_mentions(store) -> None:
    found = _matches(store, "OrderService")
    assert found and found[0].kind == KIND_DEFINITION
    assert found[0].text.strip() == "class OrderService:"
    assert {m.kind for m in found[1:]} == {KIND_MENTION}
    assert all(Path(m.path).name == "service.py" for m in found), (
        "a file outside every repository is not code")


def test_a_function_definition_is_found_by_part_of_its_name(store) -> None:
    found = _matches(store, "check_order")
    assert found[0].kind == KIND_DEFINITION and "def check_order" in found[0].text


def test_line_numbers_are_the_files_own_despite_folded_blank_lines(store) -> None:
    found = resolve_lines(_matches(store, "OrderService"))
    lines = SERVICE.split("\n")
    for match in found:
        assert match.line is not None
        assert lines[match.line - 1].strip() == match.text.strip()
    assert found[0].line == 8          # three blank lines above it, folded by the index


def test_a_line_the_file_no_longer_holds_has_no_number(store, tmp_path) -> None:
    path = store.repo / "service.py"
    path.write_text("# rewritten\n", encoding="utf-8")
    assert resolve_line(str(path), "class OrderService:", 0) is None
    assert resolve_line(str(tmp_path / "gone.py"), "anything", 0) is None


def test_line_numbers_stop_when_the_time_budget_is_spent(store) -> None:
    found = _matches(store, "OrderService")
    ticks = iter([0.0] + [10.0] * 50)
    resolved = resolve_lines(found, budget_s=1.0, clock=lambda: next(ticks))
    assert [m.line for m in resolved] == [None] * len(found)
    assert [m.text for m in resolved] == [m.text for m in found], "order never changes"


def test_nothing_to_look_for_is_no_rows(store) -> None:
    assert _matches(store, "/type py") == []


# --- the list and its summary -------------------------------------------------

REPOS = [{"name": "shop", "root_path": "D:/work/shop"},
         {"name": "inner", "root_path": "D:/work/shop/vendor/inner"}]


def _match(kind, path, line=None, text="x"):
    return CodeMatch(kind=kind, path=path, text=text, at=0, line=line, ext="py")


def test_rows_are_definitions_then_files_then_mentions() -> None:
    matches = code_match_rows([
        _match(KIND_MENTION, "D:/work/shop/a.py", 12, "use(OrderService)"),
        _match(KIND_DEFINITION, "D:/work/shop/vendor/inner/b.py", 3, "class OrderService:"),
    ], REPOS)
    files = repo_file_rows([{"path": "D:/work/shop/order_service.py", "repo": "shop"}])
    rows = code_list(files, matches)
    assert [r.match for r in rows] == ["Definition", "File name", "Mention"]
    assert rows[0].repo == "inner", "the deepest repository holding the file"
    assert rows[0].line == "3" and rows[0].code == "class OrderService:"
    assert match_counts(rows) == "1 definition  ·  1 file  ·  1 mention"


def test_with_nothing_typed_the_list_is_unchanged() -> None:
    files = repo_file_rows([{"path": "D:/work/shop/a.py", "repo": "shop"}])
    assert code_list(files, []) == files
    assert match_counts(files) == ""


# --- the typing budget ------------------------------------------------------------

def test_one_keystroke_stays_inside_the_typing_budget(tmp_path) -> None:
    repo = tmp_path / "big"
    repo.mkdir()
    with SqliteStore(tmp_path / "big.db") as s:
        repo_id = s.upsert_repo(str(repo), name="big", kind="work")
        words = ["alpha", "beta", "gamma", "delta", "render", "invoice", "customer"]
        for number in range(1000):
            path = repo / f"module_{number}.py"
            body = "\n".join(
                f"def {words[(number + k) % 7]}_{k}(value):\n    return value + {k}"
                for k in range(60))
            if number % 50 == 0:
                body += "\nclass CustomerLedger:\n    pass\n"
            path.write_text(body, encoding="utf-8")
            _index(s, path, repo_id=repo_id)
        passages = s.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        parsed = parse_query(expand_slashes("CustomerLedger"))
        code_matches(s, parsed)                                  # warm
        timings = []
        for _ in range(5):
            started = time.perf_counter()
            found = code_matches(s, parsed)
            timings.append(time.perf_counter() - started)
    assert passages >= 2000
    assert found[0].kind == KIND_DEFINITION
    assert statistics.median(timings) < 0.040, (passages, timings)


# --- the window -------------------------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets", exc_type=ImportError)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from app.ui.code_view import CodeView  # noqa: E402
from tests.unit.test_code_view import FakeStore  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    yield QApplication.instance() or QApplication([])


def test_the_code_tab_lists_the_definition_first_with_its_line(qapp, monkeypatch) -> None:
    monkeypatch.setattr("app.ui.code_view.run", lambda _pool, worker: worker.run())
    monkeypatch.setattr("app.ui.tasks.code_content_matches", lambda *_a, **_k: [
        _match(KIND_DEFINITION, "D:/SearchProject/app/cli.py", 42, "class CustomerId:"),
        _match(KIND_MENTION, "D:/SearchProject/app/other.py", 7, "x = CustomerId()"),
    ])
    view = CodeView(FakeStore())
    view.show()
    view._repos_read(view._store.repos_list())
    view.input.setText("CustomerId")
    view._typed()

    table = view.results.table
    headings = [table.horizontalHeaderItem(c).text() for c in range(table.columnCount())]
    first = [table.item(0, c).text() for c in range(table.columnCount())]
    assert first[headings.index("Match")] == "Definition"
    assert first[headings.index("Line")] == "42"
    assert first[headings.index("Code")] == "class CustomerId:"
    assert not table.isColumnHidden(headings.index("Code"))
    assert view.summary.text().startswith("1 definition")
