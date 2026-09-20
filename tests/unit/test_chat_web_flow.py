"""The order of a turn with the Web switch on: the files first, the web only when they come up thin.

Layer: L8b. The defect these guard (reported 2026-09-20): with Web ON, a question the local files
could already answer still asked permission to search the web. The rule is `app/chat/webrule.py`
(its table is in `test_chat_webrule.py`); this file holds the behaviour that follows from it:

* the files answered  -> no prompt, no request, no connection beyond this computer;
* the files came up thin -> the prompt appears, and after Allow the provider (a recording
  transport) is asked with a short phrase containing none of the local paths, names or passages;
* Skip -> the answer is from the files alone with the plain one-line note;
* the person's own words ask for the web -> the prompt appears even though the files answered, and
  the answer keeps "from your files" and "from the web" visibly apart, web sources marked in the pane.

No network: a recording transport, and a socket guard for the cases that must not connect at all.
"""

from __future__ import annotations

import socket

import pytest

from app.chat.testing import FakeLLM
from app.chat.types import ChatTurn, NarrationEvent, SourcesEvent
from tests.fixtures import chat_eval as fx
from tests.fixtures.web_pages import WIKI_EXTRACT, WIKI_SEARCH
from tests.unit.chat_env import Env

FILES_ANSWER = "How much notice must the tenant give?"
FILES_THIN = "What is a PST file?"
LOCAL = ("127.0.0.1", "localhost", "::1", "", "None")


class Reply:
    def __init__(self, text: str) -> None:
        self.status_code, self.text = 200, text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "application/json"}


class Transport:
    def __init__(self) -> None:
        self.requests: list[dict] = []

    def __call__(self, method, url, *, params=None, headers=None, timeout=8.0):
        self.requests.append({"method": method, "url": url, "params": dict(params or {}),
                              "headers": dict(headers or {})})
        return Reply(WIKI_SEARCH if (params or {}).get("list") == "search" else WIKI_EXTRACT)

    def sent(self) -> str:
        return repr(self.requests)


class Gate:
    """The tab's "Allow this search?" prompt: records what it was asked, answers as told."""

    def __init__(self, answer: bool = True, transport: Transport | None = None) -> None:
        self.answer, self.asked, self.sent_before_answer = answer, [], None
        self.transport = transport

    def __call__(self, query: str) -> bool:
        self.asked.append(query)
        if self.transport is not None:
            self.sent_before_answer = len(self.transport.requests)
        return self.answer


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    e = Env(tmp_path_factory.mktemp("webflow"))
    yield e
    e.close()


