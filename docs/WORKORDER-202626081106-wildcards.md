# Work order: wildcards that work, without growing the index

**Doc version:** 1.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3
**Created:** 2026-08-26 11:06 · **Layer:** L4 - `app/search/query.py`, `app/storage/sqlite_store.py`

Commit the working tree before starting.

---

## 1. Where it stands

**Trailing `*` already works** and is real FTS5 prefix matching. Two other
things people type do not, and both fail **silently**:

| Typed | Becomes | Result |
|---|---|---|
| `invoic*` | `"invoic"*` | works - invoice, invoices, invoicing |
| `proj* plan*` | `"proj"* OR "plan"*` | works |
| `*voice` | `"voice"` | **the star is dropped without a word** |
| `inv?ice` | `"inv" OR "ice"` | **`?` splits it into two terms** |

The silence is the defect. `*voice` returns hits for "voice", so it looks like a
wildcard search that worked and returned everything there was.

Filenames need none of this: `files_fts` is a **trigram** index, so `voice`
already finds `Invoice 2024.pdf` by substring. This work order is about content.

## 2. The approach, and the measurement behind it

**`fts5vocab`** - a virtual table over the terms FTS5 already stores. A wildcard
becomes a `LIKE` over the vocabulary, and the matching terms become an ordinary
`OR` query. **No new index, no re-index, no extra disk.**

Measured on this machine before writing any of this:

```
distinct terms: 399,851
LIKE %voic%  ->   8 terms in  77 ms
LIKE %voic   ->   5 terms in  78 ms
LIKE inv%    ->  18 terms in  24 ms
```

77ms for a leading wildcard over 400,000 terms. A corpus at 600GB might hold two
to five million distinct terms, so scale it to roughly 400ms-1s - acceptable for
a query somebody typed a `*` into on purpose, and **paid only when a wildcard is
present**.

### The catch that shapes the whole design

**The vocabulary holds Porter stems, not words.** Found by testing, not by
reading:

```
stored terms: ['invoic', 'novic', 'password', 'reset', 'servic', 'voic', 'voicemail']
```

So `LIKE '%voice'` matches **nothing** - the stored form is `voic`. The pattern
must be stemmed before it reaches the vocabulary.

The consequence is a semantic decision rather than a bug: **suffix wildcards
become approximate.** `*voice` will also reach `invoicing`, because they share a
stem. That is usually what somebody wants and it is not strictly what they
typed, so **it has to be said out loud in the results** - see 3.5.

### The alternative, and why not

A second FTS5 table with a `trigram` tokenizer over content gives exact
substring matching and no stemming problem. It is what makes filename search
work. But **a trigram index runs 2-5x the size of the text it indexes** - on
600GB of extracted text that is hundreds of gigabytes for a rare query. Right
for filenames, which are tiny. Wrong for content.

## 3. What to build

### 3.1 The vocabulary table

- [ ] `CREATE VIRTUAL TABLE IF NOT EXISTS chunks_vocab USING fts5vocab('chunks_fts', 'row')`
      in a migration. It is a **view over existing data** - no rows are written,
      nothing is rebuilt, and an existing index gains it instantly.
- [ ] The `'row'` form gives `term`, `doc` and `cnt`, which is what 3.3 needs for
      ordering.

### 3.2 Parsing

- [ ] `*` anywhere -> `%`, `?` -> `_`, in a `LIKE` pattern.
- [ ] Escape a literal `%` or `_` typed by the person, or a search for `100%`
      becomes a search for everything.
- [ ] A term with **no** wildcard takes none of this path. Ordinary searches must
      not get slower or stranger, and they are almost every search.
- [ ] A bare `*` or `?` alone stays as it is today: nothing, rather than
      everything.

### 3.3 Expansion

- [ ] Stem the literal fragments before matching, so the pattern meets the stored
      form. Reuse the same Porter implementation FTS5 uses, or accept the
      approximation and document it.
