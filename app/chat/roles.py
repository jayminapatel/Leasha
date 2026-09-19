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
from typing import Optional, Sequence

from app.llm.models import parameter_billions, rank

__all__ = ["RoleModels", "resolve_roles", "suggest_modes", "SMALL_MODEL_B", "AFFORDABLE_MAX_B"]

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
