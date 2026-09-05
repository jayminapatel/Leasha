# Work order register

**Doc version:** 1.0 · **Updated:** 2026-08-30 · **Applies to:** app v0.3.3

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
| 0 | `202626082352` | Remediate the 2026-08-26 review | RELEASED *(inferred)* | 52 / 6 | — |
| 0a | `202626270046` | Context-aware `/` menu, GUI and CLI | ACTIVE | **25 / 0** | Was queued behind review §2 (H5, H6, H11) |
| 0b | `202626270114` | Index Tuning — one screen, three modes | RELEASED | 31 / 9 | — |
| 0c | `202626270157` | The search experience — one box for an 8-year-old | RELEASED | 23 / 3 | After 0b |
| 0d | `202626270257` | Privacy defaults | RELEASED | **9 / 0** | Gap-schedulable |
| 0e | `202626270326` | Workspace features — pop-outs, viewers, tools | RELEASED | 29 / 1 | After 0c |
| 0f | `202626270508` | Media by default, and the OCR ladder | RELEASED | 0 / 17 | Foundation for 0h–0j |
| 0g | `202626270509` | mbox, Takeout, chats | RELEASED | 0 / 7 | Gap-schedulable |
| 0h | `202626270510` | Pictures I — the CLIP lane | RELEASED | 4 / 9 | 0f |
| 0i | `202626270511` | Pictures II — tags, enrichment, places | RELEASED | 0 / 14 | 0h |
| 0j | `202626270512` | The Photo Tagger — naming people | RELEASED | 0 / 13 | 0h |
| 0k | `202626270513` | Offline Media I — drives in drawers | RELEASED | 0 / 17 | Deepest storage change — no interleaving |
| 0l | `202626270514` | Offline Media II — network, cloud, placeholders | RELEASED | 0 / 17 | 0k |
| 0n | `202626270602` | Reports, and the Life Timeline | RELEASED | 0 / 15 | After 0l, before 0m |
| 0p | `202626271317` | Every table sorts, every header sits over its column | RELEASED | 11 / 6 | Gap-schedulable. §5 = column widths, do last |
| 0q | `202626271510` | The results, world class | RELEASED | 0 / 25 | Gap-schedulable |
| 0r | `202626271601` | The splash, and a fast lifecycle | RELEASED | 0 / 18 | Design settled |
| 0s | `202626271137` | The seven adoptions — five-AI review | RELEASED | 12 / 5 | Gap-schedulable |

**Three rows that contradict the old HANDOFF text, and are worth reading twice.**
`202626270046` is marked ACTIVE and is **fully ticked** — it is finished, not running.
`202626270257` is likewise complete at 9/0. And `202626270326` §1, §2 and §3a shipped
in commits `aa9fb28`, `ec40c40` and `b1fdd7c`, while HANDOFF still described the whole
order as future work.

---

## 3. Held, draft, parked and shipped

| Ref | Order | Status | Done/Open | Note |
|---|---|---|---|---|
| `202626270547` | Test automation — the GUI clicked for real | HELD | 0 / 18 | Queue letter **0m**, referenced by 0n's sequencing. §0 is the `widget.grab()` utility, first item on release |
| `202626270611` | The Chat tab — answers with receipts | HELD | 0 / 26 | Deliberately after the finder is finished |
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
