"""Order 1s 6a: an internet lookup's answer, kept so it can be offered again without asking again.

Layer: L7

Before this, a lookup was remembered (`enrich:seen:<kind>:<key>` -> when) but its answer was not, so a
result the person did not accept in the run that found it was lost until the repeat interval ran out
(ninety days by default). Now the answer is kept beside the visit:

* `index_state` `enrich:result:<kind>:<key>` -> a small JSON (a few hundred bytes). `<key>` is the same
  hash, domain or company key as the visit's - **never an email address**. For Gravatar and the logo it
  is the proposals found, as text; for Wikidata it is the company's facts, so a colleague at the same
  company gets them too.
* a photo or logo's bytes do **not** go into `index_state` (a 256-pixel PNG can be tens of kilobytes,
  and `index_state` is read whole by other code). They go into `<data>/cache/enrich/<sha>.png`, named by
  the picture's own hash, and the JSON holds only that hash. A cleared cache costs only the picture: it
  is not offered again until the next lookup.

A lookup that failed to get an answer keeps nothing (the caller never calls `keep`), exactly as it is
not remembered as a visit. What is offered again is checked against the card as it is now, so a value
accepted since is not offered twice. Nothing here makes a request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app.contacts.card import Card, Field, Photo, field_key
from app.contacts.enrich.base import Proposal

__all__ = ["result_key", "photo_dir", "keep", "offer_again", "keep_facts", "facts_again"]

_VERSION = 1


def result_key(kind: str, key: str) -> str:
    # The key is a hash, a domain or a company key; an address in it would be a leak into index_state.
    assert "@" not in key, "an email address must never be part of an enrichment memory key"
    return f"enrich:result:{kind}:{key}"


def photo_dir(store: Any) -> Optional[Path]:
    """`<data>/cache/enrich` beside the database.

    The database is `<data>/fts/knowledge.db` in the standard layout (`app/core/config.py`), so this is the
    data folder's own `cache` (`settings.cache_path`), which an index move carries with it. A database
    somewhere else (a test's, or a moved `FTS_DB`) keeps its pictures in a `cache` beside it."""
    raw = getattr(store, "db_path", None)
    if not raw:
        return None
    db = Path(raw)
    base = db.parent.parent if db.parent.name == "fts" else db.parent
    return base / "cache" / "enrich"


#: The most the kept pictures may take, together. A constant (rule 11): nobody tunes it, and at about 20 KB a
#: photo or favicon it holds well over a thousand. What would change it: a measured book whose pictures
#: outgrow it. Past it the least recently used go first; a remembered answer whose picture went still
#: offers its words.
PHOTO_CACHE_BYTES = 25 * 1024 * 1024


def _prune(folder: Path, keep: Path) -> None:
    """Keep the folder under `PHOTO_CACHE_BYTES`, oldest-used first; never the picture just written."""
    try:
        files = sorted((f for f in folder.iterdir() if f.is_file() and f.suffix in (".png", ".jpg")),
                       key=lambda f: f.stat().st_atime)
        total = sum(f.stat().st_size for f in files)
        for f in files:
            if total <= PHOTO_CACHE_BYTES:
                break
            if f == keep:
                continue
            total -= f.stat().st_size
            f.unlink(missing_ok=True)
    except OSError:                                      # a cache: a failed tidy costs only space
        pass


def _save_photo(store: Any, photo: Photo) -> bool:
    folder = photo_dir(store)
    if folder is None:
        return False
    path = folder / (photo.sha + (".png" if photo.mime == "image/png" else ".jpg"))
    try:
        if not path.exists():
            folder.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".part")
            tmp.write_bytes(photo.data)
            tmp.replace(path)
            _prune(folder, path)
        return True
    except OSError:                                      # no room or no rights: keep the text answer only
        return False


def _load_photo(store: Any, sha: str, mime: str) -> Optional[Photo]:
    folder = photo_dir(store)
    if folder is None or not sha.isalnum():
        return None
    path = folder / (sha + (".png" if mime == "image/png" else ".jpg"))
    try:
        photo = Photo(mime, path.read_bytes())
    except OSError:
        return None
    return photo if photo.sha == sha else None           # a changed file is not the picture that was found


