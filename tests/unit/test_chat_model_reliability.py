"""The chat models' reliability and memory - the code review of 2026-10-04.

Layer: L1-L8b, no Qt, no network, no model files (fake models, fake transports).

The owner: "the chat is really slow", then "comprehensive code review for
performance consistency and reliability ... Do the recommended push and finish
all". What is pinned here:

* a reply waiting for the model inside Leasha waits only until its own time limit
  or its Stop - never for ever behind an orphaned answer (finding 5);
* the router, the planner and the web query hand the question's Stop to the model;
* at most the default model and one picked are loaded; kept prompt starts share one
  budget across the process and are dropped when idle (finding 4);
* every request to Ollama carries one window, Chat's, and one keep-alive (finding 3);
* a model is loaded ahead only if it fits in the memory free (finding 2);
* a Settings save points Settings' client at Settings' model, not the picked one (6);
* a web search holds to a wall clock and stops when asked (finding 7);
* a model bigger than `AFFORDABLE_MAX_B` is warned about (finding 2, its wording).
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from app.chat import roles, web
from app.chat.llm import OllamaLLM, stop_kwargs
from app.chat.plan import Plan, planner_queries
from app.chat.router import route_question
from app.core.errors import AppErrorException
from app.llm import engines
from app.llm.ollama import KEEP_ALIVE, OllamaClient
from app.ort import llm as ort_llm
from app.ort.llm import OnnxLLM
from tests.unit.test_ort_llm import fake_llm

GB = 1024 ** 3


# ---------------------------------------------------------------------------
# Finding 5: the one-reply-at-a-time lock does not wait for ever
# ---------------------------------------------------------------------------

def _hold(client: OnnxLLM) -> threading.Event:
    """Hold `client`'s reply lock from another thread until the event is set."""
    release, held = threading.Event(), threading.Event()

    def holder() -> None:
        with client._busy:
            held.set()
            release.wait(10)

    threading.Thread(target=holder, daemon=True).start()
    assert held.wait(5)
    return release


def test_a_reply_behind_another_times_out_at_its_own_limit(monkeypatch):
    """Interpret (5 s) waited up to 120 s behind a chat answer."""
    client = fake_llm(monkeypatch, "ok")
    release = _hold(client)
    try:
        started = time.monotonic()
        with pytest.raises(AppErrorException) as caught:
            client.generate("emails from chris", timeout=0.4)
        assert caught.value.error.code == "ERR_LOCAL_MODEL_TIMEOUT"
        assert time.monotonic() - started < 2.0
    finally:
        release.set()


def test_a_stopped_question_stops_waiting_for_the_model(monkeypatch):
    """After Stop, the next question queued behind the orphaned one with no way out."""
    client = fake_llm(monkeypatch, "ok")
    release = _hold(client)
    stop = threading.Event()
    try:
        threading.Timer(0.2, stop.set).start()
        started = time.monotonic()
        pieces = list(client.chat_stream([{"role": "user", "content": "hi"}],
                                         should_stop=stop.is_set, timeout=30))
        assert pieces == [] and time.monotonic() - started < 2.0
    finally:
        release.set()


def test_the_lock_is_free_again_after_a_reply_and_after_a_stop(monkeypatch):
    client = fake_llm(monkeypatch, "fine")
    assert client.generate("q").text == "fine"
    assert client.generate("q", should_stop=lambda: True).text == ""
    assert client._busy.acquire(blocking=False)
    client._busy.release()


def test_the_router_and_the_planner_hand_the_model_their_stop(monkeypatch):
    seen = []

    class Model:
        model = "m"

        def generate(self, prompt, *, should_stop=None, **_kw):
            seen.append(should_stop)
            return SimpleNamespace(text='{"queries": []}' if "JSON" in prompt else "LOOKUP")

    stop = lambda: False                                       # noqa: E731
    route_question("tell me about the thing we discussed at length", (), llm=Model(),
                   should_stop=stop)
    planner_queries(Model(), Plan(question="boiler quote"), should_stop=stop)
    assert seen and all(s is stop for s in seen)


