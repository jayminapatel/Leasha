"""L8a: plain English to the query syntax that already exists.

Layer: L8a

**No test here needs a running Ollama.** The client is a fake returning canned
strings, which is possible only because the model's job is so narrow: sentence
in, query string out, asserted against an expected string.

The tests fall into two halves, and the second is the important one.

The **table** checks the happy path - that a sentence carrying a sender, a file
type or a date produces the operator for it.

The **junk** tests check what happens when the model misbehaves, which it will:
empty replies, prose, unclosed quotes, invented operators, ten thousand
characters. Every one must fall back to the raw text. Never a partial query -
half a translation is worse than none, because it looks deliberate and quietly
changes what was searched for, and the person has no way to tell.
"""

from __future__ import annotations

from datetime import date

import pytest

from app.search.query import parse_query
from app.search.translate import (
    MAX_OUTPUT_CHARS,
    QueryTranslator,
    build_prompt,
    clean_output,
)

TODAY = date(2025, 6, 15)


class FakeClient:
    """Returns a canned reply. Records how many times it was asked."""

    def __init__(self, reply: str = "", *, healthy: bool = True, raises: BaseException = None):
        self.reply = reply
        self.healthy = healthy
        self.raises = raises
        self.calls = 0
        self.prompts: list[str] = []

    def health(self, *, force: bool = False) -> bool:
        return self.healthy

    def has_model(self) -> bool:
        return self.healthy

    def generate(self, prompt: str, **_kwargs):
        self.calls += 1
        self.prompts.append(prompt)
        if self.raises is not None:
            raise self.raises

        class Response:
            text = self.reply

        return Response()


def translate(reply: str, sentence: str = "find something"):
    return QueryTranslator(FakeClient(reply), enabled=True, today=TODAY).translate(sentence)


# ---------------------------------------------------------------------------
# The table the work order asks for: sentence -> expected query
#
# Fifteen pairs covering sender, file type, dates in both directions, relative
# dates, phrases, exclusions, and combinations.
# ---------------------------------------------------------------------------

TABLE = [
    ("the safety report Dave sent about Leeds",
     "safety report Leeds from:dave"),
    ("emails from chris about buying a licence",
     "buying licence from:chris"),
    ("pdfs about the site survey",
     "site survey type:pdf"),
    ("spreadsheets with the budget figures",
     "budget figures type:xlsx"),
    ("anything from priya after June last year",
     "from:priya after:2024-06-01"),
    ("the contract we signed before March 2025",
     "contract signed before:2025-03-01"),
    ("presentations about onboarding from last month",
     "onboarding type:pptx after:2025-05-01"),
    ('the document that says "critical control point"',
     '"critical control point"'),
    ("safety reports but not the drafts",
     "safety reports -draft"),
    ("word documents in the Leeds project folder",
     "type:docx path:leeds"),
    ("emails from dave with a pdf attached about pricing",
     "pricing from:dave type:pdf"),
    ("the audit findings from two years ago",
     "audit findings after:2023-06-15"),
    ("everything about the pump station",
     "pump station"),
    ("the invoice from acme before the year end",
     "invoice acme from:acme before:2025-12-31"),
    ("meeting notes from chris last week, not the agenda",
     "meeting notes from:chris after:2025-06-08 -agenda"),
]


@pytest.mark.parametrize(("sentence", "expected"), TABLE, ids=[s[:34] for s, _ in TABLE])
def test_the_translation_is_used_verbatim_when_it_is_valid(sentence: str, expected: str) -> None:
    """The model's output is the query, unchanged, when the parser accepts it.

    Nothing here is testing the model - it is testing that a *valid* answer
    survives the pipeline intact. A translation that gets quietly rewritten on
    the way through would be untraceable from the search box.
    """
    result = translate(expected, sentence)

    assert result.query == expected
    assert result.changed
    assert result.note == ""


@pytest.mark.parametrize(("_sentence", "expected"), TABLE, ids=[s[:34] for s, _ in TABLE])
def test_every_expected_query_parses(_sentence: str, expected: str) -> None:
    """The table is only meaningful if every target is a query the app can run."""
    parsed = parse_query(expected)
    assert parsed.has_text or parsed.has_filters
    assert not parsed.unknown_operators