def keep(store: Any, kind: str, key: str, found: list[Proposal]) -> None:
    """Keep what one lookup returned (possibly nothing) as `enrich:result:<kind>:<key>`."""
    items: list[dict] = []
    for p in found:
        if p.is_photo:
            if isinstance(p.new, Photo) and _save_photo(store, p.new):
                items.append({"k": "photo", "sha": p.new.sha, "mime": p.new.mime, "r": p.receipt,
                              "c": p.confidence, **_opens(p)})
            continue
        item = {"k": p.field_kind, "l": p.field_label, "n": str(p.new), "r": p.receipt, "c": p.confidence,
                "d": dict(p.data), **_opens(p)}
        # A value with an "@" in it could be an address; it is not worth keeping one in index_state for.
        if "@" in json.dumps(item):
            continue
        items.append(item)
    store.set_state(result_key(kind, key), json.dumps({"v": _VERSION, "found": items}, separators=(",", ":")))


def _opens(p: Proposal) -> dict:
    """`{"o": opens}` to keep with an answer (owner, 2026-10-15: a remembered suggestion opens where it came
    from too), or nothing. Never an address: an `opens` with an "@" in it (a `mailto:`) is not kept, in the
    same spirit as the keys - the answer itself is still kept, without it."""
    o = str(getattr(p, "opens", "") or "")
    return {"o": o} if o and "@" not in o else {}


def _opens_again(it: dict, kind: str, key: str, receipt: str) -> str:
    """Where a kept answer opens. One kept before `opens` existed has no "o": a logo's is its company's home
    page (what a new logo carries, `logo.home_page`), any other's its receipt when that is a bare https
    address (Gravatar's profile page), else nothing."""
    o = str(it.get("o") or "")
    if o:
        return o
    if kind == "logo" and key:
        return f"https://{key}/"
    return receipt if receipt.startswith("https://") and " " not in receipt else ""


def _read(store: Any, kind: str, key: str) -> Optional[dict]:
    raw = store.get_state(result_key(kind, key))
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return data if isinstance(data, dict) and data.get("v") == _VERSION else None


def offer_again(store: Any, kind: str, key: str, cid: int, card: Card, region: str,
                confidence: Optional[float] = None) -> Optional[list[Proposal]]:
    """The kept answer as proposals for this contact, less what its card already holds. None: nothing kept
    (a visit from before 6a, or one whose record is unreadable) - the caller offers nothing, as before.

    `confidence`, when given, is the source's sureness now, used instead of the one kept: a kept answer
    carries the sureness of the day it was found, and the logo's rose from 0.4 to 0.5 (2026-10-15)."""
    data = _read(store, kind, key)
    if data is None:
        return None
    have = {field_key(f, region) for f in card.fields}
    out: list[Proposal] = []
    for it in data.get("found") or []:
        if not isinstance(it, dict):
            continue
        k = str(it.get("k") or "")
        conf = float(confidence) if confidence is not None else float(it.get("c") or 0.5)
        receipt = str(it.get("r") or "")
        if not receipt:
            continue                                     # a value with no receipt is not offered (order 1s)
        if k == "photo":
            if card.photo is not None:
                continue
            photo = _load_photo(store, str(it.get("sha") or ""), str(it.get("mime") or "image/png"))
            if photo is not None:
                out.append(Proposal(cid, "photo", "", "", photo, kind, receipt, conf,
                                    opens=_opens_again(it, kind, key, receipt)))
            continue
        p = Proposal(cid, k, str(it.get("l") or ""), "", str(it.get("n") or ""), kind, receipt, conf,
                     data={str(a): str(b) for a, b in (it.get("d") or {}).items()},
                     opens=_opens_again(it, kind, key, receipt))
        if p.is_scalar:
            if card.get(k).strip() == str(p.new).strip():
                continue
        elif field_key(Field(k, str(p.new), p.field_label, dict(p.data)), region) in have:
            continue
        out.append(p)
    return out


def keep_facts(store: Any, key: str, facts: Any) -> None:
    """Wikidata: the company's facts (or None, "no such company") under its company key."""
    body = None if facts is None else {"qid": facts.qid, "label": facts.label, "website": facts.website,
                                       "hq": facts.headquarters, "ind": list(facts.industries),
                                       "amb": bool(facts.ambiguous)}
    store.set_state(result_key("wikidata", key), json.dumps({"v": _VERSION, "facts": body}, separators=(",", ":")))


def facts_again(store: Any, key: str) -> tuple[bool, Any]:
    """(kept, facts): `kept` False when nothing was kept for this company."""
    from app.contacts.enrich.wikidata import CompanyFacts  # noqa: PLC0415 - wikidata imports this package's base

    data = _read(store, "wikidata", key)
    if data is None:
        return False, None
    f = data.get("facts")
    if not isinstance(f, dict) or not str(f.get("qid") or ""):
        return True, None
    return True, CompanyFacts(str(f.get("qid") or ""), str(f.get("label") or ""), str(f.get("website") or ""),
                              str(f.get("hq") or ""), [str(x) for x in f.get("ind") or []], bool(f.get("amb")))
