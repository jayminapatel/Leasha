"""The controls beside the search box: scope, Interpret, rerank, view.

Layer: L5

Its own module for the reason `indexing_settings.py` and `model_box.py` are:
`search_view.py` had grown past the length a view is allowed to be, and the rule
that keeps views short is the rule that keeps logic out of them. Four controls
with four tooltips is sixty lines of construction that says nothing about how
searching works.

**Every tooltip here is load-bearing.** Each of these controls changes what a
search does, and none of them is self-explanatory from its label — "Rerank" in
particular is a word nobody outside this project has met. A control whose effect
you cannot predict is a control people stop touching.
"""

from __future__ import annotations

from typing import Any, Optional

from PyQt6.QtWidgets import QCheckBox, QComboBox, QPushButton, QWidget

__all__ = [
    "build_input", "build_scope", "build_interpret", "build_rerank",
    "build_controls", "build_toolbar", "SCOPES",
]


def build_input(parent: Optional[QWidget], on_typed: Any, on_submit: Any,
                store: Any = None) -> Any:
    """The search box, with the `/` filter dropdown attached.

    Returns `(line_edit, completer)`. The completer must be kept alive by the
    caller: a `QCompleter` that is garbage collected stops completing, silently,
    and the dropdown would simply never appear again.

    **The placeholder advertises the doorway, not the grammar.** Every filter
    worked since Layer 4 and nothing in the application had ever mentioned them,
    so the box was in practice a bag of words. Five operators do not fit in a
    placeholder and nobody read them when they were there; `/` does fit, and it
    is the convention every chat tool has already taught people.
    """
    from PyQt6.QtWidgets import QLineEdit

    from app.ui.widgets.command_popup import attach_to

    box = QLineEdit(parent)
    # **The thinnest placeholder in the application, on the box that most needs
    # a good one.** "press / for filters" says a menu exists and nothing about
    # what is in it; Files, Mail and Code all name real examples, and this is
    # the box somebody meets first. Naming three switches from three different
    # sources is also the only thing on screen that says the box reaches mail
    # and repositories at all.
    box.setPlaceholderText(
        "Search everything — files, mail and code.  "
        "Or / for filters: /type pdf  /from dave  /newest")
    box.setClearButtonEnabled(True)
    box.textChanged.connect(on_typed)
    box.returnPressed.connect(on_submit)
    # **Every switch there is, index and repository alike.** Asked for as
    # *"Search should have all switches"*, and honest only because the box can
    # now answer the repository ones: `app/search/federate.py` runs them, on the
    # full tier, over the repositories in the index. Offering them before that
    # existed would have been the failure this application keeps finding - a
    # menu row that quietly does nothing.
    #
    # The same list the Code tab uses. One catalogue, because the two boxes now
    # reach the same two engines and a second list would only be somewhere for
    # them to disagree.
    from app.ui.widgets.code_commands import (
        ALL_CATALOGUE, catalogue_command_for, catalogue_matching,
    )

    # `store` only makes the *value* half of the menu better - which extensions
    # exist, who has sent mail, which repositories were found. Without it the
    # grammar's own values are still offered, so a box with no store is not a
    # box with a broken menu.
    #
    # No `lookup`: the Code tab can offer real branch and author names because
    # it knows which repository is selected, and this box does not. The grammar's
    # own suggestions still appear, which is the honest half of that menu.
    return box, attach_to(
        box, store=store, catalogue=ALL_CATALOGUE,
        matcher=catalogue_matching, resolve=catalogue_command_for,
    )

#: (label, value). Value travels into `ParsedQuery.scope`.
#:
#: A filter, not a mode: nobody should have to decide whether a thing was an
#: email or a document *before* typing, because the usual answer is "I do not
#: remember, that is why I am searching".
SCOPES: tuple[tuple[str, str], ...] = (
    ("Everything", "all"),
    ("Mail only", "mail"),
    ("Documents only", "documents"),
    ("Code only", "code"),
)


def build_scope(parent: Optional[QWidget], on_change: Any) -> QComboBox:
    """The scope chips.

    **"Everything" is a union, not a fallback.** This box narrows; it never
    adds. Whatever a focused tab can find, the search box finds too - so a
    filter learned anywhere works here, and there is never a reason to go to
    another tab to ask a question this one cannot.
    """
    scope = QComboBox(parent)
    for label, value in SCOPES:
        scope.addItem(label, value)
    scope.setToolTip(
        "Narrow the search. Everything searches all of it.\n\n"
        "Mail only — messages, showing who sent them and when instead of a path.\n"
        "Documents only — files on disk that are not in a repository.\n"
        "Code only — anything inside a code repository, whatever its type:\n"
        "a README in a repository counts, a .py file in Downloads does not.\n\n"
        "For 'files that look like code' wherever they are, type type:code instead."
    )
    scope.currentIndexChanged.connect(on_change)
    return scope


def scope_value(scope: QComboBox) -> str:
    """The selected scope, defaulting to everything."""
    return str(scope.currentData() or "all")


def select_scope(scope: QComboBox, value: str) -> None:
    """Select a scope by value. An unknown value is ignored.

    Ignored rather than raised: this is driven across a tab boundary with a
    plain string - the Code tab asks for `code` when it hands a repository to
    the search box - and a stale caller must not be able to close the window.
    """
    index = scope.findData(value)
    if index >= 0:
        scope.setCurrentIndex(index)


