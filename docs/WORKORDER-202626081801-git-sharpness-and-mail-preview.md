# Work order: git search sharpness, and the mail preview

**Doc version:** 1.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3
**Created:** 2026-08-26 18:01 · **Layer:** L4/L5 - `app/search/gitquery.py`, `app/search/gitsearch.py`, `app/ui/widgets/git_tree.py`, `app/ui/preview_loader.py`

**Thread:** the single merged thread

**Raised by the owner**, three things in two sentences:

> *"the code does not search ability to search through the search commit messages. also when
> search criteria is typed in git view it should only show gits that have documents which match
> not all gits. This search has to be sharp and the switches should search the appropriate area
> of git, the files, all searches should search contents which it does"*

> *"also the mails dont preview properly"*

Each was checked against the code before being written down. §1 and §3 are missing
capabilities; §2 is a defect in what a pane shows.

---

## 1. Commit messages cannot be searched at all

The git side runs four kinds of command (`gitquery.py:66-75`):

| Kind | Command | Searches |
|---|---|---|
| `KIND_GREP` | `git grep` | file **contents** at a revision |
| `KIND_LOG` | `git log -S` | which **commits changed** the pattern |
| `KIND_PATCH` | `git log -S` with the diff | the same, with matching lines |
| `KIND_NAME_STATUS` | `git log --name-status` | a file's life |

**`git log --grep` appears nowhere.** `-S` searches the *content* of diffs; `--grep` searches
the commit *message*, and they answer different questions. The only place a message is read at
all is the suggestion menu - `"commit": ["git", "log", "-n", "50", "--pretty=format:%h  %s"]`
(`gitsearch.py:606`) - which offers recent commits to pick from and searches nothing.

So *"which commit said it was fixing the licence bug"* has no answer, and the owner is right
that it should. It is also `GitSearch.txt` UC-020, *"search commit metadata, authors and
messages"* - the only part of that use case built is `/author`.

### What to build

A `/message` switch (alias `/msg`), mapping to `git log --grep=<pattern>` with the same
case and regex handling the other kinds already honour, and composing with `/author`,
`/since`, `/before`, `/branch` and `/rev` exactly as they compose today.

**It is cheap, and cheaper than the ones beside it.** `--grep` reads commit headers only, not
diffs, so it costs a fraction of `-S` - the switch that already sets the expectation for what a
history search costs. It belongs on the fast side of that line and should say so.

**Results are commits, not files.** A message match has no file and no line, so the row is a
commit: hash, author, date, subject. The `git_result_row` shape already handles a row with no
working-tree file - it has to, because a `-S` hit can name a file deleted years ago.

### Built — 2026-08-26

`/message` (aliases `/msg`, `/subject`) in `gitquery.py`. `GitQuery.message`, added to
`GIT_ONLY` so the router sends it to git, and to `GIT_COMMANDS` so the `/` dropdown offers it
without further work.

* `wants_history()` returns True for it - a commit message is a property of a commit, and
  `git grep` has nowhere to put it. Same reason `/author` is there.
* **`is_message_only()` marks it not slow.** `--grep` reads the header; `-S` diffs every commit
  it walks. Billing a header scan as slow would put a confirmation in front of a search that
  does not need one. A query carrying both a message *and* a pattern is slow again, correctly.
* Both together AND in git - `/message licence CustomerId` is "said licence *and* touched
  CustomerId", which is the useful reading.
* `_explain` names the message before the author and the dates, because it is the strongest
  narrowing in the line and because "no matches" means nothing until you know whether it read
  messages or diffs.

7 tests in `test_gitquery.py`; 46/46 in that file pass, zero regressions across the suite, zero
lint delta. A2 and A3 are covered. **A1 and A4 are not** - they need a real repository and the
Code tab, so they are Windows work.

## 2. The git tree lists every repository, whatever was typed

`git_tree.show_repos()` (`widgets/git_tree.py:94`) is given the full list and draws it. Nothing
narrows it by the query. Type a term that matches files in one repository and the other three
sit there as though they matched too.

That is the same fault as `code_type_filter` and the empty-list states in
`WORKORDER-202626081149-code-tab.md` §4 and §5: a pane showing something the query did not ask
for, with nothing saying why. And it is more misleading here, because a tree is read as *"these
are the repositories that have what you asked for"*.

