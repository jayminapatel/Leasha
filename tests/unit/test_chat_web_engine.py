"""The web in the conversation: optional, local first, and private by construction.

Layer: L8b. The owner's addition of 2026-09-20 ("and optionally can augment from web") to the
"chat should use local source though" clarification. The rules these tests hold:

* **OFF by default**, and off means no connection at all (`test_chat_conversation.py` runs whole
  conversations under a socket guard; this file adds the switch cases);
* **local first**: the files are searched before the web is asked, and the web never replaces what the
  files say;
* **only a short search phrase leaves the machine** - never a file name, a path, a passage, an email or
  the conversation - checked by planting distinctive strings in the local documents and reading every
  outgoing request;
* **ask first** really blocks until the person allows it; skip means skip;
* the web failing is one plain sentence, and the answer stands on the files.

No network: every call goes through a recording transport.
"""

from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import pytest

from app.chat.reconcile import audit_answer
from app.chat.testing import FakeLLM
from app.chat.types import ChatTurn, NarrationEvent, SourcesEvent, TokenEvent
from tests.fixtures import chat_eval as fx
from tests.fixtures.web_pages import WIKI_EXTRACT, WIKI_SEARCH
from tests.unit.chat_env import Env, ask

PLANTED = ("ZEBRA-4471", "Quillfeather", "C:/Private", "C:\\Private", "purchase-order-quillfeather.pdf",
           "Bartholomew Quill")


class Reply:
    def __init__(self, text: str, status: int = 200, kind: str = "application/json") -> None:
        self.status_code, self.text = status, text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": kind}


class Transport:
    """A recording stand-in for the network: Wikipedia's two calls, canned."""

    def __init__(self, *, fail: bool = False) -> None:
        self.requests: list[dict] = []
        self.fail = fail

    def __call__(self, method, url, *, params=None, headers=None, timeout=8.0):
        self.requests.append({"method": method, "url": url, "params": dict(params or {}),
                              "headers": dict(headers or {}), "timeout": timeout})
        if self.fail:
            raise ConnectionError("no route to host")
        if (params or {}).get("list") == "search":
            return Reply(WIKI_SEARCH)
        return Reply(WIKI_EXTRACT)

    def everything_sent(self) -> str:
        return repr(self.requests)


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("web"))
    # A local document holding strings that must never leave this computer.
    text = ("Purchase order ZEBRA-4471 for Quillfeather Ltd. A purchase order is approved by Bartholomew Quill "
            "and the total is 8,450 pounds payable within thirty days.")
    file_id = e.store.upsert_file("C:/Private/purchase-order-quillfeather.pdf", parent_dir="C:/Private",
                                  ext="pdf", size_bytes=1, mtime_ns=1, status="INDEXED", source_kind="file")
    e.store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
    yield e
    e.close()


def engine(env, llm=None, *, transport=None, **settings):
    settings.setdefault("web_ask_first", False)
    eng = env.engine(llm or FakeLLM("extractive"), web_enabled=True, web_provider="wikipedia", **settings)
    eng.web_transport = transport if transport is not None else Transport()
    return eng


def run(eng, question, history=None, **kwargs):
    events: list = []
    turn = eng.ask(question, history or [], events.append, lambda: False, **kwargs)
    return turn, events


# =========================================================================== off means off

def test_the_web_is_off_by_default_whatever_the_conversation_asks(env):
    transport = Transport()
    off = env.engine(FakeLLM())                                     # web_enabled is False
    off.web_transport = transport
    turn, _events = run(off, "What is a PST file?", web=True)       # the chip says on, Settings says off
    assert transport.requests == [] and turn.kind == "general"
    assert not off.cfg.web_enabled


def test_a_conversation_with_the_chip_off_never_searches_even_when_settings_allow_it(env):
    transport = Transport()
    on = engine(env, transport=transport)
    turn, _events = run(on, "What is a PST file?", web=False)
    assert transport.requests == [] and turn.kind == "general"


def test_small_talk_and_personal_questions_never_use_the_web(env):
    transport = Transport()
    on = engine(env, transport=transport)
    run(on, "hi", web=True)
    run(on, "What did I agree with my solicitor about the zebra?", web=True)
    assert transport.requests == []


# =========================================================================== local first, web augments

