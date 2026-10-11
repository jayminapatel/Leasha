"""vCard 2.1, 3.0 and 4.0, to and from a `Card`.

Layer: L7

Order 1k 4c, 6i, 6j, 6r. One module reads and writes vCards for the CardDAV
connector, the importer, the exporter and the QR code, so a card is spelled one
way everywhere.

Reading is forgiving - real files come from phones, Outlook and hand editing:
line folding (RFC 6350 and the 2.1 `=` soft break), `QUOTED-PRINTABLE`, a
`CHARSET` parameter, `TYPE=` parameters in any case or as bare 2.1 tokens, and
a file holding many cards. Unknown properties are kept in `Card.fields` as
`custom` values only when they are `X-` properties with a value; everything
else is dropped rather than guessed at.

Writing is strict: CRLF lines folded at 75 octets, text escaped.
"""

from __future__ import annotations

import base64
import binascii
import quopri
import re
from typing import Iterable, Iterator

from app.contacts.card import Card, Field, Photo, display_name

__all__ = ["parse", "parse_one", "dumps", "dump_many", "split_cards"]

_TEXT_ESCAPES = {"\\n": "\n", "\\N": "\n", "\\,": ",", "\\;": ";", "\\\\": "\\"}

_TYPE_LABELS = {
    "work": "work", "home": "home", "cell": "mobile", "mobile": "mobile", "iphone": "mobile",
    "fax": "fax", "pager": "pager", "voice": "", "pref": "", "internet": "", "text": "",
    "other": "other", "main": "main", "car": "car", "isdn": "isdn", "video": "video",
}


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------

def split_cards(text: str) -> list[str]:
    """The `BEGIN:VCARD ... END:VCARD` blocks of `text`, in order."""
    return [m.group(0) for m in re.finditer(
        r"BEGIN:VCARD.*?END:VCARD", text, flags=re.IGNORECASE | re.DOTALL)]


def _unfold(block: str) -> list[str]:
    raw = block.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: list[str] = []
    for line in raw:
        if out and (line.startswith(" ") or line.startswith("\t")):
            out[-1] += line[1:]
        elif out and out[-1].endswith("=") and re.search(r"quoted-printable", out[-1], re.I):
            out[-1] = out[-1][:-1] + line                      # 2.1 soft break
        else:
            out.append(line)
    return [ln for ln in out if ln.strip()]


def _unescape(value: str) -> str:
    return re.sub(r"\\[nN,;\\]", lambda m: _TEXT_ESCAPES[m.group(0)], value)


