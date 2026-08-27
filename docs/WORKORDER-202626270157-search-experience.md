# Work order (One thread): the search experience — one box for an 8-year-old, power for everyone else

**Doc version:** 1.15 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Search policy + translate + UI surfaces + Code tab)
**Status:** RELEASED by the owner 2026-08-27 — sequenced after
`WORKORDER-202626270114-index-tuning.md`. The translator (§3) is **built** in
this order and **fine-tuned later**: the owner has scheduled tuning it for
after indexing is resolved, so §3 lands mechanically complete with its tests
and its quality pass is explicitly out of scope here.

## 0. The principles this order is built on (owner, 2026-08-27)

1. **Progressive disclosure by tab.** The first Search tab is the universal
   surface — it searches everything and its acceptance test is literal: *an
   8-year-old finds her homework.* Files, Mail and Code are power surfaces
   and keep the `/` grammar and expert wording. Same engine, different
   contracts per surface.
2. **Every behaviour is configurable; every constant is justified; nothing is
   hidden.** Helpful behaviours ship **always-on** and each is individually
   switch-off-able — users mature, and nobody is forced in a direction. The
   line: behaviours a user can *perceive* get a switch; internal constants
   stay demoted per `WORKORDER-everything-tunable-has-a-ui.md`.
3. **Safety invariants are not configurable**: never delete user files, never
   send data off the machine, always show when search is degraded. Drawn on
   purpose, here, so nobody debates it later.
4. **Existing labels and descriptions never change** — same rule as the
   index-tuning order. New controls follow plain words; old ones move
   verbatim.
5. **Every control's tooltip states its effect** — what pressing it does and
   what happens at the limit, in plain words. This is already the house style
   (`indexing_settings.py`: "the wording on these controls is the feature");
   this order makes it a rule with a test (§7).

## 1. The policy seam — one place where surfaces differ

