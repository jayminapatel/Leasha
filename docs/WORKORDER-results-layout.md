# Work order: the search results layout

**Doc version:** 1.0 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

The owner does not like how results are presented. Asked whether the problem was the layout or
the relevance, the answer was **layout** - the right documents are turning up, they are just
badly shown.

So this changes presentation only. **Do not touch ranking, fusion, or the retrieval path.**

Commit the working tree before starting.

---

## 1. What a row looks like now

`results_view.py` builds three stacked labels per `QListWidgetItem`:

```
3. D:\Archive\2019\Projects\Leeds\...\report_final_v3.pdf
page 4  ·  keyword and meaning both matched  ·  score 0.83
...the site survey identified three hazards at the pump station...
```

Six problems, in the order they cost the user something.

### 1.1 One row per chunk, so one document can fill the page

This is the important one. Results are chunk-level, and nothing groups them, so a long PDF
matching in five places takes five of the top ten rows. The user sees three documents where
they should see ten.

### 1.2 The path is the headline, and it is truncated

`shorten_path(..., limit=70)` elides the middle - which is exactly where the distinguishing
part of a long archive path lives. A person recognises `report_final_v3.pdf`; nobody scans
`D:\Archive\2019\Projects\...`. The browser convention has this the right way round: title
first, location small and grey underneath.

### 1.3 No date

In a fifteen-year archive with eight versions of everything, the date is frequently the *only*
thing that distinguishes results. It is absent. `format_when()` already exists in
`presenter.py` - it is used for `FileRow` and simply never wired into `ResultRow`.

### 1.4 No type

Whether a hit is an email, a spreadsheet or a scanned PDF changes how you read it, and it is
currently inferable only from the file extension buried in a truncated path.

### 1.5 Rank numbers

`1. 2. 3.` restates what the ordering already says, and makes the list read like a printed
report.

### 1.6 The explain string and score sit in every row, forever

`keyword and meaning both matched · score 0.83` is genuinely valuable - being able to ask "why
is this here" is where trust comes from, and it should not be deleted. But it is the second
thing the eye lands on, on every row, for the life of the application. It belongs on demand.

## 2. The data is already being fetched and discarded

The cheap part. `keyword.py` already selects `f.ext` and `f.mtime_ns` in both queries, and the
LanceDB table already carries `ext` and `mtime_ns` columns. `SearchResult` does not have fields
for them, so they are dropped on the floor.

**Add `ext: str = ""` and `mtime_ns: int = 0` to `SearchResult`** and populate from both
retrievers. No new queries, no schema change.

Email metadata (subject, sender) does need a lookup in `messages`. **Batch it**: one query for
the `file_id`s on the visible page, not one per row. `mail_rows()` in `presenter.py` already
formats sender, recipients and date; reuse it rather than writing new formatting.

## 3. Group in the presenter, never in the engine

**`SearchEngine` keeps returning chunk-level results, ranked exactly as it does today.**
Grouping is a display concern and belongs in `presenter.py`.

This is not a stylistic preference. The measured baseline in `HANDOFF.md` §3b - 75% at rank 1
after translation - was taken against chunk-level ranking. Grouping inside the engine would
change what "rank 1" means and make every future measurement incomparable with that one. The
`evaluate` command must keep measuring the same thing.

Consequence to handle: if the fused set is 50 chunks and 30 belong to one PDF, grouping yields
few documents. **Fetch deeper than you display.** Take the existing fused limit, group, then
show the top N *groups*. Make the fetch depth a constant with a comment saying why it exceeds
the display count.

### The group

```python
@dataclass(frozen=True, slots=True)
class ResultGroup:
    file_id: int
    name: str            # filename, or the subject for a message
    folder: str          # breadcrumb, not a raw path
    kind: str            # "pdf", "email", "sheet"
    when: str            # format_when()
    rows: list[ResultRow]    # every matching chunk, best first
    best: ResultRow          # rows[0], the snippet shown collapsed
```

