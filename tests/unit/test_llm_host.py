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


# --- 2026-10-10: the meaning model and the reranker, in the search host ----------------
#
# The two models the window still loaded itself, at start-up (owner: "take the other
# models into the helper too"). `RemoteEmbedder.for_meaning` and `RemoteReranker` are
# what the window's search engine is given; the fakes below stand in for the hosted
# adapters (`app/ort/hosted.py`), and those adapters are tested in this process further
# down, with the real `Embedder` and `Reranker` and no model.

FAKE_RERANKER = "tests.unit._fake_llm:FakeReranker"

_SEARCH_SETTINGS = dict(
    chat_engine="onnx", model_cache="", embed_device="auto", embed_model="meaning-m",
    embed_dim=2, embed_quantised=False, rerank_model="rerank-m", rerank_enabled=False,
)


def _refusing_popen(*_args, **_kwargs):
    raise OSError("the process could not be created")


def test_the_query_embedder_answers_from_the_search_host(host):
    from app.index.embedder import Embedder

    percents: list = []
    local = Embedder("meaning-m", dim=2, batch_size=2, on_progress=percents.append)
    remote = RemoteEmbedder.for_meaning(host, local, factory=FAKE_EMBEDDER)
    assert remote.embed([]) == [] and host.started == 0, "no texts, no host started"
    assert (remote.model_name, remote.dim, remote.batch_size) == ("meaning-m", 2, 2)
    assert remote.choice is None, "the processor is the host's; this side never gates on it"
    vectors = remote.embed(["ab"])
    assert vectors.shape == (1, 2) and vectors[0][0] == 2.0
    assert list(map(list, remote.embed_all(["a", "bb", "ccc"]))) == [[1.0, 1.0], [2.0, 1.0],
                                                                     [3.0, 1.0]]
    assert remote.loaded
    remote.warm_up()
    assert percents[:2] == [10.0, 100.0] and percents[-1] == 50.0, \
        "the splash's download hook stays here and hears the host's progress"


def test_the_reranker_scores_in_the_host_and_keeps_its_own_rules(host):
    from app.llm.remote_models import RemoteReranker

    ranker = RemoteReranker("rerank-m", host=host, factory=FAKE_RERANKER, enabled=True, top_n=10)
    hits = [{"text": "a"}, {"path": "photo.jpg"}, {"text": "ccc"}, {"text": "bb"}]
    ranked = ranker.rerank("q", hits)
    assert ranked[1] == {"path": "photo.jpg"}, "a textless row keeps its fused place"
    assert [hit.get("text") for hit in ranked] == ["ccc", None, "bb", "a"]
    assert ranker.available and ranker.warm_up()
    assert ranker.choice is None, "no graphics-card gate is taken on this side"
    ranker.enabled = False                       # the Rerank switch, live, with nothing sent
    assert ranker.rerank("q", hits) == hits


def test_a_reranker_that_is_off_starts_no_host(host):
    from app.llm.remote_models import RemoteReranker

    ranker = RemoteReranker("rerank-m", host=host, factory=FAKE_RERANKER, enabled=False)
    hits = [{"text": "a"}, {"text": "ccc"}]
    assert ranker.warm_up() is False
    assert ranker.rerank("q", hits) == hits
    assert host.started == 0


def test_a_reranker_whose_host_dies_keeps_the_fused_order_and_recovers(host):
    from app.llm.remote_models import RemoteReranker

    ranker = RemoteReranker("rerank-m", host=host, factory=FAKE_RERANKER, enabled=True)
    hits = [{"text": "a"}, {"text": "ccc"}]
    assert ranker.rerank("die", hits) == hits, "a dead host is a scoring failure: fused order"
    assert ranker.available, "one failure is within the budget"
    assert [hit["text"] for hit in ranker.rerank("q", hits)] == ["ccc", "a"]
    assert host.started == 2


