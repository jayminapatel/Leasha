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
- [ ] **2c** **Open at the line.** Enter (or double-click) opens the file in the person's editor at
      that line when one is found - VS Code (`code -g path:line`), then Notepad++ (`-n`), then
      Sublime (`path:line`) - otherwise the default program. A new "Editor for code" setting
      (Automatic, or a program chosen by the person) under Settings, per non-negotiable #11.
- [x] **2d** The summary keeps its current shape and adds the split:
      `3 definitions · 12 files · 48 mentions`.

## 3. Code: history that answers as it goes

- [ ] **3a** **Streaming.** Rows appear as git prints them (`Popen`, read line by line on a
      worker), newest first, with a live line: `Searching history… 12 found so far`.
- [ ] **3b** **Every repository at once** when none is named: one git process per repository, at
      most two at a time, results merged by date. Today it answers "Name a repository first".
- [ ] **3c** **A commit opens as a commit.** Selecting a history row shows, in the preview pane:
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

- [x] **4a** **A header card**, drawn rather than typed: the sender's name large with the address
      beside it, To and Cc, the date in words (*Tuesday 2 January 2024, 09:00*), the subject as a
      heading, and attachments as chips. The plain `From: ...` block remains what Copy produces.
- [x] **4b** **The searched words highlighted** in the body, with next and previous (F3 and
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
