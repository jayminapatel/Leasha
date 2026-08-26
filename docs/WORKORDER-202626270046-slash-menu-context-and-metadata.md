# Work order (One thread): context-aware `/` menu, GUI and CLI

**Doc version:** 1.1 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Search catalogue + Storage accessors + UI popup + CLI)
**Status:** ACTIVE — the owner has chosen the full option: GUI context + metadata
(sections 1–3), the `leasha shell` REPL with a live dropdown as the primary CLI
deliverable (4g), and the PowerShell tab-completer for one-shot commands
(4a–4d). Sequence within this order: **1 → 2 → 4g → 4a–4d → 3**, with 4b–4d
allowed in parallel once 2a lands. This order queues behind the open items of
`WORKORDER-202626082352-review-remediation.md` §2 (H5, H6, H11) — do not start
it while those are unticked; H5/H6 touch the same keyword/value query paths
this order builds on.

## What was asked for, and what already exists

The brief asked for three things: nested sub-commands with context-aware
completions, dynamic option loading from live data, and inline help beside each
item in the popup. **Two of the three substantially exist**, and this order
deliberately builds on them rather than beside them:

| Asked for | Already built | The gap this order closes |
|---|---|---|
| Nested sub-commands | Two tiers: `/name` → value menu (`slash_context` modes `"command"`/`"value"`) | The value tier ignores every token already typed — no third level, no conditioning |
| Dynamic data loading | `value_suggestions` reads the index (`distinct_values`), git (`lookup`), and `enabled_extensions()`, off-thread, TTL-cached | Values resolve globally, never against the query being built |
| Inline metadata | Command rows show name, expected value, one-line summary | Value rows are bare strings — no counts, no hints |

The brief's "nested map pattern" with hardcoded sub-key dictionaries is
**explicitly not the design**. This codebase's one non-negotiable for commands is
a single catalogue in `app/search/commands.py` feeding three consumers that must
never disagree — the dropdown, `app.cli commands`, and the model grammar — with
`test_command_subsets` and the wiring tests holding it. A parallel nested dict
would be a second grammar, which is the exact failure §commands.py documents.
Likewise "load from JSON files": the live sources here are the index and git,
and they are already wired. Nothing external is needed.

## 1. Context-aware value resolution (the real work)

Typing `repo:leasha /branch ` should offer leasha's branches, not every branch
in every repository. Typing `type:pdf /from ` should offer the people who sent
PDFs. Today the earlier tokens are parsed and then ignored by the menu.

- [x] **1a** (UI/presenter) `slash_context` returns the *settled tokens* as well:
  `(head, mode, partial, context)` where `context` is the already-typed filters
  parsed from `head` with the existing `parse_query` — no second parser. Callers
  that ignore the fourth element keep working.
- [x] **1b** (Storage) `distinct_values(kind, prefix=, limit=, within=)` accepts
  an optional filter context and applies it through the existing
  `file_filter_sql` — one filter grammar, as always. Bounded exactly as today;
  the `within` clause must use the same indexes search uses, and a context that
  cannot be answered bounded (e.g. free-text terms) is *dropped from the
  clause*, never allowed to widen the query cost. Measure both shapes on the
  scale fixture before accepting.
- [x] **1c** (Search) `Command` gains `scoped_by: tuple[str, ...]` — which
  earlier filters may narrow this command's values (`branch` ← `repo`;
  `from`/`to` ← `type`, `after`, `before`; `type` ← `repo`). The catalogue
  stays the single source; the model grammar and CLI help are regenerated from
  the same field so all three consumers still agree.
- [x] **1d** (UI/presenter) `value_suggestions` passes the context through to
  both readers (store `within=`, git lookup gains the repo argument it already
  implicitly wants). The popup's TTL cache key must include the context — a
  cached global answer served under `repo:leasha` is a wrong answer with a
  120-second lifetime.
- [x] **1e** Falling back is mandatory: a scoped query that returns nothing
  offers the *unscoped* values with a dimmed "(all)" marker rather than an empty
  menu — an empty menu is indistinguishable from a broken one, which is the
  failure this whole widget exists to prevent.

## 2. Value-row metadata

- [ ] **2a** (Storage) `distinct_values` returns `(value, count)` pairs (it is
  already a GROUP BY; the count is free). Callers that want strings unpack.
- [ ] **2b** (UI) the value menu shows the count dimmed beside each value —
  `pdf   12,431 files`, `dave@…   316 messages` — through the same
  `QStandardItemModel` the popup already builds; a second column role plus the
  existing delegate treatment, painted in the palette's colours like
  `command_icon` does. Counts are of the *scoped* set when a context applies,
  which is what makes 1b and 2a one query, not two.
