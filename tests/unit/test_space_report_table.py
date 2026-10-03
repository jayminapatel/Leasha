r"""Order 202626270602 (0n) section 3a - the Space Report as a table.

Layer: L5

"Table + a few plain numbers; sortable; row -> reveals the copies with their
sources." Three levels, cheapest first: the row shaping as plain data (no Qt),
the widget on its own, and the real `MainWindow` - open Reports, pick the
Space Report, click a heading, open a row, Export - because a widget that
passes alone is exactly how a feature ships unreachable (`test_wired_features`).
"""

from __future__ import annotations

import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from app.reports.space import (  # noqa: E402
    DuplicateCopy,
    DuplicateGroup,
    NearDuplicatePhotoGroup,
    SourceDuplicateShare,
    SourceUniqueness,
    SpaceDocument,
    SpaceFindings,
    document_for,
)
from app.ui.presenter.space_rows import space_headline, space_tables  # noqa: E402

KB = 1024


def _local(path):
    return DuplicateCopy(path=path, source_name="This computer", source_kind="local")


def _volume(path, name="Old WD", status="offline"):
    return DuplicateCopy(
        path=f"leasha-volume://7/{path}", source_name=name, source_kind="drive",
        source_status=status)


def _findings(**overrides):
    base = dict(
        groups=(
            DuplicateGroup("h-zeta", 3 * KB, (
                _local(r"D:\Docs\zeta.bin"), _local(r"D:\Backup\zeta.bin"),
                _volume("old/zeta.bin"))),
            DuplicateGroup("h-alpha", 10 * KB, (
                _local(r"D:\Docs\alpha.bin"), _volume("alpha.bin"))),
        ),
        near_duplicates=(NearDuplicatePhotoGroup(
            "ffff", (_local(r"D:\Photos\a.jpg"), _volume("a-small.jpg")), (9 * KB, 2 * KB)),),
        duplicate_share=(
            SourceDuplicateShare("Old WD", "drive", "offline", 4, 100),
            SourceDuplicateShare("NAS", "network", "online", 35, 100),
            SourceDuplicateShare("This computer", "local", "", 1, 10),
        ),
        uniqueness=(
            SourceUniqueness("Old WD", "drive", "offline", 1_700_000_000, 372),
            SourceUniqueness("This computer", "local", "", None, 9),
        ),
        total_reclaimable=16 * KB, generated_at=1_700_000_000)
    base.update(overrides)
    return SpaceFindings(**base)


# ---------------------------------------------------------------------------
# The row shaping - plain data
# ---------------------------------------------------------------------------

def _table(findings, key):
    return {t.key: t for t in space_tables(findings)}[key]


def test_every_row_sorts_on_a_value_that_lines_up_with_its_cells():
    for table in space_tables(_findings()):
        assert len(table.headers) == len(table.aligns)
        for row in table.rows:
            assert len(row.cells) == len(row.sort) == len(table.headers), table.key
            for child in row.children:
                assert len(child.cells) == len(child.sort) == len(table.headers), table.key


def test_duplicate_rows_sort_on_bytes_and_counts_not_on_their_words():
    table = _table(_findings(), "duplicates")
    by_name = {row.cells[0]: row for row in table.rows}
    zeta, alpha = by_name["zeta.bin"], by_name["alpha.bin"]
    assert (zeta.sort[1], zeta.sort[2], zeta.sort[3]) == (3, 3 * KB, 6 * KB)
    assert (alpha.sort[1], alpha.sort[2], alpha.sort[3]) == (2, 10 * KB, 10 * KB)
    # As text, "10.0 KB" sorts before "3.0 KB" - the failure `SORT_ROLE` exists for.
    assert alpha.cells[2] < zeta.cells[2] and alpha.sort[2] > zeta.sort[2]


def test_a_duplicate_row_reveals_each_copy_with_its_source():
    zeta = next(r for r in _table(_findings(), "duplicates").rows if r.cells[0] == "zeta.bin")
    assert len(zeta.children) == 3
    places = [child.cells[-1] for child in zeta.children]
    assert places == ["This computer", "This computer", "Old WD (offline)"]
    # A local copy shows its real path; a volume copy its path *on that volume*,
    # never the internal `leasha-volume://` address.
    assert zeta.children[0].cells[0] == r"D:\Docs\zeta.bin"
    assert zeta.children[2].cells[0] == "old/zeta.bin"
    assert zeta.cells[-1] == "This computer, Old WD (offline)"


