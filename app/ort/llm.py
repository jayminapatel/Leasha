r"""The chat model on ONNX Runtime: the same interface Chat and Interpret use for Ollama.

Layer: L2.

`app/chat/llm.py`'s `LLM` protocol is what the chat engine, the router, the
planner and Interpret's `QueryTranslator` call - `generate`, `stream`,
`chat_stream`, `health`, `has_model`, `context_window`, plus `available_models`,
`set_model` and `warm` that the Settings boxes use. `OnnxLLM` answers all of
them from a model running inside Leasha, so nothing that calls a model has to
know which engine it got. `CHAT_ENGINE` chooses (owner, 2026-09-29: "for chat it
should be configurable to use ollama or onnx"); ONNX is the default.

**The model** is `onnx-community/Qwen2.5-1.5B-Instruct`, int8 - the same model
Interpret already used through Ollama (`qwen2.5:1.5b`), Apache-2.0. It is a
decoder-only export: `input_ids`, `attention_mask`, `position_ids` and
`past_key_values.N.{key,value}` in, `logits` and `present.N.*` out
(`app/ort/generate.py` reads the names from the session). Qwen 2.5 uses
grouped-query attention, so the cache has `num_key_value_heads` heads, not
`num_attention_heads` - read from `config.json`.

**Prompts** are Qwen's ChatML (`<|im_start|>role\n...<|im_end|>\n`), written
here rather than rendered from the tokenizer's Jinja template: it is five lines,
and a template engine is not a dependency Leasha has. `generate(prompt)` treats
the prompt as one user message, which is what Ollama's `/api/generate` does
with a model's template.

**What Ollama did that this does differently.** `json_mode` cannot constrain
the output here, so it asks for JSON in the system message and hands back the
first JSON object in the reply. `think` is ignored - Qwen 2.5 has no thinking
mode (`can_think()` is False, as it was through Ollama). One reply is generated
at a time; a second caller waits for the first.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, Mapping, Optional, Sequence

import numpy as np

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger
from app.ort import hub
from app.ort.generate import Decoder, generate, sample
from app.ort.session import interactive_threads, load_session

__all__ = ["OnnxLLM", "ENGINE", "chatml", "first_json"]

_log = logger.bind(component="ort.llm")

#: What `CHAT_ENGINE` says for this engine, and what `engine` reports.
ENGINE = "onnx"

DEFAULT_SYSTEM = "You are a helpful assistant."
JSON_SYSTEM = ("You are a helpful assistant. Reply with one JSON object and nothing else - "
               "no explanation, no code fence.")

#: The context the chat engine is told it has. The model allows far more, but
#: every token of prompt costs time on a processor; this is the number Ollama
#: was asked for (`OLLAMA_DEFAULT_CONTEXT`), so budgets stay as they were.
CONTEXT_TOKENS = 4096


def chatml(messages: Sequence[Mapping[str, str]], *, system: Optional[str] = None) -> str:
    """Qwen's chat format, ending with an open assistant turn."""
    parts: list[str] = []
    rows = list(messages)
    if not rows or rows[0].get("role") != "system":
        parts.append(f"<|im_start|>system\n{system or DEFAULT_SYSTEM}<|im_end|>\n")
    for row in rows:
        role = str(row.get("role") or "user")
        parts.append(f"<|im_start|>{role}\n{row.get('content') or ''}<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
    return "".join(parts)


def _system_and_rest(messages: Sequence[Mapping[str, str]],
                     system: Optional[str]) -> tuple[str, list[Mapping[str, str]]]:
    rows = list(messages)
    if rows and rows[0].get("role") == "system":
        return str(rows[0].get("content") or ""), rows[1:]
    return system or DEFAULT_SYSTEM, rows


def llama3(messages: Sequence[Mapping[str, str]], *, system: Optional[str] = None) -> str:
    """Llama 3's format (`<|start_header_id|>role<|end_header_id|>`)."""
    sys_text, rows = _system_and_rest(messages, system)
    parts = ["<|begin_of_text|>",
             f"<|start_header_id|>system<|end_header_id|>\n\n{sys_text}<|eot_id|>"]
    for row in rows:
        parts.append(f"<|start_header_id|>{row.get('role') or 'user'}<|end_header_id|>\n\n"
                     f"{row.get('content') or ''}<|eot_id|>")
    parts.append("<|start_header_id|>assistant<|end_header_id|>\n\n")
    return "".join(parts)


def gemma(messages: Sequence[Mapping[str, str]], *, system: Optional[str] = None) -> str:
    """Gemma's format. It has no system turn: the system text leads the first user turn,
    as Gemma's own template does; the assistant is called `model`."""
    sys_text, rows = _system_and_rest(messages, system)
    parts, lead = ["<bos>"], sys_text
    for row in rows:
        role = "model" if row.get("role") == "assistant" else "user"
        content = str(row.get("content") or "")
        if lead and role == "user":
            content, lead = f"{lead}\n\n{content}", ""
        parts.append(f"<start_of_turn>{role}\n{content}<end_of_turn>\n")
    parts.append("<start_of_turn>model\n")
    return "".join(parts)


def phi3(messages: Sequence[Mapping[str, str]], *, system: Optional[str] = None) -> str:
    """Phi-3's format (`<|user|>`, `<|assistant|>`, `<|end|>`)."""
    sys_text, rows = _system_and_rest(messages, system)
    parts = [f"<|system|>\n{sys_text}<|end|>\n"]
    for row in rows:
        parts.append(f"<|{row.get('role') or 'user'}|>\n{row.get('content') or ''}<|end|>\n")
    parts.append("<|assistant|>\n")
    return "".join(parts)


#: Prompt format name -> builder, and the tokens that end a turn in it. A chat
#: model's format is in its catalogue entry (read from the model's own chat
#: template when the list is updated from Hugging Face, 2026-09-30).
FORMATS = {"chatml": chatml, "llama3": llama3, "gemma": gemma, "phi3": phi3}
END_TOKENS = {"chatml": ("<|im_end|>", "<|endoftext|>"),
              "llama3": ("<|eot_id|>", "<|end_of_text|>"),
              "gemma": ("<end_of_turn>", "<eos>"),
              "phi3": ("<|end|>", "<|endoftext|>")}


def format_prompt(fmt: str, messages: Sequence[Mapping[str, str]], *,
                  system: Optional[str] = None) -> str:
    return FORMATS.get(fmt or "chatml", chatml)(messages, system=system)


def first_json(text: str) -> str:
    """The first balanced `{...}` in `text`, or `text` unchanged. What `json_mode`
    promised callers through Ollama: a string that parses."""
    start = text.find("{")
    while start != -1:
        depth, in_string, escaped = 0, False, False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
            elif char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start:index + 1]
                    try:
                        json.loads(candidate)
                        return candidate
                    except ValueError:
                        break
        start = text.find("{", start + 1)
    return text