def test_a_reranker_model_that_will_not_load_in_the_host_is_todays_failed_load(host):
    from app.llm.remote_models import RemoteReranker

    ranker = RemoteReranker("absent", host=host, factory=FAKE_RERANKER, enabled=True)
    hits = [{"text": "a"}, {"text": "ccc"}]
    assert ranker.warm_up() is False
    assert ranker.available is False, "latched for the session, as a local failed load is"
    assert ranker.rerank("q", hits) == hits


def test_a_search_host_that_cannot_start_degrades_as_a_failed_load_did():
    """No host: keyword-only results with the notice, the fused order, no raise."""
    from app.index.embedder import Embedder
    from app.llm.remote_models import RemoteReranker
    from app.search import vector
    from app.search.engine import SearchEngine
    from app.search.query import parse_query

    host = ModelHost(popen=_refusing_popen)
    try:
        embedder = RemoteEmbedder.for_meaning(host, Embedder("meaning-m", dim=2),
                                              factory=FAKE_EMBEDDER)
        ranker = RemoteReranker("rerank-m", host=host, factory=FAKE_RERANKER, enabled=True)
        with pytest.raises(AppErrorException) as caught:
            embedder.embed(["boiler"])
        assert caught.value.error.code == "ERR_MODEL_HOST_ENDED", \
            "the host's own error, not a bare OSError"
        problems: list = []
        assert vector.search(None, embedder, parse_query("boiler pressure"),
                             problems=problems) == []
        assert problems, "the keyword-only notice has its cause"
        # The warm-up on the window's worker: neither model raises out of it.
        SearchEngine.warm_up(SimpleNamespace(embedder=embedder, reranker=ranker))
        assert ranker.available is False
        hits = [{"text": "a"}, {"text": "ccc"}]
        assert ranker.rerank("q", hits) == hits
    finally:
        host.close()


def test_the_window_builds_its_search_models_in_the_search_host():
    from app.index.embedder import Embedder
    from app.llm.remote_models import RemoteReranker
    from app.search.rerank import Reranker

    settings = SimpleNamespace(**_SEARCH_SETTINGS)
    try:
        engines.use_model_host(True)
        embedder = engines.meaning_embedder(settings)
        ranker = engines.search_reranker(settings, enabled=True)
        assert type(embedder).__name__ == "RemoteEmbedder"
        assert isinstance(ranker, RemoteReranker) and ranker.enabled
        search = engines.model_host(engines.SEARCH_HOST)
        assert embedder._host is search and ranker._host is search
        others = (engines.model_host("chat"), engines.model_host("vision"))
        assert all(search is not other for other in others), \
            "a query must never wait behind a chat or Florence-2 load"
        assert not search.alive, "building them starts nothing: the warm-up does"
    finally:
        engines.use_model_host(False)
    assert type(engines.meaning_embedder(settings)) is Embedder       # command line, tests
    assert type(engines.search_reranker(settings)) is Reranker


def _calls_in(path) -> list[tuple[str, str]]:
    """Every `Name.attribute(...)` call in a source file, as (name, attribute)."""
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [(node.func.value.id, node.func.attr) for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)]


def test_the_windows_engine_is_given_the_hosted_models():
    """`app/main.py` builds the window's `SearchEngine` models through the factories,
    after turning the hosts on, and closes the hosts before it exits."""
    from pathlib import Path

    main = Path(__file__).resolve().parents[2] / "app" / "main.py"
    calls = _calls_in(main)
    assert ("_engines", "meaning_embedder") in calls
    assert ("_engines", "search_reranker") in calls
    assert ("Embedder", "from_settings") not in calls, "the window's own load is back"
    assert ("Reranker", "from_settings") not in calls, "the window's own load is back"
    assert ("_engines", "close_hosts") in calls
    text = main.read_text(encoding="utf-8")
    assert text.index("_engines.use_model_host(True") < text.index("_engines.meaning_embedder(")
    assert text.index("_engines.close_hosts()") < text.index("_exit_fast(code)")


