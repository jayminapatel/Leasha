r"""Which colour a result's kind badge takes. Qt-free.

Layer: L5 (presenter half)

UI Redesign (202626160950 §4a, §0.1-5). The brand's three splash stripes
carry meaning on result rows: documents take the blue, mail the orange, code
the green. Everything else - pictures, archives, an unknown extension -
takes the neutral fourth. The words on the badge are `presenter.kind_tag`,
unchanged since 0q kept them for the tooltip; only the colour is decided
here, so a test can check every kind without a display.
"""

from __future__ import annotations

from app.ui.presenter import is_code_kind, kind_tag

__all__ = ["FAMILY_DOC", "FAMILY_MAIL", "FAMILY_CODE", "FAMILY_OTHER",
           "family_for", "badge_token", "badge_word"]

FAMILY_DOC = "doc"
FAMILY_MAIL = "mail"
FAMILY_CODE = "code"
FAMILY_OTHER = "other"

#: Kinds that read as "a document" to the person - the blue stripe.
_DOCUMENT_KINDS = frozenset({
    "pdf", "docx", "doc", "odt", "rtf", "xlsx", "xls", "ods", "csv",
    "pptx", "ppt", "odp", "txt", "md", "log", "epub", "html", "htm",
})

_TOKENS = {
    FAMILY_DOC: "kind_doc",
    FAMILY_MAIL: "kind_mail",
    FAMILY_CODE: "kind_code",
    FAMILY_OTHER: "kind_other",
}


def family_for(kind: str) -> str:
    """`doc` / `mail` / `code` / `other` for a `ResultGroup.kind` or `ext`."""
    k = (kind or "").lower().lstrip(".")
    if k in ("email", "eml", "msg"):
        return FAMILY_MAIL
    if k in _DOCUMENT_KINDS:
        return FAMILY_DOC
    if is_code_kind(k):
        return FAMILY_CODE
    return FAMILY_OTHER


def badge_token(kind: str) -> str:
    """The palette token the badge is filled with."""
    return _TOKENS[family_for(kind)]


def badge_word(kind: str) -> str:
    """The short word on the badge - `kind_tag`, verbatim."""
    return kind_tag(kind)