# ---------------------------------------------------------------------------
# Junk in - raw text out. Never a partial query.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("name", "reply"), [
    ("empty", ""),
    ("whitespace", "   \n  "),
    ("prose", "I think you are looking for the safety report that Dave sent, "
              "which should be in the Leeds folder somewhere."),
    ("unclosed quote", 'type:pdf "site survey'),
    ("invented operator", "colour:red leeds"),
    ("another invented one", "author:dave leeds"),
    ("ten thousand characters", "leeds " * 2000),
    ("only an operator name", "type:"),
    ("refusal", "I cannot help with that request."),
    ("json", '{"query": "type:pdf leeds"}'),
])
def test_junk_output_falls_back_to_the_raw_text(name: str, reply: str) -> None:
    sentence = "find the leeds safety report"
    result = translate(reply, sentence)

    assert result.query == sentence, f"{name}: produced {result.query!r}"
    assert not result.changed
    assert result.note, f"{name}: fell back without saying why"


def test_an_invented_operator_is_rejected_rather_than_passed_through() -> None:
    """`colour:red` would parse as a bare term and silently narrow nothing, so
    the person sees unexplained results rather than an error."""
    result = translate("colour:red leeds", "red things about leeds")

    assert result.query == "red things about leeds"
    assert "colour" in result.note


def test_a_date_the_parser_rejects_causes_full_fallback_not_a_partial_query() -> None:
    """The specific rule from the work order. Dropping just the date would
    silently widen the search - the person asked for "before March" and would
    get everything, with no indication that the constraint was lost."""
    result = translate("after:notadate leeds safety", "leeds safety since whenever")

    assert result.query == "leeds safety since whenever"
    assert "leeds safety" not in result.query.replace("leeds safety since whenever", "")


def test_a_date_operator_the_parser_rejects_falls_back_the_same_way() -> None:
    """`date:` (order "dates" §1a) is caught by the same rule: a bad value is
    an unknown operator, so the sentence runs as typed rather than widened."""
    result = translate("date:2017-13 leeds safety", "leeds safety in the 13th month")

    assert result.query == "leeds safety in the 13th month"
    assert "date:2017-13" in result.note


def test_output_longer_than_the_cap_is_prose_whatever_it_says() -> None:
    result = translate("x" * (MAX_OUTPUT_CHARS + 1), "anything")
    assert not result.changed


# ---------------------------------------------------------------------------
# Ollama absent, slow or broken
# ---------------------------------------------------------------------------

def test_no_client_at_all_still_returns_a_usable_query() -> None:
    """`translate` has no error path a caller can forget, because forgetting it
    would mean a search that does not run - worse than a blunt one."""
    result = QueryTranslator(None, enabled=True).translate("find the leeds report")

    assert result.query == "find the leeds report"
    assert not result.changed
    assert result.note


def test_ollama_unreachable_falls_back_and_the_search_still_runs() -> None:
    from app.core.errors import AppErrorException, make_error

    client = FakeClient(raises=AppErrorException(make_error(
        "ERR_OLLAMA_DOWN", "test", details="refused")))
    result = QueryTranslator(client, enabled=True, today=TODAY).translate("find the leeds report")

    assert result.query == "find the leeds report"
    assert result.error is not None and result.error.code == "ERR_OLLAMA_DOWN"


def test_any_unexpected_exception_is_still_a_fallback() -> None:
    """The boundary. A translator that raises would take a search down with it."""
    client = FakeClient(raises=RuntimeError("something nobody predicted"))
    result = QueryTranslator(client, enabled=True, today=TODAY).translate("find the leeds report")

    assert result.query == "find the leeds report"
    assert not result.changed


def test_the_fallback_is_logged_once_not_once_per_key() -> None:
    """Per-key logging would fill the log with the same line while somebody
    typed, burying everything else at the moment it is needed."""
    translator = QueryTranslator(None, enabled=True)
    translator.translate("one")
    translator.translate("two")
    translator.translate("three")

    assert translator._warned is True


def test_available_is_false_and_never_raises_when_the_probe_explodes() -> None:
    class Exploding:
        def health(self, *, force=False):
            raise OSError("network stack on fire")

        def has_model(self):
            return True

        def generate(self, prompt, **kw):
            raise AssertionError("should not be reached")

    assert QueryTranslator(Exploding(), enabled=True).available() is False


# ---------------------------------------------------------------------------
# §3c - composition with Ollama: rules run first, the model gets the residue
# ---------------------------------------------------------------------------

