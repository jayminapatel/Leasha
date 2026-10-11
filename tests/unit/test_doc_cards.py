r"""A card for every document: what it is, who is in it, its dates and sums.

Layer: L6

Order `smarter-answers` section 2 (owner, 2026-10-12). The model is read, not
trusted: names, sums and years must be in the document, the kind from a fixed
list. A card is a labelled passage, so keyword and meaning search find it with
no change to search, and a `doc_cards` row, so a changed file is carded again.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from types import SimpleNamespace

import pytest

from app.index import doc_cards as cards
from app.storage.sqlite_store import SqliteStore

LETTER = (
    "Invoice 1043 from Acme Plumbing Ltd to Jordan Smith, 14 March 2024.\n"
    "Replacing the boiler valve and labour: GBP 450.00, due by 2024-04-14.\n"
    + "Thank you for your business. " * 10
)

REPLY = json.dumps({
    "kind": "invoice",
    "people": ["Jordan Smith", "Alex Brown"],            # Alex is not in the letter
    "organisations": ["Acme Plumbing Ltd"],
    "dates": ["2024-03-14", "2019-01-01", "14/03/2024"],  # 2019 is not in it; the last is not ISO
    "amounts": ["GBP 450.00", "£999"],              # 999 is not in it
    "summary": "An invoice for a boiler valve. It is due in April. It has a third sentence.",
})


# -- reading a reply ----------------------------------------------------------------


def test_only_what_the_document_says_is_kept():
    card = cards.read_card(REPLY, LETTER, today=date(2026, 10, 12))

    assert card is not None
    assert card.kind == "invoice"
    assert card.people == ("Jordan Smith",)
    assert card.organisations == ("Acme Plumbing Ltd",)
    assert card.dates == ("2024-03-14",)
    assert card.amounts == ("GBP 450.00",)
    assert card.summary == "An invoice for a boiler valve. It is due in April."
    assert {"person:Alex Brown", "date:2019-01-01", "date:14/03/2024",
            "amount:£999"} <= set(card.dropped)


def test_a_kind_off_the_list_is_other_and_nonsense_is_nothing():
    odd = cards.read_card('{"kind": "a love poem", "summary": "Verses."}', LETTER)
    assert odd is not None and odd.kind == "other" and odd.summary == "Verses."

    assert cards.read_card("I cannot help with that.", LETTER) is None
    assert cards.read_card('{"kind": "other"}', LETTER) is None
    assert cards.read_card('{"kind": ', LETTER) is None


def test_the_reply_may_have_words_around_the_json():
    card = cards.read_card('Here you are: {"kind": "letter", "summary": "A note."} Done.', LETTER)
    assert card is not None and card.kind == "letter"


def test_a_currency_sign_becomes_its_code():
    card = cards.read_card(json.dumps({"kind": "receipt", "amounts": ["£450.00"]}), LETTER)
    assert card is not None and card.amounts == ("GBP 450.00",)


def test_the_card_text_names_its_fields_in_plain_words():
    card = cards.read_card(REPLY, LETTER, today=date(2026, 10, 12))
    text = cards.card_text(card)

    assert text.startswith("Invoice. People: Jordan Smith.")
    assert "Organisations: Acme Plumbing Ltd." in text
    assert "Dates: 2024-03-14." in text and "Amounts: GBP 450.00." in text


def test_the_prompt_reads_only_the_start_of_a_long_document():
    prompt = cards.build_prompt("big.pdf", "q" * (cards.MAX_CHARS * 3))
    assert prompt.split("Document:", 1)[1].strip() == "q" * cards.MAX_CHARS
    assert "File name: big.pdf" in prompt


# -- the store ------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def _document(store, name, text, *, mtime=1, ext="pdf", content_hash="h1"):
    file_id = store.upsert_file(path=f"/docs/{name}", size_bytes=1, mtime_ns=mtime,
                                ext=ext, content_hash=content_hash)
    store.replace_chunks(file_id, [{"ordinal": 0, "text": text}])
    store.mark_indexed(file_id)
    return file_id


def _waiting(store):
    return [file_id for batch in store.iter_card_candidates(
        cards.DOC_EXTS, cards.MAIL_EXTS, prompt_version=cards.PROMPT_VERSION,
        label=cards.CARD_LABEL) for file_id, _path, _hash in batch]


def _save(store, file_id, content_hash="h1"):
    card = cards.read_card(REPLY, LETTER)
    return store.save_doc_card(
        file_id, content_hash, outcome="made", fields=card.as_row(),
        text=cards.card_text(card), label=cards.CARD_LABEL, model="test",
        prompt_version=cards.PROMPT_VERSION)


def test_documents_wait_newest_first_and_code_never_does(store):
    old = _document(store, "old.pdf", LETTER, mtime=1)
    new = _document(store, "new.docx", LETTER, mtime=5, ext="docx")
    _document(store, "main.py", LETTER, mtime=9, ext="py")

    assert _waiting(store) == [new, old]


def test_a_card_is_a_searchable_passage_and_a_row(store):
    file_id = _document(store, "invoice.pdf", LETTER)
    chunk_id, removed = _save(store, file_id)

    assert removed == []
    found = store.conn.execute(
        "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH 'organisations'").fetchall()
    assert [int(r[0]) for r in found] == [chunk_id]
    row = store.doc_card(file_id)
    assert row["kind"] == "invoice" and row["people"] == ["Jordan Smith"]
    assert _waiting(store) == []


def test_a_changed_file_is_carded_again_and_its_old_card_goes(store):
    file_id = _document(store, "invoice.pdf", LETTER)
    first, _ = _save(store, file_id)
    store.upsert_file(path="/docs/invoice.pdf", size_bytes=2, mtime_ns=2, ext="pdf",
                      content_hash="h2")
    store.mark_indexed(file_id)                    # read again, with new words

    assert _waiting(store) == [file_id]
    second, removed = _save(store, file_id, content_hash="h2")
    assert removed == [first] and second is not None   # SQLite may reuse the id
    assert store.conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE file_id = ? AND label = ?",
        (file_id, cards.CARD_LABEL)).fetchone()[0] == 1


def test_a_file_read_again_loses_its_card_passage_and_is_offered_again(store):
    """`replace_chunks` keeps only the passages it is handed, so a re-read with
    the same hash drops the card while its row stays - found reading the code."""
    file_id = _document(store, "invoice.pdf", LETTER)
    _save(store, file_id)
    store.replace_chunks(file_id, [{"ordinal": 0, "text": LETTER}])

    assert _waiting(store) == [file_id]


def test_nothing_to_say_is_remembered(store):
    file_id = _document(store, "invoice.pdf", LETTER)
    store.save_doc_card(file_id, "h1", outcome="nothing", fields=None, text=None,
                        label=cards.CARD_LABEL, model="test",
                        prompt_version=cards.PROMPT_VERSION)
    assert _waiting(store) == []
    assert store.doc_card_counts() == {"made": 0, "nothing": 1}


def test_a_new_prompt_cards_everything_again(store):
    file_id = _document(store, "invoice.pdf", LETTER)
    _save(store, file_id)
    later = [f for batch in store.iter_card_candidates(
        cards.DOC_EXTS, cards.MAIL_EXTS, prompt_version=cards.PROMPT_VERSION + 1,
        label=cards.CARD_LABEL) for f, _p, _h in batch]
    assert later == [file_id]


def test_clearing_takes_every_card_and_its_passage(store):
    file_id = _document(store, "invoice.pdf", LETTER)
    chunk_id, _ = _save(store, file_id)

    rows, removed = store.clear_doc_cards(cards.CARD_LABEL)
    assert (rows, removed) == (1, [chunk_id])
    assert store.doc_card(file_id) is None and _waiting(store) == [file_id]


def test_a_card_is_written_from_the_document_not_from_another_card(store):
    file_id = _document(store, "invoice.pdf", LETTER)
    _save(store, file_id)
    text = store.card_source_text(file_id, cards.MAX_CHARS)
    assert cards.CARD_LABEL not in text and text.startswith("Invoice 1043")


def test_a_message_is_read_with_its_headers(store):
    file_id = _document(store, "msg", "Body of the note about the boiler.", ext="pst")
    store.set_message(file_id, subject="Boiler", sender="jordan@example.com",
                      recipients=json.dumps(["alex@example.com"]), sent_at=1710374400)
    text = store.card_source_text(file_id, cards.MAX_CHARS)
    assert text.splitlines()[:4] == ["From: jordan@example.com", "To: alex@example.com",
                                     "Date: 2024-03-14", "Subject: Boiler"]


# -- the run's pass ---------------------------------------------------------------------


class _Model:
    model = "stand-in"

    def __init__(self, reply=REPLY, fail=False):
        self.reply, self.fail, self.prompts = reply, fail, []

    def has_model(self):
        return True

    def generate(self, prompt, **_how):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("model gone")
        return SimpleNamespace(text=self.reply)


def _pipeline(store, model, *, enabled=True):
    from app.index import pipeline as module

    built = module.Pipeline.__new__(module.Pipeline)
    built.store = store
    built._stop = threading.Event()
    built._stop.set()
    built._interrupted = False
    built.governor = SimpleNamespace(
        wait_while_throttled=lambda should_stop: SimpleNamespace(action="go"))
    built._log = __import__("app.core.logging", fromlist=["logger"]).logger
    built._announce_phase = lambda stats, on_progress, phase: None
    built.embedded_at_end = 0

    def embed(stats, **_how):
        built.embedded_at_end += 1
    built._drain_unembedded = embed
    built._delete_chunk_vectors_once_committed = lambda ids: None
    built.config = SimpleNamespace(doc_cards_enabled=enabled,
                                   card_model=(lambda: model))
    return built


def _stats():
    from app.index.pipeline import IndexStats
    return IndexStats()


def test_the_pass_cards_each_document_and_embeds_the_cards(store):
    file_id = _document(store, "invoice.pdf", LETTER)
    short = _document(store, "note.txt", "Thanks!", ext="txt", mtime=3)
    model = _Model()
    stats = _stats()

    _pipeline(store, model)._drain_doc_cards(stats)

    assert stats.enrichment_counts["doc_cards"] == 1
    assert len(model.prompts) == 1                 # the short note never reached the model
    assert store.doc_card(file_id)["kind"] == "invoice"
    assert store.doc_card_counts() == {"made": 1, "nothing": 1}
    assert store.doc_card(short) is None


def test_the_pass_does_nothing_when_switched_off(store):
    _document(store, "invoice.pdf", LETTER)
    model = _Model()
    stats = _stats()

    _pipeline(store, model, enabled=False)._drain_doc_cards(stats)

    assert model.prompts == [] and stats.enrichment_counts["doc_cards"] == 0


def test_a_model_that_keeps_failing_stops_the_pass_and_marks_nothing(store):
    for n in range(6):
        _document(store, f"doc{n}.pdf", LETTER, mtime=n + 1)
    model = _Model(fail=True)
    stats = _stats()

    _pipeline(store, model)._drain_doc_cards(stats)

    from app.index.card_pass import CARD_FAILURES_STOP
    assert len(model.prompts) == CARD_FAILURES_STOP
    assert store.doc_card_counts() == {"made": 0, "nothing": 0}   # all still waiting


def test_a_stop_pressed_mid_pass_leaves_the_rest_for_next_time(store):
    for n in range(4):
        _document(store, f"doc{n}.pdf", LETTER, mtime=n + 1)
    model = _Model()
    pipeline = _pipeline(store, model)
    original = model.generate

    def stop_after_first(prompt, **how):
        pipeline._interrupted = True
        return original(prompt, **how)
    model.generate = stop_after_first

    pipeline._drain_doc_cards(_stats())

    assert store.doc_card_counts()["made"] == 1
    assert len(_waiting(store)) == 3


# -- Chat reads a card, never quotes it (2f) ----------------------------------------------


def _hit(store, file_id, chunk_id, text, label=""):
    from app.search.engine import SearchResult
    return SearchResult(chunk_id=chunk_id, file_id=file_id, path=f"/docs/{file_id}.pdf",
                        text=text, page=None, label=label, rank=1, score=1.0)


def test_a_card_hit_becomes_the_documents_own_passage(store):
    from app.chat.engine import ChatEngine

    file_id = _document(store, "invoice.pdf", LETTER)
    card_chunk, _ = _save(store, file_id)
    engine = ChatEngine.__new__(ChatEngine)
    engine.store = store

    swapped = engine._own_passages([_hit(store, file_id, card_chunk, "AI document card: ...",
                                         label=cards.CARD_LABEL)])

    assert len(swapped) == 1
    assert swapped[0].text == LETTER and swapped[0].label == ""


def test_a_card_hit_beside_a_passage_of_the_same_file_is_dropped(store):
    from app.chat.engine import ChatEngine

    file_id = _document(store, "invoice.pdf", LETTER)
    card_chunk, _ = _save(store, file_id)
    own = store.own_passage(file_id)
    engine = ChatEngine.__new__(ChatEngine)
    engine.store = store

    kept = engine._own_passages([_hit(store, file_id, own["id"], LETTER),
                                 _hit(store, file_id, card_chunk, "card", label=cards.CARD_LABEL)])
    assert [r.chunk_id for r in kept] == [own["id"]]


def test_the_model_is_told_what_a_source_is_and_the_stand_in_never_quotes_it(store):
    from app.chat import prompts
    from app.chat.context import Piece, Source, about_line
    from app.chat.engine import ChatEngine

    file_id = _document(store, "invoice.pdf", LETTER)
    _save(store, file_id)
    source = Source(n=1, file_id=file_id, path="/docs/invoice.pdf", name="invoice.pdf",
                    pieces=[Piece(chunk_id=1, text=LETTER)], passage=LETTER)
    engine = ChatEngine.__new__(ChatEngine)
    engine.store = store
    engine._add_about([source])

    assert source.about == "an invoice; dated 2024-03-14; involving Acme Plumbing Ltd"
    system = prompts.archive_system([source])
    assert prompts.CARD_MARK + " " + source.about in system
    read_back = prompts.parse_sources(system)[1]
    assert prompts.CARD_MARK not in read_back and read_back.endswith(LETTER.strip())
    assert about_line({"kind": "other"}) == ""