- [ ] **2c** (UI) date commands show the resolved range as the hint —
  `/after 30d   (since 28 Jul)` — computed by the presenter, Qt-free, so it can
  be tested without a display.

## 3. Third tier, only where it earns its place

Full recursive nesting is **out of scope** — no filter here has a grammar deep
enough to justify it, and a generic tree invites the popup to become a query
builder. Two concrete cases only:

- [ ] **3a** `/type` offers the kind words first (`documents`, `mail`, `code`,
  `excel`…) and expands a chosen kind into its extensions as a second value
  page — backed by the existing `_EXT_GROUPS`, breadcrumbed in the popup header,
  Backspace returns to the kinds.
- [ ] **3b** dates: picking `/after` offers the relative spellings first
  (`RELATIVE_DATES`), and a `custom…` row that leaves `after:` in the box with
  the hint showing the accepted forms — which is what happens today, made
  explicit.

## 4. The CLI gets the same completion — not a lesser copy

This section is **in scope, not optional**. The shell is PowerShell on Windows
(the platform the product ships for), and the completer must satisfy a
constraint the GUI popup never faces: a Tab press expects an answer in tens of
milliseconds, and a cold Python start with the app's imports costs hundreds.
So the design splits static from dynamic, and pre-computes the dynamic half.

- [ ] **4a** (CLI) `leasha completions --powershell` emits a
  `Register-ArgumentCompleter -CommandName leasha` script **generated from the
  catalogue at run time** — subcommands and flags from `build_parser`, filter
  names, aliases, value hints and summaries from `COMMANDS` (summaries become
  each `CompletionResult`'s tooltip, which is the CLI's inline metadata). Never
  a hand-written list; regenerating after a catalogue change is the update
  path, and a test asserts the emitted script names every command the
  catalogue does.
- [ ] **4b** (Index) the pipeline writes a **completions sidecar** at the end of
  every run (and after prune): `<index>/completions.json` holding the top
  values per source — extensions, senders, recipients, repos, branches — with
  counts, from the same `distinct_values` call the popup uses. Bounded, a few
  KB, atomic-rename write. This is the brief's "dynamic data loading" made
  shell-fast: the completer reads a file, never starts Python.
- [ ] **4c** (CLI) the emitted completer resolves values in two steps: the
  sidecar first (instant, covers nearly every Tab press), and only when the
  sidecar has no entry for the source, a fallback call to
  `leasha suggest <source> <prefix>` — a new subcommand that lazy-imports
  nothing but `sqlite3` + the store path (the `doctor.py` discipline), applies
  the same bounded `distinct_values`, and prints one value per line. Budget:
  measured under 300ms cold on the scale fixture, or the fallback is dropped
  and the sidecar is the whole answer.
- [ ] **4d** (Install) `leasha completions install` appends the completer to the
  user's PowerShell profile, `-Remove` undoes it — the exact contract
  `add-to-path.ps1` already established, and the same no-admin-rights rule.
  `run-install.cmd` offers it as the one optional question after the index
  location.
- [ ] **4e** (CLI) `app.cli commands` prints the `scoped_by` relationships from
  1c, so the help, the popup and the completer describe the same grammar.
- [ ] **4f** (LLM) `grammar_for_model` mentions scoping so the translator stops
  proposing `branch:x` without a repo when several repositories are indexed.

