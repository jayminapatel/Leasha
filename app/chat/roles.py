"""Which model plays which part - section 4d of the work order.

Layer: L8b - pure. No network: it is handed the list of installed models
(`OllamaClient.available_models()`) and decides.

**Three roles, one cost rule: cheap first.**

* **router** - decides what kind of question this is. Only ever asked when the
  rules could not tell, so it is called on a minority of questions. A 1-3B model.
* **planner** - turns a question into search queries, and proposes further ones
  when the first round was thin. Small, for the same reason.
* **answerer** - reads the passages and writes the answer. The one that is
  worth spending on: the strongest model the machine can afford.

**The default is one model everywhere** (section 4d: "the performance-correct
default - Ollama's residency and reload cost make one resident model right for
most machines"). Every role left empty inherits; the split into a small model for
router/planner and a bigger one for the answerer happens only when *installed
models make it free to*: a small chat model is already pulled and the configured
one is bigger. Reloading a 4GB model to answer a one-word routing question is not
cheap, so the small model has to genuinely be small - `SMALL_MODEL_B` billion
parameters or fewer - or it is not used for the small roles.

`EMBED_MODEL` is not a role. It is a rebuild decision and has its own home.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from app.llm.models import parameter_billions, rank

__all__ = [
    "RoleModels", "resolve_roles", "suggest_modes", "SMALL_MODEL_B", "AFFORDABLE_MAX_B",
    "InstalledModels", "is_vision_name", "size_of", "ram_line", "NO_VISION_MODEL_LINE",
    "OLLAMA_UNREACHABLE_LINE", "MEMORY_OVERHEAD",
    "ModelOption", "option_value", "parse_option", "answer_options", "default_option",
    "fits_in_memory", "PRELOAD_SHARE",
]

#: A model this size or smaller may play router/planner.
SMALL_MODEL_B = 3.0

#: The largest model chosen *automatically* for the answerer. Larger ones can
#: still be picked by hand; this is only where "choose for me" stops. A 20B model
#: on a CPU is a minute a sentence, and choosing it for somebody unasked is a
#: worse mistake than choosing a weaker one.
AFFORDABLE_MAX_B = 9.0


@dataclass(frozen=True)
class RoleModels:
    router: str = ""
    planner: str = ""
    answerer: str = ""
    #: Plain words about any choice that was made for the person - shown in the
    #: debug pane and beside the model dropdown.
    notes: tuple[str, ...] = field(default_factory=tuple)

    def distinct(self) -> tuple[str, ...]:
        """The models that would need to be resident, without repeats."""
        seen: list[str] = []
        for name in (self.router, self.planner, self.answerer):
            if name and name not in seen:
                seen.append(name)
        return tuple(seen)


def _installed_match(name: str, installed: Sequence[str]) -> Optional[str]:
    """The installed spelling of `name` (`mistral` -> `mistral:latest`), or None."""
    wanted = (name or "").strip()
    if not wanted:
        return None
    bare = wanted.split(":")[0]
    for candidate in installed:
        if candidate == wanted:
            return candidate
    for candidate in installed:
        if candidate.split(":")[0] == bare and ":" not in wanted:
            return candidate
    return None


def resolve_roles(
    installed: Sequence[str],
    *,
    configured: str = "mistral",
    router: str = "",
    planner: str = "",
    answerer: str = "",
) -> RoleModels:
    """Assign a model to each role.

    Explicit choices win, exactly as typed - a name that is not installed is kept
    (and noted), because silently swapping the model somebody chose is the fault
    `app.llm.models.choose` was written to avoid. Empty roles are filled cheaply.
    """
    notes: list[str] = []
    usable = [c for c in rank(installed) if c.selectable]
    names = [c.name for c in usable]

    def keep(role: str, wanted: str) -> str:
        if installed and _installed_match(wanted, installed) is None:
            notes.append(f"The {role} model '{wanted}' is not installed; "
                         f"run: ollama pull {wanted}")
        return wanted

    # ---- the answerer -------------------------------------------------------
    chosen_answerer = ""
    if answerer.strip():
        chosen_answerer = keep("answering", answerer.strip())
    elif configured and (not installed or _installed_match(configured, installed)):
        chosen_answerer = configured
    elif usable:
        affordable = [c for c in usable
                      if c.billions is not None and SMALL_MODEL_B < c.billions <= AFFORDABLE_MAX_B]
        pick = max(affordable, key=lambda c: c.billions or 0) if affordable else usable[0]
        chosen_answerer = pick.name
        notes.append(f"'{configured}' is not installed, so answers use {pick.name}.")
    else:
        chosen_answerer = configured

    # ---- the small roles ----------------------------------------------------
    small = next((c for c in usable
                  if c.billions is not None and c.billions <= SMALL_MODEL_B), None)
    inherited = chosen_answerer
    answerer_size = parameter_billions(chosen_answerer)
    if answerer_size is not None and answerer_size <= SMALL_MODEL_B:
        # The answerer is already a small model: giving the small roles a second
        # one would make Ollama hold (or reload) two for no saving at all.
        small = None

    def small_role(role: str, explicit: str) -> str:
        if explicit.strip():
            return keep(role, explicit.strip())
        if small is not None:
            return small.name
        return inherited

    chosen_router = small_role("routing", router)
    chosen_planner = small_role("planning", planner)

    if small is not None and len({chosen_router, chosen_planner, chosen_answerer}) > 1 \
            and not (router.strip() or planner.strip()):
        notes.append(f"Routing and planning use the small model {small.name}"
                     f"; answers use {chosen_answerer}.")

    return RoleModels(router=chosen_router, planner=chosen_planner,
                      answerer=chosen_answerer, notes=tuple(notes))


def suggest_modes(installed: Sequence[str], configured: str = "mistral") -> dict[str, str]:
    """`{"fast": name, "thoughtful": name}` for the tab's plain-words control.

    Fast is the smallest usable chat model; Thoughtful the strongest affordable
    one. With a single model installed both are that model, and the tab says so
    rather than offering a choice that changes nothing.
    """
    usable = [c for c in rank(installed) if c.selectable]
    if not usable:
        return {}
    fast = next((c for c in usable if c.billions is not None), usable[0]).name
    affordable = [c for c in usable if c.billions is None or c.billions <= AFFORDABLE_MAX_B]
    known = [c for c in affordable if c.billions is not None]
    configured_hit = _installed_match(configured, installed)
    if configured_hit and configured_hit in [c.name for c in affordable]:
        thoughtful = configured_hit
    elif known:
        thoughtful = max(known, key=lambda c: c.billions or 0).name
    else:
        thoughtful = affordable[-1].name if affordable else usable[-1].name
    return {"fast": fast, "thoughtful": thoughtful}


# ---------------------------------------------------------------------------
# What is installed, and what the choices cost in memory (work order 4d)
# ---------------------------------------------------------------------------

#: Name fragments of models that read pictures, used only when Ollama's own
#: `capabilities` list (`/api/show`) is missing - older builds do not send it.
VISION_NAME_HINTS = ("llava", "bakllava", "moondream", "minicpm-v", "-vl", "vl:", "vision",
                     "qwen2.5vl", "qwen3-vl")

#: How much more memory a resident model takes than its file: the context window
#: (Ollama's key/value cache) and working buffers. **An estimate, and flagged as
#: one** - it moves with the context length and the model, and nobody has measured
#: it here. It is used only to say "about N GB", so being off by a fifth changes a
#: sentence, not a decision.
MEMORY_OVERHEAD = 1.2

NO_VISION_MODEL_LINE = ("None of the models installed here can read pictures. To describe photos, "
                        "install one - for example run: ollama pull llava")
OLLAMA_UNREACHABLE_LINE = ("Leasha could not reach Ollama, so these lists show only what is saved. "
                           "Start Ollama, then press Look again.")


@dataclass(frozen=True)
class InstalledModels:
    """What Ollama reports, as the roles grid needs it. Built by
    `app.chat.llm.probe_installed`; empty and `reachable=False` when Ollama is not
    answering, which is a normal state and never an exception."""

    reachable: bool = False
    #: Every model that can answer a prompt (embedding models are left out).
    names: tuple[str, ...] = ()
    #: The ones among them that can read pictures.
    vision: tuple[str, ...] = ()
    #: File size in bytes by installed name.
    sizes: Mapping[str, int] = field(default_factory=dict)
    #: This computer's memory in MB, or 0 when it could not be read.
    ram_mb: int = 0


def is_vision_name(name: str) -> bool:
    lowered = (name or "").lower()
    return any(hint in lowered for hint in VISION_NAME_HINTS)


def size_of(name: str, sizes: Mapping[str, int]) -> int:
    """File size in bytes of `name`, tolerating `mistral` for `mistral:latest`; 0 if unknown."""
    hit = _installed_match(name, list(sizes))
    return int(sizes.get(hit, 0)) if hit else 0


def _gb(n_bytes: float) -> str:
    gb = n_bytes / 1024 ** 3
    return f"{gb:.1f} GB" if gb < 10 else f"{gb:.0f} GB"


def ram_line(models: Sequence[str], sizes: Mapping[str, int], ram_mb: int = 0) -> str:
    """One plain sentence on what these models need in memory if they are all kept
    ready at once - the order's "these two together need about 11 GB - you have 32".

    `""` when nothing is known (no model chosen, or none of them has a known size):
    a sentence with a made-up number is worse than none. Names with no known size are
    left out of the sum and the sentence says so.
    """
    seen: list[str] = []
    for name in models:
        hit = _installed_match(name, list(sizes)) or name
        if name and hit not in seen:
            seen.append(hit)
    known = [(n, size_of(n, sizes)) for n in seen if size_of(n, sizes) > 0]
    if not known:
        return ""
    total = sum(size for _n, size in known) * MEMORY_OVERHEAD
    have = f" This computer has {ram_mb / 1024:.0f} GB." if ram_mb else ""
    if len(known) == 1:
        text = f"{known[0][0]} needs about {_gb(total)} of memory while it is ready.{have}"
    else:
        names = ", ".join(n for n, _s in known[:-1]) + f" and {known[-1][0]}"
        text = (f"{names} together need about {_gb(total)} of memory if all are kept ready "
                f"at once.{have}")
    if ram_mb and total > ram_mb * 1024 ** 2 * 0.6:
        text += (" That is more than is comfortable here: Ollama will swap them in and out, "
                 "and every swap adds seconds to an answer. Using one model for every job "
                 "avoids it.")
    left_out = [n for n in seen if size_of(n, sizes) <= 0]
    if left_out:
        text += f" (No size is known for {', '.join(left_out)}, so it is not counted.)"
    return text


# ---------------------------------------------------------------------------
# Choosing the answering model at chat or search time (2026-10-04)
# ---------------------------------------------------------------------------
# The owner: "if multiple models are available they should be listed so they can
# be changed at chat or search time". One list for the Chat tab's drop-down and for
# Interpret's on the Search page: every model that can write an answer - the ones
# inside Leasha (ONNX) that are downloaded, and Ollama's installed text models.

ONNX_PREFIX = "onnx:"
OLLAMA_PREFIX = "ollama:"


@dataclass(frozen=True)
class ModelOption:
    """One model a person can pick. `value` names the runner and the model."""

    value: str
    label: str
    size_bytes: int = 0

    @property
    def engine(self) -> str:
        return parse_option(self.value)[0]

    @property
    def name(self) -> str:
        return parse_option(self.value)[1]


def option_value(engine: str, name: str) -> str:
    """`"onnx:<catalogue key>"` or `"ollama:<installed name>"`; "" with no name."""
    name = str(name or "").strip()
    if not name:
        return ""
    return (ONNX_PREFIX if str(engine).strip().lower() == "onnx" else OLLAMA_PREFIX) + name


def parse_option(value: str) -> tuple[str, str]:
    """`(engine, name)` from an option's value; `("", "")` for anything else."""
    text = str(value or "").strip()
    for prefix in (ONNX_PREFIX, OLLAMA_PREFIX):
        if text.startswith(prefix) and len(text) > len(prefix):
            return prefix[:-1], text[len(prefix):]
    return "", ""


