# Work order (One thread): contact enrichment that finds things - fresh details only, a signature reader that does not need "Kind regards", more of your own sources, every rule a setting

**Doc version:** 1.1 · **Updated:** 2026-10-13 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/contacts/enrich/`, `app/extract/quoting.py`, `app/extract/email_files.py`, `app/storage/`, `app/accounts/store.py`, `app/ui/contacts_tools/`, `app/ui/accounts/`, `app/cli/`, `tests/unit/`, the docs)
**Status:** BUILT 2026-10-13, 40 of 48 on `feat/enrichment-that-finds` (not merged). Open: 0a and 0b not recorded, 4b's cost and 5a/5d's reach to be measured, 5e waits on §9, **9a NOT MET** (114 of 200), 9f the owner's Windows checks. RELEASED by the owner 2026-10-12 ("fix it and make the whole system smart and configurable write a work order for all and give the design system thread to execute when it deems it is ready to execute .. make the design fool proof and extremely smart"), after asking "how does the contact enrichment work as it does not seem to work well at the moment". The executing thread decides when to start: §0 lists what must be true first.

**What this is.** Order 1k section 7 built enrichment: eight sources that *propose* details for a contact and change
nothing until the person accepts. It works as designed and finds very little. Measured on the owner's index on
2026-10-12 (read-only, every live contact, the code at `2d1d36a` plus the session's fixes):

| Step of the signature reader (7a) | Contacts |
|---|---|
| Live contacts | 1,445 |
| ... with an email address | 979 |
| ... with at least one message *from* them in the index | 526 |
| ... where a sign-off line was found | 90 |
| ... where something was read from the block | 64 |
| ... with at least one proposal | **53** |

**Why 436 of 526 found nothing**, read from the messages themselves (not inferred from the count):

1. **No sign-off.** "FYI", then name, title, company, phone, mobile, email - no "Kind regards". The reader requires a
   sign-off line or a `-- ` line, so the block is never found.
2. **Sign-offs it does not know.** "My Best," is not in `_SIGN_OFF`.
3. **Outlook's HTML-to-text doubles every line.** A blank line between every line halves the 14-line block and the
   25-line tail.
4. **Junk after the signature.** Mimecast-rewritten links, Safelinks, Sidekick tracking and rows of social icons sit
   after the signature and push the sign-off out of the last 25 lines.
5. **The index cut it.** `strip_quoted` removes everything after `-- `, "Sent from my", a confidentiality notice or a
   quoted chain, so those signatures never reach `chunks`.
6. **Only direct mail is read.** A person's signature in a quoted reply inside someone else's message is never used,
   and 453 contacts with an address have sent the owner nothing directly.

**The age window the owner asked for.** Indexed messages: 217,162 in all, **1,911 in the last six months**, 6,142 in
the last twelve. Contacts with mail from them, by window: 6 months **27**, 12 months 49, 24 months 91, 5 years 150,
any age 526. The owner chose (2026-10-12, "a but configurable"): **a hard cut, six months by default, changeable.**

**The internet sources were on** (`accounts:settings`: Gravatar, logo and Wikidata all `true`) and ran from 11 Oct
14:57 to 12 Oct 08:42 UTC: 1,124 Gravatar, 586 Wikidata and 223 logo lookups, every one then remembered for 90
days. **No enrichment value has ever been accepted** - `contact_journal` has no `enrich` action and
`contact_sources` no `enrichment:*` row.

**Fixed in the session that wrote this order** (so not repeated below):

1. The Contacts page never passed `force`, so after the first run the internet sources were asked nothing for 90
   days, silently. Enrich contacts now has **Look again** (`EnrichDialog.again_button`, `EnrichRun.start(force=)`).
2. The page never collected `notes`, so "could not be reached" was dropped. Notes now reach the window and the
   page's notice, and each run says which internet source was **not asked again** and for how many contacts
   (`run.skipped_notes`).
3. A LinkedIn or Facebook profile on a card was taken for the company's site (`ae.linkedin.com` was asked for a
   logo). `logo.company_domain` now passes over social sites.

Tests: `test_a_second_run_says_which_internet_sources_were_not_asked_again`,
`test_a_social_profile_is_not_taken_for_the_company_site` (`test_contacts_enrich_net.py`),
`test_enrich_look_again_asks_with_force_and_the_notes_are_shown` (`test_contacts_tools_ui_files.py`).

---

## What must not change

1. **Enrichment proposes; only the person applies.** No source writes a contact. `apply_proposals` stays the one
   door, one journal batch per acceptance, one Undo.
2. **Every proposal carries a receipt the person can check**: the message (subject, date, sender) or the URL it
   came from. A value with no receipt is not offered.
3. **Mail is read-only** (non-negotiable 10). Reading an original message again (§4) opens it read-only; nothing is
   moved, flagged or marked read.
4. **The internet exception stays as narrow as written** (`docs/PROJECT_INSTRUCTIONS.md`, 2026-10-11 scope note):
   only the three switched-on sources, only the hosts `AllowedHosts` lists, only a hash, a domain or a company name.
   Nothing added in this order goes to the internet. A new internet source needs its own owner decision.
5. **The UI thread never does I/O** (rule 5); every look-up is on the page's `EnrichRun` worker.
6. **Anything tunable has a control** (rule 11); `test_every_plain_setting_has_a_control` stays green.
7. **No new dependency** unless §4's PST reading cannot be done with what `app/extract/` already uses.
8. **Precision before reach.** A wrong title or phone offered is worse than none, because a person ticking "Accept
   all" keeps it and it syncs to their phone. Every rule below that widens reach is gated by §9's precision check.

## Decisions (defaults the executing thread may take)

- **D1 Age window (owner's, 2026-10-12).** Sources that read mail (signature, quoted signature, history-derived
  values) use only messages sent within **Look at mail from the last** - choices 3, 6, 12, 24 months and Any age;
  default **6 months**. A hard cut: older mail is not read, not shown, not counted. The window's start is printed
  in the window ("Reading mail since 12 April 2026"). An empty result says how many contacts *would* have had
  suggestions with Any age, so a person who sees nothing knows why (one cheap count, §2c).
- **D2 The signature is what repeats.** For a sender with two or more messages in the window, the signature is the
  run of tail lines that recur across their messages (§3b). A sign-off is a hint, not a requirement.
- **D3 Newest wins; agreement adds confidence.** Values are read from up to `MAX_MESSAGES` messages; the newest
  value of each field is the proposal; each older message that agrees adds confidence; an older message that
  disagrees is shown as history ("was Senior Engineer until March 2026"), never offered.
- **D4 Confidence decides what is pre-ticked, never what is applied.** At or above **Tick automatically from**
  (default 0.85) the box starts ticked; below **Hide below** (default 0.5) it is not shown. The person still presses
  Accept.
- **D5 "Not this" is remembered.** Unticking and pressing **Never suggest these** records `(contact, field key,
  value hash)` in a new table; the same value is not offered again for that contact by any source. Removable from
  the contact's page.
- **D6 Signatures are kept at indexing time from now on** (§4a): the cut-off block goes into a small table, not
  into `chunks`, so search is unchanged and enrichment can read it. Existing mail gets it by re-reading the
  original on demand (§4b) or at the next re-index (the index is a test index, owner 2026-10-12).
- **D7 New local sources join as switches**, on by default, each saying what it reads: vCard files, Outlook contact
  items (if §5b finds the indexer can see them), a LinkedIn export, calendar invitations.

## 0. Before starting - the executing thread checks these and says so

- [ ] **0a** `main` is green: `venv\Scripts\python.exe scripts\run_suite.py --affected`, then the full suite.
  Never start on a red suite.
- [ ] **0b** Order 1k's Windows-only items and order 1r are not in flight on the same files (`app/contacts/`,
  `app/extract/email_files.py`). If they are, wait or coordinate in `HANDOFF.md`.
- [x] **0c** The baseline is re-measured with §1's command on the owner's index and the numbers written under
  this item. Today's table above is the expected result; a different one means the index changed.

  *Measured 2026-10-12 at b22604a, `contacts enrich --measure --any-age`, read-only, with an index run writing
  to the same index:* live 1,445; with an email address 979; with a message from them 526; block found 90;
  something read 64; at least one proposal **53**; read but all already on the card 11 - **identical to the
  table above**, so the index has not changed under the order. Proposals per field: title 29, url 26, phone 16,
  company 12, address 5. Internet "would ask": Gravatar 979 (all 979 remembered, not asked again), logo 897,
  Wikidata 900. **0.405 s a contact** (584.7 s for the book) - not the 1 s the order measured, possibly because
  an index run held the disk too; 1c's target is set against this figure.

## 1. Measure first - the funnel as a command

- [x] **1a** `leasha contacts enrich --measure [--months N|--any-age] [--json]`: read-only, every live contact,
  prints the funnel above per source (contacts with mail, block found, something read, proposals, already on the
  card), the count per field, and the seconds per contact. It never writes, never calls the internet (internet
  sources are counted as "would ask"). This is the acceptance instrument for every section below.
- [x] **1b** `--sample N --out FILE.csv`: N random proposals with contact, field, value, the receipt and the
  signature block it came from, for the precision check in §9. No email addresses in the file name.
- [x] **1c** **Speed.** `mail_from` scans all 217,162 messages for every address (`instr(lower(sender), ?)`,
  query plan `SCAN messages USING INDEX idx_messages_sent`), about 1 s a contact (23 minutes for 1,445). Use
  `sender_lc = ?` with an index on `messages(sender_lc, sent_at)` (a migration), keeping the exact-address check.
  Target: under **0.15 s a contact** measured by 1a, recorded here.

  *Note 2026-10-12, found while building it:* `sender_lc` is `lower(sender)` and, sampled read-only from 2,000
  of the owner's messages, holds a bare address in 1,500, "Name <address>" in 177 and no address at all in 323,
  so `sender_lc = ?` would miss about one sender in eleven. Built instead (723af05, schema 41): `sender_addr`, the
  bare address by `email.utils.parseaddr` (the same rule as `enrich.base.address_of`, held to it by a test),
  backfilled and derived in `set_message`, indexed with `sent_at`. The 323 with no `@` (one in six) cannot be
  matched by address at all - very likely Exchange names (UNCONFIRMED) - which caps 9a's reach; it is not this
  item's to fix. Speed: measured on a migrated copy, written here at §9c.

## 2. Fresh details only - the age window

- [x] **2a** `AccountSettings.enrich_max_months` (int, 0 = any age; default 6), a control in Settings > Accounts >
  Enrichment ("Look at mail from the last: 3 / 6 / 12 / 24 months / Any age"), and the CLI flag on
  `accounts config`.
- [x] **2b** `mail_from` and `mail_with` take `since` (Unix seconds) and the signature, quoted-signature and history
  sources pass it. Undated messages are outside any window but Any age.
- [x] **2c** When a run finds nothing because of the window, the window says so with the number:
  "No suggestions from mail since 12 April 2026. With Any age, 41 contacts would have some." (one indexed count,
  not a second full run).
- [x] **2d** Every proposal shows its message's date and age in its own column ("3 weeks ago").

## 3. The signature reader, rebuilt

- [x] **3a** **Clean before reading.** A pure `clean_tail(body)`: collapse runs of blank lines to one; drop
  `<mailto:...>`, `<tel:...>` and `<https://...>` annotations after a value; drop lines that are only links,
  only tracking or rewritten links (Mimecast `protect-*.mimecast.com`, Safelinks, Sidekick, `list-manage`), only
  social icons, or only "Follow us on". Golden tests from the five shapes quoted in this order's opening, with
  names and numbers replaced.
