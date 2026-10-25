# Work order (One thread): contact sync that is fast and can be trusted - batched writes, one count everywhere, a check after every sync, a brake that holds only deletes

**Doc version:** 1.3 · **Updated:** 2026-10-25 · **Applies to:** app v1.1.0
**Thread:** One thread (`app/accounts/engine.py`, `app/accounts/provider.py`, `app/accounts/http.py`,
`app/accounts/providers/google_people.py`, `app/accounts/providers/graph_contacts.py`,
`app/accounts/providers/carddav.py`, `app/accounts/service.py`, `app/accounts/store.py`, `app/contacts/hub.py`,
`app/ui/accounts/`, `app/ui/contacts/loader.py`, `app/ui/contacts_tools/sync_dialog.py`,
`app/ui/presenter/sync_status.py`, `app/cli/accounts.py`, `tests/unit/`, the docs)
**Status note, 2026-10-12 (latest): MERGED to `main` as #70 (`32fc2d5`)** at the owner's "merge now", before the Windows suite had finished; the items still open below are open on `main`.

**Status note, 2026-10-12 (later): RELEASED by the owner and BUILT the same night** on branch
`feat/contact-sync-fast-and-trusted` (worktree `.worktrees/sync-fast`), owner: "recommended and i have changed the
delete limit, do recommended except one order for all and then execute this order as this should not affect
the current running do check though". D1-D6 confirmed as written; the one exception, asked and answered the
same evening, is the build order: **everything built together in one branch**, not confidence first. The
owner's app was running from `D:\Local\GitHub\Leasha` (`pythonw -m app.main`) with a sync in flight, so
all of it was built in a separate worktree and nothing was run against his accounts or written to
`D:\LeashaIndex\Data` (read-only queries only). UI changes were checked with the Design system review thread
(no conflicts; its tooltip wording and the `accounts config` flags were taken). Boxes are ticked only where a
test proves them; what needs the owner's accounts is left open and marked UNVERIFIED.

**Status:** DRAFT, written 2026-10-12 at the owner's request ("create a work order specifically design to fix both
speed and confidence"). **Not to be started until the owner releases it.** D1-D6 below are the recommended
defaults; the owner confirms or changes them when releasing.

**Where this came from.** The owner, 2026-10-12: "the contact synching feature is not perfect i think and it is too
slow compared to what i have seen and also the contact count number are different from what the app shows and
actual .. critically rethink the contact sync mechanism.. and think of how a world class system would work as this
is too slow and fails to give confidence." Then, with the counts read from each place: "oulook 1436, leasha 1442
google 1444".

**The verdict that shapes this order: keep the engine, rebuild how it talks and how it reports.** The model
built by order 1k is sound and stays - Leasha as the hub, a link per account with the last agreed copy as the
merge base, a three-way merge, resumable tokens, one account as one failure domain. What is slow is the
transport (one contact at a time, two or three requests per contact, a new connection for each). What breaks
confidence is the reporting (three definitions of "how many", a brake that freezes every write, a run that
records nothing until it ends, and an account that never settles).

---

## Measured, 2026-10-12 (read-only, `D:\LeashaIndex\Data\fts\knowledge.db`, `sync_runs` and `contact_links`)

**Speed.**

| Run | Account | Started - ended | Work | Time |
|---|---|---|---|---|
| 504 | GMail Main (Google) | 17:19:57 - 21:10:28 | 58 created, 1,099 updated, 45 deleted, 1 refused | **3 h 50 min** for four accounts |
| 505 | MS Main (Microsoft) | 17:19:58 - 21:10:28 | 52 created, 1,382 updated, 50 deleted | (same run) |
| 506 | Apple (iCloud CardDAV) | 17:20:00 - 21:10:28 | 163 updated, 11 deleted | (same run) |
| 507 | Leasha (CardDAV) | 17:20:27 - 21:10:28 | 717 created, then `WinError 10054` (connection reset), failed | (same run) |
| 489 | GMail Main | 13:52, 841 s | 183 updated, then stopped | **4.6 s a contact** |
| 485 | Apple | 12:52, 2,795 s | 58 added, 4,026 changed, **4,072 conflicts** on a 1,445-contact book | 0.7 s a contact |
| 487, 488 | GMail Main, MS Main | 13:39, 12,103 s | the app closed; the run row holds **no plan at all** | 3 h 22 min, nothing recorded |
| 470-482 | GMail Main, MS Main | hourly, nothing to do | 5-10 s each | (fine) |

