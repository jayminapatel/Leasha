# Work order: scope change - local search you can describe in plain English

**Doc version:** 2.0 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

**Supersedes version 1.0 of this document**, which made prose chat the headline feature. That
was an inference, and it was wrong. The owner has since been explicit:

> "Local search is my main objective, but I want to write in normal text what I am looking for,
> and this complexity to solve may need AI."

The deliverable is **search that returns documents**, driven by a plain-English description.
Prose answers are a secondary mode, not the point.

**Read this before starting Layer 7.** Layer 7 is next on the plan and is cancelled.
`app/office/builder.py` is still a 225-byte stub, so nothing is lost by stopping now.

Commit the working tree before starting.

---

## 1. The revised plan

| Layer | Was | Now |
|---|---|---|
| L0-L4 | Foundation through search | **Keep.** This *is* the local search |
| L5 | PyQt6 UI | **Keep** |
| L6 | Knowledge graph | **Remove** - section 3 |
| L7 | Office document builder | **Cancelled** - never requested, not started |
| L8 | RAG answers | **Replaced** by L8a and L8b below |
| **L8a** | - | **Natural-language query translation. The priority.** |
| **L8b** | - | Prose answers over results. Secondary, build only if wanted |
| L9 | Hardening and packaging | Keep, smaller |
| L10 | Adaptive tuning | **Cancelled** - speculative |

## 2. Measure before building - this may already work

**Semantic search is natural-language search.** Embedding the sentence "the safety report Dave
sent about the Leeds site" and finding similar passages is exactly what Layer 4 already does.

So the first task is not to write code. It is:

1. Index a real corpus.
2. Type twenty real sentences into the search box - the kind the owner would actually use.
3. Record, for each, whether the wanted document appeared in the top ten.

**Write the results into the changelog.** That number decides whether L8a is worth building,
and it is the only honest way to know.

The expected outcome, worth stating so it can be checked rather than assumed: **topic matching
will be decent, constraints will be ignored.** It will find safety reports about Leeds; it will
not reliably honour "from Dave" or "before the audit", because embeddings encode meaning, not
metadata. If that is what the twenty sentences show, L8a is precisely the right fix. If
constraints are honoured fine, stop - the feature is already delivered.

## 3. Remove the knowledge graph

Not in the search path, nothing depends on it, the only user does not want it, and git
preserves it at the current tag.

**Delete:** `app/graph/`, `app/ui/graph_view.py`, `cmd_graph` in `app/cli.py`, graph handling
in `app/ui/workers.py` and `app/ui/presenter.py`, and the tests `test_cooccurrence.py`,
`test_graph_builder.py`, `test_graph_llm.py`, `test_cli_graph.py`,
`tests/integration/test_layer6_acceptance.py`. Remove graph sections from `README.md`,
`BUILD_SPEC_V2.md`, `LOCAL_KNOWLEDGE_GRAPH_V2.md`, `docs/TROUBLESHOOTING.md`. Drop `pyvis` and
`networkx` from `requirements.txt` **if nothing else uses them - check first**.

**Keep, deliberately:**

- **`app/llm/ollama.py`** - it will look orphaned for one commit. L8a needs it.
- **The three tables** `entities`, `entity_mentions`, `entity_edges`. Dropping them means a
  migration to schema v5, and migrations only step forward: reviving the feature would then
  need a v6 to undo it. They are empty. Comment them as deprecated, pointing here. Drop at L9
  if it still seems worthwhile. **Do not bump the schema version for this change.**

## 4. L8a - natural-language query translation

### The shape of it

`app/search/query.py` already parses typed operators, and `SearchEngine.search()` takes a raw
string. The field aliases already exist:

| Concept | Accepted as |
|---|---|
| file type | `type:` `ext:` `kind:` |
| after a date | `after:` `since:` |
| before a date | `before:` `until:` |
| sender | `from:` `sender:` |

So **the model's entire job is to emit a query string in the syntax that already exists**:

```
"the safety report Dave sent me about Leeds before the audit last March"
    ->  safety report Leeds from:dave before:2025-03-01
```

That framing carries three properties worth naming, because they are why this design is safe:

- **The output goes through `parse_query()`**, which is already written and tested. A malformed
  translation is caught by existing code.
- **The model cannot express anything the user could not have typed.** There is no injection
  surface, because the output is validated against a fixed grammar.
- **It is trivially testable.** Sentence in, query string out, asserted against an expected
  string. No model needed in the test.

### The rule that changes, stated precisely

The standing rule was "search never calls the LLM". This bends it, so restate rather than erode:

> **The retrieval path never calls the LLM.** Query *translation* may. It is optional, it costs
> about a second, it runs once before the search, and it is always visible to the user. Plain
> keyword and semantic search remain instant and never touch a model.

Enforce the first sentence with a static test: nothing under `app/search/` may import
`app.llm`, except the single translation module, which must be importable independently and
must not be reachable from `SearchEngine.search()`.

### Transparency is a hard requirement

**The app shows the query it built, and lets the user edit it.** Put the translated query in
the search box itself, so the next search starts from it.

This is not polish. If AI silently rewrites a query, search becomes unpredictable - "why did it
find *that*?" - and unpredictable search over your own archive is worse than blunt search,
because you stop trusting the results. A visible, editable translation means a bad translation
is a two-second correction rather than a mystery.

### Behaviour

- **Trigger.** Explicit, not automatic: an "Interpret" button or `Ctrl+Enter`. Plain Enter runs
  the raw text unchanged, exactly as today. The user chooses to spend the second.