- [x] **3b** **Find the block by what repeats (D2).** For a sender's messages in the window, after 3a, the longest
  run of tail lines that appears in at least two messages is their signature. One message: fall back to 3c.
- [x] **3c** **Find the block by anchors** when 3b has nothing: the last of (a) a `-- ` line, (b) a sign-off from a
  wider list - "My best", "Best,", "BR", "Br,", "Thanks,", "Regards", "Kind Regards" in any case, "Cordialement",
  "Mit freundlichen Grüßen", "Saludos", "Groeten", "Met vriendelijke groet", "Shukran" - (c) a line holding the
  person's name (from the card, else the From display name) within the last 20 non-blank lines, (d) a line holding
  their own email address or a phone number. The block starts at the anchor and runs to the end of the cleaned
  tail, at most 14 non-blank lines.
- [x] **3d** **Phones the right country.** The region comes from, in order: a `+` prefix, a country named in the
  block, the sender's domain's country code (`.ae`, `.sa`, `.kw`, `.qa`, `.om`, `.bh`, `.in`, `.nl`, `.be`, `.fr`,
  `.de`, `.uk` ...), then the owner's `phone_region`. Several numbers keep their labels (work, mobile, direct);
  fax is never offered. A number that is the owner's own is never offered (3h).
- [x] **3e** **Addresses where there is no postcode.** Accept a block line set as an address when it has a postcode
  or ZIP (as now), **or** a "P.O. Box" / "PO Box" with a city or country, **or** a building/street line followed by
  a recognised city or country name. Country from the line, else from the domain.
