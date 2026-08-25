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

__all__ = ["build_input", "build_scope", "build_interpret", "build_rerank", "SCOPES"]


def build_input(parent: Optional[QWidget], on_typed: Any, on_submit: Any) -> Any:
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
    box.setPlaceholderText("Search…    press / for filters")
    box.setClearButtonEnabled(True)
    box.textChanged.connect(on_typed)
    box.returnPressed.connect(on_submit)
    return box, attach_to(box)

#: (label, value). Value travels into `ParsedQuery.scope`.
#:
#: A filter, not a mode: nobody should have to decide whether a thing was an
#: email or a document *before* typing, because the usual answer is "I do not
#: remember, that is why I am searching".
SCOPES: tuple[tuple[str, str], ...] = (
    ("Everything", "all"),
    ("Mail only", "mail"),
    ("Documents only", "documents"),
)


def build_scope(parent: Optional[QWidget], on_change: Any) -> QComboBox:
    """The scope chips."""
    scope = QComboBox(parent)
    for label, value in SCOPES:
        scope.addItem(label, value)
    scope.setToolTip(
        "Narrow the search to mail or to files on disk.\n"
        "Mail results show who sent it and when instead of a file path."
    )
    scope.currentIndexChanged.connect(on_change)
    return scope


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
    """The rerank toggle.

    On by default because it is what makes the top three results worth reading.
    Offered at all because it is the single biggest cost in the pipeline, and
    somebody on a slow machine should be able to trade precision for speed
    without editing configuration or restarting.
    """
    toggle = QCheckBox("Rerank", parent)
    toggle.setToolTip(
        "Slower but more precise ordering. Turning it off does not need a restart."
    )
    toggle.setChecked(True)
    toggle.stateChanged.connect(on_change)
    return toggle