def test_a_model_that_takes_no_stop_is_called_without_one():
    calls = []
    stop = lambda: False                                       # noqa: E731
    plain = SimpleNamespace(generate=lambda prompt, **kw: "x")
    assert stop_kwargs(plain, stop) == {"should_stop": stop}
    strict = SimpleNamespace(generate=lambda prompt, max_tokens=None: "x")
    assert stop_kwargs(strict, lambda: False) == {}
    assert stop_kwargs(strict, None) == {}

    # Ollama answers a whole reply at once: the wrapper drops the Stop for its client.
    def generate(prompt, *, max_tokens=None, num_ctx=None):
        calls.append({"max_tokens": max_tokens, "num_ctx": num_ctx})
        return "ok"

    llm = OllamaLLM(SimpleNamespace(generate=generate, model="m"), num_ctx=8192)
    assert llm.generate("p", max_tokens=3, should_stop=lambda: False) == "ok"
    assert calls[-1] == {"max_tokens": 3, "num_ctx": 8192}


# ---------------------------------------------------------------------------
# Finding 4: what is loaded, and the kept prompt starts
# ---------------------------------------------------------------------------

class _FakeLoaded:
    def __init__(self, folder, spec, device, fmt) -> None:
        self.spec, self.on_gpu, self.prompt_format = spec, False, fmt


@pytest.fixture()
def loading(monkeypatch):
    """`OnnxLLM._ensure` for real, with a stand-in for the model on disk."""
    monkeypatch.setattr(ort_llm, "_resident", [])
    monkeypatch.setattr(ort_llm, "_Loaded", _FakeLoaded)

    def resolve(copies, cache_dir):
        return SimpleNamespace(key=copies[0].key, label=copies[0].key), "folder"

    monkeypatch.setattr(ort_llm.hub, "resolve_any", resolve)

    def make(key: str, *, default: bool = False) -> OnnxLLM:
        client = OnnxLLM(None)
        client._copies = lambda: (SimpleNamespace(key=key),)
        client.keep_resident = default
        return client
    return make


def test_at_most_the_default_and_one_picked_model_are_loaded(loading):
    default, first, second = loading("qwen", default=True), loading("gemma"), loading("phi")
    default.warm()
    first.warm()
    assert ort_llm.resident() == [default, first]
    second.warm()
    assert ort_llm.resident() == [default, second], "the picked one used longest ago goes"
    assert not first.is_loaded() and default.is_loaded() and second.is_loaded()
    first.warm()                                   # used again: loaded again, and the other goes
    assert ort_llm.resident() == [default, first] and not second.is_loaded()


def test_the_default_goes_only_when_nothing_else_can(loading):
    default, picked = loading("qwen", default=True), loading("gemma")
    default.warm()
    picked.warm()
    assert default.is_loaded() and picked.is_loaded()


def test_a_model_asked_to_unload_mid_reply_unloads_when_the_reply_ends(loading):
    client = loading("gemma")
    client.warm()
    assert client._busy.acquire(blocking=False)
    try:
        assert client.unload() is False and client.is_loaded()
        client._after_reply()                       # what the reply does as it ends
        assert not client.is_loaded() and client not in ort_llm.resident()
    finally:
        client._busy.release()


def test_unloading_lets_the_kept_prompts_go(loading):
    client = loading("gemma")
    client.warm()
    client._slots = [([1, 2], {"k": np.zeros(4)}, time.monotonic())]
    assert client.unload() and client._slots == [] and client._prefix_owner is None