- **Ollama absent or slow.** Fall back to running the raw text as a normal search, with a quiet
  note. `ERR_OLLAMA_DOWN` already exists and carries the fix. **Never block a search on the
  model.**
- **Timeout.** Cap it (start at 5s). On timeout, fall back to the raw text.
- **Nothing extractable.** If the sentence carries no filters, emit the cleaned text and say so.
  Not every sentence needs translating.
- **Relative dates.** "last March", "before the audit", "a couple of years ago" resolve against
  today's date. `_parse_date` already exists; the model should emit ISO dates and let it
  validate. A date the parser rejects means the whole translation is rejected and the raw text
  is used.
- **Cache translations** keyed on the sentence. Repeating a search should not pay twice.

### Acceptance tests

Use a fake client returning canned strings. **No test may require a running Ollama.**

- [x] A table of at least fifteen sentence -> expected-query pairs, covering: sender, file type,
      date before, date after, relative dates, phrases, exclusions, and combinations.
- [x] Output always parses with `parse_query()`. Property test it with junk model output:
      empty, prose, an unclosed quote, an invented operator, 10,000 characters. Every one falls
      back to the raw text rather than raising.
- [x] An invented operator (`colour:red`) is rejected, not passed through.
- [x] A date the parser rejects causes fallback to raw text, not a partial query.
- [x] Ollama unreachable: the search still runs on the raw text and returns results.
- [x] Translation exceeding the timeout falls back, and the fallback is logged once, not per key.
- [x] The translated query is returned to the caller for display - assert it reaches the
      presenter, since the UI contract depends on it.
- [x] Static: nothing in `app/search/` imports `app.llm` except the translation module.
- [ ] **Recorded in the changelog:** median translation latency on the owner's hardware.

## 5. L8b - prose answers. Secondary, and only if asked for

Do not build this until L8a is in use and the owner asks for it. If it is built:

One search, then answer - the owner's explicit choice over multi-step retrieval. Reuse
`SearchEngine`; do not write a second retrieval path. **Every claim carries a citation that
opens the source passage**; an answer with no citations is a bug, asserted in a test. This is
the only workable mitigation for a 7B model stating wrong things confidently over technical
material: it makes a wrong answer visibly wrong rather than plausibly wrong. Instruct the model
that "the passages do not answer this" is a correct response. Follow-ups re-run retrieval. When
the context fills, drop history, never passages. Stream the output.

## 6. Also in scope: strip quoted replies and signatures

Independent of everything above, and it improves search directly.

A twelve-deep email thread currently has its text indexed twelve times over - inflating the
index, wasting embedding time, and filling results with near-duplicates. Strip quoted chains
and signature blocks at extraction (`email_files.py`, `email_pst.py`):
`-----Original Message-----`, `________________________________`, `On <date>, <person> wrote:`,
`Sent from my ...`.

Keep the *first* occurrence, so a reply quoting something not otherwise in the corpus stays
findable. Log bytes stripped, so the effect is measurable.

## 7. Documents to update

- `HANDOFF.md` - the scope change in the owner's own words, and what would justify reviving the
  graph, Layer 7, or L8b. This decision record matters more than the code.
- `BUILD_SPEC_V2.md` - remove L6, L7, L10; replace L8 with L8a and L8b.
- `LOCAL_KNOWLEDGE_GRAPH_V2.md` - the app is local search with plain-English queries. The
  knowledge graph is gone; consider whether the document's name still fits.
- `README.md` - one headline feature, not seven.
- `docs/TROUBLESHOOTING.md` - remove graph entries; add "the interpreted query was wrong" with
  the fix being to edit it in the search box.
- `CHANGELOG.md` and `VERSION`.

## 8. Order

1. **Measure plain semantic search on twenty real sentences. Record it.** This may end the
   work order at step 1, which would be a good outcome.
2. Remove the graph. Suite green.
3. Delete `app/office/`. Suite green.
4. Update the specs so the written plan matches the built one.
5. Build L8a.
6. Stop. Use it. Only then consider L8b.

## 9. Still outstanding, unchanged

The real-Outlook COM check in `HANDOFF.md` has not been run. **Do not bump `VERSION` to 0.4.0
until it has.** No test can cover it.

## 10. Decisions recorded

- **Search returns documents.** Prose answers are secondary. The owner's stated objective.
- **Measure before building.** Semantic search may already be sufficient; twenty sentences will
  say.
- **The LLM fills in a form that already exists** - it emits the established query syntax,
  validated by the existing parser. It never reaches the retrieval path.
- **Translation is always visible and editable.** Invisible query rewriting destroys trust.
- **Translation is optional and never blocks a search.** Ollama down means plain search.
- **Graph removed, L7 cancelled, L10 cancelled.**
- **L8b deferred** until L8a has been used in anger.

---

## Audited 2026-08-27 — 8 ticked, one left for the owner

Verified against `tests/unit/test_translate.py` (23 tests) and
`test_translate_timeout.py` (16), item for item: the sentence table holds
exactly fifteen pairs and every one of them parses; junk output, an invented
operator like `colour:red`, and a date the parser rejects each fall back to the
raw text rather than to a partial query; Ollama unreachable and Ollama timing
out both leave the search working, and the fallback is logged once rather than
once per key; the translated query reaches the caller for display; and the
static guard holds - nothing on the retrieval path imports the LLM, and the
search engine cannot reach the translator at all.

**Still open:** the median translation latency on the owner's hardware. It is
a number from a machine with Ollama running, so it cannot be taken here.
