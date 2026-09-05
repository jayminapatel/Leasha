r"""What may be dragged out of the results list. Workspace §3b.

Layer: L5 presenter — Qt-free by design (see `app/ui/drag_out.py`), so every
rule here runs without a widget.
"""

from __future__ import annotations

from types import SimpleNamespace

from app.ui.drag_out import draggable, existing, paths_for


def row(path: str = "", **extra):
    return SimpleNamespace(path=path, **extra)


# ---------------------------------------------------------------------------
# draggable()
# ---------------------------------------------------------------------------

def test_a_real_path_drags_itself():
    assert draggable(row(r"D:\Archive\report.pdf")) == r"D:\Archive\report.pdf"


def test_none_drags_nothing():
    assert draggable(None) == ""


def test_an_empty_path_drags_nothing():
    assert draggable(row("")) == ""


def test_a_mail_message_drags_nothing_never_the_whole_pst():
    """§3b: "never the whole PST." A message's path is a synthetic key into
    the archive, not a file - see the module docstring for why there is
    nothing on disk to hand `QDrag`."""
    assert draggable(row("pst://Personal Folders/12345")) == ""


def test_a_pst_embedded_attachment_also_drags_nothing():
    """The attachment's own text was extracted into `chunks`; its bytes were
    never written back to disk - `extract/email_pst.py` reads them from a
    `TemporaryDirectory` that is gone before indexing moves on."""
    assert draggable(row("pst://Personal Folders/12345/attachments/report.pdf")) == ""


# ---------------------------------------------------------------------------
# paths_for()
# ---------------------------------------------------------------------------

def test_paths_for_keeps_order_and_drops_repeats():
    rows = [row(r"D:\a.pdf"), row(r"D:\b.pdf"), row(r"D:\a.pdf")]
    assert paths_for(rows) == (r"D:\a.pdf", r"D:\b.pdf")


def test_paths_for_skips_mail_rows_in_a_mixed_selection():
    rows = [row(r"D:\a.pdf"), row("pst://Store/1"), row(r"D:\b.pdf")]
    assert paths_for(rows) == (r"D:\a.pdf", r"D:\b.pdf")


def test_paths_for_of_nothing_is_empty():
    assert paths_for([]) == ()
    assert paths_for(None) == ()


# ---------------------------------------------------------------------------
# existing()
# ---------------------------------------------------------------------------

def test_existing_keeps_only_real_files(tmp_path):
    here = tmp_path / "here.txt"
    here.write_text("x", encoding="utf-8")
    gone = tmp_path / "gone.txt"

    assert existing([str(here), str(gone)]) == (str(here),)


def test_existing_of_nothing_is_empty():
    assert existing([]) == ()
    assert existing(None) == ()


def test_existing_never_raises_on_a_malformed_path():
    assert existing(["\x00bad"]) == ()
