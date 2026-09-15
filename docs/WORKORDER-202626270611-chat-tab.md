# Work order (One thread): the Chat tab — ask your archive, and every answer has receipts

**Doc version:** 1.8 · **Updated:** 2026-09-15 · **Applies to:** app v0.3.3
**Thread:** One thread (new tab + Search/LLM layers + eval harness)
**Status: ACTIVE — promoted 2026-09-13.** Was HELD, created at the owner's
request to be scheduled by him later; the owner has now promoted it. The
reopening note is on `WORKORDER-scope-change-search-and-chat.md`, appended
2026-09-13. Prerequisites at promotion, checked rather than assumed: the
search-experience order (translator seam, policy machinery) landed and is
`SHIPPED` (0c); the photo lanes (0510-0512) have not, so their benefit does
not compound yet - built against text only until they do. A 7-8B CPU model
is the design target and is available locally (`mistral:latest`, 7.2B,
already the configured `OLLAMA_MODEL` default) - see §4 for what gets
measured against it as sections land, rather than all at once at the end.

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
  *Built 2026-09-13 as `app/chat/router.py`, `RouteDecision.matched_rule`/
  `used_model`/`raw_model_output` carrying the debug-pane visibility. Two
  real bugs found and fixed by checking against the actual design-target
  model (`mistral:latest`, 7.2B) rather than only the fake-client tests: a
  blunt "how much" rule misrouted a single-figure LOOKUP question straight
  to AGGREGATE with no chance for the model to overrule it, and the
  SYNTHESIS rule missed "what's changed" (only matching the bare "what
  changed"). `ROUTE_TIMEOUT_S=5s` is kept short on measurement, not despite
  it: a genuinely cold model load took 15.91s and a warm one 0.86s; Ollama
  keeps generating after the client's timeout gives up, so a cold miss
  costs one safe LOOKUP fallback while the model warms for every later
  question in the session, which is preferable to making routing itself
  the slow part of the first question. FOLLOW-UP is gated on `history`
  being non-empty and is never offered to the model (no conversation is
  shown to it in this call), so a follow-up phrase with nothing to follow
  falls through to LOOKUP rather than being guessed. 35 tests, `tests/
  unit/test_chat_router.py`, all fake-client per the same rule `test_
  translate.py` holds itself to.*
- [x] **1b The loop**: plan search queries from the question (REUSING the
  translator — question → filter grammar + terms); retrieve via the real
  engine (all lanes: text, and when 0510+ land, images/people/places);
  assess sufficiency; the model may request FURTHER searches (bounded, ≤3
  rounds) when context is thin — a small model in a tight tool loop beats a
  small model with a stuffed window. Each step streams a plain-words
  narration line ("Searching… found 8 documents · reading the 2019
  contract…") — the loop's own progress is the trust-building UX.
  *Built 2026-09-13 as `app/chat/loop.py`. Text lanes only - photo/people/
  places lanes wait on 0h-0j, unlanded; "reading the 2019 contract" style
  per-document narration is §1c's job (context economy) and is not claimed
  here. Round one reuses `QueryTranslator` exactly as asked; rounds two and
  three ask the model for a follow-up query, validated the same way a
  translation is - parsed with `parse_query()`, rejected rather than run if
  it does not fit.*

  *Three real bugs found by testing against the real local models rather
  than only fakes. (1) `parse_query()` happily accepts prose as a bag of
  keywords - that is what it is for - so a follow-up like "I think you
  should look in the Documents folder" parsed cleanly and would have run as
  a search; rejected now on the same "translation compresses" principle
  `translate.py` uses, capped at `_MAX_PLAIN_WORDS` when nothing about the
  reply reads as a deliberate query (no operator, no phrase). (2) A model
  repeating its own last query is now rejected as stalling rather than run
  again for no new information. (3) The sufficiency-check timeout was first
  measured at a misleadingly fast 4.4-4.7s by timing the *same* prompt three
  times against `mistral:latest` (7.2B) - which is fast only because Ollama
  caches an identical prompt. Against real, varying questions the same model
  took 31-36s per check; `qwen2.5:1.5b`, the "tiny+fast" role §4d of this
  order designs for rather than the "strongest affordable" Chat role, did
  the same job in 4.6-7.6s cold/semi-warm and under a second once genuinely
  warm. `SUFFICIENCY_TIMEOUT_S=10s` is set against that model, documented as
  such, and will need reconsidering once model roles (§4d) exist and
  sufficiency might run on whichever model is actually answering.*

  *21 tests, `tests/unit/test_chat_loop.py`, fake engine/translator/client
  throughout - the same rule `test_translate.py` and `test_chat_router.py`
  hold themselves to.*
- [x] **1c Context economy for small models**: per-document extract-then-
  combine (map-reduce) for synthesis instead of one giant prompt; the
  RERANK_WINDOW machinery reused for snippet windows; context budget derived
  from the model's actual window (queried from Ollama), envelope-style.
  *Built 2026-09-13: the "extract" half. `OllamaClient.context_length()`
  (new) reads `/api/tags`, which already carries `details.context_length`
  per installed model - the same endpoint `available_models()`/`has_model()`
  already call, so this needed no new probe. `app/chat/context.py` turns
  that into a character budget by envelope arithmetic (reserve output
  tokens, reserve prompt overhead, convert the rest at `app.search.window.
  RERANK_WINDOW_CHARS`'s own already-measured ratio - "600 is about 150
  tokens" - rather than a second, invented one), divides it across however
  many documents are in play, floored at `RERANK_WINDOW_CHARS` for the same
  reason that module's own docstring gives ("the licence expires" without
  which licence), and calls `app.search.window.windows_for` - unchanged -
  to do the actual cutting. Verified against the real `qwen2.5:1.5b` and its
  real 32,768-token window, not an assumed number.*

  *The "combine" half - folding sized extracts into one synthesis prompt -
  is deliberately not built yet. It would mean generating prose before §2
  (verification) exists to check it, which is the order's own principle 1:
  "the model never speaks unverified." `build_context` produces exactly the
  sized, per-document material a combine step will need; the step itself
  waits until there is a verifier to gate its output.*

  *22 tests total: 7 for `context_length()` (`tests/unit/
  test_ollama_context_length.py`, `_get` monkeypatched, no real Ollama
  needed) and 15 for the budget arithmetic (`tests/unit/
  test_chat_context.py`).*
- [x] **1d AGGREGATE answers are computed, not generated**: "how many PDFs
  did Dave send in 2019" runs as a real query; the number in the answer IS
  the query result, injected — the model may only phrase around values it
  cannot alter (template slots, not free generation over numbers).
  *Built 2026-09-13. `app.search.keyword.count_matching(store, parsed)` is
  the query - an exact, unlimited `COUNT(DISTINCT file_id)` over the same
  FTS expression and filter SQL `search()` already uses, not a count of
  `search()`'s own capped, ranked page (a file with three matching passages
  is one document, not three; `KEYWORD_LIMIT` never truncates the total).
  `app/chat/aggregate.py` wraps it: the model, if one is given, may only
  phrase a sentence around the number - its reply is rejected, falling back
  to a plain templated sentence that cannot be wrong, unless every number in
  it is `count` and no other. Found live, the same lesson `translate.
  build_prompt`'s own docstring already records: asked only for "the number,
  unchanged, in a sentence," `qwen2.5:1.5b` answered with the bare digit and
  nothing else - correct, but not a sentence. One example fixed it
  immediately ("There are 12 contracts." → "There are 7 invoices."). 9 SQL
  correctness tests against a real `SqliteStore` (`tests/unit/
  test_aggregate_count.py`) and 14 fake-client tests for the phrasing
  contract (`tests/unit/test_chat_aggregate.py`).*

  *2026-09-15: a genuine bug found only by running `app.chat.session.ask()`
  end to end against the real fixture corpus, the real translator and the
  real `qwen2.5:1.5b` — every unit test above fakes the translator or skips
  it, so nothing had exercised what `answer_aggregate` actually does with
  one. "how many PDFs are in the index" against a corpus that provably
  contains a PDF (`Docs\Licences\licence-terms-2025.pdf`) came back "There
  are 0 PDFs in the index." Root cause: `translate.build_prompt` tells the
  model to "keep everything else as plain words," and with no worked
  example showing an interrogative sentence, the model followed that
  literally - "how many pdfs are in the index" became the query "how many
  are in the index type:pdf", not "type:pdf". `count_matching` then
  required the literal words "how"/"many"/"index" to appear in a document's
  text, which real files never contain, so it silently returned 0 for a
  corpus that had matches. The same lesson this file already records twice
  over (§1a, §1d above): a small model needs a worked example, not a rule,
  for an input shape it has not seen. Fixed with one more entry in
  `app.search.commands.EXAMPLES` - `("how many pdfs are in the index",
  "type:pdf")` - which fits under the existing 1901-char prompt ceiling
  (`test_the_prompt_got_shorter_despite_gaining_examples`) with 9 characters
  to spare. Re-verified against the real model: the same question now
  returns a non-zero, correct-shaped count.*

  *Not fixed, and flagged rather than guessed past: one example teaches the
  one phrasing, not the pattern. Re-tested against the real model
  afterwards, "how many PDFs are in the index" translated to
  "type:docx type:pdf" (an extra, unrequested filter — over-counts rather
  than under-counts) and "how many invoices are there" - a content-noun
  aggregate rather than a file-type one - still translated to "how many are
  there type:pdf" and returned 0. A second worked example for the
  content-noun case would need roughly 45 more prompt characters than the
  ceiling currently allows, so fixing it is a real, scoped follow-up (either
  a documented ceiling increase or a shorter existing example), not
  something to absorb quietly into this note.*
- [x] **1e ABSENCE protocol**: retrieval-negative questions answer with what
  was searched (the queries, visibly), the honest scope sentence ("nothing
  in Leasha's index matches — sources currently indexed: …"), and offer the
  obvious next steps (different words; is it on an unscanned drive?).
  *Built 2026-09-13 as `app/chat/absence.py`. Deliberately not gated on the
  router's `ABSENCE` class - this is the loop's own principle 4
  ("absence is stated honestly"), not only that route's: a `LOOKUP` that
  finds nothing deserves the same honesty an "is there…" question does, so
  `is_empty(loop_result)` is the gate, checked after the loop runs,
  independent of which class routed it there. No model involved at all -
  the whole point is a deterministic sentence that cannot itself
  hallucinate, built from the queries `run_loop` actually ran and whichever
  folders are currently configured. 11 tests, `tests/unit/
  test_chat_absence.py`, pure - no client, no store.*

## 2. Verification: no sentence without a receipt

- [x] **2a** generation carries inline source markers; **post-generation
  validation** checks every sentence's marker points at a retrieved chunk
  AND the sentence is supported by it (cheap entailment proxy: embedding
  similarity between sentence and cited span above a tuned threshold; below
  threshold → the sentence is dropped and, if the answer thins too far, the
  loop retries once with tighter instructions). Unverifiable prose never
  renders. This is the load-bearing mechanism of the whole order.
  *Built 2026-09-13 as `app/chat/verify.py::verify_answer` - the validation
  half. `SIMILARITY_THRESHOLD=0.75`, measured against the real embedder
  (`BAAI/bge-small-en-v1.5`) over five hand-built (chunk, genuinely-
  supported, genuinely-fabricated) triples: a first guess of `0.55` was
  tried and rejected because it would have let four of the five
  fabrications through. A citation to a chunk that was never retrieved is
  treated as gravely as fabricating the claim itself; a sentence citing
  several chunks survives if any one supports it.*

  *The retry-on-thinning half, flagged above as not yet built, was closed
  the same day as `app/chat/answer.py::answer_question` - the first
  genuinely open-ended generation call in this order, held to account by
  `verify_answer` rather than trusted. `THIN_DROP_RATIO=0.5`: above half
  the sentences dropped, one retry with a tighter prompt; a second failure
  is reported as `thin=True`, the caller's cue to fall back to §1e rather
  than show a threadbare answer.*

  *Found live, against the real target model: `qwen2.5:1.5b` asked to end
  every sentence with a citation wrote "Claim. [1]" - the marker **after**
  the period - far more often than the "[1]." this module's own splitter
  had assumed. Splitting on the period alone stranded the marker as its
  own citation-only fragment with no sentence to belong to, so every
  answer verified as unsupported regardless of what it said.
  `split_sentences` now treats a marker's own `]` as a sentence boundary
  too, and refuses to split immediately before a marker - "Claim. [1] Next
  claim. [2]" now splits correctly either way round.*

  *Found live, second case: asked something the two passages could not
  answer ("What is the capital of France?"), `qwen2.5:1.5b` answered
  "Paris" anyway, citing a passage about a tenancy deposit - a real
  hallucination with a fabricated citation, exactly the failure this
  architecture exists to prevent. The retry, asked to try again, produced
  "France is the capital of France" and cited the same wrong passage.
  Both attempts were correctly rejected by `verify_answer` (`thin=True`
  after the retry) - the system never rendered either sentence. This is
  the design working as intended, not a gap: a model that will not say
  "I don't know" is exactly why nothing here trusts the model's own good
  behaviour.*
- [x] **2b** quotes are VERBATIM by construction: quoted spans are copied
  from the chunk by the system, not re-generated by the model (the model
  selects offsets; the system prints the text). A quote in chat can never
  misquote.
  *Built 2026-09-13 as `verify.resolve_quotes`. `{{quote:chunk:start:end}}` -
  curly braces so it can never be confused with a `[N]` citation marker -
  is replaced with the literal source text at that range; the model never
  types the quoted words at all, so there is no wording for it to get
  wrong. An out-of-range chunk or a range outside the chunk's own length is
  dropped outright, never substituted approximately. Resolved before
  citation-checking in `verify_answer`, so a sentence built around a quote
  is verified against its real, substituted text.*
- [x] **2c** dates, numbers, names appearing in answers are cross-checked
  against the cited chunks' text (exact/fuzzy presence); a fabricated
  number dies in validation.
  *Built 2026-09-13 as `verify.cross_checked` - numbers only; dates already
  reduce to number tokens this catches ("2019", "12", "500"), and names are
  not yet attempted. **Measured to be necessary, not a belt-and-braces
  extra**: changing "500 pounds" to "5,000 pounds" in an otherwise-correct,
  correctly-cited sentence scores 0.774 against the source - comfortably
  above `SIMILARITY_THRESHOLD` - because the topic and wording barely
  moved. §2a's embedding check alone would have let it through; `verify_
  answer` requires both gates, not either.*

  *34 tests total across §2, `tests/unit/test_chat_verify.py`: mechanical
  cases (splitting, marker extraction, quote resolution) need no model at
  all; the threshold itself is measured against the real embedder, the
  same convention `recency.WEIGHT` and `name_match.WEIGHT` use.*

## 3. The tab (owner: NO badges — receipts are integrated, not stigmata)

- [ ] **3a** a Chat tab: clean conversation bubbles, streaming tokens, Stop
  button, Enter sends / Shift+Enter newline. No "AI-generated, verify!"
  banners anywhere — the honesty lives in the *architecture*: superscript
  source numbers in the prose, a **Sources pane** alongside listing the
  cited documents as the same result cards search uses — click opens,
  right-click gives the full result menu, hover on a superscript highlights
  the exact passage in the pane. The receipt IS the interface.
- [ ] **3b Answers can BE result sets**: FIND-routed questions ("show me the
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

## 4. Measured before shipped (what "world class" means here)

- [ ] **4a** `evaluate --chat`: a fixture QA set (≥60 pairs across all
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

- [ ] **4e-1 Quotes cut at sentence boundaries.** 2b copies verbatim spans
  by offsets the model selects; the system **snaps those offsets to
  sentence boundaries** before printing (never mid-word, prefer whole
  sentences — the 1c logic from the results order, reused). A quote that
  reads like a sentence is trusted like one; verbatim is preserved because
  snapping only widens or narrows to real text, never rewrites.
- [ ] **4e-2 Stable streaming.** The results order's stable-update rule,
  applied to chat: while tokens stream, the Sources pane may append but
  never reshuffles — a receipt the user is mousing toward must not move.
  Superscript numbering is assigned in first-mention order and never
  renumbers mid-answer.
- [ ] **4e-3 Keyboard flow in chat.** From the message box: ↓/↑ walk the
  Sources pane with focus staying in the box, preview-on-selection,
  Enter (with a source selected) opens it, Esc returns to the
  conversation. The 6a convention, same muscle memory as search.
- [ ] **4e-4 Answers end by saying so.** The terminator instinct (5c),
  chat-shaped: an AGGREGATE answer states its scope in the same breath as
  its number ("47 photos — counted across everything indexed, including
  offline drives"), and a FIND answer's inline grid ends with "that's all
  N". An answer that declares its edges reads as finished, not truncated.

Tests ride the sections they touch: 4e-1 property test (snapped span is a
substring of the chunk, verbatim, whole words); 4e-2 pytest-qt
streaming-reshuffle assertion; 4e-3 the keyboard scenario; 4e-4 string
checks in the aggregate/find fixtures.

## 5. Tests

- [ ] router: each class routed correctly on the fixture set (Qt-free).
- [ ] verification: a deliberately hallucinating stub model produces ZERO
  rendered unverified sentences (the load-bearing test of the order);
  misquote-injection dies in 2b; fabricated-number dies in 2c.
- [ ] aggregate: the number shown equals the query result, always (property
  test over generated corpora).
- [ ] absence: planted-absence fixtures produce the protocol answer.
- [ ] UI: pytest-qt scenarios per 0m's convention — ask, stream, click a
  receipt, pin to shelf, follow-up scoped, Ollama-absent degrade.
- [ ] the no-badge rule as a test: no fixed string in the tab matches the
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