Why, read from the code at `2d0accc`:

1. **One contact at a time, one account at a time.** `engine._push` walks `_dirty` serially; `sync` pushes the
   accounts one after another, so four accounts take the sum of their times, not the longest.
2. **Two or three requests per contact.** Microsoft update = `GET` (`_current`, the pre-read) + `PATCH` + `GET`
   (`_held_after_write`, the read-back). Google update = `PATCH` + `GET`, though `updateContact` already returns
   the saved person with `personFields`. Microsoft create = `POST` + `GET`; Google create = `POST` + `GET`, and
   `createContact` also returns the person. CardDAV update may also rewrite group cards (`_set_membership`).
3. **A refused Microsoft update is resent one property at a time** (`_update_piecewise`): about 25 more `PATCH`es.
4. **A new TCP and TLS connection for every request.** `UrllibTransport` opens through `urllib.request`, which
   sends `Connection: close`.
5. **One SQLite commit per contact** on a 12 GB database (`hub.set_link`, `hub.update` each open `store.write()`).
6. **No sign of throttling.** `logs/app/app_2026-10-12.log` has no 429, `Retry-After` or `ERR_PROVIDER_LIMIT`
   line from the accounts code (UNCONFIRMED whether the retry layer logs its retries at all - §1c).

What the providers allow (checked 2026-10-12 against their own pages):

1. **Google People API:** `people:batchCreateContacts` and `people:batchUpdateContacts` take **up to 200
   contacts** a request; `people:batchDeleteContacts` takes up to 500. Google: "Mutate requests for the same user
   should be sent sequentially to avoid increased latency and failures." So: batches, one at a time per account.
2. **Microsoft Graph:** `$batch` takes **20 requests** a call; Outlook runs up to four of a batch's requests at a
   time per mailbox; the mailbox limit is 10,000 requests per 10 minutes and four concurrent requests.
3. So 1,099 Google updates are about **6 requests instead of about 2,200**, and 1,382 Microsoft updates about
   **70 instead of about 4,100**.

**Confidence.**

1. **The owner's counts, 2026-10-12 about 21:15:** Outlook **1,436**, Leasha **1,442**, Google **1,444**.
2. **What Leasha's tables say at the same time:** 1,445 live contacts; GMail Main 1,445 synced links to live
   contacts **plus 6 synced links to contacts in the Bin and 5 waiting deletes**; MS Main 1,445 synced links;
   Apple 1,445; Leasha (CardDAV) 1,043 synced and 402 waiting to be created. So:
   - Leasha believes Outlook holds 1,445; Outlook shows 1,436 - **9 unexplained**.
   - Leasha believes Google holds up to 1,456 (1,445 + 6 + 5); Google shows 1,444 - **unexplained**.
   - The tables hold 1,445 live; the app showed the owner 1,442 - **3 unexplained** (which screen it was is
     not known; §0c asks).
   Nobody can say today which contacts make up those gaps. That is the confidence problem in one line.
3. **Three definitions of "how many".**
   - Settings > Accounts (`ui/accounts/state.py:45`): links `synced` + `pending_create`, **including contacts in
     the Bin**. Leasha (CardDAV) shows 1,445 there while 402 of them are not at the account.
   - Contacts page, per account (`ui/contacts/loader.py:70`): distinct **live** contacts with a link `synced`,
     `pending_create` or `deleted_remote`.
   - The account itself: whatever Google or Outlook counts. Neither of Leasha's numbers is that.
