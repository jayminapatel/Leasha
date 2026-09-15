"""The one place a user-facing setting is declared.

Layer: L0

Non-negotiable 11 says every tunable has a UI or is not tunable. A rule nobody
can check is a rule that decays - which is exactly the finding of
`docs/REVIEW-2026-08-25.md`, where a documented search cache turned out never to
have been wired up. So the rule is enforced the way doc versions and handoff
currency already are: by a registry plus tests over it.

This module is **declarative only**. It reads nothing, writes nothing, and
imports nothing from the rest of the application, so the tests over it run in
milliseconds and cannot be broken by unrelated work.

Three tests give the rule teeth (`tests/unit/test_settings_registry.py`):

  * every key `config.py` reads appears here - a new setting without a control
    fails the suite
  * every entry names a UI surface that exists
  * every entry round-trips through the writer unchanged

`app/core/env_writer.py` is the other half: the application writes `.env`, the
user never does.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "Setting",
    "SETTINGS",
    "GROUPS",
    "SURFACES",
    "by_key",
    "by_group",
    "keys",
    "needs_restart",
]

#: Where a setting is shown. A registry entry naming a surface not listed here
#: is a typo, and the test says so rather than the control quietly not existing.
#:
#: **Renamed 2026-09-05, pages-reorg order §1d.** `settings.indexing` and
#: `settings.tuning` never named a place on the Settings page - they always
#: meant "the Indexing page's Schedule shelf" and "...Tuning shelf", which the
#: old `settings.*` prefix quietly implied otherwise. Now that both pages have
#: named categories (`app/ui/widgets/category_nav.py`), the surface name says
#: which page as well as which shelf: `indexing.schedule`, `indexing.tuning`.
#: Nothing about *which* controls build which surface changed - see
#: `tests/unit/test_settings_reachable.py`'s `SURFACE_MODULES`, unchanged
#: file-for-file, only renamed key-for-key.
SURFACES = (
    "settings.search",
    "indexing.schedule",
    #: The Index Tuning screen. Everything that decides **how fast a run goes
    #: and how much of the machine it takes**, in one place, because tuning by
    #: hunting across three panels is how a setting ends up changed twice and
    #: understood never. `indexing.schedule` keeps what is left: *when* a run
    #: happens, which is a different question.
    "indexing.tuning",
    "settings.reading",
    "settings.models",
    "settings.storage",
)

#: Display order of the groups. Not all of them are on the Settings page:
#: Schedule and Tuning are the Indexing page's own two categories (its third,
#: Status, holds no settings at all - see the pages-reorg order §2c, "a
#: control or a readout, never a setting shown twice").
GROUPS = ("Search", "Schedule", "Tuning", "Reading", "Models", "Storage")


@dataclass(frozen=True, slots=True)
class Setting:
    """One user-facing setting.

    `key` is the `.env` key, which is also the storage format - there is no
    second name to keep in step.

    `kind` drives the control the UI builds: bool -> checkbox, int -> spin box,
    choice -> combo, text -> line edit, path -> a flow (see `destructive`).

    `destructive` marks a setting that changes what the index *is* rather than
    how it behaves. Those are never plain fields: they state the cost and
    confirm, because silently repointing at an empty index or invalidating four
    million vectors is not something a text box should be able to do.
    """

    key: str
    label: str
    kind: str                       # bool | int | choice | text | path
    default: Any
    group: str
    surface: str
    help: str = ""
    minimum: Optional[int] = None
    maximum: Optional[int] = None
    unit: str = ""
    choices: tuple[str, ...] = ()
    restart: bool = False
    destructive: bool = False
    #: Set when a value is deliberately not a plain control, with the reason.
    #: The UI is expected to render the named flow instead.
    flow: str = ""


SETTINGS: tuple[Setting, ...] = (
    # --- Search ------------------------------------------------------------
    Setting(
        key="RERANK_ENABLED", label="Rerank results", kind="bool", default=True,
        group="Search", surface="settings.search",
        help="Slower and more precise. Reranking runs a second model over the "
             "top results; switch it off if search feels sluggish.",
    ),
    Setting(
        key="RERANK_TOP_N", label="Results to rerank", kind="int", default=30,
        group="Search", surface="settings.search", minimum=5, maximum=100,
        unit="results",
        help="How many results the reranker looks at. More is slower.",
    ),
    Setting(
        key="RERANK_WINDOW_CHARS", label="Text per result when reranking",
        kind="int", default=600, group="Search", surface="settings.search",
        minimum=200, maximum=2000, unit="characters",
        help="How much of each result the reranker reads.",
    ),

    # --- Search: what search may do on your behalf --------------------------
    #
    # **Six switches, not twenty-four.** Each names one behaviour, and each can
    # only turn it *off*: on means "follow this surface's own contract", which
    # is what keeps spelling correction away from identifiers on the Code tab
    # and plain-words notices away from people who wanted the technical ones.
    # See `app/search/policy.py` for the table of per-surface defaults.
    #
    # They ship **on**, per the order's §0: helpful behaviour is always-on and
    # individually switch-off-able, because people mature and nobody should be
    # forced in a direction.
    Setting(
        key="SEARCH_FIX_SPELLING", label="Fix obvious spelling", kind="choice",
        default="auto", group="Search", surface="settings.search",
        choices=("auto", "suggest", "off"),
        help="When a word matches nothing at all, look for the closest real "
             "word. Automatic uses it and says which word was used; Suggest "
             "offers it without changing what you typed. Never applies to "
             "code, where a misspelt name may be exactly what is there.",
    ),
    Setting(
        key="SEARCH_RELAX_ON_EMPTY", label="Try again with fewer words",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="When nothing matches all your words, drop the rarest one and "
             "show what matches the rest - labelled on the page, so you can "
             "see what was dropped. Off means an empty result stays empty.",
    ),
    Setting(
        key="SEARCH_AUTO_CHIPS", label="Offer filters it recognises",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="A name, a date or a kind of file in what you typed becomes a "
             "chip you can accept or dismiss. What you typed is never changed.",
    ),
    Setting(
        key="SEARCH_RECENCY_BLEND", label="Prefer recent documents",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="Among equally good matches, newer ones come first. It never "
             "hides an older document, it only orders them.",
    ),
    Setting(
        key="SEARCH_VERSION_FOLDING", label="Fold older versions together",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="Near-identical documents collapse into one row, newest shown, "
             "with the rest one click away. Off shows every copy separately.",
    ),
    Setting(
        key="SEARCH_PLAIN_WORDS", label="Explain in plain words", kind="bool",
        default=True, group="Search", surface="settings.search",
        help="Messages about what search could and could not do are written "
             "for anybody rather than for a developer. The power tabs keep "
             "the technical wording either way.",
    ),
    Setting(
        key="SEARCH_EXPLAIN_RESULTS", label="Say why a result is here",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="Each result can show what put it there - which of your words "
             "are in it, whether it was found by meaning, how recent it is, "
             "whether you have opened it before. Facts only: it never "
             "invents a score.",
    ),
    Setting(
        key="SEARCH_OFFER_RECENT", label="Offer what you searched for before",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="Clicking into an empty search box shows the last few things "
             "you looked for, and the searches you saved. Switch it off on a "
             "screen other people can see.",
    ),
    Setting(
        key="MINI_SEARCH_ENABLED", label="Search from anywhere with a shortcut",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="Press the shortcut in any application and a small search box "
             "appears. Type, press Enter, and the document opens.",
    ),
    Setting(
        key="MINI_SEARCH_HOTKEY", label="The shortcut that opens it",
        kind="text", default="Ctrl+Alt+L", group="Search",
        surface="settings.search",
        help="Something like Ctrl+Alt+L. It needs at least one of Ctrl, Alt, "
             "Shift or Win. If another program is already using it, Leasha "
             "says so and nothing changes.",
    ),
    Setting(
        key="MINI_SEARCH_PREFILL_SELECTION",
        label="Pre-fill the box from a text selection",
        kind="bool", default=True, group="Search", surface="settings.search",
        help="If you have text selected in another program when you press "
             "the shortcut, the box opens with it already typed in - "
             "selected, so the next keystroke replaces it. It never searches "
             "by itself.",
    ),
    Setting(
        key="CODE_EDITOR", label="Open code results in", kind="choice",
        default="auto", group="Search", surface="settings.search",
        choices=("auto", "vscode", "cursor", "vscodium", "sublime",
                 "notepadpp", "idea", "pycharm", "vim", "nvim", "emacs",
                 "none"),
        help="A code result opens at its line in this editor. Automatic uses "
             "the first one it finds installed. None keeps the old behaviour "
             "of showing the file in Explorer.",
    ),
    Setting(
        key="CODE_EDITOR_COMMAND", label="Or a command of your own",
        kind="text", default="", group="Search", surface="settings.search",
        help="For an editor not in the list. Use {path} and {line} where they "
             "belong - for example: myeditor --at {line} {path}. Left empty, "
             "the choice above is used.",
    ),

    # --- Indexing: *when* a run happens. How fast it goes is Tuning. -------
    Setting(
        key="INDEX_SCHEDULE", label="When to index", kind="choice",
        default="manual", group="Schedule", surface="indexing.schedule",
        choices=("manual", "interval", "daily"),
        help="Manual means it only runs when you ask.",
    ),
    Setting(
        key="INDEX_INTERVAL_HOURS", label="Index every", kind="int", default=6,
        group="Schedule", surface="indexing.schedule", minimum=1, maximum=168,
        unit="hours",
        help="Used when the schedule is set to interval.",
    ),
    Setting(
        key="INDEX_DAILY_AT", label="Index daily at", kind="text",
        default="02:00", group="Schedule", surface="indexing.schedule",
        help="24-hour time. Used when the schedule is set to daily.",
    ),

    # --- Tuning: how fast a run goes, and how much of the machine it takes --
    #
    # **One screen for the lot**, because these numbers interact. Workers and
    # ONNX threads multiply; a batch is memory and so is the ceiling that
    # governs it; and what gets *read* decides the length of a run as surely
    # as how quickly it is read. Split across three panels, somebody raises one
    # and is slower, and has no way to see why.
    #
    # Every one of them is bounded per machine by `app/core/envelope.py`, and
    # every bound carries its reason. See §3 of the index-tuning order.
    Setting(
        key="INDEX_TUNING_MODE", label="Tuning", kind="choice",
        default="defaults", group="Tuning", surface="indexing.tuning",
        choices=("defaults", "auto", "manual"),
        help="Defaults uses what this machine's specification implies. "
             "Auto-tune refines that with what past runs actually measured. "
             "Manual lets you set every control by hand, within the limits "
             "this machine allows. Switching back keeps your manual values "
             "stored but inert, so experimenting is reversible.",
    ),
    Setting(
        key="INDEX_WORKERS", label="Files read at once", kind="int", default=0,
        group="Tuning", surface="indexing.tuning", minimum=0, maximum=32,
        unit="threads",
        help="0 chooses a sensible number for this machine. Raise it only if "
             "indexing is slow and the machine is otherwise idle.",
    ),
    Setting(
        key="ONNX_INTRA_OP_THREADS", label="Threads per model call",
        kind="int", default=0, group="Tuning", surface="indexing.tuning",
        minimum=0, maximum=64, unit="threads",
        help="0 chooses for this machine: roughly the cores the file readers "
             "have not already taken. Threads beyond that contend rather than "
             "help, which is the commonest way a tuning screen makes a machine "
             "slower while every control reads faster.",
    ),
    Setting(
        key="EMBED_BATCH", label="Chunks per model call", kind="int",
        default=0, group="Tuning", surface="indexing.tuning",
        minimum=0, maximum=1024, unit="chunks",
        help="0 chooses for this machine's memory. A batch is text held in "
             "memory all at once, so the ceiling is what stops a large one "
             "ending a run on a small machine.",
    ),
    Setting(
        key="EMBED_QUANTISED", label="Use the smaller model file", kind="bool",
        default=False, group="Tuning", surface="indexing.tuning",
        restart=True,
        help="A quantised model is a few times smaller and faster on a "
             "processor, at a small cost in ranking quality. It buys nothing "
             "on a graphics card, so it is unavailable when one is in use.",
    ),
    Setting(
        key="INDEX_MEMORY_MB", label="Memory ceiling", kind="int", default=4000,
        group="Tuning", surface="indexing.tuning", minimum=256, maximum=16384,
        unit="MB",
        help="Indexing PAUSES rather than exceeding this. It does not fail, "
             "and nothing already indexed is lost.",
    ),
    Setting(
        key="INDEX_CPU_PERCENT", label="CPU ceiling", kind="int", default=80,
        group="Tuning", surface="indexing.tuning", minimum=10, maximum=100,
        unit="%",
        help="Indexing PAUSES while the whole machine is busier than this, so "
             "it gets out of the way of whatever you are doing.",
    ),
    Setting(
        key="INDEX_LOW_PRIORITY", label="Run at low priority", kind="bool",
        default=True, group="Tuning", surface="indexing.tuning",
        help="Lets everything else on the machine have the processor and the "
             "disk first.",
    ),
    Setting(
        key="INDEX_PAUSE_ON_BATTERY", label="Pause on battery", kind="bool",
        default=True, group="Tuning", surface="indexing.tuning",
        help="Indexing is the fastest way to flatten a laptop battery. It "
             "resumes on its own when you plug in; nothing is lost by waiting.",
    ),
    Setting(
        key="INDEX_TWO_PHASE", label="Make text searchable first", kind="bool",
        default=True, group="Tuning", surface="indexing.tuning",
        help="Words become searchable as soon as a file is read, and the "
             "meaning model catches up behind. On a large corpus that is the "
             "difference between search being useful on day one and on day "
             "fourteen. Nothing is skipped either way.",
    ),
    Setting(
        key="INDEX_BULK_FTS", label="Bulk-load the word index", kind="choice",
        default="auto", group="Tuning", surface="indexing.tuning",
        choices=("auto", "on", "off"),
        help="For a large first run, the word index is built once at the end "
             "rather than kept up to date file by file - much faster, but "
             "nothing is searchable until the run finishes. Automatic uses it "
             "only when the run is big enough to be worth it.",
    ),
    Setting(
        key="EMBED_DEDUP", label="Embed repeated text once", kind="bool",
        default=True, group="Tuning", surface="indexing.tuning",
        help="Signatures, disclaimers and boilerplate repeat across thousands "
             "of documents. Each distinct passage is sent to the model once "
             "and the result reused, which changes no result and saves the "
             "time.",
    ),
    Setting(
        key="INDEX_OCR_PASS", label="When to read images", kind="choice",
        default="with-run", group="Tuning", surface="indexing.tuning",
        choices=("with-run", "after-run", "manual"),
        help="Reading text out of an image costs about 3.6 seconds a page, so "
             "on a large corpus it decides how long a run takes. After-run "
             "leaves the rest of the index usable while the images are done.",
    ),
    Setting(
        key="INDEX_OCR_MODE", label="Images and scans", kind="choice",
        default="both", group="Tuning", surface="indexing.tuning",
        choices=("both", "text", "images"),
        help="Reading text out of an image takes about 3.6 seconds a page - "
             "roughly eight times what everything else costs - so on a large "
             "corpus it decides how long a run takes. Text first means search "
             "becomes useful in a day or two rather than a fortnight, with the "
             "images filled in afterwards.",
    ),
    Setting(
        key="INDEX_NAME_ONLY", label="Index every file by name", kind="bool",
        default=True, group="Tuning", surface="indexing.tuning",
        help="Records a row for every file, including the ones nothing can "
             "read - .zip, .mp4, .exe. They are findable by name; their "
             "contents are not searchable, and nothing is opened. Switch it "
             "off if you are indexing a media drive and do not want two "
             "million video files in the index.",
    ),
    Setting(
        key="ARCHIVE_RECHECK_DAYS", label="Re-check archived folders every",
        kind="int", default=30, group="Tuning", surface="indexing.tuning",
        minimum=0, maximum=365, unit="days",
        help="A folder marked as an archive is walked once and then left alone. "
             "It is still re-walked when the folder itself changes, when you "
             "ask for a rescan, and after this many days. 0 means only the "
             "first two.",
    ),
    Setting(
        key="ARCHIVE_READ_INSIDE", label="Read inside .zip archives", kind="bool",
        default=True, group="Tuning", surface="indexing.tuning",
        help="Files inside a .zip become searchable by their contents, not "
             "only by the archive's name. Encrypted members, anything nested "
             "more than two deep, and anything claiming an implausible "
             "expansion are recorded by name and never opened.",
    ),
    Setting(
        key="ARCHIVE_MAX_MB", label="Largest archive to read inside",
        kind="int", default=100, group="Tuning", surface="indexing.tuning",
        minimum=1, maximum=10000, unit="MB",
        help="A 40GB backup zip is indexed by name rather than read. Raising "
             "this is a decision about time: an archive's members are read one "
             "at a time and each one costs what that file would cost on disk.",
    ),
    Setting(
        key="OCR_WHITE_PAGE_PERCENT",
        label="How plain white a photo must be to count as a scanned page",
        kind="int", default=70, group="Tuning", surface="indexing.tuning",
        minimum=1, maximum=100, unit="%",
        help="Above this percentage of plain white, Leasha treats the image "
             "as a document and reads it in full, skipping the quicker check "
             "it would otherwise run first. Lower it to catch scanned pages "
             "with shading or colour; raise it if ordinary photos of pale "
             "backgrounds - snow, whiteboards, plain walls - are being read "
             "as documents unnecessarily.",
    ),
    Setting(
        key="PDF_OCR_PAGES", label="Pages to read from a scanned PDF",
        kind="int", default=0, group="Tuning", surface="indexing.tuning",
        minimum=0, maximum=500, unit="pages",
        help="0 leaves scanned PDFs unread, as they are today. A scanned "
             "manual is a document whose entire contents are unreachable; at "
             "about 3.6 seconds a page, 20 pages is roughly a minute a "
             "document and covers the title, contents and introduction - most "
             "of what makes it findable. Only the images pass uses this.",
    ),
    Setting(
        key="MIN_FREE_GB", label="Stop if free space drops below", kind="int",
        default=5, group="Tuning", surface="indexing.tuning",
        minimum=1, maximum=500, unit="GB",
        help="Indexing pauses rather than filling the disk. Progress is kept.",
    ),

    # --- Models ------------------------------------------------------------
    Setting(
        key="OLLAMA_URL", label="Ollama address", kind="text",
        default="http://127.0.0.1:11434", group="Models",
        surface="settings.models",
        help="Only used to interpret what you type. Search never calls it, so "
             "leaving it unreachable costs nothing but the Interpret button.",
    ),
    Setting(
        key="OLLAMA_MODEL", label="Local model", kind="text", default="mistral",
        group="Models", surface="settings.models",
        help="Any model you have pulled in Ollama.",
    ),
    # Work order 0i section 3. A separate setting from OLLAMA_MODEL - that one
    # answers query translation and graph enrichment, which want a small, fast
    # text model; Describe and the caption trickle need a vision-capable one
    # (llava/qwen-vl class), and pointing both features at the same field
    # would force one choice to serve two different jobs.
    Setting(
        key="OLLAMA_VISION_MODEL", label="Photo description model",
        kind="text", default="llava", group="Models", surface="settings.models",
        help="The Ollama model that answers Describe on a photo. Needs a "
             "vision-capable model - llava or qwen2.5vl are common choices. "
             "The Describe button is greyed out, with the exact command to "
             "pull one, until this model is actually installed.",
    ),
    # Work order 0i section 3b. OFF by default per the item's own text - a
    # description per photo costs seconds on CPU, corpus-wide that is hours
    # of a machine's own time nobody asked to spend, so this stays a
    # deliberate choice rather than something Auto-tune switches on for you
    # today. Non-negotiable #11: a real control, plain words, not a hidden
    # constant - the switch this item calls for.
    # Work order 0j (202626270512), the whole order's own guardrails: "OFF
    # by default behind one plain-words switch". Face embeddings are
    # biometric-adjacent, so this is the one switch in the whole application
    # whose help text has to name exactly what turning it on stores and what
    # turning it off - or Forget - removes, in the same sentence, per the
    # order's own instruction ("the whole feature's off-switch also states,
    # plainly, what stored data the switch governs").
    Setting(
        key="PEOPLE_RECOGNITION_ENABLED",
        label="Recognise people in photos on this computer",
        kind="bool", default=False, group="Models", surface="settings.models",
        help="Finds faces in your photos and groups similar ones into piles "
             "you can name - 'Daddy', 'Mum' - so you can search for people, "
             "the same way Google Photos does, except nothing ever leaves "
             "this computer. Off by default: this stores a description of "
             "each face's shape (numbers, not a picture) for every photo "
             "with a person in it, and only names YOU give a pile are ever "
             "attached to it - Leasha never guesses or suggests a name from "
             "anywhere else. Turning this off stops new faces being found; "
             "it does not delete what is already stored - use 'Forget this "
             "person' on the Photo Tagger page for that, per photo or per "
             "person.",
    ),
    Setting(
        key="CAPTION_TRICKLE_ENABLED",
        label="Describe photos in the background",
        kind="bool", default=False, group="Models", surface="settings.models",
        help="Slowly writes an AI description for every photo that does not "
             "have one yet, a few at a time, paced by the same battery/CPU "
             "limits indexing already respects - never enough to make a "
             "laptop hot in a lap. Off by default: describing a whole photo "
             "collection this way can take hours of the machine's own time. "
             "Needs the photo description model above to be installed.",
    ),
    # Grouped with Search, not Models, deliberately. **The Models panel is about
    # Ollama**, and a reranking field under that heading implies search calls
    # the LLM - which it never does, and which is the most persistent
    # misunderstanding about how this application works. The reranker is a small
    # local cross-encoder that search runs directly, so it belongs beside the
    # switch that turns reranking on and the two numbers that decide its cost.
    #
    # Agreed as a one-off crossing of the UI/Backend boundary; see
    # docs/WORKORDER-CONVENTIONS.md §3.
    Setting(
        key="RERANK_MODEL", label="Rerank model", kind="text",
        default="Xenova/ms-marco-MiniLM-L-6-v2", group="Search",
        surface="settings.search", restart=True,
        help="Changing this downloads a new model on next start.",
    ),
    # Both of the following change what the index *is*, not how it behaves.
    Setting(
        key="EMBED_MODEL", label="Meaning model", kind="text",
        default="BAAI/bge-small-en-v1.5", group="Models",
        surface="settings.models", restart=True, destructive=True,
        flow="rebuild-vectors",
        help="Changing this invalidates every vector in the index and requires "
             "re-embedding everything. The flow states the cost before it starts.",
    ),
    Setting(
        key="EMBED_DIM", label="Meaning model dimensions", kind="int",
        default=384, group="Models", surface="settings.models",
        minimum=64, maximum=4096, unit="dimensions", restart=True,
        destructive=True, flow="rebuild-vectors",
        help="Set by the meaning model. Changing it by hand cannot make an "
             "existing index work; it only makes the mismatch louder.",
    ),
    Setting(
        key="EMBED_DEVICE", label="Run models on", kind="choice",
        default="auto", group="Tuning", surface="indexing.tuning",
        choices=("auto", "cpu", "gpu"), restart=True,
        help="Which processor runs the meaning model, the reranker and OCR. "
             "Automatic uses the graphics card when this machine has one that "
             "works, and the processor otherwise. It is not destructive: the "
             "same model produces the same vectors either way, so switching "
             "does not invalidate an index.",
    ),

    # --- Storage -----------------------------------------------------------
    Setting(
        key="DATA_PATH", label="Index location", kind="path", default="",
        group="Storage", surface="settings.storage", restart=True,
        destructive=True, flow="move-index",
        help="Where the index lives. Typing a new path here would not move an "
             "index - it would point at a different, probably empty one. The "
             "flow offers move, use existing, or start new.",
    ),
    Setting(
        key="REQUIRED_FREE_GB", label="Free space needed to index", kind="int",
        default=300, group="Tuning", surface="indexing.tuning",
        minimum=1, maximum=10000, unit="GB",
        help="Checked before a run starts, on the index drive.",
    ),
)


def keys() -> frozenset[str]:
    """Every `.env` key with a control."""
    return frozenset(setting.key for setting in SETTINGS)


def by_key(key: str) -> Optional[Setting]:
    for setting in SETTINGS:
        if setting.key == key:
            return setting
    return None


def by_group() -> dict[str, list[Setting]]:
    """Settings grouped for display, in `GROUPS` order."""
    grouped: dict[str, list[Setting]] = {name: [] for name in GROUPS}
    for setting in SETTINGS:
        grouped.setdefault(setting.group, []).append(setting)
    return {name: items for name, items in grouped.items() if items}


def needs_restart() -> tuple[Setting, ...]:
    """Settings whose change only takes effect on the next start.

    The UI must say so on the control. A change that appears to work and does
    not is worse than one that is refused.
    """
    return tuple(setting for setting in SETTINGS if setting.restart)


#: Keys that must never be removed from `.env`, whatever asks.
#:
#: **"Restore defaults" deleted every one of these and the application could
#: not start again.** `defaults.py` sent `{key: None for key in pinned}` and
#: `env_writer.render` dutifully dropped each line - correct behaviour on its
#: own, because `None` is how a key gets *unpinned* so a better code default
#: can reach an existing install. That mechanism exists for `RERANK_MODEL`,
#: which the installer froze on a model 9.2x slower than the code's choice.
#:
#: It is nonsense for a path. `DATA_PATH` has `default=""`, so "restore the
#: default" means "leave the index location blank", and `load_settings` then
#: refuses to start with *"'' does not exist"* - before logging is configured,
#: so with no log line either. The owner's `.env` lost `DATA_PATH`,
#: `VECTOR_PATH`, `FTS_DB`, `CACHE_PATH`, `MODEL_CACHE` and `STATE_PATH` in one
#: press, and the only symptom was an application that would not open.
#:
#: A setting is protected when it is `destructive` - it changes what the index
#: *is* - or when it is a path with no usable default. Both mean the same
#: thing: there is nothing to fall back to, so removal is not an operation.
#: Where everything lives. **Not all of these are in `SETTINGS`**, and that is
#: exactly why they are listed by name.
#:
#: `DATA_PATH` is a registered setting; `FTS_DB`, `VECTOR_PATH`, `CACHE_PATH`,
#: `MODEL_CACHE`, `STATE_PATH`, `PROJECT_PATH` and `LOG_PATH` are not - they are
#: written by the installer and by the index-move flow, and read by
#: `load_settings`. Deriving the protected set from the registry alone would
#: therefore have guarded one of the seven and left the application just as
#: unable to start.
#:
#: The owner's `.env` lost six of these and the only symptom was a window that
#: never appeared - `load_settings` refuses before logging is configured, so
#: there is not even a line saying which key went.
#: **Only the roots.** `VECTOR_PATH`, `FTS_DB`, `CACHE_PATH`, `MODEL_CACHE` and
#: `STATE_PATH` are deliberately *not* here: they derive from `DATA_PATH`, and
#: `index_move` removes them on purpose - a sub-path left pinned outranks
#: `DATA_PATH` and silently strands that part of the index on the old drive,
#: which is the bug that made a move look like a no-op.
#:
#: The first version of this set listed all eight, and
#: `test_the_pinned_subpaths_are_removed_not_rewritten` caught it immediately -
#: a guard against losing data that would have broken moving data.
LOCATION_KEYS: frozenset[str] = frozenset({
    "DATA_PATH", "PROJECT_PATH", "LOG_PATH",
})


def protected() -> frozenset[str]:
    """Keys nothing may remove from `.env`. See the note above.

    Two sources, deliberately. The registry contributes anything `destructive`
    or any path with no usable default; `LOCATION_KEYS` contributes the ones
    that never reached the registry at all. A key belongs here when removing it
    leaves nothing to fall back to.
    """
    from_registry = frozenset(
        setting.key for setting in SETTINGS
        if setting.destructive or (setting.kind == "path" and not setting.default)
    )
    return from_registry | LOCATION_KEYS


def resettable() -> tuple[Setting, ...]:
    """Everything a "restore defaults" may legitimately unpin."""
    guarded = protected()
    return tuple(setting for setting in SETTINGS if setting.key not in guarded)
