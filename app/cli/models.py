r"""`models`: every model Leasha needs - which are here, and downloading them.

Layer: L1 (`app.core.model_catalogue`; the only I/O is the model folder)

    python -m app.cli models list
    python -m app.cli models list --json
    python -m app.cli models download search rerank
    python -m app.cli models download all

The command line half of Settings, Models' download buttons (owner,
2026-10-08), and what the installer runs for each model ticked: `leasha-cli.exe
models download search`. The list, its order and its names come from
`app.core.model_catalogue`, so the three always agree.

`download` exits 0 when every model asked for is here at the end - downloaded
now or already - and 1 otherwise, with the sentence why for each one that is
not. A model already here is not fetched again, so running it twice is safe.
"""

from __future__ import annotations

import argparse
import json

from app.cli._common import EXIT_ERROR, EXIT_OK, _load


def _size(mb: int) -> str:
    """The published size in the words the installer's task list uses."""
    return f"about {mb / 1024:.1f} GB" if mb >= 1024 else f"about {mb} MB"


def cmd_models(args: argparse.Namespace) -> int:
    """`models list` (the default when no action is given) or `models download`."""
    from app.core import model_catalogue

    settings = _load(args)
    action = getattr(args, "models_action", None) or "list"
    if action == "list":
        return _list(model_catalogue, settings, bool(getattr(args, "json", False)))
    return _download(model_catalogue, settings, list(args.keys),
                     bool(getattr(args, "json", False)))


def _list(catalogue, settings, as_json: bool) -> int:
    """Each needed model with here/missing. Always exits 0: a missing model is
    a fact about this computer, not a failure of the listing."""
    rows = []
    for model in catalogue.needed_models(settings):
        here = catalogue.is_present(model.key, settings)
        rows.append((model, here))
    if as_json:
        print(json.dumps([{
            "key": m.key, "title": m.title, "purpose": m.purpose, "model": m.model,
            "approx_mb": m.approx_mb, "install_default": m.install_default,
            "optional_note": m.optional_note, "present": here,
        } for m, here in rows], indent=2))
        return EXIT_OK
    width = max(len(m.key) for m, _ in rows)
    for model, here in rows:
        state = "here" if here else "missing"
        print(f"{model.key:<{width}}  {state:<7}  {model.title} - {model.model} "
              f"({_size(model.approx_mb)})")
        if model.optional_note:
            print(f"{'':<{width}}           {model.optional_note}")
    missing = [m.key for m, here in rows if not here]
    print()
    if missing:
        print(f"Missing: {', '.join(missing)}. Download them with: "
              f"models download {' '.join(missing)}   (or: models download all)")
    else:
        print("Every model Leasha needs is on this computer.")
    return EXIT_OK


def _download(catalogue, settings, keys: list[str], as_json: bool) -> int:
    """Fetch the models named (or `all`). Exit 0 only when every one asked for
    is present afterwards - checked on disk, not inferred from the download's
    own report, because the installer's model step once ran to completion and
    left nothing behind (HANDOFF, 2026-10-08: no `.env` yet, so no model folder)."""
    # "photo_tags" as well as "photo-tags": the installer's task has to be called
    # models\photo_tags, since Inno allows no "-" in a task name.
    asked = [k.strip().lower().replace("_", "-") for k in keys]
    wanted = list(catalogue.KEYS) if "all" in asked else list(dict.fromkeys(asked))
    unknown = [k for k in wanted if k not in catalogue.KEYS]
    if unknown:
        print(f"Not a model Leasha needs: {', '.join(unknown)}. "
              f"Choose from: {', '.join(catalogue.KEYS)}, or all.")
        return EXIT_ERROR

    say = (lambda _text: None) if as_json else _progress_printer()
    results = catalogue.download_all(settings, on_progress=say, keys=wanted)
    # "Everything asked for is here at the end", checked rather than inferred.
    here = {key: catalogue.is_present(key, settings) for key in wanted}
    if as_json:
        print(json.dumps({"results": results, "present": here}, indent=2))
    else:
        print()
        for key in wanted:
            print(f"{key}: {results.get(key, '')}")
    not_here = [key for key in wanted if not here[key]]
    if not not_here:
        if not as_json:
            print("Everything asked for is on this computer.")
        return EXIT_OK
    if not as_json:
        print(f"Not on this computer: {', '.join(not_here)}. Settings, Models can "
              f"download them later, or run this again.")
    return EXIT_ERROR


def _progress_printer():
    """Each progress sentence once: a download repeats its line every half second,
    and a console (or the installer's log) wants a line only when it changes."""
    last = [""]

    def say(text: str) -> None:
        if text != last[0]:
            last[0] = text
            print(text, flush=True)

    return say


def add_models_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p = sub.add_parser("models", parents=[common],
                       help="every model Leasha needs: which are here, and downloading them")
    actions = p.add_subparsers(dest="models_action")
    actions.add_parser("list", parents=[common],
                       help="each model: key, what it is for, size, here or missing")
    d = actions.add_parser("download", parents=[common],
                           help="download models by key (search, rerank, pictures, "
                                "photo-tags, speech, chat, faces), or all")
    d.add_argument("keys", nargs="+", metavar="KEY",
                   help="one or more keys, or all")
    p.set_defaults(func=cmd_models)
