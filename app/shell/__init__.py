"""`leasha shell` — an interactive session with a real dropdown.

Layer: L5-adjacent. A terminal front end over the same presenter the window
uses, which is the rule this package lives under: it makes no decision of its
own. Anything it needs that the presenter does not offer is added to the
presenter, where it is tested without a terminal — exactly as the Qt views are.
"""

from __future__ import annotations

__all__ = ["completer", "repl"]