@pytest.fixture()
def only_this_computer(monkeypatch):
    """Any attempt to look up or connect to a host that is not this computer is recorded (and fails)."""
    seen: list[str] = []
    real_create, real_gai = socket.create_connection, socket.getaddrinfo

    def create_connection(address, *args, **kwargs):
        seen.append(str(address[0]))
        if str(address[0]) not in LOCAL:
            raise ConnectionRefusedError(f"blocked: {address[0]!r}")
        return real_create(address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        seen.append(str(host))
        if str(host) not in LOCAL:
            raise OSError(f"blocked: {host!r}")
        return real_gai(host, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return seen


def engine(env, transport=None, *, ask_first=True, llm=None):
    eng = env.engine(llm or FakeLLM("extractive"), web_enabled=True, web_provider="wikipedia",
                     web_ask_first=ask_first)
    if transport is not None:
        eng.web_transport = transport
    return eng


def run(eng, question, gate=None, history=None):
    events: list = []
    turn = eng.ask(question, history or [], events.append, lambda: False, web=True, web_gate=gate)
    return turn, events


def _remote(seen: list[str]) -> list[str]:
    return [host for host in seen if host not in LOCAL]


# =========================================================================== the files answered

@pytest.mark.parametrize("question", [FILES_ANSWER, "What is a tenancy agreement?"])
@pytest.mark.parametrize("ask_first", [True, False])
def test_when_the_files_answer_there_is_no_prompt_and_no_connection(env, only_this_computer, question, ask_first):
    gate = Gate()
    eng = engine(env, ask_first=ask_first)                  # the real transport: it would connect
    turn, events = run(eng, question, gate)

    assert turn.kind == "answer" and turn.receipts and not turn.receipts[0].path.startswith("http")
    assert gate.asked == [], "the person must not be asked about a web search the files made unnecessary"
    assert _remote(only_this_computer) == [], "nothing may leave this computer"
    said = " ".join(e.text for e in events if isinstance(e, NarrationEvent))
    assert "web" not in said.lower()
    assert "did not search the web" not in turn.text and "web" not in turn.text.lower()
    assert turn.debug["web_need"] == {"wanted": False, "reason": "files"} and "web" not in turn.debug


def test_with_the_chip_off_nothing_is_decided_or_asked_at_all(env):
    gate = Gate()
    eng = engine(env)
    events: list = []
    turn = eng.ask(FILES_THIN, [], events.append, lambda: False, web=False, web_gate=gate)
    assert gate.asked == [] and "web_need" not in turn.debug


# =========================================================================== the files came up thin

def test_when_the_files_are_thin_the_prompt_appears_and_after_allow_only_a_clean_phrase_is_sent(env):
    transport = Transport()
    gate = Gate(True, transport)
    turn, events = run(engine(env, transport), FILES_THIN, gate)

    assert gate.asked == ["PST file"]                        # shown to the person first
    assert gate.sent_before_answer == 0                      # ...and nothing had left before Allow
    assert transport.requests, "after Allow the provider must be contacted"
    sent = transport.sent().lower()
    for doc in fx.CORPUS:
        name = doc.path.rsplit("/", 1)[-1]
        assert doc.path.lower() not in sent and name.lower() not in sent, doc.path
        assert doc.text[:40].lower() not in sent, doc.path
    assert "archive" not in sent and "c:/" not in sent and "c:\\" not in sent
    assert turn.debug["web_need"]["reason"] in ("none", "thin")
    assert any(r.locator == "Web" for r in turn.receipts)    # the web's part is marked as the web


def test_skip_answers_from_the_files_alone_with_one_plain_sentence(env):
    transport = Transport()
    gate = Gate(False, transport)
    turn, _events = run(engine(env, transport), FILES_THIN, gate)

    assert gate.asked == ["PST file"] and transport.requests == []
    assert turn.kind == "general"
    first = turn.text.split("\n\n", 1)[0]
    assert first == "I couldn't find that in your files. I did not search the web."
    assert "**Not from your files:**" in turn.text           # still visibly not from the files


def test_a_thin_question_about_the_persons_own_affairs_never_prompts(env, only_this_computer):
    gate = Gate()
    turn, _events = run(engine(env), "What did I agree with my solicitor about the zebra?", gate)
    assert gate.asked == [] and _remote(only_this_computer) == []
    assert turn.debug.get("web_need", {}).get("wanted") in (None, False)


def test_the_person_is_never_asked_twice_in_one_turn(env):
    """The files answered thinly, the answer stage then found nothing usable: still one prompt."""
    transport = Transport()
    gate = Gate(False, transport)
    run(engine(env, transport), "How does the boiler pressure valve work in my flat?", gate)
    run(engine(env, transport), FILES_THIN, gate)
    assert len(gate.asked) <= 1


# =========================================================================== the person asks for it

def test_the_persons_own_words_can_ask_for_the_web_even_when_the_files_answered(env):
    transport = Transport()
    gate = Gate(True, transport)
    question = FILES_ANSWER + " Search the web too."
    turn, events = run(engine(env, transport, llm=FakeLLM("extractive")), question, gate)

    assert turn.debug["web_need"] == {"wanted": True, "reason": "asked"}
    assert len(gate.asked) == 1 and "web" not in gate.asked[0].lower().split()   # the instruction is not searched for
    assert transport.requests
    assert "two months notice [1]" in turn.text              # what the files say stands
    locators = {r.locator for r in turn.receipts}
    assert "Web" not in {r.locator for r in turn.receipts if not r.path.startswith("http")}
    offered = [e for e in events if isinstance(e, SourcesEvent)][0].receipts
    assert any(r.path.startswith("C:/") for r in offered) and any(r.locator == "Web" for r in offered)
    assert locators                                          # receipts exist


def test_asking_for_the_web_and_saying_skip_leaves_the_files_answer_whole(env):
    transport = Transport()
    gate = Gate(False, transport)
    turn, _events = run(engine(env, transport), FILES_ANSWER + " Search the web too.", gate)
    assert transport.requests == [] and len(gate.asked) == 1
    assert "two months notice [1]" in turn.text and turn.text.rstrip().endswith("I did not search the web.")


def test_a_files_answered_turn_after_a_web_turn_is_not_prompted_by_the_earlier_one(env):
    transport = Transport()
    gate = Gate(True, transport)
    eng = engine(env, transport)
    history: list[ChatTurn] = []
    first, _ = run(eng, FILES_THIN, gate, history)
    history += [ChatTurn("user", FILES_THIN), first]
    asked_after_first = len(gate.asked)
    run(eng, FILES_ANSWER, gate, history)
    assert len(gate.asked) == asked_after_first
