"""`open` and `completions`: the `leasha://` link and PowerShell tab completion.

Layer: L0 (desktop integration - `app.core.deeplink`, `app.core.osbridge.
startmenu` and the completer; no store layer is driven)

Everything here is per-user and needs no administrator rights: the registry
key, the Start-menu shortcut and the profile edit all live under the account
that runs it, so the installer can offer them without elevating.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.cli._common import EXIT_OK, _report
from app.core.config import load_settings, project_root
from app.core.errors import make_error


def cmd_open(args: argparse.Namespace) -> int:
    r"""Act on a `leasha://` link. Adoptions §7a.

    This is what Windows runs for a `leasha://search?q=...` URL, and it is
    **not a way to start the window**. If a window is already open, the query
    is left in `index_state` for it to pick up and this process exits; if one
    is not, the link is still recorded, so the next start runs it. Either way
    the person gets their search, which is the only thing they asked for.

    `register` and `unregister` write and remove the per-user scheme. Both are
    no-ops off Windows, and both say so rather than pretending.
    """
    from app.core.deeplink import (
        SCHEME,
        handover,
        parse,
        register,
        registry_values,
        unregister,
    )
    from app.core.deeplink import open_command as _open_command

    action = str(getattr(args, "url", "") or "").strip()

    if action == "register":
        target = str(getattr(args, "path", "") or "") or _launcher_path()
        if register(target):
            print(f"{SCHEME}:// links now open Leasha.")
            return EXIT_OK
        print(f"Could not register {SCHEME}:// links. "
              f"This only works on Windows. The command it would have "
              f"written is:\n  {_open_command(target)} \"%1\"")
        return EXIT_OK

    if action == "unregister":
        print(f"{SCHEME}:// links no longer open Leasha."
              if unregister() else
              f"Nothing to remove: {SCHEME}:// was not registered here.")
        return EXIT_OK

    if action == "show":
        for sub, value in registry_values(
                _open_command(_launcher_path())).items():
            print(f"{sub or '(default)':<20} {value}")
        return EXIT_OK

    request = parse(action)
    if request is None:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.open", key="url",
            reason=f"{action!r} is not a Leasha link",
            suggestion=(
                f"A link looks like {SCHEME}://search?q=safety%20report\n"
                f"  leasha open register     make Windows open these links\n"
                f"  leasha open unregister   stop it"),
        ), args.json)

    # `--env`, like every other command. This called `load_settings()` bare, so
    # `leasha --env X open ...` read the project's own `.env` and ignored the
    # option it was given (found 2026-09-20 by a test that had no `.env` at all).
    env_file = getattr(args, "env", None)
    settings = load_settings(Path(env_file) if env_file else None)
    from app.storage.sqlite_store import SqliteStore

    with SqliteStore(settings.fts_db) as store:
        handover(store, request)
    print(f"Searching for {request.query!r} in Leasha.")
    return EXIT_OK


def _launcher_path() -> str:
    """What Windows should run for a link: the installed `leasha.cmd`."""
    found = project_root() / "leasha.cmd"
    return str(found if found.exists() else Path(sys.executable))


def cmd_shortcut(args: argparse.Namespace) -> int:
    r"""Add or remove Leasha in the Start menu. `app.core.osbridge.startmenu`.

    `create` (the default) writes the per-user shortcut, `remove` deletes it,
    `show` prints what it holds. Off Windows, `create` says so rather than
    pretending.
    """
    from app.core.osbridge.startmenu import create, exists, remove, shortcut_path, shortcut_spec

    action = str(getattr(args, "action", "") or "create")
    root = project_root()

    if action == "remove":
        print("Removed Leasha from the Start menu." if remove() else
              "Nothing to remove: Leasha was not in the Start menu.")
        return EXIT_OK

    if action == "show":
        print(f"{'file':<20} {shortcut_path()} "
              f"({'present' if exists() else 'not there'})")
        for key, value in shortcut_spec(root).items():
            print(f"{key:<20} {value}")
        return EXIT_OK

    if create(root):
        print(f"Leasha is in the Start menu: {shortcut_path()}")
        return EXIT_OK
    print("Could not add Leasha to the Start menu. This only works on Windows, "
          f"from an installation with {shortcut_spec(root)['target']}.")
    return EXIT_OK


def cmd_completions(args: argparse.Namespace) -> int:
    r"""Emit or install the PowerShell tab completer.

    `--powershell` prints it; `install` appends a dot-source line to the
    profile and `install --remove` takes it out again - the contract
    `add-to-path.ps1` established, including the no-administrator-rights rule.

    Generated from the catalogue every time, so regenerating after a change to
    the filters is the whole update path.
    """
    from app.search.pwsh_completer import completer_script, install_into

    root = project_root()
    script = completer_script(project_path=root)

    if getattr(args, "action", "") != "install":
        print(script)
        return EXIT_OK

    target = Path(args.path).expanduser() if getattr(args, "path", "") else None
    if target is None:
        return _report(make_error(
            "ERR_CONFIG_INVALID", "cli.completions",
            key="--path",
            reason="the PowerShell profile to change was not given",
            suggestion=(
                "PowerShell knows where its own profile is. Run:\n"
                "  leasha completions install --path $PROFILE\n"
                "Add --remove to take it out again."),
        ), args.json)

    # **Written where the completer can find it, not into the profile.** A
    # profile holding the whole script would have to be edited again on every
    # catalogue change; a dot-source of a generated file does not.
    generated = root / "leasha-completions.ps1"
    generated.write_text(script, encoding="utf-8")

    updated = install_into(target, generated, remove=bool(args.remove))
    if updated is None:
        print("Nothing to change - "
              + ("it was not installed." if args.remove else "already installed."))
        return EXIT_OK

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(updated, encoding="utf-8")
    print(("Removed from " if args.remove else "Installed into ") + str(target))
    if not args.remove:
        print("Open a new PowerShell window, then type `leasha ` and press Tab.")
    return EXIT_OK


def add_open_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_open = sub.add_parser(
        "open", parents=[common],
        help="act on a leasha:// link, or register the scheme")
    p_open.add_argument(
        "url", nargs="?", default="",
        help=("the link, or one of: register, unregister, show"))
    p_open.add_argument(
        "--path", default="",
        help="what a link should run; defaults to this installation")
    p_open.set_defaults(func=cmd_open)


def add_shortcut_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_shortcut = sub.add_parser(
        "shortcut", parents=[common],
        help="add or remove Leasha in the Start menu")
    p_shortcut.add_argument(
        "action", nargs="?", default="create", choices=["create", "remove", "show"],
        help="create (the default), remove, or show what it holds")
    p_shortcut.set_defaults(func=cmd_shortcut)


def add_completions_parser(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    p_completions = sub.add_parser(
        "completions", parents=[common],
        help="tab completion for PowerShell")
    p_completions.add_argument(
        "action", nargs="?", default="", choices=["", "install"],
        help="omit to print the script; 'install' to add it to a profile")
    p_completions.add_argument(
        "--powershell", action="store_true",
        help="emit the PowerShell completer (the default and only shell today)")
    p_completions.add_argument(
        "--path", default="",
        help="the profile to change, normally $PROFILE")
    p_completions.add_argument(
        "--remove", action="store_true",
        help="take the completer back out of the profile")
    p_completions.set_defaults(func=cmd_completions)