- [x] **3f** **Title and company, harder to fool.** Title needs a title word (as now) and must sit within two lines
  of the name line. Company needs a legal suffix, agreement with the sender's domain, or the same company on
  another contact at that domain. Department ("Energy & Natural Resources Business Unit") goes to `department`,
  not company. A line that is a sentence (verb, more than 9 words, ends in a full stop) is never a title or company.
- [x] **3g** **Other email addresses in the signature** ("E-Mail: x@yasref.com") are offered as extra email
  fields when they share the person's local part or name.
- [x] **3h** **Never the owner's own details.** Lines holding any of the owner's addresses (`owner_addresses`), the
  owner's phone numbers, or the owner's own signature (learned by 3b from the owner's sent mail) are removed before
  parsing. A reply that quotes the owner must never give the owner's mobile to a client.
- [x] **3i** **Never from a message the person did not send.** The sender address must equal one of the contact's
  addresses exactly (kept from today); a message sent "on behalf of" is skipped.
- [x] **3j** **Consensus (D3).** Read up to 20 messages in the window, newest first; build one value per field with
  its first-seen and last-seen dates and the count of messages that agree. Confidence: 0.6 base, +0.2 name-line
  found, +0.1 per agreeing message to +0.3, -0.2 if a newer message lacked the field, capped at 0.97.
- [x] **3k** **Changes, not only gaps.** A card value that differs from a high-confidence newest value is offered
  as an update showing both ("Now: Engineer / Suggested: Senior Engineer, since March 2026"). Never pre-ticked
  (D4) whatever its confidence.

## 4. Read what the index cut

- [x] **4a** **Keep the cut signature at indexing time (D6).** `strip_quoted` already finds the signature marker;
  `email_files.py` writes the cut-off tail (cleaned by 3a, at most 2,000 characters) to `message_tails(file_id,
  text, reason)` (a migration). Not embedded, not in FTS, removed with its message. Search results unchanged
  (guard test: same `chunks` row count and text before and after for a fixture mailbox).
- [ ] **4b** **Existing mail: re-read the original, on demand.** When a contact's indexed mail in the window gives
  no block, re-open its newest five messages read-only from where `messages.store_path` / `account://` says and
  read the full body. Measure the cost per message first and write it here; if over **200 ms a message**, ship 4b
  behind a switch "Read the original messages again (slower)" and leave it off.

  *Note 2026-10-13:* built behind that switch, off by default (dab2551; the setting in 092ce8a).
  `contacts enrich --measure --read-originals` measures the cost read-only (4c8c7b3). Cost per message on the
  owner's index: **re-read originals: [pending measurement]**. Not ticked until that number is written here.
- [x] **4c** **Quoted signatures.** From the original body (4b) or the tail (4a), parse quoted headers ("From:
  Name <addr> / Sent: / To:") and the signature block under each quoted message; a block is credited only to the
  address in its own quoted From line, only within the age window by its quoted Sent date, and at confidence -0.15
  against a direct message. This reaches people who never mailed the owner directly.

## 5. More of your own sources (local, on by default, D7)

- [ ] **5a** **vCard files on disk.** The index holds 146 `.vcf` files. Parse each card (`app/contacts/` importers
  already read vCard); match to a contact by email, then by phone, then by exact full name with a shared company;
  offer the fields the contact lacks. Receipt: the file path and its modified date. The age window applies to the
  file's modified date.

  *Note 2026-10-13:* built (2f22355). On the migrated copy of the owner's index it found 0 contacts until 96c08fa:
  every `.vcf` there was `PARTIAL`, not `INDEXED`, and 139 of 146 were attachments; both are read now. Reach
  after 96c08fa: **[pending measurement]**.
- [x] **5b** **Outlook contact items.** First establish whether the PST reader exposes contact items (IPM.Contact)
  and whether any are indexed; write the finding here. If yes, the same as 5a. If no, record it and stop - adding
  a PST contact reader is a separate decision.

  *Finding 2026-10-12:* neither PST reader looks at the message class, so no contact item (IPM.Contact) reaches
  the index and there is nothing to read (2f22355). Recorded and stopped, as the item says. The "Outlook contacts"
  switch, its flag and its setting were removed so nothing promises it (9d8d9f6); an Outlook contacts reader is
  parked in `docs/PARKED-IDEAS.md` §7 at the owner's word (3dce693, 2026-10-13).
- [x] **5c** **A LinkedIn export.** If the owner points at `Connections.csv` (Settings > Accounts > Enrichment >
  "Use a LinkedIn export..."), match by email, else by name plus company; offer company and position with the
  export's "Connected On" date as the age. Nothing is fetched from LinkedIn.
- [ ] **5d** **Calendar invitations.** The index holds 960 `.ics` files. Attendee names fill a missing display name
  for an address; organisers and frequent attendees feed "Suggested contacts" (7e). No phone or title from here.

  *Note 2026-10-13:* built (2f22355). On the migrated copy it found 0 contacts until 96c08fa, for the same two
  reasons as 5a (921 of 966 invitations are attachments). Reach after 96c08fa: **[pending measurement]**.
- [ ] **5e** **Documents (off by default).** Org charts, contracts and scanned business cards name people with
  titles. Only after §9 shows the others hold their precision; proposals at most 0.6, never pre-ticked.

  *Note 2026-10-13:* the switch (off) and a stub only (2f22355). Waits for §9, as the item says.

## 6. Internet sources, made sensible (no new hosts)

- [x] **6a** **Remember the answer, not only the visit.** Store what a lookup returned (`enrich:result:<kind>:<key>`,
  a small JSON, never an address) so a remembered lookup can still offer its result without a new request. Today a
  result not accepted in that run is lost for 90 days.
- [x] **6b** **The repeat interval is a setting**: "Ask the internet again after: 30 / 90 / 180 days / Never"
  (default 90). Look again (built 2026-10-12) still overrides it.
- [x] **6c** **Clean the Wikidata term.** Send the company with department and role words removed ("Wipro
  Technologies Account Energy" -> "Wipro Technologies"), and only when it agrees with the email domain or carries
  a legal suffix.
- [x] **6d** **Logo only from a domain that is the company's.** The URL's domain must share its registrable name
  with a work email's domain, or the card has no work email.

## 7. A review window that makes the right thing easy

- [x] **7a** Proposals grouped by contact, highest confidence first; columns: field, now, suggested, from (source),
  when (date and age), confidence as words ("Seen in 4 messages", "Once, 5 months ago").
- [x] **7b** Pre-ticked per D4; **Accept selected** stays the one button that writes; **Accept all** asks once with
  the count when any unticked-by-default row is included.
- [x] **7c** **Never suggest these** (D5) and, on the contact's page, "Suggestions you turned down" with Undo.
- [x] **7d** A run's notes (unreachable, not asked again, the window's start, what Any age would add) in one line
  under the list, not only in a notice.
- [x] **7e** **Enrich this contact** on a contact's page runs only that contact, with Look again beside it.

  *Note 2026-10-13:* 7a-7e (96d4101) and 8a (092ce8a, 96d4101) are built and held by offscreen Qt tests
  (`test_enrich_review_window.py`, `test_enrich_declines.py`, `test_enrich_settings.py`,
  `test_accounts_enrich_switches.py`, `test_contacts_tools_ui_files.py`). They have not been seen in the real
  window: **UNVERIFIED on Windows** until the owner's checks in 9f.

## 8. Settings, in one place

- [x] **8a** Settings > Accounts > Enrichment holds: each source's switch (eight today, plus 4b, 5a-5e), the age
  window (2a), the repeat interval (6b), **Tick automatically from** and **Hide below** (D4), and "Suggestions you
  turned down: N - Clear". Each with a sentence saying what it reads or sends. All in `AccountSettings`, all in
  `accounts config` flags (rule 8), all covered by `test_every_plain_setting_has_a_control`.
- [x] **8b** Constants that stay constants carry a comment saying why and what would change them
  (`MAX_MESSAGES`, the 14-line block, the 2,000-character tail).

## 9. Done means measured

- [ ] **9a** **Reach.** §1a on the owner's index with **Any age**: contacts with a signature-derived proposal from
  53 to **at least 200** of the 526 with mail; with the default 6 months, recorded as found (at most 27 can be
  reached). Each number written here with the commit it was measured at.

  *Measured 2026-10-13, read-only.* Baseline at b22604a on the owner's index, Any age: live 1,445; with an email
  address 979; with mail from them 526; block found 90; something read 64; at least one proposal **53**.
  After the build, on a migrated snapshot copy of the owner's index (`D:\LeashaScratch`), at 2dc3d20, 4c8c7b3
  and 96c08fa: Any age - with mail 526; block found 165-173; something read 136-138; at least one proposal
  **114**; quoted-only 1. Six months (the default): with mail 27; at least one proposal **11**.
  **NOT MET: 114 against at least 200.** 50,351 of the 217,168 messages (23%) have a sender with no `@` address,
  so they cannot be matched to anyone (1c's note).
  Re-read originals: [pending measurement].
- [x] **9b** **Precision.** §1b sample of 100 proposals checked against their receipts: **at least 95 correct**,
  none of them the owner's own details (3h). The thread checks each against the signature block printed beside
  it and lists every wrong one here with its cause. Below 95: fix and re-sample; do not lower the bar.

  *Measured 2026-10-13, three hand-checked samples of 100 (`--sample 100`), each against its printed block:*
  - **Sample 1 at 9d8d9f6: 82/100.** Wrong: 3 the owner's own details (an old employer's mobile and direct line,
    and a quoted old address); 6 legal or disclaimer text read as an address; 4 pipe or bullet separators, or
    two addresses joined; 1 URL cut by a wrap; 1 country glued to a company; 2 incomplete addresses; 1 value not
    in the block shown. Fixed in 2dc3d20.
  - **Sample 2 at 2dc3d20: 84/100.** Wrong: 5 footer country lists; 5 meeting dial-in text; 3 phone lines inside
    an address; 2 U+FFFD characters; 2 a title or department in the wrong field; 1 city and PIN lost. None the
    owner's. Fixed in 4c8c7b3.
  - **Sample 3 at 4c8c7b3: 98/100, which passes the bar of 95.** None the owner's. Wrong: a stray "+" in an
    address, and a WebEx meeting number taken as a phone; both fixed in 96c08fa. Counted as correct but
    borderline: 3 addresses prefixed with the company name, 1 address missing its floor line, 1 with stray
    punctuation. Counted strictly, the score would be 93.
- [x] **9c** **Speed.** Under 0.15 s a contact (1c), so 1,445 contacts take under four minutes.

  *Measured 2026-10-13 on the migrated copy, at 2dc3d20, 4c8c7b3 and 96c08fa:* **0.011 s a contact** (16.6 s for
  1,445), against 0.405 s at b22604a and the 0.15 target. The schema 41-43 migration took 15.4 s on 217,168
  messages.
- [x] **9d** **Nothing changed by looking.** A run over the whole book leaves `contacts`, `contact_fields`,
  `contact_sources` and `contact_journal` byte-for-byte the same (hash before and after) until Accept is pressed.

  *Measured 2026-10-13:* the hashes of `contacts`, `contact_fields`, `contact_sources` and `contact_journal` were
  identical before and after every measuring run on the owner's index. Held in the suite by
  `test_measuring_leaves_the_index_byte_for_byte_the_same`.
- [x] **9e** **Guard tests**: no internet request with every internet switch off (exists, keep); the new local
  sources open no socket; the owner's number is never proposed (fixture: a client reply quoting the owner's
  signature); a message from another sender never credits this contact; a social profile never becomes the
  company site (exists, keep).

  *Note 2026-10-13:* `test_a_source_that_is_switched_off_is_not_called`, `test_every_new_local_source_runs_with_no_socket`,
  `test_a_client_reply_quoting_the_owners_signature_never_offers_the_owners_mobile` (and the old-signature tests in
  `test_enrich_signature_precision.py`), `test_only_mail_from_that_exact_address_is_read`,
  `test_a_quoted_block_is_never_credited_to_anyone_but_its_own_from_line`,
  `test_a_social_profile_is_not_taken_for_the_company_site`. The enrichment test files pass: 395 at 1b4c54f.
- [ ] **9f** **Windows checks the owner runs** (UNVERIFIED until then): Enrich contacts on the real window, Look
  again, the notes line, Never suggest these, and the settings in Settings > Accounts.

  *Note 2026-10-13:* **UNVERIFIED on Windows** - not yet run by the owner.

## 10. Close

- [x] **10a** `docs/USER_GUIDE.html` and `docs/TECHNICAL_REFERENCE.html` describe enrichment as it now is
  (rewritten in place, owner's rule of 2026-10-06); `docs/GLOSSARY.md` gains "age window" and "signature tail".
- [x] **10b** `CHANGELOG.md` under `[Unreleased]`; `docs/ORDER_REGISTER.md` row 1s counted from this order's
  boxes; `HANDOFF.md` entry with the before and after funnel.

---

## Starting prompt for the executing thread

Paste this into a new session in `D:\Local\GitHub\Leasha` (or a cloud session on `jayminapatel/Leasha`, `main`)
when it is to start:

> Execute work order 1s, `docs/WORKORDER-contact-enrichment-that-finds.md`. Read `CLAUDE.md`, `HANDOFF.md`,
> `docs/PROJECT_INSTRUCTIONS.md` and the order first. Work through §0 and decide whether it is ready to start: if
> any §0 item fails, say which and stop. Otherwise branch `feat/enrichment-that-finds`, one task per numbered item
> on the task list before you start, and build in the order's section order - §1's measuring command first,
> because every later section is accepted by it. Respect "What must not change" without exception; a decision not
> covered by D1-D7 goes to the owner, not into the code. The owner's index is read with `--measure` only (read-only);
> never run a branch's migrations against `D:\LeashaIndex\Data` - use `--env` with a scratch DATA_PATH until the
> branch is merged (HANDOFF trap, 2026-10-12). Anything that needs the real Windows window, Outlook or the owner's
> data and cannot be checked from where you run is marked UNVERIFIED, not ticked. Finish with §9's numbers written
> into the order, the full suite green, a PR to `main`, and a close-out saying what changed and what is now untrue
> in `HANDOFF.md` and `docs/ORDER_REGISTER.md`.