def test_reset_shared_unloads_what_it_forgets(tmp_path, loading, monkeypatch):
    engines.reset_shared()
    try:
        shared = engines.text_model(SimpleNamespace(chat_engine="onnx", model_cache=str(tmp_path),
                                                    embed_device="cpu"))
        assert shared.keep_resident, "Settings' model is unloaded last"
        shared._copies = lambda: (SimpleNamespace(key="qwen"),)
        shared.warm()
        assert shared.is_loaded()
    finally:
        engines.reset_shared()
    assert not shared.is_loaded() and ort_llm.resident() == []


def _cache(nbytes: int) -> dict:
    return {"past_key_values.0.key": np.zeros(nbytes, np.uint8)}


def test_the_prompt_budget_is_shared_by_every_loaded_model(monkeypatch):
    """Each picked model kept its own 384 MB; now the process keeps that much in all."""
    monkeypatch.setattr(ort_llm, "PREFIX_BUDGET_BYTES", 250)
    a, b = fake_llm(monkeypatch, "x"), fake_llm(monkeypatch, "y")
    monkeypatch.setattr(ort_llm, "_resident", [a, b])
    a._keep_prefix([1], _cache(100))
    b._keep_prefix([2], _cache(100))
    assert len(a._slots) == 1 and len(b._slots) == 1
    b._keep_prefix([3], _cache(100))                  # 300 > 250: the oldest, a's, goes
    assert a._slots == [] and [s[0] for s in b._slots] == [[2], [3]]


def test_the_newest_prompt_is_kept_even_alone_over_budget(monkeypatch):
    monkeypatch.setattr(ort_llm, "PREFIX_BUDGET_BYTES", 10)
    client = fake_llm(monkeypatch, "x")
    client._keep_prefix([1], _cache(100))
    assert [s[0] for s in client._slots] == [[1]]


def test_kept_prompts_are_dropped_after_ten_quiet_minutes(monkeypatch):
    assert ort_llm.PREFIX_IDLE_S == 600.0
    client = fake_llm(monkeypatch, "ok")
    client.generate("a long standing instruction")
    assert client._slots and client._idle_timer is not None
    client._idle_timer.cancel()
    assert not client.drop_idle_prefixes(), "not while it is still recent"
    later = time.monotonic() + ort_llm.PREFIX_IDLE_S + 1
    assert client.drop_idle_prefixes(now=later) and client._slots == []


def test_kept_prompts_are_not_dropped_under_a_running_reply(monkeypatch):
    client = fake_llm(monkeypatch, "ok")
    client.generate("prompt")
    client._idle_timer.cancel()
    with client._busy:
        assert not client.drop_idle_prefixes(now=time.monotonic() + 10_000)
    assert client._slots


# ---------------------------------------------------------------------------
# Finding 3: one window and one keep-alive for every request to Ollama
# ---------------------------------------------------------------------------

def _recording_client(**kw):
    sent = []

    def transport(method, url, payload=None, timeout=None):
        sent.append((url, payload))
        return {"response": "ok", "models": [{"name": "qwen2.5:1.5b"}]}

    client = OllamaClient("http://x:1", "qwen2.5:1.5b", transport=transport, **kw)
    client.health = lambda force=False: True
    return client, sent


def test_a_client_holds_its_window_for_generate_and_warm():
    client, sent = _recording_client(num_ctx=8192)
    client.generate("Interpret this")
    client.warm()
    options = [p["options"] for url, p in sent if url.endswith("/api/generate")]
    assert [o.get("num_ctx") for o in options] == [8192, 8192]
    assert all(p["keep_alive"] == KEEP_ALIVE for url, p in sent if url.endswith("/api/generate"))


def test_interprets_client_gets_chats_window_from_the_same_settings():
    from app.chat.config import ChatSettings

    settings = SimpleNamespace(chat_engine="ollama", ollama_url="http://x:1", ollama_model="m",
                               chat_context_tokens=6144)
    client = engines.text_model(settings)
    assert client.num_ctx == engines.ollama_context(settings) == 6144
    # 2026-10-05: the same machine on both sides. `ollama_context` reads this
    # computer's memory; the right-hand side said 32 GB whatever it was, so
    # the test passed on the owner's 32 GB laptop and failed on a 7 GB runner.
    import psutil

    here = SimpleNamespace(ram_mb=int(psutil.virtual_memory().total / 1024 ** 2))
    assert engines.ollama_context(SimpleNamespace()) == ChatSettings.from_settings(
        SimpleNamespace(), profile=here).context_tokens


