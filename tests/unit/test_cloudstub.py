"""Google Workspace pointers: indexed, explained, and never fetched.

Layer: L2

A `.gdoc` is not a document - it is a few hundred bytes of JSON holding a URL.
Drive for Desktop leaves thousands of them in a synced folder.

**The reason to index them is the failure they otherwise cause.** Unsupported,
they are skipped and invisible: somebody searches for a document they know
exists, finds nothing, and concludes search is broken. Indexed, the pointer is
findable by name and says what it is.

**The reason to test them is the promise they could break.** Fetching the URL
would be an authenticated request telling Google what is being indexed and when,
from an application whose entire proposition is that nothing leaves the machine.
The test below asserts that, rather than trusting it - "we would never do that"
is exactly the kind of promise that erodes one convenience at a time.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract.cloudstub import KINDS, CloudStubExtractor, parse_stub

MODULE = Path(__file__).resolve().parents[2] / "app" / "extract" / "cloudstub.py"


def stub(folder: Path, name: str, payload: dict) -> Path:
    path = folder / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The promise
# ---------------------------------------------------------------------------

def test_it_cannot_reach_the_network():
    """Asserted, not assumed.

    This application's proposition is that nothing leaves the machine. Fetching
    a Drive URL would be an authenticated request revealing what is being
    indexed and when - and it would look like a small convenience while doing it.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    imported = {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }

    forbidden = {"requests", "urllib", "http", "httpx", "socket", "aiohttp", "ftplib"}
    assert not (imported & forbidden), f"this module must not reach the network: {imported & forbidden}"


def test_no_call_in_this_module_looks_like_a_fetch():
    """A belt to the braces above: an import could be added inside a function."""
    source = MODULE.read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.strip().startswith("#")
    )
    for suspicious in ("urlopen(", "requests.get", "requests.post", "httpx.", "socket."):
        assert suspicious not in code, f"{suspicious} must not appear here"


# ---------------------------------------------------------------------------
# Reading the pointer
# ---------------------------------------------------------------------------

def test_a_modern_stub_yields_link_id_and_account(tmp_path):
    path = stub(tmp_path, "2025 Budget Review.gdoc", {
        "url": "https://docs.google.com/document/d/1AbCdEf/edit?usp=drivesdk",
        "doc_id": "1AbCdEf",
        "email": "chris@acme.com",
    })
    document = next(iter(CloudStubExtractor().extract(path)))

    assert "2025 Budget Review" in document.text
    assert "1AbCdEf" in document.text
    assert "chris@acme.com" in document.text
    assert document.meta["cloud_doc_id"] == "1AbCdEf"
    assert document.meta["content_is_remote"] == 1


def test_the_filename_is_indexed_because_it_is_all_the_meaning_there_is(tmp_path):
    """"2025 Budget Review.gdoc" is the only searchable content the file has, so
    a search for "budget review" must find it the way it finds a PDF."""
    path = stub(tmp_path, "Quarterly Budget Review.gdoc",
                {"url": "https://docs.google.com/document/d/X/edit"})
    document = next(iter(CloudStubExtractor().extract(path)))

    assert "Quarterly Budget Review" in document.text


def test_it_says_plainly_that_the_content_is_elsewhere(tmp_path):
    """The sentence that turns "search is broken" into an answer."""
    path = stub(tmp_path, "Notes.gdoc", {"url": "https://docs.google.com/document/d/X/edit"})
    document = next(iter(CloudStubExtractor().extract(path)))

    assert "not stored on this machine" in document.text


@pytest.mark.parametrize(("extension", "expected"), sorted(KINDS.items()))
def test_every_extension_is_named_in_words(extension, expected, tmp_path):
    """A row saying ".gjam" helps nobody; "Google Jamboard" does."""
    path = stub(tmp_path, f"thing{extension}",
                {"url": f"https://docs.google.com/x/d/ID{extension}/edit"})
    document = next(iter(CloudStubExtractor().extract(path)))
    assert expected in document.text


def test_an_older_drive_format_still_parses():
    """Drive has changed this JSON more than once, and old files keep the old
    shape. Reading only the current key set would lose every historic one."""
    info = parse_stub(json.dumps({
        "doc_url": "https://drive.google.com/open?id=OLD1",
        "resource_id": "OLD1",
    }).encode())

    assert info.url.endswith("OLD1")
    assert info.doc_id == "OLD1"


def test_the_id_is_recovered_from_the_url_when_it_is_not_a_field():
    """The id is stable across renames and is what somebody pastes to find the
    file again, so it is worth having even when Drive did not write it out."""
    info = parse_stub(json.dumps(
        {"url": "https://docs.google.com/spreadsheets/d/9ZzZ/edit"}).encode())
    assert info.doc_id == ""            # not a field

    from app.extract.cloudstub import _doc_id_from_url

    assert _doc_id_from_url(info.url) == "9ZzZ"


# ---------------------------------------------------------------------------
# When it is not a pointer at all
# ---------------------------------------------------------------------------

def test_a_file_that_is_not_json_is_a_precise_skip(tmp_path):
    """A `.gdoc` that is not JSON is a renamed file, and saying so is more
    useful than an empty result."""
    path = tmp_path / "renamed.gdoc"
    path.write_text("this is not json at all", encoding="utf-8")

    with pytest.raises(AppErrorException) as caught:
        list(CloudStubExtractor().extract(path))
    assert caught.value.error.code == "ERR_CLOUD_STUB"


def test_json_without_a_link_names_the_keys_it_did_find(tmp_path):
    """So the next person can add the key rather than guess at the format."""
    path = stub(tmp_path, "odd.gdoc", {"unrelated": "keys", "another": 1})

    with pytest.raises(AppErrorException) as caught:
        list(CloudStubExtractor().extract(path))
    assert "another, unrelated" in caught.value.error.details


def test_something_far_too_large_is_not_read_as_a_stub(tmp_path):
    """A pointer is a few hundred bytes. Anything bigger is a real document
    somebody renamed, and parsing megabytes as JSON to find that out is waste."""
    from app.extract import cloudstub

    path = tmp_path / "huge.gdoc"
    path.write_text("x" * (cloudstub.MAX_STUB_BYTES + 1), encoding="utf-8")

    with pytest.raises(AppErrorException) as caught:
        list(CloudStubExtractor().extract(path))
    assert caught.value.error.code == "ERR_CLOUD_STUB"


def test_parse_never_raises_whatever_it_is_given():
    """It is called on whatever is on disk, and a crash in an extractor takes an
    index worker with it."""
    for payload in (b"", b"null", b"[]", b"{", b"\xff\xfe\x00", b'"a string"', b"123"):
        assert parse_stub(payload).empty


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_every_pointer_extension_is_registered_and_switched_on():
    import app.extract  # noqa: F401 - importing populates the registry
    from app.core.formats import load_rules
    from app.extract.base import REGISTRY

    rules = load_rules()
    for extension in (".gdoc", ".gsheet", ".gslides"):
        assert REGISTRY.get(extension) is not None
        assert REGISTRY[extension].name == "cloudstub"
        rule = rules.rule_for(extension)
        assert rule is not None and rule.enabled, f"{extension} is still off"