def test_similar_photos_are_never_given_a_space_you_would_get_back():
    """`NearDuplicatePhotoGroup` carries no reclaimable bytes on purpose."""
    table = _table(_findings(), "similar")
    assert not any("back" in h.lower() or "reclaim" in h.lower() or "free" in h.lower()
                   for h in table.headers)
    row = table.rows[0]
    assert row.cells[:2] == ("a.jpg", "2")
    assert row.sort[2] == 9 * KB, "the largest version, shown - never summed"
    assert [c.sort[2] for c in row.children] == [9 * KB, 2 * KB]
    assert "not identical" in row.tooltip


def test_by_source_and_the_only_copy_are_flat_and_sortable_on_numbers():
    share = _table(_findings(), "by-source")
    assert all(not row.children for row in share.rows)
    nas = next(r for r in share.rows if r.cells[0] == "NAS")
    old = next(r for r in share.rows if r.cells[0].startswith("Old WD"))
    assert old.cells[0] == "Old WD (offline)"
    assert nas.cells[3] == "35%" and nas.sort[3] == 0.35
    assert nas.cells[3] < old.cells[3] or nas.sort[3] > old.sort[3]   # "35%" < "4%" as text
    only = _table(_findings(), "only-copy")
    first = only.rows[0]
    assert first.cells[:3] == ("Old WD", "372", "Offline")
    assert first.sort[1] == 372 and first.sort[3] == 1_700_000_000
    assert only.sort_column == -1, "opens in the report's order: drives first"


def test_a_table_with_nothing_in_it_says_so_in_plain_words():
    for table in space_tables(SpaceFindings()):
        assert not table.rows and table.empty.endswith(".")
    assert space_headline(SpaceFindings()) == "No duplicate files were found."


def test_the_headline_gives_the_plain_numbers_in_the_documents_own_words():
    headline = space_headline(_findings())
    assert "would free 16.0 KB" in headline
    assert "372 files exist nowhere else but Old WD, currently offline (last seen" in headline


def test_the_document_and_the_table_come_from_the_same_findings():
    findings = _findings()
    document = document_for(findings)
    assert isinstance(document, str) and isinstance(document, SpaceDocument)
    assert document.findings is findings
    assert document.startswith("# The Space Report")


# ---------------------------------------------------------------------------
# The widget on its own
# ---------------------------------------------------------------------------

pytest.importorskip("PyQt6.QtWidgets")

from PyQt6.QtCore import QPoint, Qt  # noqa: E402
from app.ui.widgets.timeline_host import REPORT_KEY  # noqa: E402 - the list's key role

from app.ui.widgets.sortable_item import SORT_ROLE  # noqa: E402
from app.ui.widgets.space_table import SpaceTables  # noqa: E402


def _column(tree, column):
    return [tree.topLevelItem(i).text(column) for i in range(tree.topLevelItemCount())]


def test_the_widget_shows_a_tab_per_question_with_names_and_tooltips(qtbot):
    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(_findings())
    titles = [widget.tabs.tabText(i) for i in range(widget.tabs.count())]
    assert titles == ["Duplicates", "Similar photos", "By source", "The only copy"] or \
        titles == ["Duplicates", "Similar photos", "By source", "The only copy"]
    assert all(widget.tabs.tabToolTip(i) for i in range(widget.tabs.count()))
    assert widget.tabs.accessibleName() and widget.tabs.toolTip()
    for tree in widget.trees.values():
        assert tree.accessibleName() and tree.toolTip()


def test_rows_open_by_default_collapsed_and_sort_on_real_values(qtbot):
    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(_findings())
    tree = widget.trees["duplicates"]

    # Opens biggest saving first, every row collapsed.
    assert _column(tree, 0) == ["alpha.bin", "zeta.bin"]
    assert not tree.topLevelItem(0).isExpanded()

    tree.sortByColumn(2, Qt.SortOrder.AscendingOrder)          # "Size of each"
    assert _column(tree, 0) == ["zeta.bin", "alpha.bin"], "3 KB before 10 KB, not 10 before 3"
    tree.sortByColumn(2, Qt.SortOrder.DescendingOrder)
    assert _column(tree, 0) == ["alpha.bin", "zeta.bin"]

    tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)          # "Copies"
    assert _column(tree, 0) == ["zeta.bin", "alpha.bin"]

    share = widget.trees["by-source"]
    share.sortByColumn(3, Qt.SortOrder.AscendingOrder)
    assert [share.topLevelItem(i).data(3, SORT_ROLE) for i in range(3)] == [0.04, 0.1, 0.35]
    assert _column(share, 3) == ["4%", "10%", "35%"]


