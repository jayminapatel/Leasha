# Work order (One thread): the search experience — one box for an 8-year-old, power for everyone else

**Doc version:** 1.5 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
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

- [ ] **4a Paste-an-error routing.** A query that looks pasted rather than
  typed — contains `:` + parentheses, quote pairs, `Traceback`, or a
  path:line shape — routes to verbatim substring matching over the existing
  trigram index, punctuation preserved, no syntax required. The result
  header names the mode ("exact match"); one click returns to normal
  search. This is the most common coder search and today tokenisation
  breaks it.
- [ ] **4b Open in editor at line.** Code results open in the configured
  editor at the line (`code -g file:line` for VS Code; the command is a
  Setting with detection, same pattern as converters), plus a copy-
  `path:line` action. Reveal-in-Explorer stays for documents.
- [ ] **4c Definition boost.** A hit on the line *defining* the symbol
  (`def`/`class`/`function`/assignment patterns per language, from the
  existing code_types machinery) ranks above hits that merely use it.
  Measured against `evaluate` code sentences before accepting.
- [ ] **4d `/changed <text>` — the pickaxe.** Surface `git log -S` through
  the existing git layer: commits where the string was added or removed,
  shown with message, author, date. Nothing else on the market does this
  well, and the answer carries the *why* that grep cannot.

## 5. Finding by how people actually remember

- [ ] **5a More like this.** Right-click on any result → nearest neighbours
  by the document's own vectors. Everything required already exists; this
  is the feature that makes semantic search tangible.
- [ ] **5b Attachments as first-class results.** `/has attachment` plus an
  attachment-primary result row (the attachment is the object, its message
  is the context, open-attachment is the default action) — people remember
  "the file Dave sent", not the subject line.
- [ ] **5c Numbers.** Measure first: do "40000", "40,000" and "£40k" find
  each other across spreadsheet cells and document text? Record the answer
  here; build normalisation only if the measurement shows the gap.

## 6. UI polish that carries the rest

- [ ] **6a Tooltips state the effect — everywhere.** Every interactive
  control in every view gets a tooltip saying what it does and, where
  relevant, what happens at the limit. Enforced like accessible names: a
  test walks the widget tree and fails on any control without one
  (excluding pure display widgets), and a review pass rewrites any existing
  tooltip that names a mechanism without naming its effect — allowed
  despite principle 4 because tooltips are help text, not labels; labels
  still never change.
- [ ] **6b Scalable text.** Replace hardcoded pixel font sizes (17 rules,
  theme.py) with point sizes or a scale factor honouring Windows display
  scaling — the one accessibility item the 25 Aug review left partial, and
  it matters for every older relative this product now targets.
- [ ] **6c** the remediation order's §4 UI items (M10 modal storms, M11
  preview decode, M12 rerank sync, M19 screen-reader text) are
  prerequisites of "world class" but stay tracked THERE — this order does
  not duplicate them; do not tick anything twice.

## 7. Tests

- [ ] **The 8-year-old scenarios**, as integration tests on the fixture
  corpus: misspelled single word finds the document (2a); over-specified
  sentence with a wrong word still lands via relaxation, and the label is
  asserted (2b); plain sentence with a known sender name produces the chip
  (3a/3b); every scenario runs with Ollama absent.
- [ ] Policy: each `SearchPolicy` field provably changes engine behaviour
  (the anti-P1 wiring rule); per-surface defaults assert the owner's matrix.
- [ ] Notices: every notice code has a plain-register sentence (table
  coverage test).
- [ ] Tooltips: the §6a walker test.
- [ ] Relevance gates: 2d recency and 4c definition boost each accepted only
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

## Done means

Each ticked item: change + tests + `pytest tests -q` green + committed by
name; ranking changes carry their `evaluate` numbers in this file. The order
is done when the 8-year-old scenarios pass with Ollama absent, every
behaviour they exercise has a visible switch, and no control anywhere lacks a
tooltip that says what it does. §3's `[TUNE]` markers surviving to the end is
expected, not a failure — tuning is the owner's next agenda item after
indexing.
