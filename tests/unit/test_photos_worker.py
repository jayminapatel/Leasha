r"""The Photos tab's filtering runs on a worker, not on the window's thread.

2026-10-09: the window stopped answering while the photo library (46,000
pictures) was narrowed, sorted and counted on its own thread after every
search. These tests hold that the work now lives in worker functions that
return the same answers, and that the window's draw path does none of it.
"""

from __future__ import annotations

import datetime as dt
import inspect
from types import SimpleNamespace

from app.search.query import parse_query
from app.storage.sqlite_store import PhotoRow
from app.ui import photos_view
from app.ui.presenter import photos as pp


def _row(n: int, *, people=(), place=None, when=dt.datetime(2022, 6, 1, 12), mail=False):
    path = f"D:/Photos/p{n}.jpg" if not mail else f"pst://2024/{n}/attachment 1/p{n}.jpg"
    return PhotoRow(
        file_id=n, path=path, ext="jpg", size_bytes=1000 + n, mtime_ns=0,
        taken_at_ns=int(when.timestamp() * 1_000_000_000) if when else None,
        taken_is_hint=False, place=place, people=tuple(people), faces=len(people),
        described=False, has_text=False, page_like=False, status="indexed")


def test_arrange_gives_the_same_pictures_and_counts_as_the_old_window_code() -> None:
    rows = [_row(1, people=("Sarita",), place="Plano"),
            _row(2, when=dt.datetime(2019, 3, 3, 9)),
            _row(3, people=("Jason",), place="Plano"),
            _row(4, mail=True)]
    parsed = parse_query("")
    shown, order, shown_count, scope = photos_view._arrange(rows, parsed, "", "newest")

    # what `_show` computed on the window's thread before 2026-10-09
    expected_shown = pp.sort_rows(pp.narrow(rows, parsed, ""), order)
    assert [r.file_id for r in shown] == [r.file_id for r in expected_shown]
    assert shown_count == len(expected_shown)
    assert scope == pp.in_scope(rows, parsed)


def test_arrange_honours_a_person_in_the_box() -> None:
    rows = [_row(1, people=("Sarita",)), _row(2, people=("Jason",)), _row(3)]
    parsed = parse_query("who:Sarita")
    shown, _order, shown_count, _scope = photos_view._arrange(rows, parsed, "", "newest")
    assert [r.file_id for r in shown] == [1] and shown_count == 1


def test_arrange_uses_the_sort_the_box_asks_for() -> None:
    rows = [_row(1, when=dt.datetime(2020, 1, 1)), _row(2, when=dt.datetime(2024, 1, 1))]
    parsed = SimpleNamespace(sort="oldest")
    shown, order, _count, _scope = photos_view._arrange(rows, parsed, "", "newest")
    assert order == "oldest" and [r.file_id for r in shown] == [1, 2]


def test_library_returns_the_side_counts_so_the_window_does_not_compute_them() -> None:
    rows = [_row(1, people=("Sarita",)), _row(2, people=("Sarita",))]

    class _Store:
        def photo_library(self, _extensions):
            return rows

        def suggestion_counts(self):
            return [("pile", "Sarita", 3)]

    library_rows, waiting, counts = photos_view._library(_Store())
    assert library_rows == rows
    assert waiting == 3
    assert counts == pp.facets(rows)


def test_the_window_thread_does_no_narrowing_or_counting_on_a_search() -> None:
    """The draw path in `_show` must hand the work to a worker. A guard, so the
    expensive calls cannot quietly come back to the window's thread."""
    source = inspect.getsource(photos_view.PhotosView._show)
    for call in ("narrow(", "in_scope(", "sort_rows(", "facets("):
        assert call not in source, f"{call} is back on the window's thread"
    drawn = inspect.getsource(photos_view.PhotosView._drawn)
    for call in ("narrow(", "in_scope(", "sort_rows("):
        assert call not in drawn
