r"""The chat model runs in a host process, so loading it cannot freeze the window.

2026-10-09. Building an ONNX Runtime session holds Python's global lock for the
whole load (a ticker on another thread stopped for 20.1 s while a 1 GB chat
model loaded), so the window froze at every load and Windows labelled it "Not
responding". `app/ort/llm_host.py` holds the model; `app/llm/remote_onnx.py` is
the proxy the window talks to. These tests run the real pipe and the real child
process, with a fake model (`tests/unit/_fake_llm.py`) in place of the real one.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import pytest

from app.core.errors import AppErrorException
from app.llm import engines
from app.llm.remote_models import RemoteEmbedder, RemoteFlorence
from app.llm.remote_onnx import ModelHost, RemoteOnnxLLM

FAKE = "tests.unit._fake_llm:FakeLLM"
FAKE_EMBEDDER = "tests.unit._fake_llm:FakeEmbedder"
FAKE_FLORENCE = "tests.unit._fake_llm:FakeFlorence"


@pytest.fixture
def host():
    process = ModelHost(factory=FAKE)
    try:
        yield process
    finally:
        process.close()


@pytest.fixture
def client(host, tmp_path):
    return RemoteOnnxLLM(host, tmp_path, "", timeout=30.0)


def test_a_call_comes_back_whole(client):
    reply = client.generate("hello")
    assert reply.text.startswith("echo:hello:")
    assert client.chat([{"role": "user", "content": "hi"}]).text.startswith("echo:hi:")


def test_a_stream_arrives_piece_by_piece_in_order(client):
    assert list(client.stream("one two three")) == ["one", "two", "three"]
    assert list(client.chat_stream([{"role": "user", "content": "a b"}])) == ["a", "b"]


def test_stop_ends_a_reply_early(client):
    flag = threading.Event()
    seen = []
    started = time.monotonic()
    for piece in client.stream("w " * 400, should_stop=flag.is_set):
        seen.append(piece)
        if len(seen) == 3:
            flag.set()
    assert 3 <= len(seen) < 100, len(seen)
    assert time.monotonic() - started < 10


def test_an_error_in_the_host_is_the_same_error_here(client):
    with pytest.raises(AppErrorException) as caught:
        client.generate("boom")
    assert caught.value.error.code == "ERR_LOCAL_MODEL_TIMEOUT"


def test_two_replies_at_once_do_not_mix(client):
    out: dict[str, list[str]] = {}

    def run(name, text):
        out[name] = list(client.stream(text))

    threads = [threading.Thread(target=run, args=("a", "a1 a2 a3 a4 a5")),
               threading.Thread(target=run, args=("b", "b1 b2 b3 b4 b5"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert out == {"a": ["a1", "a2", "a3", "a4", "a5"], "b": ["b1", "b2", "b3", "b4", "b5"]}


def test_the_window_keeps_running_while_the_host_holds_its_own_lock(client):
    """The reason for all of it. The host spins for a second with its lock held, as a
    model load does; a ticker on this process's other thread must not notice."""
    client.warm()                                 # the host is up, the imports done
    gaps: list[float] = []
    stop = threading.Event()

    def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    threading.Thread(target=ticker, daemon=True).start()
    time.sleep(0.2)
    gaps.clear()
    started = time.perf_counter()
    client.generate("spin")
    took = time.perf_counter() - started
    stop.set()
    assert took >= 0.9, "the host did not hold its lock long enough to prove anything"
    assert max(gaps) < 0.3, f"this process stalled for {max(gaps):.2f}s while the host loaded"


def test_a_host_that_dies_costs_the_reply_and_the_next_question_starts_another(client, host):
    with pytest.raises(AppErrorException) as caught:
        list(client.stream("die x y z"))
    assert caught.value.error.code == "ERR_MODEL_HOST_ENDED"
    assert client.generate("again").text.startswith("echo:again:")
    assert host.started == 2


def test_the_questions_that_read_the_disk_never_start_the_host(host, tmp_path):
    client = RemoteOnnxLLM(host, tmp_path, "", timeout=30.0)
    client.has_model()
    client.available_models()
    client.serving()
    client.can_think()
    client.context_window()
    assert client.health() in (True, False)
    assert client.is_loaded() is False
    assert host.started == 0, "a cheap question waited on, or started, the host"


