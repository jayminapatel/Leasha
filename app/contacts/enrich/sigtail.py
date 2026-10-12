"""Order 1s 3a: clean a message's text before the signature reader looks at it.

Layer: L7

Pure: text in, text out. What the order's opening found on the owner's index is
why each rule exists - Outlook's HTML-to-text leaves a blank line between every
line and `<mailto:...>` after every address, and mail gateways append rewritten
links (Mimecast, Safelinks), tracking pixels (Sidekick, Mailchimp's list-manage)
and rows of social icons after the signature, which pushed it out of the lines
the old reader looked at.

`own_part` keeps only what the sender wrote in *this* message: everything from a
quoted chain on (the markers `app.extract.quoting` already trusts) is somebody
else's text, and reading it as the sender's is how a client's reply could offer
the owner's own mobile (3h). A confidentiality notice ends the own part too - it
sits after the signature and repeats on every message, so it would otherwise be
the longest run a sender repeats (3b).
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

from app.extract.quoting import QUOTE_MARKERS

__all__ = ["clean_tail", "own_part", "is_tracking", "is_homepage", "line_key", "TRACKING_HOSTS"]

#: Characters Outlook and phones leave in "empty" lines: no-break and zero-width spaces, the BOM.
_INVISIBLE = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"), None)
_SPACES = re.compile(r"[\u00a0\u2007\u202f\t]")

#: `ann@x.com<mailto:ann@x.com>`, `www.x.com<http://www.x.com/>`, `020 7946 0018<tel:02079460018>` -
#: the annotation repeats the value, so it goes and the value stays.
_ANNOTATION = re.compile(r"\s*[<\[](?:mailto|tel|callto|sip|https?):[^>\]\s]*[>\]]", re.I)
#: Pictures Outlook renders as text: `[cid:image001.png@01DA...]`, `[image: logo]`, `[Description: ...]`.
_IMAGE = re.compile(r"\[(?:cid:|image\b|description:|logo\b|icon\b|signature\b)[^\]]*\]", re.I)

#: Rewritten or tracking links. A line that is only these is never part of anybody's details.
TRACKING_HOSTS = ("mimecast.com", "safelinks.protection.outlook.com", "sidekickopen", "hubspotlinks.com",
                  "list-manage.com", "mailchi.mp", "urldefense.", "linkprotect.cudasvc.com", "clicktime.",
                  "t.yesware.com", "mailtrack.io", "streak", "bananatag", "sendgrid.net", "mandrillapp.com")
_TRACKING_PATHS = ("unsubscribe", "/track/", "/redirect", "click?", "/ls/click", "utm_")

_URL_TOKEN = re.compile(r"^(?:https?://\S+|www\.\S+)$", re.I)
_SOCIAL_WORDS = {"linkedin", "facebook", "twitter", "x", "instagram", "youtube", "tiktok", "whatsapp", "telegram",
                 "snapchat", "pinterest", "vimeo", "xing", "threads", "bluesky", "icon", "logo", "image", "follow",
                 "us", "on", "and"}
_FOLLOW_US = re.compile(r"^\W*(?:follow|connect with|find|join|like)\s+us\b", re.I)


def is_tracking(url: str) -> bool:
    low = url.lower()
    host = (urlsplit(low if "://" in low else "https://" + low).hostname or "")
    return any(t in host for t in TRACKING_HOSTS) or any(p in low for p in _TRACKING_PATHS)


def is_homepage(url: str) -> bool:
    """A company's own web address: a host and at most one short path segment, no query.
    `www.acme.example/en` is; `acme.example/news/2026/our-plant?utm=x` is a link to something."""
    low = url.lower().rstrip(".,;:")
    parts = urlsplit(low if "://" in low else "https://" + low)
    segments = [s for s in parts.path.split("/") if s]
    return bool(parts.hostname) and not parts.query and len(segments) <= 1 and \
        all(len(s) <= 20 for s in segments)


def _junk_line(line: str) -> bool:
    """A line that carries no detail: only links that are not a home page, only social icons, "Follow us"."""
    if _FOLLOW_US.match(line):
        return True
    tokens = [t for t in re.split(r"[\s|,;\u2022\u00b7]+", line) if t]
    if tokens and all(_URL_TOKEN.match(t) for t in tokens):
        return any(is_tracking(t) or not is_homepage(t) for t in tokens)
    words = [w for w in re.split(r"[^a-z]+", line.lower()) if w]
    return bool(words) and all(w in _SOCIAL_WORDS for w in words) and any(
        w not in ("us", "on", "and", "follow", "x") for w in words)


def clean_tail(body: str) -> str:
    """3a. Annotations dropped, junk lines dropped, every run of blank lines one blank line, edges trimmed."""
    out: list[str] = []
    for raw in (body or "").splitlines():
        line = _SPACES.sub(" ", raw.translate(_INVISIBLE)).rstrip()
        if not line.strip():
            out.append("")
            continue
        stripped = _IMAGE.sub("", _ANNOTATION.sub("", line)).rstrip()
        if not stripped.strip() or _junk_line(stripped.strip()):
            continue                                       # gone, not blank: it was never a gap in the text
        out.append(stripped)
    collapsed: list[str] = []
    for line in out:
        if line or (collapsed and collapsed[-1]):
            collapsed.append(line)
    while collapsed and not collapsed[-1]:
        collapsed.pop()
    return "\n".join(collapsed)


#: The confidentiality notices `app.extract.quoting` cuts at, here as the end of the sender's own text.
_NOTICE = (re.compile(r"^\s*This e?-?mail (and any attachments? )?(is|are|may be) (strictly )?confidential", re.M | re.I),
           re.compile(r"^\s*The information (contained )?in this e?-?mail", re.M | re.I),
           re.compile(r"^\s*(?:CONFIDENTIALITY|DISCLAIMER)\b", re.M | re.I))


def own_part(text: str) -> str:
    """The sender's own text of one message: up to the first quoted chain or confidentiality notice."""
    text = text or ""
    cut = len(text)
    for pattern in (*QUOTE_MARKERS, *_NOTICE):
        m = pattern.search(text)
        if m and m.start() < cut:
            cut = m.start()
    return text[:cut]


def line_key(line: str) -> str:
    """How two lines are compared across messages: case, spacing and edge punctuation do not count."""
    return re.sub(r"\s+", " ", line.casefold()).strip(" |-,.;:\u2013\u2014")
