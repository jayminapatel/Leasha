r"""A stand-in for `app.ort.llm.OnnxLLM`, loaded by the model host in tests.

The host is named a factory (`ModelHost(factory="tests.unit._fake_llm:FakeLLM")`)
and builds this instead of the real model, so the real pipe, the real child
process and the real stop and death paths are tested in seconds without loading
a gigabyte. The prompts below are the test's switches:

    "spin"  holds Python's lock for one second, the way a model load does
    "boom"  fails with a chat-model error
    "die"   ends the whole process after its first piece, the way a fault in
            the model library does

2026-10-10: `FakeReranker` too, for the search host's reranker.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Optional

from app.chat.llm import Completion
from app.core.errors import AppErrorException, make_error


class FakeLLM:
    engine = "onnx"

    def __init__(self, cache_dir: Optional[Path], model: str = "", *, device: str = "auto",
                 timeout: float = 120.0) -> None:
        self.cache_dir = cache_dir
        self.model = model or "fake"
        self.keep_resident = False
        self.pid = os.getpid()

    def set_model(self, name: str) -> None:
        self.model = name

    def warm(self, **_kwargs: Any) -> bool:
        return True

    def unload(self) -> bool:
        return True

    def _spin(self) -> None:
        end = time.perf_counter() + 1.0
        while time.perf_counter() < end:         # pure Python: holds the lock
            pass

    def generate(self, prompt: str, *, should_stop: Any = None, **_kwargs: Any) -> Any:
        if prompt == "spin":
            self._spin()
        if prompt == "boom":
            raise AppErrorException(make_error("ERR_LOCAL_MODEL_TIMEOUT", "tests", timeout_s=1))
        return Completion(text=f"echo:{prompt}:{self.model}:{self.keep_resident}",
                          model=self.model, elapsed_s=0.0)

    def chat(self, messages: Any, **kwargs: Any) -> Any:
        return self.generate(messages[-1]["content"], **kwargs)

    def stream(self, prompt: str, *, should_stop: Any = None, **_kwargs: Any) -> Any:
        for index, word in enumerate(prompt.split()):
            if should_stop is not None and should_stop():
                return
            if prompt.startswith("die") and index == 1:
                os._exit(3)
            time.sleep(0.01)
            yield word

    def chat_stream(self, messages: Any, **kwargs: Any) -> Any:
        yield from self.stream(messages[-1]["content"], **kwargs)


class FakeEmbedder:
    """Stands in for `app.ort.hosted.HostedEmbedder`."""

    HOSTED_METHODS = frozenset({"embed", "warm_up"})

    def __init__(self, kind: str = "clip-text", env_file: str = "",
                 spec: Optional[dict] = None) -> None:
        # `spec` (2026-10-10): the meaning model's constructor arguments, sent by
        # `RemoteEmbedder.for_meaning`; kept so a test can read what arrived.
        self.progress_sink: Any = None
        self.kind = kind
        self.spec = dict(spec or {})

    def embed(self, texts: Any) -> Any:
        import numpy as np

        if self.progress_sink is not None:
            self.progress_sink(10.0)
            self.progress_sink(100.0)
        if list(texts) == ["spin"]:
            end = time.perf_counter() + 1.0
            while time.perf_counter() < end:     # pure Python: holds the lock
                pass
        return np.array([[float(len(text)), 1.0] for text in texts])

    def warm_up(self) -> None:
        if self.progress_sink is not None:
            self.progress_sink(50.0)
        return None


class FakeReranker:
    """Stands in for `app.ort.hosted.HostedReranker` (2026-10-10).

    Scores a passage by its length, so the longest comes first. The model
    name "absent" fails to load, the way a model that is not downloaded does;
    the query "die" ends the process, the way a fault in the model library does.
    """

    HOSTED_METHODS = frozenset({"load", "score"})

    def __init__(self, model_name: str = "", cache_dir: Any = None, device: str = "auto") -> None:
        self.progress_sink: Any = None
        self.model_name = model_name

    def load(self) -> bool:
        if self.model_name == "absent":
            raise RuntimeError("ValueError: the reranker model is not downloaded")
        return True

    def score(self, query: str, passages: Any) -> Any:
        self.load()
        if query == "die":
            os._exit(3)
        return [float(len(passage)) for passage in passages]


class FakeFlorence:
    """Stands in for `app.ort.hosted.HostedFlorence`."""

    HOSTED_METHODS = frozenset({"describe", "tag_image"})

    def __init__(self) -> None:
        self.progress_sink: Any = None

    def describe(self, path: str) -> Any:
        if path == "die":
            os._exit(3)
        if path == "spin":
            end = time.perf_counter() + 1.0
            while time.perf_counter() < end:     # pure Python: holds the lock
                pass
        return f"described:{path}"

    def tag_image(self, path: str) -> Any:
        return None