def test_open_rows_and_the_chosen_sort_survive_showing_the_same_findings_again(qtbot):
    widget = SpaceTables()
    qtbot.addWidget(widget)
    findings = _findings()
    widget.set_findings(findings)
    tree = widget.trees["duplicates"]
    tree.sortByColumn(1, Qt.SortOrder.AscendingOrder)
    tree.expandItem(tree.topLevelItem(0))
    widget.set_findings(findings)
    assert widget.trees["duplicates"] is tree
    assert tree.topLevelItem(0).isExpanded()


def test_the_copies_inside_a_row_are_made_when_it_is_opened_and_only_once(qtbot):
    """2026-10-02. Every copy of every group used to be given a row when the
    table was built, on the window's thread: 23 ms per thousand copies, and
    1,356 ms of a window that did not answer on the owner's index of mail."""
    from PyQt6.QtWidgets import QTreeWidgetItem

    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(_findings())
    tree = widget.trees["duplicates"]
    zeta = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0) == "zeta.bin")

    assert zeta.childCount() == 0, "the copies were built before anybody asked"
    assert zeta.childIndicatorPolicy() == \
        QTreeWidgetItem.ChildIndicatorPolicy.ShowIndicator, "no arrow to open it with"

    tree.expandItem(zeta)
    assert zeta.childCount() == 3
    assert sorted(zeta.child(i).text(4) for i in range(3)) == \
        ["Old WD (offline)", "This computer", "This computer"]
    assert zeta.child(0).toolTip(0), "a copy lost its tooltip"

    tree.collapseItem(zeta)
    tree.expandItem(zeta)
    assert zeta.childCount() == 3, "opening it twice made the copies twice"


def test_a_group_with_thousands_of_copies_costs_nothing_until_it_is_opened(qtbot):
    """Counted, not timed: a number of rows is the same on every machine."""
    big = tuple(DuplicateGroup(f"h{g}", 40 * KB, tuple(
        _local(rf"D:\Mail\{g}\{c}\image001.png") for c in range(4000))) for g in range(25))
    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(_findings(groups=big))
    tree = widget.trees["duplicates"]

    assert tree.topLevelItemCount() == 25
    assert sum(tree.topLevelItem(i).childCount() for i in range(25)) == 0
    assert tree.topLevelItem(0).text(1) == "4,000", "the count is still on the row"

    tree.expandItem(tree.topLevelItem(0))
    assert tree.topLevelItem(0).childCount() == 4000
    assert sum(tree.topLevelItem(i).childCount() for i in range(25)) == 4000


def test_rows_shaped_on_the_worker_are_used_and_not_shaped_again(qtbot, monkeypatch):
    """2026-10-03. Shaping the rows - one per group and per copy - was the last
    of the Space Report's work still done on the window's thread (about
    170 ms for 100,000 copies). `presenter.space_rows.shape` does it on the
    worker and the widget draws from that."""
    from app.reports.space import document_for
    from app.ui.presenter.space_rows import shape
    import app.ui.widgets.space_table as module

    document = shape(document_for(_findings()))
    assert document.shaped and document.shaped[0] == space_headline(document.findings)
    assert [t.key for t in document.shaped[1]] == ["duplicates", "similar", "by-source", "only-copy"]

    def _not_here(_findings):
        raise AssertionError("the rows were shaped on the window's thread")

    monkeypatch.setattr(module, "space_tables", _not_here)
    monkeypatch.setattr(module, "space_headline", _not_here)
    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(document.findings, document.shaped)
    assert widget.headline.text() == document.shaped[0]
    assert set(widget.trees) == {"duplicates", "similar", "by-source", "only-copy"}


def test_a_document_that_is_only_words_is_left_alone_by_shape():
    from app.ui.presenter.space_rows import shape

    assert shape("just a document") == "just a document"


def test_a_flat_table_has_no_arrows(qtbot):
    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(_findings())
    share = widget.trees["by-source"]
    assert not share.rootIsDecorated()
    assert all(share.topLevelItem(i).childCount() == 0
               for i in range(share.topLevelItemCount()))