def test_a_picked_ollama_model_for_interpret_carries_the_window_too():
    from app.ui.tasks import interpret_client

    settings = SimpleNamespace(ollama_url="http://x:1", chat_context_tokens=6144)
    client = interpret_client(settings, "ollama:qwen2.5:1.5b")
    assert client.model == "qwen2.5:1.5b" and client.num_ctx == 6144


def test_chat_streams_with_the_same_keep_alive_as_every_other_call():
    seen = []
    llm = OllamaLLM(SimpleNamespace(url="http://x:1", model="m", health=lambda: True),
                    num_ctx=4096, stream_transport=lambda url, payload, budget:
                    seen.append(payload) or iter([{"response": "hi", "done": True}]))
    assert "".join(llm.stream("p")) == "hi"
    assert seen[0]["keep_alive"] == KEEP_ALIVE and seen[0]["options"]["num_ctx"] == 4096


# ---------------------------------------------------------------------------
# Finding 2: a model is loaded ahead only when it fits
# ---------------------------------------------------------------------------

def test_a_model_is_loaded_ahead_only_if_it_fits_now():
    from app.ui.tasks import warm_if_fits

    calls = []
    warmer = lambda: calls.append(1) or True                  # noqa: E731
    assert not warm_if_fits(warmer, int(17.7 * GB), free_mb=20_000), "26B on a 32 GB laptop"
    assert not warm_if_fits(warmer, 0, free_mb=20_000), "unknown size: not unasked"
    assert calls == []
    assert warm_if_fits(warmer, int(1.7 * GB), free_mb=8_000) and calls == [1]

    def broken():
        raise RuntimeError("no")

    assert warm_if_fits(broken, GB, free_mb=8_000) is False, "never raises"


def test_interprets_remembered_big_pick_is_not_loaded_at_start_up(monkeypatch):
    from app.ui import tasks

    warmed = []
    monkeypatch.setattr(tasks, "free_memory_mb", lambda: 20_000)
    monkeypatch.setattr(OllamaClient, "warm", lambda self, **kw: warmed.append(self.model) or True)
    settings = SimpleNamespace(ollama_url="http://x:1")
    tasks.interpret_client(settings, "ollama:gemma4:26b", warm=True, size_bytes=int(17.7 * GB))
    assert warmed == []
    tasks.interpret_client(settings, "ollama:qwen2.5:1.5b", warm=True, size_bytes=GB)
    assert warmed == ["qwen2.5:1.5b"]


def test_a_model_bigger_than_the_affordable_size_is_warned_about():
    big = roles.ModelOption("ollama:gemma4:26b", "gemma4:26b · Ollama · 17 GB", 17 * GB)
    note = roles.large_model_note(big)
    assert "gemma4:26b" in note and "26B" in note and "minutes" in note
    assert roles.large_model_note(roles.ModelOption("ollama:qwen2.5:1.5b", "q", GB)) == ""
    assert roles.large_model_note(roles.ModelOption("ollama:mistral:latest", "m", 4 * GB)) == ""
    assert roles.large_model_note(None) == ""


# ---------------------------------------------------------------------------
# Finding 6: a Settings save and a picked Interpret model
# ---------------------------------------------------------------------------