class FakeStore:
    """Just enough of `SqliteStore.distinct_values` for the rules to use."""

    def __init__(self, senders=(), recipients=(), exts=()):
        self._values = {"sender": senders, "recipient": recipients, "ext": exts}

    def distinct_values(self, kind: str, limit: int = 400):
        return self._values.get(kind, ())


STORE = FakeStore(senders=["dave.smith@acme.com"], exts=["pdf"])


def test_the_model_is_handed_the_residue_not_the_whole_sentence() -> None:
    """The point of §3c: a smaller prompt, because the deterministic part
    (here, the sender and the year) is already settled before the model is
    asked anything."""
    client = FakeClient("SHOULD NOT MATTER")
    translator = QueryTranslator(client, enabled=True, today=TODAY)

    translator.translate("Dave emailed the invoice about pricing", store=STORE)

    prompt = client.prompts[0]
    assert "Dave" not in prompt
    assert "invoice" not in prompt
    assert "Sentence: the about pricing" in prompt


def test_rules_and_the_models_answer_are_composed() -> None:
    """The chips the rules found and what the model made of what was left,
    joined into one query - neither half silently dropped."""
    client = FakeClient("pricing")            # borrows the residue's own word
    translator = QueryTranslator(client, enabled=True, today=TODAY)

    result = translator.translate(
        "Dave emailed the invoice about pricing", store=STORE)

    assert "from:dave.smith@acme.com" in result.query
    assert "type:pdf" in result.query
    assert "pricing" in result.query
    assert result.changed


def test_nothing_left_to_translate_means_no_model_call_at_all() -> None:
    """The residue can be empty - the rules claimed every word - and that is
    "smaller prompts" taken to its limit: no prompt, because there is nothing
    left to ask about."""
    client = FakeClient("SHOULD NOT BE CALLED")
    translator = QueryTranslator(client, enabled=True, today=TODAY)

    result = translator.translate("Dave emailed invoice 2024", store=STORE)

    assert client.calls == 0
    assert "from:dave.smith@acme.com" in result.query
    assert "type:pdf" in result.query
    assert result.changed


def test_ollama_absent_still_offers_the_rules_alone() -> None:
    """§3c's second sentence: with no Ollama, Interpret still works - "powered
    by rules alone" - it is only the depth of what gets extracted that
    changes, never whether the button does anything at all."""
    translator = QueryTranslator(None, enabled=True, today=TODAY)

    result = translator.translate(
        "the invoice Dave sent me last year", store=STORE)

    assert "from:dave.smith@acme.com" in result.query
    assert "type:pdf" in result.query
    assert result.changed
    assert "Ollama" in result.note


def test_a_rejected_model_reply_still_keeps_the_rules_half() -> None:
    """The model failing must not throw away what the rules already found -
    only its own contribution is lost, exactly like any other fallback."""
    client = FakeClient("I cannot help with that request.")
    translator = QueryTranslator(client, enabled=True, today=TODAY)

    result = translator.translate(
        "Dave emailed the invoice about pricing", store=STORE)

    assert "from:dave.smith@acme.com" in result.query
    assert "type:pdf" in result.query


def test_with_no_store_composition_never_engages() -> None:
    """No store means the rules cannot check real names or extensions, so
    nothing changes: the whole sentence goes to the model exactly as it did
    before §3c existed."""
    client = FakeClient("type:pdf leeds")
    translator = QueryTranslator(client, enabled=True, today=TODAY)

    result = translator.translate("pdfs about leeds")

    assert client.prompts[0].strip().endswith("Sentence: pdfs about leeds\nQuery:")
    assert result.query == "type:pdf leeds"


def test_a_store_set_on_the_translator_itself_is_used_too() -> None:
    """`store=` on `translate()` is the override; `QueryTranslator(store=...)`
    is what a caller with nowhere to pass it per-call would set instead."""
    client = FakeClient("SHOULD NOT BE CALLED")
    translator = QueryTranslator(client, enabled=True, today=TODAY, store=STORE)

    result = translator.translate("Dave emailed invoice 2024")

    assert client.calls == 0
    assert "from:dave.smith@acme.com" in result.query


def test_a_store_that_raises_costs_the_composition_not_the_search() -> None:
    """Never raises - a bad store degrades to today's whole-sentence
    behaviour rather than failing the search."""
    class Broken:
        def distinct_values(self, *_a, **_kw):
            raise RuntimeError("the index is locked")

    client = FakeClient("type:pdf leeds")
    translator = QueryTranslator(client, enabled=True, today=TODAY)

    result = translator.translate("pdfs about leeds", store=Broken())

    assert result.query == "type:pdf leeds"


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

