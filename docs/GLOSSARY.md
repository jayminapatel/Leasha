# Glossary

**Doc version:** 1.14 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3

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
| L1 | Storage — SQLite/FTS5 and LanceDB, migrations | `app/storage`. Schema `CURRENT_VERSION = 39` (`app/storage/migrations.py`) |
| L2 | Extraction — PDF, Office, plaintext, Outlook/PST, chunking | `app/extract`; local models in `app/ort` |
| L3 | Indexing pipeline — walker, workers, resumable cursor | `app/index` |
| L4 | Search — BM25 + ANN, RRF fusion, rerank, filters | `app/search`; read-only reports in `app/reports` |
| L5 | UI shell | `app/ui`. PySide6 6.11 (Qt's own binding); signals are `Signal`, and `shiboken6` stands where PyQt had `sip`, through `app/ui/qtsip.py`. The terminal prompt is `app/shell` |
| L6 | ~~Knowledge graph~~ | **Removed.** The name survives in `LOCAL_KNOWLEDGE_GRAPH_V2.md` and in a session title; there is no knowledge graph |
| L7 | ~~Office document builder~~ | **Cancelled.** Never requested, never started |
| L8a | Natural-language query translation | `app/llm` + `app/search/translate*.py`. Runs on ONNX Runtime inside Leasha by default (`CHAT_ENGINE=onnx`, `app/llm/engines.py`), Ollama as the alternative |
| L8b | Prose answers over results | **Built:** the Chat tab, `app/chat`. Same engine choice as L8a |
| L9 | Hardening and packaging | **Built** for the owner's own use: the Windows installer in `packaging/` (order `202626082213`). Not distributed to anyone else |
| L10 | ~~Adaptive tuning~~ | **Cancelled.** Speculative |

---

## 2. Work orders

| Term | Means | Notes |
|---|---|---|
| Work order | A `docs/WORKORDER-*.md` file. The formal instruction channel from owner to build thread | Never created unprompted |
| Order ref | The 12-digit stamp in the filename, e.g. `202626270326` | Not a date you can parse — treat it as an opaque identifier |
| Queue letter | `0a`, `0b` … `0z`, then `1a`, `1b` …. The sequence position | Defined in `ORDER_REGISTER.md` §2. Letters skip: there is no `0m` or `0o` in the released queue |
| `**Status:**` | Header line: DRAFT / RELEASED / ACTIVE / SHIPPED / HELD / PARKED / SUPERSEDED | Vocabulary in `ORDER_REGISTER.md` §1 |
| Gap-schedulable | Small and self-contained; may be slotted into any gap without disturbing the queue | — |
| `[FINALISE]` | A decision inside a DRAFT order that cannot be answered from the code | Five of them held L9; all are decided |
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
| Notices | Structured warnings attached to a `SearchResponse` — degraded lane, capped expansion, relaxed query | The CLI prints them; the window draws them in a line above the results (`app/ui/widgets/notice_bar.py`) |
| Translation | Turning a sentence into filter syntax, once, before the search, always shown and editable | Never in the retrieval path |
| Catalogue | The set of `/` commands offered as you type | `app/search/commands.py` |
| Stem | FTS5 stores Porter stems, not words — `invoic`, not `invoice` | Why suffix wildcards are approximate: `*voice` also reaches `invoicing` |

### The `/` operators

Verified against `app/search/commands.py`.

`type` · `from` · `to` · `subject` · `has` · `status` · `after` · `before` · `date` ·
`between` · `path` · `repo` · `on` · `name` · `sort` · `size` · `shows` · `place` ·
`who` · `only` · `saved`

`status` is the indexing state, on every tab; `date` and `between` take a date range;
`on` is an offline volume name, `who` a person, `shows` an image tag. `type` also takes
`image`, `photo` and `picture`. Planned by a released order, not yet built: `changed`.

---

## 5. Indexing and extraction

| Term | Means | Notes |
|---|---|---|
| Walk | The filesystem traversal that finds candidate files | `app/index/walker.py` |
| Root | A folder the user has told Leasha to index | Name-based exclusions prune folders found *during* a walk, never a folder named directly as a root |
| Cursor | The persisted position in a run, written *before* it is needed | A crash costs seconds, not hours |
| Name-only | A file indexed by filename and metadata, with no content read | `stats.name_only`. Correct for encrypted files and cloud placeholders |
| Skip ledger | The record of files that could not be read, with the reason | One bad file never halts a run |
| Junction | A Windows folder that is really a link to another folder (`mklink /J`). Not a symlink to Python, so `os.walk` follows it | Never followed by the walker, the scan or the folder watch since 2026-10-08; counted as `directory junction (not followed)` |
| Settle | Recognising an unchanged file by hash so it is not re-extracted | H1 is the finding that this was not happening |
| Chunk | A unit of text sent to the embedder | SQLite `chunks` is the authority; LanceDB is derived and rebuildable |
| Segment | A labelled span within a document — a spreadsheet cell, an email header, an AI-written tag | AI-written segments are always marked as such |
| Held pass | Work deferred to a later run rather than done now | The shape the enrichment backlog takes |
| Enrichment backlog | The work left after a file is indexed, done when the machine is idle | One drain loop, `Pipeline._run_enrichment_drains` (order `202626270511`): it drains unembedded chunks, and video and audio through `app/index/media_backlog.py` (order `202626270515`). Files waiting for OCR are re-queued by the walker instead; untagged images are declared (`KIND_UNTAGGED_IMAGE`) but not drained yet |

---

## 6. Errors

`app/core/errors.py` is the authority. 83 codes registered.

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
| Kind 1 | Removable drives | Identity: volume GUID plus hardware serial on Windows; on macOS the volume UUID, stored as `macos-volume:<UUID>` (`app/core/osbridge/volumes.py`) |
| Kind 2 | Network locations | Identity: normalised UNC path. **Credentials never touched**. Windows only |
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

---

## 11. Accounts, contacts and sync

Order 1k. The code is the authority: `app/accounts` (sign-in, the sync engine, live mail),
`app/contacts` (the hub and its tools) and `app/ui/accounts`, `app/ui/contacts`.

| Term | Means | Notes |
|---|---|---|
| Account | One connection to a provider, in the person's own name for it ("Work", "Home"): Google, Microsoft, Apple (iCloud), or any IMAP or CardDAV server. Any number of each. It can bring in mail (read only), contacts, or both | `accounts` table; Settings > Accounts. Its secrets are in Windows Credential Manager, never in a column |
| Account worker | The one background thread that talks to every account, one at a time per provider. It runs only while Leasha is open | `app/accounts/engine.py`, `service.py`; started by the schedule or Sync now, nothing else |
| App password | A password made at the provider for one program (iCloud, Gmail over IMAP, Yahoo, Fastmail ...), so the real password is never given to Leasha | Used by Apple, IMAP and CardDAV accounts; kept in Credential Manager |
| Bin | Where a deleted contact waits for 30 days. Deleting a contact in Leasha also removes it from every account it is kept in; restoring it from the Bin writes it back | Contacts sidebar, **Bin** |
| Changed in two places | A contact whose same field was edited in Leasha and at an account (or at two accounts) since the last sync. Under the default rule the newest edit wins, the other value stays in History, and the contact carries a marker until the person presses Acknowledge | `ERR_SYNC_CONFLICT_HELD`; Contacts sidebar, **Changed in two places**; Settings > Accounts, **When the same contact changed in two places** |
| Client ID | The code that identifies the sign-in registration a person makes in their own name at Google or Microsoft. Leasha ships with none | Settings > Accounts, **Sign-in set-up**; the user guide walks through making one |
| Contact | One person (or company) in Leasha's contact book: typed, repeatable fields (email, phone, address, website, dates, related people, handles), a photo, notes, groups, tags and the accounts it is kept in | `contacts`, `contact_fields`; `app/contacts/card.py` |
| Delete brake | The stop on a sync that would delete more contacts than the safety limit (20 contacts or 5% of the book by default). Nothing is deleted until the person presses Allow | `ERR_SYNC_DELETE_BRAKE`; Settings > Accounts > **Safety** |
| Deleted elsewhere | A contact an account no longer has. Leasha and the other accounts keep it; it is listed for the person to look at, never deleted on a hunch | Contacts sidebar, **Deleted elsewhere**; `contact_links.state = 'deleted_remote'` |
| Direction | What a contact sync may do with one account: **Two-way** (read it and write to it; the default for every account, work accounts included), **Into Leasha only** (read it, never write), or **Off** | Settings > Accounts, the account's **Contacts** tab |
| Master account | The account whose contact list is the master: what it deletes is deleted from Leasha's book (into the Bin) and from the other accounts, behind the delete brake. Leasha's own book is the master *copy*; this chooses whose deletes count | Settings > Accounts > Keeping contacts in step; `master_account_id` |
| Mirror | Keeping every contact in every account that takes contacts back, so an add or an edit in any one reaches all | `mirror_contacts` |
| Shared number | A phone number or email address held by different people (an office landline, a family mailbox); it never matches contacts to each other | `contacts:shared_values`; "Not a duplicate" |
| Dry run | A sync that reads and plans and writes nothing: not Leasha, not the account, not the saved position. The first sync of an account is one until the person presses Approve, when the option "Dry run before an account's first sync" is on (it is off by default) | `ERR_SYNC_NEEDS_APPROVAL`; `contacts sync --dry-run` |
| Enrichment | Proposed additions to a contact (a phone number from a signature, a company from an email domain, a photo from a named face, and three optional internet look-ups: Gravatar, a company's own icon, Wikidata for companies). Nothing is kept until the person accepts it, and each internet source is off by default | `app/contacts/enrich/`; hosts pinned in `app/accounts/http.py` |
| Hub | Leasha's own master copy of the contact book. Accounts are copies that are kept in step with it, not the source | `ContactHub`, `app/contacts/hub.py`; SQLite is the authority (rule 6) |
| Journal | The record of every change to a contact (who made it: Leasha, an import, or an account) with what was there before, so any change, a whole merge, a clean-up or a whole sync can be undone | `contact_journal`; a contact's **History**; `contacts history`, `contacts undo` |
| Kept in | The set of accounts a contact is stored in. A contact made in Leasha goes to the default account; one that came from an account is kept in that account; nothing is copied everywhere unless the person asks. Adding an account writes the contact there; taking one away deletes it from that account only after the person confirms | `contact_links`; the editor's **Kept in** chips |
| Live mail | Mail fetched from an account over the internet, read only, and indexed like any other mail. Mail read from files and Outlook archives stays with the indexing schedule | `app/accounts/mail_sync.py`; Settings > Accounts, the account's **Mail** tab |
| Quiet hours | Two times between which no scheduled sync starts. Sync now still works | Settings > Accounts |
| Smart group | A saved filter over the contact book ("no phone number", "company is Acme") that is always up to date | Contacts sidebar, **New smart group** |
| Sync log | The table of every sync: when, which account, what started it, what was added, changed, deleted and held, how long it took, and any problem with its fix. **Undo this sync** is here | Settings > Accounts, **Sync log**; `contacts log` |
| Tag | A coloured label on a contact; any number per contact. Leasha's own, so it is sent to an account only where that account has an equivalent | Contacts sidebar, **Tags** |

---

## Added 1 October 2026

| Term | Means | Notes |
|---|---|---|
| Rail | The column of page buttons down the left of the window: Search, Mail, Photos, Files, Contacts, Code, Browse (the timeline), Chat, Tidy, then the Indexing pill and Settings | `app/ui/widgets/rail.py` |
| Tidy | The rail page (Reports until order 1q) for what is indexed and for clearing exact copies, similar photos, large files and leftovers on this computer. It moves a file only to the Recycle Bin, only after the plan is reviewed, never the last copy, and can put a batch back | `app/tidy/`, `app/ui/tidy_view.py` |
| Keep rule | Which file of a Tidy set stays: Leasha picks, Keep the oldest, Keep the newest, or I'll choose (nothing marked until you tick) | `app/tidy/keep.py` |
| Indexing pill | The rail's status button: "Up to date", "Indexing", "Paused", with a coloured dot | Opens the Indexing page |
| What gets read | The Indexing page that gathers every reading lever by place: files, email, attachments, zips, pictures, video and audio, code | `app/ui/widgets/what_gets_read.py`, `app/ui/presenter/coverage.py` |
| `MAIL_ATTACHMENTS` | What is read from an email attachment: `names`, `documents` (default), `pictures`, `everything` | `app/extract/mail_attachments.py` |
| Chip | A filter drawn under a box, with × to remove it. "Auto chips" are the ones read from your sentence | `SEARCH_AUTO_CHIPS`; on every tab |
| `read_box` | One reading of a box for every tab: slash commands, then `translate_rules`, then the parser | `app/ui/tasks.py` |
| `CHAT_ENGINE` | Where Chat, Interpret and Describe run: `onnx` (inside Leasha, default) or `ollama` | `app/llm/engines.py` |
| ORT | ONNX Runtime; also Leasha's package of local models | `app/ort` |
| Separate process | Indexing in a child `app.cli index --events jsonl` instead of inside the window | `INDEX_SEPARATE_PROCESS`, off by default |
| Read processes | Readers run in one child process per extraction thread | `INDEX_READ_PROCESSES`, off by default |
| Folder watching | Indexing a file seconds after it is saved, without a full run | `INDEX_WATCH_FOLDERS`, `app.cli watch`; off by default |
| Governor | What pauses a run when the machine is busy, on battery, or short of space, and stops it below the free-disk floor | `app/index/resources.py` |
| TimedOut | A file that ran out of its time limit; retried with a longer one | `ERR_FILE_TIMEOUT`, `app.cli timed-out` |
| Offline Media | Drives catalogued once and findable after unplugging; a volume is never stored by drive letter | `volumes` table, the Offline shelf of the Indexing page |

## Added 4 October 2026

| Term | Means | Notes |
|---|---|---|
| Change marker | A fingerprint from inside a file that says whether its contents moved, used where the date lies. For a `.pst`: five numbers from the header that Outlook does not touch when it merely opens the file | `email_pst.archive_marker`, stored in `files.content_hash` as `pst-header:...`; `Extractor.change_marker` |
| Entry (in Folders to index) | One line of the list: a folder, or since 3 October one file. A file entry is walked as its own folder naming only that file | `walker.walk`; "Add file…" |
| Index now | The play button on one line of Folders to index: read that entry in full, now, and nothing else | `IndexController._index_folder_now`; CLI `leasha index --no-prune --recheck-archives -- FOLDER` |
| Hardware ID | The serial of the disk itself (not the volume's), shown on the Offline list so two drives with the same label can be told apart; a share shows its address | `offline.hardware_id_words`, `remember_hardware_serial` |
| Value question | A Chat question that names something written inside a document - a number, a date, an address - and is answered by quoting it rather than listing files | `router.VALUE_NOUNS`, routed to LOOKUP |
| Preview toggle | The panel icon that shows or hides a tab's preview pane; one control on Search, Files, Mail, Code and Chat | `view_options.preview_toggle` |
| Row cell | The plain widget that holds a control placed on a row of a list; named `rowCell` so the theme lets the row show through | `buttons.put_on_row` |
| Text-safe tint | A brand colour darkened until white text on it reaches 4.5:1. The kind badges use them | Brand `brand.json` `textSafe`; `test_brand_colours.py` |
| Lockup / symbol | The brand's logo with its wordmark / the cluster alone. The About box shows the lockup; the icon is the symbol | `assets/leasha-lockup*.png`, `leasha-symbol.svg` |
| Demonstration store | The small index in `D:\Demo\leasha-guide` that the guide's pictures are taken against; never the owner's data | `tools/guide_pictures.py` |

## Added 5 October 2026

| Term | Means | Notes |
|---|---|---|
| Photos tab | The rail tab for pictures: narrowing lists on the left, the photos as Details or Small/Medium/Large thumbnails, an info panel, a full-size viewer, and People to name | `app/ui/photos_view.py`; the Photo Tagger page now opens inside it |
| `only:` | The shared switch for what a picture has: named, unnamed, no-faces, described, undescribed, text, screenshots | `ParsedQuery.only`, `storage.filters.ONLY_SQL`; the one operator not told to the translation model |
| Accept all | Yes to every waiting "Is this ...?" suggestion, for everyone or one person, after the numbers are shown | `SqliteStore.accept_all_suggestions` |
| Write names into photos | Option b: the people and the description written into a photo's own XMP, on demand only - the owner's exception to non-negotiable 10 | `app/index/photo_metadata.py`; CLI `photos --write-names` |
| Sidecar | A `.xmp` file beside a photo carrying its metadata, so the photo itself is not changed - the default for Write names into photos | Adobe's `IMG_0001.xmp`; `IMG_0001.HEIC.xmp` when two photos share a stem |
| Thumbnail cache | 320-pixel copies of pictures in `<data>/thumbs`, named by path, size and modified time, so a changed photo gets a new one | `widgets/photo_thumbs.py` |
| Tray status line | The live line in the tray icon's menu and tooltip: what indexing is doing, or how many files the index holds; clicking it opens Indexing | `presenter/tray_words.py`, fed by the same signals as the pill |
| Name date | A date read from a photo's file name when its metadata has none - a precise hint, shown "about 11 Aug 2022, 16:22" | `era_hints.guess_moment`; after EXIF, before the folder year |
