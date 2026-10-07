# Leasha — session briefing

**Doc version:** 1.3 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5

**Leasha** is a Windows desktop app that searches ~100GB of local files and Outlook
mail from a plain-English description. One process, fully offline, single user.
Benchmark: an eight-year-old finds her homework.

This file is the front door. It points; it does not restate. Where two documents
disagree, the one named below wins.

## Read before working

1. `HANDOFF.md` — state, decisions, traps. **Where things are.**
2. `docs/PROJECT_INSTRUCTIONS.md` — the 12 non-negotiables and how a layer gets built.
   **How to work.**
3. `docs/ORDER_REGISTER.md` — every work order, its status and queue position.
   **What to do next.**
4. The active order itself, then `BUILD_SPEC_V2.md` for the layer you are touching.

`CLAUDE.md` carries this same briefing plus *Where a session runs*: the working copy is a
clone of GitHub `main`, code travels by git only, and release builds live outside the
repository. For a fast picture of the whole application, open `docs/USER_GUIDE.html` and
`docs/TECHNICAL_REFERENCE.html`: they describe; the documents named here win.

## Who owns what

| Question | Authority |
|---|---|
| What a term means | `docs/GLOSSARY.md` |
| What state the project is in | `HANDOFF.md` |
| What the rules are | `docs/PROJECT_INSTRUCTIONS.md` |
| Version scheme, release checklist, git conventions | `docs/VERSIONING.md` |
| Architecture, error contract, honest performance numbers | `LOCAL_KNOWLEDGE_GRAPH_V2.md` |
| Per-stage performance budget | `BUILD_SPEC_V2.md` |
| What is queued, and what shipped | `docs/ORDER_REGISTER.md` |
| Ideas approved but not ordered | `docs/PARKED-IDEAS.md` |

## Standing rules

**Verify, never guess.** Run the command, read the function, check the second source
before asserting anything. Three wrong diagnoses in one session are why. When
something cannot be checked from here — `D:\Leasha\Data`, the Windows venv, Qt
behaviour — say so and ask, rather than reasoning past the gap.

**Never create a work order unprompted.** Answer design questions in conversation.
Offer in one line and wait.

**Never reword a released work-order item, a UI label or an existing description.**
Corrections go in a dated note above the item, never as an edit to it.

**Documentation says what is true now (owner, 2026-10-06).** README, the user guide, the
technical reference, troubleshooting, the glossary, the architecture and build specs, the
developer guides and the checklists are rewritten in place to describe Leasha as it is at
the moment - no dated notes stacked on top. The rule above still holds for *records* - work
orders, `ORDER_REGISTER.md`, `CHANGELOG.md`, `HANDOFF.md` entries, reviews - and for UI labels.

**Working version first.** No structural refactor before the feature orders are done.
Bug fixes and measured performance work are exempt.

**Every `.md` in this tree** must open with an H1 and carry the header format in
`docs/VERSIONING.md`, or `tests/unit/test_docs_versioned.py` goes red. There is no
exemption list.

**Task list before starting.** One task per item, so progress is watchable live.

**Fix what you find (owner, 2026-09-30).** A bug or fault met while working is fixed
in the same session, with a test, and reported at close-out - not listed back as a
question. Ask only when the owner's input is genuinely needed: their data, their
accounts, anything destructive, or a product decision only they can make. New work
orders still wait for the owner (above).

## Close-out

Finish a working session by saying what changed, what is now untrue in `HANDOFF.md`
or `docs/ORDER_REGISTER.md`, and what the next thread needs to know. A state document
that has quietly gone stale is worse than none, because somebody acts on it.
