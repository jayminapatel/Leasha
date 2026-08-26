# Work order: search does not do what a person expects

**Doc version:** 1.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3
**Created:** 2026-08-26 10:59 · **Layer:** L4 - `app/search/query.py`, `app/search/engine.py`, the search view

**Thread:** the single merged thread

**Raised by the owner**, in these words: *"I am not happy with what the search does."* Three
queries produced that verdict. All three are reasonable, none of them works, and the reasons
are different in each case.

Every finding below was verified by running the real parser over the real query. Nothing here
is inferred.

---

## 1. The three queries

```
find a project execution plan as a word document
find a project execution plan as a word document which is the latest
files from repo starting with DF_
```

What the parser actually does with them:

| Query | `ext` | `after` | Terms searched |
|---|---|---|---|
| 1 | **none** | — | `find, project, execution, plan, word, document` |
| 2 | **none** | **none** | `… which, is, the, latest` |
| 3 | **none** | — | `files, repo, starting, with, DF` |

Three separate faults, and the third one loses a character.

## 2. This was already measured, and the measurement predicted it

`app.cli evaluate --builtin`, run this morning:

```
overall             70%
topic only          88%   (8 sentences)
with a constraint   58%   (12 sentences)
    type           100%
    date            67%
    sender          50%
    recipient        0%
    attachment       0%
```

`evaluate.py` records the prediction it was built to test: *"topic matching will be decent,
constraints will be ignored."* It was right. **The harness is not the problem and does not need
changing** - it is already telling us exactly where search fails, and this work order is the
response to it rather than a fresh investigation.

Note that run was **keyword-only**. The full pipeline needs `--rerank`, and the number to beat
should be taken in that mode.

## 3. Findings

### F1 — Thirteen instruction words are searched as document content

`find`, `show`, `get`, `give`, `search`, `looking`, `need`, `want`, `anything`, `latest`,
`newest`, `biggest`, `recent` are **not** in `_STOPWORDS` (`app/search/query.py:508`), so they
go into the FTS expression as terms.

`"find a project execution plan"` becomes `"find" OR "project" OR "execution" OR "plan"`. The
word *find* is the owner talking **to** the application, and it is being matched **against**
the corpus - in an archive of project documents it will hit thousands of files and drag the
ranking with it.

The mechanism to fix this already exists and is the right one. `_STOPWORDS` are removed from
the *keyword* expression but deliberately kept in `embed_text`, because *"report from Dave"*
and *"report for Dave"* embed differently. Instruction words want the same treatment for the
same reason: noise to BM25, harmless context to the embedder.

**Do not simply add them to `_STOPWORDS`.** Some are meaningful as content - "latest version",
a file literally named `Search.md`. A second list, `_INSTRUCTION_WORDS`, applied only when the
word is not the *only* content word left, keeps `_content_terms`' existing "unless that would
leave nothing" guard honest.

### F2 — The kind vocabulary exists, but only after a colon

`_EXT_GROUPS` (`query.py:78`) already knows twelve kind words:

```
code, doc, email, excel, mail, powerpoint, ppt, sheet, slides, text, word, xls
```

`/type excel` works perfectly. **"get me all excel files" does nothing** - `excel` sits in
`terms` and no `ext` filter is produced. The vocabulary is there; the bridge from prose to
filter is not, and that bridge is what the owner expected.

This is the same shape as the `/type` menu fault fixed in `4f2b92c`: capability built, invisible
at the point of use.

**The fix is a suggestion, not silent rewriting.** A non-negotiable says a query the application
altered must be visible and editable, because invisible narrowing makes search unpredictable.
So: when free text contains a kind word next to a document noun (`word document`, `excel file`,
`a pdf`), offer it - a chip or a status line reading *"Search **.docx** only?"* that applies
`type:docx` when clicked. One click, visible, reversible, and it teaches the grammar. Silently
filtering on a guessed word is how somebody loses a document and never knows why.

### F3 — There is no way to ask for the newest thing

The complete filter set is `type, from, to, subject, has, after, before, path, repo, name,
size`. Nothing sorts. Results are always ranked by relevance.

*"which is the latest"* is therefore **unanswerable by any current mechanism** - and Interpret
cannot rescue it either, because translation may only emit operators that exist. The word
`latest` becomes a search term (see F1) and quietly makes the results worse.

This is the one finding that is a missing feature rather than a defect. `/newest` (or `/sort
date`) re-ordering the final result set after rerank is cheap, gives Interpret something to
emit for a phrase people use constantly, and is honest about the trade: relevance order is
abandoned, deliberately, because the person asked for recency.

**Say so on screen when it is applied.** A result list silently sorted by date when the user
believes it is sorted by relevance is worse than not having the feature.

### F4 — Underscores are stripped from bare terms

`_TERM` is `[^\W_]+(?:[._'\-][^\W_]+)*\*?`. The character class `[^\W_]` explicitly excludes
underscore, and the continuation requires a word character *after* each separator. So:

| Typed | Searched for |
|---|---|
| `DF_1234` | `DF_1234` — fine |
| `DF_` | **`DF`** — the anchor is gone |
| `__init__.py` | **`init`, `py`** — both leading underscores lost |

Intra-word underscores survive; leading and trailing ones do not. `3b29b7f` just took this
application from 34 to **405** source and code types, and `__init__`, `_private`, `DF_` and
`SNAKE_CASE_` prefixes are exactly what somebody will type into a code search.

Filter values are unaffected - `/name DF_` keeps the underscore, and so does a quoted `"DF_"` -
because neither goes through `_TERM`. That is the workaround; it is not the fix.

### F5 — The degradation warning has cried wolf sixty times

```
no vector hits for a query with N keyword hits -
meaning-based search may not be working. Check: app.cli stats
```

