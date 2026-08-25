"""AutoCAD drawings: what is read, and what is honestly reported instead.

Layer: L2

Two formats, two different problems, and conflating them is the failure this
guards. DXF is published and readable; DWG is proprietary, has no Python reader,
and must go through a converter. The tests that matter most are the ones
asserting a drawing is **never silently lost**: a drawing nobody can parse is
still indexed by its number, because that is what people search for.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.extract.base import extractor_for
from app.extract.cad import DWG_RELEASES, CadExtractor, dwg_release


def test_dxf_is_claimed_but_dwg_is_left_for_the_converter():
    """The subtle one. An extractor claiming an extension stops `extract()`
    ever reaching Tier 2, so registering `.dwg` here would disable the only
    thing that can read it."""
    assert extractor_for(Path("plan.dxf")) is not None
    assert extractor_for(Path("plan.dxf")).name == "cad"
    assert extractor_for(Path("plan.dwg")) is None, (
        "a .dwg extractor would shadow the dwg2dxf converter route"
    )


def test_the_dwg_converter_route_exists_and_targets_this_extractor():
    from app.core.formats import load_rules

    rule = load_rules(None).converter_for(".dwg")
    assert rule is not None, "no converter route for .dwg"
    assert rule.then == "cad", "the converted DXF must come back to this reader"
    assert rule.binary in {"dwg2dxf", "ODAFileConverter"}
    assert not rule.enabled, "converters ship disabled - the binary may be absent"


def test_the_converter_binary_is_on_the_allow_list():
    """Config naming a binary the code has not allowed is refused at run time.
    Shipping a route that would be blocked is shipping a dead setting."""
    from app.core.formats import load_rules
    from app.extract.converter import ALLOWED_BINARIES

    rule = load_rules(None).converter_for(".dwg")
    assert rule.binary in ALLOWED_BINARIES


# --- the DWG header --------------------------------------------------------

@pytest.mark.parametrize("marker,expected", [
    (b"AC1032", "AutoCAD 2018"),
    (b"AC1015", "AutoCAD 2000"),
    (b"AC1009", "R11/R12"),
])
def test_dwg_release_is_read_from_the_header(tmp_path: Path, marker, expected):
    drawing = tmp_path / "site.dwg"
    drawing.write_bytes(marker + b"\x00" * 64)
    assert dwg_release(drawing) == expected


def test_an_unknown_but_plausible_marker_is_reported_verbatim():
    """A newer AutoCAD than this table knows about should still say something
    true rather than nothing at all."""
    import tempfile

    with tempfile.TemporaryDirectory() as td:
        drawing = Path(td) / "future.dwg"
        drawing.write_bytes(b"AC1099" + b"\x00" * 32)
        assert dwg_release(drawing) == "AC1099"
        assert "AC1099" not in DWG_RELEASES


def test_dwg_release_never_raises_on_rubbish(tmp_path: Path):
    assert dwg_release(tmp_path / "does-not-exist.dwg") is None
    junk = tmp_path / "not-a-drawing.dwg"
    junk.write_bytes(b"\xff\xfe\x00")
    assert dwg_release(junk) is None


# --- reading ---------------------------------------------------------------

def _dxf_document(path: Path):
    return list(CadExtractor().extract(path))[0]


def test_an_unparseable_drawing_is_still_indexed_by_name(tmp_path: Path):
    """Never an empty document: empty means ERR_NO_TEXT_LAYER and the drawing
    disappears from the index entirely - the worst outcome for a file somebody
    searches for by number."""
    pytest.importorskip("ezdxf")

    broken = tmp_path / "DRG-4471-rev-C.dxf"
    broken.write_text("this is not a DXF at all", encoding="utf-8")

    document = _dxf_document(broken)

    assert not document.is_empty
    assert "DRG 4471 rev C" in document.text, "the drawing number must survive"
    assert any(w.code == "ERR_NO_TEXT_LAYER" for w in document.warnings)


def test_a_missing_library_degrades_to_the_name_with_a_fix(tmp_path, monkeypatch):
    """Without ezdxf the drawing is name-only - and says which package would
    change that, rather than leaving an empty result to be interpreted."""
    import builtins

    real_import = builtins.__import__

    def no_ezdxf(name, *args, **kwargs):
        if name == "ezdxf" or name.startswith("ezdxf."):
            raise ImportError("simulated: ezdxf is not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_ezdxf)

    drawing = tmp_path / "P-1002-steam-header.dxf"
    drawing.write_text("irrelevant", encoding="utf-8")

    document = _dxf_document(drawing)

    assert "P 1002 steam header" in document.text
    warning = document.warnings[0]
    assert "ezdxf" in warning.render()


def test_text_and_title_block_attributes_are_read(tmp_path: Path):
    """The title block is the point: drawing number, title and revision are
    block attributes, and an extractor that skipped them would miss the one
    field people actually search by."""
    ezdxf = pytest.importorskip("ezdxf")

    drawing = ezdxf.new()
    space = drawing.modelspace()
    space.add_text("GENERAL NOTES: all welds to BS EN 287")
    space.add_mtext("Pump P-101 discharge\\Pto vessel V-205")

    block = drawing.blocks.new(name="TITLEBLOCK")
    block.add_attdef(tag="DRG_NO", text="")
    insert = space.add_blockref("TITLEBLOCK", (0, 0))
    insert.add_auto_attribs({"DRG_NO": "DRG-4471"})

    drawing.layers.add("P-STEAM-HP")

    path = tmp_path / "sheet.dxf"
    drawing.saveas(path)

    document = _dxf_document(path)

    assert "GENERAL NOTES" in document.text
    assert "Pump P-101 discharge" in document.text
    assert "DRG-4471" in document.text, "title-block attribute was not read"
    assert "P-STEAM-HP" in document.text, "layer names say what a drawing covers"
    assert not any(
        "\\P" in segment.text for segment in document.segments
    ), "MTEXT formatting codes must be stripped, not indexed"


def test_a_drawing_with_no_text_is_reported_not_dropped(tmp_path: Path):
    """A pure geometry export is a real thing, and still findable by name."""
    ezdxf = pytest.importorskip("ezdxf")

    drawing = ezdxf.new()
    drawing.modelspace().add_line((0, 0), (10, 10))
    path = tmp_path / "geometry-only.dxf"
    drawing.saveas(path)

    document = _dxf_document(path)

    assert "geometry only" in document.text
    assert any(w.code == "ERR_NO_TEXT_LAYER" for w in document.warnings)


def test_default_layers_do_not_count_as_content(tmp_path: Path):
    """`0` and `Defpoints` are in every DXF ever written. Counting them as
    content made every geometry export index as 'Layers: 0, Defpoints' with no
    file name - findable by nothing, and the name-only fallback never ran."""
    ezdxf = pytest.importorskip("ezdxf")

    drawing = ezdxf.new()
    drawing.modelspace().add_line((0, 0), (1, 1))
    path = tmp_path / "DRG-9001.dxf"
    drawing.saveas(path)

    document = _dxf_document(path)

    assert "DRG 9001" in document.text, "the drawing number must lead"
    assert "Defpoints" not in document.text


def test_real_layer_names_survive_in_a_textless_drawing(tmp_path: Path):
    """The other half: a drawing with no notes but meaningful layers should
    keep them - they are the only clue to what it covers."""
    ezdxf = pytest.importorskip("ezdxf")

    drawing = ezdxf.new()
    drawing.layers.add("E-LIGHTING")
    drawing.modelspace().add_line((0, 0), (1, 1))
    path = tmp_path / "DRG-9002.dxf"
    drawing.saveas(path)

    document = _dxf_document(path)

    assert "DRG 9002" in document.text
    assert "E-LIGHTING" in document.text


def test_the_reader_declares_ezdxf_as_a_soft_requirement():
    """Soft, because a drawing without it is still indexed by name - the
    difference between 'Limited' and 'Cannot read' on screen."""
    requirements = {r.module: r for r in CadExtractor.requires}
    assert "ezdxf" in requirements
    assert requirements["ezdxf"].hard is False