def test_the_only_copy_keeps_the_reports_order_until_somebody_sorts(qtbot):
    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(_findings(uniqueness=(
        SourceUniqueness("Old WD", "drive", "offline", 1, 5),
        SourceUniqueness("Holiday SD", "drive", "offline", 1, 900),
        SourceUniqueness("This computer", "local", "", None, 7),
    )))
    tree = widget.trees["only-copy"]
    assert _column(tree, 0) == ["Old WD", "Holiday SD", "This computer"]
    tree.sortByColumn(1, Qt.SortOrder.DescendingOrder)
    assert _column(tree, 0) == ["Holiday SD", "This computer", "Old WD"]


def test_an_empty_section_shows_its_sentence_instead_of_an_empty_table(qtbot):
    widget = SpaceTables()
    qtbot.addWidget(widget)
    widget.set_findings(SpaceFindings())
    assert widget.trees == {}
    assert widget.headline.text() == "No duplicate files were found."


# ---------------------------------------------------------------------------
# The real window: Reports -> Space Report -> sort -> open a row -> Export
# ---------------------------------------------------------------------------

from tests.unit.conftest import gui_pump  # noqa: E402


@pytest.fixture
def space_window(gui_mainwindow, qtbot):
    """The GUI window with a planted duplicate structure, Reports shown and the
    Space Report picked, loaded by the real worker."""
    app, window, store, _engine = gui_mainwindow
    volume = store.upsert_volume("gui-old-wd", kind="drive", name="Old WD", status="OFFLINE")
    for path, size, content_hash, on_volume in (
        ("C:/space/zeta.bin", 3 * KB, "gz", False), ("C:/space/backup/zeta.bin", 3 * KB, "gz", False),
        ("zeta-old.bin", 3 * KB, "gz", True),
        ("C:/space/alpha.bin", 10 * KB, "ga", False), ("alpha-old.bin", 10 * KB, "ga", True),
    ):
        if on_volume:
            store.upsert_file(
                f"leasha-volume://{volume}/{path}", size_bytes=size, mtime_ns=1, ext="bin",
                parent_dir="p", source_kind="file", status="INDEXED",
                content_hash=content_hash, volume_id=volume, relative_path=path)
        else:
            store.upsert_file(
                path, size_bytes=size, mtime_ns=1, ext="bin", parent_dir="C:/space",
                source_kind="file", status="INDEXED", content_hash=content_hash)
    with store.write() as conn:
        conn.execute("UPDATE files SET indexed_at = 1700000000 + id WHERE indexed_at IS NULL")

    view = window.reports_view
    view._space_cached_at = None
    view._space_document = ""
    window.resize(1100, 700)
    window.show()
    view.list.setCurrentRow([view.list.item(i).data(REPORT_KEY) for i in range(view.list.count())]
                            .index("space"))
    window._show(view)
    view.refresh()                     # the tab switch does this too - explicit, so a
    #                                    view that was already the current tab reloads
    qtbot.waitUntil(lambda: bool(view._space_document), timeout=15000)
    gui_pump(app)
    return app, window, view


@pytest.mark.gui
def test_the_space_report_appears_as_a_table_not_a_document(space_window):
    _app, _window, view = space_window
    assert not view.space_table.isHidden() and view.body.isHidden()
    tree = view.space_table.trees["duplicates"]
    assert [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())] \
        == ["alpha.bin", "zeta.bin"]
    assert "would free" in view.space_table.headline.text()
    view.list.setCurrentRow(0)                       # Digital Inheritance is still a document
    assert view.space_table.isHidden() and not view.body.isHidden()
    view.list.setCurrentRow(1)
    assert not view.space_table.isHidden()