Rank the group by its best chunk's score. Do not average - averaging punishes a long document
that matches strongly once, which is the common case.

## 4. The row

Collapsed, one group:

```
[icon]  report_final_v3.pdf                            12 Mar 2019
        Archive > 2019 > Projects > Leeds            3 matches v
        ...identified three hazards at the pump station requiring...
```

- **Name** 15px, medium weight, primary text. Filename for a file; **subject** for a message.
- **Date** right-aligned, muted, 12px. `format_when()`.
- **Folder** as a breadcrumb, muted, 12px. Not a raw path - keep the full path in the tooltip
  and in "Copy path".
- **Match count** only when it is more than one. Clicking expands to the individual chunks,
  each with its page and snippet, and each independently openable at that location.
- **Snippet** from the best chunk, existing highlighting unchanged.
- **Icon** by kind. Tabler-style outline glyph or a short text tag - either is fine, but it
  must survive dark mode and high-DPI.

A message row substitutes `from Chris Bell · 2 attachments` for the folder line.

**Keep**: the missing-file marking. A result whose file has vanished is a real finding and the
existing behaviour of showing rather than hiding it is correct.

**Move**: `explain` and `score` to the tooltip, plus a "Why this result?" item in the existing
right-click menu. Under `Density.COMPACT` they stay hidden; a debug view option may show them
inline for people who want them back.

## 5. Preferences

`view_options.py` already has `ViewPreferences` and `Density`. Add:

- `group_by_document: bool = True` - because someone will want the flat list back
- `show_scores: bool = False`

Both persist through the existing `load_prefs`/`save_prefs`, and both belong in the existing
view menu rather than a new settings page.

## 6. Acceptance tests

Presenter tests only - `presenter.py` must still not import Qt, and the existing guard test
must stay green.

- [ ] Five chunks from two files produce two groups, ordered by their best chunk's score.
- [ ] A group's `best` is its highest-scoring chunk, and `rows` is ordered best first.
- [ ] A single-match group reports no match count.
- [ ] `group_by_document=False` returns the flat list, unchanged from today.
- [ ] A message group uses the subject as its name and sender as its subtitle, via `mail_rows`.
- [ ] A file with no `messages` row falls back to filename and folder without raising.
- [ ] `mtime_ns=0` (unknown) renders empty rather than "1 Jan 1970".
- [ ] `ext` and `mtime_ns` survive both retrievers into `SearchResult` - assert on the keyword
      path and the vector path separately.
- [ ] The full path remains available for tooltip, open, reveal and copy, even though the
      breadcrumb is shown.
- [ ] Mail metadata is fetched in **one** query for a page of results - assert the call count.
      A per-row lookup is 50 queries per keystroke at debounce.
- [ ] `SearchEngine` output is byte-identical to before this change - grouping must be provably
      display-only.
- [ ] `app.cli evaluate --builtin` produces the same numbers as `HANDOFF.md` §3b.

## 7. Do not change

- Ranking, RRF, rerank, or anything under `app/search/`, beyond adding two fields to a dataclass.
- The retrieval path's independence from `app.llm`.
- The missing-file marking.
- The existing snippet builder and its highlighting.

## 8. Order

1. `ext` and `mtime_ns` through `SearchResult`. Suite green - nothing visible changes yet.
2. `ResultGroup` and grouping in `presenter.py`, with tests. Still nothing visible.
3. Rewire `results_view.py` to draw groups.
4. Expand and collapse.
5. Mail rows.
6. View preferences and the menu.
7. **Open the window and look at it.** Every UI fault in this project so far was found by
   somebody clicking, never by a test - eleven of them. This will be no different.

## 9. Recorded

- **Layout, not relevance.** The owner confirmed the right documents are being found.
- **Grouping is display-only**, so the §3b measurement stays comparable.
- **Group score is the best chunk, not the mean** - averaging punishes long documents that
  match strongly in one place.
- **`explain` is moved, not deleted.** "Why is this here" is where trust comes from; it just
  does not need to be on screen permanently.
- **The flat list stays available** behind a preference.