Scoped completion (1b's `within=`) applies to the GUI only in 4a–4d. The
tab completer offers **unscoped** values from the sidecar — parsing the
half-typed query inside a PowerShell script block to condition the values is a
second parser in a second language, which is exactly the drift §5 forbids.
Scoped completion in a terminal is 4g's job.

- [ ] **4g** (CLI) `leasha shell` — an interactive session on **prompt_toolkit**,
  and **the primary deliverable of this section**: it is the only way a
  terminal gets a real dropdown. A shell prompt is owned by the shell — no
  completer script can draw a menu that follows the keystrokes — so the asked-
  for dropdown-in-CLI means an interactive session, and prompt_toolkit draws
  exactly that: `complete_while_typing=True`, menu opening on `/` with the
  same trigger discipline as the GUI (`slash_context` decides, so `D:/docs`
  and `12/03` stay inert in the terminal for the same tested reason they do
  in the window), arrow keys and Tab to pick, Escape to dismiss.
  The tab completer (4a–4d) remains for one-shot commands but is bounded by
  what a script block can do; the REPL is bounded by nothing that matters: a
  persistent process with the store open, so completion is an in-process call
  with no sidecar, no cold-start budget, and **scoping works** — the completer
  is a thin `Completer` subclass over the same `slash_context` /
  `value_suggestions` / `parse_query` the GUI popup uses, one grammar in one
  language. Each menu row carries the catalogue summary as its description
  column and the count from 2a dimmed beside the value — the inline metadata
  requirement, natively. History via `FileHistory` under the index folder;
  results print through the existing CLI renderers; `Ctrl+D` leaves.
  Dependency note: prompt_toolkit is pure Python with one effective transitive
  dep (wcwidth) and first-class Windows console support — Backend adds it to
  `requirements.txt` per §2 of the conventions. The REPL makes no decision of
  its own: anything it needs that the presenter does not offer is added to the
  presenter, where it is tested without a terminal — the same rule the Qt
  views live under. Guard test: the shell completer module imports nothing
  from `app/ui/widgets/`, and offers exactly the commands `COMMANDS` names.

  The full feature list for 4g, each its own tick:

  - [ ] **4g-1 dropdown**: opens as you type — on `/` for commands, stays open
    for values once a `name:` is settled, exactly the two-menu behaviour of the
    GUI popup; substring matching identical to `value_suggestions` (one
    matching rule, tested once). Arrow keys and Tab select, Escape dismisses,
    Enter with the menu closed runs the search.
  - [ ] **4g-2 rows**: value + dimmed count (from 2a) + the catalogue summary
    as the description column; command rows keep their glyph from
    `command_icon`'s catalogue field. What the GUI shows and what the REPL
    shows come from the same presenter call — a drift test compares them.
  - [ ] **4g-3 scoping**: the completer passes the settled tokens (1a's
    `context`) into `value_suggestions`, so `repo:leasha branch:<Tab>` offers
    leasha's branches in the terminal. The 1e fallback rule (never an empty
    menu when unscoped values exist) applies unchanged.
  - [ ] **4g-4 responsiveness**: completion runs through prompt_toolkit's
    `ThreadedCompleter` so a store read never blocks a keystroke — the same
    non-negotiable the GUI popup honours with its worker, kept by the same
    means: bounded `distinct_values`, TTL cache shared with the popup's
    presenter-side cache, never per-keystroke unbounded work.
  - [ ] **4g-5 toolbar**: a bottom toolbar shows the current command's
    `value_hint` while a value is being typed, the 2c resolved-date hint for
    date commands, and the index health notice (`semantic_health`) when
    degraded — the CLI's NoticeBar, one line, never a modal-style interruption.
  - [ ] **4g-6 history**: `FileHistory` stored beside the index (not the repo),
    with grey inline auto-suggest from history, right-arrow to accept. History
    is queries only; it must never record anything but what was typed.
  - [ ] **4g-7 running a search**: Enter parses with `parse_query` and runs the
    real `SearchEngine`, printing through the existing CLI renderers —
    result line, count summary, and notices exactly as `app.cli search` prints
    them today; no second renderer. `/help` prints `help_lines`; `/quit` and
    Ctrl+D leave; Ctrl+C clears the line, never kills the session.
  - [ ] **4g-8 tests**: completer parity with `COMMANDS` (names, aliases,
    subsets); the `slash_context` inertness cases (`D:/docs`, `12/03`,
    `http://`) re-asserted through the REPL completer; a no-Qt-import guard on
    every `app/cli*` shell module; scoped-completion and empty-fallback cases
    mirrored from the GUI popup's tests.

## 5. Rules that bound this work (from the codebase's own record)

* **No unbounded work behind a keystroke.** Every new query shape in 1b gets a
  measured bound on the scale fixture, and the popup keeps its worker + TTL +
  generation-stamp discipline. A scoped `distinct_values` that cannot be
  bounded is not offered — dropped scope, not dropped frames.
* **Never rewrite what was typed.** Context parsing reads `head`; it must not
  normalise, reorder, or touch it. `12/03`, `D:/docs`, `http://` stay inert
  exactly as `slash_context` already guarantees — extend its tests, keep them.
* **Qt-free decisions.** Everything in 1a/1c/1d/2c is presenter/search-layer
  logic, testable without a display. The widget arranges; it does not decide.
* **One grammar.** Any moment this order tempts a second catalogue, a nested
  dict in the widget, or a hand-maintained completion list — stop; that is the
  failure `commands.py`'s docstring was written about.

## Done means

