# Work order (Master thread): Accounts - live mail in, and a contact book that syncs

**Doc version:** 1.1 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** Master thread with helper threads (new `app/accounts/`, new `app/contacts/`,
`app/storage/migrations.py`, `app/ui/shell.py`, a new `app/ui/contacts_view.py` and its widgets,
`app/cli/`, `app/serve/mcp.py`, `tests/unit/`, the user guide, `docs/GLOSSARY.md`, `docs/PROJECT_INSTRUCTIONS.md`)
**Status:** DRAFT, written 2026-10-11 at the owner's request ("go with c, Leasha as hub, write the
draft work order"). Nobody may start it. Decisions D1-D14 carry defaults; the ones marked
**[FINALISE]** are the owner's. D15 is decided.

**Where this came from.** A conversation on 2026-10-11, in the owner's words:

1. "what would it take to index live mail from connection to google microsoft apple etc
   including contacts and contacts sync system"
2. "the contact has to be able to synch contacts and keep them updated in multiple accounts a
   true contact sync functionality .. the offline rule is for keeping the data offline not
   source of data, the exception will be contact sync"
3. Between IMAP + CardDAV (option b) and each provider's own API (option c): "go with c, Leasha
   as hub".
4. "the master contact needs a rich editor which can view contacts as list or business cards ..
   also think if we can enrich the contacts free of charge .. also there can be multiple
   accounts not only three i.e. multiple google acounts, microsoft work and home accounts etc"
5. "the contact management has to be a fully featured world class contact management with
   export functionality too"
6. "there will be additional contacts pill"
7. "dont forget the mcp server updates", then "the only read for mcp"
8. "mechanism for scheduled sync and how often how to get mail etc should be comprehensive and
   professional in settings"

**Why option c.** Microsoft 365 and Outlook.com contacts have no CardDAV, so they can be reached
only through Microsoft Graph (UNCONFIRMED - check against Microsoft's current documentation
before release, item 0c). Once Microsoft has to be registered for contacts anyway, IMAP saves
no sign-in work, and the providers' own APIs send only what changed and keep Gmail's labels as
they are.

**What exists today, read 2026-10-11.**

1. Mail comes in from files and from Outlook. `app/extract/email_pst.py` reads `.pst` and `.ost`
   through Outlook (MAPI) or libpff; `email_mbox.py`, `email_emlx.py`, `email_olm.py` and
   `email_files.py` read the rest. The `messages` table (`app/storage/schema.sql`) is keyed to
   a `files` row and records `store_path` and a MAPI `entry_id`, with no account and no server id.
2. Contacts do not exist as a thing. A `.vcf` is indexed as plain text
   (`app/extract/source_types.py`). `sender` and `recipients` are text columns.
3. Named people exist on the photo side: `piles` (a name) and `faces` (`pile_id`), from order
   0j's Photo Tagger.
4. No code stores a secret. Nothing in `app/` uses Credential Manager, DPAPI or `keyring`.
5. The schema is at 36 (`app/storage/migrations.py`, `CURRENT_VERSION`).

---

## Two rule changes this order needs (the owner's, 2026-10-11)

Both are the owner's decisions from the conversation above. They are written into
`docs/PROJECT_INSTRUCTIONS.md` as dated notes by item 0a. Nothing changes them until this order
is RELEASED.

1. **"Things that are deliberately not built" - Cloud APIs.** The rule reads as a rule about
   where the data lives, not where it comes from. Account connections may fetch mail and
   contacts from Google, Microsoft and Apple. Everything fetched is stored and searched on this
   machine only. Search, indexing and reading files stay offline as before. Only these may go
   out:
   1. Sign-in and the provider calls that fetch mail and contacts.
   2. Contact changes written back to the person's own accounts (exception 2 below).
   3. Enrichment calls, and only the ones the person has switched on (§7).

   Nothing else leaves the machine: no telemetry, no index content, no passages.