### What to build

When the box has criteria, the tree shows **only repositories with at least one matching file**,
and says so - `2 of 4 repositories match`. Empty criteria shows all of them, as now.

Two constraints, both already established elsewhere in this application:

* **The count must come from the same query the list ran.** A second, differently-filtered
  count is how the tree and the list come to disagree, which is §5's whole subject.
* **It must not cost a subprocess.** Which repositories have matching *indexed* files is one
  SQL aggregate over `files.repo_id`. A branch scope answers from the listing already fetched
  for the selection (`code_rows_for`'s `cached`), never a fresh `git ls-tree` per keystroke.

**A repository with no match is hidden, not greyed.** A tree of four with three inert rows is
the noise being removed.

## 3. Mail previews show reassembled index text, not the message

`MailView` installs `stored_text` as its `body_provider` (`mail_view.py:158`), and that
function reads `chunks_for_file` and returns `"\n\n".join(chunk.text for chunk in chunks)`
(`preview_loader.py:319`).

So the preview is **the indexed text, reassembled**, and three things have happened to it
before it gets there:

1. **Quoted replies and signatures are stripped at index time** by `strip_quoted`
   (`extract/quoting.py:126`). The overnight log shows this working hard - *"stripped 38,609
   chars of quoted"* on a single message. Correct for indexing; it means the preview of a reply
   shows only the new text, with the thread it is replying to gone.
2. **Headers are not shown.** From, To, Sent and Subject are columns in the table, but the
   preview body has no header block, so a message read on its own has no context.
3. **Chunk boundaries become blank lines.** The join is cosmetic, not structural, and long
   messages read as arbitrarily broken paragraphs.

Reading from `chunks` is the right *source* and the docstring explains why: there is nothing on
disk that is *this* message - the PST holds a hundred thousand of them, and the text was
extracted once. That does not change.

### What to build

* **A header block above the body**: From, To, Sent, Subject, and whether anything was
  attached. The `MailRow` already carries all of it, so this is presentation, not a new query.
* **Say what was removed.** Where `strip_quoted` took something out, one line - *"Quoted reply
  and signature removed - 38,609 characters"* - in the preview's notice line, which already
  exists for the "text extracted from the document" case. The same honesty, applied to mail.
* **Join chunks without inventing paragraph breaks.** Chunking is an indexing decision and
  should not be visible.

**Consider keeping the quoted text.** Not indexing it is right - a thread quoted twenty times
would be indexed twenty times. Not being able to *read* it is a different decision, and it was
never taken deliberately: it fell out of reusing the indexed text for display. If the raw
message body is affordable to store for mail specifically, the preview should show what
arrived. That is a size question and wants a measurement, not an argument.

## 4. What is already right, and stays

The owner's *"all searches should search contents which it does"* is correct and worth
recording so it is not disturbed: `KIND_GREP` searches file contents at a revision, `-S`
searches the content of every diff it walks, and the index path searches chunk text through
FTS5. Content search is the part that works.

## 5. Acceptance

| | Criterion |
|---|---|
| A1 | `/message licence` finds the commit whose message contains it and does not require the word to appear in any file |
| A2 | `/message` composes with `/author`, `/since` and `/branch` in one command |
| A3 | A message search costs measurably less than the same search with `/history`, and the summary says which one ran |
| A4 | A commit-message hit renders as a commit row - hash, author, date, subject - with no file path invented for it |
| A5 | With criteria typed, the git tree lists only repositories holding a match, and states `N of M` |
| A6 | Clearing the criteria restores every repository |
| A7 | A5 costs no subprocess: assert no `git` invocation on a keystroke |
| A8 | A mail preview shows From, To, Sent and Subject above the body |
| A9 | Where quoted text was stripped, the preview says so, with the amount |
| A10 | A message whose text spans several chunks reads as continuous prose |

## 6. Note on sequencing

§2 overlaps `WORKORDER-202626081149-code-tab.md` §8, the parked two-view design - both are about
the tree and the list agreeing. §2 here is a defect and can be fixed on its own; it does not
need the parked scenarios, and doing it first will make those scenarios easier to write because
the tree will then behave as the owner expects while they are written.
