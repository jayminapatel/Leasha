r"""AutoCAD drawings: `.dxf` natively, `.dwg` through a converter.

Layer: L2

**The two formats are not the same problem, and pretending they are is how this
goes wrong.** DXF is a published interchange format that `ezdxf` reads in pure
Python. DWG is AutoCAD's proprietary binary format with no open specification;
`ezdxf` does not read it and never has. So DWG is a Tier 2 converter route -
`dwg2dxf` or the ODA File Converter turns it into DXF, and this extractor reads
that - which is why `.dwg` is deliberately **not** in `extensions` below. An
extractor claiming an extension stops `extract()` ever reaching the converter,
so registering `.dwg` here would silently disable the only thing that can read it.

**What is worth indexing in a drawing is its text, not its geometry.** Title
blocks, revision clouds, general notes, part callouts, layer and block names -
that is what somebody searches for when they are trying to find the drawing
again. Coordinates and line work are not searchable in any useful sense and are
skipped entirely, which is also what keeps a 200MB drawing cheap to read.

Text lives in several places and all of them matter:

* `TEXT` and `MTEXT` - notes and callouts, the bulk of it.
* Block attributes (`INSERT` -> `ATTRIB`) - **the title block**. Drawing number,
  title, revision, drawn-by and date are almost always attributes, so an
  extractor that skipped them would miss the one field people search by.
* Dimension overrides - a dimension reading "SEE NOTE 4" rather than a number.
* Layer and block names - "E-LIGHTING", "P-STEAM-HP". Not text an author typed
  into the sheet, but they say what the drawing is about, and searching them is
  how somebody finds every drawing with a steam service on it.

Paper space layouts are read as well as model space, and each becomes its own
segment with the layout's name as a label, so a hit can say *which sheet*.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from app.core.errors import make_error
from app.core.format_health import Requirement
from app.core.logging import logger
from app.extract.base import Document, DocumentBuilder, register

__all__ = ["CadExtractor", "dwg_release", "DWG_RELEASES"]

log = logger.bind(component="extract.cad")

#: The six ASCII bytes every DWG starts with, mapped to the release a person
#: would recognise. Read straight from the header so a `.dwg` that cannot be
#: converted still says something true and useful about itself rather than
#: appearing as a bare filename.
DWG_RELEASES = {
    "AC1006": "R10",
    "AC1009": "R11/R12",
    "AC1012": "R13",
    "AC1014": "R14",
    "AC1015": "AutoCAD 2000",
    "AC1018": "AutoCAD 2004",
    "AC1021": "AutoCAD 2007",
    "AC1024": "AutoCAD 2010",
    "AC1027": "AutoCAD 2013",
    "AC1032": "AutoCAD 2018",
}

#: Entities whose text is worth having. Queried by name rather than walked, so
#: a drawing's geometry is never materialised at all.
_TEXT_ENTITIES = "TEXT MTEXT ATTRIB ATTDEF"

#: Present in every DXF regardless of content, so they identify nothing.
_DEFAULT_LAYERS = frozenset({"0", "Defpoints"})

#: Guards a pathological drawing: a generated one can hold hundreds of thousands
#: of tiny text entities, and past a point they stop being notes and start being
#: a way to spend ten minutes on one file.
_MAX_ENTITIES = 50_000


def dwg_release(path: Path) -> Optional[str]:
    """The AutoCAD release a `.dwg` was written by, or None.

    Never raises: this runs on a file that may be truncated, on a disconnected
    drive, or not a DWG at all despite its name.
    """
    try:
        with path.open("rb") as handle:
            marker = handle.read(6).decode("ascii", errors="replace")
    except OSError:
        return None
    return DWG_RELEASES.get(marker, marker if marker.startswith("AC") else None)


def _entity_text(entity: Any) -> str:
    """Whatever this entity has to say, as plain text. Never raises.

    MTEXT carries formatting codes (`\\P` for a paragraph break, `{\\fArial|b1;}`
    for a font run). `plain_text()` strips them; without it the index fills with
    control sequences that match nothing and wreck the snippets.
    """
    try:
        plain = getattr(entity, "plain_text", None)
        if callable(plain):
            return str(plain() or "")
        return str(getattr(entity.dxf, "text", "") or "")
    except Exception:                            # noqa: BLE001 - one odd entity
        return ""


def _layout_text(layout: Any) -> Iterator[str]:
    """Text from one layout, including block attributes."""
    seen = 0
    try:
        entities = layout.query(_TEXT_ENTITIES)
    except Exception as exc:                     # noqa: BLE001
        log.debug("could not query layout: {}", exc)
        return

    for entity in entities:
        seen += 1
        if seen > _MAX_ENTITIES:
            log.debug("stopped at {} text entities", _MAX_ENTITIES)
            return
        text = _entity_text(entity).strip()
        if text:
            yield text

    # Title-block fields live on INSERT entities as attributes, and are not
    # returned by the query above in every ezdxf version. Asked for explicitly,
    # because the drawing number is the single most searched field there is.
    try:
        for insert in layout.query("INSERT"):
            for attribute in getattr(insert, "attribs", ()) or ():
                tag = str(getattr(attribute.dxf, "tag", "") or "").strip()
                value = _entity_text(attribute).strip()
                if value:
                    yield f"{tag}: {value}" if tag else value
    except Exception as exc:                     # noqa: BLE001
        log.debug("could not read block attributes: {}", exc)


def _names(drawing: Any) -> str:
    """Layer and block names, which say what the drawing covers.

    Not authored text, so kept in one segment of its own rather than mixed into
    the notes - a hit on "E-LIGHTING" should read as what it is.
    """
    parts: list[str] = []
    try:
        layers = sorted({
            str(layer.dxf.name) for layer in drawing.layers
            # `0` and `Defpoints` exist in every DXF ever written, so they say
            # nothing about this one. Keeping them made every drawing look as
            # though it had content, which suppressed the name-only fallback
            # and left geometry exports indexed as "Layers: 0, Defpoints".
            if str(layer.dxf.name) not in _DEFAULT_LAYERS
        })
        if layers:
            parts.append("Layers: " + ", ".join(layers))
    except Exception:                            # noqa: BLE001
        pass
    try:
        blocks = sorted({
            str(block.name) for block in drawing.blocks
            # `*Model_Space`, `*Paper_Space0` and friends are structural, present
            # in every drawing, and searching for them finds everything.
            if not str(block.name).startswith("*")
        })
        if blocks:
            parts.append("Blocks: " + ", ".join(blocks))
    except Exception:                            # noqa: BLE001
        pass
    return "\n".join(parts)


class CadExtractor:
    """AutoCAD DXF drawings, read for their text.

    `.dwg` is handled by the Tier 2 converter route and is intentionally absent
    from `extensions` - see the module docstring.
    """

    name = "cad"
    extensions = frozenset({".dxf"})
    reads_externally = False

    #: Soft: without ezdxf a drawing is still indexed by name and by the release
    #: that wrote it, which is what makes it findable at all. The distinction
    #: matters on screen - "Limited" rather than "Cannot read".
    requires = (
        Requirement(
            "ezdxf", "ezdxf",
            provides="notes, title-block fields and layer names inside drawings",
            hard=False,
        ),
    )

    def supports(self, path: Path) -> bool:
        return path.suffix.lower() in self.extensions

    def extract(self, path: Path) -> Iterable[Document]:
        """The drawing's text, one segment per layout, or a name-only document.

        Never raises for a drawing ezdxf rejects and never yields nothing: a
        geometry-only or unreadable file still comes back as its name plus an
        `ERR_NO_TEXT_LAYER` warning, so a search by drawing number finds it.
        Reads only; nothing is written beside the file.
        """
        try:
            import ezdxf                          # noqa: PLC0415 - lazy by design
            from ezdxf.lldxf.const import DXFError
        except ImportError:
            yield self._name_only(
                path,
                "Reading the text inside a drawing needs the ezdxf package.",
                fix=r"venv\Scripts\pip install ezdxf",
            )
            return

        try:
            drawing = ezdxf.readfile(str(path))
        except (DXFError, UnicodeDecodeError) as exc:
            # A drawing ezdxf rejects is still worth having by name: somebody
            # looking for it by number should find it and be told why its
            # contents are missing, rather than find nothing at all.
            yield self._name_only(
                path,
                f"This drawing could not be parsed ({type(exc).__name__}).",
                fix="Open it in a CAD application and re-save it as DXF.",
            )
            return
        except OSError as exc:
            yield self._name_only(path, f"The file could not be read: {exc}")
            return

        builder = DocumentBuilder(path)
        builder.meta["dxf_version"] = str(getattr(drawing, "dxfversion", "") or "")

        try:
            layouts = [drawing.modelspace()] + [
                drawing.layout(name) for name in drawing.layout_names()
                if name.lower() != "model"
            ]
        except Exception:                        # noqa: BLE001 - model space alone
            layouts = [drawing.modelspace()]

        authored = False
        for number, layout in enumerate(layouts, start=1):
            label = str(getattr(layout, "name", "") or f"Layout {number}")
            text = "\n".join(_layout_text(layout))
            if text.strip():
                authored = True
                builder.add(text, page=number, label=label, prefix_label=True)

        names = _names(drawing)

        if not authored:
            # A pure geometry export is a real thing. Its file name is what
            # somebody will search for, so that leads - with the layer names
            # after it, which are the only other clue to what it covers.
            # **Judged on authored text, not on the built document**: every DXF
            # carries structure, so testing the document for emptiness would
            # mean this never fired and the drawing indexed as "Layers: 0".
            document = self._name_only(
                path, "This drawing contains no text, only geometry.",
                extra=names,
            )
            yield document
            return

        if names:
            builder.add(names, page=len(layouts) + 1, label="Drawing structure")
        yield builder.build()

    def _name_only(
        self, path: Path, reason: str, fix: str = "", extra: str = ""
    ) -> Document:
        """The drawing as its file name plus an honest note about the rest.

        Never an empty document: `extract()` treats empty as
        `ERR_NO_TEXT_LAYER` and the file disappears from the index entirely,
        which for a drawing somebody searches by number is the worst outcome.
        """
        builder = DocumentBuilder(path)
        builder.add(path.stem.replace("_", " ").replace("-", " "), label="File name")

        release = dwg_release(path) if path.suffix.lower() == ".dwg" else None
        if release:
            builder.add(f"AutoCAD drawing, {release}", label="Format")
        if extra:
            builder.add(extra, label="Drawing structure")

        builder.warn(make_error(
            "ERR_NO_TEXT_LAYER", "extract.cad",
            path=str(path),
            details=reason,
            suggestion=fix or "The drawing is indexed by its file name.",
        ))
        return builder.build()


register(CadExtractor())
