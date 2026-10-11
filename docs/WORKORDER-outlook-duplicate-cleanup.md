# Work order (One thread): copies of a message in a mailbox are moved to its Deleted Items, from a button on its line

**Doc version:** 1.1 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/extract/outlook_cleanup.py`, `app/cli/`, `app/ui/widgets/mail_archives_box.py`,
`app/ui/controllers/`, `app/ui/presenter/`, `app/ui/shell.py`, `tests/unit/`, the user guide)
**Status:** BUILT 2026-10-11, all 10; RELEASED by the owner 2026-10-11 ("yes write the order for the outlook clean-up and execute"), after
order 1j removed the copies from the index and the owner asked whether they could go from the mailbox too.

**Where this came from.** Order 1j §3 found, on the owner's index, 3,458 messages that an Outlook archive holds
twice (same text, subject, sender and sent time), every pair inside one `.pst` - most in `2011.pst` (1,664) and
`2018.pst` (1,224). The index now holds one copy of each and never adds the second. The mailbox still holds both.

**What must not change.** Leasha's promise elsewhere stands: indexing, search and every report never write to a
mail archive. This order adds **one** action that does, and only when the owner presses it, names the count and
says yes. Nothing is ever deleted outright: a copy is **moved to that mailbox's own Deleted Items**, where Outlook
can put it back. The index is not touched by this action.

---

## Decisions (defaults the building thread may take; say which were taken at close-out)

- **D1 Read through Outlook, not the index.** The index no longer holds the copies (1j §3), and only Outlook can
  move an item. The mailbox is walked through Outlook (classic), as the existing Outlook reader does
  (`email_pst.Win32ComSession`), which attaches an archive only if Outlook does not already have it and detaches
  only what it attached.
- **D2 What a copy is.** Within one mailbox: the same subject, sender, sent time, body text and attachment names.
  Stricter than 1j's rule by the attachment names, because here the action changes the owner's mail.
- **D3 Which copy stays.** The first found by a stable walk (folders in Outlook's order, items by received time).
  Items already in Deleted Items are neither counted nor moved.
- **D4 Where the button is.** A third icon button on each line of Settings > Mail archives, beside "Read again"
  and "Clear and read again".

---

## 1. Finding and moving

> *2026-10-11, built:* `app/extract/outlook_cleanup.py`. Candidates come from each folder's table (`Folder.GetTable`,
> `GetArray` - subject, sender name, sent time, size), and only items that agree on all four are opened and compared by D2.
> Measured on `2007.pst`: 345 rows in 0.91 s by `GetArray` against 25 s row by row. Falls back to opening every item when a
> folder has no table. D1-D4 taken as written.
- [x] **1a** `outlook_cleanup.find_copies(path)` walks one mailbox through Outlook and returns the copies by D2,
  with each kept item, its folder and subject. Reads only.
- [x] **1b** `outlook_cleanup.move_copies(path, plan)` moves each copy to that store's Deleted Items, checking first
  that the item still has the subject and time it was found with. Returns how many moved and how many were left.
- [x] **1c** `leasha outlook-dedupe <archive> --dry-run` prints the count and five examples; `--yes` moves them.

## 2. The button

> *2026-10-11, built:* the Duplicates column (`MailArchivesBox.copies_requested`, icon `copy`), `OutlookCleanupController` (count on a worker, `ask`, move on a worker), wording in `presenter/mail_archives.py`.
- [x] **2a** Each Mail archives line gets "Move duplicates to Deleted Items" (D4). Pressed: the copies are counted on
  a worker, the owner is asked with the number, and only a yes moves them, on a worker; the result is said.
- [x] **2b** No copies: it says so and asks nothing. Outlook not installed, or busy: a sentence, not a failure.

## 3. Tests and documents

- [x] **3a** Tests with a fake Outlook: copies found by D2 (not by subject alone), Deleted Items left out, moved only
  after a yes, an item that changed since it was counted left where it is, the archive detached afterwards only if
  Leasha attached it.
- [x] **3b** The user guide's Mail archives section says what the button does and that copies can be restored from
  Deleted Items.
> *2026-10-11, dry runs on the owner's laptop (read-only, while an index run was going):* `2007.pst` - 345 items, no
> copies (75 s, before `GetArray`). `2008.pst` - 1,838 items, **1 copy**, "RE: Overtime Approval": the same pair order 1j
> found in the index (`pst://2008/2108548` / `2108580`). 280 s, before `GetArray`. Nothing was moved.
- [x] **3c** On the owner's laptop: a dry run on one archive, the count recorded here. **Moving is the owner's
  press of the button**, never a thread's.

## Close-out

Tick each box only when built and tested. Update this order's Status line, the register row
(`docs/ORDER_REGISTER.md`, 1l) and `HANDOFF.md`, and name which of D1-D4 were taken as written.
