r"""The gathered working set itself, apart from any panel. Workspace §3c.

Layer: L5 presenter — Qt-free (see `app/ui/pinned.py`), moved here unchanged
from `docs/_superseded/pinned.py` after checking it against the current
`ResultRow`/`ResultGroup` shapes.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.ui.pinned import LIMIT, Pin, add, decode, encode, paths, remove, summary


def row(path: str, name: str = ""):
    return SimpleNamespace(path=path, name=name)


def test_add_gathers_a_row_newest_last():
    found = add(add((), row(r"D:\a.pdf")), row(r"D:\b.pdf"))
    assert found == (Pin(r"D:\a.pdf"), Pin(r"D:\b.pdf"))


def test_adding_the_same_path_twice_is_not_an_error_or_a_duplicate():
    once = add((), row(r"D:\a.pdf", "A"))
    twice = add(once, row(r"D:\a.pdf", "A"))
    assert twice == once


def test_add_never_raises_on_a_pathless_item():
    assert add((), SimpleNamespace()) == ()
    assert add((), None) == ()


def test_add_respects_the_limit():
    found: tuple = ()
    for i in range(LIMIT + 5):
        found = add(found, row(f"D:\\file{i}.pdf"), limit=LIMIT)
    assert len(found) == LIMIT
    # the fifty-first is dropped, not swapped for an earlier one
    assert found[0].path == "D:\\file0.pdf"


def test_remove_takes_one_path_out():
    found = add(add((), row(r"D:\a.pdf")), row(r"D:\b.pdf"))
    assert remove(found, r"D:\a.pdf") == (Pin(r"D:\b.pdf"),)


def test_remove_of_a_path_not_present_changes_nothing():
    found = add((), row(r"D:\a.pdf"))
    assert remove(found, r"D:\nope.pdf") == found


def test_encode_decode_round_trips_through_json():
    found = add(add((), row(r"D:\a.pdf", "A")), row(r"D:\b.pdf"))
    restored = decode(encode(found))
    assert restored == found


def test_decode_never_raises_on_garbage():
    assert decode("not json") == ()
    assert decode(None) == ()
    assert decode(12345) == ()
    assert decode([{"path": ""}, {"nope": "x"}, {"path": r"D:\ok.pdf"}]) == (Pin(r"D:\ok.pdf"),)


def test_decode_is_the_identity_on_its_own_output():
    """The panel hands its own state back in when it redraws."""
    found = add((), row(r"D:\a.pdf"))
    assert decode(found) == found


def test_paths_is_every_path_in_order():
    found = add(add((), row(r"D:\a.pdf")), row(r"D:\b.pdf"))
    assert paths(found) == (r"D:\a.pdf", r"D:\b.pdf")


def test_summary_of_nothing_says_nothing():
    assert summary(()) == ""


def test_summary_counts_with_correct_pluralisation():
    assert summary(add((), row(r"D:\a.pdf"))) == "1 document"
    assert summary(add(add((), row(r"D:\a.pdf")), row(r"D:\b.pdf"))) == "2 documents"


def test_summary_says_so_at_the_limit():
    found: tuple = ()
    for i in range(3):
        found = add(found, row(f"D:\\f{i}.pdf"), limit=3)
    assert "as many as this can hold" in summary(found, limit=3)


def test_pin_label_falls_back_to_the_filename():
    assert Pin(r"D:\Archive\report.pdf").label == "report.pdf"
    assert Pin(r"D:\Archive\report.pdf", "Q3 Report").label == "Q3 Report"