def test_the_files_are_searched_before_the_web_is_asked(env):
    order: list[str] = []
    real_search = env.search.search

    def search(*a, **k):
        order.append("files")
        return real_search(*a, **k)

    env.search.search = search
    transport = Transport()
    real_call = transport.__call__

    def traced(*a, **k):
        order.append("web")
        return real_call(*a, **k)

    try:
        eng = engine(env, transport=traced)
        eng.web_transport = traced
        run(eng, "What is a PST file?", web=True)
    finally:
        env.search.search = real_search
    assert order[0] == "files" and "web" in order and order.index("files") < order.index("web")


def test_when_the_files_have_nothing_the_web_answers_after_one_plain_sentence_and_is_marked_as_the_web(env):
    transport = Transport()
    eng = engine(env, transport=transport)
    turn, events = run(eng, "What is a PST file?", web=True)

    assert turn.kind == "answer" and turn.text.startswith("I couldn't find that in your files.\n\n")
    body = turn.text.split("\n\n", 1)[1]
    assert body.startswith("From the web:")                          # visibly not from the files
    assert turn.receipts and all(r.path.startswith("https://en.wikipedia.org/") for r in turn.receipts)
    assert all(r.locator == "Web" for r in turn.receipts) and audit_answer(turn) == []
    offered = [e for e in events if isinstance(e, SourcesEvent)][0].receipts
    assert offered and offered[0].locator == "Web"                   # marked in the sources pane from the start
    assert {urlparse(r["url"]).hostname for r in transport.requests} == {"en.wikipedia.org"}


def test_what_the_files_say_stands_and_the_web_is_offered_beside_it_never_instead(env):
    llm = FakeLLM("extractive")
    transport = Transport()
    eng = engine(env, llm, transport=transport)
    turn, _events = run(eng, "How much notice must the tenant give? Search the web too.", web=True)
    assert "two months notice [1]" in turn.text and turn.receipts[0].path.startswith("C:/")
    system = llm.chat_calls[0][1][0]["content"]
    assert "Web passages:" in system and "never overruled by the web" in system
    import re

    files, web = system.split("Web passages:")
    assert "Sources:\n[1] tenancy-agreement" in files                # the files first
    local = len(re.findall(r"^\[\d+\] ", files.split("Sources:")[1], re.M))
    assert re.search(rf"^\[{local + 1}\] Personal Storage Table", web, re.M)   # the web is numbered on from them


def test_claims_from_the_web_are_checked_against_the_web_passages(env):
    llm = FakeLLM(chat_replies=[
        "From the web: A Personal Storage Table is an open proprietary file format [1]. "
        "It was invented by a team of forty engineers in Lisbon in 1991 [1]."])
    turn, _events = run(engine(env, llm), "What is a PST file?", web=True)
    assert "open proprietary file format" in turn.text
    assert "Lisbon" not in turn.text and "1991" not in turn.text     # not on the page, so not in the answer


def test_a_web_that_cannot_help_falls_back_to_the_labelled_general_answer_with_one_sentence_about_why(env):
    transport = Transport(fail=True)
    turn, _events = run(engine(env, transport=transport), "What is a PST file?", web=True)
    assert turn.kind == "general"
    first = turn.text.split("\n\n", 1)[0]
    assert first.startswith("I couldn't find that in your files.") and "could not be reached" in first
    assert "**Not from your files:**" in turn.text


def test_a_web_failure_beside_a_good_local_answer_is_said_once_and_the_answer_stands(env):
    turn, _events = run(engine(env, transport=Transport(fail=True)), "How much notice must the tenant give? Search the web too.", web=True)
    assert turn.kind == "answer" and "two months notice [1]" in turn.text
    assert turn.text.rstrip().endswith("The web search could not be reached.")


# =========================================================================== privacy

def test_nothing_the_files_hold_ever_appears_in_anything_sent(env):
    """The planted strings are in the local documents; the question is the person's own words. Every
    request - URL, parameters, headers - is read for them."""
    transport = Transport()
    eng = engine(env, transport=transport)
    turn, events = run(eng, "What is a purchase order? Search the web too.", web=True)
    assert transport.requests, "the web must actually have been asked - otherwise this proves nothing"
    sent = transport.everything_sent()
    for secret in PLANTED:
        assert secret.lower() not in sent.lower(), secret
    assert "purchase order" in transport.requests[0]["params"]["srsearch"].lower()
    # ...and the local passage did reach the model's prompt (it is local), just not the network
    assert any("ZEBRA-4471" in m["content"] for k, ms in eng._role("answerer").chat_calls for m in ms)