def build_interpret(parent: Optional[QWidget], on_click: Any) -> QPushButton:
    """The Interpret button.

    **Explicit, never automatic.** Plain Enter runs what was typed, exactly as
    it always has; this button is the person choosing to spend a second on a
    model. Silently interpreting every search would make results unpredictable,
    and unpredictable search over your own archive is worse than blunt search,
    because you stop trusting it.

    Hidden entirely when interpretation is switched off — see
    `SearchView.set_interpret_enabled`. A greyed-out button is a permanent
    question with no answer visible on the screen it appears on.
    """
    button = QPushButton("Interpret", parent)
    button.setToolTip(
        "Turn a sentence into a search query using a local model.  Ctrl+Enter\n\n"
        "The query it builds goes into the box so you can read and edit it.\n"
        "If the model is unavailable, your words are searched for unchanged."
    )
    button.clicked.connect(on_click)
    return button


def build_rerank(parent: Optional[QWidget], on_change: Any) -> QCheckBox:
    r"""The rerank toggle.

    On by default because it is what makes the top three results worth reading.
    Offered at all because it is the single biggest cost in the pipeline, and
    somebody on a slow machine should be able to trade precision for speed
    without editing configuration or restarting.

    **It is not the source of truth, and pretending otherwise made it lie.**
    This box was hard-coded checked and never persisted, while the Settings
    checkbox beside it *was* persisted - so somebody who turned reranking off in
    Settings came back to a toolbar claiming it was on, and every search from
    that box asked for it again. Two controls for one setting that never agreed:
    the same shape as the bug fixed in `set_message` the day before.
    `shell` now initialises both from `ui:rerank_enabled` and routes both
    through one handler.
    """
    toggle = QCheckBox("Rerank", parent)
    toggle.setToolTip(
        "Slower but more precise ordering. Turning it off does not need a "
        "restart, and it is the same setting as the one on the Settings page."
    )
    toggle.setChecked(True)
    toggle.stateChanged.connect(on_change)
    return toggle


def build_toolbar(view: Any, *, controls: Any, status: Any, body: Any) -> Any:
    r"""Lay the search page out: the control row, the status line, the results.

    **Assembly, not decision** - and here because this module is already "the
    search bar and its controls", while `search_view.py` is held under 250 lines
    by `test_every_qt_view_keeps_its_logic_in_the_presenter`. That guard fired
    when the repository half was added, and it was right to: a view at its
    ceiling is one where the next feature has nowhere to go, and the answer is
    to move what was never view logic rather than to keep squeezing.

    `controls` is the row of widgets to the right of the box, in display order.
    Returns the notice bar, which the view needs to write degradations into.
    """
    from PyQt6.QtWidgets import QHBoxLayout, QVBoxLayout

    from app.ui.widgets.notice_bar import NoticeBar

    notices = NoticeBar(view)
    top = QHBoxLayout()
    top.addWidget(view.input, stretch=1)
    for widget in controls:
        top.addWidget(widget)

    layout = QVBoxLayout(view)
    layout.addLayout(top)
    layout.addWidget(status)
    # Above the results and below the status line: a degradation is about the
    # results, so it belongs where the eye lands before reading them.
    layout.addWidget(notices)
    layout.addWidget(body, stretch=1)
    return notices


def build_controls(view: Any, *, on_scope: Any, on_interpret: Any,
                   on_rerank: Any, on_view: Any) -> tuple:
    r"""The four controls to the right of the box, and the status line.

    Returns `(scope, interpret, rerank, view_button, status)`.

    **The keyboard shortcuts are built here too**, deliberately: `Ctrl+Enter` is
    the Interpret button by another name, and a shortcut that lives apart from
    the control it duplicates is how the two come to disagree about whether the
    feature is switched on.

    The view button carries the results pane's own text size and spacing.
    Results are the one place in this window people *read* rather than scan, and
    the size that suits a paragraph of snippet is not the size that suits a
    toolbar - so it is this pane's setting rather than an application-wide zoom.
    No columns: a result is not a table.
    """
    from PyQt6.QtGui import QKeySequence, QShortcut
    from PyQt6.QtWidgets import QLabel

    from app.ui.view_options import button as view_button

    scope = build_scope(view, on_scope)
    interpret = build_interpret(view, lambda _checked=False: on_interpret())
    rerank = build_rerank(view, lambda _state: on_rerank())
    # **Gated on the button being visible.** Interpret is hidden when Ollama is
    # switched off or absent - `set_interpret_enabled` - and the shortcut was
    # not, so Ctrl+Enter on a machine with no Ollama started a translation that
    # could only fail, from a feature the person had switched off or never had.
    # `interpret.isVisible()` is the same condition the button is drawn by, so
    # the two cannot drift apart.
    for keys in ("Ctrl+Return", "Ctrl+Enter"):
        QShortcut(QKeySequence(keys), view,
                  activated=lambda: on_interpret() if interpret.isVisible() else None)

    chooser = view_button(view, None, "", on_change=on_view, grouping=True)
    status = QLabel("")
    status.setObjectName("searchStatus")
    return scope, interpret, rerank, chooser, status
