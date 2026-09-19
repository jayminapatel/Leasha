# Work order (One thread): the Chat tab — ask your archive, and every answer has receipts

**Doc version:** 1.1 · **Updated:** 2026-09-19 · **Applies to:** app v0.3.3
**Thread:** One thread (new tab + Search/LLM layers + eval harness)
**Status: HELD — created at the owner's request, to be scheduled BY THE OWNER
later. Do not execute until he promotes it (registers a queue position in
HANDOFF).** Promotion consciously reopens the search-and-chat scope decision:
at promotion, a dated note goes on `WORKORDER-scope-change-search-and-chat.md`
recording that the owner reopened it with this design — appended, never
edited. Prerequisites when promoted: the search-experience order (translator
seam, policy machinery) landed; benefits compound with 0510–0512 (photo
lanes) and grow with the GPU machine, but a 7–8B CPU model is the design
target — this must be good on the machine of today.

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

## 3. The tab (owner: NO badges — receipts are integrated, not stigmata)

- [ ] **3a** a Chat tab: clean conversation bubbles, streaming tokens, Stop
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
- [ ] **3c The context shelf**: documents the conversation has touched
  accumulate as visible chips (the pinned-working-set pattern); the user
  can pin (keep in scope), remove (out of scope), or add (drag a result
  in). Follow-ups search the shelf first, the corpus second. What the model
  can see is always exactly what the shelf shows — no hidden context.
- [ ] **3d Sessions**: conversations persist locally (the index-sensitivity
  sentence extends to chat logs), listed in a sidebar, deletable; a session
  reopens with its shelf intact. Plain-words model control ("Fast /
  Thoughtful" mapped to installed Ollama models; greyed-with-reason when
  Ollama is absent — the whole tab degrades to a plain explanation of what
  to install, H4 register).
- [ ] **3e** tab-one plain-words rules apply to every fixed string; policy
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
- [ ] **4b** floors recorded in this file at first measurement and pinned as
  regression tests; the order does not ship below: citation validity ≥98%,
  aggregate exactness 100%, absence honesty 100%, extractive ≥85% on the
  fixture. Synthesis has no floor at v1 — it ships only if the measured
  quality on 7–8B justifies it, else the router keeps synthesis questions
  in extract-and-quote mode until the GPU machine (an honest downgrade the
  user never sees as an error).
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
- [ ] UI: pytest-qt scenarios per 0m's convention — ask, stream, click a
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