60 occurrences in the error log. **Every one is a false alarm**, verified two ways: the store
holds 38,986 vectors of 39,306 chunks, and the two lines that mark a *real* failure -
`query embedding failed` and `ANN search failed` in `vector.py` - have **never appeared in any
log**.

The cause is at `engine.py:399`. `vector.search()` returns `[]` for three reasons, two of which
are entirely normal: nothing to embed (a filter-only query such as `type:pdf` has no free text),
and filters excluded everything. The warning fires on all three.

**A warning that is wrong sixty times out of sixty trains everyone to ignore it**, and this one
guards a genuine failure mode - the one `HANDOFF-thread-merge.md` raises as B2, still open.
Warn only when `embed_text` was non-empty and the filter set was non-empty; log the ordinary
cases at DEBUG. This must be fixed **before** B2's on-screen notice ships, or the window will
start showing users a warning that is usually untrue.

### F6 — About 6% of the corpus has no searchable content

From `app.cli stats`: 2,677 files, of which

```
FAILED     153   ERR_CONVERTER_MISSING
SKIPPED     24   ERR_NO_TEXT_LAYER
NAME_ONLY  164
```

153 files fail because a converter binary is not installed. They are findable by name and
invisible to every content search, and no amount of query tuning will surface them. Run
`app.cli formats` to name the missing binary and install it, or record the decision not to.

Separately, and already noted elsewhere: `ERR_NO_TEXT_LAYER`'s suggestion text still says OCR is
*"something V2 does not do"*, which stopped being true when `pdf.py` gained `_ocr_pages`. It is
off behind `LEASHA_PDF_OCR_PAGES` - an env var with no control, which is non-negotiable 11.
106 PDFs and 95 Visio files hit this path in the last run.

### F7 — Interpret is the designed answer to F1 and F2, and nobody reaches for it

Ollama is configured (`mistral`, `127.0.0.1:11434`). Pressing Interpret turns *"as a word
document"* into `type:docx` - that is precisely its job, and it is why translation was built.

The owner did not press it. That is a discoverability fault, not a user error: a button whose
value is invisible until pressed will not be pressed. When a query is long, has no operators
and contains a kind word, the status line should say so - *"Press Interpret to turn this into
filters"* - once, quietly, near the results.

**This does not replace F1 to F3.** Search must work with Ollama stopped; that is the first
non-negotiable. Interpret makes good queries better, it does not make bad parsing acceptable.

## 4. What to build, in order

Cheapest and highest value first. Each is independently shippable.

| # | Change | Where | Size |
|---|---|---|---|
| 1 | `_INSTRUCTION_WORDS`, stripped from FTS, kept in `embed_text` | `query.py` | small |
| 2 | Fix `_TERM` to keep leading and trailing underscores | `query.py` | one regex |
| 3 | Warn only on a real vector failure | `engine.py:399`, `vector.py` | small |
| 4 | `/newest` — re-sort after rerank, and say so on screen | `commands.py`, `engine.py`, UI | medium |
| 5 | Kind-word suggestion chip — offered, never automatic | `presenter.py`, search view | medium |
| 6 | Interpret hint when a query would benefit | `presenter.py`, search view | small |
| 7 | Install the missing converter; correct `ERR_NO_TEXT_LAYER`; give PDF OCR a control | ops, `errors.py`, settings | small |

Items 1-3 are half a day and fix the parsing faults outright. Items 4-6 are the ones the owner
will notice.

## 5. What not to do

| Not this | Why |
|---|---|
| Auto-interpret every query | Invisible rewriting is the thing the whole design refuses. Suggest, never apply |
| Silently map any kind word to a filter | "word count", "excel formula" are real searches. Offer it |
| Put an LLM in the retrieval path | First non-negotiable. Search works with Ollama stopped, or it is not this application |
| Relax the evaluate harness to make numbers look better | It predicted every one of these findings. It is the instrument, not the target |
| Add a chat box | The request is that the search box works, not that it be replaced |

## 6. Acceptance — measured, not felt

**Record the before, now, in both modes**, or item 4 cannot be argued about afterwards:

```powershell
.\leasha evaluate --builtin --rerank
.\leasha evaluate --builtin --rerank --interpret
```

Then build `docs/questions-owner.txt` - **twenty of the owner's own sentences** against the real
index, one `sentence | part-of-the-wanted-path` per line, starting with the three in §1. The
built-in corpus is 21 documents and its own output calls every number optimistic; the owner's
questions against the owner's corpus are the measurement that decides whether this worked.

```powershell
.\leasha evaluate --questions docs\questions-owner.txt --rerank
```

Required outcomes:

| | Criterion |
|---|---|
| A1 | All three §1 queries return the intended document in the top 3, **without** Interpret |
| A2 | `--builtin --rerank` overall recall@1 does not fall. Constraint categories rise |
| A3 | `find`, `show me`, `get me` prefixed onto an otherwise-working query do not change its top result |
| A4 | `__init__.py`, `DF_`, `_private` each reach the term list intact |
| A5 | The vector warning fires **zero** times across a full session of ordinary searching, and still fires when vectors are deliberately emptied |
| A6 | `/newest` reorders by date, and the result list says that is what happened |
| A7 | A kind-word suggestion is offered, never auto-applied, and one click applies it visibly |

## 7. Definition of done

1. A1-A7 pass. The whole suite passes on Windows, not the non-Qt subset.
2. Before and after numbers recorded in `HANDOFF.md` - both modes, both corpora.
3. **The owner runs their own three queries and says whether it is better.** That is the
   acceptance test this document exists for; everything above is instrumentation for it.
4. `CHANGELOG.md` appended under `[Unreleased]`. Append only.
5. `WORKORDER-CONVENTIONS.md` §7 gains a row.
