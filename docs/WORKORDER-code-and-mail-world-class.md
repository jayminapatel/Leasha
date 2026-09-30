# Work order (One thread): the Code tab and the mail preview, world class

**Doc version:** 1.2 · **Updated:** 2026-09-29 · **Applies to:** app v0.3.3
**Created:** 2026-09-27 · **Layer:** L1/L4/L5 - `app/search/gitsearch.py`, `app/search/gitquery.py`, `app/ui/code_view.py`, `app/ui/widgets/code_results.py`, `app/ui/widgets/git_tree.py`, `app/ui/presenter/code.py`, `app/ui/preview_loader.py`, `app/ui/mail_view.py`, `app/storage/sqlite_store.py`
**Thread:** One thread, in small PRs, each merged by the owner
**Status:** RELEASED *(owner, 2026-09-27: "design it ... and build straight away")*

## Why

The owner, 2026-09-27, on the two PARKED orders (`202626081149` the Code tab, `202626081801` git
sharpness and mail preview):

> *"think you are World Class coder and design it from that perspective, what you would need to
> get the most use of it and how most people would like to use it and feel this is exactly what is
> needed."*

Asked who it is for, the owner answered: **a developer (the owner), everyday people, and both**.

**Both parked orders are mostly built already** (see the register's dated note, 2026-09-27):
081801 §1-§3 and 081149 §2-§3 shipped (`CHANGELOG.md`, "Fixed — the git tree listed every
repository ...", "Fixed — mail previews now show the message ...", "Fixed — repository
attribution can be undone ..."). This order starts from what exists and asks what would make each
surface feel finished, to the people who use it.

## Who uses it, and what they are really asking

| Person | Surface | The question in their head | What "world class" feels like |
|---|---|---|---|
| **A developer** (the owner) | Code | "Where is `ResetPasswordHandler` defined, and who uses it?" | Type the name, and the definition is the first row, with the line and a snippet. Enter opens the editor **at that line**. |
| A developer | Code | "When did this line appear, and why?" | The history answer arrives as it is found, can be stopped with Esc, and a commit opens as its message, files and diff. |
| A developer | Code | "Why is this folder called a repository?" | One click: "Ignore this repository", undoable, with no command line. |
| **Anyone** | Mail | "What did the school say about the trip?" | The message looks like an email: who, when, subject, attachments; the words searched for are highlighted; the rest of the conversation is one click away. |
| Anyone | Mail | "Show me the real thing." | A button that opens the message in Outlook (or the file in its own program). |

## Principles (each is an existing rule, restated for this order)

1. **Typing never waits.** Anything behind a keystroke is an indexed query under the typing budget
   (`BUILD_SPEC_V2.md`: typing tier under 40 ms). Git never runs behind a keystroke (HANDOFF
   decision "History search is its own job").
2. **One box.** The Code tab keeps one box and one list; the grammar picks the engine
   (`presenter.code_route`). Nothing new asks the person to choose an engine first.
3. **Honest.** A slow thing says it is slow, a stopped thing says it stopped, and nothing hidden is
   hidden silently (081149 §4-§5, 081801 §3's quoted-text notice).
4. **Read-only against user data** (non-negotiable #10): nothing here writes to a repository or a
   mailbox. Opening in Outlook or an editor hands the file to that program.
5. **No new dependency** unless the case is made in this order. Everything below uses what is
   installed: PyQt6, SQLite FTS5, git on PATH.
6. **No existing label is reworded.** "Search history", "Git view" and the mail headers stay as they
   are; new controls get new labels.
7. **Windows first, Mac second** (order 0x): every subprocess goes through a helper that hides the
   console on Windows and does nothing elsewhere.

## 1. Fixes first (small, no design question)

> **2026-09-27, §1 built.** 1a: `osbridge.hidden_console_flags()` (`CREATE_NO_WINDOW` on Windows,
> 0 elsewhere), used by `gitsearch._run`, the one place git is started. 1b: `gitsearch.StopFlag`;
> `_run` waits in 0.1 s steps and ends git when the flag is flipped (measured: well under the 0.5 s
> budget); the button reads "Stop" while a search runs and Esc does the same; a newer search stops the
> older one; the result says "History search stopped". 1c: the row menu's "Ignore this repository"
> (`widgets/repo_ignore.py`) asks once, forgets on a worker, and offers Undo in a note under the
> summary; Undo is `SqliteStore.restore_repo` with the record `repo_undo_record` read beforehand -
> exact and immediate, never taking back a file another repository has claimed since. 1d was done in
> the design PR. Tests: `tests/unit/test_code_fixes_0y.py` (11).

- [x] **1a** **git flashes a console window on Windows.** `gitsearch._run` calls `subprocess.run`
      with no `creationflags`; the window runs under `pythonw.exe`, so every history search, branch
      listing and `/author` value lookup opens and closes a black console window. Fix: one
      `osbridge` helper, `hidden_console_flags()`, used by every git call.
- [x] **1b** **A history search cannot be stopped.** It runs up to 120 s with no way out. Esc in
      the box, or the same button (which reads "Stop" while it runs), ends the git process at once.
      A newer search also ends the older one.
- [x] **1c** **"Ignore this repository" on the row menu** (081149 §2 item 1, built only as
      `app.cli repos --forget`). The row menu offers it for any row in a repository, says what it
      does in the confirmation, and a note under the list offers "Undo" until the next search.
- [x] **1d** The register and `WORKORDER-CONVENTIONS.md` rows for 081149 and 081801 are corrected
      with dated notes (both still read as not built).

## 2. Code: search inside the code as you type

> **2026-09-29, 2a, 2b and 2d built; 2c next, in its own PR.** `app/search/code_search.py`: one
> `keyword.search` over the code scope (so `/repo` and `/type` mean what they mean elsewhere), then each
> passage read line by line - a line declaring a name that contains the word is a Definition (the git
> grammar's `SYMBOL_PATTERNS`, loosened so `password` finds `ResetPasswordHandler`), any other line
> holding it a Mention. **Line numbers come from the file, not the index**: the indexed text of a code
> file folds blank lines (measured: 551 lines indexed for a 565-line file), so `resolve_lines` reads
> the real file on the worker and finds the line nearest the passage, inside a 30 ms budget; a row past
> the budget, or whose file has changed, shows no number rather than a wrong one. Measured: 2 ms a
> keystroke on this repository's 3,467 passages, under 40 ms at 2,000+ in the test. New columns Match,
> Line and Code appear only once something is typed. Tests: `tests/unit/test_code_search.py` (10).

> **2026-09-30, 2c built.** Most of it was already in the tree and unused: the editor table and command
> builder (`app/ui/editors.py`), the finder for editors not on `PATH` (`osbridge/programs.py`), and the
> setting with its control in Settings (`CODE_EDITOR`, `CODE_EDITOR_COMMAND`, `widgets/editor_box.py`).
> **The "Editor for code" setting is that existing one**, labelled "Open code results in" under
> "Opening code results" (Automatic, an installed editor, None, or a command of the person's own); no
> second setting was added and its label was not reworded. What was missing was every caller: nothing
> ran `command_for`, so Enter handed the file to its usual program whatever the setting said, and the
> Code table had no Enter key at all. Built: `CodeResults.open_selected` sends a row that knows its
> line (a Definition, a Mention, a git hit in the checkout) as `open_at_requested(path, line)`; Enter on
> the table does what a double-click does; `workers.open_at_line` (on a worker, never the UI thread)
> builds the command, starts the editor with no console window, and otherwise opens the file in its
> usual program and says so with the line number; `MainWindow._open_code_at` reads the setting in
> force, so a choice just made in Settings applies to the next Enter without a restart. A row with no
> line opens as before. The editor order is now the one written here (VS Code, Notepad++, Sublime;
> Cursor and VSCodium sit with VS Code). The row menu gained "Copy path and line", which the Settings
> note already promised. A terminal editor (Vim, Neovim) is given a console of its own
> (`osbridge.new_console_flags`) - **UNCONFIRMED on a real window**, as is A5. On this machine
> Automatic resolves to `...\Microsoft VS Code\bin\code.CMD -g <path>:<line>` (the command was built
> and read, not run). Tests: `tests/unit/test_code_open_at_line.py` (20); no test starts an editor.

Today typing matches **file names and paths** (`browse_files` scoped to code). Finding where a
function is used means leaving for the Search tab and adding the Code chip. For a developer that is
the main question, so it belongs in the Code tab's own box.

- [x] **2a** Typing searches **names and contents together**, in one indexed query each, off the UI
      thread, inside the typing budget. The list shows, in this order:
      1. **Definitions** - passages where the typed word is being *defined* (`def`, `class`,
         `function`, `interface`, a `name =` at the start of a line), using the `SYMBOL_PATTERNS`
         the git grammar already has, applied to the passage text in Python after FTS narrows it;
      2. **Files** whose name matches (today's rows, unchanged);
      3. **Mentions** - other passages containing the word.
- [x] **2b** Each content row shows the **line number** and **one line of code** with the word
      highlighted. The line number is computed from the passage's `char_start` in the stored text
      (no file read), so it costs nothing extra on screen and stays correct after indexing.
- [x] **2c** **Open at the line.** Enter (or double-click) opens the file in the person's editor at
      that line when one is found - VS Code (`code -g path:line`), then Notepad++ (`-n`), then
      Sublime (`path:line`) - otherwise the default program. A new "Editor for code" setting
      (Automatic, or a program chosen by the person) under Settings, per non-negotiable #11.
- [x] **2d** The summary keeps its current shape and adds the split:
      `3 definitions · 12 files · 48 mentions`.

## 3. Code: history that answers as it goes

> **2026-09-30, 3a and 3b built.** `gitsearch._stream` starts git with `Popen` (the same hidden-console
> flag as `_run`) and hands over each line as it is printed; a reader thread feeds a queue so the Stop
> flag and the time limit are looked at every 0.1 s even while git prints nothing. `stream_query` reads
> those lines with the four readers `run_query` already had (`_LineReader` calls them, so the two cannot
> disagree). `search_repositories` runs one git per repository, at most two at a time
> (`MAX_PARALLEL_REPOS`), reports rows as they are found, and merges the result newest first; a
> repository that fails is named with git's own reason and the others carry on; the same commit seen in
> two checkouts is listed once. In the window (`widgets/git_tree.py`) no `/repo` now means every
> repository - only a name that matches none is still answered "Name a repository first" - the live
> line reads `Searching history… 12 found so far` (with several repositories, `· 1 of 3 repositories
> searched`), the row somebody has selected stays selected while more arrive, and Stop keeps what had
> been found. The Repository column holds the repository's name for these rows; a commit's short id
> and author moved to Where. `app.cli gitsearch --every-repo` does the same headless.
> **Three things found by measuring.** (1) `--pretty=format:` puts the newline between commits, so a
> row was only complete when git found the next: the last row of a search arrived when git ended
> (4.2 s of 4.2 s). It is `tformat:` now (1.1 s of 4.5 s). (2) **§1b's Stop did not end git on
> Windows.** The `git` on `PATH` is a launcher that starts the real git as a child; `kill()` ended the
> launcher and the real git read on to the end, holding the pipe - Stop came back 0.8 s to 2.3 s late.
> `osbridge.programs.git_program()` starts the real git (`mingw64\bin\git.exe`; its output was compared
> byte for byte with the launcher's for `--version`, `config --list`, `log -S`, `grep`, `show`) and
> Stop now returns 0.05 s to 0.29 s after it is asked, measured on real git in this repository. An
> install laid out differently still gets the `git` on `PATH`, and then Stop returns at once but that
> git may run on to its own end. (3) Drawing the Code list costs about 0.6 ms a row on the interface
> thread (47 ms for 50 rows, 99 ms for 200, 1.3 s for 2,000), so while git runs the list shows the
> newest 200 and says so, rows are handed over at most four times a second, and the whole list is
> drawn once at the end. That final draw of a 2,000-row result is still over a second; it was before
> this order too, and is not fixed here. **Measured:** first row shown 0.06-0.19 s into a history
> search that took 1.7-2.2 s (real git, this repository, 3 rows); with the fake git that prints one
> row and then takes 1 s, the first row is asserted at least 0.7 s before the end. "Newest first"
> across repositories is exact to the day, because the date git is asked for is the day. A6 is
> covered headless (`test_rows_from_every_repository_are_merged_newest_first`, and a real run over two
> checkouts and a folder that is not a repository); **A1 and A2 on the real window are the owner's.**
> Tests: `tests/unit/test_git_streaming.py` (38), `tests/unit/test_code_history_live.py` (26).

> **2026-09-30, 3c built.** A history row now carries its commit, the repository folder and the text
> that was searched for (`presenter.git_result_row`). `preview_loader.load_preview_for` sends such a row
> to `app/ui/commit_preview.py`, which runs `gitsearch.show_commit` - one `git show --raw --patch -m
> --first-parent`, read line by line and ended at 3,000 diff lines - and builds the page: the subject
> as a heading, the message, the author with the date in words (*Tuesday 2 January 2024, 09:00*), the
> commit id, `3 files changed` with Added / Modified / Deleted / Renamed beside each, and the diff with
> added lines green, removed lines red and the searched text dark on yellow wherever it occurs
> (whatever its capitals). Everything that came from the repository is escaped, so markup in a commit
> message is shown and not obeyed. It runs on the pane's own worker after the pane's usual 200 ms
> pause, so only the selected row ever starts git; a diff that was cut says so, and a commit git
> cannot show leaves the row's own three lines with git's reason above them. A merge shows what it
> brought in (against its first parent). A changed line from `/added-only` or `/removed-only` is
> history too: it used to preview today's file, and now shows its commit. `app.cli gitsearch --repo
> <folder> --show <id>` prints the same headless. **Measured:** 413 ms from selection to a finished
> page for a real commit of this repository (26,941 characters of page), on the worker. **Not
> built:** the pane does not scroll to the first highlighted place, and F3 does not step between
> them - Ctrl+F in the pane finds the text; §4b builds next/previous for mail and the same keys could
> serve here. The highlight colours were chosen to read on a light and a dark page but were **not
> looked at in the real window** (owner check). Tests: `tests/unit/test_commit_preview.py` (13), and
> `show_commit` against invented and real git in `tests/unit/test_git_streaming.py`.

> **2026-09-30, §3 run end to end without a screen.** The Code tab was built offscreen with a real
> event loop, the real worker pool and real git, over three "repositories" (this checkout, a second
> checkout of it, and a folder that is not a repository): the first row was drawn 0.17 s in, four rows
> by 0.41 s, git finished at 2.02 s, every draw happened on the interface thread, the summary named
> the folder that could not be searched, the shared commits were listed once, and selecting the first
> row drew its commit with the highlight. Drawing the largest page the pane can be given (3,000 diff
> lines, 448,037 characters) costs 88 ms on the interface thread; building it costs 15 ms on the
> worker. None of this replaces the owner's look at the real window (A1, A2, A5, A6).

- [x] **3a** **Streaming.** Rows appear as git prints them (`Popen`, read line by line on a
      worker), newest first, with a live line: `Searching history… 12 found so far`.
- [x] **3b** **Every repository at once** when none is named: one git process per repository, at
      most two at a time, results merged by date. Today it answers "Name a repository first".
- [x] **3c** **A commit opens as a commit.** Selecting a history row shows, in the preview pane:
      the message, author and date, the files changed, and the diff with the searched text
      highlighted (`git show`, on a worker, only when selected).

## 4. Mail: a preview people recognise

> **2026-09-30, 4a built.** A message is previewed as a message from every list whose pane holds a
> store (Mail and Search): `preview_loader.mail_preview` asks `messages` by `file_id` on the worker,
> and the pane draws `widgets/mail_card.py` in place of its title line - subject as a heading, the
> sender large with the address beside it, recipients, the date in words, one chip per attachment.
> The words are decided without Qt in the new `presenter/mail.py`; the day is worded by
> `timeline_words.day_heading` with the time added (no second formatter). The typed body is now the
> message's own words only; Select All and Copy still give the plain `From: ...` block and the
> message (`MailBody`), and "Pin in a window" is unchanged. A Mail row draws its card at once from
> the row, before the read lands, so arrowing the list does not flick between a title and a card.
> **Three limits, each from what the index holds, none fixed here:** (1) To and Cc are one list in
> `messages.recipients`, so the card shows them on one line labelled "To" with a tooltip saying so -
> showing Cc apart needs a schema column and a re-index; (2) the readers keep the sender's address
> and drop the display name, so the large line is usually the address; (3) attachment names are read
> back from the `Attachments:` line the indexer writes above a message's text, split on ", " - a
> file name holding ", " becomes two chips. Tests: `tests/unit/test_mail_preview_card.py` (27).
> Grabbed to a PNG, light and dark, and looked at: the card reads as an email header; the chips
> needed 4px of padding to round (Qt draws no rounding under 22px tall).

> **2026-09-30, 4b built - and what "as the file preview does" turned out to mean.** The file
> preview had Ctrl+F (a find box: type a word, Enter steps) and nothing else: no searched word was
> highlighted on its own and F3 was bound nowhere (`Key_F3` did not appear under `app/`). So it is
> built once, in the pane, for a file and a message alike: `widgets/search_marks.py` paints the
> searched words over the text in the find box's own colour and F3 / Shift+F3 step between them,
> wrapping; a line above the text says `3 matches of what you searched for - F3 for the next,
> Shift+F3 for the previous`, then `2 of 3`. Which characters count is `snippets.term_spans` - the
> result snippets' own rule (a word from its start, any case), with positions corrected to Qt's
> UTF-16 counting so a word after an emoji is highlighted where it is. The words come from the list
> the pane is attached to: Search's typed terms (`ResultsView.explain_context`), and on the Mail tab
> the `/subject` value plus any plain words typed (`presenter.mail.mail_terms`). While the find box
> is open with something typed, the highlight and F3 are its; closing it puts the searched words
> back. Plain text only (a message, a text file): an HTML or Markdown preview keeps Ctrl+F.
> Tests: `tests/unit/test_mail_preview_marks.py` (19), A8 among them on the Mail tab.

> **2026-09-30, 4c built.** `SqliteStore.conversation_messages` is the one statement, on
> `idx_messages_conv`, returning the Mail list's own row shape plus the start of each message's first
> passage (a correlated lookup on `idx_chunks_file_ord`, inside the same statement). It is asked on
> the preview's worker with the message (`preview_loader._conversation`) and drawn under the card:
> `4 messages in this conversation`, then one line each - sender, date as the Mail list writes it,
> the message's first line - oldest first, the one on show in bold. Clicking a line previews that
> message in the same pane, by the route a row selected in the list takes; the list stays, to go
> back by. A message on its own shows no list. **The list holds the newest 25**: a conversation key
> is whatever the mail said, and an archive with no threading headers falls back to the subject
> line, so one key can be thousands of unrelated messages; past 25 the heading says `More than 25
> messages in this conversation - the newest 25 are listed`. **Measured** (section 5, budget under
> 20 ms): over a synthetic store of 40,000 messages in 10,000 conversations, query and wording
> together, median of 7 warm - 0.6 to 0.9 ms for a conversation of four, 3.0 ms for one longer than
> the list (the worst case: 26 first passages read). On this machine while five other threads were
> running tests, with the list then at 50, the same test read 7 ms and 17 ms - inside the budget
> but too near it, which is why the list is 25. Tests: `tests/unit/test_mail_conversation.py`
> (19), A7 among them.

> **2026-09-30, 4d built; A9 itself is the owner's check.** `presenter.mail.original_target` decides
> what a message's original is: a message with an `entry_id` from a `.pst`/`.ost` gets a new button,
> "Open in Outlook"; a `.eml`/`.msg`/`.emlx` file is opened by the pane's existing "Open" (which on
> the Mail tab used to search inside it instead); a message inside an mbox or `.olm` gets neither,
> rather than a button that fails. **Nothing is opened by previewing** - only by the click, on a
> worker (`widgets/mail_open.py`), through a seam: the pane's `outlook_launcher` is a stand-in in
> every test and the real `osbridge.outlook.show_in_outlook` otherwise. **Outlook was not started
> to build or test this**, so the real launcher is *(UNCONFIRMED against a real Outlook)*: it uses
> `Namespace.AddStore` when the archive is not already in Outlook's list (Outlook then keeps it
> open - the tooltip says so), `GetItemFromID` and `Display`. One thing the owner's check must
> cover: **the libpff reader stores a message's number inside the archive, not Outlook's
> identifier**, so `pst_entry_id` builds the identifier from the archive's root folder identifier
> with its last four bytes replaced (the published layout of a `.pst` identifier); a libpff message
> with no number cannot be opened and says so. A failure is `ERR_OUTLOOK_OPEN` (new), with a way
> out. The quoted-text notice stays. Tests: `tests/unit/test_mail_open_original.py` (15).

- [x] **4a** **A header card**, drawn rather than typed: the sender's name large with the address
      beside it, To and Cc, the date in words (*Tuesday 2 January 2024, 09:00*), the subject as a
      heading, and attachments as chips. The plain `From: ...` block remains what Copy produces.
- [x] **4b** **The searched words highlighted** in the body, with next and previous (F3 and
      Shift+F3), exactly as the file preview does.
- [x] **4c** **The conversation.** Under the header, `4 messages in this conversation`: a short
      list (sender, date, first line) from `messages.conversation`, which is already indexed.
      Clicking one shows it in the same pane. One indexed query, on a worker.
- [x] **4d** **Open the original.** "Open in Outlook" for a message from Outlook (its `entry_id`),
      "Open" for a `.eml`/`.msg` file, so the full message - including the quoted text the index
      deliberately does not hold - is one click away. The honest quoted-text notice stays.

## 5. Budgets and measurements (non-negotiable #9)

| Thing | Budget | How it is measured |
|---|---|---|
| Code typing, names + contents (§2a) | under 40 ms warm at 100k passages | a timing test over a synthetic store, like the search tier tests |
| First history row (§3a) | shown before git finishes | a test with a fake git that prints slowly |
| Stop (§1b) | the git process ends within 0.5 s | a test with a fake git that never ends |
| Conversation strip (§4c) | under 20 ms | one query on the indexed `conversation` column |

## 6. Acceptance

| # | Given | When | Then |
|---|---|---|---|
| A1 | Windows, the window running | a history search runs | no console window appears (owner check) |
| A2 | a history search running | Esc | the list says it was stopped and git has ended |
| A3 | a repository row | "Ignore this repository" | its files leave the Code list; Undo brings them back |
| A4 | `ResetPasswordHandler` defined once and used five times | typed in Code | the definition is the first row, with its line number |
| A5 | a content row | Enter | the editor opens at that line (owner check with VS Code) |
| A6 | no `/repo`, three repositories | `CustomerId /history` | rows from all three, newest first, arriving as found |
| A7 | a message with three replies indexed | previewed | the header card, and "4 messages in this conversation" |
| A8 | a search for "trip" | a message previewed | "trip" is highlighted and F3 moves between hits |
| A9 | a message from Outlook | "Open in Outlook" | Outlook shows that message (owner check) |

## 7. Order of work (one PR each, smallest first)

1. §1a-§1d - fixes, no design question.
2. §2 - search inside the code (the developer's main question).
3. §4 - the mail preview (the everyday person's main question).
4. §3 - streaming history across repositories.

## Definition of done

- Every box ticked, or carried with a dated note saying why.
- Each PR: its tests, the suite run with the known sandbox failures compared against `main`,
  `CHANGELOG.md` under `[Unreleased]`, and a HANDOFF checklist line for anything only Windows can
  show.
- Not in scope: sending or editing mail, writing to a repository, anything over the network.
