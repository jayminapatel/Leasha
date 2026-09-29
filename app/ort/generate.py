r"""Token-by-token generation with a key/value cache, on plain ONNX Runtime.

Layer: L2.

The three generating models here (Florence-2's decoder, Whisper's decoder, the
chat model) are exported by the same tool with the same naming, which is what
this file relies on - and **reads from the session rather than assuming**:

* the cache goes in as `past_key_values.<layer>[.decoder|.encoder].<key|value>`
  and comes out as `present.<layer>[...]` with the same tail;
* a *merged* decoder also takes `use_cache_branch` - false on the first step,
  when the cache is empty, true after;
* in an encoder-decoder model the `.encoder.` part of the cache is computed on
  the first step only (it depends on the encoder's output, which does not
  change) and is fed back unchanged afterwards.

An empty cache is a tensor with a zero-length sequence axis. Its other sizes
come from the session's declared input shapes, and where those are symbolic,
from the model's `config.json` (`heads`, `head_dim`) - which is why `CacheSpec`
is built with both.

What a step feeds besides the cache (token ids or embeddings, masks, positions,
the encoder's output) is the caller's: `Decoder.step(inputs, ...)`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Optional, Sequence

import numpy as np

__all__ = ["CacheSpec", "Decoder", "LogitsRule", "no_repeat_ngram", "suppress", "force_first",
           "greedy", "sample", "generate"]

_PAST = re.compile(r"^past_key_values\.(\d+)\.(?:(decoder|encoder)\.)?(key|value)$")

_ORT_TYPES = {
    "tensor(float)": np.float32,
    "tensor(float16)": np.float16,
    "tensor(double)": np.float64,
}


@dataclass
class CacheSpec:
    """Which cache inputs a decoder session has, and how to make them empty."""

    names: list[str]
    dtypes: dict[str, Any]
    shapes: dict[str, list]
    heads: int
    head_dim: int
    merged: bool                       # takes `use_cache_branch`
    encoder_names: list[str] = field(default_factory=list)

    @classmethod
    def from_session(cls, session: Any, *, heads: int, head_dim: int) -> "CacheSpec":
        names, dtypes, shapes, encoder = [], {}, {}, []
        merged = False
        for arg in session.get_inputs():
            if arg.name == "use_cache_branch":
                merged = True
                continue
            match = _PAST.match(arg.name)
            if not match:
                continue
            names.append(arg.name)
            dtypes[arg.name] = _ORT_TYPES.get(arg.type, np.float32)
            shapes[arg.name] = list(arg.shape)
            if match.group(2) == "encoder":
                encoder.append(arg.name)
        return cls(names=names, dtypes=dtypes, shapes=shapes, heads=heads, head_dim=head_dim,
                   merged=merged, encoder_names=encoder)

    def empty(self, batch: int = 1) -> dict[str, np.ndarray]:
        """A zero-length cache: `[batch, heads, 0, head_dim]` per entry."""
        out: dict[str, np.ndarray] = {}
        for name in self.names:
            declared = self.shapes.get(name) or [None, None, None, None]
            dims = []
            for index, dim in enumerate(declared):
                if index == 0:
                    dims.append(batch)
                elif index == 2:
                    dims.append(0)
                elif isinstance(dim, int) and dim > 0:
                    dims.append(dim)
                else:
                    dims.append(self.heads if index == 1 else self.head_dim)
            out[name] = np.zeros(dims, dtype=self.dtypes[name])
        return out


def _past_name(present: str) -> str:
    return "past_key_values" + present[len("present"):]


class Decoder:
    """One decoder session and the cache it carries between steps."""

    def __init__(self, session: Any, *, heads: int, head_dim: int) -> None:
        self.session = session
        self.spec = CacheSpec.from_session(session, heads=heads, head_dim=head_dim)
        self._outputs = [o.name for o in session.get_outputs()]
        self._inputs = {i.name for i in session.get_inputs()}
        self.past: dict[str, np.ndarray] = {}
        self.steps = 0

    def reset(self) -> None:
        self.past = {}
        self.steps = 0

    def accepts(self, name: str) -> bool:
        return name in self._inputs

    def step(self, inputs: dict[str, np.ndarray]) -> np.ndarray:
        """Run one step; returns the logits for the **last** position, `[vocab]`."""
        first = not self.past
        feeds = dict(inputs)
        feeds.update(self.spec.empty() if first else self.past)
        if self.spec.merged:
            feeds["use_cache_branch"] = np.array([not first], dtype=bool)
        results = self.session.run(self._outputs, {k: v for k, v in feeds.items()
                                                    if k in self._inputs})
        named = dict(zip(self._outputs, results))
        fresh: dict[str, np.ndarray] = {}
        for name, value in named.items():
            if not name.startswith("present"):
                continue
            past = _past_name(name)
            if past in self.spec.encoder_names and not first:
                # Computed once, on the first step; later steps may return an
                # empty or unchanged tensor here - the first one is the real one.
                fresh[past] = self.past[past]
            else:
                fresh[past] = value
        self.past = fresh
        self.steps += 1
        logits = named.get("logits")
        if logits is None:
            logits = results[0]
        return np.asarray(logits)[0, -1, :]


#: A rule that edits the next token's logits in place: `(generated, logits)`.
LogitsRule = Callable[[Sequence[int], np.ndarray], None]


def no_repeat_ngram(size: int) -> LogitsRule:
    """Forbid any `size`-gram from appearing twice (Florence-2's default of 3)."""
    def rule(generated: Sequence[int], logits: np.ndarray) -> None:
        if size <= 0 or len(generated) < size - 1:
            return
        prefix = tuple(generated[-(size - 1):]) if size > 1 else ()
        for start in range(len(generated) - size + 1):
            if tuple(generated[start:start + size - 1]) == prefix:
                logits[generated[start + size - 1]] = -np.inf
    return rule


def suppress(token_ids: Iterable[int]) -> LogitsRule:
    ids = [int(t) for t in token_ids]

    def rule(_generated: Sequence[int], logits: np.ndarray) -> None:
        valid = [t for t in ids if 0 <= t < logits.shape[0]]
        logits[valid] = -np.inf
    return rule


def force_first(token_id: int) -> LogitsRule:
    """The first generated token is `token_id` (Florence-2's `forced_bos_token_id`)."""
    def rule(generated: Sequence[int], logits: np.ndarray) -> None:
        if not generated:
            keep = logits[token_id]
            logits[:] = -np.inf
            logits[token_id] = keep if np.isfinite(keep) else 0.0
    return rule


def greedy(logits: np.ndarray) -> int:
    return int(np.argmax(logits))


def sample(temperature: float, top_p: float = 0.9,
           rng: Optional[np.random.Generator] = None) -> Callable[[np.ndarray], int]:
    """Temperature and nucleus sampling; `temperature <= 0` is greedy."""
    generator = rng or np.random.default_rng()

    def pick(logits: np.ndarray) -> int:
        if temperature <= 0:
            return greedy(logits)
        scaled = logits.astype(np.float64) / temperature
        scaled -= np.max(scaled[np.isfinite(scaled)])
        probs = np.where(np.isfinite(scaled), np.exp(scaled), 0.0)
        order = np.argsort(-probs)
        cumulative = np.cumsum(probs[order]) / probs.sum()
        keep = order[: int(np.searchsorted(cumulative, top_p) + 1)]
        chosen = probs[keep] / probs[keep].sum()
        return int(generator.choice(keep, p=chosen))
    return pick


def generate(step: Callable[[list[int], int], np.ndarray], *, prompt: Sequence[int],
             max_new_tokens: int, eos: Iterable[int], rules: Sequence[LogitsRule] = (),
             pick: Callable[[np.ndarray], int] = greedy,
             should_stop: Optional[Callable[[], bool]] = None) -> Iterator[int]:
    """Yield generated token ids, one at a time, until an end token or the limit.

    `step(new_ids, position)` runs the model on the tokens not yet seen - the
    whole `prompt` first, then one token at a time - and returns the last
    position's logits. `position` is how many tokens the model had already
    been given. End tokens are not yielded.
    """
    ends = {int(t) for t in eos}
    generated: list[int] = []
    pending = list(prompt)
    seen = 0
    for _ in range(max(0, int(max_new_tokens))):
        if should_stop is not None and should_stop():
            return
        logits = np.array(step(pending, seen), dtype=np.float32, copy=True)
        seen += len(pending)
        for rule in rules:
            rule(generated, logits)
        token = pick(logits)
        if token in ends:
            return
        generated.append(token)
        yield token
        pending = [token]