4. **Known reasons an account's own number can differ, none shown to the person:** Microsoft child contact
   folders are never read (`graph_contacts.py`, top-level folders only); Google "Other contacts" are not
   contacts and are not read; contacts waiting to be written; deletes waiting; contacts in the Bin still linked.
5. **The delete brake froze every write.** From 13:52 to 17:19 every scheduled sync of GMail Main and MS Main was
   held at 45 deletes (limit 20): `_push` raises `_Held` before writing anything, so 1,099 waiting updates waited
   too, and each held run said nothing else.
6. **Apple does not settle.** Run 485: 2,768 inbound merges and 4,072 conflicts for 1,445 contacts - each
   contact merged about twice and found changed again. A sync straight after a clean sync must write nothing; this
   one never got there. Cause not found (UNCONFIRMED: a representation that round-trips differently - group
   cards, labels, phone format).
7. **A run records nothing until it ends.** `finish_run` writes the plan once, at the end. Runs 487 and 488 ran
   3 h 22 min and left an empty row. Runs 504-507 sat as "running" for 3 h 50 min with no counts in the table.
8. **What was refused is not easy to act on.** "Janus Christiansen: GMail Main refused the request" is in the
   run text; there is no list of refused contacts with a reason and a Try again.

---

## What must not change

1. **Never overwrites blind (1k rule 3c).** Every update carries the version it was based on; a stale one is
   re-read, re-merged and retried. Batching keeps this per contact: one item's conflict never fails the batch.
2. **Nothing deleted on a hunch (D7 of 1k).** The delete brake stays, with its limits. What changes is its reach
   (D2): it holds deletes, not everything.
3. **The merge base stays honest.** After a write, the base is what the account *holds*, not what was sent
   (`_held_after_write`'s reason). Where a write's own response returns the saved record, that response is the
   read-back; a separate `GET` only where it does not.
4. **Resumable.** A crash costs at most the batch in flight; the next run continues from the saved state.
5. **One account is one failure domain.** An account that fails never stops another.
6. **No new host, no new scope, no new dependency.** The batch endpoints are on hosts already allowed and are
   covered by `https://www.googleapis.com/auth/contacts` and `Contacts.ReadWrite` (checked in §0d). Keep-alive
   is `http.client` from the standard library. The website's permission table mirrors `oauth.scopes_for`
   (HANDOFF trap, 2026-10-11) - it must not need to change.
7. **The UI thread does no I/O** (rule 5). The check after a sync (§3) runs on the sync's worker.
8. **No existing label is reworded** (`CLAUDE.md`). New numbers get new labels; an old label that becomes wrong is
   replaced by a new control, never edited.

## Decisions (recommended defaults; the owner confirms on release)

- **D1 Measure first.** Every run records, per account and phase: requests, seconds, contacts written, refused,
  and retries slept. Nothing in §5 is accepted except by these numbers.
- **D2 The brake holds deletes only.** Past the limit, the deletes wait - each named in the Sync window with
  **Allow these deletes** - and every create and update is written. The run ends `held-deletes`, not `held`.
- **D3 One count, three parts, everywhere.** For each account: **At <account>** (the number the account itself
  returned at the last check, with its time), **Waiting** (creates and deletes not yet written) and **Kept only
  in Leasha** (live contacts the account cannot or does not hold, with the reason). The Contacts page's total is
  live contacts, nothing else. No other count of contacts is shown anywhere.
- **D4 A check after every sync, on by default.** Read-only: the account's list of ids and versions (the cheapest
  call it has), compared with the links. The result is **In step** or the exact contacts that differ, by name,
  with the reason. A setting turns it off; `leasha accounts verify` stays and uses the same code.