def answer_options(onnx: Sequence[tuple[str, str, int]],
                   installed: InstalledModels) -> list[ModelOption]:
    """Every model that can answer, inside Leasha first, then Ollama's smallest first.

    `onnx` is `(catalogue key, label, approx MB)` for each downloaded chat copy.
    Ollama's picture readers are left out by *name* (`llava` and its kind are
    Describe's), not by Ollama's `vision` capability: a model that also reads
    pictures - `gemma4` - is still a chat model. Models that only make vectors are
    left out too (`probe_installed` drops those already)."""
    out = [ModelOption(option_value("onnx", key), f"{label} · in Leasha · {_gb(mb * 1024 ** 2)}",
                       int(mb) * 1024 ** 2)
           for key, label, mb in onnx if key]
    for choice in rank(installed.names):
        if not choice.selectable or is_vision_name(choice.name):
            continue
        size = size_of(choice.name, installed.sizes)
        where = f"Ollama · {_gb(size)}" if size else "Ollama"
        out.append(ModelOption(option_value("ollama", choice.name), f"{choice.name} · {where}",
                               size))
    return out


def default_option(options: Sequence[ModelOption], engine: str, onnx_key: str = "",
                   ollama_name: str = "") -> str:
    """The option Settings would use now, so the drop-down starts on it: the ONNX copy
    that serves, or the configured Ollama model (`mistral` finds `mistral:latest`).
    "" when it is not among the options."""
    if str(engine).strip().lower() == "onnx":
        wanted = option_value("onnx", onnx_key)
        return wanted if any(o.value == wanted for o in options) else ""
    names = [o.name for o in options if o.engine == "ollama"]
    hit = _installed_match(ollama_name, names)
    return option_value("ollama", hit) if hit else ""


#: Share of the memory free right now that a model loaded ahead of time may take.
#: The same 60% `ram_line` calls comfortable, against what is free rather than
#: what is fitted, because the point is not to push the index run into swap.
PRELOAD_SHARE = 0.6


def fits_in_memory(size_bytes: int, free_mb: int) -> bool:
    """Whether a model of `size_bytes` may be loaded before anybody asks for it.
    Unknown size or unknown free memory: no - loading ahead is a courtesy, and a
    courtesy that pushes the machine into swap is not one."""
    if size_bytes <= 0 or free_mb <= 0:
        return False
    return size_bytes * MEMORY_OVERHEAD <= free_mb * 1024 ** 2 * PRELOAD_SHARE
