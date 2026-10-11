# Work order (One thread): Mail shows the mailboxes and their folders on the left, and a click narrows the list

**Doc version:** 1.0 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/ui/mail_view.py`, `app/ui/widgets/`, `app/ui/tasks.py`, `app/storage/sqlite_store.py`, `tests/unit/`, the user guide)
**Status:** RELEASED by the owner 2026-10-11 ("b do them all"), after asking what a View menu like the Photos tree's would hold on Mail, Files, Code and Browse

**Where this came from.** The owner likes the Photos tab's list on the left and asked for the same on the other
pages; the thread's recommendation, taken, was Mail first: every message already carries its archive
(`messages.store_path`) and its folder in it (`messages.folder_path`, schema 32), so nothing new has to be read.

## Decisions (defaults the building thread may take)

- **D1 What the list shows.** Each mail archive, then its folders as Outlook nests them (Inbox, Inbox/Projects ...),
  each with how many messages it holds. "All mail" at the top.
- **D2 A click narrows** the Mail list to that archive or folder and its sub-folders, together with whatever is
  typed in the box; clicking "All mail" takes it away. It is said in the summary line.
- **D3 Switched like the Photos list**: a panel icon beside Preview and View, and View > **Folders**, remembered as
  `ui:mail_folders`. On by default.

## Items

- [ ] **1a** `SqliteStore.mail_folder_counts()` - `(archive, folder, messages)`, one grouped read, on a worker.
- [ ] **1b** The Mail list honours an archive and folder (`browse_messages_typed`, and its count).
- [ ] **1c** `MailFolderList` on the left of Mail (D1), the click (D2), the switch (D3).
- [ ] **2a** Tests: counts, nesting, the narrowing with and without typed words, the switch remembered.
- [ ] **2b** The user guide's Mail section says what the list does.
