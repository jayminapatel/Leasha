r"""`leasha shell` — the interactive session, and the only real dropdown a
terminal can have.

Layer: L5-adjacent.

**Why a session rather than a completer script.** A shell prompt is owned by
the shell: no `Register-ArgumentCompleter` block can draw a menu that follows
the keystrokes, and the tab completer is bounded by what a script block can do.
prompt_toolkit draws exactly the asked-for thing, and a persistent process with
the store already open removes every constraint the tab completer lives under -
no sidecar, no cold-start budget, and scoping works because the parser is right
there.

**It decides nothing.** Every rule comes from the presenter, every result is
printed by the renderers `app.cli search` already uses, and every filter is
parsed by `parse_query`. A second renderer or a second grammar in here would be
the drift this order exists to prevent, so there is neither.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = ["run_shell", "history_path", "toolbar_text"]

#: What the session prints before the first prompt. Short: somebody who typed
#: `leasha shell` wants a prompt, not a manual.
BANNER = (
    "Leasha. Type to search, `/` for filters, Tab to pick.\n"
    "`/help` for the filter list, Ctrl+D to leave."
)

PROMPT = "leasha> "


def history_path(settings: Any) -> Path:
    """Where the session's history lives: **beside the index, not the repo.**

    The index is the thing this session is about, and a repository that gets
    re-cloned or a project folder that gets rebuilt by the installer must not
    take somebody's history with it.
    """
    data = getattr(settings, "data_path", None) or Path.cwd()
    return Path(data) / "shell_history"


def toolbar_text(completer: Any, last: Any = None,
                 document: Any = None) -> str:
    """The bottom line: what this filter wants, and anything degraded.

    The CLI's notice bar. One line, never an interruption - the same rule the
    window's notice bar follows, for the same reason: a modal over a search
    somebody is still typing is worse than the thing it warns about.
    """
    parts: list[str] = []

    note = str(getattr(completer, "note", "") or "")
    if note:
        parts.append(note)

    if document is not None:
        from app.search.commands import command_for
        from app.ui.presenter import resolved_date, slash_context

        _head, mode, partial, _context = slash_context(
            document.text_before_cursor)
        if mode == "value":
            name = document.text_before_cursor.rpartition(" ")[2].partition(":")[0]
            command = command_for(name)
            if command is not None:
                parts.append(f"{name}: {command.value_hint}")
                if getattr(command, "is_date", False) and partial:
                    resolved = resolved_date(partial)
                    if resolved:
                        parts.append(resolved)

    health = _health(last)
    if health:
        parts.append(health)

    return "  ·  ".join(parts)


def _health(last: Any) -> str:
    """The index-health notice, from the **last response**. Never raises.

    `semantic_health` reads a response, not a store: the condition it detects
    is keyword results arriving with no vector results, which is a fact about
    one search rather than about the index sitting there. So the toolbar can
    only say it once a search has run, which is also the first moment somebody
    could be misled by it.
    """
    if last is None:
        return ""
    try:
        from app.ui.presenter import semantic_health

        return str(semantic_health(last) or "")
    except Exception:                            # noqa: BLE001 - a toolbar
        return ""


def run_shell(engine: Any, settings: Any, *, renderer: Any = None,
              session: Any = None) -> int:
    """The session. Returns an exit code.

    `renderer` and `session` are injectable so the loop can be driven by a test
    without a terminal - the same reason the presenter exists at all.
    """
    from prompt_toolkit import PromptSession
    from prompt_toolkit.auto_suggest import AutoSuggestFromHistory
    from prompt_toolkit.completion import ThreadedCompleter
    from prompt_toolkit.history import FileHistory

    from app.shell.completer import LeashaCompleter

    completer = LeashaCompleter(getattr(engine, "store", None))

    if session is None:
        history = history_path(settings)
        history.parent.mkdir(parents=True, exist_ok=True)
        session = PromptSession(
            PROMPT,
            # **Threaded, so a store read never blocks a keystroke.** The same
            # non-negotiable the Qt popup keeps with its worker.
            completer=ThreadedCompleter(completer),
            complete_while_typing=True,
            history=FileHistory(str(history)),
            auto_suggest=AutoSuggestFromHistory(),
            bottom_toolbar=lambda: toolbar_text(
                completer, getattr(completer, "last_response", None),
                session.default_buffer.document),
        )

    print(BANNER)
    while True:
        try:
            line = session.prompt()
        except KeyboardInterrupt:
            # **Ctrl+C clears the line; it never kills the session.** Losing a
            # session to a mistyped key is the thing that stops people using a
            # REPL at all.
            continue
        except EOFError:
            return 0

        text = (line or "").strip()
        if not text:
            continue
        if text in ("/quit", "/exit"):
            return 0
        if text == "/help":
            _print_help()
            continue

        last = _run_one(engine, text, renderer)
        completer.last_response = last
    return 0


def _print_help() -> None:
    """The catalogue's own help, not a second copy of it."""
    from app.search.commands import help_lines

    for line in help_lines():
        print(line)


def _run_one(engine: Any, text: str, renderer: Any = None) -> Any:
    """One search, printed by the CLI's own renderer. **Never raises.**

    A session that dies on one bad search is a session nobody trusts with a
    long one.
    """
    if renderer is None:
        from app.cli import print_response as renderer

    try:
        response = engine.search(text)
    except Exception as exc:                     # noqa: BLE001 - see docstring
        print(f"That search could not be run: {exc}")
        return None
    try:
        renderer(response, text)
    except Exception as exc:                     # noqa: BLE001
        print(f"The results could not be printed: {exc}")
    return response