Each ticked item: the change, tests beside the existing ones
(`test_command_subsets`, the `slash_context` cases, popup TTL tests), the scale
fixture measurement for 1b recorded in this file, `pytest tests -q` green,
committed by name. Sections 1 and 2 are the product; section 4 can proceed in
parallel (it depends only on the catalogue and `distinct_values`, both of which
exist today); section 3 does not start until 1d is ticked.

---

## 1a and 1b delivered, 2026-08-27

`tests/unit/test_slash_context.py`, 17 tests.

**1a** `slash_context` returns a `SlashContext` NamedTuple - `(head, mode,
partial, context)` - with `context` the already-typed filters parsed by
`parse_query`. Parsed **only in value mode**: the command tier has no use for
it and this runs on every keystroke. Never raises, because a half-typed query
is the normal state of a search box and a dropdown that throws mid-sentence is
worse than one offering unscoped values.

A NamedTuple rather than a plain 4-tuple, and unpacking three now fails loudly
rather than silently dropping the context - a menu that looks right and answers
the wrong question is the failure this section exists to remove. The two call
sites in `command_popup.py` are updated.

**1b** `distinct_values(kind, prefix=, limit=, within=)`. The context is applied
through `file_filter_sql` - the one filter grammar - and free text never
reaches it, so a half-typed sentence cannot turn an indexed lookup into a scan.
That is the order's rule, satisfied by which function is called rather than by
a check afterwards. The four queries became a `_ValueShape` table because each
now has to take an appended clause, and four near-identical statements drifting
apart is a fault this project has already had.

**The scoped shape is sampled, and the measurement is why.** On 500,000 files,
`folder` scoped by `type:pdf`:

| shape | time | result |
|---|---|---|
| group the whole filtered corpus | **437ms** | 25 folders |
| count the first `VALUE_SAMPLE` (20,000) | **11.1ms** | the same 25 folders, same order |

437ms behind a keystroke is precisely the "widening the query cost" this item
forbids, so it does not get to. Grouping a filtered corpus cannot use an index
for both the filter and the grouping; sampling restores the bound. The menu is
ordered by frequency and capped at forty, so what it needs is the values and
their order, and the sample gives both exactly - only the absolute counts are
proportional, which matters for **2a** and is stated where they are produced.

A value too rare to appear in the first 20,000 matching files is by definition
not among the commonest, and typing one more character narrows the candidates -
so the sample closes on the exact answer as somebody types towards it.

Unscoped lookups are untouched and their counts stay exact.

## 1c, 1d and 1e delivered, 2026-08-27 — §1 complete

**1c** `Command.scoped_by`. `type` <- `repo`; `from` and `to` <- `type`,
`after`, `before`; and in `gitquery`, every command with a value source -
`branch`, `tag`, `author`, `committer`, `commit`, `message` - <- `repo`,
because all of them are facts about one checkout. In the catalogue, so the
dropdown, the CLI and the model grammar cannot disagree.

`scope_for` narrows by blanking the fields `scoped_by` does not name.
`_FILTER_FIELDS` holds each field's *neutral* value and mirrors
`ParsedQuery.has_filters` - a test asserts that, because the two answer the
same question from opposite ends. The first version blanked `scope` to `""`
when its neutral value is `"all"`, so narrowing `/type` by a `path:` it does
not permit produced an empty query that still claimed to have filters.

**1d** the context reaches both readers - `within=` for the store, `repo=` for
the git lookup where it accepts one - and `scope_key` puts the scope in the
popup's TTL cache key. Keyed on the command name alone, the global answer for
`/from` would be served under `repo:leasha from:` for two minutes: a wrong
answer with a lifetime, which is worse than a slow one because nothing about it
looks wrong. The key is built from the *narrowed* scope, so typing more free
text after a filter does not throw the cache away for nothing.

**1e** a scoped lookup that returns nothing offers the unscoped values and says
so through `notes` (`ALL_VALUES_NOTE`), which the widget renders dimmed. The
first version gated on the whole merged list and could never fire: the
grammar's kind words - `excel`, `word` - are facts about the language that no
scope narrows, so `/type` under a repository with nothing in it still had a
full-looking menu. It gates on what the *index* returned under the scope.

**One fragility found while testing.** `value_suggestions` catches everything a
reader raises, so sending `within=` to a store that has never heard of it turns
a `TypeError` into an empty index tier - the menu silently falls back to kind
words and looks fine. Four existing tests caught it by accident. `within` is
now passed only when there is a scope, and a test covers the old signature on
purpose.
