r"""A stand-in for `app.ort.llm.OnnxLLM`, loaded by the model host in tests.

The host is named a factory (`ModelHost(factory="tests.unit._fake_llm:FakeLLM")`)
and builds this instead of the real model, so the real pipe, the real child
process and the real stop and death paths are tested in seconds without loading
a gigabyte. The prompts below are the test's switches:

    "spin"  holds Python's lock for one second, the way a model load does
    "boom"  fails with a chat-model error
    "die"   ends the whole process after its first piece, the way a fault in
            the model library does
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

    def __init__(self, kind: str = "clip-text", env_file: str = "") -> None:
        self.progress_sink: Any = None

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
        return None


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