def test_settings_model_goes_to_settings_client_not_the_picked_one():
    from app.search.translate import QueryTranslator

    picked = SimpleNamespace(model="gemma4:26b", set_model=lambda name: setattr(picked, "model", name))
    default = SimpleNamespace(model="qwen", set_model=lambda name: setattr(default, "model", name))
    translator = QueryTranslator(picked, enabled=True)
    translator.reconfigure(model="mistral", timeout_s=9, model_client=default)
    assert (picked.model, default.model) == ("gemma4:26b", "mistral")
    assert translator.client is picked and translator.timeout_s == 9
    translator.reconfigure(model="phi3")                      # no picker: the one in use
    assert picked.model == "phi3"


# ---------------------------------------------------------------------------
# Finding 7: the web is held to a wall clock, and stops when asked
# ---------------------------------------------------------------------------

class _Slow:
    """A transport whose every request takes `delay` and redirects `hops` times."""

    def __init__(self, delay: float, hops: int = 0) -> None:
        self.delay, self.hops, self.timeouts = delay, hops, []

    def __call__(self, method, url, *, params=None, headers=None, timeout=8.0):
        self.timeouts.append(timeout)
        if self.delay > timeout:                   # as `requests` does: out of time
            time.sleep(timeout)
            raise TimeoutError("read timed out")
        time.sleep(self.delay)
        if len(self.timeouts) <= self.hops:
            return SimpleNamespace(status_code=302, headers={"Location": url + "x"}, content=b"",
                                   text="")
        return SimpleNamespace(status_code=200, headers={"Content-Type": "text/plain"},
                               content=b"plain words", text="plain words")


def test_redirects_share_one_time_limit_rather_than_each_having_it():
    slow = _Slow(0.15, hops=3)
    started = time.monotonic()
    pages = web.fetch_pages([web.WebHit("t", "http://example.org/a", "s", "example.org")],
                            timeout=0.3, transport=slow)
    assert time.monotonic() - started < 0.9, "four hops, one limit"
    assert pages == [] and len(slow.timeouts) < 4
    assert all(later_one < earlier for earlier, later_one in zip(slow.timeouts, slow.timeouts[1:]))


def test_the_pages_stop_at_the_deadline_and_at_stop():
    hits = [web.WebHit("t", f"http://{x}.example.org/", "s", "example.org") for x in "abc"]
    slow = _Slow(0.0)
    assert web.fetch_pages(hits, transport=slow, deadline=time.monotonic() - 1) == []
    assert web.fetch_pages(hits, transport=slow, should_stop=lambda: True) == []
    assert slow.timeouts == []
    assert len(web.fetch_pages(hits, transport=slow, max_pages=3)) == 3


def test_a_stopped_question_does_not_search_the_web():
    calls = []
    result = web.run_web("pst file", web.WebSettings(enabled=True, provider="wikipedia"),
                         transport=lambda *a, **k: calls.append(a), should_stop=lambda: True)
    assert result.error == web.MSG_STOPPED and calls == []


class _Trickle:
    """A `requests` response that sends a byte every `gap` seconds, for ever - or
    blocks in its read until it is closed."""

    def __init__(self, gap: float, *, blocking: bool = False) -> None:
        self.gap, self.blocking = gap, blocking
        self.status_code, self.headers = 200, {"content-type": "text/plain"}
        self.closed = threading.Event()

    def iter_content(self, _size):
        while True:
            if self.blocking:
                self.closed.wait(10)
                raise ConnectionError("connection closed under the read")
            time.sleep(self.gap)
            yield b"x"

    def close(self):
        self.closed.set()


@pytest.mark.parametrize("blocking", [False, True])
def test_a_trickling_server_is_cut_off_at_the_time_limit(monkeypatch, blocking):
    import requests

    reply = _Trickle(0.05, blocking=blocking)
    monkeypatch.setattr(requests, "request", lambda *a, **k: reply)
    started = time.monotonic()
    with pytest.raises(web._WebError) as caught:
        web._default_transport("GET", "http://example.org/", timeout=0.4)
    assert str(caught.value) == web.MSG_TIMEOUT
    assert time.monotonic() - started < 1.5 and reply.closed.is_set()
