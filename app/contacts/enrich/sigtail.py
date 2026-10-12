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

**What the first precision check added (order 1s 9b, 18 of 100 wrong).** Legal text
had become addresses ("X Ltd (company no. ...) is located at ..."), so a line that is
a paragraph or carries legal words is dropped, and everything after a notice's heading
or a legal opening ("This email originates from ...") goes with it. Separators had
stayed inside values ("12 Mill Lane | Leeds"), so a line is split on a spaced bar, a
bullet or a middle dot before anything reads it. HTML entities (`&#43;` for a phone's
`+`) are decoded, because many real blocks are HTML turned into text.
"""

from __future__ import annotations

import html
import re
from urllib.parse import urlsplit

from app.extract.quoting import QUOTE_MARKERS

__all__ = ["clean_tail", "own_part", "is_tracking", "is_homepage", "line_key", "TRACKING_HOSTS", "is_legal",
           "is_prose", "split_line", "LEGAL", "is_meeting"]

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


#: The HTML entities HTML-to-text leaves behind. Only these: a bare `&` in "Smith & Sons" is text, and a
#: general unescape would also turn "&copy" without its `;` into a sign.
_ENTITY = re.compile(r"&(?:#\d{1,6}|#x[0-9a-f]{1,6}|quot|lt|gt|amp|nbsp|apos);", re.I)

#: Words that make a line legal text - a registration, a notice, a licence - never a detail of a person.
#: Narrow on purpose: "Registered Nurse" and "Authorised Signatory" are titles, so `registered` and
#: `authorised` count only in the phrases a notice uses.
LEGAL = re.compile(
    r"\b(?:registered\s+(?:in|office|address|number|no\b|with|at|charity|company)|registration\s+(?:no|number)|"
    r"company\s+(?:no\b|number|reg)|reg\.?\s*no\b|vat\b|regulated|authori[sz]ed\s+(?:and|by)|monitored|"
    r"confidential(?:ity)?|intended\s+recipient|disclaimer|trade\s?marks?|licen[cs]e[ds]?\b|liabilit(?:y|ies)|"
    r"pursuant|privileged|unauthori[sz]ed)", re.I)
#: A legal line that opens a notice: what follows it is the notice too, whatever its words.
_NOTICE_OPENING = re.compile(
    r"^\W*(?:(?:e-?mail|important|legal|privacy)\s+)?(?:confidentiality|disclaimer)\b|"
    r"^\W*(?:this|the\s+information\s+in\s+this)\s+(?:e-?mail|message|communication|transmission)\b|"
    r"^\W*all\s+e-?mails?\b", re.I)
#: "firm. It is the" - a full stop after a lower-case word, then more words: a sentence, not a label.
#: "St. James's", "Ltd. 12 Mill Lane" and "J. Smith" are not, which is why the word before is lower case.
_MID_STOP = re.compile(r"\b[a-z]{3,}\.\s+\S+\s+\S+\s+\S+")
#: The separators a signature puts between values on one line: a bar with a space or a comma beside it
#: ("Centre |, Doha"), a bullet, a middle dot, a small square - and U+FFFD after a space, which is what a
#: bullet becomes when the text was decoded in the wrong code page (9b round 2: "\ufffd 2000 Antwerpen").
#: "a|b" inside a value is left alone.
_SEPARATOR = re.compile(r"\s+\|\s*|\s*\|\s+|\s*\|(?=,)|\s*[\u2022\u00b7\u25aa\u25cf]\s*"
                        r"|\s+\ufffd\s*")


#: Order 1s 9b round 2: a meeting invitation (Teams, WebEx, Zoom) under a signature - dial-in numbers, a
#: conference id, a table of countries and toll numbers - was read as an address. From its first marker on
#: the rest of the message is the invitation. "call in" only in the invitation's own phrases, because
#: "can you call in tomorrow" is prose above a signature.
_MEETING = re.compile(
    r"\b(?:or\s+call\s+in|call[- ]in\s+(?:numbers?|\(audio)|conference\s+id|join\s+by\s+phone|"
    r"meeting\s+options|join\s+(?:microsoft\s+)?teams\s+meeting|join\s+from\s+a\s+video\s+system|reset\s+pin|"
    r"find\s+a\s+local\s+number|join\s+(?:zoom|webex)\s+meeting|meeting\s+id\s*:|toll\s*(?:number\s*)?(?::|$))"
    r"|,{2,}\s*\d|\d{4,}#", re.I)


def is_meeting(line: str) -> bool:
    """A line of a meeting invitation: one of its phrases, or a dial string ("...,,125254186#")."""
    return bool(_MEETING.search(line or ""))


def is_legal(line: str) -> bool:
    return bool(LEGAL.search(line or ""))


def is_prose(line: str) -> bool:
    """A sentence somebody wrote, not a line of a signature: more than 12 words (unless it is a list -
    three commas make "Level 12, Falcon Tower, ..., Dubai" an address, not prose), or a full stop
    mid-line followed by more words."""
    words = line.split()
    if len(words) > 12 and line.count(",") < 3:
        return True
    return len(words) >= 7 and bool(_MID_STOP.search(line))


def split_line(line: str) -> list[str]:
    """One line's values: split on its separators, each part's edge commas trimmed ("Centre |, Doha").
    A line with no separator is left as it is ("Kind regards," keeps its comma)."""
    if not _SEPARATOR.search(line):
        return [line.strip()] if line.strip() else []
    parts = [p.strip(" ,;") for p in _SEPARATOR.split(line)]
    return [p for p in parts if p.strip(" ,;.-")]


def clean_tail(body: str) -> str:
    """3a. Entities decoded; lines split on their separators; annotations, junk, legal and paragraph lines
    dropped, and everything after a notice's opening; every run of blank lines one blank line; edges trimmed."""
    out: list[str] = []
    in_paragraph = False
    for raw in (body or "").splitlines():
        line = _ENTITY.sub(lambda m: html.unescape(m.group(0)), raw)
        line = _SPACES.sub(" ", line.translate(_INVISIBLE)).rstrip()
        if not line.strip():
            out.append("")
            in_paragraph = False
            continue
        stripped = _IMAGE.sub("", _ANNOTATION.sub("", line)).strip().lstrip("\ufffd ")
        if not stripped:
            continue                                       # gone, not blank: it was never a gap in the text
        if is_meeting(stripped):
            break                                          # a meeting invitation: the rest is the invitation
        if is_legal(stripped):
            if _NOTICE_OPENING.search(stripped):
                break                                      # a notice's heading or opening: the rest is the notice
            # A long legal line is a wrapped paragraph: its next lines ("Conduct Authority.") are the notice
            # too. A short one ("VAT No: GB 123") can sit inside a signature, so only it goes.
            in_paragraph = len(stripped.split()) > 12
            continue
        if in_paragraph:
            continue                                       # the rest of a legal paragraph, up to a blank line
        # A sentence goes; the sign-off under it is still read. Judged part by part, because a signature
        # set on one line with separators ("13D | 4817 ZX Breda | The Netherlands | ...") is long, not prose.
        out += [part for part in split_line(stripped) if not _junk_line(part) and not is_prose(part)]
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