def _split_value(value: str, sep: str) -> list[str]:
    parts, cur, i = [], [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            cur.append(value[i:i + 2])
            i += 2
            continue
        if ch == sep:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
        i += 1
    parts.append("".join(cur))
    return parts


def _parse_line(line: str) -> tuple[str, dict[str, list[str]], str]:
    head, _, value = line.partition(":")
    if "://" in head and ";" not in head.split("://")[0]:        # "URL:http://x" split wrongly
        head, _, value = line.partition(":")
    pieces = head.split(";")
    name = pieces[0].split(".")[-1].upper()                      # drop an Apple "item1." group
    params: dict[str, list[str]] = {}
    for piece in pieces[1:]:
        if "=" in piece:
            k, _, v = piece.partition("=")
            params.setdefault(k.upper(), []).extend(x.strip('"') for x in v.split(","))
        elif piece:
            params.setdefault("TYPE", []).append(piece)           # 2.1 bare token
    return name, params, value


def _decode(value: str, params: dict[str, list[str]]) -> str:
    enc = [e.upper() for e in params.get("ENCODING", [])]
    charset = (params.get("CHARSET") or ["utf-8"])[0]
    if "QUOTED-PRINTABLE" in enc:
        try:
            return quopri.decodestring(value.encode("latin-1", "replace")).decode(charset, "replace")
        except (LookupError, ValueError):
            return value
    return value


def _label(params: dict[str, list[str]]) -> str:
    labels = []
    for t in params.get("TYPE", []):
        for token in t.split(","):
            mapped = _TYPE_LABELS.get(token.strip().lower(), token.strip().lower())
            if mapped and mapped not in labels:
                labels.append(mapped)
    return " ".join(labels)


def _is_pref(params: dict[str, list[str]]) -> bool:
    types = [t.lower() for t in params.get("TYPE", [])]
    return "pref" in types or "1" in params.get("PREF", [])


def parse_one(block: str) -> Card:
    card = Card()
    notes: list[str] = []
    for line in _unfold(block):
        name, params, raw = _parse_line(line)
        if name in ("BEGIN", "END", "VERSION", "PRODID", "REV", "UID", "KIND"):
            continue
        value = _decode(raw, params)
        if name == "N":
            parts = [_unescape(p) for p in _split_value(value, ";")] + [""] * 5
            card.scalars.update({"family": parts[0], "given": parts[1], "additional": parts[2],
                                 "prefix": parts[3], "suffix": parts[4]})
        elif name == "FN":
            card.scalars.setdefault("_fn", _unescape(value))
        elif name == "NICKNAME":
            card.scalars["nickname"] = _unescape(value)
        elif name == "ORG":
            parts = [_unescape(p) for p in _split_value(value, ";")]
            card.scalars["company"] = parts[0]
            if len(parts) > 1:
                card.scalars["department"] = " ".join(p for p in parts[1:] if p)
        elif name == "TITLE":
            card.scalars["title"] = _unescape(value)
        elif name == "NOTE":
            notes.append(_unescape(value))
        elif name == "EMAIL":
            card.fields.append(Field("email", _unescape(value).strip(), _label(params), primary=_is_pref(params)))
        elif name == "TEL":
            card.fields.append(Field("phone", _unescape(value).replace("tel:", "").strip(), _label(params),
                                     primary=_is_pref(params)))
        elif name == "ADR":
            p = [_unescape(x) for x in _split_value(value, ";")] + [""] * 7
            data = {"street": p[2] or p[1],
                    "city": p[3], "region": p[4], "postcode": p[5], "country": p[6]}
            data = {k: v for k, v in data.items() if v}
            if data:
                text = ", ".join(data[k] for k in ("street", "city", "region", "postcode", "country") if k in data)
                card.fields.append(Field("address", text, _label(params), data=data, primary=_is_pref(params)))
        elif name == "URL":
            card.fields.append(Field("url", _unescape(value).strip(), _label(params)))
        elif name == "BDAY":
            card.fields.append(Field("date", _date(value), "birthday"))
        elif name == "ANNIVERSARY":
            card.fields.append(Field("date", _date(value), "anniversary"))
        elif name == "RELATED":
            card.fields.append(Field("related", _unescape(value), _label(params)))
        elif name in ("IMPP", "X-JABBER", "X-SKYPE", "X-AIM", "X-MSN", "X-YAHOO", "X-ICQ"):
            card.fields.append(Field("im", _unescape(value).split(":", 1)[-1] if name == "IMPP" else _unescape(value),
                                     _label(params) or name.replace("X-", "").lower()))
        elif name == "X-SOCIALPROFILE":
            card.fields.append(Field("social", _unescape(value), (params.get("TYPE") or [""])[0].lower()))
        elif name == "X-ABDATE":
            card.fields.append(Field("date", _date(value), _unescape((params.get("X-ABLABEL") or ["other"])[0]).strip("_$!<>").lower()))
        elif name == "CATEGORIES":
            for g in _split_value(value, ","):
                g = _unescape(g).strip()
                if g and g not in card.groups:
                    card.groups.append(g)
        elif name == "PHOTO":
            photo = _photo(value, params)
            if photo is not None:
                card.photo = photo
        elif name.startswith("X-DATE-"):
            card.fields.append(Field("date", _date(value), name[7:].replace("-", " ").lower()))
        elif name.startswith("X-") and value.strip() and name not in ("X-ABLABEL", "X-ABUID", "X-ABRELATEDNAMES"):
            card.fields.append(Field("custom", _unescape(value), name[2:].replace("-", " ").lower()))
    if notes:
        card.scalars["notes"] = "\n".join(notes)
    fn = card.scalars.pop("_fn", "")
    if not any(card.get(k) for k in ("given", "family", "additional")) and fn:
        # No structured name: split FN on its last space, the usual reading.
        if card.get("company") == fn:
            pass
        else:
            bits = fn.rsplit(" ", 1)
            if len(bits) == 2:
                card.scalars["given"], card.scalars["family"] = bits
            else:
                card.scalars["given"] = fn
    card.scalars = {k: v for k, v in card.scalars.items() if v}
    return card


def _date(value: str) -> str:
    v = value.strip()
    m = re.fullmatch(r"(\d{4})-?(\d{2})-?(\d{2})(?:T.*)?", v)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.fullmatch(r"--(\d{2})-?(\d{2})", v)
    return f"--{m.group(1)}-{m.group(2)}" if m else v


def _photo(value: str, params: dict[str, list[str]]) -> "Photo | None":
    mime = "image/jpeg"
    if value.lower().startswith("data:"):
        head, _, data = value.partition(",")
        mime = head[5:].split(";")[0] or mime
        value = data
    else:
        kind = (params.get("TYPE") or params.get("MEDIATYPE") or ["jpeg"])[0].lower()
        mime = kind if "/" in kind else f"image/{'jpeg' if kind in ('jpg', 'jpeg') else kind}"
    if value.lower().startswith("http"):
        return None                       # a link is never followed: nothing is fetched on import
    try:
        data = base64.b64decode(re.sub(r"\s+", "", value), validate=False)
    except (binascii.Error, ValueError):
        return None
    return Photo(mime, data) if data else None


def parse(text: str) -> list[Card]:
    return [parse_one(b) for b in split_cards(text)]


# --------------------------------------------------------------------------
# Writing
# --------------------------------------------------------------------------

def _esc(text: str) -> str:
    return (text or "").replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _fold(line: str) -> str:
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        limit = 75 if not out else 74
        if size + n > limit:
            out.append(cur)
            cur, size = ch, n
        else:
            cur += ch
            size += n
    out.append(cur)
    return "\r\n ".join(out)


def _types(label: str, pref: bool, version: str) -> str:
    toks = [t for t in label.split() if t]
    if pref:
        toks.append("pref")
    if not toks:
        return ""
    if version == "4.0":
        return ";TYPE=" + ",".join(t.lower() for t in toks)
    return ";TYPE=" + ",".join(t.upper() if t.lower() in ("work", "home", "cell", "fax", "pref") else t
                                for t in (("cell" if x == "mobile" else x) for x in toks))


def dumps(card: Card, *, version: str = "3.0", uid: str = "") -> str:
    """One vCard. `version` is `"3.0"` or `"4.0"`."""
    if version not in ("3.0", "4.0"):
        raise ValueError("vCard version must be 3.0 or 4.0")
    L = ["BEGIN:VCARD", f"VERSION:{version}"]
    s = card.get
    L.append("N:" + ";".join(_esc(s(k)) for k in ("family", "given", "additional", "prefix", "suffix")))
    L.append("FN:" + _esc(display_name(card) or "No name"))
    if s("nickname"):
        L.append("NICKNAME:" + _esc(s("nickname")))
    if s("company") or s("department"):
        L.append("ORG:" + _esc(s("company")) + (";" + _esc(s("department")) if s("department") else ""))
    if s("title"):
        L.append("TITLE:" + _esc(s("title")))
    for f in card.fields:
        t = _types(f.label, f.primary, version)
        if f.kind == "email":
            L.append(f"EMAIL{t}:{_esc(f.value)}")
        elif f.kind == "phone":
            L.append(f"TEL{t}:{f.value}")
        elif f.kind == "address":
            d = f.data or {}
            if d:
                L.append(f"ADR{t}:;;" + ";".join(_esc(d.get(k, "")) for k in
                                                  ("street", "city", "region", "postcode", "country")))
            else:
                L.append(f"ADR{t}:;;{_esc(f.value)};;;;")
        elif f.kind == "url":
            L.append(f"URL{t}:{f.value}")
        elif f.kind == "date":
            lab = f.label.lower()
            if lab == "birthday":
                L.append(f"BDAY:{f.value}")
            elif lab == "anniversary" and version == "4.0":
                L.append(f"ANNIVERSARY:{f.value}")
            else:
                L.append(f"X-DATE-{re.sub(r'[^A-Za-z0-9]+', '-', f.label or 'other').upper()}:{f.value}")
        elif f.kind == "related":
            L.append(f"X-RELATED{t}:{_esc(f.value)}" if version == "3.0" else f"RELATED{t}:{_esc(f.value)}")
        elif f.kind == "social":
            L.append(f"X-SOCIALPROFILE;TYPE={f.label or 'other'}:{_esc(f.value)}")
        elif f.kind == "im":
            L.append(f"IMPP:{_esc(f.value)}" if version == "4.0" else f"X-{(f.label or 'IM').upper()}:{_esc(f.value)}")
        elif f.kind == "custom":
            L.append(f"X-{re.sub(r'[^A-Za-z0-9]+', '-', f.label or 'CUSTOM').upper()}:{_esc(f.value)}")
    if s("notes"):
        L.append("NOTE:" + _esc(s("notes")))
    if card.groups:
        L.append("CATEGORIES:" + ",".join(_esc(g) for g in card.groups))
    if card.photo is not None:
        kind = card.photo.mime.split("/")[-1].upper().replace("JPG", "JPEG")
        b64 = base64.b64encode(card.photo.data).decode("ascii")
        L.append(f"PHOTO;ENCODING=b;TYPE={kind}:{b64}" if version == "3.0"
                 else f"PHOTO:data:{card.photo.mime};base64,{b64}")
    if uid:
        L.append("UID:" + uid)
    L.append("END:VCARD")
    return "\r\n".join(_fold(x) for x in L) + "\r\n"


def dump_many(cards: Iterable[Card], *, version: str = "3.0") -> str:
    return "".join(dumps(c, version=version) for c in cards)