def test_a_model_that_tries_to_smuggle_local_data_into_the_search_phrase_is_cleaned(env):
    class Smuggler(FakeLLM):
        def generate(self, prompt, **kw):
            from app.chat.testing import FakeReply

            if prompt.startswith("Write a short web search query"):
                return FakeReply("purchase order ZEBRA-4471 Quillfeather C:/Private/purchase-order-quillfeather.pdf "
                                 "Bartholomew Quill 8,450 pounds")
            return super().generate(prompt, **kw)

    transport = Transport()
    run(engine(env, Smuggler("extractive"), transport=transport), "What is a purchase order? Search the web too.", web=True)
    assert transport.requests
    sent = transport.everything_sent().lower()
    for secret in PLANTED + ("8,450", "8450"):
        assert secret.lower() not in sent, secret


def test_only_a_short_phrase_and_a_polite_agent_are_sent(env):
    transport = Transport()
    run(engine(env, transport=transport), "What is a PST file and how is one opened in Outlook these days?", web=True)
    first = transport.requests[0]
    assert len(first["params"]["srsearch"].split()) <= 10
    assert set(first["headers"]) <= {"User-Agent", "Accept", "Accept-Language"}
    assert first["headers"]["User-Agent"].startswith("Leasha-Chat/")
    for request in transport.requests:                            # no cookies, referrer or account, ever
        assert not {"Cookie", "Referer", "Authorization"} & set(request["headers"])


# =========================================================================== ask first

def _threaded(eng, question, gate):
    out: dict = {}
    events: list = []

    def work():
        out["turn"] = eng.ask(question, [], events.append, lambda: False, web=True, web_gate=gate)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    return thread, out, events


def test_ask_first_blocks_until_allowed_and_shows_the_phrase_before_anything_is_sent(env):
    transport = Transport()
    eng = engine(env, transport=transport, web_ask_first=True)
    asked: list[str] = []
    release = threading.Event()

    def gate(query):
        asked.append(query)
        assert release.wait(timeout=20)
        return True

    thread, out, events = _threaded(eng, "What is a PST file?", gate)
    deadline = time.time() + 15
    while not asked and time.time() < deadline:
        time.sleep(0.02)
    assert asked and asked[0] == "PST file"                       # the exact phrase the person is asked about
    time.sleep(0.3)
    assert transport.requests == [], "nothing may be sent while the person has not answered"
    assert any(isinstance(e, NarrationEvent) and '"PST file"' in e.text for e in events)   # and it was shown
    release.set()
    thread.join(timeout=30)
    assert out["turn"].kind == "answer" and transport.requests


def test_skip_means_nothing_is_sent_and_the_answer_stands_on_the_files_with_one_sentence_saying_so(env):
    transport = Transport()
    eng = engine(env, transport=transport, web_ask_first=True)
    thread, out, _events = _threaded(eng, "How much notice must the tenant give? Search the web too.", lambda q: False)
    thread.join(timeout=30)
    assert transport.requests == []
    assert "two months notice [1]" in out["turn"].text and out["turn"].text.rstrip().endswith("I did not search the web.")


def test_ask_first_without_a_gate_fails_closed(env):
    transport = Transport()
    turn, _events = run(engine(env, transport=transport, web_ask_first=True), "What is a PST file?", web=True)
    assert transport.requests == [] and turn.kind == "general"


def test_when_ask_first_is_off_the_phrase_is_still_shown_before_it_is_sent(env):
    transport = Transport()
    seen_before_send: list[bool] = []

    def traced(method, url, **kw):
        seen_before_send.append(any(isinstance(e, NarrationEvent) and "Searching the web for" in e.text
                                    for e in events))
        return transport(method, url, **kw)

    eng = engine(env)
    eng.web_transport = traced
    events: list = []
    eng.ask("What is a PST file?", [], events.append, lambda: False, web=True)
    assert seen_before_send and all(seen_before_send)


def test_stop_during_the_ask_lets_the_engine_go_without_searching(env):
    transport = Transport()
    eng = engine(env, transport=transport, web_ask_first=True)
    stop = threading.Event()
    release = threading.Event()

    def gate(query):
        release.wait(timeout=20)
        return False

    events: list = []
    out: dict = {}

    def work():
        out["turn"] = eng.ask("What is a PST file?", [], events.append, stop.is_set, web=True, web_gate=gate)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    time.sleep(0.3)
    stop.set()
    release.set()
    thread.join(timeout=30)
    assert transport.requests == [] and isinstance(out["turn"], ChatTurn)
