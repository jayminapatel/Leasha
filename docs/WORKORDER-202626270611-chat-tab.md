# Work order (One thread): the Chat tab — ask your archive, and every answer has receipts

**Doc version:** 1.4 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3
**Thread:** One thread (new tab + Search/LLM layers + eval harness)

> **Dated note, 2026-09-20 - the Status line below is corrected; it said HELD and
> the order is RELEASED.** The Status line was written on 2026-08-28 when the owner
> asked for the order to be held for him to schedule. On 2026-09-19 the owner
> instructed the build thread to add the Chat tab ("can you also add the chat page
> which is outstanding workorder", then "actually do all the workorders you can
> then test") and it was built and merged that day; `ORDER_REGISTER.md` has carried it
> as RELEASED since. The line and the register disagreed for a day. **Decision, taken
> by the lead on the owner's behalf on 2026-09-20: the Chat tab stands as RELEASED.**
> The "deliberate refusal of chat over the index" in
> `WORKORDER-scope-change-search-and-chat.md` is reversed for local, receipt-backed
> answers only (a dated confirming note is on that order). Nothing below this note
> was reworded except that one Status line; its original words are kept here so the
> history is not lost: "HELD - created at the owner's request, to be scheduled BY THE
> OWNER later. Do not execute until he promotes it." The prerequisites it listed (the
> search-experience order's translator seam and policy machinery landed) held when
> the tab was built.

**Status: RELEASED** — built and merged 2026-09-19 on the owner's instruction;
the owner's reopening of the search-and-chat scope decision is recorded as a dated
note on `WORKORDER-scope-change-search-and-chat.md`. Open items are listed in the
dated notes below, section by section.

> **Dated note, 2026-09-20 (later) - the owner changed what the Chat tab is: a conversation
> that reads his own files first.** His words, in order: "the chat has to behave like i am
> talking to ai chat like in claude"; then "the chat should use local source though"; then
> "and optionally can augment from web". This note is the decision and the list of what it
> supersedes. **Nothing below it is reworded**: each superseded item has its own dated note
> above it, marked SUPERSEDED IN PART, and keeps its words so the history reads.
>
> **What the tab is now.**
> 1. *Retrieval first.* Every substantive question - including a general-sounding one
>    ("what is a PST file?") - searches the local index before a word is said. Only social
>    or meta turns (hello, thanks, "what can you do?"), instructions about the previous
>    answer ("shorter", "translate that", "why?", "continue"), and writing / maths / code
>    tasks that name none of his files skip it: router class `CHAT`
>    (`app/chat/router.py`; the router model is never asked to choose it, so a small model
>    cannot talk the tab out of searching). This **supersedes** the first reading of the
>    owner's request ("a plain question must not go searching").
> 2. *Real conversation.* The model is sent the conversation as role-tagged messages through
>    Ollama's `/api/chat` (`app/chat/llm.py`, `chat_stream`), streamed as it is written, with a
>    sliding memory sized from the model's real context window
>    (`app/chat/memory.py`: newest turns whole, older ones dropped whole-turn first, the turn
>    at the edge cut at a sentence, what was dropped kept as a one-line-per-turn digest).
>    One persona, in one place (`app/chat/prompts.py`, `PERSONA`), plus his own note through
>    the setting `CHAT_STYLE_NOTE`.
> 3. *Answers from his files read like an assistant wrote them*: flowing prose, numbered
>    citations to the Sources pane, written once and streamed; when it is complete every
>    sentence is judged against the retrieved passages (`app/chat/reconcile.py`). A sentence
>    no passage supports is taken out - **not the whole answer** - a sentence padded past what
>    a passage says is cut back to the clause it does say, the rest is renumbered, and the
>    answer ends "I could not confirm the rest of that from your files." if anything went. If
>    nothing survives, the turn is the plain "I couldn't find that in your files."
> 4. *Nothing found.* One plain sentence, then - for a question that is not about his own
>    affairs (no "my", "we", "I") and only then - a short answer from general knowledge under
>    the label "Not from your files:". A question about his own affairs gets the
>    searched-and-found-nothing account instead: a general answer to it would be a guess about
>    his life.
> 5. *Fully local.* All inference is Ollama on this computer. `tests/unit/test_chat_conversation.py`
>    runs whole conversations under a patched `socket.create_connection` /
>    `getaddrinfo` that fails the test on any host but loopback (once with the fake model, once
>    through `OllamaLLM` with a recording transport); the Settings text of the three chat model
>    settings now says they run on this computer.
> 6. *The web is optional, off by default, and chat-only* (the owner's explicit exception to the
>    offline rule; search, indexing and everything else stay offline). See the dated note above
>    section 3e for the design and its guards.
>
> **What this supersedes, item by item** (each has its own note): design principle 1 ("The
> model never speaks unverified") - claims about the files are still checked, but *after* they
> are written, so unchecked words are on screen while they stream and the kept text is the
> checked text; 1c's "per-document extract-then-combine (map-reduce)" - one streamed answer,
> checked by sentence; 2a's "if the answer thins too far the loop retries once with tighter
> instructions" - no retry, a partial answer instead; the section 2 heading's "no sentence
> without a receipt" and "Unverifiable prose never renders" - an unmarked sentence that makes no
> claim about the files (conversation, a labelled general paragraph) is allowed, and an unmarked
> sentence with an invented figure, name, month or quotation is dropped (`audit_answer` re-checks
> the finished turn; a **limit, stated plainly**: an unmarked, vague, general-sounding claim about
> the files that shares little with any passage is treated as conversation and kept - a lexical
> check cannot see it); 3a's "streaming tokens" is now literally so (before, a verified sentence
> at a time); 4b's `synthesis_combine` "off" - it is **on** and now means "a question across
> several documents is answered as one account" (off asks for one short paragraph per document);
> the tab's fixed strings (see the note above section 3).

> **Dated note, 2026-09-19 - what was built and merged, checked against the code
> and the tests.** The Chat engine (`app/chat/`), the tab (`app/ui/chat_view.py`,
> `app/ui/chat_sessions.py`, `app/ui/widgets/chat_*.py`,
> `app/ui/controllers/chat_controller.py`), the schema-26 `chat_sessions` table
> and `evaluate --chat` are on main. Items below are ticked only where the code
> exists, a named test proves it and that test passes; every other item is left
> open with its reason in the note above its section. This note does not reword
> any item and does not change the HELD status line beneath it.
> Verification run, 2026-09-19: the twelve `tests/unit/test_chat_*.py` files gave
> 401 passed, 1 skipped (the real-model check, which needs
> `LEASHA_CHAT_REAL_MODEL`). Two of five full runs of `test_chat_tab_qt.py`
> also showed an error at teardown of
> `test_the_chat_settings_group_builds_one_control_per_declared_setting` (a Qt
> event-loop `TypeError: () missing 1 required positional argument: 's'`); that
> test passes on its own and the cause was not found. Treat it as a flaky
> teardown, not a clean pass.
> **Known gaps, in one place:** (1) the tab's preview is a passage strip under the
> Sources list, not the search page's full `PreviewPane`; (2) the Fast /
> Thoughtful toggle is remembered and handed to `ChatEngine.ask` as `style`, but
> `ask` has no such parameter and `supported_kwargs` drops it, so the toggle does
> not change the model (`suggest_modes` and `set_model` exist in the engine and
> nothing in `app/ui/` calls them); (3) for the same reason the shelf's `scope`
> (pin / remove / add) never reaches the real engine - the tab tests use
> `FakeChatEngine`, which accepts it, and `ChatEngine.set_shelf` is called only
> from its own tests; (4) there is no Chat golden image
> (`tests/golden/ui-redesign/*/` holds `search-home.png` and `search-results.png`
> only); (5) extractive quality with real models is below the 85% floor - see the
> note above section 4.

> **Dated note, 2026-09-20 - SUPERSEDED IN PART: principle 1 below.** "The model never speaks
> unverified" now holds for **claims about the person's files** only, and is enforced *after* the
> words are written, not before: the words stream as they are generated and the kept text is the
> checked text (see the decision note at the top). Conversation that makes no claim about the files
> and a labelled general answer are not verified, because they claim nothing about them. Principles
> 2, 3 and 4 stand; 4 gained a labelled general answer, after the plain sentence, for a question
> that is not about his own affairs.

## Why this design succeeds where naive RAG disappoints

Every "chat with your documents" product fails the same way: stuff retrieved
text into a prompt, hope the model behaves, ship the hallucinations. This
design refuses that shape on four principles, each already proven in this
codebase:

1. **The model never speaks unverified.** The translator precedent
   (quarantined LLM, output validated by the parser, shown for correction)
   applied to answers: the model proposes, the system verifies against the
   index, only verified content reaches the screen.
2. **The index answers what the index can answer.** Counting, listing and
   filtering questions are DATABASE questions — answered exactly by queries,
   with the model only phrasing. A model is never asked to count.
3. **Retrieval is the accuracy ceiling, so retrieval leads.** The chat is a
   search agent that narrates its searching — not a context-window gambler.
4. **Absence is stated honestly.** "I searched these ways and found nothing
   in the index" is a first-class answer — never "that doesn't exist."

> **Dated note, 2026-09-20 - SUPERSEDED IN PART: 1c's "per-document extract-then-combine
> (map-reduce) for synthesis instead of one giant prompt".** A question across several documents is
> answered as one flowing account, written once and streamed, then checked sentence by sentence
> (`synthesis_combine` is on and means that; off asks for one short paragraph per document). The
> rest of 1c stands: the context budget still comes from the model's real window, now shared with the
> conversation (the numbered passages get 55% of it, the conversation the rest). 1a gained a class,
> `CHAT` - no retrieval - and the router model is never asked to choose it.

## 1. The engine: an agentic retrieval loop (the core)

- [x] **1a Question router** (rules first, model assist second — the
  translator pattern): classifies each question into LOOKUP (extract from
  documents), AGGREGATE (count/list/filter — the index answers), SYNTHESIS
  (across documents), FIND (really a search — return results, not prose),
  ABSENCE ("do I have…"), and FOLLOW-UP (context-dependent). The router is
  Qt-free, testable, and its decision is visible in the debug pane.
- [x] **1b The loop**: plan search queries from the question (REUSING the
  translator — question → filter grammar + terms); retrieve via the real
  engine (all lanes: text, and when 0510+ land, images/people/places);
  assess sufficiency; the model may request FURTHER searches (bounded, ≤3
  rounds) when context is thin — a small model in a tight tool loop beats a
  small model with a stuffed window. Each step streams a plain-words
  narration line ("Searching… found 8 documents · reading the 2019
  contract…") — the loop's own progress is the trust-building UX.
- [x] **1c Context economy for small models**: per-document extract-then-
  combine (map-reduce) for synthesis instead of one giant prompt; the
  RERANK_WINDOW machinery reused for snippet windows; context budget derived
  from the model's actual window (queried from Ollama), envelope-style.
- [x] **1d AGGREGATE answers are computed, not generated**: "how many PDFs
  did Dave send in 2019" runs as a real query; the number in the answer IS
  the query result, injected — the model may only phrase around values it
  cannot alter (template slots, not free generation over numbers).
- [x] **1e ABSENCE protocol**: retrieval-negative questions answer with what
  was searched (the queries, visibly), the honest scope sentence ("nothing
  in Leasha's index matches — sources currently indexed: …"), and offer the
  obvious next steps (different words; is it on an unscanned drive?).

> **Dated note, 2026-09-20 - SUPERSEDED IN PART: this section's heading ("no sentence without a
> receipt") and 2a's "the sentence is dropped and ... the loop retries once with tighter
> instructions" / "Unverifiable prose never renders".** A sentence with a marker `[n]` is still
> checked against the passage it cites (2a-2c unchanged: words found together, negation agrees, every
> figure, date, name and quotation exists, quotes verbatim - `app/chat/verify.py`) and is dropped if it
> fails; but there is **no retry** - a partial answer is kept instead and says what it could not
> confirm - and unverified words are on screen while they stream (the finished turn replaces them). A
> sentence with **no** marker is judged too (`app/chat/reconcile.py`): if it says what a passage says it
> is attached to it (or dropped if it contradicts it), if it states a figure, name, month or quotation
> that no passage has it is dropped, and otherwise it is conversation and stays. A sentence padded past
> what its passage says is cut back to the clause that is supported. `audit_answer` re-checks a finished
> turn independently; `audit_turn` remains the strict check for the no-model extract-and-quote fallback.
> **Limit, stated plainly:** an unmarked, vague, general-sounding claim about the files that shares less
> than half its words with any passage is treated as conversation and kept - a lexical check cannot see it.

## 2. Verification: no sentence without a receipt

- [x] **2a** generation carries inline source markers; **post-generation
  validation** checks every sentence's marker points at a retrieved chunk
  AND the sentence is supported by it (cheap entailment proxy: embedding
  similarity between sentence and cited span above a tuned threshold; below
  threshold → the sentence is dropped and, if the answer thins too far, the
  loop retries once with tighter instructions). Unverifiable prose never
  renders. This is the load-bearing mechanism of the whole order.
- [x] **2b** quotes are VERBATIM by construction: quoted spans are copied
  from the chunk by the system, not re-generated by the model (the model
  selects offsets; the system prints the text). A quote in chat can never
  misquote.
- [x] **2c** dates, numbers, names appearing in answers are cross-checked
  against the cited chunks' text (exact/fuzzy presence); a fabricated
  number dies in validation.

> **Dated note, 2026-09-19 - section 3.** 3b is ticked
> (`test_chat_tab_qt.py::test_a_find_answer_shows_the_real_results_and_ends_with_thats_all`).
> **3a is left open:** bubbles, streaming, Stop, Enter / Shift+Enter, the Sources
> pane on the search results list, click-to-open and the hover-shows-the-passage
> behaviour are built and tested
> (`test_ask_shows_the_narration_then_streams_text_then_receipts_open_the_source`,
> `test_hovering_a_number_shows_its_passage_without_picking_it`,
> `test_enter_sends_and_shift_enter_starts_a_new_line`,
> `test_stop_mid_stream_keeps_what_arrived_and_says_it_stopped`); but the
> right-click menu comes from `ResultsView`, and the chat view connects only its
> open and reveal signals - pin, re-index and find-similar have no listener in the
> chat view - and no test presses right-click. The passage is shown in a strip
> under the list, not highlighted inside a preview.
> **3c is left open:** chips, pin, remove and drag-to-add work and are saved
> (`test_pin_remove_and_add_change_what_the_next_question_searches`,
> `test_the_shelf_change_is_saved`), and the engine does search the documents
> earlier answers touched first
> (`test_chat_engine.py::test_a_follow_up_is_answered_from_the_documents_already_touched`);
> but the user's pin / remove / add state is not passed to the real engine (gap 3
> above), so "what the model can see is exactly what the shelf shows" is not yet
> true with the real engine.
> **3d is left open:** sessions persist, list, rename, delete and reopen with
> their shelf (`test_chat_sessions.py`,
> `test_a_conversation_is_saved_and_comes_back_with_its_shelf_and_sources`,
> `test_new_rename_and_delete`) and the Ollama-absent page degrades to plain words
> (`test_without_the_helper_the_page_says_so_in_plain_words_and_search_still_works`);
> the Fast / Thoughtful mapping to installed models is not wired (gap 2).
> **3e is left open:** the plain-words and no-badge rules are tested
> (`test_chat_tab.py::test_no_fixed_string_in_the_tab_is_a_warning_banner`,
> `test_the_fixed_words_are_plain_english`) and `CHAT_MAX_ROUNDS`,
> `CHAT_VERIFY_STRICTNESS` and the three model keys are declared in
> `app/core/settings_registry.py` and read through `app/chat/config.py`
> (`test_chat_layering.py::test_every_chat_tunable_is_declared_where_the_ui_will_find_it`);
> but the Chat group on Settings > Models is always visible, not "invisible
> outside Manual" - the Manual mode belongs to the Index Tuning screen and Chat is
> not in `app/core/envelope.py`.

> **Dated note, 2026-09-19, later the same day - the wiring gaps above are closed,
> and 3a, 3c, 3d and the section-5 UI-scenarios item are ticked.** The earlier note
> listed features built on one side and never connected. Connected now, each with a
> test against the **real** `ChatEngine`, real store and real search (only the
> language model is a stand-in) in `tests/unit/test_chat_wiring.py`, and against the
> real window in `tests/unit/test_chat_wiring_qt.py`:
>
> * **Pins.** `ChatEngine.ask` takes `scope=` (the pinned documents' paths). They are
>   looked at first for *every* question - not only follow-ups - and when they do not
>   answer, the corpus is searched as before
>   (`test_a_pinned_document_is_looked_at_first_for_a_question_that_is_not_a_follow_up`,
>   `test_a_pin_that_does_not_answer_falls_back_to_the_corpus`). Scope belongs to one
>   question and does not leak into the next.
> * **Removal.** `removed=` - a document taken off the shelf is never a source in any
>   round (`test_a_document_taken_off_the_shelf_is_never_used_as_a_source`; removing
>   and pinning the same one leaves it out). The tab passes both, snapshotted on the
>   window thread and resolved to file ids on the worker.
> * **Fast / Thoughtful.** `style=` chooses the answering model from what is installed
>   (`test_fast_and_thoughtful_choose_different_models_when_two_are_installed`). **An
>   explicit `CHAT_MODEL` wins over it, and the turn's debug says so**
>   (`test_an_explicit_chat_model_wins_and_the_turn_says_so`). When the control would
>   change nothing - one model installed, or `CHAT_MODEL` set - a plain line beside it
>   says so instead of leaving a control that does nothing (`speed_note`).
> * **The Sources pane's right-click menu.** Pin, re-index and "More like this" now
>   act: pin puts the document on the shelf pinned; re-index starts indexing that
>   document's folder; "More like this" opens the Search page with the results. The
>   test right-clicks the pane's own list and invokes the actions it builds
>   (`test_the_real_right_click_menu_of_a_source_pins_re_indexes_and_finds_similar`).
>   A source carrying no passage id has its file's first passage looked up on a
>   worker first.
>
> **Still open, on purpose.** 3e (chat behaviours as envelope tunables, invisible
> outside Manual): the Chat group on Settings is always visible and Chat is not in
> `app/core/envelope.py`. 4b and 4c as in the note above. 4d: there is still no roles
> grid, no Describe role and no RAM line. **Not verified with a live model:** the
> Fast / Thoughtful mapping is tested against stand-in clients, not a running Ollama
> (UNCONFIRMED against real models). The passage is shown in a strip under the
> Sources list, not highlighted inside the full `PreviewPane`, which is why 3a's
> "highlights the exact passage in the pane" is read as that strip.

> **Dated note, 2026-09-20 - SUPERSEDED IN PART: 3a's "clean conversation bubbles, streaming
> tokens"; the tab as it is now, and the words that changed.** The assistant's words sit on the
> page with no box round them; the person's are a soft block on the right. A slim "Thinking..."
> shows before the first word and goes when it arrives. Tokens appear as they are generated
> (before, a verified sentence at a time). Stop always ends a turn: the bubble closes at once
> with what had arrived, and if the engine - still loading a model, so with nothing to stop
> between - has not come back within four seconds the box is handed back, the orphaned worker's
> late words are ignored, and the next question gets a fresh engine. The view follows the newest
> text only while the reader is at the bottom; Enter sends, Shift+Enter starts a new line, and
> the box grows with the *wrapped* text, up to five lines. **Markdown is drawn as it streams**
> (`app/ui/widgets/chat_markdown.py`): bold, italic, headings, bullet and numbered lists,
> tables, inline code and fenced code blocks with a Copy button on each. **Copy** is on every
> answer (the words, without the source numbers), **Regenerate** on the last (a little warmer
> each time, so the answer differs), **Edit** on the last message (it comes back into the box and
> the exchange is taken out), **Try again** beside a reply that stopped part-way or failed (a
> reply that dies mid-stream keeps its words, and says why in one plain sentence with the fix),
> **New chat** in the sidebar, and a **short title** from the fast model after the first answer
> (the first words of the question stand in until it arrives; asked once; a rename by the person
> wins). Saved conversations keep the new fields (`model`, `partial`, `auto_titled`, `web`).
> **Strings reworded by the owner's instruction** (old -> new): the box's placeholder "Ask about
> your files and mail. Enter sends; Shift+Enter starts a new line." -> "Ask about your files, or
> just talk. Enter sends; Shift+Enter starts a new line."; the empty heading "Ask about anything
> you have kept." -> "Ask about your files, or just talk." (its hint gained "What is a PST
> file?"); the Sources pane heading "Sources" -> "Local sources", and its empty line "Sources
> appear here as the answer uses them." -> "Passages from your files appear here as the answer
> uses them." (the owner asked that the pane be visibly the local sources; a web page in it is
> marked Web and opens in the browser).
> **A bug found by looking at the real window, not by a test:** `ChatSessions` called the
> store's `save_session` with one dict, but the store's method is
> `save_session(session_id, title, turns, shelf, model)`, so every save raised `TypeError` and
> **no conversation was ever kept outside the tests' in-memory double**. Fixed in
> `app/ui/chat_sessions.py` (a translation both ways: the tab's own id, its flags and the shelf's
> removed list ride in a `_meta` first entry of the stored turns) and pinned against the real
> `SqliteStore` in `tests/unit/test_chat_sessions_store.py`, which is the test that was missing.
> Also found and fixed: `test_the_settings_page_carries_the_chat_group_and_forwards_its_changes`
> could hang the whole run - a settings write raced the file on Windows, the error opened a modal
> dialog, and nothing clicked it. The Chat tests' harness now records errors and captures `.env`
> writes instead (`tests/unit/test_chat_tab_qt.py::chat`).

## 3. The tab (owner: NO badges — receipts are integrated, not stigmata)

- [x] **3a** a Chat tab: clean conversation bubbles, streaming tokens, Stop
  button, Enter sends / Shift+Enter newline. No "AI-generated, verify!"
  banners anywhere — the honesty lives in the *architecture*: superscript
  source numbers in the prose, a **Sources pane** alongside listing the
  cited documents as the same result cards search uses — click opens,
  right-click gives the full result menu, hover on a superscript highlights
  the exact passage in the pane. The receipt IS the interface.
- [x] **3b Answers can BE result sets**: FIND-routed questions ("show me the
  photos of the kids at the beach in 2015") answer with an inline result
  grid/rows — the chat drives every search lane conversationally and shows
  real results, not prose about results. Mixed answers (a sentence + the
  seven documents) are the norm, not the exception.
- [x] **3c The context shelf**: documents the conversation has touched
  accumulate as visible chips (the pinned-working-set pattern); the user
  can pin (keep in scope), remove (out of scope), or add (drag a result
  in). Follow-ups search the shelf first, the corpus second. What the model
  can see is always exactly what the shelf shows — no hidden context.
- [x] **3d Sessions**: conversations persist locally (the index-sensitivity
  sentence extends to chat logs), listed in a sidebar, deletable; a session
  reopens with its shelf intact. Plain-words model control ("Fast /
  Thoughtful" mapped to installed Ollama models; greyed-with-reason when
  Ollama is absent — the whole tab degrades to a plain explanation of what
  to install, H4 register).
> **Dated note, 2026-09-20 - 3e (chat tunables as envelope settings), and the web.**
> **3e.** Every chat tunable is declared once in `app/core/settings_registry.py`, read into
> `Settings.chat_*` by `app/core/config.py`, resolved in `app/chat/config.py`
> (`ChatSettings.from_settings`) and given a control on Settings > Models & AI
> (`app/ui/widgets/chat_box.py`, built from the registry). They follow the Index Tuning mode:
> in Defaults and Auto-tune the envelope decides (`app/core/envelope.py`: `chat_max_rounds`,
> `chat_verify_strictness`, and the new `chat_context_tokens` - 4096 below 8GB of RAM, else
> 8192, **an estimate, flagged as one**: measured only on a 34GB CPU-only machine) and nothing
> is forced onto a model role; in Manual what was typed counts, clamped to the envelope; what was
> typed in Manual is kept and inert elsewhere. New: `CHAT_CONTEXT_TOKENS` (how much the model
> reads at once) and `CHAT_STYLE_NOTE` (the person's own words, added to the persona - never
> replacing it). The help text of `CHAT_MODEL`, `CHAT_ROUTER_MODEL` and `CHAT_PLANNER_MODEL` gained
> "It runs on this computer" (the owner asked for it).
> **The web** (owner: "and optionally can augment from web" - an explicit exception to the offline
> rule, for chat only; search, indexing and everything else stay offline). Settings: `CHAT_WEB_ENABLED`
> (off), `CHAT_WEB_PROVIDER` (auto | duckduckgo | wikipedia | searxng | brave), `CHAT_WEB_ASK_FIRST`
> (on), `CHAT_WEB_SHOW_QUERY` (on), `CHAT_WEB_SEARXNG_URL`, `CHAT_WEB_BRAVE_KEY` (a field the owner
> fills, masked on screen; Leasha never fills it in). **These are always visible, in every tuning
> mode** - a privacy choice, not a tuning - in words that say what leaves the computer. In the tab:
> a **Web** chip beside the box, per conversation, remembered with it, absent unless Settings allows
> the web, off until turned on. Design (`app/chat/web.py`; the engine imports it inside the one
> function that needs it, `test_chat_layering.py::test_the_web_module_is_only_ever_imported_lazily_by_the_engine`):
> *local first* - the files are searched, then the web adds what they do not say, or answers when they
> have nothing, and is never used for a question about his own affairs (my / our / we / I); the answer
> keeps "From your files" and "From the web:" visibly apart, web passages are numbered after the
> local ones, marked Web in the Sources pane and opened in the browser. **What leaves the computer is
> one keyword phrase** (at most ten words), composed by the local model from the question alone and
> scrubbed of paths, e-mail addresses, file names, long digit runs and every file name, folder and
> passage the search retrieved; it is **shown before it is sent**, and sent only after Allow unless
> the person turned "Ask before each web search" off (with no way to ask, an ask-first search is
> skipped, not sent). Result pages are read as text only (the top three, 1 MB each, no script run, at
> most three redirects, loopback and private addresses refused). A claim from the web is cited to the
> web passage and checked against it; a claim about his files still needs a local passage. If the web
> fails the answer says so in one sentence and stands on the files; it never blocks the answer.
> **Provider - called live from this machine on 2026-09-20:** Wikipedia's API answers (0.9 s at first,
> 4-22 s minutes later on a busy network); DuckDuckGo's HTML, lite and instant-answer endpoints all
> answered HTTP 202 with an anti-bot challenge and were not bypassed; Mojeek, Marginalia and Brave's
> HTML search were bot-blocked. **So the one keyless provider that works is Wikipedia** (encyclopedic
> questions only); a general web search needs his own SearXNG address or a Brave key, and the
> DuckDuckGo, SearXNG and Brave parsers were built from their documented shapes and **not seen live**.
> Guards: `tests/unit/test_chat_web.py` (the module, including a socket-level guard) and
> `tests/unit/test_chat_web_engine.py` - off means off; the files are searched before the web is
> asked; **nothing planted in the local documents (a file name, a path, a passage, a name, a figure)
> appears in any outgoing URL, parameter or header, including when the model tries to smuggle it
> into the phrase**; ask-first really blocks until Allow; Skip and Stop send nothing.

> **Dated note, 2026-09-20 (later still) - the web is asked only when the files come up thin;
> this CORRECTS the flow in the note above.** The defect: with Web on, a question the files could
> already answer still showed "Ask before each web search" (the engine consulted the web after every
> retrieval). The owner's requirement is "the chat should use local source though" and "optionally
> can augment from web", so the order of a turn with Web on is now: (a) local retrieval and the
> assessment, always first; (b) `app/chat/webrule.py` (pure text and numbers, unit-tested as a table in
> `tests/unit/test_chat_webrule.py`) decides from a rule, not a vibe: **ask** when the person's own
> words ask for it ("search the web", "google", "online"), when no passage supports the question (none
> retrieved, or the answer stage found nothing usable), when the assessment was thin, or when the
> files cover under 80% of the question's words *and* it is about the present ("latest", "current",
> "news", "today", "price"...) or asks what / who an outside thing is; **never** for a question about
> the person's own affairs, and **never** when the files answered; (c) only then the prompt (if "Ask
> before each web search" is on) and the search; (d) the answer keeps "from your files" and "From the
> web:" apart and web sources are marked Web in the Sources pane. The person is asked at most once per
> turn. The instruction to use the web ("search the web too") is stripped from the phrase that is sent.
> Tests: `test_chat_web_flow.py` (files answered -> no prompt and no non-loopback connection; thin ->
> prompt, then only a clean phrase after Allow; Skip -> the plain one-liner; asked -> prompt though the
> files answered), `test_chat_webrule.py`. The word "may" in the Settings help ("may then search the web
> for what they do not say") already described this; no label was reworded.
> **Providers, called live from this machine, 2026-09-20 ~12:37, one request each, 8 s timeout, polite
> agent, query "what is a PST file":** Wikipedia - WORKS (5 hits, 3 pages read, 6.3 s in all);
> DuckDuckGo - anti-bot challenge again, reported as "asked for proof that a person was searching",
> not bypassed; SearXNG and Brave - **cannot be tried here** (they need the person's own server address
> and key, and no key may be entered by an agent). Settings therefore says so on the choice itself
> (`app/ui/widgets/chat_box.py`, `CHOICE_NOTES`; the stored value stays the bare word): Wikipedia "works,
> no account; encyclopedia questions only", DuckDuckGo "experimental, unverified", SearXNG and Brave
> "unverified". Automatic still tries Wikipedia then DuckDuckGo and moves on from a challenge.

- [x] **3e** tab-one plain-words rules apply to every fixed string; policy
  seam: chat behaviours (rounds, verification threshold, model mapping) are
  envelope tunables, invisible outside Manual.

> **Dated note, 2026-09-19 - section 4.** 4a is ticked
> (`test_chat_evaluate.py::test_the_fixture_has_at_least_sixty_pairs_across_every_router_class`,
> `test_it_has_planted_absences_and_trap_questions`,
> `test_evaluate_chat_runs_from_the_command_line_without_a_model`; the fixture is
> 80 questions).
> **4b is left open: the extractive floor was not met with real models.** The
> build thread reports extractive correctness of 83.9% and 83.3% with real models,
> below this order's 85% ship floor (numbers as reported to this documentation
> pass; the runs were not repeated here and the model names were not given
> (UNCONFIRMED here)). The deterministic stand-in clears every floor and is
> pinned in `test_chat_evaluate.py`, but it is not a language model. Measured
> 2026-09-19 with `python -m app.cli evaluate --chat --chat-fake`, 80 questions,
> stand-in `FakeLLM` (NOT a language model): citation validity 100.0% (74 of 74
> sentences, none without a receipt), extractive 96.8%, aggregate exactness
> 100%, absence honesty 100%, absence recall 100%, router 100%, find 100%,
> refusal on thin retrieval 66.7%, trap questions 60.0%, synthesis 80.0% (no
> floor at v1). Citation validity, aggregate exactness and absence honesty have
> no real-model measurement recorded in this file (UNCONFIRMED). Synthesis ships
> as extract-and-quote (`synthesis_combine` is off in `app/chat/config.py`),
> which is this item's own stated downgrade.
> **4c is left open:** the harness measures first narration, first answer text
> and whole-answer latency (`app/chat/evaluate.py`), and first narration under
> 1s is asserted (`test_chat_evaluate.py::test_every_first_narration_arrives_within_a_second`;
> `test_chat_engine.py::test_the_first_narration_comes_before_any_model_is_consulted`,
> so it does not depend on the model). The only first-answer-text p95 recorded is
> the stand-in's (0.09s on 2026-09-19, meaningless as a model latency); no
> real-model p95 is recorded here.
> **4d is left open:** the engine resolves three roles - router, planner, answerer
> - with a one-model-everywhere default, explicit roles kept exactly as typed and
> embedding models never chosen (`test_chat_llm_roles.py`,
> `test_chat_engine.py::test_roles_default_to_one_model_and_split_only_on_request`,
> `test_set_model_writes_through_to_the_answering_role`). Not built: the Manual
> roles grid of installed-model dropdowns, the Describe (vision) role, the
> RAM-honesty line, and the in-tab Fast / Thoughtful write-through from the UI
> (gap 2). The three chat model settings are plain text boxes.
> **4e:** 4e-1 (`test_chat_text.py::test_a_snapped_span_is_a_substring_of_whole_words`),
> 4e-2 (`test_chat_tab_qt.py::test_sources_only_ever_append_and_numbers_never_change_mid_answer`,
> `test_chat_verify.py::test_sources_are_numbered_in_first_mention_order_and_never_renumbered`),
> 4e-3 (`test_chat_tab_qt.py::test_the_keyboard_walks_the_sources_without_leaving_the_box`)
> and 4e-4 (`test_chat_aggregate.py::test_every_aggregate_answer_states_its_scope_in_the_same_breath`,
> `test_a_list_says_when_it_is_complete_and_when_it_is_not`, and the FIND ending
> in `test_chat_tab_qt.py::test_a_find_answer_shows_the_real_results_and_ends_with_thats_all`)
> are ticked. On 4e-3 the "preview" is the passage strip, not the full
> `PreviewPane` (gap 1); the search page's row upgrades reach chat because the
> Sources pane uses the same `ResultsView`.

## 4. Measured before shipped (what "world class" means here)

- [x] **4a** `evaluate --chat`: a fixture QA set (≥60 pairs across all
  router classes, including planted absence cases and trap questions whose
  tempting answer is wrong) scoring: citation validity % (every rendered
  sentence's receipt checks out), extractive correctness, aggregate
  exactness (must be 100% — they're queries), absence honesty (never claims
  nonexistence-in-world, always scopes to index), and refusal correctness
  (thin retrieval → says so rather than guessing).
> **Dated note, 2026-09-20 - 4b, 4c and the conversation: measured, with real models where it could be, and
> what could not be.** Command: `python -m app.cli evaluate --chat [--chat-model NAME] [--chat-conversation]
> [--chat-runs N]`; the fixture is now 96 questions (13 conversational, 3 general-but-not-in-the-files) plus
> two scripted multi-turn conversations (`CONVERSATIONS`: a greeting, a general question, a follow-up on that
> answer, an archive question, a follow-up on it, "shorter", a Regenerate; then the files first and talk
> about the answer). Every turn has a wall-clock limit (`ask_with_limit`, 900 s) and a reply that trickles
> for 15 minutes is ended (`REPLY_DEADLINE_S`): a stalled turn is a failed turn, reported, and the run goes on.
> **The old figures - extractive 83.9% and 83.3% "with real models" - are replaced** (they were never
> reproduced; no model was named). **What was measured, on this machine (Windows 11, 12 logical CPUs, 32 GB,
> Ollama on the CPU, shared with other work - so every latency is noisy and is a maximum-honest, not a best case):**
> * **Deterministic stand-in (`FakeLLM`, NOT a language model)**, all 96 questions: citation validity 100.0%
>   (52 of 52 claims, none without a receipt), extractive 96.8%, aggregate exactness 100%, absence honesty
>   100%, absence recall 100%, refusal on thin retrieval 66.7%, conversation 100%, labelled general fill 100%,
>   trap questions 60.0%, router 100%, find 100%, synthesis 60.0% (no floor at v1); both scripted conversations
>   10 of 10 steps the right shape. Pinned, with margin, in `test_chat_evaluate.py` (`PINNED`). This is the
>   router, the loop, the checking and the counting - not a model.
> * **Real model, `mistral` (the shipped default answerer; `llama3.2:1b` routes), partial**: the run was
>   killed by the machine's watchdog after 23 questions (a 7B model on a busy CPU took 15-200 s a question), so
>   **17 of the first 23 lookup questions correct = 73.9%**, on the code as it stood *before* two later fixes
>   (ordinals - "the 1st of March" is now read as "1 March" - and cutting a padded sentence back to its
>   supported clause). It is **below the 85% extractive floor**. Where it missed, the checking was doing its
>   job (a wrong "three nonconformities" for the real seven was dropped, not shown); the cost is that the
>   answer became "I couldn't find that in your files", which is wrong the other way round (the fact *is* there).
> * **Real model, `llama3.2:1b`**, the first 16 questions of a 22-question subset that was still running at the
>   deadline (nine lookups, two follow-ups, two counts, two planted absences, one trap): lookups 2 of 9 correct
>   (22%) - far below the floor, as expected of a 1B model; the counts, the planted absences and the trap were
>   right (they do not depend on the model writing). A separate run of the conversation script, 10 steps: **9 of
>   10 the right shape** (the miss: "Who is my landlord?" was not answered from the file). First narration under
>   0.3 s; first token min 0.22 s, p50 6.7 s, max 35.4 s; whole reply p50 19.8 s, max 54 s (n=10, other jobs on
>   the same CPU).
> **Decisions.** The floors are **not lowered**: citation validity >= 98%, aggregate exactness 100%, absence
> honesty 100%, extractive >= 85% stay the bar for the answering model. **No real model measured here clears
> the extractive floor**, so 4b stays open. Citation validity, aggregate exactness and absence honesty do not
> depend on a model writing well and are 100% on the stand-in; they were not re-measured with a full real-model
> run (the runs above were bounded by the watchdog), so they are **UNCONFIRMED for real models**. 4c stays open:
> first-token p95 for a real model was not measured with enough runs to call it a p95 (`--chat-runs N` exists
> for it).
> **A real-model conversation, as it reads** (`mistral`, this machine, 2026-09-20; the first reply took 3.5 minutes
> because three jobs were sharing the CPU while the model loaded): "hi" -> "Hello there! I'm Leasha, your personal
> assistant inside your computer. I can help you with a variety of tasks, like finding files or answering
> questions based on the information in your files and emails. Just ask me what you need..."; "How much notice
> must the tenant give?" -> "The tenant must give two months notice, as stated in the tenancy agreement [1]."
> With `llama3.2:1b` (the reply to "shorter" copied the "Not from your files:" label it had seen in its own earlier
> reply; the engine's own lead and label are no longer shown back to the model - not re-measured): "What is a PST file?" -> "I couldn't find that in
> your files." / "**Not from your files:** PST files are a type of file used by Microsoft Outlook to store email
> messages..."; "explain that like I'm eight" -> "... A PST file is like a special box for keeping track of
> emails..."; "shorter" -> a shorter version. **Fast / Thoughtful, against the live Ollama:** Fast chose
> `llama3.2:1b`, Thoughtful `mistral:latest` (`debug["style"]`); both ran; the answering model changed with the
> control. **Live web run** (`llama3.2:1b` answering, Wikipedia, "Ask before each web search" on): "What is a PST
> file?" -> the phrase "PST file" was shown and allowed, five hits and three pages read, and the answer was two
> sentences cited to the Wikipedia page (marked Web, `locator="Web"`); a model that writes bold headings and adds a
> name from the page shows the 1B model's limits, not the plumbing's. **One thing that run showed and is not
> done:** with the web on, a question the files can already answer ("How much notice must the tenant give?")
> still asks whether to search the web - the ask is the guard, but the web should be consulted only when the
> files are thin or the checked answer is partial; that refinement is not built.

> **2026-09-20 (closing pass) - 4b and 4c are left open on purpose, and the floor is NOT lowered.**
> Real-model lookup ("extractive") measurements on this machine, both partial because a full
> 96-question run costs about 40 s a question while the machine is shared: `mistral` 17 of the first
> 23 lookups (73.9%, cut off by a watchdog), `llama3` 5 of the first 9 (56%, stopped for time),
> `llama3.2:1b` 2 of 9. None reaches the 85% ship floor; the earlier 83.9% / 83.3% figures in this
> file remain UNCONFIRMED (never reproduced here). The stand-in `FakeLLM` still clears every floor
> and is pinned in `test_chat_evaluate.py`, but it is not a language model. Decision: the 85% floor
> stays as the target, because a floor chosen to fit a weak model would certify answers nobody
> should trust. **The way to close 4b is a stronger local model** (`gpt-oss:20b` and `gemma4:26b`
> are installed and were not run to completion: minutes a question on this laptop) or the owner
> choosing a lower floor with eyes open. `python -m app.cli evaluate --chat --chat-model NAME
> --chat-ids L01,...,L27` measures the lookups alone. 4c: first narration under one second is
> asserted without a model; first-answer-token p95 was not measured (`--chat-runs N` does it).

> **2026-09-20 (Opus pass) - the floor was being measured against half the retrieval stack, and
> the real number is 84.0%, not 73.9%.** `evaluate --chat` built its engine with
> `keyword_engine`: no embedder, no vector store. So every lookup whose answer has to be found
> by *meaning* failed before the model was asked, and the score was reported as the model's
> quality. This repository has made the identical mistake once before, on the **search**
> evaluation - the comment in `app/cli/evaluate.py` beginning "The vectors have to be built or
> this is not the full pipeline" is its record - and the `no vector hits` warnings were sitting
> in the log both times. Fixed: a real model now gets `real_engine` (vectors built from the
> fixture corpus, about a second); `--chat-fake` keeps keyword-only so it stays fast; and the
> report prints **which retrieval it measured**, because a measurement that does not name its
> own conditions invites this a third time.
>
> **Measured after the fix**, `mistral` answering, `llama3.2:1b` routing and planning, all 27
> lookups, real retrieval, on the owner's machine:
>
> | | keyword only (the old, wrong way) | real retrieval |
> |---|---|---|
> | extractive | 73.9% (17 of 23, run cut short) | **84.0%** (23 of 27) |
> | citation validity | - | 97.1% (33 of 34 sentences; **0 without a receipt**) |
> | absence honesty | - | 100.0% |
> | router | - | 100.0% |
>
> **4b stays open, and the floor is still not lowered.** 84.0% against 85% is now a miss of one
> point rather than eleven, and citation validity is 97.1% against 98% - one sentence in
> thirty-four. Both are close enough that a stronger answerer is the obvious next thing to try
> (`gpt-oss:20b` and `gemma4:26b` are installed and were not run: minutes a question on this
> laptop). What is *not* acceptable is moving the floor to meet the model. The command to repeat
> it: `python -m app.cli evaluate --chat --chat-model NAME --chat-ids L01,...,L27`.

- [ ] **4b** floors recorded in this file at first measurement and pinned as
  regression tests; the order does not ship below: citation validity ≥98%,
  aggregate exactness 100%, absence honesty 100%, extractive ≥85% on the
  fixture. Synthesis has no floor at v1 — it ships only if the measured
  quality on 7–8B justifies it, else the router keeps synthesis questions
  in extract-and-quote mode until the GPU machine (an honest downgrade the
  user never sees as an error).
> **2026-09-30, 4c on the owner's laptop: not measured; the command could not have measured the
> engine the app now uses, and that is fixed.** `evaluate --chat` built its models from Ollama whatever
> `CHAT_ENGINE` said (`_pick_models`), and `CHAT_ENGINE` has been `onnx` - the model inside Leasha -
> since 2026-09-29. So no latency in this file describes today's default. Now, with no `--chat-model`,
> the engine in the settings answers (one model for every role, as `ChatEngine` does it), the report's
> heading names the copy on disk (`OnnxLLM.serving()`: here "Qwen 2.5 1.5B Instruct, 4-bit", the only
> chat copy in `D:\Leasha\Data\models`), and `--chat-model NAME` still means an Ollama model. The run
> itself was not started: the owner needed the machine for a real index run. **No figure was taken, so
> 4c stays open.** What remains, on a quiet machine: `venv\Scripts\python.exe -m app.cli evaluate
> --chat --chat-runs 3 --json` (96 questions each run; it builds its own fixture in a temporary folder -
> `run_cli` - and reads no document of the owner's). It loads the model before the first question, so
> the cold first call has to be timed apart (a fresh process, first question only). Tick 4c only if the
> first narration line is under 1 s and the first-answer-text p95 is recorded.
- [ ] **4c** latency: first narration line <1s, first answer token p95
  measured and recorded; the loop's visible steps make honest latency feel
  fast — but measure anyway.

## 4d. ADDED by owner idea 2026-08-28 — model ROLES, not one model

Model selection is per-ROLE with a one-model-everywhere default:
roles = **Interpret/translator** (tiny+fast, 1–3B class — shared by all
search tabs), **Chat** (strongest affordable — this order's engine),
**Describe** (vision-capable only). Defaults mode: one global model, all
roles inherit (also the performance-correct default — Ollama model
residency/reload cost makes one resident model right for most machines).
Manual: a roles grid in Models & AI — dropdowns of INSTALLED models
(`ollama list`), vision roles offer only vision-capable models
(greyed-with-reason otherwise), with a RAM-honesty line ("these two
together need ~11GB — you have 32"). Chat's in-tab Fast/Thoughtful writes
through to the Chat role. **EMBED_MODEL stays OUT** — rebuild decision,
separate home, its own warning. Eval floors (4b) and the translator
[TUNE] pass are measured PER ASSIGNED MODEL. Schema leaves room for
per-surface overrides within a role later, evidence-gated.

## 4e. ADDED 2026-08-28 — adopted from the results-presentation order (202626271510)

The Sources pane (3a) and inline result answers (3b) render with the same
delegate the search pages use, so that order's row upgrades — snippet
windows centred on the match, sentence-boundary cuts, real file icons,
sender-first mail rows, monospace code snippets, hover, pixel scrolling,
left-elided locations, twin disambiguation — **flow into chat
automatically. Verify the inheritance at build time; re-implement
nothing.** Four adaptations are chat's own:

- [x] **4e-1 Quotes cut at sentence boundaries.** 2b copies verbatim spans
  by offsets the model selects; the system **snaps those offsets to
  sentence boundaries** before printing (never mid-word, prefer whole
  sentences — the 1c logic from the results order, reused). A quote that
  reads like a sentence is trusted like one; verbatim is preserved because
  snapping only widens or narrows to real text, never rewrites.
- [x] **4e-2 Stable streaming.** The results order's stable-update rule,
  applied to chat: while tokens stream, the Sources pane may append but
  never reshuffles — a receipt the user is mousing toward must not move.
  Superscript numbering is assigned in first-mention order and never
  renumbers mid-answer.
- [x] **4e-3 Keyboard flow in chat.** From the message box: ↓/↑ walk the
  Sources pane with focus staying in the box, preview-on-selection,
  Enter (with a source selected) opens it, Esc returns to the
  conversation. The 6a convention, same muscle memory as search.
- [x] **4e-4 Answers end by saying so.** The terminator instinct (5c),
  chat-shaped: an AGGREGATE answer states its scope in the same breath as
  its number ("47 photos — counted across everything indexed, including
  offline drives"), and a FIND answer's inline grid ends with "that's all
  N". An answer that declares its edges reads as finished, not truncated.

Tests ride the sections they touch: 4e-1 property test (snapped span is a
substring of the chunk, verbatim, whole words); 4e-2 pytest-qt
streaming-reshuffle assertion; 4e-3 the keyboard scenario; 4e-4 string
checks in the aggregate/find fixtures.

> **Dated note, 2026-09-19 - section 5.** Ticked, with evidence: router
> (`test_chat_router.py::test_every_fixture_question_goes_to_the_right_machine`);
> verification (`test_chat_engine.py::test_a_hallucinating_model_produces_zero_unreceipted_sentences`,
> `test_chat_verify.py::test_a_misquote_dies`, `test_a_fabricated_number_dies_in_validation`);
> aggregate (`test_chat_aggregate.py::test_over_generated_corpora_the_number_always_equals_the_count`,
> a property test); absence (`test_chat_absence.py::test_a_planted_absence_gets_the_protocol_answer`);
> the no-badge rule (`test_chat_tab.py::test_no_fixed_string_in_the_tab_is_a_warning_banner`,
> `test_chat_tab_qt.py::test_nothing_on_the_chat_page_is_a_warning_banner`).
> **The UI scenarios item is left open.** The scenarios exist (ask, stream, click a
> receipt, pin, follow-up, Ollama-absent) and pass, but they run against
> `FakeChatEngine`, not `ChatEngine`; "follow-up scoped" therefore proves the tab
> hands `scope` to a fake that accepts it, not that the real engine is scoped
> (gap 3). It closes when one scenario drives the real engine with a stub model.
> On 2a: the support check is lexical first (a sentence's meaningful words must
> be found in the window of the passage it cites, `SUPPORT` 0.7, tunable as
> `CHAT_VERIFY_STRICTNESS`) with an embedding-similarity rescue when the search
> engine has an embedder; it is not embedding-first as the item words it. The
> unreceipted-sentence count on the fixture is zero either way.

## 5. Tests

- [x] router: each class routed correctly on the fixture set (Qt-free).
- [x] verification: a deliberately hallucinating stub model produces ZERO
  rendered unverified sentences (the load-bearing test of the order);
  misquote-injection dies in 2b; fabricated-number dies in 2c.
- [x] aggregate: the number shown equals the query result, always (property
  test over generated corpora).
- [x] absence: planted-absence fixtures produce the protocol answer.
- [x] UI: pytest-qt scenarios per 0m's convention — ask, stream, click a
  receipt, pin to shelf, follow-up scoped, Ollama-absent degrade.
- [x] the no-badge rule as a test: no fixed string in the tab matches the
  warning-banner deny-list; honesty is structural, asserted structurally.

## Done means

Change + tests + suite green + committed by name; 4b floors recorded here
with the model(s) measured; CHANGELOG. Acceptance sentences: ask "what did
we agree with the landlord about the deposit" and get two sentences whose
receipts open the actual letters; ask "how many photos from Diwali 2019" and
the number is a database fact; ask about something that isn't there and be
told exactly what was searched; and at no point does anything rendered lack
a receipt — because the system, not the model's good behaviour, guarantees
it.
