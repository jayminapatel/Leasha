# Glossary

**Doc version:** 1.0 · **Updated:** 2026-08-30 · **Applies to:** app v0.3.3

The words this project uses, and what they actually mean here. Written because every
one of them was previously inferred from context on each new session, and several
mean something narrower than they do elsewhere.

Where a term is defined by code rather than by prose, the file is named. **That file
is the authority, not this table** — if the two disagree, the code is right and this
document is stale.

---

## 1. Layers

Eleven were numbered; three are dead. The live nine are what `HANDOFF.md` §3 counts.

| Term | Means | Notes |
|---|---|---|
| L0 | Foundation — config, `AppError`, logging, single-instance, CLI | `app/core` |
| L1 | Storage — SQLite/FTS5 and LanceDB, migrations | `app/storage`. Schema `CURRENT_VERSION = 16` |
| L2 | Extraction — PDF, Office, plaintext, Outlook/PST, chunking | `app/extract` |
| L3 | Indexing pipeline — walker, workers, resumable cursor | `app/index` |
| L4 | Search — BM25 + ANN, RRF fusion, rerank, filters | `app/search` |
| L5 | UI shell | `app/ui`. PyQt6 |
| L6 | ~~Knowledge graph~~ | **Removed.** The name survives in `LOCAL_KNOWLEDGE_GRAPH_V2.md` and in a session title; there is no knowledge graph |
| L7 | ~~Office document builder~~ | **Cancelled.** Never requested, never started |
| L8a | Natural-language query translation | `app/llm` + `app/search/translate*.py` |
| L8b | Prose answers over results | **Deferred** until L8a has been used in anger |
| L9 | Hardening and packaging | **Not started.** Gated by the five `[FINALISE]` decisions in `ORDER_REGISTER.md` §5 |
| L10 | ~~Adaptive tuning~~ | **Cancelled.** Speculative |

---

## 2. Work orders

| Term | Means | Notes |
|---|---|---|
| Work order | A `docs/WORKORDER-*.md` file. The formal instruction channel from owner to build thread | Never created unprompted |
| Order ref | The 12-digit stamp in the filename, e.g. `202626270326` | Not a date you can parse — treat it as an opaque identifier |
| Queue letter | `0a`, `0b` … `0s`. The sequence position | Defined in `ORDER_REGISTER.md` §2. Letters skip: there is no `0m` in the released queue |
| `**Status:**` | Header line: DRAFT / RELEASED / ACTIVE / SHIPPED / HELD / PARKED / SUPERSEDED | Vocabulary in `ORDER_REGISTER.md` §1 |
| Gap-schedulable | Small and self-contained; may be slotted into any gap without disturbing the queue | — |
| `[FINALISE]` | A decision inside a DRAFT order that cannot be answered from the code | Five of them gate L9 |
| `[TUNE]` | A threshold deliberately left to be measured later | — |
| Tangent guard | The rule that an idea not in the order being executed belongs to another order or to the owner | In each order's header |

---

## 3. Review findings

| Term | Means | Notes |
|---|---|---|
| H1 – H11 | High findings from `REVIEW-2026-08-26.md` | 11 of them. H1 = skipped files re-extracted on every run; H2/H3 = migration repairs; H4 = vector failure kills the whole search; H10 = `.doc` conversion dead while Settings reports it working |
| M1 – M20 | Medium findings, same review | 20 of them |
| "the review" | Unqualified, means `REVIEW-2026-08-26.md` | `REVIEW-2026-08-25.md` is the earlier one; eleven of its forty-two findings were still live at the later review |
| Adoptions | The seven ideas taken from the five-AI comparison | Order `202626271137` |

---

## 4. Search

| Term | Means | Notes |
|---|---|---|
| RRF | Reciprocal Rank Fusion — combines keyword and vector result lists by rank, never by score | `app/search/fusion.py`. `RRF_K = 60` |
| Lane | One retrieval channel feeding the fusion — keyword, vector, and later CLIP and people | — |
| BM25 | The keyword ranking function, via SQLite FTS5 | — |
| ANN | Approximate nearest neighbour, over LanceDB | — |
| Relaxation | Loosening a query that returned nothing, **visibly** | `app/search/relax.py`. Silent relaxation is a defect |
| Folding | Collapsing near-duplicate results into one row with a count | `app/search/folding.py`. Version folding for documents; burst folding for photos |
| Notices | Structured warnings attached to a `SearchResponse` — degraded lane, capped expansion, relaxed query | **Open defect: the CLI prints these; the window does not draw them** |
| Translation | Turning a sentence into filter syntax, once, before the search, always shown and editable | Never in the retrieval path |
| Catalogue | The set of `/` commands offered as you type | `app/search/commands.py` |
| Stem | FTS5 stores Porter stems, not words — `invoic`, not `invoice` | Why suffix wildcards are approximate: `*voice` also reaches `invoicing` |

### The `/` operators

Verified against `app/search/commands.py`.

`type` · `from` · `to` · `subject` · `has` · `after` · `before` · `path` · `repo` ·
`name` · `sort` · `size` · `saved`

Planned by released orders, not yet built: `on` (offline volume name), `who` (person),
`shows` (image tag), `place`, `changed`.

---

## 5. Indexing and extraction

