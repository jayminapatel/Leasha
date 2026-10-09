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
