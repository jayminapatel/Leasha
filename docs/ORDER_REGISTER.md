# Work order register

**Doc version:** 1.5 · **Updated:** 2026-09-13 · **Applies to:** app v0.3.3

Every work order in one table, with its status and where it sits in the queue.

Before this document existed the queue lived in `HANDOFF.md` §"What is Next" and each
order's own `**Status:**` line pointed back at it — a circular register, and by
2026-08-30 the HANDOFF half had gone stale while shipped work was still listed as
next. **This table is now the register.** `HANDOFF.md` links here and does not
restate it.

**Progress numbers are counted from the checkboxes in each order**, not asserted.
Regenerate them with:

```powershell
venv\Scripts\python.exe -m app.cli orders     # if built; otherwise the shell one-liner in §4
```

---

## 1. Status vocabulary

| Status | Means |
|---|---|
| `DRAFT` | Written, not authorised. Decisions inside it are still open. Nobody may start it |
| `RELEASED` | Authorised by the owner. It will be built, in queue order |
| `ACTIVE` | Being built now. At most one order should hold this |
| `SHIPPED` | Every item ticked and committed. Kept for the record |
| `HELD` | Authorised in principle, parked behind a named condition |
| `PARKED` | Written, not authorised, no condition set. Revisit deliberately |
| `SUPERSEDED` | Replaced by a later order, named in the row |

**Two conventions this table introduces, because their absence is what let the queue
drift.** First, every order carries a `**Status:**` line in its header — 25 of the 42
did not on 2026-08-30 and are marked *(inferred)* below until backfilled. Second,
`RELEASED` must be able to become `SHIPPED`; before this document there was no
completion state at all, so a finished order looked exactly like a waiting one.

---

## 2. The queue — released and active work, in order

The sequence is the owner's, carried from `HANDOFF.md` §"What is Next" as it stood on
2026-08-28 and corrected against the checkbox counts.

| # | Ref | Order | Status | Done/Open | Blocked by |
|---|---|---|---|---|---|
| 0 | `202626082352` | Remediate the 2026-08-26 review | RELEASED *(inferred)* | 53 / 5 | Three of the five are structural splits, deferred by "working version first" |
| 0a | `202626270046` | Context-aware `/` menu, GUI and CLI | SHIPPED | 25 / 0 | Was queued behind review §2 (H5, H6, H11) |
| 0b | `202626270114` | Index Tuning — one screen, three modes | RELEASED | 35 / 5 | — |
| 0c | `202626270157` | The search experience — one box for an 8-year-old | **SHIPPED** | **26 / 0** | Closed 2026-09-07 |
| 0d | `202626270257` | Privacy defaults | **SHIPPED** | **9 / 0** | Was already complete; status corrected 2026-09-07 |
| 0e | `202626270326` | Workspace features — pop-outs, viewers, tools | **SHIPPED** | **30 / 0** | Closed 2026-09-07 by §5c |
| 0f | `202626270508` | Media by default, and the OCR ladder | **SHIPPED** | **17 / 0** | Closed 2026-09-07 by §3a's display/sort wiring and §2e's tunable |
| 0g | `202626270509` | mbox, Takeout, chats | **SHIPPED** | **7 / 0** | Was already complete; status corrected 2026-09-07 |
| 0h | `202626270510` | Pictures I — the CLIP lane | RELEASED | 12 / 1 | Its last item's proof is **gated on the real CLIP model** — see the order's 2026-09-07 note for the one command that closes it |
| 0i | `202626270511` | Pictures II — tags, enrichment, places | RELEASED | 0 / 14 | 0h |
| 0j | `202626270512` | The Photo Tagger — naming people | RELEASED | 0 / 13 | 0h |
| 0k | `202626270513` | Offline Media I — drives in drawers | RELEASED | 0 / 17 | Deepest storage change — no interleaving |
| 0l | `202626270514` | Offline Media II — network, cloud, placeholders | RELEASED | 0 / 17 | 0k |
| 0n | `202626270602` | Reports, and the Life Timeline | RELEASED | 0 / 15 | After 0l, before 0m |
| 0p | `202626271317` | Every table sorts, every header sits over its column | **SHIPPED** | **17 / 0** | Was already complete; status corrected 2026-09-07 |
| 0q | `202626271510` | The results, world class | RELEASED | 20 / 5 | Gap-schedulable; 4a and 3c's line number await an owner decision (see the order's own dated notes) |
| 0r | `202626271601` | The splash, and a fast lifecycle | RELEASED | 17 / 1 | §2b partially closed 2026-09-07 (Mail/Code deferred; Files/Indexing/Settings still up-front) — one item open |
| 0s | `202626271137` | The seven adoptions — five-AI review | **SHIPPED** | **17 / 0** | Closed 2026-09-07 |
| 0t | `202626270611` | The Chat tab — ask your archive, and every answer has receipts | **ACTIVE** | 3 / 23 | Promoted 2026-09-13 (was HELD) — see `WORKORDER-scope-change-search-and-chat.md`'s reopening note. Photo lanes (0h-0j) not yet landed, so built against text only for now. §1a (router), §1b (the loop) and §1d (aggregate counting) closed same day |

