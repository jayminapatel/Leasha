# Work order (One thread): date ranges in every box, a live index log, and runs that say they were interrupted

**Doc version:** 1.0 · **Updated:** 2026-09-27 · **Applies to:** app v0.3.3
**Thread:** One thread (query parser + the four search boxes; pipeline activity
events + an Indexing-page log; run records + PST resume)
**Status:** RELEASED by the owner 2026-09-27, with the instruction to build it in the
same session that wrote it.

**Where this came from.** The owner's structured feedback of 2026-09-27 listed seven
problems. Three were bugs with a verified cause and were fixed directly in the same
change, not ordered here: the page-switch freeze while indexing (a UI-thread state
write waiting on the indexer's write lock), date filters that never looked at an
email's sent date, and progress-bar phases that sent no progress at all. The other
three are new behaviour, and they are this order. The seventh, general jerkiness,
has no measured cause yet and is not ordered. It needs the owner's lag-monitor numbers
from a real run (`HANDOFF.md` §3, "shutdown: window responsiveness this session").

What was checked before writing this, so nobody re-derives it:

- **Dates today.** `after:`/`since:`, `before:`/`until:` and `/after`, `/before`
  exist (`app/search/commands.py`, `app/search/query.py`). They take whole days only.
  A time of day (`after:2017-03-01T10:00`) is an unknown operator. `date:` and `A..B`
  ranges do not exist. There is no date control anywhere in the UI. The only range
  boxes are the Timeline picker's `range_from`/`range_to`. Search, Files, Mail and
  Code each have their own box and command set, and so does the mini-search popup.
- **The Indexing page has no log.** Its only list is the run's notices, as plain
  strings with no time (`IndexStats.notices`). The Settings → Debug pane already
  shows `HH:mm:ss` lines, but it is the application log, not the run's story.
- **Interrupted runs.** Resume is per file: unfinished files stay `PENDING`, and
  nothing is corrupted. **A PST has no resume inside itself.** An interrupted archive
  is re-read from its first message next run. Messages already indexed are skipped by
  text hash, so only the parse is repeated. `email_pst.walk_session`'s folder-level
  `on_folder` checkpoint exists, but nothing passes it. The UI says nothing about a
  previous run that did not finish.

## 1. Date ranges, everywhere there is a search box

- [ ] **1a** `date:` operator: `date:2017`, `date:2017-03`, `date:2017-03-14`, and
      ranges `date:2017-01-01..2017-06-30` (either end may be open: `date:..2017`,
      `date:2017..`). It sets the same `after`/`before` the existing operators do, so
      filtering, the sent-date rule for mail and the CLI all follow for free.
- [ ] **1b** times of day on `after:`, `before:` and `date:`: `2017-03-01T10:00`,
      and a quoted `"2017-03-01 10:00"`. The whole-day behaviour of a date-only value
      is unchanged.
- [ ] **1c** a `/date` slash command that takes the same values, with an entry and
      example in every box's command popup where a date means something: Search,
      Files, Mail and the mini-search. Code gets it only if its rows carry a date;
      otherwise record why not in this item.
- [ ] **1d** a mistyped date says what was wrong and what would work (non-negotiable
      #2), instead of silently becoming a search term.
- [ ] **1e** tests: parser cases for every form above, one per box proving the
      command is offered and applied, and the CLI (`app.cli search "date:2017"`).

## 2. A live log on the Indexing page

- [ ] **2a** the pipeline records what it is doing as timestamped activity entries:
      phase changes, each large file it starts (an archive, a long media file), a
      pause and its reason, warnings, and the run's notices. They sit in a bounded
      buffer on the run, not the application log, and cost nothing when nobody reads
      them.
- [ ] **2b** the Indexing page shows them in a small scrolling log with an
      `HH:MM:SS` time on every line. The newest line is at the bottom, and it scrolls
      with the run unless the owner has scrolled up to read. Lines are capped. The
      widget lives in `app/ui/widgets/` and its formatting in the presenter:
      `indexing_view.py` is already over its line guard and must not grow.
- [ ] **2c** the run's existing notices carry the time they happened, both in the
      log and wherever they are shown today.
- [ ] **2d** `app.cli index` prints the same entries with the same timestamps
      (non-negotiable #8).
- [ ] **2e** tests: entries are emitted in order for a small real run, the buffer
      is bounded, the widget keeps the reader's scroll position, and repaints stay
      within the existing 0.25 s paint throttle.

## 3. Interrupted runs, and archives read part-way

- [ ] **3a** a run that ended without finishing (crash, power cut, the app killed)
      is recognised the next time the Indexing page opens. The page says so in plain
      words: when it stopped, how many files it had not reached, and that starting
      again carries on from there with nothing lost.
- [ ] **3b** a PST resumes inside itself at folder granularity: the folder cursor
      is persisted before it is needed (non-negotiable #4) through the existing
      `resume:` mechanism, and the next run starts at the first unfinished folder.
      Build it for the libpff backend first. It can be tested here against a real
      archive fixture. The Outlook backend follows only if its folder walk can be
      proven equivalent; if not, record why here.
- [ ] **3c** an archive read part-way says so on the Indexing page: which one, and
      that it will carry on next run. This is kept distinct from "Mail archives partly
      read", which means damage, not interruption.
- [ ] **3d** tests: an index stopped mid-archive and then resumed ends with exactly
      the message set of an uninterrupted run, with no duplicates and no gaps. A killed
      run is detected as interrupted, and a clean stop is not.

## 4. Done means

Change + tests + the targeted tests green against a recorded baseline + committed by
name, then CHANGELOG. Acceptance sentence: type `date:2017-03..2017-06` in any box and
see only what is from those months, mail included. Watch the Indexing page and read,
line by line and with times, what it is doing right now. Pull the plug mid-archive,
and on the next launch be told plainly what happened and that carrying on loses
nothing.
