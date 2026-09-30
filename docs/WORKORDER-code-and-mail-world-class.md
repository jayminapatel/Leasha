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

- [x] **3a** **Streaming.** Rows appear as git prints them (`Popen`, read line by line on a
      worker), newest first, with a live line: `Searching history… 12 found so far`.
- [x] **3b** **Every repository at once** when none is named: one git process per repository, at
      most two at a time, results merged by date. Today it answers "Name a repository first".
- [x] **3c** **A commit opens as a commit.** Selecting a history row shows, in the preview pane:
      the message, author and date, the files changed, and the diff with the searched text
      highlighted (`git show`, on a worker, only when selected).

## 4. Mail: a preview people recognise

- [ ] **4a** **A header card**, drawn rather than typed: the sender's name large with the address
      beside it, To and Cc, the date in words (*Tuesday 2 January 2024, 09:00*), the subject as a
      heading, and attachments as chips. The plain `From: ...` block remains what Copy produces.
- [ ] **4b** **The searched words highlighted** in the body, with next and previous (F3 and
      Shift+F3), exactly as the file preview does.
- [ ] **4c** **The conversation.** Under the header, `4 messages in this conversation`: a short
      list (sender, date, first line) from `messages.conversation`, which is already indexed.
      Clicking one shows it in the same pane. One indexed query, on a worker.
- [ ] **4d** **Open the original.** "Open in Outlook" for a message from Outlook (its `entry_id`),
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