2. **Non-negotiable 10, "Read-only against user data".** It stays true for documents and email:
   mail is read, never changed, moved, flagged or deleted on any server. **Contacts are the one
   exception.** Leasha holds the master copy, and the sync engine writes a contact's changes
   to the accounts it is kept in (D4). Every outward write is journalled and can be undone (§3).
   The precedent is the 2026-10-05 note on rule 10 (photo names into XMP, only when asked).

---

## Decisions (defaults the building thread may take; say which were taken at close-out)

- **D1 One order or three. [FINALISE]** Default: one order built in phases. Each phase merges
  and ships on its own, and §9's tests ship with each phase.
  1. **Phase A:** accounts, the contact hub and its sync, the Contacts page, and the contact
     tools on the MCP server, and Settings > Accounts for contacts (§0-§4, §6, §8b-§8g, §10
     except its mail items).
  2. **Phase B:** live mail (§5), its MCP filter (§8a) and §10's mail items.
  3. **Phase C:** enrichment (§7).
- **D2 Who owns the app registrations. [FINALISE]** A Google Cloud project and a Microsoft Entra
  app registration are needed, each owned by an account. Default: the owner's own accounts,
  for the owner's own use (the install order's "no distribution for now", §5 of
  `docs/ORDER_REGISTER.md`). Creating them is the owner's job, because they are accounts
  (item 1a gives the steps).
- **D3 Google's review. [FINALISE]** Full Gmail read access is a "restricted" permission;
  contacts are "sensitive". UNCONFIRMED, from general knowledge: an app left in Google's
  "Testing" mode needs no review for up to 100 named test users, but its sign-ins expire after
  7 days, so the person signs in again weekly. Default: Testing mode, the owner as the only test
  user, and the weekly sign-in shown in plain words on the account's row. Publishing (review,
  plus a yearly paid security assessment for Gmail) waits until distribution is decided.
- **D4 Which contacts go to which account.** Not every contact belongs everywhere: a work
  account's contacts are the employer's, and work colleagues do not belong in a home Google
  account. Default: **each contact carries a "Kept in" set of accounts**. A contact that came
  from an account is kept in that account. A contact made in Leasha goes to the default account
  (D5). Adding or removing an account from "Kept in" is an edit like any other, and removing one
  deletes the contact from that account only after the person confirms. Nothing copies every
  contact to every account by default. "Keep in all accounts" is a bulk action the person
  chooses.
- **D5 The default account for a new contact.** Default: the first account added with contact
  sync on. Changeable in Settings > Accounts (rule 11).
- **D6 Two accounts both changed the same field.** Default: field by field, three-way against
  what Leasha last synced. A change on one side only wins with no question. When both sides
  changed, the most recent edit wins, the loser is kept in the journal, and the contact shows a
  "changed in two places" marker until the person looks at it.
- **D7 Deletes.** Default:
  1. A contact deleted in one account is not deleted in Leasha or the other accounts. It is
     listed under "Deleted elsewhere" for the person to confirm or restore.
  2. A contact deleted in Leasha goes to the bin (30 days) and leaves every account it was kept
     in. Restoring it from the bin writes it back.
  3. A sync that would delete more than 20 contacts, or 5% of the book, stops and asks.
- **D8 The first sync of an account.** Default: a dry run that lists what it would add, merge
  and change in Leasha and in each account, and writes nothing until the person says yes.
- **D9 Merging people across accounts.** Default: the same person from two accounts is
  suggested as one contact when an email address or a phone number (normalised, E.164) matches.
  A name match alone is suggested, never merged. Suggestions are accepted by the person, and a
  merge can be split again.
- **D10 Fields an account cannot hold.** Google, Microsoft and Apple each store a contact
  differently. Default: Leasha keeps every field. Each account is written only the fields it can
  hold, and a field it cannot hold is never removed from Leasha by a round trip.
- **D11 How and how often sync runs.** The full mechanism and its Settings are §10. Defaults:
  1. Mail every 5 minutes, and the iCloud inbox near-instantly (IMAP IDLE).
  2. Contacts every 15 minutes.
  3. Both on start, on wake from sleep, and on "Sync now".
  4. Sync runs while Leasha is open or in the tray, never after it exits (single process).

  Google and Microsoft push are not used: both need a public web address or a cloud service
  outside this machine. Changes are polled instead, which their change-only APIs make cheap.
