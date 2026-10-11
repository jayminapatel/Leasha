r"""What the owner has marked on the Tidy page, and the rules that mark it. Qt-free.

Layer: L5 presenter. Order 1q D4 (2026-10-11).

Marks are paths, kept across sets and kinds until **Review and remove...**. A set is
*settled* once it has been marked - by the keep rule the first time it is shown, or
by hand - and a rule never marks a settled set again; only choosing a rule redoes
the set on screen. "Leasha picks" on similar photos needs each photo's pixels and
sharpness, read on a worker (`needs_quality`, then `set_quality`).
"""

from __future__ import annotations

from typing import Any, Iterable

from app.tidy import keep
from app.tidy.remove import Plan, make_plan

__all__ = ["TidyMarks"]


class TidyMarks:
    """The page's marks. Every method is plain Python, so the tests need no window."""

    def __init__(self, rule: str = keep.DEFAULT_RULE) -> None:
        self.rule = rule if rule in keep.RULES else keep.DEFAULT_RULE
        self.marked: set[str] = set()
        self.settled: set[str] = set()
        self.quality: dict[str, dict] = {}

    def needs_quality(self, tset: Any) -> bool:
        """True when the rule cannot mark this set until its photos have been read."""
        return (tset.key not in self.settled and tset.kind == "similar" and self.rule == "leasha"
                and tset.key not in self.quality)

    def set_quality(self, key: str, quality: dict) -> None:
        self.quality[key] = quality

    def apply_rule(self, tset: Any) -> bool:
        """Mark a new set by the rule. False when it is settled or waits for its photos."""
        if tset.key in self.settled or self.needs_quality(tset):
            return False
        self.marked |= keep.marked_by_rule(tset, self.rule, self.quality.get(tset.key))
        self.settled.add(tset.key)
        return True

    def settle(self, sets: Iterable[Any]) -> None:
        """These sets are by hand from the start (D5): no rule marks them."""
        self.settled.update(s.key for s in sets)

    def toggle(self, tset: Any, path: str, on: bool) -> None:
        if tset is not None:
            self.settled.add(tset.key)
        (self.marked.add if on else self.marked.discard)(path)

    def keep_only(self, tset: Any, path: str) -> None:
        """Keep `path`; mark the rest of its set."""
        self.settled.add(tset.key)
        for f in tset.files:
            (self.marked.discard if f.path == path else self.marked.add)(f.path)

    def choose_rule(self, rule: str, current: Any = None) -> None:
        """A new rule: remembered, and the set on screen marked again by it."""
        self.rule = rule if rule in keep.RULES else self.rule
        if current is not None:
            for f in current.files:
                self.marked.discard(f.path)
            self.settled.discard(current.key)
            self.apply_rule(current)

    def mark_all(self, sets: Iterable[Any]) -> None:
        """"Mark every set by this rule" - each set not yet settled."""
        for tset in sets:
            self.apply_rule(tset)

    def keep_only_live(self, live: set[str]) -> None:
        """After a reload: a file that went, or changed, is no longer marked."""
        self.marked &= live

    def plan(self, sets: Iterable[Any]) -> Plan:
        return make_plan(sets, self.marked)

    def forget(self, paths: Iterable[str]) -> None:
        self.marked -= set(paths)
