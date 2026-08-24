"""Entities and edges without an LLM, from the text alone.

Layer: L6

**Why this is the default and not the fallback.** An LLM extracting entities is
better at knowing that "Jenny" and "J. Okonkwo" are the same person. It is also
non-deterministic, needs a model running, takes hours over a large corpus, and
cannot be tested. This module is none of those things: same text in, same graph
out, on any machine, in seconds. It is the floor the graph stands on, and the
LLM pass in `entities_llm.py` improves it rather than replacing it.

**What counts as an entity here.** Email addresses, filenames and capitalised
n-grams. That is a deliberately blunt instrument, and blunt is the right choice:
in technical documents and fifteen years of work email, the capitalised phrases
*are* the projects, sites, systems, standards and people. The value is not in
classifying them correctly, it is in knowing which ones keep turning up together.

**Why PMI rather than raw counts.** Every document containing the word "Project"
is not a finding. Pointwise mutual information asks whether two entities appear
together *more than chance would predict given how common each is*, so a pair
that co-occurs 30 times because both are ubiquitous scores near zero, while a
pair that co-occurs 8 times and almost never apart scores high. Raw
co-occurrence produces a hairball centred on the most common word in the corpus;
this produces edges someone would actually draw.

Everything here is a pure function over text and counts. No storage, no config,
no I/O - which is why the whole module is tested with literal strings.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

__all__ = [
    "Entity",
    "ChunkEntities",
    "extract",
    "pmi",
    "npmi",
    "score_edges",
    "MIN_ENTITY_CHARS",
    "MAX_ENTITY_WORDS",
    "STOPWORDS",
]

#: Below this an "entity" is an initial or an artefact of bad extraction.
MIN_ENTITY_CHARS = 3

#: A capitalised run longer than this is a heading or a title, not a name.
#: Measured against real documents: "Health And Safety Executive Guidance Note"
#: is a heading; nobody refers to it that way in a sentence.
MAX_ENTITY_WORDS = 5

#: Words that are capitalised for reasons other than being a name. The list is
#: short on purpose - it targets the two things that actually generate noise:
#: the first word of a sentence, and the calendar. Anything longer becomes a
#: maintenance burden that quietly deletes real entities ("March" the surname,
#: "May" the person) with no way to notice.
STOPWORDS = frozenset("""
the a an and or but if then else for nor yet so as at by from in into of on to
with without within over under this that these those there here it its their
his her our your my we you they he she i us them me him
is are was were be been being am do does did done have has had having
will would shall should can could may might must
not no all any both each few more most other some such only own same than too
very just now new next last first second third
please thanks thank regards dear hi hello best kind sincerely
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october november
december mon tue wed thu fri sat sun jan feb mar apr jun jul aug sep sept oct
nov dec
subject re fw fwd cc bcc sent from to date attachment attached
version note table figure appendix section page ref issue rev draft final
""".split())

#: Ordinary English words that a *single-word* entity is never allowed to be.
#:
#: This list is the direct result of the first run against a real corpus of
#: slide decks and product literature. The top entities came back as "Connect",
#: "Enterprise", "System", "Access", "Optimize", "Learn", "Use", "How",
#: "Customers", "Ability", "Slide" - and, because a slide heading is set in
#: capitals, the acronyms "DATA", "CLOUD" and "DESIGN". Every one of them is a
#: capitalised English word, not a name, and together they crowded out the
#: entities that were actually there.
#:
#: **It applies to single-word entities only.** "Data Cloud", "System Platform"
#: and "Customer FIRST" are real names and survive intact - the ambiguity exists
#: only for a lone word, where capitalisation is the sole evidence and heading
#: case destroys it.
#:
#: This is a judgement call with a cost: a company genuinely called "Connect"
#: becomes invisible as a one-word node. That is the right trade at this scale -
#: one missing node against several hundred that are all noise - and the list is
#: a module constant precisely so it can be argued with.
COMMON_WORDS = frozenset("""
connect connected connecting enterprise system systems access accessing account
optimize optimise optimized optimizing learn learning use using used usage user
users how why what when where who which customer customers client clients
ability abilities slide slides group groups team teams company companies
data cloud design designed designing digital solution solutions service services
product products platform platforms software hardware network networks
report reports reporting result results value values benefit benefits
process processes operation operations project projects program programme
manage managed management manager control controls controlled monitor monitoring
support supported supporting deliver delivered delivery build built building
create created creating improve improved improvement increase reduce reduced
performance quality safety security risk risks cost costs time times
work works working workflow business businesses industry industries market
technology technologies information insight insights knowledge experience
overview summary introduction conclusion agenda objective objectives goal goals
key main core new next more less best better good great full complete
today tomorrow yesterday year years month months week weeks day days
example examples case cases study studies detail details step steps
question questions answer answers problem problems challenge challenges
opportunity opportunities strategy strategies plan plans approach approaches
level levels type types area areas part parts item items number numbers
name names title titles chapter contents index reference references
start started starting end ending finish thank thanks welcome
contact see read view download discover explore find get make take meet join
register watch listen follow share click visit try ask tell show help let keep
put bring give need want know think say look come go about above below into
provide provided provides accelerate accelerated enable enabled enables ensure
deliver delivering drive driving unlock empower transform transforming
ideal operational flexible open hybrid real real-time realtime secure smart
fast easy simple powerful scalable global local modern unified integrated
trusted proven native agile lean green clean single multiple standard advanced
align aligned integrate integrated operate operating production produce
maintain maintenance engineer engineering execution simulation visualisation
""".split())

#: Lowercase words allowed *inside* a capitalised name without breaking it, so
#: "Bank of England" and "Department for Work and Pensions" survive as one
#: entity. Only these, and never at either end.
_CONNECTORS = frozenset({"of", "for", "and", "de", "van", "der", "von", "la", "le", "du", "da"})

_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")

#: A filename needs a real extension - 2 to 5 letters - or every decimal number
#: and every sentence ending in ".com" becomes a file.
#:
#: **No spaces, deliberately.** Real filenames often contain them, but a filename
#: sitting in a sentence has no left-hand delimiter, so a space-tolerant pattern
#: walks backwards through the prose and produces entities like "about the HACCP
#: review and attached Pasteuriser Report.docx". That was the first thing this
#: pattern did when tested. Losing the leading words of a spaced filename costs
#: precision on one node; keeping them poisons the graph with sentence fragments.
_FILENAME_RE = re.compile(r"\b[\w][\w\-.]{0,60}\.(?:[A-Za-z]{2,5})\b")

#: Extensions worth treating as a file entity. Deliberately the set this app can
#: index, plus archives: a graph node for "index.html" from a quoted URL is noise.
_FILE_EXTS = frozenset("""
pdf doc docx xls xlsx xlsm ppt pptx txt md csv tsv rtf odt ods odp
msg eml pst ost zip 7z rar tar gz dwg dxf vsd vsdx mpp one
""".split())

#: An all-caps run of 2-6 letters: ISA, HACCP, OEE, SCADA, MES. In this corpus
#: these are among the most useful nodes in the graph, and a case-insensitive
#: name matcher would lose them among ordinary words.
#: The internal `&` is not decoration: P&ID, R&D and H&S are all single terms in
#: this corpus, and without it the pattern splits them and files "ID" as a node.
#: Length is checked in `_acronyms`, not here: expressing "2 to 6 characters"
#: alongside the optional `&` groups needs a variable-width lookbehind, which
#: Python's `re` will not compile.
_ACRONYM_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:&[A-Z0-9]+)*\b")

#: An acronym shorter than this is an initial; longer than this is shouting.
_ACRONYM_MIN, _ACRONYM_MAX = 2, 8

#: Roman numerals from document and slide headings. Listed explicitly rather
#: than detected by pattern, because a pattern over {I V X L C D M} also matches
#: CD, DC, MD, IC and LCD - all real terms. "I" is absent: it is a pronoun and
#: already a stopword.
_ROMAN_NUMERALS = frozenset("""
II III IV VI VII VIII IX XI XII XIII XIV XV XVI XVII XVIII XIX XX
""".split())

_TOKEN_RE = re.compile(r"[A-Za-z][\w'’&.-]*")

_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True, slots=True)
class Entity:
    """One node. `key` is identity; `display` is what a person reads.

    Two surface forms with the same key are the same entity, which is the whole
    reason this is a pair rather than a string.
    """

    key: str
    display: str
    kind: str  # email | file | acronym | name


@dataclass(frozen=True, slots=True)
class ChunkEntities:
    """What one chunk contributed: how often each entity appeared in it."""

    counts: Mapping[Entity, int]

    def __iter__(self):
        return iter(self.counts)

    def __len__(self) -> int:
        return len(self.counts)


def normalise(text: str) -> str:
    """The identity function for entity keys: casefold and collapse whitespace.

    Both halves matter. Casefolding merges "ACME LTD" with "Acme Ltd"; collapsing
    whitespace merges a name that was line-wrapped in a PDF with the same name
    that was not. In a corpus of PDFs the second is the more common duplicate,
    and it is invisible in a rendered graph - two nodes with identical labels.
    """
    return _WS_RE.sub(" ", text).strip().casefold()


def _is_noise(word: str) -> bool:
    return word.casefold() in STOPWORDS


def _trim(word: str) -> str:
    """Strip trailing sentence punctuation the token pattern swallowed.

    Without this, "on Tuesday." yields the entity "Tuesday." - which is not in
    the stopword list, so the calendar leaks straight into the graph one full
    stop at a time. Leading punctuation cannot occur; the pattern starts on a
    letter.
    """
    word = word.rstrip(".,;:!?-'’")
    # Strip the possessive. Without it "AVEVA's" and "AVEVA" are two nodes with
    # different keys - which is exactly the duplicate `normalise` exists to
    # prevent, arriving through a door it does not cover. Both apostrophes,
    # because a Word document and a PDF disagree about which one they use.
    for possessive in ("'s", "’s", "'S", "’S"):
        if word.endswith(possessive):
            return word[: -len(possessive)]
    return word


def _title_case(word: str) -> bool:
    """Capital first letter, and not shouting. `Pensions` yes, `MES` no."""
    return bool(word) and word[:1].isupper() and not word.isupper()


def _looks_like_filename(word: str) -> bool:
    return "." in word and word.rsplit(".", 1)[-1].casefold() in _FILE_EXTS


def _emails(text: str) -> Iterable[Entity]:
    for match in _EMAIL_RE.finditer(text):
        raw = match.group(0).rstrip(".")
        yield Entity(key=raw.casefold(), display=raw, kind="email")


def _files(text: str) -> Iterable[Entity]:
    for match in _FILENAME_RE.finditer(text):
        raw = match.group(0).strip()
        ext = raw.rsplit(".", 1)[-1].casefold()
        if ext not in _FILE_EXTS:
            continue
        if "@" in raw:  # already claimed by the email pass
            continue
        yield Entity(key=raw.casefold(), display=raw, kind="file")


def _acronyms(text: str) -> Iterable[Entity]:
    for match in _ACRONYM_RE.finditer(text):
        raw = match.group(0)
        if not (_ACRONYM_MIN <= len(raw) <= _ACRONYM_MAX):
            continue
        if raw.casefold() in STOPWORDS or raw.isdigit():
            continue
        # A slide heading is set in capitals, so "DATA", "CLOUD" and "DESIGN"
        # arrive looking exactly like acronyms. They are ordinary words, and on
        # a corpus of decks they outnumber the real terms.
        if raw.casefold() in COMMON_WORDS:
            continue
        if raw in _ROMAN_NUMERALS:
            continue
        if not any(char.isalpha() for char in raw):
            continue
        yield Entity(key=raw.casefold(), display=raw, kind="acronym")


def _finish_run(run: Sequence[str], *, opened_sentence: bool = False) -> Optional[Entity]:
    """Turn a completed run of words into an entity, or reject it.

    A plain function taking the run as an argument rather than a closure over the
    loop's variable. The closure version worked - the generator was always
    consumed before the variable was rebound - but it worked by timing, and the
    day someone stores the generator instead of draining it, every entity in the
    document quietly becomes the last phrase in its sentence.
    """
    if not run:
        return None
    phrase = " ".join(run)
    # Count capitalised words only. "Department for Work and Pensions" is four
    # words that matter and two joining them; charging the cap for "for" and
    # "and" is how a real organisation gets rejected as a heading.
    content = [word for word in run if word.casefold() not in _CONNECTORS]
    if len(phrase) < MIN_ENTITY_CHARS or len(content) > MAX_ENTITY_WORDS:
        return None

    # At least one word has to be a word. Since ALL-CAPS tokens may now join a
    # run, "A B C are the options" otherwise yields "B C" - three characters,
    # two "words", and nothing at all.
    if not any(len(word) >= MIN_ENTITY_CHARS for word in content):
        return None
    # A run made entirely of ordinary words is a heading, however it is cased.
    # "DATA CLOUD DESIGN" off a slide, "Enterprise System" out of a brochure.
    # Checked against COMMON_WORDS only, not STOPWORDS, so a genuine name that
    # happens to contain one - "Customer FIRST" - survives.
    if content and all(word.casefold() in COMMON_WORDS for word in content):
        return None

    if len(run) == 1:
        only = run[0]
        # A lone word is only a name if capitalisation is real evidence. An
        # ALL-CAPS single word is an acronym and `_acronyms` owns it; a
        # capitalised English word is a heading or a sentence opening, and on a
        # corpus of slide decks those were most of the graph.
        if (
            _is_noise(only)
            or only.isupper()
            or only.casefold() in COMMON_WORDS
            or len(only) < MIN_ENTITY_CHARS
        ):
            return None
        # **A lone Title-Case word that opened its sentence carries no evidence
        # at all.** Its capital is grammar. A slide bullet is its own sentence,
        # so "Provide real-time insight" and "Accelerate delivery" were entering
        # the graph as the entities "Provide" and "Accelerate" - and no blocklist
        # can keep up with the supply of verbs.
        #
        # This costs nothing for a real name, because a name that matters is
        # mentioned mid-sentence somewhere in the corpus and that occurrence is
        # still collected. It only removes the ones whose *sole* evidence was a
        # capital letter in the one position where every word has one.
        if opened_sentence:
            return None
    return Entity(key=normalise(phrase), display=phrase, kind="name")


def _names(text: str) -> Iterable[Entity]:
    """Runs of capitalised words, with lowercase connectors allowed inside.

    The hard case is the first word of a sentence, which is capitalised for
    grammar rather than because it names anything. A single leading capitalised
    word that is also a stopword is dropped; a *run* is kept, because "Water
    treatment failed" and "Water Treatment Ltd failed" differ exactly there.
    """
    for sentence in re.split(r"(?<=[.!?:;\n])\s+", text):
        run: list[str] = []
        pending_connector: list[str] = []
        run_opened_sentence = False

        for position, token in enumerate(_TOKEN_RE.finditer(sentence)):
            word = _trim(token.group(0))
            # An empty token, or one the file pass already owns. Letting a
            # filename join a name run produces "Pasteuriser Report.docx" as a
            # second node beside the file node it duplicates.
            # A Roman numeral is heading numbering. It breaks a run rather than
            # joining it, so "OPEN HYBRID II" cannot slip past the
            # all-ordinary-words check on the strength of the "II".
            if not word or _looks_like_filename(word) or word in _ROMAN_NUMERALS:
                found = _finish_run(run, opened_sentence=run_opened_sentence)
                if found is not None:
                    yield found
                run, pending_connector, run_opened_sentence = [], [], False
                continue

            # An ALL-CAPS word joins a run rather than breaking it. Product and
            # organisation names mix the two constantly - "AVEVA System
            # Platform", "AVEVA CONNECT", "Customer FIRST" - and treating the
            # capitalised word as a wall fragmented every one of them into
            # pieces, which is where "System" and "Enterprise" came from as
            # standalone nodes. A lone ALL-CAPS word is still rejected in
            # `_finish_run` and picked up as an acronym instead.
            capitalised = word[:1].isupper()
            if capitalised:
                # A capitalised word at position 0 that is an ordinary word is
                # grammar, not part of a name: "The pump" must not yield "The",
                # and "Discover AVEVA Insight" is a product with an imperative
                # in front of it, not a three-word product.
                if position == 0 and not run and (_is_noise(word) or word.casefold() in COMMON_WORDS):
                    continue
                if pending_connector and not _title_case(word):
                    # "... Platform and AVEVA CONNECT": the word after the
                    # connector is ALL-CAPS, so this is a new name, not a
                    # continuation. Close the run and start again from here.
                    found = _finish_run(run, opened_sentence=run_opened_sentence)
                    if found is not None:
                        yield found
                    run, pending_connector, run_opened_sentence = [word], [], False
                    continue
                if not run:
                    run_opened_sentence = position == 0
                run.extend(pending_connector)
                pending_connector = []
                run.append(word)
            elif run and word.casefold() in _CONNECTORS and _title_case(run[-1]):
                # Hold it: only keep it if another capitalised word follows, so
                # "Bank of England" joins but "Acme of" does not trail one.
                #
                # `_title_case` on the preceding word is what stops "and" from
                # welding two separate names together. "SCADA and MES" is two
                # entities and one conjunction; "Work and Pensions" is one name.
                # The difference is that a genuine multi-word name is Title Case
                # around its connectors, while a conjunction between acronyms is
                # not - which held on every real example that produced a wrong
                # entity here.
                pending_connector.append(word)
            else:
                found = _finish_run(run, opened_sentence=run_opened_sentence)
                if found is not None:
                    yield found
                run, pending_connector, run_opened_sentence = [], [], False

        found = _finish_run(run, opened_sentence=run_opened_sentence)
        if found is not None:
            yield found


def extract(text: str) -> ChunkEntities:
    """Every entity in one chunk, with how many times each appeared.

    Counts are per chunk rather than per corpus because that is the unit the
    edges are built from: two entities are related if they appear in the *same
    chunk*, which is roughly "the same paragraph or two" and is a far stronger
    claim than "the same 80-page document".
    """
    counts: Counter[Entity] = Counter()
    for finder in (_emails, _files, _acronyms, _names):
        for entity in finder(text):
            counts[entity] += 1
    return ChunkEntities(counts=dict(counts))


def pmi(joint: int, count_a: int, count_b: int, total: int) -> float:
    """Pointwise mutual information, in bits.

    Zero means "exactly as often as chance would predict". Positive means the
    two attract each other. Undefined pairs (either never seen, or never seen
    together) return -inf, which sorts them to the bottom without a special case
    at every call site.
    """
    if joint <= 0 or count_a <= 0 or count_b <= 0 or total <= 0:
        return float("-inf")
    p_joint = joint / total
    p_a = count_a / total
    p_b = count_b / total
    return math.log2(p_joint / (p_a * p_b))


def npmi(joint: int, count_a: int, count_b: int, total: int) -> float:
    """PMI normalised to [-1, 1], which is what makes a threshold portable.

    Raw PMI's ceiling depends on how rare the pair is, so "keep edges above 3.0"
    means something different on a 200-chunk index than on a 2,000,000-chunk one
    and any default is wrong on arrival. Dividing by -log2(p(joint)) fixes the
    range: 1.0 is "these two never appear apart", 0.0 is chance, -1.0 is "these
    two never appear together". A threshold chosen once holds as the corpus grows.
    """
    value = pmi(joint, count_a, count_b, total)
    if value == float("-inf"):
        return -1.0
    p_joint = joint / total
    denominator = -math.log2(p_joint)
    if denominator == 0:  # the pair is in every chunk; no information in it
        return 0.0
    return value / denominator


def score_edges(
    pairs: Mapping[tuple[int, int], int],
    entity_chunk_counts: Mapping[int, int],
    total_chunks: int,
    *,
    min_weight: int = 2,
    min_npmi: float = 0.0,
) -> list[tuple[int, int, int, float]]:
    """Turn raw co-occurrence counts into weighted, scored edges.

    Returns `(a_id, b_id, weight, npmi)` with `a_id < b_id`, sorted strongest
    first. Two filters, both defaults chosen to be defensible rather than clever:

    `min_weight=2` drops pairs seen together exactly once. A single co-occurrence
    is indistinguishable from two words happening to land in the same chunk, and
    these pairs are the overwhelming majority by count - on a real corpus they
    are most of the table and none of the insight.

    `min_npmi=0.0` keeps only pairs that co-occur more than chance predicts.
    Anything at or below zero is a statement about how common the words are, not
    about a relationship between them.
    """
    edges: list[tuple[int, int, int, float]] = []
    for (raw_a, raw_b), weight in pairs.items():
        if weight < min_weight:
            continue
        a_id, b_id = (raw_a, raw_b) if raw_a < raw_b else (raw_b, raw_a)
        score = npmi(
            weight,
            entity_chunk_counts.get(a_id, 0),
            entity_chunk_counts.get(b_id, 0),
            total_chunks,
        )
        if score <= min_npmi:
            continue
        edges.append((a_id, b_id, weight, score))
    edges.sort(key=lambda edge: (-edge[3], -edge[2], edge[0], edge[1]))
    return edges


def pair_keys(entity_ids: Sequence[int]) -> Iterable[tuple[int, int]]:
    """Every unordered pair from one chunk's entities, each once, a < b.

    Quadratic in the number of entities in a chunk, which is fine at ~512 tokens
    and catastrophic if it is ever pointed at a whole document. The caller caps
    it; this function does not, because silently dropping entities to stay fast
    would make the graph wrong in a way nobody could see.
    """
    unique = sorted(set(entity_ids))
    for index, a_id in enumerate(unique):
        for b_id in unique[index + 1:]:
            yield (a_id, b_id)
