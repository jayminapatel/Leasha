"""Nothing in a previewed email may reach the internet.

Layer: L5

`QTextBrowser` does not execute JavaScript, so script is not the interesting
risk. **It does fetch remote images**, and a marketing email from 2009 is full
of tracking pixels that have been waiting fifteen years for somebody to open it.
Previewing a search result would phone home to the sender - silently, from a
machine whose founding promise is that nothing leaves it.

So the property under test is blunt: **after sanitising, no remote URL survives
anywhere in the output.** Several tests assert exactly that over the whole
string rather than checking a particular tag, because the way this fails in
practice is a shape nobody thought to check.
"""

from __future__ import annotations

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.ui.sanitise import sanitise_email_html, strip_to_text

#: Anything that would make a widget open a connection.
REMOTE_MARKERS = ("http://", "https://", "//evil", "ftp://", "file://")


def assert_nothing_remote(html: str) -> None:
    lowered = html.lower()
    for marker in REMOTE_MARKERS:
        assert marker not in lowered, f"{marker!r} survived: {html}"


# --- the leak that matters --------------------------------------------------

def test_a_tracking_pixel_is_removed_and_counted():
    result = sanitise_email_html(
        '<p>Hello</p><img src="http://tracker.example/open.gif?id=42" '
        'width="1" height="1">'
    )

    assert_nothing_remote(result.html)
    assert result.blocked_images == 1
    assert "Hello" in result.html
    assert "nothing was fetched" in result.notice()


@pytest.mark.parametrize("source", [
    "http://a.example/x.png",
    "https://a.example/x.png",
    "//a.example/x.png",              # protocol-relative, fetched over https
    "  HTTPS://A.EXAMPLE/x.png",      # spacing and case
    "ftp://a.example/x.png",
    "file://C:/Windows/system32/x.png",
])
def test_every_remote_image_form_is_blocked(source):
    result = sanitise_email_html(f'<img src="{source}">')

    assert_nothing_remote(result.html)
    assert "<img" not in result.html.lower()


def test_the_alt_text_survives_so_the_reader_knows_something_was_there():
    result = sanitise_email_html(
        '<img src="https://a.example/logo.png" alt="Acme logo">')

    assert "Acme logo" in result.html
    assert_nothing_remote(result.html)


def test_a_remote_link_is_readable_but_not_clickable():
    result = sanitise_email_html(
        '<a href="https://a.example/offer">Click here</a>')

    assert "Click here" in result.html, "the words are worth reading"
    assert_nothing_remote(result.html)
    assert result.blocked_links == 1


def test_a_mailto_link_is_kept():
    """Nothing is fetched by opening a composer."""
    result = sanitise_email_html('<a href="mailto:a@b.c">Email Dave</a>')

    assert "mailto:a@b.c" in result.html


def test_a_css_import_cannot_survive():
    """`@import` inside a style block is a remote fetch wearing a hat."""
    result = sanitise_email_html(
        '<style>@import url("https://a.example/mail.css");</style><p>Hi</p>')

    assert_nothing_remote(result.html)
    assert "@import" not in result.html
    assert "Hi" in result.html


# --- script and friends -----------------------------------------------------

@pytest.mark.parametrize("tag", ["script", "iframe", "object", "embed", "applet"])
def test_dangerous_tags_are_dropped_with_their_contents(tag):
    result = sanitise_email_html(
        f'<p>before</p><{tag} src="https://a.example/x">payload</{tag}><p>after</p>')

    assert f"<{tag}" not in result.html.lower()
    assert "payload" not in result.html
    assert_nothing_remote(result.html)
    assert "before" in result.html and "after" in result.html


def test_a_link_tag_cannot_pull_a_stylesheet():
    result = sanitise_email_html(
        '<link rel="stylesheet" href="https://a.example/mail.css"><p>Hi</p>')

    assert_nothing_remote(result.html)
    assert "<link" not in result.html.lower()


@pytest.mark.parametrize("attribute", [
    "onload", "onerror", "onclick", "onmouseover", "onfocus", "formaction",
])
def test_event_handlers_are_not_carried_through(attribute):
    """The allow-list removes these without needing to name them, which is why
    it is an allow-list: a block-list has to keep up with the specification."""
    result = sanitise_email_html(f'<p {attribute}="doSomething()">text</p>')

    assert attribute not in result.html.lower()
    assert "doSomething" not in result.html
    assert "text" in result.html


def test_a_javascript_url_is_refused_even_when_spaced_out():
    result = sanitise_email_html(
        '<a href="java\tscript:alert(1)">x</a><a href="JAVASCRIPT:alert(1)">y</a>')

    assert "javascript" not in result.html.lower().replace(" ", "")


# --- keeping the message readable -------------------------------------------

def test_ordinary_formatting_survives():
    result = sanitise_email_html(
        "<p>Dear <b>Jaymin</b>,</p><ul><li>one</li><li>two</li></ul>")

    for fragment in ("<p>", "<b>", "Jaymin", "<ul>", "<li>", "two"):
        assert fragment in result.html