**2026-09-13 — order 0's "Relevance" item closed, 52/6 → 53/5.** The recency
half was already shipped in 0c; the filename-match half did not exist. Built
as `app/search/name_match.py`, measured against the twenty built-in sentences
the same way `recency.WEIGHT` was (14/20 off, 16/20 on), and wired in behind
its own `SEARCH_FILENAME_MATCH_BLEND` setting. Chaining it after `recency.
blend` in sequence turned out to silently discard whichever ran first - see
the item's own dated note in `WORKORDER-202626082352-review-remediation.md`
for why, and `engine._blend_recency_and_filename` for the fix. The other five
open items in that order were checked individually rather than assumed
buildable: `schema.sql`'s seed version is a documented "closed on
measurement, not built" decision, `P8` needs `evaluate --builtin` on the
owner's own machine, and the remaining three are the structural splits
already deferred by "working version first".

**2026-09-07 — recounted, and six orders are now finished.** The previous set of
numbers was written on 2026-08-30 and had gone stale within days: 0f, 0g, 0h, 0p and
0q had all moved, and three orders standing at zero open were still listed as
waiting. That is precisely the drift this register was created to end, so these
counts come from §4's command rather than from editing the table by hand.

Finished in that session: **0c** (§6c — a cross-reference, closed by verifying its
four referenced items line by line in the remediation order rather than duplicating
them), **0e** (§5c, the DWG simplified view) and **0s** (the pytest-qt sweep across
all seven adoptions). **0d, 0g and 0p** were already at zero open and needed only
their status corrected — exactly the "finished or lying" case §4 warns about.

**0h is deliberately left at 12 / 1.** Its last item asks that a photo be found by
typing a description of it. The test is written against the real wiring and runs, but
the real CLIP model cannot be fetched in the build sandbox — `huggingface.co` answers
403 through the proxy, and disk was *not* the constraint. Ticking it would assert
something nobody has run, so the order carries the exact command that closes it on a
machine which can reach the model.

**2026-09-07, second pass — 0f closed, 0r nearly.** Four more parallel lanes, run the
same way as the first pass. **0f** (media by default, and the OCR ladder) went from
15/2 to 17/0 and is now `SHIPPED`: lane-a closed §3a's remaining "and any date display
use it" clause — the shot date now reaches both the SELECT projections
(`keyword.py`, `vector.py`'s image-search hydrate site too, which the item's own
wording had missed, `sqlite_store.py`) and the `/newest`/`/oldest` sort order
(`recency.py`, `folding.py`), with the SQL shaped by measurement rather than the
obvious `COALESCE` form, which a 200k-row `EXPLAIN QUERY PLAN` showed cost 39.5ms
against 0.10ms for the form actually shipped. Lane-b closed §2e's remaining tunables
half: `OCR_WHITE_PAGE_PERCENT` is now a real setting with a plain-words label, wired
into `ocr_ladder.py` and surfaced on the Index Tuning screen's Coverage group,
default-identical to the old hardcoded `0.7` cutoff.

**0r** (the splash, and a fast lifecycle) went from 15/3 to 17/1. Lane-c found the
pytest-qt splash checklist item had gone stale the same way 0c/0e/0d/0g/0p did on the
first pass — 1b's fade and minimum-hold fix landed 2026-09-05 but the §4 test item's
own note was never revisited after — and, checking honestly rather than assuming
either way, found three of six clauses genuinely still unproven; wrote one composed
scenario covering all six and ticked it. Lane-c also wrote the missing "stub-slowed
stage" test and found a real bug doing it: `app/main.py` was importing the search and
storage stack — `app.search.vector`, `app.ui.shell`, `app.extract.ocr` among them —
before `splash.show()`, contradicting both `main.py`'s own comment and `splash.py`'s
module docstring. Fixed. Lane-d closed §2b partially: Mail and Code tab construction
now defers to after `window.show()` via `QTimer.singleShot(0, ...)`; Files, Indexing
and Settings still build up front, flagged as a follow-up rather than attempted in
one pass given the file's size and risk. Measured in the build sandbox only —
662ms→180-410ms constructor time — with the owner's own <1.5s wall-clock target still
needing verification on the real machine; the order carries the exact reproduction
command. §2b stays unticked, honestly, and is 0r's one remaining open item.