- **D12 How much mail the first fetch takes.** Default: everything. The account row offers
  "only the last N years" before the first fetch. The fetch is resumable (rule 4) and paced to
  each provider's limits.
- **D13 Mail an account and an Outlook archive both hold.** Default: one message, matched by
  its `Message-ID` header. The live copy wins, and the archive copy is kept as a second place
  it lives. This joins order 1j's duplicate PST work and must not undo it.
- **D14 The rail entry.** The owner's instruction: Contacts gets its own pill. Default label
  **"Contacts"** (a new label), icon `contact`, placed after Files. Search finds contacts too
  (§6f), so the pill is for managing them.
- **D15 The MCP server stays read-only (decided by the owner, 2026-10-11: "the only read for
  mcp").** Contacts, accounts and live mail are added to what AI programs can **read** (§8).
  No tool creates, changes or deletes a contact. The server keeps its rule: "nothing that
  writes, moves, deletes or opens anything" (`app/serve/mcp.py`).

---

## 0. Records and rules

- [ ] **0a** Write the two dated notes above into `docs/PROJECT_INSTRUCTIONS.md` (the Cloud APIs
  line and rule 10), and a `HANDOFF.md` entry, on release.
- [ ] **0b** The Chat order's guard (a turn with the web switch off opens no non-loopback
  connection) is joined by one for this order: an index run, a search and a Chat turn with web
  off open no non-loopback connection, **even with accounts connected**. Only the account worker
  and enrichment may connect, and a test pins which hosts each may reach.
- [ ] **0c** Every provider fact this order marks UNCONFIRMED is checked against the provider's
  current documentation before Phase A starts, and the result is written as a dated note above
  the item it affects. In particular: Microsoft contacts have no CardDAV; Google Testing mode
  and the 7-day sign-in; the scopes for Gmail read, Google contacts read-write and Graph
  `Mail.Read` / `Contacts.ReadWrite`; iCloud CardDAV with an app-specific password.

## 1. Accounts and sign-in

- [ ] **1a** Owner steps, written in the user guide with screenshots taken on release: create
  the Google Cloud project and its desktop OAuth client, and the Microsoft Entra registration
  (personal and work accounts, a public client, a loopback redirect). Their client IDs go in
  through Settings > Accounts, never a file (rule 11).
- [ ] **1b** An `accounts` table (migration 37): id, provider (`google`, `microsoft`, `apple`),
  address, the person's own name for it ("Work", "Home"), colour, mail on/off, contacts
  on/off, added and last-synced times, last error. **Any number of accounts of each provider**:
  several Google accounts, a Microsoft work and a Microsoft home account, more than one iCloud.
- [ ] **1c** Sign-in for Google and Microsoft through the browser, with PKCE and a loopback
  redirect, as a desktop app does. Apple through an app-specific password the person makes at
  Apple's account page; the page tells them where.
- [ ] **1d** Secrets (refresh tokens, app passwords) are kept in Windows Credential Manager
  through the `keyring` library (a wheel, rule 12; macOS Keychain through the same library).
  They are never written to SQLite, `.env`, a log or a crash report, and a test greps the log
  and the database after a sign-in to prove it.
- [ ] **1e** A work account whose administrator has not approved the app fails with an
  `AppError` that says so and what to ask IT for (rule 2), not an OAuth error page.
- [ ] **1f** Removing an account signs out, deletes its secret, and asks whether to keep its
  contacts in Leasha (default: keep, with that account taken out of their "Kept in") and its
  mail in the index (default: remove).
- [ ] **1g** CLI first (rule 8): `app.cli accounts add|list|remove|signin|status`.

## 2. The contact hub (Leasha's master copy)

