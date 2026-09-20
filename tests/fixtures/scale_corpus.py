r"""A deterministic plain-text corpus at a size where an index run is throughput,
not process start-up - the nightly loop's scale fixture (order 0m section 5a).

Layer: L1 fixture (tooling and tests only)

`tests/fixtures/generate.py` builds ~35 real-format files (PDF, Office, mail,
corrupt ones). That is the right corpus for "does every extractor still work"
and the wrong one for "how fast is an index run": at that size the wall clock
is the interpreter starting and the model loading. This is the other half - a
few hundred small text documents, each unique (so content-hash de-duplication
cannot collapse them), written from this project's own vocabulary so the
search stage has real words to find.

**Build or refresh, never rebuild blindly.** `ensure_scale_corpus` writes a
manifest beside the folder; when the count and the generator version match it
does nothing (a night's cost is zero), and when either differs it clears its
own folder and writes it again. It only ever deletes inside the folder it was
given and only files it named itself - it will not touch anything else there.
"""

from __future__ import annotations

import json
from pathlib import Path

__all__ = ["ensure_scale_corpus", "DEFAULT_FILES", "GENERATOR_VERSION"]

#: Bumped when the text a document holds changes, so an old corpus is refreshed.
GENERATOR_VERSION = 1

DEFAULT_FILES = 300
WORDS_PER_DOCUMENT = 260

_VOCABULARY = (
    "pump station commissioning report valve replacement shutdown flow rate "
    "manifold safety induction training budget forecast quarterly maintenance "
    "schedule pressure vessel inspection certificate calibration record "
    "instrument loop drawing isolation permit contractor handover survey "
    "northern plant vibration tolerance turbine gearbox bearing lubricant "
    "invoice supplier delivery warehouse pallet inventory audit review "
    "meeting minutes action owner deadline risk mitigation approval"
).split()

_SITES = ("barnsley", "leeds", "newcastle", "sheffield", "doncaster", "hull", "york", "bradford")


def _document(number: int) -> str:
    """Unique per `number`: a different starting offset, stride and site."""
    size = len(_VOCABULARY)
    stride = 3 + (number % 11)
    words = [_VOCABULARY[(number * 13 + i * stride) % size] for i in range(WORDS_PER_DOCUMENT)]
    site = _SITES[number % len(_SITES)]
    lines = [f"Document {number:05d} - {site} site notes"]
    for start in range(0, len(words), 13):
        lines.append(" ".join(words[start:start + 13]).capitalize() + ".")
    return "\n".join(lines) + "\n"


def ensure_scale_corpus(root: Path, files: int = DEFAULT_FILES) -> dict:
    """Make `root` hold exactly `files` documents. Returns
    `{"path", "files", "refreshed", "bytes"}`."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    # Beside the folder, not in it: the indexer would otherwise index the manifest.
    manifest_path = root.parent / f"{root.name}.manifest.json"
    wanted = {"version": GENERATOR_VERSION, "files": int(files), "words": WORDS_PER_DOCUMENT}

    try:
        current = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = None
    on_disk = sum(1 for _ in root.glob("doc-*.txt"))
    if current == wanted and on_disk == files:
        return {"path": root, "files": files, "refreshed": False,
                "bytes": sum(p.stat().st_size for p in root.glob("doc-*.txt"))}

    for stale in root.glob("doc-*.txt"):            # only files this module names
        stale.unlink()
    total = 0
    for number in range(files):
        body = _document(number).encode("utf-8")
        (root / f"doc-{number:05d}.txt").write_bytes(body)
        total += len(body)
    manifest_path.write_text(json.dumps(wanted), encoding="utf-8")
    return {"path": root, "files": files, "refreshed": True, "bytes": total}
