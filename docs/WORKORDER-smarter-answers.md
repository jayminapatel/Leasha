# Work order (One thread): smarter answers - a stronger answerer, a card for every document, answers in Search, your own words, questions across documents

**Doc version:** 1.1 · **Updated:** 2026-10-12 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/chat/`, `app/index/`, `app/storage/`, `app/ui/widgets/`, `app/cli/`, `tests/unit/`, the docs)
**Status:** BUILT 2026-10-12, 23 of 24 (6b HELD until the final index); full suite owed - run combined with main by the Mail and Contacts thread (`test/combined`). RELEASED by the owner 2026-10-12 ("do all of them as the questions is the last once the full index is done, note this index is still test until we get it perfect then i will do the final index"), after asking "now we have all this data in our index is embedding etc enough to be smart what can we do so the system is more ai and smarter in its response and answers especially when we use natural language .. i want it to work like i am talking to an very smart system"

**What this is.** Embeddings find passages that sound like the question; they do not know what a
document *is*, who it involves or how two documents relate. Six levers, ranked in conversation on
2026-10-12 and all released: a stronger answering model chosen by measurement (1), a short card per
document written at indexing time (2), an answer in the Search box (3), the owner's own words (4),
questions across documents (5), and the owner's own question set (6), which is last because it is
written against the final index.

**What must not change.**
1. **The retrieval path never calls a model.** Search works with every model stopped. The answer
   card in Search (3) runs *after* the results are drawn, in its own worker, from `app/ui`; nothing
   under `app/search/` imports `app.chat` or `app.llm` (`test_chat_layering.py`, `test_translate.py`).
2. **No sentence without a receipt.** Every answer, the Search card included, goes through the
   existing verifier. A document card (2) helps *find* a passage; it is never itself the receipt for
   a claim about what a document says - the passage is.
3. **The floors are not lowered** (`WORKORDER-202626270611-chat-tab.md` 4b): citation validity >= 98%,
   aggregate exactness 100%, absence honesty 100%, extractive >= 85%.
4. **The index is a test index** (owner, 2026-10-12). Everything section 2 writes is re-runnable and
   removable: cards carry the model and prompt version they were made with, and a command clears them.

## Decisions (defaults the building thread may take)

- **D1 The answerer is chosen by a number.** Today's default (Qwen 2.5 1.5B inside Leasha) has never
  been scored; the 84.0% on record was `mistral` through Ollama (chat order, 2026-09-20 note). Score
  the default first, then the installed larger models. A model becomes a default only if it meets all
  four floors on the whole fixture; until one does, the measurements are recorded and the trade
  (quality against seconds per answer on this laptop) is put to the owner. Models not installed are
  not downloaded without the owner's yes.
- **D2 Cards are a labelled passage plus a table row.** The card's words go in as a passage labelled
  `Document card` (the photo descriptions' route, `add_caption_chunk`), so keyword and meaning search
  find it with no change to search; its fields (kind, people, organisations, dates, amounts, summary)
  go in `doc_cards`, keyed by file and `content_hash`, so a changed file gets a new card. AI-written
  text says so where a person reads it. On by default, at the end of a run, after the meaning
  catch-up, throttled like every backlog; one switch in Settings > Models.
- **D3 The answer card asks only questions.** Rules only (`route_question(text, llm=None)`) decide;
  a lookup, summary, comparison, timeline, count or "do I have" question gets a card, a word or a
  name does not. It starts after the person stops typing (or on Enter), never while Chat is answering,
  and is dropped the moment the box changes. "Continue in Chat" carries the question across. One
  switch in Settings > Search, on by default.
- **D4 Your words are a short list the owner owns.** "Mum" means "Jane Example"; "the house" means
  "12 Example Road". Added in Settings > Search or by telling Chat ("remember Mum is ..."), always
  shown back, removable. Applied to what Chat and the Search card search for, never silently to the
  plain search box.
- **D5 Compare and timeline are their own kinds.** A comparison retrieves for each side separately so
  both reach the model; a timeline puts dated passages in date order and the answer is a dated list.
  No floor at v1; measured on new fixture questions.
- **D6 The owner's question set is last.** The format and the command are built now; the questions
  are written by the owner against the final index.

## 1. A stronger answerer, chosen by measurement

- [x] **1a** Score today's default (ONNX, Qwen 2.5 1.5B) over the whole fixture:
  `venv\Scripts\python.exe -m app.cli evaluate --chat --json`. Record every measure and the timings here.
- [x] **1b** One question each (`--chat-ids L01`) for the installed larger models (`mistral`,
  `llama3`, `gpt-oss:20b`, `gemma4:26b`), to see what a question costs on this laptop; then the
  lookups (`L01-L27`) for the ones that answer in reasonable time. Record.
- [x] **1c** Act on the numbers (D1): a model that meets every floor on the whole fixture becomes the
  Thoughtful answerer when installed, its result file pinned as the regression the tests read; if none
  does, record it and put the trade to the owner.

> *2026-10-12, measured (section 1):* `evaluate --chat --json` at a26e1e0, real retrieval, whole fixture
> (96), Qwen 2.5 1.5B 4-bit inside Leasha, this laptop's processor: citation validity 100% (69 of 69),
> extractive 93.5%, aggregate exactness 100%, absence honesty 100%, synthesis 80%, traps 80%, refusal 66.7%;
> first answer text p50 5.9 s / p95 13.9 s, whole answer p50 6.9 s / p95 39.2 s / max 300.6 s (S02). **Clears
> every floor** - the Chat order's 4b. Wrong: L09, F06, T02, S04. Result pinned:
> `tests/fixtures/chat_eval_measured.json`, read by `test_chat_evaluate.py::test_the_measured_real_model_
> clears_every_floor_and_the_floors_are_not_lowered` (which also pins the floors). 1b, L01 alone: 1.5B 49.0 s
> (its first question, model load included; the rest ran 8-23 s), `llama3` 62.6 s, `mistral` 103.5 s (2 of 3
> sentences checked), `gpt-oss:20b` 115.2 s (0 of 1). `gemma4:26b` not run: ~18 GB and 12.5 GB free. 1c: the
> default already meets every floor and the larger models are 4-10x slower here, so nothing changes.

## 2. A card for every document, made at indexing time

- [x] **2a** Migration 40: `doc_cards` (file, content hash, kind, people, organisations, dates,
  amounts, summary, model, prompt version, made at, outcome).
- [x] **2b** `app/index/doc_cards.py` (pure): the prompt, a strict reading of the model's JSON (a
  fixed list of kinds, ISO dates, amounts with a currency, a summary of at most two sentences, anything
  else discarded), and the card's searchable text.
- [x] **2c** The end-of-run drain: indexed files with text and no card for their current hash, newest
  first, throttled, stoppable, resumable from the table alone; writes the row and the labelled
  passage, then embeds it in the same run. The card passage survives the keyword-only marking of
  spreadsheets and drawings, and a file re-read keeps no stale card.
- [x] **2d** The switch (`DOC_CARDS_ENABLED`, Settings > Models) and the run's progress words.
- [x] **2e** `leasha cards` - how many are made and waiting, show one file's card, make N now, clear
  them all (the test index, owner 2026-10-12).
- [x] **2f** Chat uses a source's card: its dates and kind are shown to the answering model beside the
  passage, never as a receipt.

> *2026-10-12, built:* migration 40 `_v40_cards_and_words`; `app/index/doc_cards.py` (prompt, `read_card`,
> `card_text`), `app/index/card_pass.py` (`run_cards`, shared by the run and the CLI), `Pipeline._drain_doc_cards`
> after the photo passes, `PHASE_DOC_CARDS`, `KIND_DOC_CARDS`; store `iter_card_candidates`, `card_source_text`,
> `save_doc_card`, `doc_card(s_for_files)`, `own_passage`, `clear_doc_cards`; `run_setup.card_model_factory` (chat
> model on the processor: its decode step is not under the graphics-card lock); `DOC_CARDS_ENABLED` (on) in the
> Chat box (`chat_box.ALWAYS_SHOWN`); `app/cli/cards.py`; Chat `_own_passages` and `_add_about`,
> `prompts.CARD_MARK`. Departure from D2: the knowledge-graph tables (v3) were meant to take the people and
> organisations; they are marked deprecated in `clear_index`, so the fields stay in `doc_cards` only.
> **Measured on the Chat fixture (46 documents):** 15 made, 16 too short or nothing usable, 0 failed;
> **20.8 s a card** on this laptop's processor. Read from the samples: two plain emails called "invoice",
> "me" as a person, a summary inventing "March 2022" - so a kind now needs supporting words in the text (else
> `email` / `other`), pronouns are never people, and a summary with a number the document lacks is dropped
> (4947346). At ~300,000 documents and mail on the owner's index that is about 70 days of the processor:
> **the owner's call, put to them 2026-10-12.** Not run on the owner's index (owner: "b", wait until it ships;
> it would pin the index at schema 40).

> *2026-10-12, later - decided by the owner:* "can you make this configurable", then "select only documents by
> default". `DOC_CARDS_SCOPE` (Settings > Models & AI > Chat > Write cards for): `documents` (default - files
> and mail attachments, not the messages), `mail`, `everything` (`doc_cards.scope_extensions`; an unknown value
> reads as documents). Documents on the owner's test index: about 80,000, so roughly 19 days the first time at
> 20.8 s a card (an estimate from file counts; short ones are skipped without the model).

## 3. Ask from the Search box

- [x] **3a** `app/ui/widgets/answer_card.py`, attached the way the person strip is: question detection
  by rules, its own timer and generation, the verified answer with numbered sources that open, and
  "Continue in Chat". `search_view.py` does not grow.
- [x] **3b** The switch (`SEARCH_ANSWERS`, Settings > Search).
- [x] **3c** A pytest-qt scenario types a question and sees the card; types a word and sees none.
- [x] **3d** A dated note on `docs/PARKED-IDEAS.md` section 4 recording the owner's ruling: the finder
  stays the headline; the answer is a card above its results.

> *2026-10-12, built:* `app/ui/presenter/answer.py` (`answer_wanted`: the router's rules, no model),
> `app/ui/widgets/answer_card.py` (`attach`, its own `answer_timer` and generation, `_answer` on a worker,
> skipped while `chat_ctl._ask` is set), placed in `search_bar.build_toolbar` after the person strip;
> `MainWindow._search_answers_on`, `_continue_in_chat`; `SEARCH_ANSWERS` in the "Answers and your words" box
> (`my_words_box.py`). `search_view.py` unchanged (249 lines). Tests: `test_answer_card_qt.py` (5).

## 4. Your own words

- [x] **4a** Migration 40 also: `my_words` (phrase, meaning, where it came from, when).
- [x] **4b** Expansion: a question naming a phrase searches for its meaning as well, in Chat and the
  Search card; the narration says so.
- [x] **4c** Chat learns and forgets: "remember Mum is Jane Example" saves and says it back;
  "forget Mum" removes it.
- [x] **4d** Settings > Search lists them, to add, change and remove (rule 11).

> *2026-10-12, built:* `app/chat/my_words.py` (`learn`, `apply`, `note_for_model`), `ChatEngine.
> _with_my_words`, `_words_turn`; store `my_words`, `set_my_word`, `remove_my_word` (kept through a Reset);
> `app/ui/widgets/my_words_box.py`, worker bodies `tasks.my_words_*`. Tests: `test_my_words.py`,
> `test_my_words_qt.py`.

## 5. Questions across documents

- [x] **5a** The router gains COMPARE and TIMELINE (rules first).
- [x] **5b** Compare: each side retrieved on its own, both guaranteed a source, the answer side by side
  with receipts.
- [x] **5c** Timeline: dated sources in date order (the card's date, else the file's), the answer a
  dated list, every line receipted.
- [x] **5d** Fixture questions for both, measured and recorded (no floor at v1).

> *2026-10-12, built and measured:* `router.cross_shape` - shapes rather than new classes, so a comparison
> is still a LOOKUP or SYNTHESIS answered and verified sentence by sentence (departure from 5a's wording, same
> intent); `ChatEngine._retrieve_sides`, `_both_sides_first`, `_in_date_order`; `prompts.SHAPE_NOTES`. 5d,
> real model on the fixture: "Compare the 2023 tenancy agreement with the 2024 tenancy agreement" - both
> agreements in the sources (after 4947346: each side keeps only the question's non-date filters), 88 s, one
> sentence survived the check; "Give me a timeline of the Leeds safety report" - final, draft and Dave's email
> in date order, 49 s, three lines kept; "Compare the two tenancy agreements" - 67 s, one source cited. The
> 1.5B model writes thin comparisons; no floor at v1. Tests: `test_cross_documents.py`.

## 6. Your own questions (HELD until the final index)

- [x] **6a** `evaluate --chat --questions FILE`: the owner's questions against the real index, one per
  line with the file the answer is in, scored for the right file cited and the fact present.
- [ ] **6b** HELD: the owner writes about fifty questions against the final index; they are scored and
  the result recorded here.

> *2026-10-12, built:* `app/chat/mine.py`, `evaluate.py::_run_owner_questions`; checked end to end on a
> scratch data folder. Tests: `test_owner_questions.py`.

## 7. Close

- [x] **7a** The user guide, the technical reference, the glossary, `CHANGELOG.md`, `HANDOFF.md` and
  the register.

> *2026-10-12, built:* the user guide (Ask a question; Chat; What gets read; Settings), the technical
> reference, the glossary, `CHANGELOG.md`, `HANDOFF.md`, the register and a dated note on `docs/PARKED-IDEAS.md`.
> **Also done the same day, on the owner's word ("turn svg and epub off now and as default"):** `.svg` and
> `.epub` ship off, and the owner's `D:\LeashaIndex\Data\extractors.toml` holds both off. **Found doing it and
> fixed:** the walk read the packaged `extractors.toml` alone, so Settings > File types never reached an index
> run (`WalkConfig.formats_from`, `test_walker.py`). The scan's counts still read the packaged file only.