| Term | Means | Notes |
|---|---|---|
| Walk | The filesystem traversal that finds candidate files | `app/index/walker.py` |
| Root | A folder the user has told Leasha to index | Name-based exclusions prune folders found *during* a walk, never a folder named directly as a root |
| Cursor | The persisted position in a run, written *before* it is needed | A crash costs seconds, not hours |
| Name-only | A file indexed by filename and metadata, with no content read | `stats.name_only`. Correct for encrypted files and cloud placeholders |
| Skip ledger | The record of files that could not be read, with the reason | One bad file never halts a run |
| Settle | Recognising an unchanged file by hash so it is not re-extracted | H1 is the finding that this was not happening |
| Chunk | A unit of text sent to the embedder | SQLite `chunks` is the authority; LanceDB is derived and rebuildable |
| Segment | A labelled span within a document — a spreadsheet cell, an email header, an AI-written tag | AI-written segments are always marked as such |
| Held pass | Work deferred to a later run rather than done now | The shape the enrichment backlog takes |
| Enrichment backlog | The single idle-drain queue for unembedded chunks, pending OCR, undescribed images and, later, video | Currently three ad-hoc mechanisms; unifying them is in order `202626270511` |

---

## 6. Errors

`app/core/errors.py` is the authority. 35 codes registered.

| Term | Means |
|---|---|
| `AppError` | One error, fully described: code, plain-English message, suggestion, details, action type |
| `guard()` | The boundary wrapper. Never raise a bare exception across a layer boundary |
| `ERROR_REGISTRY` | Where every code and its message live. New codes go here, never inline |
| `ActionType` | What is expected of the user: `AUTO_FIX`, `USER_RETRY`, `RUN_COMMAND`, `SKIP_CONTINUE`, `NONE` |
| `SKIP_CONTINUE` | This item is skipped, the batch carries on. **Expected in the thousands; not a failure** |
| `NONE` | Reported for the log's sake, nobody need act — a search abandoned because the window closed |

---

## 7. Offline Media

The category, and the reason the whole picture strand exists. Orders `202626270513`
and `202626270514`.

| Term | Means | Notes |
|---|---|---|
| Offline Media | Anything catalogued and then disconnected. Searchable while unreachable | Renamed from "removable drives" once it generalised |
| Source | One catalogued thing — a drive, a share, a cloud mount, a phone, an archived box | User-named: "Photos 2009" |
| Kind 1 | Removable drives | Identity: volume GUID plus hardware serial |
| Kind 2 | Network locations | Identity: normalised UNC path. **Credentials never touched** |
| Kind 3 | Cloud sources | Through the vendor's own desktop mount only. No OAuth, no network code |
| Kind 4 | Phones | MTP object protocol, not the walker |
| Kind 5 | Manual / archived source | Catalogue then detach: "on tape B-0042, archived Mar 2024" |
| Snapshot | A dated scan of a source | Shown on rows and results: "scanned 12 Nov" |
| Placeholder | A cloud file present by name only | **Reading its bytes downloads it.** `cloudstub.py` is the guard |
| Hydrated / dehydrated | Content present locally / evicted by the OS | Dehydration is neither deletion nor modification |

---

## 8. Pictures

| Term | Means | Notes |
|---|---|---|
| OCR ladder | The gate deciding whether an image is worth reading. Rung 0 metadata routes, rung 1 thumbnail stats, rung 2 detector-only probe | The question is "contains readable text?", never "document or photo?" |
| Detector-only probe | Running the OCR engine's detection stage without recognition | Zero boxes → "no text found (checked)", a truthful state, not an error |
| CLIP lane | Image embeddings as a third fusion lane | Pays three times: wordless search, more-like-this, burst folding |
| pHash | Perceptual hash, for cross-source duplicate detection | Milliseconds at index time |
| Era hint | An approximate date for scans with no EXIF | From folder names, or set per batch in the Photo Tagger |
| Photo Tagger | The page where the user names face clusters | Detection and clustering automatic; **identity only ever from the user**. Off by default |

---

## 9. Machine and configuration

| Term | Means | Notes |
|---|---|---|
| `DATA_PATH` | Where the index lives. Set in `.env` by the installer | Never inside the project folder |
| `LOCATION_KEYS` | The three keys with no default — `DATA_PATH`, `PROJECT_PATH`, `LOG_PATH` | "Restore defaults" removed them once, on 2026-08-26, and the app could not start at all |
| `EMBED_MODEL` | The embedding model. Changing it means a rebuild, not a restart | Deliberately excluded from per-role model assignment |
| `EMBED_DIM` | Vector width, read from `.env` | Note: `ERR_MODEL_LOAD`'s check never opens the vector table |
| ComputeProfile | Detected machine envelope driving Auto-tune | Order `202626270114` |
| `doctor.py` | Verification with remediation. Must print READY | — |
| Diagnostic bundle | `app.cli diagnose` → one zip in `logs\diagnostics\` | Attach it rather than describing the symptom |

---

## 10. Doctrine — phrases used as shorthand

| Phrase | Means |
|---|---|
| "The eight-year-old test" | The first Search tab must let a child find her homework. The benchmark user, not a metaphor |
| "Nothing fails silently" | Every degradation is visible **in the interface the user actually uses**, not merely detected |
| "If we can index, we will index" | Leasha is a lens, not a censor. No content policing, no format deny-lists |
| "Libraries before converters" | Non-negotiable #12. A subprocess converter only where no maintained wheel exists |
| "Everything tunable has a UI" | Non-negotiable #11. A value with no control becomes a constant with a comment |
| "Measure, do not assume" | "It feels fast" is not a result |
| "Load-bearing tests" | The eight guard tests in `WORKORDER-CONVENTIONS.md` §0. Each was written after the thing it prevents had already happened |
| "Working version first" | Structural refactors wait until the feature orders are done. Bug fixes and measured performance work are exempt |
