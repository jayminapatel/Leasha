r"""The search box's repository half, started on a worker.

Layer: L5

`app/search/federate.py` decides what a repository search *returns*;
`presenter.git_pass` decides *whether* to run one and what to say while it does.
This is the third and smallest part: starting it.

**It is here rather than in `search_view.py` for a rule, not a preference.**
`test_every_qt_view_keeps_its_logic_in_the_presenter` holds every view under 250
lines, and `search_view.py` stood at 247 before this feature. Three lines of
headroom is the guard saying that the next thing added to that file belongs
somewhere else - so this follows `widgets/interpret.py`, which exists for
exactly the same reason and has the same shape: a free function taking the
pieces it needs and two callbacks, so it can be tested without a window.

The parts that are decisions - which switches make a search slow, where the
ranks continue from, what the status line reads - are all in the presenter and
none of them are repeated here.
"""

from __future__ import annotations

from typing import Any, Callable

__all__ = ["run_history_pass"]


def run_history_pass(
    *,
    store: Any,
    query: str,
    tier: str,
    shown: int,
    pool: Any,
    set_status: Callable[[str], None],
    on_rows: Callable[[Any], None],
) -> bool:
    """Start the repository search if this query asked for one. False if not.

    **Off the UI thread, and only on the full tier.** `git log -S` walks every
    commit it is given; the first non-negotiable in this application is that no
    unbounded work sits behind a keystroke, and `presenter.git_pass` is what
    enforces the tier so that this function has no opinion of its own about it.

    A second worker rather than part of the index search, so results appear at
    their usual speed and history arrives when it arrives. Waiting for git
    before showing anything would make every history search look like a hang.
    """
    from app.ui.presenter import git_pass
    from app.ui.workers import CallableWorker, run

    plan = git_pass(query, tier, shown=shown)
    if not plan.wanted or store is None:
        return False

    set_status(plan.status)
    base = plan.rank_base

    def work() -> Any:
        """Worker body: the git half of the search, imported only here (see below)."""
        # Imported inside the worker. `gitsearch` is the only thing in this
        # application that forks a process, and the modules on the typing path
        # are forbidden from importing it - see `test_nothing_that_runs_on_a_
        # keystroke_imports_this`. Deferring it is what makes that true rather
        # than a technicality. `git_results` (2026-10-04) is the history step
        # `app.search.run.run_search` takes for the command line and MCP too.
        from app.search.run import git_results

        return git_results(store, query, start_rank=base)

    worker = CallableWorker(work, component="ui.search.git")
    worker.signals.finished.connect(on_rows)
    # A repository that has been moved, or a `git` that is not installed, is not
    # a failed search: the index half already answered. This half says nothing
    # rather than raising a dialog over results somebody is reading.
    worker.signals.failed.connect(lambda _error: None)
    run(pool, worker)
    return True