def test_the_index_run_keeps_its_own_meaning_model():
    """Only the window's query-time models move: every index run builds a local
    `Embedder`, whether or not the window has turned the hosts on."""
    from pathlib import Path

    from app.index.embedder import Embedder

    root = Path(__file__).resolve().parents[2] / "app"
    for builder in (root / "cli" / "index.py", root / "ui" / "controllers" / "index_controller.py",
                    root / "index" / "offline_media.py"):
        calls = _calls_in(builder)
        assert ("Embedder", "from_settings") in calls, builder.name
        assert not any(attr in ("meaning_embedder", "search_reranker") for _, attr in calls), \
            builder.name
    settings = SimpleNamespace(**_SEARCH_SETTINGS)
    try:
        engines.use_model_host(True)
        assert type(Embedder.from_settings(settings)) is Embedder
    finally:
        engines.use_model_host(False)


def test_closing_asks_every_host_to_leave_and_is_bounded(monkeypatch):
    closed: list[str] = []

    class Quick:
        def close(self):
            closed.append("quick")

    class Stuck:
        def close(self):
            closed.append("stuck")
            time.sleep(5.0)

    monkeypatch.setattr(engines, "_hosts", {"chat": Quick(), "search": Stuck(), "vision": Quick()})
    started = time.monotonic()
    engines.close_hosts(wait_s=0.3)
    assert time.monotonic() - started < 2.0, "a stuck host must not hold up the exit"
    assert sorted(closed) == ["quick", "quick", "stuck"]


def test_a_real_host_is_closed_by_close_hosts(monkeypatch, host):
    host.ensure()
    assert host.alive
    monkeypatch.setattr(engines, "_hosts", {"chat": host})
    engines.close_hosts()
    assert not host.alive


# --- the hosted adapters themselves, in this process, with no model ------------------


def test_the_hosted_meaning_model_is_the_embedder_the_window_asked_for():
    from app.ort.hosted import HostedEmbedder

    with pytest.raises(ValueError):
        HostedEmbedder(kind="nothing")
    hosted = HostedEmbedder(kind="meaning", spec={
        "model_name": "meaning-m", "dim": 2, "cache_dir": None, "batch_size": 4,
        "device": "cpu", "threads": 0, "quantised": False})
    built = hosted._built()
    assert (built.model_name, built.dim, built.batch_size, built.device) == \
        ("meaning-m", 2, 4, "cpu")
    built._encoder = lambda texts: [[3.0, 4.0] for _ in texts]   # no model: an injected encoder
    sink: list = []
    hosted.progress_sink = sink.append
    vectors = hosted.embed(["x"])
    assert abs(float(vectors[0][0]) - 0.6) < 1e-6 and abs(float(vectors[0][1]) - 0.8) < 1e-6
    assert built._on_progress == sink.append, "the download hook reaches the real embedder"


def test_a_hosted_reranker_says_why_it_did_not_load(monkeypatch):
    import sys

    from app.ort.hosted import HostedReranker

    # The import fails at once (`ModuleNotFoundError`), without loading FastEmbed.
    monkeypatch.setitem(sys.modules, "fastembed", None)
    monkeypatch.setitem(sys.modules, "fastembed.rerank.cross_encoder", None)
    hosted = HostedReranker("rerank-m")
    for _attempt in range(2):                    # asked again, it tries again
        with pytest.raises(RuntimeError, match="ModuleNotFoundError"):
            hosted.load()


def test_a_hosted_reranker_scores_and_takes_its_own_card_after_a_driver_reset(monkeypatch):
    from app.core import gpu_serialize
    from app.ort.hosted import HostedReranker

    hosted = HostedReranker("rerank-m")
    hosted._reranker._scorer = lambda query, passages: [len(p) for p in passages]
    assert hosted.score("q", ["a", "bbb"]) == [1.0, 3.0]

    marked: list = []
    monkeypatch.setattr(gpu_serialize, "mark_gpu_unreliable", marked.append)

    def reset(query, passages):
        raise RuntimeError("DXGI device removed 887A0005")

    hosted._reranker._scorer = reset
    with pytest.raises(RuntimeError):
        hosted.score("q", ["a"])
    assert marked, "the host, which holds the session, marks the card"
    assert hosted._reranker._scorer is None, "the dead session is not called again"