Both orders' full diffs were verified against the pre-lane commit (`d042f09`) before
merging — additive only, no reversions — and the two pipeline.py/doctor.py test
failures surfaced during the post-merge sweep were independently reproduced against
that same unmodified commit, confirming they predate this session's work.

---

## 3. Held, draft, parked and shipped

| Ref | Order | Status | Done/Open | Note |
|---|---|---|---|---|
| `202626270547` | Test automation — the GUI clicked for real | HELD | 0 / 18 | Queue letter **0m**, referenced by 0n's sequencing. §0 is the `widget.grab()` utility, first item on release |
| `202626082213` | Install and distribution | DRAFT | 0 / 0 | **Five `[FINALISE]` decisions open.** See §5 |
| `202626270238` | Migrate PyQt6 → PySide6 | DRAFT | 0 / 10 | Structural — deferred behind the working version |
| `202626270515` | Video and audio | DRAFT | 0 / 4 | The epoch after pictures |
| `202626271328` | The pages reorg | DRAFT | 0 / 13 | Promotion condition: 0114 + 0157 fully ticked |
| `202626081052` | OCR where it pays | PARKED *(inferred)* | 10 / 7 | Largely superseded by `202626270508`'s ladder |
| `202626081149` | The Code tab | PARKED *(inferred)* | 0 / 0 | — |
| `202626081801` | Git sharpness and mail preview | PARKED *(inferred)* | 0 / 0 | — |
| `terabyte-scale` | Indexing 600GB, heading for 1.5TB | HELD *(inferred)* | 16 / 6 | — |
| `owner-pst-scale-run` | The PST scale run | HELD *(inferred)* | 0 / 4 | Owner-run, not code |
| `libraries-before-converters` | Libraries first, converters only where none exists | SHIPPED *(doctrine)* | 0 / 8 | Now non-negotiable #12 |
| `everything-tunable-has-a-ui` | Every tunable has a UI | SHIPPED *(doctrine)* | 0 / 6 | Now non-negotiable #11 |
| `zip-archives` | Every file findable, reading inside `.zip` | SHIPPED | **39 / 0** | — |
| `202626081106` | Wildcards without growing the index | SHIPPED | **29 / 0** | — |
| `file-types-and-ocr` | Config-driven file types, format editor | SHIPPED | 21 / 1 | — |
| `scope-change-search-and-chat` | Scope change — plain-English local search | SHIPPED | 8 / 1 | The decision that defines the product |
| `202626081439` | A reset must leave nothing behind | SHIPPED *(inferred)* | 0 / 0 | — |
| `202626081059` | Search does not do what a person expects | SHIPPED *(inferred)* | 0 / 0 | — |
| `inbound-ui-fixes` | Three finished UI fixes | SHIPPED *(inferred)* | 0 / 0 | — |
| `git-search-backend` | Repository awareness | SHIPPED *(inferred)* | 0 / 0 | Phase 1 only; phase 2 (history) **not authorised** |
| `git-search-ui` | The Code tab | SHIPPED *(inferred)* | 0 / 0 | — |
| `ui-shell-and-results` | Results layout, preview, tray | SUPERSEDED | 0 / 0 | By `202626271510` |
| `CONVENTIONS` | How work orders work now | — | — | Not an order; the rules for orders |

---

## 4. Regenerating the numbers

The Done/Open columns are checkbox counts. From the repo root:

```powershell
Get-ChildItem docs\WORKORDER-*.md | ForEach-Object {
  $t = Get-Content $_ -Raw
  $d = ([regex]::Matches($t, '(?m)^\s*[-*]\s*\[[xX]\]')).Count
  $o = ([regex]::Matches($t, '(?m)^\s*[-*]\s*\[ \]')).Count
  '{0,-58} {1,4} {2,4}' -f $_.Name, $d, $o
}
```

A row whose Open count is zero and whose status is still `RELEASED` or `ACTIVE` is
either finished or lying, and either way somebody should look.

---

## 5. The five open packaging decisions

`202626082213` is the only DRAFT holding decisions nobody can answer from the code.
Listed here because they gate Layer 9 and therefore any release.

1. PyInstaller one-folder versus uv with an embedded Python.
2. Per-user versus per-machine install.
3. The index default location — `%LOCALAPPDATA%\Leasha` is correct for privacy but
   sits on C:, and a 100GB corpus wants 150GB free.
4. The update mechanism.
5. The minimum supported Windows version.

**A sixth decision has moved underneath this order and was never revisited.** It was
written while the repository had no `LICENSE`, and concluded that free code signing
through SignPath — which requires a public codebase under a recognised OSS licence —
was therefore unavailable. `LICENSE` now exists and is MIT. The signing question is
open again.