- **D5 Accounts are pushed at the same time**, one worker each (they are different hosts and different failure
  domains); within one account, Google batches go one after another (Google's own advice) and Microsoft
  batches one at a time with Outlook running up to four inside each.
- **D6 Settle before speed.** A second sync straight after a clean one writes nothing, for every provider. This is
  a test (§4a) and an acceptance number (§7c); the Apple loop is fixed before §5 lands.

## 0. Before starting - the executing thread checks these and says so

*Note, 2026-10-13: not ticked. The full suite (`run_suite.py`, 2 processes - 6 GB free, the owner's app and index running) did not finish: one process died with 0xC0000005 in `test_guide_pictures.py` (it passes alone) and the other hit the hour limit in `test_pipeline_bench.py`. `run_suite.py --affected origin/main` (every test file that can see this branch, 2,761 s): 1 failed - `test_chat_conversation_qt.py::test_the_new_settings_have_controls_that_emit_their_key`, a 5 s thread-pool wait under load; that file passes alone. `test_navigation_is_instant.py` fails on unmodified main as well under the same load (948 ms, 433 ms against 400 ms). Windows CI on the PR is the full-suite run.*
- [ ] **0a** `main` is green: `venv\Scripts\python.exe scripts\run_suite.py --affected`, then the full suite.
*Note, 2026-10-12: checked. The Design system review thread (fix/ui-one-app, PR #68) touches `panel.py` and `buttons.py` in other hunks; order 1s (uncommitted on the laptop) touches `app/accounts/store.py` settings and `globals_box.py`'s Enrichment group - rebase onto main after both land.*
- [x] **0b** No other thread has uncommitted work in `app/accounts/` or `app/contacts/` (order 1s touches
  `app/contacts/enrich/` and `app/accounts/store.py`; coordinate in `HANDOFF.md` if both are live).
*Note, 2026-10-12: the owner's counts, about 21:15: Outlook 1,436, Leasha 1,442, Google 1,444; the tables held 1,445 live. Asked which screen showed 1,442, the owner answered "privateemail synched in outlook" - the Leasha CardDAV account (`dav.privateemail.com`) is also shown in Outlook, so Outlook's number may include or be that book (UNCONFIRMED; the check of 3c answers it by name once run).*
- [x] **0c** Ask the owner which screen showed 1,442, and re-read the three counts (Leasha, Google Contacts web,
  Outlook web) on the day work starts; write them under this item.
*Note, 2026-10-12: Google's 200-a-request batch limit and Graph's 20-request `$batch` were read on their pages. Not confirmed: that Graph's PATCH answer carries the whole contact, and `If-Match` on contacts. The code does not depend on either: it keeps the batched pre-read of change keys, and reads back (one batch) any write whose answer has no contact. Left open.*
- [ ] **0d** Check against Google's and Microsoft's own pages, and write the links here: the batch endpoints'
  scopes; that Graph `PATCH /me/contacts/{id}` returns the updated contact; that `If-Match` with the contact's
  `@odata.etag` (or `W/"changeKey"`) is honoured on `PATCH` and `DELETE` (`send_if_match` is off today because
  this was never seen live).

## 1. Measure - every run says where its time went

*Note, 2026-10-12: shown by `leasha contacts log --timing` (the existing log command) rather than a new `accounts runs --last`.*
- [x] **1a** A per-account counter on the provider's transport: requests by method, bytes, seconds, retries and
  seconds slept. Stored in the run's plan (`timing`) and printed by `leasha accounts runs --last`.
- [x] **1b** `leasha contacts sync --measure`: a dry run that plans every account and reports, per account, what
  it would write and how many requests that is with today's path and with batching. Read-only.
- [x] **1c** The retry layer (`http.py`, `_send_once` and `request`) logs every retry and every sleep at INFO with
  the status and the wait, so a slow run can be told from a throttled one.
*Note, 2026-10-12: UNVERIFIED - needs a sync of the owner's accounts with this build.*
- [ ] **1d** Baseline written under this item from 1a on the owner's accounts: a no-change sync of all four, and a
  measured update of 100 contacts per account (UNVERIFIED until run on the laptop).

## 2. Progress that survives - the run row is written as it goes

- [x] **2a** `finish_run`'s plan is also written after every page pulled and every batch written (the counts and
  the last 50 names), in the same transaction as the work. A run that dies leaves what it did.
- [x] **2b** At start-up a run still `running` from a closed app is closed as `interrupted` with its saved counts
  (today's text, "The app closed before this sync finished.", is kept; the counts are added beside it).
*Note, 2026-10-12: kept as a JSON list per account in `index_state` (`contacts:failures:<id>`), not a new table, so no migration: a database this build writes still opens in the build before it.*
- [x] **2c** Refused contacts are rows, not a sentence: a `contact_sync_failures` table (account, contact, kind,
  reason, first and last seen, attempts). The Sync window lists them with **Try again** and **Open contact**; a
  contact that later goes through is removed from the list.

## 3. One count, and a check after every sync (D3, D4)

- [x] **3a** One store function, `hub.account_counts()`, returns per account: linked and at the account, waiting
  to create, waiting to delete, deleted at the account, kept only here (with reason: the account cannot hold it,
  or it was removed from that account). Every screen reads it; the two queries in `ui/accounts/state.py:45` and
  `ui/contacts/loader.py:70` are replaced by it. A test asserts no other SQL in `app/ui/` counts `contact_links`.
- [x] **3b** `ContactProvider.list_ids()` - every remote id and version, the cheapest way the account offers
  (Google `connections.list` with `personFields=metadata` and `pageSize=1000`; Graph `/me/contacts` and each
  folder with `$select=id,changeKey`; CardDAV `PROPFIND` depth 1 for `getetag`). The verify code in
  `service.verify_contacts` uses it instead of a full pull.
- [x] **3c** After every sync, per account (D4): compare `list_ids()` with the links and store the result: the
  account's own count, the time, and the lists - at the account but not linked here; linked here but not at the
  account; version differs from the last agreed. Shown in Settings > Accounts and the Sync window as **In step**
  or "3 differ" with the names and **Fix these** (pull the first list, re-check the second, re-merge the third).
*Note, 2026-10-12: child folders are listed recursively and each gets its own delta link; that Graph's contact delta works on a child folder is UNVERIFIED live.*
- [x] **3d** Microsoft child contact folders are walked (pull, `list_ids` and folder names for groups), so
  Outlook's own count and Leasha's agree. If Graph's delta cannot reach a child folder, the check says how many
  contacts sit there and that Leasha cannot read them - by number, not silently.
*Note, 2026-10-12: UNVERIFIED - the check runs after the first sync with this build.*
- [ ] **3e** The owner's three counts (§0c) are explained, contact by contact, by 3c's lists: each of the 9 at
  Microsoft, the Google difference and the 3 at Leasha has a name and a reason, written under this item.

## 4. Settle - a clean sync is followed by a sync that writes nothing (D6)

*Note, 2026-10-12: built for an iCloud-like CardDAV round trip (vCard written, PREF dropped, X-FC-CLUSTER-ID added, read back) and the fake provider (`test_an_icloud_round_trip_settles_and_a_second_sync_writes_nothing`), which fails on the old code. Round trips of Google's and Microsoft's own JSON are not built yet - left open.*
- [ ] **4a** An idempotence test per provider (Google, Microsoft, CardDAV with group cards, CardDAV with
  CATEGORIES): sync a book of realistic cards, sync again, assert zero writes and zero conflicts. Fixtures recorded
  from the owner's accounts (names and values replaced), not invented.
*Note, 2026-10-12, the diagnosis, from run 485's journal (read-only): 4,704 added values were a custom "fc cluster id" - the server's `X-FC-CLUSTER-ID`, read as a custom value by `vcard.py` and then written on to Google - and 3,976 clashes were a phone number's preferred flag, which iCloud does not keep. Fixed: `vcard.SERVER_INTERNAL_PREFIXES`; `Capabilities.primary` (False for Google, Outlook, CardDAV); a one-off repair of the book (`_repair_vendor_fields`, one undoable journal batch).*
- [x] **4b** Find why Apple merges every contact twice and records 4,072 conflicts: instrument `_inbound` to log,
  for one sync, which unit differs between the base and the account for the first 50 contacts (method §6:
  instrument, do not guess). Fix the cause in `project`, `units` or the CardDAV mapping; the diagnosis goes in
  this item.
- [x] **4c** A run where every inbound contact "changed" and the change is the same unit on most of them stops
  writing back and says so ("Apple returns phone numbers in a different form for 1,402 contacts - looked into
  before writing"), instead of writing 1,258 updates. Threshold a setting.

## 5. Speed - batched writes, kept connections, no wasted reads

- [x] **5a** `http.py`: a keep-alive transport on `http.client.HTTPSConnection`, one connection per host per
  account, reopened on a reset (`WinError 10054` retried once on a fresh connection before it is an error). The
  allow-list, TLS context, redirect refusal and redaction are unchanged; their tests are kept and run against the
  new transport.
- [x] **5b** `ContactProvider` gains optional `create_many`, `update_many`, `delete_many`, each returning one
  outcome per item (written with its new version and the saved record, conflict, or refused with a reason). The
  engine uses them when present and falls back to the one-by-one methods; the fake provider gets both.
- [x] **5c** **Google:** `batchCreateContacts` and `batchUpdateContacts` (200 a call, `readMask` = the read
  fields, so the response is the read-back) and `batchDeleteContacts` (500 a call). Rule 3c for deletes: one
  `people:batchGet` (200 a call) compares etags before the delete. Batches one after another (Google's advice).
- [x] **5d** **Microsoft:** `$batch` of 20 (`POST`, `PATCH`, `DELETE`), the `PATCH` response as the read-back.
  With 0d confirmed, `If-Match` replaces the pre-read `GET`; without it, the pre-reads go in one `$batch` of 20
  `GET`s with `$select`. A refused item alone is retried property by property; the batch is not.
*Note, 2026-10-12: built - kept connection, group cards once per batch, one multiget read-back - except "up to four requests in flight": the connector's index and group cards are shared state, and one in flight on a kept connection was judged enough until measured. Left open.*
- [ ] **5e** **CardDAV:** the `ETag` returned by `PUT` is kept; the read-back is one `addressbook-multiget` per
  50 contacts written, not one `GET` each. Group-card rewrites are collected and written once per group per run.
  Up to four requests in flight per server (a setting; one for a server that refuses it).
- [x] **5f** The engine writes one SQLite transaction per batch (links, journal, run plan), not one per contact.
- [x] **5g** Accounts are pushed at the same time (D5), each on its own worker with its own provider; the store
  serialises writes as it does now. The Sync window shows each account's line moving at once.
*Note, 2026-10-12: Google's page size is now 1,000. Graph's page size was not measured - left open.*
- [ ] **5h** Pull: Google `pageSize` 1000 (the documented maximum); Graph page size measured at 100, 500 and 999 and
  the fastest kept, with the number under this item.

## 6. The brake, the window and the words (D2)

*Note, 2026-10-12: built, with two departures. The run keeps the status `held` (the brake's message already says "so no contact was deleted", and is now true of a run that wrote everything else) rather than a new `held-deletes`. **Keep them** was not built: Restore in the Bin already keeps a contact. Left open for the owner to say whether Keep them is still wanted.*
- [ ] **6a** `_push` writes creates and updates, then the deletes up to the limit's decision: past the limit the
  deletes stay waiting, named in the Sync window with **Allow these deletes** and **Keep them** (restores the
  contacts at the account side to "kept"), and the run ends `held-deletes` with everything else written.
- [x] **6b** The Sync window's last line per account says, in words, what the account now holds and whether it is
  in step: "Google: 1,444 at the account · In step · checked 21:10" or "Outlook: 1,436 at the account · 9 differ".
- [x] **6c** The user guide and Troubleshooting gain "Why do my counts differ?" - the reasons from §3 with what
  to press for each.

## 7. Done means measured (on the owner's accounts; UNVERIFIED until run on the laptop)

*Note, 2026-10-12: 7a-7d are UNVERIFIED until the owner's accounts sync with this build.*
- [ ] **7a** **Speed, a full rewrite** (every contact updated, e.g. after a bulk merge): Google 1,445 in under
  **3 minutes**, Microsoft 1,445 in under **5 minutes**, both at the same time; today 3 h 50 min for the four
  accounts. Measured by §1a and written here with the commit.
- [ ] **7b** **Speed, nothing to do:** all four accounts in under **20 seconds** together, including §3's check.
- [ ] **7c** **Settled:** a sync straight after a clean one writes 0 contacts and records 0 conflicts on every
  account (Apple included).
- [ ] **7d** **Counts agree or are explained:** for each account, §3's "At the account" equals the number the
  owner reads in Google Contacts and Outlook web on the same day, and every difference between accounts and
  Leasha is listed by name with a reason. The owner confirms.
*Note, 2026-10-12: proved with 4 deletes past a limit of 2 and 6 updates (`test_the_brake_holds_only_the_deletes_and_everything_else_is_written`).*
- [x] **7e** **The brake:** a fixture with 45 deletes and 1,000 updates writes the 1,000 and holds the 45.
- [x] **7f** **Crash:** killing the app mid-push leaves a run row with the counts written so far, and the next
  sync writes only what is left (a test with a provider that raises after N items).

## 8. Close

- [x] **8a** `docs/USER_GUIDE.html`, `docs/TECHNICAL_REFERENCE.html` and `docs/TROUBLESHOOTING.md` describe sync
  as it now is (rewritten in place, owner's rule of 2026-10-06); `docs/GLOSSARY.md` gains "in step", "waiting"
  and "kept only in Leasha".
- [ ] **8b** `CHANGELOG.md` under `[Unreleased]`; `docs/ORDER_REGISTER.md` row 1t counted from this order's
  boxes; `HANDOFF.md` entry with the before and after numbers from §1d and §7.

---

## Starting prompt for the executing thread

Paste this into a new session in `D:\Local\GitHub\Leasha` once the owner has released the order:

> Execute work order 1t, `docs/WORKORDER-contact-sync-fast-and-trusted.md`. Read `CLAUDE.md`, `HANDOFF.md`,
> `docs/PROJECT_INSTRUCTIONS.md` and the order first. Work through §0: if any item fails, say which and stop.
> Otherwise branch `feat/contact-sync-fast-and-trusted`, one task per numbered item on the task list before you
> start, and build in this order: §1 (measure), §2 (progress that survives), §3 (one count and the check), §4
> (settle), §6 (brake), then §5 (speed) - confidence first, because §5 is accepted by §1's numbers and §4's
> idempotence test. Respect "What must not change" without exception; a decision not covered by D1-D6 goes to
> the owner. Never run a branch's migrations against `D:\LeashaIndex\Data` - use `--env` with a scratch DATA_PATH
> until the branch is merged (HANDOFF trap, 2026-10-12). Anything that needs the real accounts, the Windows window
> or the owner's data and cannot be checked from where you run is marked UNVERIFIED, not ticked. Finish with §7's
> numbers written into the order, the full suite green, a PR to `main`, and a close-out saying what changed and
> what is now untrue in `HANDOFF.md` and `docs/ORDER_REGISTER.md`.
