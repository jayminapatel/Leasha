r"""What each tunable is allowed to be **on this machine**.

Layer: L0 — one pure function per knob, taking a `ComputeProfile` and returning
`(floor, ceiling, auto, why)`.

**Two kinds of bound, and they are not the same thing.** The registry's static
`minimum`/`maximum` are the absolute limits - values outside them are wrong on
any machine, and they stay declarative and import-free. The envelope narrows
them *per machine*: eight index workers is legal in the abstract and absurd on
a dual-core laptop. So the registry keeps the hard bounds and this file keeps
the local ones, and nothing here can widen what the registry set.

**Cores are weighted for inference and counted for everything else**, and the
difference is not a detail. The owner's 13th-gen i7 reports ten cores: two
P-cores with hyper-threading and eight E-cores. An ONNX thread on an E-core
delivers a fraction of a P-core's throughput, so `onnx_threads` divides
`capacity()` rather than a flat count - which is the crudeness §0 identifies.

An *extraction* worker is different work: it opens a file, decodes it and hands
text on, waiting on the disk as much as it computes, and an E-core does that at
very nearly a P-core's rate. Weighting it produced two workers on the owner's
machine against the four in use today - a silent halving of his throughput,
justified by an estimate borrowed from the wrong kind of work. So
`index_workers` counts cores, and this paragraph exists because the first
version of this file did not make that distinction.

**Every bound carries its reason.** `why` is shown beside the control and
printed by `doctor`, because a ceiling somebody cannot account for is a ceiling
they route around by editing a file.

Pure: no store, no settings, no I/O. Testable entirely with invented profiles,
which is the point - the machines that matter are not the one running the test.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

__all__ = [
    "Bounds",
    "AUTO",
    "capacity",
    "index_workers",
    "onnx_threads",
    "picture_model_threads",
    "embed_batch",
    "embed_batch_from_rates",
    "index_memory_mb",
    "chat_max_rounds",
    "chat_verify_strictness",
    "chat_context_tokens",
    "oversubscription_warning",
    "for_setting",
    "ENVELOPES",
    "MEASURED_ENVELOPES",
]

#: The sentinel a setting holds when it has not been decided by hand.
#:
#: **Intent, not a number.** Storing the derived value would freeze this
#: machine's answer into the index, and the same index opened on a different
#: box would run on the old one's arithmetic - which is the whole failure §3b
#: exists to prevent.
AUTO = "auto"

#: How much of a P-core's work one E-core thread does.
#:
#: **0.4, and it is an estimate flagged as one.** Published single-thread
#: comparisons for Alder-Lake-and-later E-cores against their P-core siblings
#: land between roughly a third and a half on integer-heavy work; ONNX
#: inference is in that family. It is used only to *weight a core count*, so
#: being wrong by a tenth moves a worker count by less than one - and 5a's
#: measured auto-tune replaces the estimate with the machine's own number the
#: moment it has run.
E_CORE_WEIGHT = 0.4

#: The share of a core one extraction worker actually keeps busy.
#:
#: **A measurement, with a margin.** A real run on 2026-09-17/18 recorded
#: 24,841 worker-seconds of extraction across four workers in 97,444 seconds of
#: wall time - 6.4% - and a 90-document run recorded 130 across eleven in 1,055,
#: about 1%. A worker waits on the disk, on LibreOffice and on a full queue far
#: more than it computes. 0.25 is four times the larger figure, so a corpus of
#: nothing but heavy PDFs still fits inside it; the oversubscription warning
#: below still says so if it does not.
EXTRACT_WORKER_DUTY = 0.25


@dataclass(frozen=True)
class Bounds:
    """What a knob may be here, what it would be left alone, and why."""

    floor: int
    ceiling: int
    auto: int
    why: str

    def clamp(self, value: Any) -> tuple[int, Optional[str]]:
        """`(value, notice)`. **A clamp is never silent.**

        The notice names the old value, the new one and the reason, because
        "your setting was changed" without those three is a message that
        teaches nobody anything - and this fires exactly when somebody has
        carried an index to a smaller machine and is wondering why.
        """
        try:
            wanted = int(value)
        except (TypeError, ValueError):
            return self.auto, None
        if wanted < self.floor:
            return self.floor, (f"{wanted} raised to {self.floor}: {self.why}")
        if wanted > self.ceiling:
            return self.ceiling, (f"{wanted} lowered to {self.ceiling}: {self.why}")
        return wanted, None

    def resolve(self, value: Any) -> tuple[int, Optional[str]]:
        """The number to use for a stored intent - `auto` or an explicit one."""
        if value is None or str(value).strip().lower() == AUTO:
            return self.auto, None
        return self.clamp(value)


def capacity(profile: Any) -> float:
    """Usable parallel capacity, in P-core-equivalents.

    A flat core count is a lie on a hybrid part. This is the number the
    formulas below divide up, and it is deliberately conservative: a machine
    that under-uses its E-cores is slower than it could be, while one that
    over-schedules its P-cores is slower than it *was*.
    """
    performance = int(getattr(profile, "performance_cores", 0) or 0)
    efficiency = int(getattr(profile, "efficiency_cores", 0) or 0)
    if performance and efficiency:
        return performance + efficiency * E_CORE_WEIGHT

    physical = int(getattr(profile, "physical_cores", 0) or 0)
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    return float(physical or logical or 1)


def index_workers(profile: Any, *, hard_max: int = 32) -> Bounds:
    r"""Extraction workers.

    **Counted on physical cores, not on weighted capacity - and that is the
    correction.** The first version divided `capacity()` and produced *two*
    workers on the owner's 2P+8E laptop, against the four `default_workers`
    has been using. Halving somebody's throughput silently, on the strength of
    an E-core weighting invented for a different kind of work, is exactly the
    sort of "improvement" this order's §0 warns about.

    An extraction worker opens a file, decodes it and hands text on: it waits
    on the disk as much as it computes, and an E-core does that work at very
    nearly a P-core's rate. The weighting belongs to `onnx_threads`, where the
    work really is inference and really does scale with core class - which is
    also what §0 says the crude arithmetic got wrong.

    Half the cores, capped at four, floor one: `resources.default_workers`'
    reasoning kept, with a per-machine ceiling added. The person indexing is
    usually also using the computer, and an indexer that takes every core is
    one they turn off.
    """
    physical = int(getattr(profile, "physical_cores", 0) or 0)
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    cores = physical or logical or 1

    auto = max(1, min(4, cores // 2))
    ceiling = max(1, min(hard_max, cores))
    why = (f"{cores} core(s): half of them, capped at four, leaves the machine "
           f"usable while it indexes - extraction waits on the disk as much as "
           f"it computes, so this counts cores rather than weighting them")
    return Bounds(1, ceiling, min(auto, ceiling), why)


def embed_batch_from_rates(profile: Any, measured: Any) -> Optional[Bounds]:
    r"""`embed_batch`, sized by what the model actually managed here.

    **§5b, and the whole of the difference between Defaults and Auto-tune.**
    The heuristic below sizes a batch by installed memory, which is a proxy for
    the thing that matters and is wrong in both directions: a machine with
    plenty of memory and a slow processor gains nothing from a huge batch, and
    one on the graphics card wants a larger batch than its memory would
    suggest.

    Returns `None` whenever the measurement does not clearly justify a
    different answer - which is most of the time, and is the right default.
    A measured number that is close to the heuristic should not displace it:
    the two agreeing is not new information, and churning somebody's settings
    to say so is how a self-tuning system loses trust.
    """
    if measured is None:
        return None
    rates = dict(getattr(measured, "embed_per_second", {}) or {})
    if not rates:
        return None

    heuristic = embed_batch(profile)
    fastest = max(rates.values())
    if fastest <= 0:
        return None

    # **A batch is worth enlarging only when the model is fast enough to make
    # per-call overhead the cost.** Below roughly 20 chunks a second the call
    # overhead is already noise against the matrix work, and a bigger batch
    # buys nothing while holding more text in memory.
    if fastest < 20:
        return heuristic

    doubled = min(heuristic.ceiling, heuristic.auto * 2)
    if doubled <= heuristic.auto:
        return heuristic
    return Bounds(
        heuristic.floor, heuristic.ceiling, doubled,
        f"measured at {fastest:.0f} chunks a second on this machine, which is "
        f"fast enough that per-call overhead, not memory, is the limit",
    )


def onnx_threads(profile: Any, workers: Optional[int] = None) -> Bounds:
    """Intra-op threads for one ONNX session.

    Sized against **what the workers have not already taken**, because the two
    numbers multiply: the oversubscription this avoids is the commonest way a
    tuning screen makes a machine slower while every control reads "faster".
    """
    # **Here the weighting is the point.** Inference threads are the work that
    # really does run at a fraction of the speed on an E-core, which is the
    # distinction §0 draws and the reason `capacity` exists at all.
    usable = capacity(profile)
    count = int(workers if workers is not None else index_workers(profile).auto)
    # **A worker is charged what it uses, not a whole core.** This used to
    # subtract one core per worker, which on the owner's 2P+8E machine left
    # 1.2 cores and gave the model ONE thread - while embedding was 87% of the
    # run's wall time and the four workers were busy 6% of theirs. Measured
    # 2026-09-19 on 90 real documents: 1 thread 1,055 s; four 4-thread runs
    # 222-587 s (this machine varies about 2.5x run to run, so the size of the
    # gain is a range, not a number - see the order's section 0).
    taken = count * EXTRACT_WORKER_DUTY
    spare = max(1.0, usable - taken)
    auto = max(1, int(spare))
    logical = int(getattr(profile, "logical_processors", 0) or 0) or auto
    return Bounds(
        1, max(1, logical), auto,
        f"about {spare:.1f} core(s) are left once {count} worker(s) are "
        f"running; threads beyond that contend rather than help",
    )


def picture_model_threads(profile: Any, *, workers: Optional[int] = None,
                          meaning_threads: Optional[int] = None,
                          callers: int = 1) -> Optional[int]:
    r"""Intra-op threads for one picture model's ONNX session - CLIP, the face
    pack, the three OCR sessions. None when this machine cannot be described,
    and the caller then leaves the library's own default, as before.

    **2026-10-10, work order model-sequencing item 1d.** The meaning model's
    session was sized by `onnx_threads` above and every picture model was left
    at onnxruntime's default, which is one thread per physical core. On the
    owner's 2P+8E laptop (12 logical processors) that is ten threads for CLIP
    and ten for faces, on the `pictures` worker, while the four extraction
    workers and the meaning model's four threads are running too - 18 or more
    asked of a processor that has 12. Nothing here is a new number: every
    term is one this file already derives.

    **What is left of the processor, after what the run already asked for.**
    The logical processors (the yardstick `oversubscription_warning` uses),
    less the extraction workers (`index_workers`, or the run's own count),
    less the meaning model's threads (`onnx_threads`, or the run's own) -
    because during reading the picture worker runs beside both of them. Never
    more than the meaning model's own number, which is this file's answer to
    "how many threads one inference session can use here"; never fewer than
    one. On the owner's laptop: 12 - 4 - 4 = 4.

    **`callers` - threads that call one session at the same time.** An
    onnxruntime session has one intra-op pool, shared by every `Run` on it,
    and each calling thread works inside its own call as well (the pool is
    `intra_op_num_threads - 1` threads plus the caller). The OCR helper
    process calls its one engine from `HELPER_THREADS` threads at once, so
    each extra caller is one thread already busy, and is taken off. On the
    owner's laptop: 4 - 3 = 1, i.e. four pictures read side by side on four
    threads, where they were on forty.

    **A judgement to be measured, not a measurement.** The order's own
    acceptance is a before-and-after picture run on an idle laptop
    (`pipeline_bench`); fewer threads per session could, in principle, make
    the pictures pass slower while making the machine as a whole faster.
    That run is owed, and this docstring says so until it has happened.

    `workers` and `meaning_threads` are the run's resolved numbers when the
    caller has them (`index/resolve.py`); left None, the envelope's own Auto
    answers stand - which is what Defaults mode resolves to anyway, and the
    only answer a separate process (the OCR helper) can reach without
    resolving a run of its own.
    """
    physical = int(getattr(profile, "physical_cores", 0) or 0)
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    if not (physical or logical):
        # The same rule `resolve_for_run` keeps: a machine nothing is known
        # about is not a machine with one core, and answering "1" for it
        # would be a guess dressed as a bound.
        return None
    count = int(workers) if workers is not None else index_workers(profile).auto
    meaning = onnx_threads(profile, count)
    asked = int(meaning_threads) if meaning_threads else meaning.auto
    left = (logical or physical) - max(0, count) - max(0, asked)
    budget = max(1, min(meaning.auto, left))
    return max(1, budget - (max(1, int(callers or 1)) - 1))


def embed_batch(profile: Any) -> Bounds:
    """Chunks per embedding call.

    Bounded by **memory**, not by cores: a batch is text held at once, and the
    failure it prevents is an indexer that gets itself killed on a small
    machine rather than one that runs slowly on a large one.
    """
    ram_mb = int(getattr(profile, "ram_mb", 0) or 0)
    if ram_mb >= 16_000:
        auto, ceiling = 256, 1024
    elif ram_mb >= 8_000:
        auto, ceiling = 128, 512
    else:
        auto, ceiling = 64, 256
    return Bounds(
        8, ceiling, auto,
        f"{ram_mb / 1024:.0f}GB of RAM: a batch is text held in memory all at "
        f"once, and the ceiling is what keeps a large one from ending the run"
        if ram_mb else "memory unknown, so the conservative default applies",
    )


def index_memory_mb(profile: Any) -> Bounds:
    """The governor's ceiling on the indexer's own footprint.

    Half of what is installed, floored at 512MB. Half rather than more because
    the point of the ceiling is that the machine stays usable - a governor set
    to nearly all of memory is a governor that never fires until it is too late.
    """
    ram_mb = int(getattr(profile, "ram_mb", 0) or 0)
    if not ram_mb:
        return Bounds(512, 8_000, 4_000,
                      "memory could not be detected, so the default stands")
    auto = max(512, int(ram_mb * 0.5))
    ceiling = max(auto, int(ram_mb * 0.8))
    return Bounds(
        512, ceiling, auto,
        f"half of {ram_mb / 1024:.0f}GB, so the machine stays usable while it "
        f"indexes; above {ceiling / 1024:.0f}GB the ceiling stops protecting "
        f"anything",
    )


def chat_max_rounds(profile: Any) -> Bounds:
    """Searches the Chat tab may run for one question (work order 202626270611, 3e).

    A retry is one more search of the index, which is cheap, plus at most one call
    to the planning model (capped at 30 seconds by the engine) - the model call is
    what a small machine feels. Below 8GB the extra reload of a model is the cost
    that matters, so the automatic number is two; anywhere else, or when memory
    could not be detected, it is the ceiling of three.

    **The 8GB line is an estimate, and flagged as one.** Nobody has timed a retry
    on a small machine; it is chosen so that a machine which cannot keep two
    models resident does not spend its wait on a second planning call. The
    ceiling of three is the work order's own bound, not the machine's, and no
    setting can lift it.
    """
    ram_mb = int(getattr(profile, "ram_mb", 0) or 0)
    if ram_mb and ram_mb < 8_000:
        return Bounds(
            1, 3, 2,
            f"{ram_mb / 1024:.0f}GB of RAM: a retry can mean loading a second "
            f"model, which a small machine feels most, so Chat retries once at "
            f"most (an estimate - nobody has timed it here)")
    return Bounds(
        1, 3, 3,
        "another search of the index is cheap, and three is the most Chat is "
        "ever allowed - it stops earlier by itself as soon as it has enough"
        if ram_mb else
        "memory could not be detected, so the ordinary three searches apply")


def chat_verify_strictness(profile: Any) -> Bounds:
    """How closely a sentence must match its source, as a percentage.

    **Not a matter of the machine**, so the bounds are the same everywhere and
    the reason says what the number is. 70 is the value the verification tests
    and the shipped question set (`tests/fixtures/chat_eval.py`) were built on:
    a sentence needs seven in ten of its meaningful words in the passage it
    cites. Lower lets loosely-matching sentences through and higher throws out
    correct ones that quote a passage in other words. Figures, dates, names and
    quotations are checked exactly whatever this is.
    """
    return Bounds(
        30, 90, 70,
        "the same on every machine: a sentence needs seven in ten of its "
        "meaningful words in the passage it cites, the value the checking was "
        "built and measured on")


def chat_context_tokens(profile: Any) -> Bounds:
    """How many tokens of conversation the Chat model is asked to read at once.

    Ollama's own default is 4096 whatever the model was built for, which is a few
    pages: enough for one question and its passages, not for a conversation. The
    window is what the sliding memory is fitted to (`app/chat/memory.py`), and a
    bigger one costs memory (the model's key/value cache grows with it) and a longer
    load, so: **4096 below 8GB of RAM, 8192 anywhere else.** The ceiling is 32768 and
    the model's own limit still applies on top of it.

    **The 8GB line and the 8192 are estimates, flagged as such**: measured here only
    on a 34GB machine with CPU-only inference, where 8192 loaded and answered.
    """
    ram_mb = int(getattr(profile, "ram_mb", 0) or 0)
    if ram_mb and ram_mb < 8_000:
        return Bounds(
            2048, 32768, 4096,
            f"{ram_mb / 1024:.0f}GB of RAM: a bigger window makes the model use more "
            f"memory, so Chat reads four thousand tokens at a time (an estimate - "
            f"measured only on a 34GB machine)")
    return Bounds(
        2048, 32768, 8192,
        "enough for a long conversation plus the passages of one answer, and "
        "modest for the memory the model needs to hold it (an estimate - measured "
        "only on a 34GB machine)" if ram_mb else
        "memory could not be detected, so the ordinary eight thousand tokens apply")


def oversubscription_warning(profile: Any, workers: Any,
                             threads: Any) -> Optional[str]:
    """§3c's one rule worth a warning. None when there is nothing to say.

    **Warn, do not block.** A suboptimal machine configuration is the person's
    right, informed - what is not their right is being slower and not knowing.
    Illegal states are unreachable at the control; this is the legal one that
    disappoints.
    """
    logical = int(getattr(profile, "logical_processors", 0) or 0)
    try:
        total = int(workers) + int(threads)
    except (TypeError, ValueError):
        return None
    if not logical or total <= logical + 2:
        return None
    return (f"{int(workers)} workers + {int(threads)} ONNX threads "
            f"oversubscribe your {logical} threads - slower, not faster")


#: The knobs this file bounds, by the name the registry uses for them.
#:
#: Named rather than discovered, so a setting gains an envelope when somebody
#: decides what it should be here rather than by being added elsewhere.
ENVELOPES = {
    "INDEX_WORKERS": index_workers,
    "ONNX_INTRA_OP_THREADS": onnx_threads,
    "EMBED_BATCH": embed_batch,
    "INDEX_MEMORY_MB": index_memory_mb,
    # The Chat tab's two behaviours (work order 202626270611, 3e). Chat follows
    # the same three modes as the Indexing page's tuning screen: Defaults and
    # Auto-tune use `auto` below, Manual uses what was typed, clamped.
    "CHAT_MAX_ROUNDS": chat_max_rounds,
    "CHAT_VERIFY_STRICTNESS": chat_verify_strictness,
    "CHAT_CONTEXT_TOKENS": chat_context_tokens,
}

#: The knobs a *measurement* can improve on, by the same names.
#:
#: **Deliberately a short list.** Most bounds are about what the machine has -
#: memory, cores, disk - and no amount of measuring changes those. Only where
#: the heuristic is a proxy for something that can be timed does a measured
#: form earn its place, and each one here has to say what it does with the
#: number and when it declines to use it.
MEASURED_ENVELOPES = {
    "EMBED_BATCH": embed_batch_from_rates,
}


def for_setting(key: str, profile: Any, measured: Any = None) -> Optional[Bounds]:
    """The envelope for one setting, or None when it has no per-machine bound.

    None is the ordinary answer for most settings - a theme or a schedule does
    not depend on the hardware - and the caller shows the registry's static
    bounds for those.

    `measured` is §5b's half: when rates for *this* machine exist, a knob that
    has a measured form uses it and every other knob is unaffected. Passing
    None gives exactly the behaviour before Auto-tune existed, which is what
    Defaults mode wants and what every caller gets until it asks otherwise.
    """
    name = str(key or "").upper()
    if measured is not None:
        refined = MEASURED_ENVELOPES.get(name)
        if refined is not None:
            bounds = refined(profile, measured)
            if bounds is not None:
                return bounds

    builder = ENVELOPES.get(name)
    return builder(profile) if builder is not None else None