def test_a_table_keeps_its_shape():
    result = sanitise_email_html(
        '<table border="1"><tr><td colspan="2">cell</td></tr></table>')

    assert "<table" in result.html and "<td" in result.html
    assert "colspan" in result.html


def test_an_unknown_tag_is_unwrapped_rather_than_dropped():
    """Losing the words because of a tag nobody recognised would be worse than
    the tag surviving - the point is to read the message."""
    result = sanitise_email_html("<marquee>important notice</marquee>")

    assert "important notice" in result.html
    assert "marquee" not in result.html.lower()


def test_malformed_markup_still_produces_the_words():
    result = sanitise_email_html("<p>unclosed <b>bold <i>both</p><<>>")

    assert "unclosed" in result.html and "both" in result.html


def test_nothing_in_means_nothing_out():
    assert sanitise_email_html("").html == ""
    assert not sanitise_email_html("").blocked_anything


def test_text_content_is_escaped_not_reinterpreted():
    """Words that look like markup must display as words."""
    result = sanitise_email_html("<p>use &lt;script&gt; carefully</p>")

    assert "&lt;script&gt;" in result.html


# --- the notice -------------------------------------------------------------

def test_the_notice_is_empty_when_nothing_was_removed():
    assert sanitise_email_html("<p>plain</p>").notice() == ""


def test_the_notice_counts_both_kinds():
    result = sanitise_email_html(
        '<img src="https://a/1.png"><img src="https://a/2.png">'
        '<a href="https://a/x">link</a>')

    notice = result.notice()
    assert "2 remote images blocked" in notice
    assert "1 link disabled" in notice


# --- the fallback -----------------------------------------------------------

def test_strip_to_text_removes_script_bodies_not_just_tags():
    text = strip_to_text("<p>keep</p><script>var secret = 1;</script>")

    assert "keep" in text
    assert "secret" not in text


# ---------------------------------------------------------------------------
# hypothesis (order 0m §2b) - the allow-list under fuzz, not just the hand
# -picked tags and schemes above. The property is the same one the module's
# own docstring states: it is an allow-list rebuild, so anything the
# generator invents that was never named must not survive.
# ---------------------------------------------------------------------------

#: A mix of allowed and disallowed tags, dangerous attribute names, and
#: remote/dangerous attribute values - built rather than `st.text()` alone,
#: because pure noise almost never happens to look like an `<img src=...>`
#: and the interesting failures live in exactly that shape.
_TAGS = st.sampled_from(
    ["p", "b", "script", "style", "iframe", "svg", "object", "unknowntag"])
_DANGEROUS_ATTRS = st.sampled_from(
    ["onclick", "onerror", "onload", "style", "src", "href"])
_DANGEROUS_VALUES = st.sampled_from([
    "http://tracker.example/x", "https://tracker.example/x",
    "//tracker.example/x", "javascript:alert(1)",
    "JAVASCRIPT:alert(1)", "ftp://x", "mailto:a@b.example", "cid:1",
])


@st.composite
def _html_snippet(draw) -> str:
    parts = []
    for _ in range(draw(st.integers(min_value=0, max_value=6))):
        tag = draw(_TAGS)
        attr = draw(_DANGEROUS_ATTRS)
        value = draw(_DANGEROUS_VALUES)
        text = draw(st.text(alphabet=st.characters(min_codepoint=97, max_codepoint=122),
                             max_size=8))
        parts.append(f'<{tag} {attr}="{value}">{text}</{tag}>')
    return "".join(parts)


@given(st.text(max_size=300))
def test_arbitrary_text_never_raises(markup: str) -> None:
    sanitise_email_html(markup)


@given(_html_snippet())
def test_no_remote_reference_ever_survives_under_fuzz(markup: str) -> None:
    result = sanitise_email_html(markup)
    assert_nothing_remote(result.html)


@given(_html_snippet())
def test_no_javascript_scheme_ever_survives_under_fuzz(markup: str) -> None:
    assert "javascript:" not in sanitise_email_html(markup).html.lower()


@given(_html_snippet())
def test_no_event_handler_attribute_ever_survives_under_fuzz(markup: str) -> None:
    html = sanitise_email_html(markup).html
    for attr in ("onclick", "onerror", "onload"):
        assert f"{attr}=" not in html.lower()


@given(_html_snippet())
def test_dropped_tags_never_survive_under_fuzz(markup: str) -> None:
    html = sanitise_email_html(markup).html.lower()
    for tag in ("script", "style", "iframe", "object"):
        assert f"<{tag}" not in html


@given(_html_snippet())
def test_blocked_counts_are_never_negative(markup: str) -> None:
    result = sanitise_email_html(markup)
    assert result.blocked_images >= 0
    assert result.blocked_links >= 0
