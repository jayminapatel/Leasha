"""The extractor registry fills on first read, not on import. Order 0r, 2b.

Layer: L2

`app/extract/__init__.py` used to import twenty-four parsers so that each could
`register()` itself. Three modules the window's first paint genuinely needs -
`app.extract.cells` (the spreadsheet view), `app.extract.source_types` (code
types), `app.extract.timecode` (media links) - live in that same package, so
touching any one of them ran the package's `__init__` and dragged the whole
parser set, plus `email`, `mailbox`, `html` and `xml`, in before the window was
on screen.

Registration is now deferred to the first read of `REGISTRY`/`NAME_REGISTRY`.
The two halves of that bargain are what this file holds:

* **the saving** - importing the package, or any of those three leaf modules,
  imports no parser (checked in a fresh interpreter, because `sys.modules` in
  this one is already full of them);
* **and nothing else changed** - every way anyone asks the registry a question
  still answers with the complete set, and a duplicate claim still raises.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]

#: Modules whose presence in a fresh interpreter proves the parser set loaded.
#: `base` and `chunker` are deliberately not here: they are the contract and
#: the splitter, they cost nothing, and the package has always re-exported them.
_PARSERS = ("app.extract.pdf", "app.extract.office", "app.extract.media",
            "app.extract.email_mbox", "app.extract.ebook")


def _extract_modules_after(statement: str) -> set[str]:
    """`app.extract.*` modules loaded in a fresh interpreter after `statement`."""
    code = (
        "import sys\n"
        f"{statement}\n"
        "print('|'.join(sorted(m for m in sys.modules "
        "if m.startswith('app.extract.'))))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=str(ROOT), text=True,
        capture_output=True, timeout=120,
    )
    assert result.returncode == 0, (
        f"the probe interpreter failed for {statement!r}:\n{result.stderr}"
    )
    return set(result.stdout.strip().split("|")) - {""}


class TestNothingLoadsUntilItIsAsked:
    """The saving: the parsers stay unimported until the registry is read."""

    @pytest.mark.parametrize("statement", [
        "import app.extract",
        "import app.extract.cells",
        "import app.extract.source_types",
        "import app.extract.timecode",
        "import app.core.code_types",
        # **The trap that made the first version of this change worth nothing.**
        # `register()` runs at the top of every extractor module, and it reads
        # the registry to refuse a duplicate claim - so importing one reader
        # loaded all twenty-four. `app.main` imports exactly this one, before
        # the window, to set the OCR device.
        "import app.extract.ocr",
    ])
    def test_importing_the_package_imports_no_parser(self, statement: str) -> None:
        loaded = _extract_modules_after(statement)

        assert not loaded & set(_PARSERS), (
            f"{statement} imported parser modules {sorted(loaded & set(_PARSERS))}. "
            "That is the quarter-second this item removed from the start-up "
            "path - see app/extract/__init__.py."
        )

    def test_reading_the_registry_does_import_them(self) -> None:
        """The other half: asked a question, it loads everything at once."""
        loaded = _extract_modules_after(
            "from app.extract.base import REGISTRY\nREGISTRY['.pdf']"
        )

        assert set(_PARSERS) <= loaded, (
            "reading REGISTRY did not import the parser set: "
            f"missing {sorted(set(_PARSERS) - loaded)}"
        )


class TestEveryWayOfAskingStillAnswers:
    """Nothing else changed: each accessor sees the whole registry."""

    def test_the_usual_reads_all_see_the_full_set(self) -> None:
        from app.extract.base import (
            NAME_REGISTRY, REGISTRY, extractor_by_name, extractor_for,
            extractor_names, supported_extensions, supported_names,
        )

        assert len(REGISTRY) > 100, "the registry is short - did the load run?"
        assert REGISTRY[".pdf"].name == "pdf"
        assert REGISTRY.get(".docx") is not None
        assert ".xlsx" in REGISTRY
        assert set(supported_extensions()) == set(REGISTRY)
        assert supported_extensions() == frozenset(iter(REGISTRY))
        assert {ext for ext, _e in REGISTRY.items()} == set(REGISTRY)
        assert {e.name for e in REGISTRY.values()} <= set(extractor_names())
        assert extractor_by_name("pdf") is REGISTRY[".pdf"]
        assert extractor_for(Path("a/b.pdf")) is REGISTRY[".pdf"]
        assert supported_names() == frozenset(NAME_REGISTRY)
        assert "makefile" in NAME_REGISTRY

    def test_copying_the_registry_copies_its_contents(self) -> None:
        """`dict(REGISTRY)` - the trap a lazy dict subclass falls into.

        CPython copies a dict subclass's storage directly unless the subclass
        overrides `__iter__` *and* `keys`; with only one of the two, this
        returns `{}` and every caller that takes a snapshot - the tests that
        swap in a fake extractor, `app/ui/widgets/file_types.py` - silently
        sees nothing at all.
        """
        from app.extract.base import REGISTRY

        assert dict(REGISTRY) == dict(REGISTRY.items()) != {}
        assert REGISTRY.copy() == dict(REGISTRY)
        assert len(dict(REGISTRY)) == len(REGISTRY)

    def test_a_second_claim_on_one_extension_is_still_refused(self) -> None:
        """The `.svg`-claimed-twice failure that lost 32 files stays caught."""
        from app.extract.base import REGISTRY, register

        class Greedy:
            name = "greedy"
            extensions = frozenset({".pdf"})
            reads_externally = False

            def supports(self, path: Path) -> bool:
                return True

            def extract(self, path: Path):
                return iter(())

        with pytest.raises(ValueError, match="already handled by 'pdf'"):
            register(Greedy())

        assert REGISTRY[".pdf"].name == "pdf", "the refusal left a half-write"
