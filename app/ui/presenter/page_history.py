r"""Back and forward between pages. Qt-free.

Layer: L5 (presenter half)

IA-10 of the UX review (2026-10-14): a search result opens a contact, a sync line
opens an account - and there was no way back to where somebody came from. The
window keeps a browser's history of the pages it showed: Alt+Left and the mouse's
back button go back, Alt+Right and its forward button go forward.

**A page and a simple sub-state.** A place is `(page, sub)`: the page's rail title
and one short string the page can be put back to - the Settings category, or
"naming" for the Photos naming page. The sub-state of the place being left is
written into it as it is left (`leave`), so going back returns to the page as it
was left, not as it was first opened.

**Capped.** The oldest place is forgotten past `CAP`, so a day of clicking costs a
few hundred bytes. Going back and then somewhere new drops the forward places, as
every browser does. Visiting the place already current records nothing.
"""

from __future__ import annotations

from typing import Optional

__all__ = ["CAP", "Place", "PageHistory"]

#: The most places remembered.
CAP = 50

#: `(page title, sub-state)`.
Place = tuple


class PageHistory:
    """The places visited, and where in them we are."""

    def __init__(self, cap: int = CAP) -> None:
        self.cap = max(2, int(cap))
        self._places: list = []
        self._at = -1

    # -- reading -------------------------------------------------------------------

    @property
    def current(self) -> Optional[Place]:
        return self._places[self._at] if 0 <= self._at < len(self._places) else None

    def can_go_back(self) -> bool:
        return self._at > 0

    def can_go_forward(self) -> bool:
        return 0 <= self._at < len(self._places) - 1

    def places(self) -> list:
        return list(self._places)

    # -- writing -------------------------------------------------------------------

    def leave(self, sub: str) -> None:
        """The current place is being left showing `sub`: remember that for coming back."""
        if self.current is not None:
            self._places[self._at] = (self.current[0], str(sub or ""))

    def visit(self, page: str, sub: str = "") -> bool:
        """`page` was opened by somebody (not by back or forward). True when recorded."""
        if not page:
            return False
        place = (str(page), str(sub or ""))
        if self.current is not None and self.current[0] == place[0]:
            self._places[self._at] = place       # the same page: only its sub-state moved
            return False
        del self._places[self._at + 1:]
        self._places.append(place)
        if len(self._places) > self.cap:
            del self._places[:len(self._places) - self.cap]
        self._at = len(self._places) - 1
        return True

    def back(self) -> Optional[Place]:
        """Step back; the place to show, or None at the start."""
        if not self.can_go_back():
            return None
        self._at -= 1
        return self.current

    def forward(self) -> Optional[Place]:
        """Step forward; the place to show, or None at the end."""
        if not self.can_go_forward():
            return None
        self._at += 1
        return self.current
