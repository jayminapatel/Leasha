r"""The `leasha shell` completer — the same grammar the window uses.

Layer: L5-adjacent — a terminal front end over the presenter.

**One grammar in one language.** Every decision here is made by a function the
Qt popup also calls: `slash_context` decides whether a menu opens at all,
`value_suggestions` decides what is in it, `value_rows` decides how a row
reads. This module holds no rules of its own. That is the point of it - the
`D:/docs` and `12/03` cases stay inert in the terminal for the same tested
reason they stay inert in the window, rather than for a second reason written
in a second place that agrees today.

**Nothing from `app/ui/widgets/` is imported here**, and a test asserts it. The
presenter is Qt-free on purpose; a terminal reaching into a widget module would
make that untrue by accident.

Scoping works, which is the whole reason the REPL exists alongside the tab
completer: this is a persistent process with the store open, so the settled
tokens can be parsed by the real parser and passed down, and there is no cold
start to budget for.
"""

from __future__ import annotations

from typing import Any, Iterable, Optional

from prompt_toolkit.completion import Completer, Completion

from app.ui.presenter import (
    ALL_VALUES_NOTE,
    slash_context,
    value_rows,
    value_suggestions,
)

__all__ = ["LeashaCompleter", "command_rows"]


def command_rows(partial: str, catalogue: Any = None) -> list[tuple[str, str]]:
    """`(name, description)` for the command menu, from the catalogue.

    The glyph rides in front of the description rather than the text, because
    what gets inserted must be exactly the token - a completion carrying an
    icon would put the icon in the query.
    """
    if catalogue is None:
        from app.search.commands import COMMANDS as catalogue

    wanted = str(partial or "").strip().lower()
    rows = []
    for command in catalogue:
        if wanted and not any(
                spelling.lower().startswith(wanted)
                for spelling in command.spellings):
            continue
        rows.append((command.name, f"{command.icon}  {command.summary}"))
    return rows


class LeashaCompleter(Completer):
    """Commands after `/`, values after `name:`, and nothing anywhere else.

    Wrap in prompt_toolkit's `ThreadedCompleter` so a store read never blocks a
    keystroke - the same non-negotiable the Qt popup keeps with its worker,
    kept here by the same means. `leasha shell` does that wrapping; this class
    stays synchronous so it can be tested by calling it.
    """

    def __init__(self, store: Any = None, *, resolve: Any = None,
                 lookup: Any = None, limit: int = 40) -> None:
        self._store = store
        self._resolve = resolve
        self._lookup = lookup
        self._limit = limit
        #: The last menu's note, for the toolbar to render. `1e`'s "(all)"
        #: marker reaches a terminal the same way it reaches the window.
        self.note: str = ""
        #: The last search's response, which is what `semantic_health` reads.
        #: Kept here rather than in the loop so the toolbar closure has one
        #: object to ask rather than a variable it would have to capture.
        self.last_response: Any = None

    # -- prompt_toolkit's one method ----------------------------------------

    def get_completions(self, document: Any, complete_event: Any = None
                        ) -> Iterable[Completion]:
        text = document.text_before_cursor
        head, mode, partial, context = slash_context(text, self._resolve)
        self.note = ""

        if mode == "command":
            for name, description in command_rows(partial, self._catalogue()):
                yield Completion(
                    f"{name}:", start_position=-len(partial) - 1,
                    display=f"/{name}", display_meta=description)
            return

        if mode != "value":
            return

        name = text.rpartition(" ")[2].partition(":")[0]
        yield from self._values(name, partial, context)

    # -- the value tier ------------------------------------------------------

    def _values(self, name: str, partial: str, context: Any
                ) -> Iterable[Completion]:
        notes: list[str] = []
        counts: dict = {}
        found = value_suggestions(
            self._store, name, partial, self._limit,
            resolve=self._resolve, lookup=self._lookup,
            context=context, notes=notes, counts=counts)
        if ALL_VALUES_NOTE in notes:
            self.note = "showing all values - nothing matched the filters typed"

        enriched = [counts.get(value, value) for value in found]
        rows = value_rows(name, enriched, resolve=self._resolve)
        summary = self._summary(name)

        for value, row in zip(found, rows):
            # **The row is what is read; the value is what is inserted.** A row
            # carries a count or a resolved date, and neither belongs in the
            # query - the same separation `value_for_row` keeps in the window.
            from app.ui.presenter import as_typed_value

            yield Completion(
                as_typed_value(value),
                start_position=-len(partial),
                display=row.rstrip(),
                display_meta=summary,
            )

    def _summary(self, name: str) -> str:
        """The catalogue's one-line summary, as the description column."""
        resolve = self._resolve
        if resolve is None:
            from app.search.commands import command_for as resolve
        command = resolve(name)
        return str(getattr(command, "summary", "") or "")

    def _catalogue(self) -> Optional[Any]:
        """Whatever `resolve` was built from, or the index's own list."""
        return None
