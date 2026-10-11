# Work order (One thread): smarter answers - a stronger answerer, a card for every document, answers in Search, your own words, questions across documents

**Doc version:** 1.0 · **Updated:** 2026-10-12 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/chat/`, `app/index/`, `app/storage/`, `app/ui/widgets/`, `app/cli/`, `tests/unit/`, the docs)
**Status:** RELEASED by the owner 2026-10-12 ("do all of them as the questions is the last once the full index is done, note this index is still test until we get it perfect then i will do the final index"), after asking "now we have all this data in our index is embedding etc enough to be smart what can we do so the system is more ai and smarter in its response and answers especially when we use natural language .. i want it to work like i am talking to an very smart system"

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

- [ ] **1a** Score today's default (ONNX, Qwen 2.5 1.5B) over the whole fixture:
  `venv\Scripts\python.exe -m app.cli evaluate --chat --json`. Record every measure and the timings here.
- [ ] **1b** One question each (`--chat-ids L01`) for the installed larger models (`mistral`,
  `llama3`, `gpt-oss:20b`, `gemma4:26b`), to see what a question costs on this laptop; then the
  lookups (`L01-L27`) for the ones that answer in reasonable time. Record.
- [ ] **1c** Act on the numbers (D1): a model that meets every floor on the whole fixture becomes the
  Thoughtful answerer when installed, its result file pinned as the regression the tests read; if none
  does, record it and put the trade to the owner.

## 2. A card for every document, made at indexing time

- [ ] **2a** Migration 40: `doc_cards` (file, content hash, kind, people, organisations, dates,
  amounts, summary, model, prompt version, made at, outcome).
- [ ] **2b** `app/index/doc_cards.py` (pure): the prompt, a strict reading of the model's JSON (a
  fixed list of kinds, ISO dates, amounts with a currency, a summary of at most two sentences, anything
  else discarded), and the card's searchable text.
- [ ] **2c** The end-of-run drain: indexed files with text and no card for their current hash, newest
  first, throttled, stoppable, resumable from the table alone; writes the row and the labelled
  passage, then embeds it in the same run. The card passage survives the keyword-only marking of
  spreadsheets and drawings, and a file re-read keeps no stale card.
- [ ] **2d** The switch (`DOC_CARDS_ENABLED`, Settings > Models) and the run's progress words.
- [ ] **2e** `leasha cards` - how many are made and waiting, show one file's card, make N now, clear
  them all (the test index, owner 2026-10-12).
- [ ] **2f** Chat uses a source's card: its dates and kind are shown to the answering model beside the
  passage, never as a receipt.

## 3. Ask from the Search box

- [ ] **3a** `app/ui/widgets/answer_card.py`, attached the way the person strip is: question detection
  by rules, its own timer and generation, the verified answer with numbered sources that open, and
  "Continue in Chat". `search_view.py` does not grow.
- [ ] **3b** The switch (`SEARCH_ANSWERS`, Settings > Search).
- [ ] **3c** A pytest-qt scenario types a question and sees the card; types a word and sees none.
- [ ] **3d** A dated note on `docs/PARKED-IDEAS.md` section 4 recording the owner's ruling: the finder
  stays the headline; the answer is a card above its results.

## 4. Your own words

- [ ] **4a** Migration 40 also: `my_words` (phrase, meaning, where it came from, when).
- [ ] **4b** Expansion: a question naming a phrase searches for its meaning as well, in Chat and the
  Search card; the narration says so.
- [ ] **4c** Chat learns and forgets: "remember Mum is Jane Example" saves and says it back;
  "forget Mum" removes it.
- [ ] **4d** Settings > Search lists them, to add, change and remove (rule 11).

## 5. Questions across documents

- [ ] **5a** The router gains COMPARE and TIMELINE (rules first).
- [ ] **5b** Compare: each side retrieved on its own, both guaranteed a source, the answer side by side
  with receipts.
- [ ] **5c** Timeline: dated sources in date order (the card's date, else the file's), the answer a
  dated list, every line receipted.
- [ ] **5d** Fixture questions for both, measured and recorded (no floor at v1).

## 6. Your own questions (HELD until the final index)

- [ ] **6a** `evaluate --chat --questions FILE`: the owner's questions against the real index, one per
  line with the file the answer is in, scored for the right file cited and the fact present.
- [ ] **6b** HELD: the owner writes about fifty questions against the final index; they are scored and
  the result recorded here.

## 7. Close

- [ ] **7a** The user guide, the technical reference, the glossary, `CHANGELOG.md`, `HANDOFF.md` and
  the register.