def test_repeating_a_sentence_does_not_pay_for_the_model_twice() -> None:
    client = FakeClient("type:pdf leeds")
    translator = QueryTranslator(client, enabled=True, today=TODAY)

    first = translator.translate("leeds pdfs")
    second = translator.translate("leeds pdfs")

    assert client.calls == 1
    assert second.query == first.query
    assert second.from_cache and not first.from_cache


def test_a_failure_is_cached_too() -> None:
    """A machine with no Ollama should not re-probe on every press of the
    button - the answer will not have changed within one session."""
    translator = QueryTranslator(None, enabled=True)
    translator.translate("leeds")
    assert "leeds" in translator._cache


# ---------------------------------------------------------------------------
# Cleaning what models wrap around their answers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("raw", "expected"), [
    ("type:pdf leeds", "type:pdf leeds"),
    ("```\ntype:pdf leeds\n```", "type:pdf leeds"),
    ("```text\ntype:pdf leeds\n```", "type:pdf leeds"),
    ("Here is the query: type:pdf leeds", "type:pdf leeds"),
    ("Query: type:pdf leeds", "type:pdf leeds"),
    ('"type:pdf leeds"', "type:pdf leeds"),
    ("type:pdf leeds\nThat should find it.", "type:pdf leeds"),
    ("  type:pdf leeds  ", "type:pdf leeds"),
])
def test_packaging_is_stripped_without_touching_the_query(raw: str, expected: str) -> None:
    assert clean_output(raw) == expected


def test_a_real_phrase_search_is_not_unquoted() -> None:
    """`"critical control point"` is the whole query and legitimately quoted.
    Stripping those quotes would turn a phrase search into three loose words -
    a different search, silently."""
    assert clean_output('safety "critical control point"') == 'safety "critical control point"'


# ---------------------------------------------------------------------------
# The prompt, and the rule that must not erode
# ---------------------------------------------------------------------------

def test_the_prompt_only_mentions_operators_that_exist() -> None:
    from app.search.commands import COMMANDS

    prompt = build_prompt("anything", today=TODAY)
    for command in COMMANDS:
        # Order 0x §6a: a spelling row (`between:`) is told to the model under
        # the name of the filter it spells (`date:`), once.
        assert f"{command.alias_of or command.name}:" in prompt
    assert "colour:" not in prompt and "author:" not in prompt


def test_the_prompt_carries_todays_date_so_relative_dates_can_resolve() -> None:
    assert "2025-06-15" in build_prompt("last March", today=TODAY)


def test_the_retrieval_path_never_imports_the_llm() -> None:
    """The rule that bends, enforced so it does not erode.

    Query *translation* may call a model. Retrieval may not - plain keyword and
    semantic search must stay instant and must never depend on a process the
    user can stop.
    """
    import ast
    from pathlib import Path

    search_dir = Path(__file__).resolve().parents[2] / "app" / "search"
    offenders = []
    for path in search_dir.glob("*.py"):
        if path.name == "translate.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").startswith("app.llm"):
                offenders.append(path.name)
            if isinstance(node, ast.Import):
                offenders += [path.name for a in node.names if a.name.startswith("app.llm")]

    assert not offenders, f"these import app.llm and must not: {sorted(set(offenders))}"


def test_the_search_engine_cannot_reach_the_translator() -> None:
    """`SearchEngine.search()` must not be able to spend a second on a model.
    The user chooses to pay that cost, explicitly, before the search starts."""
    import ast
    from pathlib import Path

    engine = Path(__file__).resolve().parents[2] / "app" / "search" / "engine.py"
    text = engine.read_text(encoding="utf-8")

    assert "translate" not in text.replace("# ", ""), (
        "engine.py references translation; the retrieval path must not know it exists"
    )
    ast.parse(text)


def test_the_translated_query_reaches_the_caller_for_display() -> None:
    """Transparency is a hard requirement, so it is asserted rather than
    assumed. Invisible query rewriting is what makes AI search untrustworthy:
    "why did it find *that*?" has no answer, and people stop believing results
    they cannot account for.
    """
    result = translate("safety report Leeds from:dave", "the safety report dave sent")

    assert result.raw == "the safety report dave sent"
    assert result.query == "safety report Leeds from:dave"
    assert result.changed, "the UI uses this to decide whether to update the box"
