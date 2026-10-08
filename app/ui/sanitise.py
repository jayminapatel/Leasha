r"""Make fifteen-year-old email HTML safe to look at.

Layer: L5

**The leak this exists to stop is not script execution.** `QTextBrowser` does
not run JavaScript, so the obvious worry is already handled. What it *will* do
is fetch a remote image when the document asks for one - and a marketing email
from 2009 is full of tracking pixels that have been waiting fifteen years for
exactly that. Previewing a search result would phone home to whoever sent it,
silently, and "nothing leaves this machine" would stop being true the first time
somebody scrolled through a mailbox.

So every remote reference is removed **before the markup reaches the widget**.
Not disabled, not proxied - removed, because a widget that never sees a URL
cannot fetch one, and that is a property this module can be tested for.

**Allow-list, not block-list.** A list of dangerous tags is a list somebody has
to keep up to date with a specification that keeps growing; anything not on it
is permitted by default, which is the wrong default for input this old and this
untrusted. Here the opposite holds: tags are kept only if they are named, and
attributes only if they are named for that tag.

`QTextBrowser` renders a small subset of HTML anyway, so the allow-list costs
almost nothing in fidelity - most of what is stripped would not have rendered.

Deliberately Qt-free: this is the security boundary, and it must be testable on
any machine, in milliseconds, without a display.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from html.parser import HTMLParser

__all__ = ["Sanitised", "sanitise_email_html", "strip_to_text"]

#: Tags whose *contents* are dropped along with the tag. Everything else that is
#: not allowed is unwrapped, keeping the text inside it.
_DROP_ENTIRELY = frozenset({
    "script", "style", "iframe", "object", "embed", "applet", "frame",
    "frameset", "noscript", "template", "svg", "math", "head", "title",
})

#: Structure and emphasis that `QTextBrowser` actually renders.
_ALLOWED = frozenset({
    "p", "br", "div", "span", "b", "strong", "i", "em", "u", "s", "strike",
    "sub", "sup", "small", "big", "code", "pre", "kbd", "samp", "tt",
    "h1", "h2", "h3", "h4", "h5", "h6", "blockquote", "q", "cite",
    "ul", "ol", "li", "dl", "dt", "dd",
    "table", "thead", "tbody", "tfoot", "tr", "td", "th", "caption",
    "hr", "font", "center", "a", "img",
})

#: Attributes kept per tag. Anything else goes, which removes every `on*`
#: handler without needing to know their names - and there are dozens.
_ALLOWED_ATTRIBUTES: dict[str, frozenset[str]] = {
    "a": frozenset({"href", "title"}),
    "img": frozenset({"alt", "title", "width", "height"}),
    "td": frozenset({"colspan", "rowspan", "align", "valign"}),
    "th": frozenset({"colspan", "rowspan", "align", "valign"}),
    "table": frozenset({"border", "cellpadding", "cellspacing", "width"}),
    "font": frozenset({"color", "face", "size"}),
    "div": frozenset({"align"}),
    "p": frozenset({"align"}),
}

#: Schemes a link may keep. `cid:` is an email's own embedded image, which never
#: leaves the machine; `mailto:` opens a composer and fetches nothing.
_SAFE_LINK_SCHEMES = ("mailto:", "cid:", "#")

#: Tags that never have a closing tag, so the writer must not emit one.
_VOID = frozenset({"br", "hr", "img"})

_REMOTE = re.compile(r"^\s*(?:https?:|//|ftp:|file:)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Sanitised:
    """The safe markup, and what had to be taken out of it."""

    html: str
    #: Remote images found and removed. The pane says so, because an email that
    #: silently renders differently from how it was sent is confusing, and
    #: "images were blocked" is information the reader wants.
    blocked_images: int = 0
    #: Links to somewhere remote, left visible as text but not clickable.
    blocked_links: int = 0

    @property
    def blocked_anything(self) -> bool:
        return bool(self.blocked_images or self.blocked_links)

    def notice(self) -> str:
        """One line for the pane, or empty when nothing was removed."""
        if not self.blocked_anything:
            return ""
        parts = []
        if self.blocked_images:
            parts.append(
                f"{self.blocked_images} remote image"
                f"{'s' if self.blocked_images != 1 else ''} blocked"
            )
        if self.blocked_links:
            parts.append(
                f"{self.blocked_links} link{'s' if self.blocked_links != 1 else ''} "
                "disabled"
            )
        return " · ".join(parts) + " — nothing was fetched from the internet."


def _is_remote(value: str) -> bool:
    return bool(_REMOTE.match(value or ""))


class _Cleaner(HTMLParser):
    """Rebuilds the document from only what is allowed.

    Rebuilding rather than editing in place: a regex over markup this old meets
    unclosed tags, stray `<`, and attributes quoted three different ways, and
    the failure mode of getting it wrong is a tag surviving. A parser that emits
    only what it recognises cannot pass through what it did not understand.
    """

    def __init__(self) -> None:
        """Fresh parts and counters; the parser is single-use."""
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.blocked_images = 0
        self.blocked_links = 0
        self._suppress = 0          # depth inside a dropped subtree

    # -- tags ---------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs) -> None:
        """Emit the tag only if allowed, with its attributes filtered; drop subtrees."""
        if tag in _DROP_ENTIRELY:
            self._suppress += 1
            return
        if self._suppress or tag not in _ALLOWED:
            return                  # unwrapped: the text inside still arrives

        kept = self._attributes(tag, dict(attrs))
        if kept is None:
            return                  # the tag itself was the problem - drop it

        rendered = "".join(f' {name}="{value}"' for name, value in kept.items())
        self.parts.append(f"<{tag}{rendered}>")

    def handle_endtag(self, tag: str) -> None:
        """Close an allowed tag; void tags and dropped subtrees emit nothing."""
        if tag in _DROP_ENTIRELY:
            self._suppress = max(0, self._suppress - 1)
            return
        if self._suppress or tag not in _ALLOWED or tag in _VOID:
            return
        self.parts.append(f"</{tag}>")

    def handle_startendtag(self, tag: str, attrs) -> None:
        self.handle_starttag(tag, attrs)

    def handle_data(self, data: str) -> None:
        """Text, escaped, unless inside a dropped subtree."""
        if not self._suppress:
            self.parts.append(
                data.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            )

    # -- attributes ---------------------------------------------------------

    def _attributes(self, tag: str, attrs: dict) -> dict | None:
        """The attributes to keep for `tag`, or None when the tag itself must go (a
        remote or embedded image).
        """
        allowed = _ALLOWED_ATTRIBUTES.get(tag, frozenset())
        kept: dict[str, str] = {}

        if tag == "img":
            source = (attrs.get("src") or "").strip()
            if _is_remote(source):
                # **The whole point of this module.** Counted and dropped; the
                # alt text survives so the reader sees something was there.
                self.blocked_images += 1
                alt = attrs.get("alt") or "image"
                self.parts.append(f"[{_escape(alt)}]")
                return None
            if source.lower().startswith("cid:"):
                # An image carried inside the message. Nothing to fetch, but
                # `QTextBrowser` cannot resolve it either, so it becomes text.
                self.blocked_images += 0
                self.parts.append(f"[{_escape(attrs.get('alt') or 'image')}]")
                return None

        for name, value in attrs.items():
            name = (name or "").lower()
            value = value or ""
            if name not in allowed:
                continue            # every on* handler leaves here
            if name == "href":
                if _is_remote(value):
                    # Kept visible, not clickable: a fifteen-year-old link is
                    # worth reading and rarely worth following, and the widget
                    # is told never to open one anyway.
                    self.blocked_links += 1
                    continue
                if not value.lower().startswith(_SAFE_LINK_SCHEMES):
                    continue
            if "javascript:" in value.lower().replace(" ", ""):
                continue
            kept[name] = _escape(value)
        return kept


def _escape(value: str) -> str:
    """HTML-escape an attribute value or text."""
    return (
        str(value)
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def sanitise_email_html(markup: str) -> Sanitised:
    """Email HTML with every remote reference removed. Never raises.

    Malformed markup is normal here - these are messages from mail clients that
    have not existed for a decade - so a parse failure returns the text with all
    tags stripped rather than nothing at all. Showing the words is better than
    showing an error, and both are better than showing a tracking pixel.
    """
    if not markup:
        return Sanitised("")

    cleaner = _Cleaner()
    try:
        cleaner.feed(markup)
        cleaner.close()
    except Exception:                            # noqa: BLE001 - see the docstring
        return Sanitised(f"<pre>{_escape(strip_to_text(markup))}</pre>")

    return Sanitised(
        "".join(cleaner.parts),
        blocked_images=cleaner.blocked_images,
        blocked_links=cleaner.blocked_links,
    )


def strip_to_text(markup: str) -> str:
    """Every tag removed, leaving the words. The last-resort renderer."""
    without_blocks = re.sub(
        r"<(script|style)[^>]*>.*?</\1>", " ", markup or "",
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = re.sub(r"<[^>]+>", " ", without_blocks)
    return re.sub(r"[ \t]+", " ", text).strip()
