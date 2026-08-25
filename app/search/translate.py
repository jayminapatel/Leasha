r"""Plain English in, the query syntax that already exists out.

Layer: L8a

    "the safety report Dave sent me about Leeds before the audit last March"
        ->  safety report Leeds from:dave before:2025-03-01

**The model's entire job is to fill in a form that already exists.** It emits
the operators from `commands.py`, and the result goes through `parse_query()` -
which was written, tested and shipped in Layer 4. Three properties follow, and
they are why this design is safe rather than merely convenient:

* **A malformed translation is caught by existing code.** Nothing new validates
  anything; the parser that already had to be right is still the only judge.
* **There is no injection surface.** The model cannot express anything a person
  could not have typed into the box themselves, because the output is checked
  against a fixed grammar and discarded if it does not fit.
* **It is testable without a model.** Sentence in, query string out, asserted
  against an expected string. Every test in this project's suite runs with no
  Ollama installed anywhere.

**The rule that bends, stated precisely so it does not erode.** The standing
rule was "search never calls the LLM". It is now:

> **The retrieval path never calls the LLM.** Query *translation* may. It is
> optional, costs about a second, runs once before the search, and is always
> visible to the user. Plain keyword and semantic search remain instant and
> never touch a model.

A static test enforces the first sentence: nothing under `app/search/` imports
`app.llm` except this module, and `SearchEngine.search()` cannot reach it.

**Transparency is not polish.** The translated query goes into the search box,
where it can be read and edited. If AI silently rewrites a query, search becomes
unpredictable - "why did it find *that*?" - and unpredictable search over your
own archive is worse than blunt search, because you stop trusting the results.
Visible means a bad translation is a two-second correction rather than a
mystery.

**It never blocks a search.** Ollama missing, slow, or answering with nonsense
all fall back to running the raw text exactly as typed. The worst case is
today's behaviour.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, replace
from datetime import date
from typing import Any, Optional, Protocol

from app.core.errors import AppError, AppErrorException
from app.core.logging import logger
from app.search.commands import COMMANDS, examples_for_model, grammar_for_model
from app.search.query import parse_query

__all__ = [
    "Translation",
    "QueryTranslator",
    "build_prompt",
    "clean_output",
    "TRANSLATE_TIMEOUT_S",
    "MAX_QUERY_TOKENS",
    "TEST_SENTENCE",
    "MAX_OUTPUT_CHARS",
]

log = logger.bind(component="search.translate")

#: Seconds before giving up and searching the raw text. Five, because that is
#: about the longest anybody will wait having pressed a button that promised to
#: be quick - and the fallback costs nothing.
#: **Measured, not guessed.** The previous value was 5.0, chosen because it
#: felt responsive, and it made the feature fail on the owner's machine every
#: time: a trial question took 8.2s on a cold model, and the real prompt is
#: ~1,900 characters of generated grammar rather than a sentence. Every
#: translation timed out, and the message blamed Ollama for not answering.
#:
#: 30s because Interpret is an *explicit* button - somebody clicked it and is
#: willing to wait - it runs off the UI thread, and the first call of a session
#: also pays for loading the model into memory. A budget shorter than the work
#: is not responsiveness, it is a feature that never runs.
TRANSLATE_TIMEOUT_S = 30.0

#: Tokens the model may generate. A query is a handful of words - `from:chris
#: licence` is four tokens - and `clean_output` discards everything after the
#: first line anyway.
#:
#: **Ollama generates without limit unless told otherwise**, so a chatty model
#: was free to write paragraphs of explanation that were then thrown away, while
#: the caller waited for every token. That is what made mistral take over thirty
#: seconds to produce ten tokens of useful output. 64 is generous for a query
#: and still bounds the worst case tightly.
MAX_QUERY_TOKENS = 64

#: The sentence the Test button and `app.cli ollama --translate` use.
#:
#: Here rather than in the widget so it can be checked without a display - and
#: because it has one hard requirement that is easy to break by accident: **it
#: must not appear among the prompt's own examples.** A model handed the answer
#: in its own prompt copies it, and the test then reports success for a model
#: that cannot do the job. A test enforces this.
#:
#: It has a sender, a subject word and a date, so the answer shows whether the
#: model can produce operator syntax rather than only whether it can produce
#: text.
TEST_SENTENCE = "emails from chris about buying a licence last year"

#: Stop as soon as the line is finished. The prompt asks for one line and
#: `clean_output` keeps only the first, so anything after the newline is waste
#: by definition. Stopping is cheaper than generating and truncating.
#:
#: A leading newline would end generation instantly and yield nothing - but that
#: is a rejected-and-fall-back outcome, which is exactly what an empty answer
#: should be, rather than a crash or a wrong query.
STOP_SEQUENCES = ["\n"]

#: A reply longer than this is prose, not a query, whatever it says. Guards
#: against a model that decides to explain itself at length.
MAX_OUTPUT_CHARS = 400

#: The shortest an operator-free answer is allowed to be before the "translation
#: compresses" rule applies. A few words of pure keywords is a perfectly good
#: translation of a long sentence, so the rule only bites above this.
_MIN_PLAIN_WORDS = 4

#: Every operator name and alias the parser accepts.
_KNOWN = {spelling for command in COMMANDS for spelling in command.spellings}

#: `word:` at a word boundary - how an operator looks in the output.
_OPERATOR = re.compile(r"(?:(?<=\s)|^)([A-Za-z]+):", re.M)

#: Models like to wrap answers in fences or quote them whole.
_FENCE = re.compile(r"^\s*```[a-z]*\s*|\s*```\s*$", re.I)

#: "Here is the query:" and its many cousins.
_PREAMBLE = re.compile(
    r"^\s*(?:here(?:'s| is)|the|translated|query|output|result|answer)\b[^\n:]{0,40}:\s*",
    re.I,
)


class _Client(Protocol):
    """Just enough of `OllamaClient` to be faked in a test."""

    def health(self, *, force: bool = ...) -> bool: ...
    def has_model(self) -> bool: ...
    def generate(self, prompt: str, *, json_mode: bool = ..., temperature: float = ...,
                 timeout: Optional[float] = ..., max_tokens: Optional[int] = ...,
                 stop: Optional[list[str]] = ...) -> Any: ...


@dataclass(frozen=True, slots=True)
class Translation:
    """What to search for, and what to show the person.

    `query` is always safe to run: on any failure it is the raw text, so a
    caller can use it without checking anything. `changed` says whether it is
    worth putting in the search box.
    """

    raw: str
    query: str
    changed: bool = False
    #: Why it fell back, when it did. Shown quietly, not as an error - falling
    #: back is a normal outcome on a machine with no Ollama, not a failure.
    note: str = ""
    error: Optional[AppError] = None
    elapsed_s: float = 0.0
    from_cache: bool = False

    @property
    def used_model(self) -> bool:
        return self.changed and not self.from_cache


def build_prompt(sentence: str, *, today: Optional[date] = None) -> str:
    """The instruction, with the grammar generated from `commands.py`.

    Generated rather than written out, so a model can never be told about an
    operator the parser does not have. A plausible-but-wrong operator produces
    queries that match nothing and give no hint why, which is exactly the
    failure that makes AI search feel untrustworthy.
    """
    now = today or date.today()
    return (
        "Rewrite the user's sentence as a search query.\n\n"
        f"{grammar_for_model()}\n\n"
        f"Today is {now.isoformat()}. Write relative dates as YYYY-MM-DD.\n\n"
        "Reply with the query only - no explanation, no code fence.\n"
        "Use an operator only where the sentence states that constraint; "
        "keep everything else as plain words.\n\n"
        # **Examples, not more rules.** A 1.5B model given rules alone echoed
        # the sentence straight back, and the application reported that as
        # "nothing to interpret" about a sentence that plainly said "from
        # chris". Rules describe the format; examples demonstrate it, and a
        # small model pattern-matches far better than it reasons.
        f"{examples_for_model()}\n\n"
        f"Sentence: {sentence}\n"
        "Query:"
    )


#: Words that signal a constraint the parser could express. Used only to tell
#: "the sentence had nothing to interpret" apart from "the model did nothing
#: with it" - never to decide what to search for, which stays the parser's job.
_CONSTRAINT_WORDS = frozenset("""
from to sent subject about attachment attachments pdf pdfs doc docs docx
spreadsheet spreadsheets xlsx email emails mail before after since during
last yesterday today week month year years bigger smaller larger than mb kb gb
folder path named called except without not
""".split())


def _has_nothing_to_interpret(sentence: str) -> bool:
    """Is a sentence genuinely free of anything an operator could capture?

    Deliberately generous about what counts as a constraint: claiming a sentence
    is plain when it is not is the mistake being fixed here, and the cost of the
    opposite is only a slightly less specific message.
    """
    words = {word.strip(".,;:!?'\"").lower() for word in (sentence or "").split()}
    return not (words & _CONSTRAINT_WORDS)


def clean_output(text: str) -> str:
    """Strip the packaging models add, without touching the query itself."""
    cleaned = _FENCE.sub("", (text or "").strip())
    cleaned = _PREAMBLE.sub("", cleaned).strip()
    # A model that answers on several lines meant the first one.
    cleaned = cleaned.split("\n")[0].strip()
    # Whole-answer quoting: a model wrapping `"type:pdf leeds"` in quotes.
    #
    # **Only when the quoted content holds an operator.** `"critical control
    # point"` is a legitimate phrase search that happens to be the entire query,
    # and unquoting it would turn one phrase into three loose words - a
    # different search, made silently. The presence of an operator is what
    # distinguishes packaging from intent.
    if len(cleaned) > 1 and cleaned[0] == '"' and cleaned[-1] == '"' \
            and cleaned.count('"') == 2:
        inner = cleaned[1:-1].strip()
        if _OPERATOR.search(inner):
            cleaned = inner
    return cleaned


def _rejects(query: str, sentence: str = "") -> Optional[str]:
    """Why this output must not be used, or None if it is fine.

    Everything here is a reason to fall back to the raw text. **Never a partial
    query**: half a translation is worse than none, because it looks deliberate
    and quietly changes what was searched for.
    """
    if not query:
        return "the model returned nothing"
    if len(query) > MAX_OUTPUT_CHARS:
        return f"the model returned {len(query):,} characters, which is prose rather than a query"

    if query[0] in "{[":
        # `{"query": "type:pdf leeds"}` - a model that answered in JSON despite
        # being asked not to. The braces would be searched for literally.
        return "the model replied with JSON rather than a query"

    # An operator with no value: `type:` narrows nothing, so it would silently
    # be ignored and the person would get a wider search than they asked for.
    if re.search(r"(?:(?<=\s)|^)[A-Za-z]+:(?:\s|$)", query):
        return "the model produced an operator with no value"

    invented = {
        name.lower() for name in _OPERATOR.findall(query)
        if name.lower() not in _KNOWN
    }
    if invented:
        # `colour:red` parses as a bare term and would silently narrow nothing,
        # so a person would see unexplained results rather than an error.
        return f"the model invented an operator this app does not have: {', '.join(sorted(invented))}"

    if query.count('"') % 2:
        return "the model left a quote unclosed"

    try:
        parsed = parse_query(query)
    except Exception as exc:                     # noqa: BLE001 - any parser failure
        return f"the translated query did not parse: {exc}"

    if parsed.unknown_operators:
        return f"the parser rejected: {', '.join(parsed.unknown_operators)}"
    if not (parsed.has_text or parsed.has_filters):
        return "the translated query is empty"

    # A date the parser refused leaves the operator dropped rather than
    # applied, which is a silently *wider* search than the person asked for.
    for operator in ("after", "before"):
        if re.search(rf"(?:(?<=\s)|^){operator}:", query) and getattr(parsed, operator) is None:
            return f"the model produced a date {operator}: could not read"

    # **Prose and refusals, which are the common failure of a small model.**
    #
    # "I think you are looking for the safety report Dave sent, which should be
    # in the Leeds folder somewhere" parses perfectly well as a bag of terms,
    # and searching for it would quietly produce worse results than the
    # original sentence - the person's words plus the model's padding.
    # "I cannot help with that request" is the same problem in six words.
    #
    # The rule is principled rather than a keyword list: **translation
    # compresses.** Its job is to move constraints into operators and drop
    # filler, so an answer carrying no operator and no phrase must not be longer
    # than what was typed. A model that adds words without adding structure has
    # not translated anything.
    if not parsed.has_filters and not parsed.phrases and sentence:
        out_words = {word.strip(".,!?;:").lower() for word in query.split() if word.strip(".,!?;:")}
        in_words = {word.strip(".,!?;:").lower() for word in sentence.split()}

        if len(out_words) > max(len(sentence.split()), _MIN_PLAIN_WORDS):
            return "the model replied with prose rather than a query"

        # **The rule that catches a refusal, and the more principled of the
        # two.** "I cannot help with that request" is only six words, so a
        # length test lets it through - but it shares no content word with what
        # was typed. A translation with no operators has done nothing except
        # drop filler, so its words must come from the sentence. Words that did
        # not are the model talking rather than translating.
        borrowed = len(out_words & in_words)
        if out_words and borrowed * 2 < len(out_words):
            return "the model answered with its own words rather than a query"

    return None




class QueryTranslator:
    """Sentence to query, with a cache and a hard fallback to the raw text."""

    def __init__(
        self,
        client: Optional[_Client] = None,
        *,
        timeout_s: float = TRANSLATE_TIMEOUT_S,
        today: Optional[date] = None,
    ) -> None:
        self.client = client
        self.timeout_s = timeout_s
        self.today = today
        self._cache: dict[str, Translation] = {}
        self._warned = False

    def reconfigure(self, *, model: Optional[str] = None,
                    timeout_s: Optional[float] = None) -> None:
        """Change the model or its budget without a restart.

        **The cache is emptied.** Every entry in it was produced by the previous
        model, so keeping them would make a newly-chosen model appear to do
        nothing at all on any sentence tried before - and trying the same
        sentence again is the first thing anybody does after switching.
        """
        if timeout_s is not None:
            self.timeout_s = float(timeout_s)
        if model and self.client is not None and hasattr(self.client, "set_model"):
            self.client.set_model(model)
        self._cache.clear()
        # So the "unavailable" line is logged again if the new model also
        # fails: it is a different model, and that is news rather than noise.
        self._warned = False

    def client_name(self) -> str:
        """The model's name, for a message that has to name it. Never raises."""
        return str(getattr(self.client, "model", "") or "The model")

    def available(self) -> bool:
        """Whether the button should offer to do anything. Never raises."""
        if self.client is None:
            return False
        try:
            return bool(self.client.health() and self.client.has_model())
        except Exception:                        # noqa: BLE001 - a probe never fails a search
            return False

    def translate(self, sentence: str) -> Translation:
        """Translate, or return the raw text with a note saying why not.

        The return value is always usable. There is no error path for a caller
        to forget, because forgetting it would mean a search that does not run -
        and a search that does not run is a worse outcome than a blunt one.
        """
        raw = (sentence or "").strip()
        if not raw:
            return Translation(raw=raw, query=raw)

        cached = self._cache.get(raw)
        if cached is not None:
            # Repeating a search must not pay for the model twice. Pressing the
            # button again is the most natural thing to do after editing.
            return _replace_cached(cached)

        if self.client is None:
            return self._fallback(raw, "Interpreting needs Ollama, which is not configured.")

        started = time.monotonic()
        try:
            response = self.client.generate(
                build_prompt(raw, today=self.today),
                temperature=0.0,
                timeout=self.timeout_s,
                max_tokens=MAX_QUERY_TOKENS,
                stop=STOP_SEQUENCES,
            )
            text = getattr(response, "text", "") or ""
        except AppErrorException as exc:
            # **Say what actually happened.** This used to report "Ollama is not
            # answering" for every failure, including a timeout - so a machine
            # with a perfectly healthy Ollama was told to go and check Ollama.
            # `exc.error` already holds the real message; substituting a guess
            # for it threw away the only useful information in the exception.
            code = getattr(exc.error, "code", "")
            return self._fallback(
                raw,
                f"{exc.error.message} Your words were searched for as typed.",
                error=exc.error,
                elapsed_s=time.monotonic() - started,
                # A timeout is transient: the model may simply have been cold,
                # and the next press is usually the one that works. Caching it
                # would make the button permanently dead for that sentence.
                cache=code != "ERR_OLLAMA_TIMEOUT",
            )
        except Exception as exc:                 # noqa: BLE001 - boundary; never fail a search
            log.debug("translation failed: {}: {}", type(exc).__name__, exc)
            return self._fallback(
                raw, "Interpreting failed, so the words you typed were searched for as-is.",
                elapsed_s=time.monotonic() - started,
            )

        elapsed = time.monotonic() - started
        query = clean_output(text)
        reason = _rejects(query, raw)
        if reason is not None:
            log.info("translation rejected ({}): {!r}", reason, query[:120])
            return self._fallback(
                raw, f"Could not interpret that ({reason}), so it was searched for as typed.",
                elapsed_s=elapsed,
            )

        result = Translation(
            raw=raw, query=query, changed=query.strip() != raw, elapsed_s=elapsed,
        )
        if not result.changed:
            # **Two different things, and they were reported identically.**
            #
            # "Nothing to interpret" is a claim about the *sentence*, and it was
            # made about 'emails from chris about buying a licence' - which
            # plainly has something to interpret. What actually happened is that
            # the model handed the sentence straight back, which is a failure of
            # the model and should read as one, because the fix is a different
            # model rather than a different sentence.
            result = Translation(
                raw=raw, query=query, changed=False, elapsed_s=elapsed,
                note=(
                    "Nothing to interpret - searched for as typed."
                    if _has_nothing_to_interpret(raw)
                    else f"{self.client_name()} returned the sentence unchanged. "
                         "A different model may do better; searched for as typed."
                ),
            )
        self._cache[raw] = result
        log.info("translated in {:.2f}s: {!r} -> {!r}", elapsed, raw[:80], query[:80])
        return result

    def _fallback(
        self,
        raw: str,
        note: str,
        *,
        error: Optional[AppError] = None,
        elapsed_s: float = 0.0,
        cache: bool = True,
    ) -> Translation:
        """Search the raw text. Logged once per translator, not per key.

        Per-key logging would fill the log with the same line while somebody
        typed, which buries everything else at the moment it is needed.
        """
        if not self._warned:
            self._warned = True
            log.info("query translation unavailable: {}", note)
        result = Translation(raw=raw, query=raw, changed=False, note=note,
                             error=error, elapsed_s=elapsed_s)
        if cache:
            # A machine with no Ollama should not re-probe on every press of
            # the button. **But only permanent failures are cached.** A timeout
            # is transient - the model was cold, or the machine was busy - and
            # caching it made the second press return the first press's failure
            # instantly, which reads as the button being broken rather than the
            # model being slow.
            self._cache[raw] = result
        return result


def _replace_cached(translation: Translation) -> Translation:
    """The same answer, flagged as not having cost a model call."""
    return replace(translation, from_cache=True)