- [ ] **2a** Tables (migration 37):
  1. `contacts`: one row per person.
  2. `contact_fields`: typed, repeatable values - email, phone, address, URL, date, related
     person, social handle, custom field - each with a label and an order.
  3. `contact_links`: contact × account, holding the account's own id, its version tag (etag or
     change key) and a snapshot of what was last synced (the base for D6).
  4. `contact_groups` and memberships.
  5. `contact_journal`: every change, who made it (Leasha, or which account) and its undo.
  6. `contact_sources`: where each field came from (an account, an import, a signature, an
     enrichment) - the receipt the editor shows.
- [ ] **2b** SQLite is the authority (rule 6). Contacts go into the search index like mail does:
  name, company, title, notes, emails and phones are searchable, and a search hit opens the
  contact.
- [ ] **2c** Link to the photo side: a contact can be linked to a named person (`piles`), and
  its photo can be picked from that person's faces. The link is suggested when the names match
  and accepted by the person (D9's rule).
- [ ] **2d** Phone numbers are normalised to E.164 with `phonenumbers` (a wheel) for matching
  and stored as typed for display.

## 3. The sync engine

- [ ] **3a** One worker thread for all accounts (rule 5), resumable (rule 4): each account's
  change token (Google `syncToken`, Graph delta link, CardDAV `sync-token` or ctag) is saved
  after every page, before the page's changes are applied.
- [ ] **3b** Inbound: changes from each account are merged into the hub by D6 and D9. Outbound:
  hub changes go to every account in the contact's "Kept in" set (D4), fields mapped by D10.
- [ ] **3c** A write that loses a race (the version tag changed on the server) re-reads,
  re-merges and retries. It never overwrites blind.
- [ ] **3d** The first sync is the dry run of D8. The delete brake of D7 holds on every sync.
- [ ] **3e** Undo: any journal entry, or a whole sync, can be undone from the contact's history
  or the sync log. Undo writes the old values back to the accounts too.
- [ ] **3f** Provider limits: backoff on 429 and 503 using the server's `Retry-After`.
  Measured, not assumed: the first sync of a 2,000-contact account is timed and recorded here
  (rule 9).
- [ ] **3g** Every failure is an `AppError` with a code (`ERR_ACCOUNT_SIGNIN_EXPIRED`,
  `ERR_ACCOUNT_ADMIN_CONSENT`, `ERR_SYNC_CONFLICT_HELD`, `ERR_PROVIDER_LIMIT`, ...), and a
  failing account never stops the others.
- [ ] **3h** CLI: `app.cli contacts sync [--account X] [--dry-run]`, `contacts log`,
  `contacts undo <journal-id>`.

## 4. The three providers - contacts

- [ ] **4a** Google: People API, read and write, with `syncToken`; contact groups map to Google
  labels.
- [ ] **4b** Microsoft: Graph contacts, read and write, with delta queries; contact folders map
  to groups. Personal and work accounts both work.
- [ ] **4c** Apple: CardDAV against iCloud (vCard 3.0), read and write, with sync-collection
  where the server offers it and etags otherwise; groups map to iCloud groups.
- [ ] **4d** Each connector is tested against recorded responses (no network in the suite),
  plus one opt-in live test per provider that the owner runs with a throwaway account.

## 5. Live mail (read only) - Phase B

- [ ] **5a** Gmail API with `history` for changes; labels kept as labels, not copied as folders.
- [ ] **5b** Graph mail with delta per folder.
- [ ] **5c** iCloud mail over IMAP with an app-specific password: `UIDVALIDITY`, `CONDSTORE`
  where offered, and IDLE on the inbox while the app is open.
- [ ] **5d** `messages` gains `account_id`, `remote_id` and `labels` (migration); a live message
  is a `files` row with an `account://` path, so search, the Mail page, previews and
  attachments (`mail_attachments.py`) work unchanged.
- [ ] **5e** Mail deleted or moved on the server is followed in the index. Nothing is ever
  written to a mailbox (rule 10 stands for mail).
- [ ] **5f** D12's first-fetch window and D13's dedupe by `Message-ID`.
- [ ] **5g** Fetched mail goes through the ordinary indexing pipeline: words first, meaning
  after, under the same budgets.

## 6. The Contacts page - a full contact manager

- [ ] **6a** The pill (D14). The page is built lazily if it is costly at start-up; the window's
  start-up time is measured before and after (order 0r's budget).
- [ ] **6b** Two views of the same list, switched by a toggle that is remembered:
  1. **List**: a sortable, resizable table - name, company, title, email, phone, the account
     chips, groups, last contacted.
  2. **Cards**: a grid of business cards - photo or initials, name, title, company and its
     logo, main email and phone, account colours along the edge.

  Both handle 10,000 contacts without a stall (measured, rule 9).
- [ ] **6c** A sidebar to narrow the list: All, Favourites, each group, each account,
  "Changed in two places", "Possible duplicates", "Deleted elsewhere", Bin. Smart groups are
  saved filters ("company is X", "no phone number", "not contacted in a year").
- [ ] **6d** The editor: every field type of 2a, any number of each, each with a label (work,
  home, mobile, custom), drag to reorder, a main email and phone. It also holds:
  1. The photo, and the photo picker from the person's own photos (2c).
  2. Birthday, anniversary and other dates.
  3. Related people, linked to their own contacts.
  4. Notes, kept with their history.
  5. "Kept in" account chips (D4), and groups.
  6. Favourite.

  Every field shows where it came from on hover (2a's sources).
- [ ] **6e** History: the journal for this contact, with undo per change (3e).
- [ ] **6f** "From your index": the latest mail with this person, the files exchanged as
  attachments, photos of them (2c), first and last contact dates. Every item opens. Searching a
  person's name in Search shows their contact card above the results.
- [ ] **6g** Duplicates: side-by-side merge choosing each field, and split after merge (D9).
- [ ] **6h** Bulk actions on a selection: add to or remove from a group, add or remove an
  account from "Kept in", set company, favourite, delete, export.
- [ ] **6i** Import:
  1. vCard 2.1, 3.0 and 4.0 files, one or many contacts each.
  2. CSV from Google, Outlook and Apple's export layouts, with a column-mapping step for any
     other CSV.
  3. Contacts in an indexed Outlook archive (`.pst` contacts folder) and `.vcf` files already
     in the index, offered as a one-time import.

  Every import shows a preview with the duplicates it found before anything is added. Imports
  never change the source file (rule 10).
- [ ] **6j** Export, of everything or the current selection or filter:
  1. vCard 3.0 and 4.0 (one file, or one file per contact).
  2. CSV in Google's layout, Outlook's layout and a plain all-fields layout.
  3. Excel (`.xlsx`).
  4. A printable PDF as a list or as business cards (reusing `report_pdf.py`).

  Each export is round-trip tested: exported, imported into an empty book, compared field by
  field.
- [ ] **6k** Keyboard throughout: `/` to filter, arrows, Enter to open, `Ctrl+N` new,
  `Ctrl+Z` undo, `Del` to the bin. Screen-reader names on every control.
- [ ] **6l** Settings > Accounts is built to §10.

## 7. Enrichment - free of charge (Phase C)

Two kinds, kept apart in the UI. Nothing is enriched without the person's say-so: enrichment
**proposes** values, the person accepts them per contact or in bulk, and only accepted values
are kept and synced out. Every enriched value records its source (2a).

**From your own index (nothing leaves the machine; on by default):**

- [ ] **7a** Email signatures. The newest signature in mail from this person, read for phone,
  title, company, address and website, offered with the message as its receipt.
- [ ] **7b** Company from the email domain, for domains that are not free mail (a fixed list of
  free-mail domains). Colleagues are grouped by company.
- [ ] **7c** A photo from the person's named faces (2c).
- [ ] **7d** Contact history: first and last contact, how often, who else is usually on the
  thread (6f).
- [ ] **7e** People who mail you often and are in no contact are listed as "suggested
  contacts", added only by the person.

**From the internet (off by default; each one a separate switch that says what it sends):**

- [ ] **7f** Gravatar: the photo and public profile a person published, looked up by a SHA-256
  hash of their email. The switch says that the hash is sent to Gravatar.
- [ ] **7g** Company logo: the website's own icon, fetched from the company's own domain. Only
  the domain is contacted.
- [ ] **7h** Wikidata, for companies only: website, headquarters and industry, looked up by
  company name. Public data under CC0. Never used for people.
- [ ] **7i** Run on demand ("Enrich this contact", "Enrich selection"), never on a timer. The
  same lookup is not repeated within 90 days.

**Not used, and why.** Paid enrichment services (Clearbit, Apollo, ZoomInfo and the like) cost
money and send personal data to a third party. Scraping LinkedIn or people-search sites breaks
those sites' terms. Both are out of scope.

## 8. The MCP server - what AI programs can reach

Today `app/serve/mcp.py` serves four read-only tools (`search`, `find_files`, `read_text`,
`index_status`) on `127.0.0.1` with Leasha's key, and `app.cli mcp` bridges them for programs
that can only start a command.

- [ ] **8a** `search` takes an optional `account` filter, and a live message's answer
  (`_mail_card`) names the account it came from beside the sender and date. A live message's
  `account://` path is answered in a form the AI program can quote back to `read_text`.
- [ ] **8b** New read-only tools:
  1. `find_contact`: by name, email, phone or company, ranked.
  2. `get_contact`: every field with its label and source, groups, "Kept in" accounts,
     favourite.
  3. `contact_activity`: the latest mail, files exchanged and photos for one contact (6f),
     each naming the file or message it came from.
- [ ] **8c** `index_status` reports each account: provider, its name ("Work"), mail and
  contact sync on or off, last sync and last error. It never reports a token, a password or a
  client ID, and a test proves no tool's answer contains a secret from Credential Manager.
- [ ] **8d** The server's `INSTRUCTIONS` gain one sentence each for contacts and accounts,
  **appended**. The existing sentences are not reworded (CLAUDE.md).
- [ ] **8e** The bridge (`bridge_server`, `app.cli mcp`) and `app/serve/clients.py` carry the
  new tools. A stopped server answers each new tool the same way it answers the four.
- [ ] **8f** The module docstring says what the server serves now (code documentation, rewritten
  in place), and the MCP section of the user guide lists the new tools and what each returns.
- [ ] **8g** Tests: each new tool against a temporary index with a fake account; the
  loopback-only and key checks cover the new tools; a test fails if any tool the server lists
  can write to the contact hub, an account or the index (D15).

## 9. Tests and documents

- [ ] **9a** Acceptance tests written first, from §1-§8 and §10, before the code (`How a layer gets
  built`). The sync engine is tested against a fake provider covering:
  1. Two accounts and the hub, all changing at once.
  2. A conflicting field (D6).
  3. A delete in one account (D7) and the delete brake.
  4. A token expired mid-sync, and a crash mid-page (resume).
  5. A 429 storm.
  6. Undo of a whole sync.
- [ ] **9b** `Leasha.pyproj` regenerated (`scripts/regen_vs_project.py`) for every new file.
- [ ] **9c** Documentation rewritten in place (owner rule 2026-10-06): the user guide (adding an
  account, the Contacts page, import, export, enrichment, what leaves the machine and when),
  troubleshooting (sign-in expired, admin consent, a held sync), `docs/GLOSSARY.md` (account,
  hub, "Kept in", journal, enrichment), `LOCAL_KNOWLEDGE_GRAPH_V2.md` (the account worker and
  its error codes) and the README's privacy paragraph.
- [ ] **9d** `run_suite.py --affected`, then the full suite, green on Windows. The real window
  looked at once at 125%: the pill, both views, the editor, a first-sync dry run.

## 10. Settings > Accounts - scheduling, fetching and control

One page, built like the rest of Settings: every control saves at once, every value has a
plain sentence under it saying what it does, and anything destructive is a flow that states
the cost and confirms (rule 11). Everything here also has a CLI form first (rule 8):
`app.cli accounts set <account> <key> <value>` and `app.cli accounts schedule`.

**The mechanism.**

- [ ] **10a** A `SyncPolicy` beside `app/index/schedule.py`'s `SchedulePolicy`, written the same
  way: a pure function of the last sync, now and the policy, idempotent about the past (a laptop
  opened after a fortnight syncs once, not 4,000 times). It needs minutes, which `SchedulePolicy`
  (hours, and a 300 s `MIN_GAP_S` floor meant for index runs) does not have. Its own floor is
  60 s, with a comment saying why.
- [ ] **10b** One `AccountScheduler` (the `app/ui/scheduler.py` pattern: a Qt timer that only
  asks the policy and starts work) drives the account worker of §3a. Per account and per kind
  (mail, contacts), it holds:
  1. Last success, last attempt and next due time.
  2. The current backoff.
- [ ] **10c** What starts a sync:
  1. The schedule.
  2. App start.
  3. Wake from sleep (Qt's power-resume signal or the Windows `WM_POWERBROADCAST`).
  4. The network coming back after being down.
  5. "Sync now", for all accounts or one.

  A trigger arriving while that account's sync runs is folded into it, never queued twice.
- [ ] **10d** How mail is fetched, per provider, stated on the account's page in one line each:
  1. **Gmail:** the History API, polled on the schedule.
  2. **Microsoft:** Graph delta per folder, polled.
  3. **iCloud:** IMAP IDLE on the inbox while Leasha runs (new mail in seconds), other folders
     polled.
- [ ] **10e** Backoff after a failure: 1, 2, 4, 8 ... minutes up to the schedule's own
  interval, reset by a success; a provider's `Retry-After` always wins. An account that has
  failed for 24 hours shows a warning on its row and in the tray. The other accounts are not
  slowed.
- [ ] **10f** Ordering and budget: one account syncs at a time per provider, so their per-user
  limits are not hit together. Contacts go before mail when both are due (small and quick). A
  mail first fetch yields every page to a due contact sync.

**Global settings (top of the page).**

- [ ] **10g** **Schedule**, separately for Mail and for Contacts, each with:
  1. Manual.
  2. On start only.
  3. Every 2, 5, 10, 15, 30 or 60 minutes.
  4. Hourly.
  5. Daily at a time.

  Defaults are D11's.
- [ ] **10h** **Pause all sync**: a switch, with "for 1 hour / until tomorrow / until I turn it
  back on". The tray menu has the same item.
- [ ] **10i** **Conditions**:
  1. Pause on battery, using `resources.py`'s battery reading. Default: off for contacts; on
     for a mail first fetch only.
  2. Pause on a metered connection, using Windows' network cost. Default: on. The library to
     read it is chosen under rule 12 (UNCONFIRMED: which wheel exposes it).
  3. Quiet hours: no scheduled sync between two times. Default: off.

  Each condition says on the page when it is what is holding a sync back.
- [ ] **10j** **First-fetch limits**: a download speed cap for a mail first fetch (default: no
  cap), and "only on mains and an unmetered connection" (default: on).
- [ ] **10k** **When the same contact changed in two places** (D6): newest edit wins (default),
  an account priority order (drag to order), or ask me every time.
- [ ] **10l** **Safety** (D7, D8):
  1. The delete brake's count and percentage, default 20 and 5%.
  2. A dry run before an account's first sync, on by default. Turning it off is a confirm flow.
- [ ] **10m** **Notifications** (Windows toasts through the tray, each a switch):
  1. A sync is held by the delete brake.
  2. A contact changed in two places.
  3. A sign-in has expired or expires within 2 days (the Google weekly sign-in, D3).
  4. An account has failed for 24 hours.

  Nothing is sent for an ordinary successful sync.
- [ ] **10n** **Default account for new contacts** (D5).

**Per account (one row each in a list; the row opens its panel).**

- [ ] **10o** The row shows:
  1. Colour, name, provider icon and address.
  2. A status word: Up to date, Syncing, Waiting, Paused, Needs sign-in or Failed.
  3. Last sync and next sync, in the date format from Settings > Window.
  4. Drag to reorder (also the priority order of 10k).
- [ ] **10p** **Account panel - general**:
  1. Name and colour.
  2. Sign-in state and when it expires, with Reconnect.
  3. **Test connection**, which reports each scope granted.
  4. **Sync now**.
  5. "Use the global schedule" or this account's own schedule (10g's choices).
  6. **Remove account**, the flow of 1f.
- [ ] **10q** **Account panel - mail** (Phase B), headed "read only - Leasha never changes this
  mailbox":
  1. Mail on or off.
  2. Which folders or labels: all (default), or chosen in a tree. Spam and Trash are left out
     by default, and Sent is included.
  3. How far back (D12): everything, the last 1, 2, 5 or 10 years, or since a date.
  4. Attachments: indexed (default) or not, and a size limit per attachment (default: the
     index's own limit).
  5. Space used in the index by this account's mail, with "Remove this account's mail from the
     index" as a confirm flow.
  6. First-fetch progress: messages fetched of the total, rate, time left, and pause/resume.
- [ ] **10r** **Account panel - contacts**:
  1. Direction: **Two-way** (default), **Into Leasha only** (read the account, never write to
     it - for a work account the person does not want written to), or **Off**.
  2. Which contact folders, labels or groups sync: all by default, or chosen.
  3. Count of contacts kept in this account, and last changes in each direction.
- [ ] **10s** **Sync log** (a table under the list, filterable by account and kind). Each row
  shows the time, account, kind, what triggered it, the counts added, changed, deleted and
  held, the duration and any error with its fix (rule 2). It offers **Undo this sync** (3e) and
  **Export log** (CSV).
- [ ] **10t** The Accounts page reads its state off the UI thread (rule 5) and refreshes when
  shown and when the worker reports, never on its own timer.
- [ ] **10u** Tests:
  1. `SyncPolicy` across a DST change, a fortnight's sleep, a clock moved back and the 60 s floor.
  2. Triggers folded together (10c).
  3. Backoff and `Retry-After` (10e).
  4. Each condition holding a sync and saying so (10i).
  5. Direction "Into Leasha only" never writing to that account (10r).
  6. Every control saving and reloading.

---

## Acceptance (the owner's checks, on the laptop)

- [ ] **A1** Two Google accounts, a Microsoft work and a Microsoft home account and one iCloud
  account connected at once. Each shows its status.
- [ ] **A2** A phone number changed on the phone in one account appears in Leasha and in every
  other account that contact is kept in, within one sync.
- [ ] **A3** A contact made in Leasha appears in its default account. Adding a second account
  to its "Kept in" puts it there too.
- [ ] **A4** A deletion in one account is held under "Deleted elsewhere" and touches nothing
  else until confirmed.
- [ ] **A5** Undo of the last sync puts every account back.
- [ ] **A6** Live mail from a connected Gmail account is found by Search within one sync.
- [ ] **A7** Export the whole book to vCard, import it into a fresh Google account by hand,
  and every field is there.
- [ ] **A8** With every enrichment switch off, a network capture during a full sync shows only
  the providers' hosts.
- [ ] **A9** Close the laptop lid for a night, open it: each account syncs once within a minute
  of waking, and the sync log shows "wake" as the trigger.
- [ ] **A10** A work account set to "Into Leasha only" is edited in Leasha: the edit stays in
  Leasha and the work account is unchanged.

## Not in this order

1. Calendars (CalDAV, Google Calendar, Graph events). A natural follow-on, not ordered.
2. Sending or changing mail of any kind. Mail stays read-only.
3. Other providers (Yahoo, Fastmail, generic IMAP and CardDAV). The connector shape (§4) should
   make them small later; they are not built here.
4. Distribution to other people. That needs Google's published review (D3) and the install
   order's licence decision.

## Close-out

Tick each box only when built and tested. Update this order's Status line, its register row
(`docs/ORDER_REGISTER.md` §3) and `HANDOFF.md`; name which of D1-D14 were taken as written.
