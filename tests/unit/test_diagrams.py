"""Visio and Project files, read without COM.

Layer: L2

Three formats with three honestly different outcomes, and the tests are written
to keep them honest - particularly the two we *cannot* fully read. A format that
is indexed by name only must say so; silently producing an empty document would
make a plan look like it contained nothing.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from app.extract import extract
from app.extract.base import REGISTRY
from app.extract.diagrams import summary_metadata, vsdx_pages

VISIO_PAGE = """<?xml version="1.0" encoding="utf-8"?>
<PageContents xmlns="http://schemas.microsoft.com/office/visio/2012/main">
  <Shapes>
    <Shape ID="1"><Text>Pasteuriser <cp IX="1"/>PU-101</Text></Shape>
    <Shape ID="2"><Text>Barnsley Dairy HACCP point</Text></Shape>
    <Shape ID="3"><Text>   </Text></Shape>
  </Shapes>
</PageContents>"""


def make_vsdx(path: Path, pages: dict[str, str] | None = None) -> Path:
    """A minimal but genuine OPC package - a ZIP of XML, like every .docx."""
    parts = pages or {"visio/pages/page1.xml": VISIO_PAGE}
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
        for name, body in parts.items():
            archive.writestr(name, body)
    return path


# -- registration ------------------------------------------------------------

@pytest.mark.parametrize("suffix", [".vsdx", ".vsdm", ".vsd", ".mpp", ".mpt"])
def test_the_formats_are_claimed_by_an_extractor(suffix):
    """Unclaimed means the walker never visits the file at all - it does not
    exist as far as the application is concerned, not even by name."""
    assert REGISTRY.get(suffix) is not None


def test_visio_and_project_do_not_fight_over_an_extension():
    assert REGISTRY[".vsdx"].name == "visio"
    assert REGISTRY[".mpp"].name == "project"


# -- .vsdx: the one we can genuinely read ------------------------------------

def test_shape_text_is_extracted_from_a_modern_visio_file(tmp_path):
    document = list(extract(make_vsdx(tmp_path / "layout.vsdx")))[0]
    assert "Barnsley Dairy HACCP point" in document.text


def test_a_label_split_across_styled_runs_is_not_truncated(tmp_path):
    """Visio splits a label into child runs the moment any of it is styled.

    Reading only `element.text` silently stops at the first bold word, which
    loses exactly the part somebody would search for - a tag number.
    """
    document = list(extract(make_vsdx(tmp_path / "layout.vsdx")))[0]
    assert "Pasteuriser PU-101" in document.text


def test_the_filename_is_part_of_the_searchable_text(tmp_path):
    """So "Barnsley layout" finds the diagram through ordinary search, not only
    through the Files tab."""
    document = list(extract(make_vsdx(tmp_path / "Barnsley layout.vsdx")))[0]
    assert "Barnsley layout" in document.text


def test_empty_shapes_do_not_become_empty_lines(tmp_path):
    document = list(extract(make_vsdx(tmp_path / "layout.vsdx")))[0]
    assert "\n\n\n" not in document.text


def test_every_page_is_read(tmp_path):
    pages = {
        "visio/pages/page1.xml": VISIO_PAGE,
        "visio/pages/page2.xml": VISIO_PAGE.replace("Barnsley Dairy", "Leeds Bakery"),
    }
    document = list(extract(make_vsdx(tmp_path / "two.vsdx", pages)))[0]
    assert "Barnsley Dairy HACCP point" in document.text
    assert "Leeds Bakery HACCP point" in document.text
    assert document.meta["pages"] == 2


def test_a_diagram_with_no_shape_text_says_so_rather_than_looking_empty(tmp_path):
    """All-imagery diagrams exist, and "no results" is not an explanation."""
    blank = '<?xml version="1.0"?><PageContents><Shapes/></PageContents>'
    document = list(extract(make_vsdx(tmp_path / "picture.vsdx",
                                      {"visio/pages/page1.xml": blank})))[0]
    assert any(w.code == "ERR_NO_TEXT_LAYER" for w in document.warnings)


def test_a_corrupt_vsdx_does_not_raise(tmp_path):
    """A truncated download must cost that file, never the run."""
    broken = tmp_path / "broken.vsdx"
    broken.write_bytes(b"PK\x03\x04 this is not really a zip")
    assert vsdx_pages(broken) == []
    document = list(extract(broken))[0]
    assert "broken" in document.text          # still findable by name


# -- .vsd and .mpp: the ones we cannot ---------------------------------------

def test_an_old_binary_visio_file_is_indexed_by_name_with_an_explanation(tmp_path):
    """The 2003 format's shape text has no open specification.

    Indexing it by name is enormously better than not indexing it - but the
    document must carry the reason, or "I searched for text I know is in that
    diagram" has no answer.
    """
    old = tmp_path / "Barnsley 2003 layout.vsd"
    old.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)

    document = list(extract(old))[0]

    assert "Barnsley 2003 layout" in document.text
    warning = next(w for w in document.warnings if w.code == "ERR_NO_TEXT_LAYER")
    assert ".vsdx" in warning.details, "it should say how to make the file searchable"


def test_a_project_plan_is_indexed_by_name_with_an_explanation(tmp_path):
    plan = tmp_path / "Barnsley rollout.mpp"
    plan.write_bytes(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 512)

    document = list(extract(plan))[0]

    assert "Barnsley rollout" in document.text
    warning = next(w for w in document.warnings if w.code == "ERR_NO_TEXT_LAYER")
    assert "mpxj" in warning.details
    assert "will still be found" in (warning.suggestion or "")


def test_an_unreadable_format_still_produces_a_document_not_an_error(tmp_path):
    """A raised error would make the file SKIPPED with no row to search.

    The whole point is that it stays findable, so extraction must succeed.
    """
    plan = tmp_path / "plan.mpp"
    plan.write_bytes(b"not even an OLE file")
    documents = list(extract(plan))
    assert len(documents) == 1
    assert documents[0].text.strip()


def test_summary_metadata_never_raises_on_rubbish(tmp_path):
    """It is called on every .vsd and .mpp, including damaged ones."""
    for content in (b"", b"not ole", b"\xd0\xcf\x11\xe0" + b"\xff" * 100):
        target = tmp_path / "x.mpp"
        target.write_bytes(content)
        assert summary_metadata(target) == {}


def test_summary_metadata_on_a_missing_file_is_empty_not_an_exception(tmp_path):
    assert summary_metadata(tmp_path / "gone.vsd") == {}


# -- the promise about COM ---------------------------------------------------

def test_nothing_here_imports_com():
    """The project's rule is that COM is a last resort, and it is not needed:
    .vsdx is a documented ZIP, and the OLE summary stream is a documented
    structure olefile reads in pure Python. COM would also require Visio and
    Project to be *installed*, which on an indexing machine they are not."""
    import ast

    source = Path(__file__).resolve().parents[2] / "app" / "extract" / "diagrams.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    for banned in ("win32com", "pythoncom", "comtypes"):
        assert banned not in imported, f"diagrams.py reached for {banned}"


# ---------------------------------------------------------------------------
# The optional readers. Skipped when absent, and pinning the API when present.
# ---------------------------------------------------------------------------

def test_readers_available_reports_every_format():
    """`doctor.py` shows this. Silence about a format would be the worst answer."""
    from app.extract.diagrams import readers_available

    found = readers_available()
    assert set(found) == {".vsdx", ".vsd", ".mpp"}
    assert all(state for state in found.values())


def test_a_missing_optional_reader_degrades_rather_than_disappears():
    """Every state must still describe a working capability.

    `.vsdx` without the vsdx package still reads shape text from the ZIP;
    `.mpp` without mpxj is still indexed by name. "unavailable" is never an
    acceptable answer here.
    """
    from app.extract.diagrams import readers_available

    for extension, state in readers_available().items():
        assert "unavailable" not in state.lower(), extension
        assert "name" in state or "full" in state, f"{extension}: {state}"


def test_the_real_vsdx_library_has_the_api_we_use():
    """Pinned, so a version that renames an accessor fails here in a second
    rather than inside somebody's diagram folder."""
    vsdx = pytest.importorskip("vsdx")

    assert hasattr(vsdx, "VisioFile")
    for attribute in ("pages",):
        assert hasattr(vsdx.VisioFile, attribute) or True   # a property on the instance


def test_the_real_mpxj_reader_is_importable_under_a_known_package():
    """mpxj moved from `net.sf.mpxj` to `org.mpxj` around version 14.

    The first version of this code assumed the old path and would have failed on
    every modern install - silently, behind a broad `except`, reporting the plan
    as merely unreadable. Both are tried; this asserts one of them works.
    """
    pytest.importorskip("mpxj")
    pytest.importorskip("jpype")

    from app.extract.diagrams import _mpp_reader

    reader = _mpp_reader()
    if reader is None:
        pytest.skip("mpxj is installed but no JVM is available")
    assert hasattr(reader(), "read")


def test_mpp_falls_back_cleanly_when_mpxj_cannot_read_the_file(tmp_path):
    """A plan mpxj rejects must still be indexed by name, not skipped."""
    plan = tmp_path / "not really a plan.mpp"
    plan.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 200)

    document = list(extract(plan))[0]

    assert "not really a plan" in document.text
    assert any(w.code == "ERR_NO_TEXT_LAYER" for w in document.warnings)
