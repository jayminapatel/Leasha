r"""Tier 2: run an external command, then read what it produced.

Layer: L2

One implementation, and LibreOffice alone then covers `.doc`, `.xls`, `.ppt`,
`.rtf`, `.pages`, `.numbers`, `.key`, `.wpd` and `.pub` - a dozen dead formats
added by editing a text file rather than by writing a parser for each.

That leverage is the whole argument for Tier 2. It also makes this the most
dangerous module in the application, because it runs programs, so every decision
below is about narrowing what that can mean.

**The allow-list is in code, not configuration.** `config/extractors.toml` is a
file a person edits, and on a shared or synced machine it is a file *someone
else* might edit. A configuration format that can name any executable is a way
to run anything. Config chooses *which allowed converter* handles a format; it
cannot introduce a new one.

**The command is never passed to a shell.** `subprocess.run(list, shell=False)`,
always. A shell would interpret `;`, `&&`, `|`, backticks and globs in a
filename - and filenames come from the corpus being indexed, which is precisely
the input not to trust.

**The absolute path actually invoked is resolved and logged.** `soffice` on
`PATH` is whatever `PATH` says today. Recording what ran makes "it converted
nothing on that machine" answerable, and makes a hijacked `PATH` visible after
the fact rather than never.

**Every temporary directory is removed in a `finally`.** A 100GB run that leaks
one directory per converted file fills the disk, and the failure appears
somewhere else entirely.

**Converters ship disabled.** The binary may not be installed, and a format that
fails on every file is worse than one that says plainly it is switched off.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.errors import AppErrorException, make_error, raise_error
from app.core.logging import logger

__all__ = [
    "ALLOWED_BINARIES",
    "ConversionResult",
    "convert",
    "resolve_binary",
    "available_binaries",
]

log = logger.bind(component="extract.converter")

#: **The only programs this application will ever run.**
#:
#: In code, deliberately, and not read from configuration. Adding to this list
#: is a commit with a diff and a reviewer; adding to a TOML file is a text edit
#: that may not even be made by the person who owns the machine.
#:
#: Anything else named by config is refused with `ERR_CONVERTER_BLOCKED`, which
#: names the binary - and, critically, refuses *before* resolving or executing
#: anything at all.
ALLOWED_BINARIES = frozenset({
    "soffice",          # LibreOffice: doc, xls, ppt, rtf, pages, numbers, key
    "libreoffice",      # the same thing under its other name
    "pandoc",           # epub, fb2, rst, org
    "xstexporter",      # Lotus Notes NSF, on the rare machine that has it
    "tesseract",        # OCR as a converter, for anybody preferring it to RapidOCR
    # AutoCAD DWG has no open specification and no Python reader. Both of these
    # turn it into DXF, which `app/extract/cad.py` then reads properly.
    "dwg2dxf",          # LibreDWG, free and file-at-a-time
    "ODAFileConverter", # Open Design Alliance's, free to download, batch-oriented
})

#: Placeholders a converter command may use. Anything else in braces is left
#: alone rather than guessed at - a filename containing `{` is not a template.
_PLACEHOLDERS = ("{input}", "{outdir}", "{stem}")

#: Hard ceiling regardless of what config asks for. A converter that has not
#: finished in five minutes is stuck, and a 100GB run cannot afford to find that
#: out one file at a time.
MAX_TIMEOUT_S = 300


class ConversionResult:
    """What a conversion produced, and what it cost."""

    __slots__ = ("path", "binary", "elapsed_s", "cleanup")

    def __init__(
        self,
        path: Path,
        binary: str,
        elapsed_s: float,
        cleanup: Optional[Any] = None,
    ) -> None:
        #: The converted file, inside a temporary directory owned by `cleanup`.
        self.path = path
        #: The absolute path actually invoked.
        self.binary = binary
        self.elapsed_s = elapsed_s
        self._set_cleanup(cleanup)

    def _set_cleanup(self, cleanup: Optional[Any]) -> None:
        object.__setattr__(self, "cleanup", cleanup)

    def __enter__(self) -> "ConversionResult":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()

    def close(self) -> None:
        """Remove the temporary directory. Safe to call twice."""
        if self.cleanup is not None:
            try:
                self.cleanup.cleanup()
            except OSError as exc:               # noqa: BLE001 - best effort
                log.debug("temp directory not removed: {}", exc)
            self._set_cleanup(None)


def resolve_binary(name: str) -> Optional[str]:
    """The absolute path of an **allowed** binary, or None if it is not here.

    Refuses anything off the list before looking, so a blocked name is never
    even resolved - the answer to "is `curl` installed" is not one this
    application should be helping anybody find out.
    """
    if name not in ALLOWED_BINARIES:
        return None
    return shutil.which(name)


def available_binaries() -> dict[str, Optional[str]]:
    """Every allowed converter and where it is, for `doctor` and Settings.

    Settings offers to enable exactly the converters whose binary was found -
    so nobody switches on a format that will then fail on every file.
    """
    return {name: shutil.which(name) for name in sorted(ALLOWED_BINARIES)}


def _build_command(
    template: tuple[str, ...], binary_path: str, source: Path, outdir: Path
) -> list[str]:
    """Substitute the placeholders. Each argument stays one argument.

    A path with a space in it is one element of the list and reaches the child
    process intact, because nothing here joins the command into a string. That
    is the property a shell would destroy.
    """
    stem = source.stem
    command = [binary_path]
    for argument in template[1:]:
        value = argument
        for placeholder, replacement in (
            ("{input}", str(source)),
            ("{outdir}", str(outdir)),
            ("{stem}", stem),
        ):
            value = value.replace(placeholder, replacement)
        command.append(value)
    return command


def convert(
    source: Path,
    rule: Any,
    *,
    timeout_s: Optional[int] = None,
) -> ConversionResult:
    """Run one converter and return the file it produced.

    `rule` is a `ConverterRule` from `app/core/formats.py`. Raises
    `AppErrorException` with a precise code for every failure, so the caller
    records it and carries on - a converter that cannot run is one skipped file,
    not a stopped index run.

    The caller **must** close the result, or use it as a context manager, or the
    temporary directory survives the process.
    """
    binary_name = rule.command[0] if rule.command else ""

    if binary_name not in ALLOWED_BINARIES:
        # Refused before resolving, before creating anything, before running.
        # `config` can choose among allowed converters; it cannot add one.
        raise AppErrorException(make_error(
            "ERR_CONVERTER_BLOCKED", "extract.converter",
            binary=binary_name or "(empty)", path=str(source),
            details=(
                f"'{binary_name}' is not on the allow-list, which lives in "
                f"app/extract/converter.py and not in configuration. "
                f"Allowed: {', '.join(sorted(ALLOWED_BINARIES))}."
            ),
        ))

    binary_path = shutil.which(binary_name)
    if not binary_path:
        raise AppErrorException(make_error(
            "ERR_CONVERTER_MISSING", "extract.converter",
            binary=binary_name, path=str(source),
        ))

    holder = tempfile.TemporaryDirectory(prefix="leasha-convert-")
    outdir = Path(holder.name)
    command = _build_command(tuple(rule.command), binary_path, source, outdir)
    limit = min(int(timeout_s or getattr(rule, "timeout_s", 180)), MAX_TIMEOUT_S)

    started = time.monotonic()
    try:
        # **shell=False, always.** Filenames come from the corpus, and a shell
        # would interpret `;`, `&&`, `|` and backticks in one.
        finished = subprocess.run(
            command, capture_output=True, timeout=limit, check=False, shell=False,
            cwd=str(outdir),
        )
    except subprocess.TimeoutExpired:
        holder.cleanup()
        raise AppErrorException(make_error(
            "ERR_CONVERTER_FAILED", "extract.converter",
            binary=binary_name, path=str(source),
            details=f"{binary_name} did not finish within {limit}s",
        )) from None
    except OSError as exc:
        holder.cleanup()
        raise AppErrorException(make_error(
            "ERR_CONVERTER_FAILED", "extract.converter",
            binary=binary_name, path=str(source), details=str(exc),
        )) from exc

    elapsed = time.monotonic() - started
    # Logged every time, with the path actually invoked: `soffice` on PATH is
    # whatever PATH says today, and "it converted nothing on that machine" is
    # otherwise unanswerable.
    log.debug("ran {} in {:.1f}s (exit {}) for {}",
              binary_path, elapsed, finished.returncode, source.name)

    produced = _find_output(outdir, rule, source)
    if produced is None:
        detail = (finished.stderr or b"").decode("utf-8", "replace").strip()[:400]
        holder.cleanup()
        raise AppErrorException(make_error(
            "ERR_CONVERTER_FAILED", "extract.converter",
            binary=binary_name, path=str(source),
            details=(
                f"exit {finished.returncode}, and nothing matching "
                f"'{getattr(rule, 'produces', '?')}' was written"
                + (f". {detail}" if detail else "")
            ),
        ))

    return ConversionResult(produced, binary_path, elapsed, cleanup=holder)


def _find_output(outdir: Path, rule: Any, source: Path) -> Optional[Path]:
    """The converted file, by name if possible and by inspection if not.

    LibreOffice does not always name the output as documented - a `.doc` may
    come out as `.txt` or `.html` depending on version and filter. Falling back
    to "the only file it wrote" is more robust than trusting the template, and
    the alternative is a converter that works everywhere except the version
    somebody actually has.
    """
    expected = str(getattr(rule, "produces", "")).replace("{stem}", source.stem)
    if expected:
        candidate = outdir / expected
        if candidate.is_file() and candidate.stat().st_size:
            return candidate

    written = [p for p in outdir.iterdir() if p.is_file() and p.stat().st_size]
    if len(written) == 1:
        return written[0]
    if written:
        # Several files: take the largest, which for a document conversion is
        # the content rather than a stylesheet or an image.
        return max(written, key=lambda p: p.stat().st_size)
    return None


def extract_via_converter(source: Path, rule: Any) -> Iterable[Any]:
    """Convert, then hand the result to the extractor named by `then`.

    The converted file keeps the *original* path in the Document, so a search
    result points at the document somebody has rather than at a temporary file
    that no longer exists by the time they click it.
    """
    from app.extract.base import extractor_by_name

    reader = extractor_by_name(str(getattr(rule, "then", "")))
    if reader is None:
        raise_error(
            "ERR_CONVERTER_FAILED", "extract.converter",
            binary=rule.command[0] if rule.command else "?", path=str(source),
            details=f"no extractor named '{getattr(rule, 'then', '')}' is registered",
        )
        return

    with convert(source, rule) as result:
        for document in reader.extract(result.path):
            # Repointed at the real file. A result linking to
            # `/tmp/leasha-convert-xyz/report.txt` is worse than no result:
            # it looks like an answer and cannot be opened.
            document.path = source
            document.meta["converted_by"] = Path(result.binary).name
            document.meta["conversion_s"] = round(result.elapsed_s, 2)
            yield document