- [ ] `SELECT term FROM chunks_vocab WHERE term LIKE ? ORDER BY doc DESC LIMIT ?`
      - **ordered by document frequency**, so if the cap bites, the commonest
      real words survive rather than an arbitrary alphabetical slice.
- [ ] **Cap at 200 terms.** An uncapped expansion of `*a*` is every term in the
      corpus in one `OR` expression, which is how a search box becomes a way to
      hang the application.
- [ ] Build `("invoic" OR "voic" OR "voicemail")` and hand it to the existing
      query path. Everything downstream - ranking, fusion, filters, the `symbols`
      column - keeps working untouched.
- [ ] A pattern matching nothing returns no results, **not** a fallback to the
      literal text. Quietly searching for something else is the bug this work
      order exists to remove.

### 3.4 Cost control

- [ ] **Minimum two literal characters** outside the wildcards. `*a*` is a
      vocabulary scan returning everything and helps nobody - the same reasoning
      already applied to one and two-character filename queries.
- [ ] A time budget on the vocabulary lookup. If it exceeds it, return what was
      found and say the expansion was cut short.
- [ ] Cache the expansion for the session, keyed on the pattern. Somebody
      refining a query re-runs the same wildcard repeatedly.
- [ ] Wildcards are for the keyword half only. The vector half embeds the query
      text as typed; `*voice` is not a sentence and must not reach the embedder.

### 3.5 Saying what happened

**The whole point.** The current failure is silence, and an expansion that is
capped or approximate is silence with extra steps.

- [ ] `matched 8 terms: invoice, invoicing, voicemail...` under the results.
- [ ] When capped: *"more than 200 terms matched - showing the 200 most
      common"*, with the count.
- [ ] When stemming widened it: say so once, plainly. *"`*voice` also matches
      words sharing its stem, such as invoicing."*
- [ ] When nothing matched: *"no words in the index match that pattern"* - which
      is a different sentence from "no documents matched", and the difference is
      the whole diagnosis.

## 4. Tests

- [ ] `*voice` finds a document containing only `invoice`.
- [ ] `inv?ice` matches `invoice` and not `invice`.
- [ ] `pass*word` matches `password`.
- [ ] A term with no wildcard produces **byte-identical** SQL to today. This is
      the regression that matters: almost every search takes that path.
- [ ] `100%` searches for `100%`, not for everything.
- [ ] `*a*` is refused for having too little to go on, with a message.
- [ ] An expansion is capped at 200 and says so.
- [ ] A pattern matching no vocabulary returns nothing rather than falling back.
- [ ] The vocabulary table exists after migration on a database created before
      it, and the migration is re-runnable.
- [ ] **A timed test over a realistic vocabulary**, asserting the lookup stays
      inside its budget. The 77ms above is from a synthetic 400k-term index;
      re-measure against the real one once it exists and record the number here.

## 5. What must stay true

- **No re-index.** `fts5vocab` is a view over what FTS5 already stores. Anything
  requiring a rebuild at 600GB is the wrong answer to a rare query.
- **Ordinary searches are untouched**, in speed and in behaviour. The wildcard
  path is entered only when a wildcard is typed.
- **Nothing is silently dropped or silently truncated.** That is the entire
  defect being fixed; reproducing it in a new place would be worse than leaving
  it alone.
- **Bounded.** A cap on terms, a floor on literal characters, a time budget, and
  a cache. A search box that can be made to hang is not a search box.

## 6. Recorded

- **`fts5vocab` costs nothing and answers everything.** 77ms over 400k terms,
  no new index, no re-index.
- **The vocabulary is stemmed, and that is the design constraint** - found by
  testing, not by reading the documentation. Suffix wildcards are therefore
  approximate, and the UI must say so.
- **Trigram over content was the obvious answer and is the wrong one** at 2-5x
  the size of the text. Right for filenames, which is where it already is.
- **Order the expansion by document frequency**, so a cap keeps the useful terms.
- **The defect being fixed is silence.** `*voice` returning results for "voice"
  looks like a wildcard search that worked.