- [x] **1a** (Search) `SearchPolicy` dataclass passed with every query:
  `relax_on_empty` (bool), `typo_correction` ("auto" | "suggest" | "off"),
  `notice_register` ("plain" | "technical"), `auto_chips` (bool),
  `recency_blend` (bool), `version_folding` (bool). The engine reads policy;
  no view ever branches on which tab it is. Each field is backed by a
  Setting (owner's principle 2) with per-surface defaults: everything on for
  the Search tab, conservative for Files/Mail/Code.
- [x] **1b** the per-surface defaults are themselves visible in Settings as a
  small grid (surface × behaviour), and **"Reset search behaviour to
  defaults"** is one click — the sibling of Index Tuning's "Return to
  automatic". Support-at-a-distance depends on this button.

## 2. Tab one — the universal surface (the 8-year-old test)

- [x] **2a Typo tolerance.** A typed word matching nothing in `fts5vocab`
  gets edit-distance-1/2 candidates from the vocabulary (bounded exactly as
  wildcards are). Policy `typo_correction="auto"`: the best candidate joins
  the query and the result header says so in plain words ("also looked for
  'volcanoes'"); `"suggest"`: a did-you-mean chip instead. The single
  highest-value item in this order — a child who gets zero results for
  "volcanoe" concludes her essay is gone.
- [x] **2b Visible relaxation on empty.** Zero results with ≥2 content terms:
  drop the rarest term, re-run, label the page ("nothing matched all your
  words — showing results without 'the'"). Never silent (principle 3 of the
  codebase: silent rewriting loses trust — the *label* is what makes this
  legal). Policy-gated; on for tab one.
- [x] **2c Plain-words notices.** A notice register: every `Notice` code maps
  to a plain sentence for `notice_register="plain"` surfaces ("Finding
  things by meaning is off right now — results are word-matches only") and
  keeps today's wording for technical surfaces. One table, tested for
  coverage of every notice code.
- [x] **2d Recency blend + version folding.** A mild recency prior on the
  fused score (measured against `evaluate --builtin` before accepting), and
  near-duplicate results folded into one row — newest shown, "N older
  versions" expandable. Folding keys on the chunk-dedup hashes the
  index-tuning order introduces (its 6e); if 6e was closed as not-built,
  fold on `content_hash` at file level instead. This is where the
  fifteen-years-eight-versions corpus stops embarrassing the results page.
- [ ] **2e First-contact details.** Search is the default tab on launch; the
  box placeholder is a plain example sentence; an empty, focused box offers
  the user's own recent searches (from the existing usage log, newest
  first, off-able); result rows on tab one are generous — thumbnail where a
  preview exists, filename, folder, when; Enter opens the document.

## 3. The translator — works with Ollama, works without it

Built now, tuned later (see Status). The seam already exists: translation
produces the fixed filter grammar, the parser validates, the user sees and
corrects. Ollama is one backend; this adds the second.

- [x] **3a Rules backend** (`app/search/translate_rules.py`, beside
  `translate.py`): deterministic slot-filling against vocabularies the index
  already owns — capitalised tokens matched to sender/recipient names via
  `distinct_values` → `from:`/`to:`; verb list (sent, emailed, received,
  attached…) → mail scope and from/to disambiguation; noun map (report,
  spreadsheet, photo, deck, invoice…) → `type:`; date phrases ("before
  2023", "last summer", "in June") → `after:`/`before:`. Residual words pass
  through untouched. Pure, Qt-free, testable — no model, no network.
- [x] **3b Chips, not rewrites.** Extracted filters appear as removable chips
  the user accepts with one click (`auto_chips` policy: on for tab one,
  behind Interpret elsewhere). The typed text is never altered — the chips
  sit beside it. High precision by construction (the name either is in the
  sender list or no chip fires); a wrong chip costs one click.
- [ ] **3c Composition with Ollama.** When Ollama is present, rules run
  first and the model receives only the residue — smaller prompts, faster
  answers, and the deterministic part stays deterministic. When absent, the
  Interpret button remains, powered by rules alone; nothing on screen
  changes but the depth of what gets extracted.
- [x] **3d** quality pass **deferred by the owner** to after indexing work:
  the vocab/verb/noun/date tables land with obvious contents and their
  tests; tuning recall on real sentences is a later, separate effort. Leave
  a `[TUNE]` marker on each table.

## 4. The Code tab — searching the way coders search

- [x] **4a Paste-an-error routing.** A query that looks pasted rather than
  typed — contains `:` + parentheses, quote pairs, `Traceback`, or a
  path:line shape — routes to verbatim substring matching over the existing
  trigram index, punctuation preserved, no syntax required. The result
  header names the mode ("exact match"); one click returns to normal
  search. This is the most common coder search and today tokenisation
  breaks it.
- [x] **4b Open in editor at line.** Code results open in the configured
  editor at the line (`code -g file:line` for VS Code; the command is a
  Setting with detection, same pattern as converters), plus a copy-
  `path:line` action. Reveal-in-Explorer stays for documents.
- [x] **4c Definition boost.** A hit on the line *defining* the symbol
  (`def`/`class`/`function`/assignment patterns per language, from the
  existing code_types machinery) ranks above hits that merely use it.
  Measured against `evaluate` code sentences before accepting.
- [x] **4d `/changed <text>` — the pickaxe.** Surface `git log -S` through
  the existing git layer: commits where the string was added or removed,
  shown with message, author, date. Nothing else on the market does this
  well, and the answer carries the *why* that grep cannot.

## 5. Finding by how people actually remember

- [x] **5a More like this.** Right-click on any result → nearest neighbours
  by the document's own vectors. Everything required already exists; this
  is the feature that makes semantic search tangible.
- [ ] **5b Attachments as first-class results.** `/has attachment` plus an
  attachment-primary result row (the attachment is the object, its message
  is the context, open-attachment is the default action) — people remember
  "the file Dave sent", not the subject line.
- [x] **5c Numbers.** Measure first: do "40000", "40,000" and "£40k" find
  each other across spreadsheet cells and document text? Record the answer
  here; build normalisation only if the measurement shows the gap.

## 6. UI polish that carries the rest

- [x] **6a Tooltips state the effect — everywhere.** Every interactive
  control in every view gets a tooltip saying what it does and, where
  relevant, what happens at the limit. Enforced like accessible names: a
  test walks the widget tree and fails on any control without one
  (excluding pure display widgets), and a review pass rewrites any existing
  tooltip that names a mechanism without naming its effect — allowed
  despite principle 4 because tooltips are help text, not labels; labels
  still never change.
- [x] **6b Scalable text.** Replace hardcoded pixel font sizes (17 rules,
  theme.py) with point sizes or a scale factor honouring Windows display
  scaling — the one accessibility item the 25 Aug review left partial, and
  it matters for every older relative this product now targets.
- [ ] **6c** the remediation order's §4 UI items (M10 modal storms, M11
  preview decode, M12 rerank sync, M19 screen-reader text) are
  prerequisites of "world class" but stay tracked THERE — this order does
  not duplicate them; do not tick anything twice.

## 7. Tests

- [x] **The 8-year-old scenarios**, as integration tests on the fixture
  corpus: misspelled single word finds the document (2a); over-specified
  sentence with a wrong word still lands via relaxation, and the label is
  asserted (2b); plain sentence with a known sender name produces the chip
  (3a/3b); every scenario runs with Ollama absent.
- [ ] Policy: each `SearchPolicy` field provably changes engine behaviour
  (the anti-P1 wiring rule); per-surface defaults assert the owner's matrix.
- [ ] Notices: every notice code has a plain-register sentence (table
  coverage test).
- [x] Tooltips: the §6a walker test.
- [x] Relevance gates: 2d recency and 4c definition boost each accepted only
  with `evaluate` numbers recorded in this file — a ranking change without
  its measurement is not done.

## Note on 2a, added 2026-08-27 (delivery finding — the item text is unchanged)

**"volcanoe" never reaches the spelling code, and the child finds her essay
anyway.** FTS5 is configured with porter stemming, so "volcanoe" and
"volcanoes" both stem to "volcano" and match the document directly. Measured
against a one-document index:

| typed | `unmatched_terms` | corrected? |
|---|---|---|
| `volcanoe` | `()` | no — stemming already matched it |
| `volcanoes` | `()` | no — same |
| `volcanno` | `('volcanno',)` | yes → `volcano` |
| `homwork` | `('homwork',)` | yes → `homework` |

This does not weaken 2a, it narrows it usefully: **everything that reaches the
correction is a miss stemming has already failed on**, which is exactly the
population where inventing a different word is safe. The order's canonical
example simply lands one layer lower than the order assumed. `tests/unit/
test_spelling.py` therefore exercises `volcanno` and `homwork`; a test written
around `volcanoe` would have passed for the wrong reason and proved nothing.

Known limit, from the same session: candidates are fetched by prefix (three
letters, then two), so a typo in the **first two letters** finds nothing —
`vlocano` is not corrected. Widening it means scanning far more vocabulary on
every keystroke, and the common typo is a transposition or doubled letter
later in the word.

## Note on 2b, added 2026-08-27 (delivery finding — the item text is unchanged)

**"Drop the rarest term and re-run" cannot work, and what replaced it is
closer to the item's own intent.** Plain words are already joined with `OR`
(`AND_TERM_LIMIT = 1`, itself chosen against twenty real sentences), measured:

| typed | expression | results |
|---|---|---|
| `volcano zzzqqq` | `"volcano" OR "zzzqqq"` | 1 |
| `zzzqqq wwwxxx` | `"zzzqqq" OR "wwwxxx"` | 0 |

So zero results from several plain words means **every one of them matched
nothing**. There is no rarest term to drop — dropping any leaves words that
have already failed — and the re-run would return the same nothing under a
label claiming something had been done.

What actually empties a page that had something to find is a **narrowing
instruction**, and there are exactly three:

| typed | what narrowed it | relaxed to |
|---|---|---|
| `"volcano flavoured bread"` | the phrase (every word, adjacent, in order) | its words |
| `volcano AND bread` | an `AND` the person typed | `OR` |
| `volcano type:pdf` | a filter that excluded everything | the filter dropped, one at a time |

Each is dropped widest-effect-first, capped at two extra passes, each labelled
in plain words ("Nothing matched with the file-type filter applied — these
ignore it"). The item's requirement is met exactly: nothing is silent, and the
label is what makes the re-run legal. **The all-words-unknown case is left
alone deliberately** — it has nothing to relax towards and is already answered
by 2a's spelling correction and the unmatched-terms notice.

Also delivered here: `SearchEngine._retrieve`, because relaxation runs the
same pipeline twice and a second copy of it would be two pipelines that drift
apart.

## Note on 2d, added 2026-08-27 (delivery finding — the item text is unchanged)

**The relevance gate did its job: the guessed weight was twice too strong.**
Recall@1 over the twenty built-in sentences, through the real engine (keyword
and fusion; the embedding model cannot load in this environment, which is the
same pipeline `evaluate --builtin` measures):

| weight | recall@1 | moved | lost | gained |
|---|---|---|---|---|
| off | 14/20 (70%) | – | – | – |
| 0.01 | 14/20 (70%) | 0 | 0 | 0 |
| 0.02 | 14/20 (70%) | 0 | 0 | 0 |
| **0.03** | **15/20 (75%)** | 1 | 0 | 1 |
| 0.04 | 13/20 (65%) | 3 | 2 | 1 |
| 0.06 | 13/20 (65%) | 3 | 2 | 1 |
| 0.10 | 9/20 (45%) | 7 | 6 | 1 |
| 0.30 | 7/20 (35%) | 11 | 8 | 1 |

`WEIGHT = 0.06` was written into the source on judgement alone before this
ran. **The band that helps is narrow and there is a cliff at 0.04.** The one
sentence 0.03 fixes is *"what did I send to Priya"*, which returned an invoice
template and now returns the mail actually sent to her — exactly the case the
feature is for, where the topic words tie and the date decides. 70% matches
what `evaluate --builtin` reports, which is the cross-check that the harness
measures the same thing the shipped command does. A test re-runs both halves,
so the number cannot decay into a claim.

**Version folding could not use 6e's hashes.** The index-tuning order's chunk
dedup is in-run only — it never persisted a per-chunk hash — so this took the
order's own stated fallback and folds on `content_hash` at file level. That
alone is not enough: an exact hash only catches a file copied to two folders,
never eight *versions*, which is the case the item names. So folding has two
grounds and they are not equally safe:

* **identical bytes** — a fact, folds anywhere, "1 identical copy elsewhere";
* **the same document edited** — a guess, fenced by same folder, same
  extension, and a **version marker** (`v2`, `final`, `draft`, `rev 3`, `(2)`,
  a date) in at least one name.

**A bare trailing number is deliberately not a marker**: `chapter 1.docx` and
`chapter 2.docx` are two documents, and folding them would hide half a book.
`results` is never shortened — `folds` describes how to draw the same list, so
a view that ignores it draws the page it drew before, and nothing is hidden
from anything that reads the results.

Also from this section: `f.content_hash` now travels on the result rows (one
more column on a join both retrievers already did), the recency blend is off
wherever `/newest` is in force, and an undatable file counts as ancient rather
than new — the other way round is the sentinel bug that hid every PST, moved
into the ranking.

## Note on 2e, added 2026-08-27 (delivery finding — the item text is unchanged)

**Four of the five sub-items were already built**, and one of them asks for a
change this order's own principles forbid. Verified rather than assumed, and
each is now pinned by a test so it cannot quietly stop being true:

| sub-item | state |
|---|---|
| Search is the default tab on launch | **already true** — added first, nothing selects another |
| Enter opens the document | **already true** — `activated` covers Enter and double-click |
| result rows show filename, folder, when | **already true** — the delegate draws all three |
| the box placeholder | **already there, and must not change** — see below |
| an empty box offers recent searches | **new**; rules built and tested, attaching it deferred |

**The placeholder is the interesting one.** 2e asks for "a plain example
sentence". There is already a placeholder, argued where it is set: it names
`/` and three real filters, and it is the only thing on screen saying the box
reaches mail and repositories at all. Replacing it breaks **principle 4 of
this same order — existing labels and descriptions never change**. The
principle wins. The test pins that a placeholder exists, not its wording,
so somebody who *means* to improve it still can.

**Recent searches: the rules are built and tested, the attachment is not.**
`app/ui/first_contact.py` turns logged rows into a short list — deduped
case-insensitively but shown as typed, slash commands excluded (a slash
command is a mechanism, not a memory), six of them. `workers.recent_searches_
async` does the fetch, because `test_no_store_call_outside_a_worker` caught
the first version reading `searches` on the UI thread while a box was being
focused, and was right to.

What is **not** done, and why it was not done blind: the box already carries
the `/` command popup, and hanging a second dropdown off the same `QLineEdit`
is exactly the change that produces two popups fighting over one keystroke.
That cannot be verified in a headless environment, so it is left for the
owner's machine. Same reason for the **thumbnail** half of "generous rows" —
the only genuinely missing piece of that item.

## Note on 3a/3b/3d, added 2026-08-27 (delivery finding — item text unchanged)

**Two rules were silently inert on the first pass, and nothing raised.**
`translate_rules` asked the store for kinds called `from`, `to` and `type`.
Those are the *operator* names; the store's are `sender`, `recipient` and
`ext`, and `distinct_values` returns `[]` for a kind it does not know. So no
person chip ever fired — and worse, the "only offer a type the corpus actually
holds" safeguard passed everything, because the set it checked against was
always empty. The output looked plausible either way. Running it against the
fixture corpus is what showed it; two tests now pin both halves.

Also corrected before shipping: **`recipient` is stored as JSON**
(`'["me@acme.com"]'`), so every recipient lookup matched nothing until it was
unpacked; and the sender/recipient rule read *"the report Dave sent me"* and
*"what did I send to Priya"* the same way. The verb cannot separate those —
word order can, and a first-person pronoun before the sending verb is now what
means "I sent it".

**3b lives on the presenter side, and that is a rule rather than a
preference.** The first version put chips on `SearchResponse`, which tripped
`test_the_search_engine_cannot_reach_the_translator` — `engine.py` may not
know translation exists, because the retrieval path must never be able to
spend a second on a model. **That is the second time in this order a module
named `translate*` was imported into the engine, and the guard was right both
times.** Chips are something said *about* a query rather than part of running
one, so they sit with the translator where the Interpret button already is.
The policy still decides (`auto_chips`), so no view carries a rule of its own.

Cost, measured before wiring it in: **0.5ms median over 4,000 files** — three
indexed `DISTINCT`s with a `LIMIT` — against a 300ms budget.

**Deliberately not read: vague dates.** "about six months ago", "last summer",
"a while back" mean different spans to different people, and a wrong date
filter hides documents silently — the exact failure this order exists to
remove. Bare years, `before`/`after` a year, month names and "last year" are
read; the rest stay as words, where they cost nothing.

**3c is not done.** Composing with Ollama means `translate.py` handing the
residue to the model instead of the whole sentence. `Reading.residue` exists
and is tested for exactly that purpose, so the seam is ready — but the change
belongs in `QueryTranslator`, and it is worth doing when the owner's deferred
tuning pass (3d) happens, against a machine that has Ollama to measure with.

## Note on 5c, added 2026-08-27 (the measurement the item asked for)

**Measured first, as instructed. The gap was total, so it was built.** Five
documents naming the same amount, and what each query found before:

| document text | indexed as | found by |
|---|---|---|
| `total 40000 for the site` | `40000` | `40000` |
| `awarded at 40,000 pounds` | `40` · `000` | `40,000` |
| `we agreed £40k for the package` | `40k` | `40k`, `£40k` |
| `Invoice total: 40,000.00 GBP` | `40` · `000` · `00` | `40,000` |
| `roughly forty thousand` | words | `forty thousand` |

**No query found more than two of the five, and `40000.00` found nothing at
all** — not even the document containing its exact characters, because
`40,000.00` is three tokens and `40000.00` is one.

After: every numeric form — `40000`, `40,000`, `40k`, `£40k`, `40000.00` —
finds all four of the numeric documents. The words-in-full document is a
separate problem and is not covered.

**Query-side, so no re-index is needed.** The documents are already tokenised;
what changes is the question. `"40000" OR "40 000" OR "40k"` reaches all of
them, and `40 000` is an FTS5 phrase — exactly what a comma-separated number
became when it was indexed. It rides the wildcard expansion mechanism, so the
FTS builder needed no new rule and a query with no number in it costs nothing.

Three guards, each because the alternative is a menace:

* **A year is not a quantity.** `2024` must not drag in `2 024`.
* **Below a thousand there is nothing to expand**, and above a ceiling a long
  number is an order reference, not an amount.
* **`40,000` reaches the engine as `40` and `000`** — the parser splits on
  punctuation and cannot know the comma was inside the number. Reassembled on
  a trailing group of exactly three digits, and the absorbed `000` is removed
  from the query, or it would match every document containing a `000`.

Corrected before shipping: `1.5m` read as one million, because the decimals
were dropped before the multiplier was applied — a wrong answer that looks
like a right one.

## Note on 4a, added 2026-08-27 (delivery finding — item text unchanged)

**The trigram index this item routes to does not exist for content.**
`files_fts` is trigram but covers file *names* and folders; `chunks_fts` — the
text — is `porter unicode61`, token-based, and cannot answer a substring query
at all. Verified in `schema.sql`: those are the only two FTS tables.

True verbatim matching over content needs a second FTS table with a trigram
tokeniser over every chunk — **a schema change, a full re-index of the whole
corpus, and roughly the same storage again**. That is a decision with a real
cost and it belongs to the owner, so it is written down here rather than taken
inside a delivery.

**What shipped preserves order instead of punctuation**, which is the half
that finds the line: the distinctive thing about a traceback is the sequence
of its words, not its colons. Measured on a four-document corpus where two
files hold the line:

| | found |
|---|---|
| today, as a bag of ORed words | 4 of 4 — including both irrelevant files |
| as a phrase, order preserved | 2 of 2 — exactly the files that have it |

Needs no re-index, and it is announced (`NOTICE_EXACT`) with the way back,
because a phrase search is a narrowing and every narrowing here has to be
visible. Detection is deliberately hard to trigger: a named tell (`Traceback`,
`engine.py:512`) fires on its own, everything else needs length *and* two
independent signs of being code. A query that already carries quotes or a
filter is left alone — they said what they wanted.

**And it uncovered a crash in released code.** `_fts_quote` expands a
camelCase word into `("getUserName" OR ("get" AND "user" AND "name"))`, which
is correct in a term position and is not legal inside an adjacency chain: `+`
joins strings and nothing else. So **typing `"getUserName handler"` into the
box made FTS5 answer `syntax error near "+"` and failed the whole search.**
Phrases now quote plainly. The narrower reading is also the right one —
quoting is the most explicit statement of intent the box offers — and the
document side of camelCase is already covered by the `symbols` column.

## Note on 4d, added 2026-08-27 (already built — verified, not rebuilt)

**The pickaxe shipped with the git-search work and nobody ticked it.** Run
against this repository's own history rather than assumed:

    /changed AND_TERM_LIMIT
    -> git log --find-renames -GAND_TERM_LIMIT
       "every commit that touched it, in the last 2,000 commits"
       6 commits, each with message, author and date

The whole family is there: `/introduced` and `/removed` use `-S`,
`/changed` uses `-G`, `/lifecycle` combines them, and `/file-history` follows
renames. `test_changed_uses_G_rather_than_S` already documents the distinction
the item glosses over — **`-S` is "the number of occurrences changed", `-G` is
"the diff mentions it"** — and "every commit that changed this" wants the
second.

Nothing to build. The box was simply never ticked.

## Note on 5b, added 2026-08-27 (half of it, and the measured reason)

**`evaluate --builtin` scores its attachment question at 0%**, and the filter
was never the problem. `has:attachment` is parsed, is consumed by
`storage/filters.py`, and works — but **no plain sentence had ever produced
it**. It was reachable only by typing the operator, which is exactly the
knowledge tab one exists not to require.

Closed by the rules translator: *"emails with something attached about the
licence"*, *"the message that had an attachment"* and *"mail with attachments
from Chris"* now all offer `has:attachment` as a chip. Deliberately narrow —
*"the attached report"* is somebody naming a document and *"I attached the
wrong file"* is a sentence inside a message, and neither produces the filter.

The rest of 5b — an attachment-primary result row, where the attachment is the
object and its message is the context — is a result-row change and is not
done. It needs the window to judge, like 2e's thumbnails.

## Note on 4c, added 2026-08-27 (measured, as the item requires)

**BM25 cannot see the difference between defining a thing and using it**, and
a declaration loses because it appears once while every caller repeats the
name. Measured on five files, one declaring `SearchEngine`:

| | before | after |
|---|---|---|
| `app/cli.py` — uses it twice | 1 | 2 |
| `app/ui/shell.py` — uses it three times | 2 | 3 |
| `tests/test_engine.py` — uses it twice | 3 | 4 |
| `docs/DESIGN.md` — prose, mentions it twice | 4 | 5 |
| **`app/search/engine.py` — DECLARES it** | **5** | **1** |

Weight sweep, rank of the declaring file: 0.00 → 5th, 0.02 → 4th, 0.05 → 2nd,
0.10 → 1st, 0.20 → 1st, 0.40 → 1st. **0.40 shipped**, comfortably above the
0.10 threshold, because a corpus with a wider score spread needs the headroom.

**The false-positive cost is weight-independent, which is why a large weight
is safe here.** The boost *partitions* the list — everything that declares the
symbol, then everything that does not — and preserves the fused order inside
each part. So a comment the pattern wrongly matched can rise above the
non-declaring files but can never outrank a true declaration that scored
higher. Measured with a stale note reading "we used to have a class Governor
here": it lands second at every weight from 0.10 to 0.80, never first.

**Fires only on a single symbol-shaped word**, so a sentence never reaches it.
An ordinary English word does reach it and costs nothing, because no
declaration pattern can match in prose — cheaper than trying to tell code from
English in a search box.

**And the ordering bug it exposed.** Placed above `recency.blend`, the boost
did nothing at all at any weight: `blend` re-sorts from `rrf_score`, which the
definition boost deliberately does not write to, so the reorder was computed
and then discarded. **Two rankers in sequence and the second silently threw
away the first** — the order they run in is the whole behaviour, and a test
now pins it by source order rather than by outcome, since an outcome test
passes for the wrong reason whenever the scores happen to agree.

## Note on 5a, added 2026-08-27

**"Everything required already exists" was right, and that was the problem.**
The passage was embedded at index time, `VectorStore.search` takes a raw
vector, `vector.hydrate` turns rows into results — and there was no way to
*ask*. Semantic search has been in this product since Layer 4 and has never
once been something a person could point at.

`SearchEngine.similar_to(chunk_id)` closes it. **No query, so no keyword half
and no fusion**: there is no text to match, the question is "what else is like
this", and the answer is the neighbours of one point ranked by distance.

Two things it will not do. The source chunk is never its own neighbour — it is
trivially the closest thing to itself. And the rest of its own file is dropped
by default, because "more like this" answering with the next paragraph of the
same document is a correct answer to a question nobody asked (`same_file=True`
asks for it).

**Read back, not re-embedded.** `VectorStore.vector_for` is new: `search`
strips `vector` from every row, so this was the only missing piece. Embedding
the passage again would cost a model load and — if the model changed since
indexing — produce a vector that does not live in the same space as the rows
it is about to be compared against.

**The bug worth recording**: the first version looked the source chunk up with
`store.chunk_by_id`, a method this store has never had, *inside a `try`*. So
it failed on every call, the file id stayed 0, and the same-file exclusion
silently did nothing. A guard that turns a typo into a quietly disabled
feature is this codebase's recurring defect; a test now pins the call shape.

**Not done**: the right-click that invokes it. That is a menu in the results
view and needs the window to judge, like 2e's thumbnails and 5b's attachment
row. The engine method is tested against a real LanceDB table and waiting.

## Note on 4b, added 2026-08-27

**Detection follows the converters, and for the written-up reason.**
`shutil.which` alone is not enough on Windows: several editors never put
themselves on `PATH`, so a machine with VS Code installed and working would
report nothing found and offer to install software already present. That exact
bug is documented in `extract/converter.py` for LibreOffice; this is the same
lookup with an editor table.

**Only installed editors are offered**, so nothing in the list can be chosen
and then fail on every click — the converter rule again. Ten are known, ordered
by how likely somebody searching a code index is to have them rather than
alphabetically, because the first one found wins. Each spells the line
differently (`code -g {path}:{line}`, `subl {path}:{line}`, `notepad++
-n{line} {path}`, `idea --line {line} {path}`, `vim +{line} {path}`), which is
why there is a table rather than one template.

**Two settings**, both through the registry and all four guards: the choice,
and a free-text command with `{path}` and `{line}` that wins over it — an
editor this has never heard of should be a line of configuration, not a
feature request. A custom command with no `{line}` still opens the file:
landing on line one of the right file is worth having.

**Kept out of the six-behaviour box on purpose.** That box's reset button
promises to restore exactly those six, and its tooltip says what it will not
touch; an editor choice is not a search behaviour and must not be swept up by
it. `test_settings_reachable`'s surface table records that reasoning.

**Not done**: the menu item that invokes it, and the copy-`path:line` action
beside it — both are the results view's context menu, which needs the window
to judge. `editors.command_for` and `editors.copyable` are tested and waiting,
the same shape as 5a's engine method.

## Note on 6b, added 2026-08-27

**Seventeen rules, exactly as the item says.** Qt scales a pixel size for DPI
and does *not* scale it for the "Make text bigger" accessibility setting — so
somebody who had turned their text up got a window that ignored them.

**The multipliers are derived, not chosen.** At 96 DPI one point is 4/3 of a
pixel and the Windows default font is 9pt, which is 12px. The existing scale
of 12/13/15px is therefore **1.0, 1.083 and 1.25 times the system font**, so:

| | at 9pt (default) | at 18pt |
|---|---|---|
| small — secondary and metadata | 9.0pt = 12px | 18.0pt |
| body | 9.8pt = 13px | 19.5pt |
| large — the two headlines that earn it | 11.2pt = 15px | 22.5pt |

A default machine therefore sees the window it has always seen, and a machine
with larger text set gets larger text. Guarded three ways: no `px` font size
may reappear in the sheet, the default reproduces today's sizes, and the three
keep their order at every scale — a scale that collapses at some sizes is not
a scale.

A font set in pixels reports `pointSizeF() == -1`, which is a real state on
some Linux themes; left alone that negative would have gone into all seventeen
rules.

## Note on 6a, added 2026-08-27 (measured before the guard was written)

**Already the house style, which is the good news and the reason to pin it.**
Counted across `app/ui`: **116 actionable controls, 103 already carrying a
tooltip.** The thirteen without were all text boxes, twelve of which have a
*placeholder* — which is better than a tooltip, because it needs no hover and
survives a touch screen.

**Exactly one control in the whole window had neither**: the "file the
converter produces" box, which had a default value instead. A default says
what shape the answer takes and nothing about what happens if you get it
wrong — and getting it wrong there means the conversion runs, produces a file,
and nothing is indexed, because the file that was looked for is not there.
That is now said.

The guard accepts a tooltip **or** a placeholder, deliberately: demanding both
would put two labels saying the same thing on the search box, which is text
nobody reads twice. `QLabel` is not a control — a tooltip on a sentence,
explaining the sentence, is noise. The allow-list is empty and a test keeps it
short, because a long one is a guard that has been argued down rather than met.

**No review pass was needed.** The item allows rewriting an existing tooltip
that names a mechanism without naming its effect; reading them, the wording
already does the second thing — `indexing_settings.py` opens by saying "the
wording on these controls is the feature", and it holds.

## Note on §7, added 2026-08-27 — and the bug the scenarios found

**Writing the acceptance test found that §2a did not work.**

The scenario is the order's own sentence: a child types a misspelled word and
finds her essay. It failed. `parsed.terms` said `volcano`, the notice said
*"also looked for 'volcano'"* — and `fts_match()` still emitted `"volcanno"`,
so **the search that ran was the misspelled one and returned nothing.**

`to_fts_match` builds its expression from `or_groups` when there is one and
falls back to `terms` only when there is not. `dataclasses.replace(parsed,
terms=...)` therefore changed the field the UI reads and left the query
untouched. Both halves of the feature reported success and the page was empty.

**I had verified the mechanism and not the outcome.** The §2a delivery check
printed `terms=('volcano',)` and the notice text beside `results=0`, and I
read the first two and explained the third away as the vector half being
absent. The scenario test asserts what a person would see — *her essay is in
the list* — which is why it caught what a field check could not.

Fixed at the source: `query.with_terms` replaces terms **and** the single
`or_groups` entry together, and both §2a and §2b's phrase relaxation go
through it. Several OR groups are left alone deliberately — which alternative
a corrected word belongs to is a guess, and guessing there would be the same
silent wrongness somewhere subtler.

**The scenarios run with no model at all.** The fixture's embedder raises on
any call, so a scenario that only passes on a machine with Ollama or an
embedding model fails here instead of passing for the wrong reason. A test
asserts that guard is live.

Also pinned by the scenarios: two misspelled words are deliberately *not*
corrected (one is a typo, two is the wrong corpus), and the same query on the
Code tab corrects nothing, relaxes nothing and offers no chips.

## Done means

Each ticked item: change + tests + `pytest tests -q` green + committed by
name; ranking changes carry their `evaluate` numbers in this file. The order
is done when the 8-year-old scenarios pass with Ollama absent, every
behaviour they exercise has a visible switch, and no control anywhere lacks a
tooltip that says what it does. §3's `[TUNE]` markers surviving to the end is
expected, not a failure — tuning is the owner's next agenda item after
indexing.