def test_a_named_model_and_keep_resident_reach_the_host(host, tmp_path):
    client = RemoteOnnxLLM(host, tmp_path, "", timeout=30.0)
    client.keep_resident = True
    assert "True" in client.generate("x").text          # set before the host existed: replayed
    client.keep_resident = False                         # set while it runs
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and ":True" in client.generate("x").text:
        time.sleep(0.05)
    assert client.generate("x").text.endswith(":False")


def test_only_the_window_asks_for_the_host():
    settings = SimpleNamespace(chat_engine="onnx", model_cache="", embed_device="auto")
    engines.reset_shared()
    try:
        assert type(engines.text_model(settings)).__name__ == "OnnxLLM"      # CLI, tests
        engines.reset_shared()
        engines.use_model_host(True)
        assert type(engines.text_model(settings)).__name__ == "RemoteOnnxLLM"  # the window
    finally:
        engines.use_model_host(False)
        engines.reset_shared()
    assert type(engines.text_model(settings)).__name__ == "OnnxLLM"
    engines.reset_shared()


# --- the other models the window used to load itself ---------------------------------


def test_the_picture_search_encoder_returns_vectors_and_its_download_progress(host):
    encoder = RemoteEmbedder(host, factory=FAKE_EMBEDDER)
    percents = []
    encoder._on_progress = percents.append
    vectors = encoder.embed(["a cat", "dog"])
    assert vectors.shape == (2, 2) and vectors[0][0] == 5.0
    assert percents == [10.0, 100.0], "the download notice reached the window"
    encoder.warm_up()


def test_describe_returns_the_caption_and_never_raises_when_the_host_dies(host):
    florence = RemoteFlorence(host, factory=FAKE_FLORENCE)
    assert florence.describe("a.jpg") == "described:a.jpg"
    assert florence.tag_image("a.jpg") is None
    assert florence.describe("die") is None            # the host ended: the local object's "nothing"
    assert florence.describe("b.jpg") == "described:b.jpg"
    assert host.started == 2


def test_the_window_keeps_running_while_a_vision_model_holds_the_hosts_lock(host):
    florence = RemoteFlorence(host, factory=FAKE_FLORENCE)
    florence.describe("warm")
    gaps: list[float] = []
    stop = threading.Event()

    def ticker():
        last = time.perf_counter()
        while not stop.is_set():
            time.sleep(0.01)
            now = time.perf_counter()
            gaps.append(now - last)
            last = now

    threading.Thread(target=ticker, daemon=True).start()
    time.sleep(0.2)
    gaps.clear()
    started = time.perf_counter()
    florence.describe("spin")
    took = time.perf_counter() - started
    stop.set()
    assert took >= 0.9 and max(gaps) < 0.3, (took, max(gaps))


def test_the_window_installs_the_proxies_and_nothing_else_does():
    from app.extract import florence_tagger

    settings = SimpleNamespace(chat_engine="onnx", model_cache="", embed_device="auto")
    try:
        engines.use_model_host(True)
        assert type(florence_tagger._remote).__name__ == "RemoteFlorence"
        assert type(engines.clip_text_embedder(settings)).__name__ == "RemoteEmbedder"
        assert engines.model_host("chat") is not engines.model_host("vision"),             "a vision load must never share a lock with a chat reply"
    finally:
        engines.use_model_host(False)
    assert florence_tagger._remote is None
    assert type(engines.clip_text_embedder(settings)).__name__ == "Embedder"


def test_a_hosted_florence_is_reached_through_the_taggers_own_entry_points(tmp_path):
    from app.extract import florence_tagger

    class Stub:
        def describe(self, path):
            return f"stub:{path.name}"

        def tag_image(self, path):
            return "tagged"

    florence_tagger.set_engine_process(Stub())
    try:
        assert florence_tagger.describe(tmp_path / "x.jpg") == "stub:x.jpg"
        assert florence_tagger.tag_image(tmp_path / "x.jpg") == "tagged"
    finally:
        florence_tagger.set_engine_process(None)