@pytest.mark.gui
def test_clicking_a_heading_sorts_by_the_real_value(space_window, qtbot):
    app, _window, view = space_window
    tree = view.space_table.trees["duplicates"]
    header = tree.header()
    size_column = 2                                   # "Size of each"

    def click_heading():
        x = header.sectionViewportPosition(size_column) + header.sectionSize(size_column) // 2
        qtbot.mouseClick(header.viewport(), Qt.MouseButton.LeftButton,
                         pos=QPoint(x, header.height() // 2))
        gui_pump(app)

    click_heading()
    assert header.sortIndicatorSection() == size_column
    names = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    ascending = header.sortIndicatorOrder() == Qt.SortOrder.AscendingOrder
    assert names == (["zeta.bin", "alpha.bin"] if ascending else ["alpha.bin", "zeta.bin"])
    click_heading()
    flipped = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert flipped == names[::-1]


@pytest.mark.gui
def test_opening_a_row_reveals_every_copy_and_its_source(space_window, qtbot):
    app, _window, view = space_window
    tree = view.space_table.trees["duplicates"]
    zeta = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0) == "zeta.bin")
    # 2026-10-02: the copies are made when the row is opened (`space_table._tree`),
    # so the count of three is asserted after the click, where it was before it.
    assert not zeta.isExpanded()
    # The arrow in the margin left of the row - the way a person opens it.
    rect = tree.visualItemRect(zeta)
    qtbot.mouseClick(tree.viewport(), Qt.MouseButton.LeftButton,
                     pos=QPoint(rect.left() - tree.indentation() // 2, rect.center().y()))
    gui_pump(app)
    assert zeta.isExpanded() and zeta.childCount() == 3
    places = sorted(zeta.child(i).text(4) for i in range(zeta.childCount()))
    assert places == ["Old WD (offline)", "This computer", "This computer"]
    assert any(zeta.child(i).text(0) == "zeta-old.bin" for i in range(3))


@pytest.mark.gui
def test_a_second_load_asked_for_while_one_is_running_does_not_rebuild_the_table(
        space_window, qtbot, monkeypatch):
    r"""2026-10-02. The test above failed now and then with "wrapped C/C++
    object of type SortableTreeItem has been deleted", and it was right to.

    `ReportsView.refresh` started a full load every time it was called, each
    carrying the "data as of" stamp from *before* any of them finished - so two
    calls close together (this module's own fixture makes two; a person makes
    them by leaving Reports and coming straight back) ran the Space Report's
    queries twice, and the second answer, equal to the first but a new object,
    rebuilt the table under whoever had a row open. Made to happen on purpose
    here: the second load is held back until the first is on screen and a row
    is in hand.
    """
    import threading
    import time

    from PyQt6.QtCore import QThreadPool

    import app.ui.reports_view as reports_view

    app, _window, view = space_window
    QThreadPool.globalInstance().waitForDone(15_000)     # nothing of the fixture's left
    gui_pump(app)

    loads: list = []
    lock = threading.Lock()
    real = reports_view._report_snapshot

    def recorded(store, last_known=None, on_progress=None):
        with lock:
            n = len(loads)
            loads.append(last_known)
        out = real(store, last_known, on_progress)
        if n and out is not None:
            time.sleep(0.5)             # a second full load lands late
        return out

    monkeypatch.setattr(reports_view, "_report_snapshot", recorded)

    view._space_cached_at = None        # as after an index run: the data has moved
    view._space_document = ""
    view.refresh()
    view.refresh()                      # ...and Reports is asked again straight away
    qtbot.waitUntil(lambda: bool(view._space_document), timeout=15000)
    gui_pump(app)

    tree = view.space_table.trees["duplicates"]
    zeta = next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                if tree.topLevelItem(i).text(0) == "zeta.bin")
    tree.expandItem(zeta)

    qtbot.waitUntil(lambda: len(loads) >= 2, timeout=15000)
    QThreadPool.globalInstance().waitForDone(15_000)
    gui_pump(app)

    assert loads[0] is None
    assert loads[1] is not None, (
        "a second full load ran alongside the first, with the stamp from "
        "before either had finished")
    assert view.space_table.trees["duplicates"] is tree, "the table was rebuilt"
    assert zeta.isExpanded() and zeta.childCount() == 3, "the opened row was lost"


@pytest.mark.gui
def test_export_still_writes_the_same_document_as_a_pdf(space_window, qtbot, tmp_path, monkeypatch):
    from PyQt6.QtCore import QThreadPool
    from PyQt6.QtWidgets import QFileDialog

    import app.ui.reports_view as reports_view

    app, _window, view = space_window
    assert view.export.isEnabled()
    target = tmp_path / "space-report.pdf"
    monkeypatch.setattr(QFileDialog, "getSaveFileName",
                        staticmethod(lambda *a, **k: (str(target), "PDF files (*.pdf)")))
    written: list = []
    real_write = reports_view._write_pdf
    monkeypatch.setattr(reports_view, "_write_pdf",
                        lambda document, path: (written.append(str(document)), real_write(document, path)))

    qtbot.mouseClick(view.export, Qt.MouseButton.LeftButton)
    QThreadPool.globalInstance().waitForDone(15_000)
    gui_pump(app)

    assert target.is_file() and target.read_bytes()[:4] == b"%PDF"
    assert len(written) == 1
    document = written[0]
    assert document == str(view._space_document)
    assert document.startswith("# The Space Report") and "alpha.bin" in document
    assert view._space_document.findings.groups, "the table and the PDF share one set of findings"