class _Loaded:
    """One model's session, tokenizer and end tokens."""

    def __init__(self, folder: Path, model: hub.OnnxModel, device: str,
                 prompt_format: str = "chatml") -> None:
        from tokenizers import Tokenizer

        self.prompt_format = prompt_format if prompt_format in FORMATS else "chatml"

        config = json.loads((folder / "config.json").read_text(encoding="utf-8"))
        try:
            generation = json.loads((folder / "generation_config.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            generation = {}
        heads = int(config.get("num_attention_heads", 12))
        kv_heads = int(config.get("num_key_value_heads", heads))
        head_dim = int(config.get("head_dim") or int(config.get("hidden_size", 1536)) // heads)
        # `basic`, not full optimisation: see `load_session` - at full
        # optimisation the cached step picked different words (2026-09-30).
        loaded = load_session(folder / model.graph_file(model.graphs[0]), what="chat model",
                              device=device, optimise="basic", threads=interactive_threads())
        self.decoder = Decoder(loaded.session, heads=kv_heads, head_dim=head_dim)
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        eos = generation.get("eos_token_id", config.get("eos_token_id", []))
        self.eos = [int(e) for e in (eos if isinstance(eos, list) else [eos])]
        for token in END_TOKENS[self.prompt_format]:
            found = self.tokenizer.token_to_id(token)
            if found is not None and found not in self.eos:
                self.eos.append(found)
        self.max_positions = int(config.get("max_position_embeddings", 32768))
        self.on_gpu = loaded.on_gpu


class OnnxLLM:
    """A model inside Leasha, speaking `app/chat/llm.py`'s `LLM` protocol."""

    engine = ENGINE
    #: Read by code written for `OllamaClient` (`vision_caption.unavailable_reason`).
    url = "(inside Leasha)"

    def __init__(self, cache_dir: Optional[Path], model: str = "", *, device: str = "auto",
                 timeout: float = 120.0) -> None:
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.device = device
        self.timeout = float(timeout)
        self._model = (hub.by_key(model) or hub.QWEN_1_5B).key
        #: The model a caller actually named - empty when it named none, so the
        #: catalogue's ranking decides (`_copies`). `_model` cannot say this: it
        #: falls back to the int8 key, which is not the preferred copy.
        self._asked = self._model if model and hub.by_key(model) is not None else ""
        self._loaded: Optional[_Loaded] = None
        self._loaded_key = ""
        self._load_error: Optional[BaseException] = None
        self._lock = threading.Lock()          # loading
        self._busy = threading.Lock()          # one reply at a time

    # -- which model -------------------------------------------------------------

    @property
    def model(self) -> str:
        return self._model

    def set_model(self, name: str) -> None:
        chosen = hub.by_key(name)
        if chosen is not None and chosen.key != self._model:
            self._model = chosen.key
            self._asked = chosen.key
            self._load_error = None

    def _spec(self) -> hub.OnnxModel:
        return hub.by_key(self._model) or hub.QWEN_1_5B

    def available_models(self) -> list[str]:
        """Chat models downloaded into the cache - what a drop-down may offer."""
        return [m.key for m in (hub.QWEN_1_5B,) if hub.present(m, self.cache_dir)]

    def has_model(self) -> bool:
        return hub.resolve_any(self._copies(), self.cache_dir) is not None

    def health(self, *, force: bool = False) -> bool:
        """Up means "can answer": the model is downloaded and did not fail to load."""
        return self.has_model() and self._load_error is None

    def can_think(self) -> bool:
        return False

    def context_window(self) -> int:
        return CONTEXT_TOKENS

    def missing_error(self) -> AppErrorException:
        spec = self._spec()
        return AppErrorException(make_error(
            "ERR_LOCAL_MODEL_MISSING", "ort.llm", model=spec.label,
            details=(f"{spec.label} is not downloaded - Settings, Models, Chat, Download "
                     f"(about {spec.approx_mb} MB)."),
        ))

    def _copies(self) -> tuple[hub.OnnxModel, ...]:
        """The chat copies to try, best first: the catalogue's chat entries (verified
        first, by rank - the 4-bit Qwen leads, `catalogue.json`), falling back to the
        built-in definitions when the catalogue cannot be read."""
        try:
            from app.ort import catalogue

            cat = catalogue.load()
            rows = cat.for_job("chat")
            ordered = [e for e in rows if e.is_verified] + [e for e in rows if not e.is_verified]
            picked = catalogue.chosen("chat")          # "Use this" (2026-09-30) goes first
            # 2026-09-30: a catalogue key handed to the constructor goes before
            # even that. It was accepted and never read, so asking for Gemma
            # loaded Qwen (found checking order 1c item 8). Anything that is not
            # a catalogue key - an Ollama-style name - is ignored, as before.
            if self._asked and cat.by_key(self._asked) is not None:
                picked = self._asked
            if picked and cat.by_key(picked) is not None:
                ordered = [cat.by_key(picked)] + [e for e in ordered if e.key != picked]
            if ordered:
                return tuple(e.model() for e in ordered)
        except Exception:                               # noqa: BLE001 - built-ins below
            pass
        return (hub.QWEN_1_5B_Q4, hub.QWEN_1_5B)

    @staticmethod
    def _format_of(key: str) -> str:
        """The chat format the catalogue records for this copy (ChatML if none)."""
        try:
            from app.ort import catalogue

            entry = catalogue.load().by_key(key)
            return (entry.prompt_format if entry is not None and entry.prompt_format
                    else "chatml")
        except Exception:                               # noqa: BLE001
            return "chatml"

    def _prompt(self, messages: Sequence[Mapping[str, str]], *,
                system: Optional[str] = None) -> str:
        return format_prompt(self._ensure().prompt_format, messages, system=system)

    def _ensure(self) -> _Loaded:
        with self._lock:
            if self._loaded is not None and self._loaded_key == self._model:
                return self._loaded
            found = hub.resolve_any(self._copies(), self.cache_dir)
            if found is None:
                raise self.missing_error()
            spec, folder = found
            try:
                started = time.monotonic()
                self._loaded = _Loaded(folder, spec, self.device, self._format_of(spec.key))
                self._loaded_key = self._model      # the choice, whichever copy served it
                self._load_error = None
                _log.info("{} loaded in {:.1f}s on the {}", spec.key, time.monotonic() - started,
                          "graphics card" if self._loaded.on_gpu else "processor")
                return self._loaded
            except AppErrorException:
                raise
            except Exception as exc:                     # noqa: BLE001 - said plainly below
                self._load_error = exc
                raise AppErrorException(make_error(
                    "ERR_LOCAL_MODEL_FAILED", "ort.llm",
                    details=f"the chat model could not be loaded: {type(exc).__name__}: {exc}",
                )) from exc

    def warm(self, **_kwargs: Any) -> bool:
        try:
            self._ensure()
            return True
        except AppErrorException:
            return False

    # -- generating ------------------------------------------------------------------

    def _tokens(self, prompt_text: str, *, temperature: float, max_tokens: Optional[int],
                timeout: Optional[float], should_stop: Optional[Callable[[], bool]],
                state: Optional[dict] = None) -> Iterator[str]:
        """Decoded text pieces, as they arrive. `state["timed_out"]` is set when
        the deadline, not the model, ended the reply."""
        loaded = self._ensure()
        ids = loaded.tokenizer.encode(prompt_text, add_special_tokens=False).ids
        budget = CONTEXT_TOKENS - len(ids)
        limit = max(1, min(int(max_tokens or 512), budget if budget > 0 else 1))
        deadline = time.monotonic() + float(timeout or self.timeout)

        def stopping() -> bool:
            if time.monotonic() > deadline:
                if state is not None:
                    state["timed_out"] = True
                return True
            return bool(should_stop and should_stop())

        with self._busy:
            # **The start of the last prompt is not read twice** (2026-09-30).
            # Interpret, the router and the planner send the same long
            # instructions every time with a new sentence at the end; reading
            # them was most of Interpret's 17-24 s on the owner's laptop. As
            # Ollama does, the cache after the last prompt is kept, cut back to
            # the part this prompt shares with it, and only the rest is read.
            # Exact for the 4-bit model: its cached and recomputed answers agree
            # token for token (the int8 copy's did not - see `hub.QWEN_1_5B_Q4`).
            reuse = self._shared_prefix(loaded, ids)
            loaded.decoder.reset()
            if reuse:
                loaded.decoder.past = {name: value[:, :, :reuse, :]
                                       for name, value in self._prefix_past.items()}
            captured = {"done": False}

            def step(new_ids: list[int], position: int) -> np.ndarray:
                start = position + reuse
                total = start + len(new_ids)
                logits = loaded.decoder.step({
                    "input_ids": np.array([new_ids], dtype=np.int64),
                    "attention_mask": np.ones((1, total), dtype=np.int64),
                    "position_ids": np.arange(start, total, dtype=np.int64)[None, :],
                })
                if not captured["done"]:
                    # The cache right after the whole prompt, before any reply.
                    captured["done"] = True
                    self._prefix_ids = list(ids)
                    self._prefix_past = dict(loaded.decoder.past)
                return logits

            produced: list[int] = []
            shown = ""
            for token in generate(step, prompt=ids[reuse:], max_new_tokens=limit, eos=loaded.eos,
                                  pick=sample(temperature), should_stop=stopping):
                produced.append(token)
                text = loaded.tokenizer.decode(produced, skip_special_tokens=True)
                if text.endswith("�"):        # half of a multi-byte character
                    continue
                if len(text) > len(shown):
                    piece, shown = text[len(shown):], text
                    yield piece

    def _shared_prefix(self, loaded: Any, ids: list[int]) -> int:
        """How many leading tokens of `ids` the kept cache already holds. At least
        one token is always left to read, so the model has something to answer."""
        previous = getattr(self, "_prefix_ids", None)
        if not previous or getattr(self, "_prefix_owner", None) is not loaded:
            self._prefix_owner = loaded
            self._prefix_ids, self._prefix_past = [], {}
            return 0
        shared = 0
        for a, b in zip(previous, ids):
            if a != b:
                break
            shared += 1
        return min(shared, len(ids) - 1)

    @staticmethod
    def _until_stop(pieces: Iterator[str], stop: Optional[list[str]]) -> Iterator[str]:
        """Stop at the first stop string, which is not yielded - Ollama's `stop`."""
        if not stop:
            yield from pieces
            return
        held = ""
        longest = max(len(s) for s in stop)
        for piece in pieces:
            held += piece
            cut = min((held.find(s) for s in stop if s in held), default=-1)
            if cut >= 0:
                if held[:cut]:
                    yield held[:cut]
                return
            if len(held) > longest:
                yield held[:-longest]
                held = held[-longest:]
        if held:
            yield held

    def stream(self, prompt: str, *, temperature: float = 0.0, timeout: Optional[float] = None,
               max_tokens: Optional[int] = None, stop: Optional[list[str]] = None,
               should_stop: Optional[Callable[[], bool]] = None) -> Iterator[str]:
        text = self._prompt([{"role": "user", "content": prompt}])
        yield from self._until_stop(self._tokens(text, temperature=temperature,
                                                 max_tokens=max_tokens, timeout=timeout,
                                                 should_stop=should_stop), stop)

    def chat_stream(self, messages: Sequence[Mapping[str, str]], *, temperature: float = 0.4,
                    timeout: Optional[float] = None, max_tokens: Optional[int] = None,
                    stop: Optional[list[str]] = None,
                    should_stop: Optional[Callable[[], bool]] = None,
                    think: Optional[str] = None) -> Iterator[str]:
        text = self._prompt(messages)
        yield from self._until_stop(self._tokens(text, temperature=temperature,
                                                 max_tokens=max_tokens, timeout=timeout,
                                                 should_stop=should_stop), stop)

    def generate(self, prompt: str, *, json_mode: bool = False, temperature: float = 0.0,
                 timeout: Optional[float] = None, max_tokens: Optional[int] = None,
                 stop: Optional[list[str]] = None, images: Optional[list[str]] = None) -> Any:
        """One whole reply, `Completion`-shaped (`.text`, `.model`, `.elapsed_s`).

        Raises `ERR_LOCAL_MODEL_TIMEOUT` when nothing came back in time - the
        callers that branch on `ERR_OLLAMA_TIMEOUT` treat it the same.
        """
        from app.chat.llm import Completion

        if images:
            raise AppErrorException(make_error(
                "ERR_LOCAL_MODEL_FAILED", "ort.llm",
                details="the chat model cannot see pictures; Describe uses the photo model"))
        started = time.monotonic()
        limit = float(timeout or self.timeout)
        text = self._prompt([{"role": "user", "content": prompt}],
                      system=JSON_SYSTEM if json_mode else None)
        # **The reply is started for it.** Ollama's `format: json` constrains
        # decoding with a grammar; this cannot, so the answer begins with "{"
        # already written - measured 2026-09-30, the instruction alone got
        # "You are a helpful assistant. Reply with JSON." back instead of JSON.
        lead = "{" if json_mode else ""
        text += lead
        state: dict = {}
        reply = "".join(self._until_stop(self._tokens(text, temperature=temperature,
                                                      max_tokens=max_tokens, timeout=limit,
                                                      should_stop=None, state=state), stop))
        elapsed = time.monotonic() - started
        # A reply the clock cut short is not an answer: half a query in
        # Interpret's box is worse than none. Ollama's client raises here too.
        if state.get("timed_out"):
            raise AppErrorException(make_error(
                "ERR_LOCAL_MODEL_TIMEOUT", "ort.llm", timeout_s=round(limit),
                details=f"no reply within {limit:.0f}s"))
        reply = lead + reply
        if json_mode:
            reply = first_json(reply)
        return Completion(text=reply.strip(), model=self._model, elapsed_s=elapsed)

    def chat(self, messages: Sequence[Mapping[str, str]], **kwargs: Any) -> Any:
        from app.chat.llm import Completion

        started = time.monotonic()
        text = "".join(self.chat_stream(messages, **kwargs))
        return Completion(text=text.strip(), model=self._model,
                          elapsed_s=time.monotonic() - started)
