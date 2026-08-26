r"""Nothing may remove a key the application cannot start without.

Layer: L0

**The worst bug of the project so far, and the quietest.** `.env` arrived
holding only `PROJECT_PATH` and `LOG_PATH`, four empty *"Written by Leasha"*
headers and a stray `OLLAMA_URL`. `DATA_PATH`, `VECTOR_PATH`, `FTS_DB`,
`CACHE_PATH`, `MODEL_CACHE` and `STATE_PATH` were gone, and the application
could not start at all - `load_settings` refuses with *"'' does not exist"*
**before logging is configured**, so there was no log line, no traceback and no
window. From outside: "now the application does not even start says some path is
missing".

`env_writer.render` removes a key whose value is `None`, and that is correct and
load-bearing: it is the only way to *unpin* a setting so an improved default can
reach an existing install. The installer once wrote
`RERANK_MODEL=BAAI/bge-reranker-base` into every install and froze all of them on
a model measured 9.2x slower than the code's own choice, with no way to undo it.
Removal is that way.

It is nonsense for a location. `DATA_PATH` is declared `default=""`, so
"restore the default" means "leave the index location blank".

**Only the roots are protected, and working out which took a failing test.**
The first version guarded all eight location keys, and
`test_the_pinned_subpaths_are_removed_not_rewritten` failed at once:
`VECTOR_PATH`, `FTS_DB`, `CACHE_PATH`, `MODEL_CACHE` and `STATE_PATH` derive
from `DATA_PATH`, and `index_move` **removes them on purpose** - a sub-path left
pinned outranks `DATA_PATH` and strands that part of the index on the old drive.

A guard against losing data that would have broken moving data. `DATA_PATH`,
`PROJECT_PATH` and `LOG_PATH` have nothing to derive from, so those are the
three.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from app.core.env_writer import apply_values
from app.core.settings_registry import LOCATION_KEYS, protected

WHOLE = {
    "DATA_PATH": r"D:\Leasha\Data",
    "PROJECT_PATH": r"D:\SearchProject",
    "LOG_PATH": r"D:\SearchProject\logs",
    "EMBED_DIM": "384",
    "RERANK_MODEL": "BAAI/bge-reranker-base",
}


def _env() -> Path:
    path = Path(tempfile.mkdtemp()) / ".env"
    path.write_text(
        "\n".join(f"{key}={value}" for key, value in WHOLE.items()) + "\n",
        encoding="utf-8")
    return path


def test_restore_defaults_cannot_delete_a_location():
    r"""**Exactly what the button sent**, against a whole `.env`."""
    path = _env()

    apply_values(path, {key: None for key in WHOLE})

    after = path.read_text(encoding="utf-8")
    missing = sorted(key for key in LOCATION_KEYS if f"{key}=" not in after)

    assert not missing, (
        f"a reset removed {missing} - the application cannot start without them")


def test_unpinning_a_key_that_has_a_default_still_works():
    """The mechanism must survive the guard, or the guard has broken the feature.

    `RERANK_MODEL` is the reason removal exists: pinned by an old installer to a
    model 9.2x slower than the code's own choice, and unreachable by any later
    improvement until the line goes.
    """
    path = _env()

    apply_values(path, {"RERANK_MODEL": None})

    assert "RERANK_MODEL" not in path.read_text(encoding="utf-8")


def test_every_location_key_is_protected():
    """Named, because most of them are in no registry to be derived from."""
    guarded = protected()

    for key in LOCATION_KEYS:
        assert key in guarded, f"{key} can be deleted from .env"


def test_a_protected_key_can_still_be_changed():
    """Guarded against *removal*, not against being set - moving an index is a
    real operation and it rewrites exactly these keys."""
    path = _env()

    apply_values(path, {"DATA_PATH": r"E:\Elsewhere"})

    assert r"DATA_PATH=E:\Elsewhere" in path.read_text(encoding="utf-8")


def test_the_restore_button_never_offers_a_location():
    """The other half: the caller must not even list them as removable."""
    from app.ui.widgets.defaults import pinned_in

    offered = set(pinned_in(_env()))

    assert not (offered & LOCATION_KEYS), (
        f"restore-defaults offers to remove {sorted(offered & LOCATION_KEYS)}")
