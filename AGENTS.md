# Leasha — session briefing

**Doc version:** 1.1 · **Updated:** 2026-10-01 · **Applies to:** app v0.3.3

**Leasha** is a Windows desktop app that searches ~100GB of local files and Outlook
mail from a plain-English description. One process, fully offline, single user.
Benchmark: an eight-year-old finds her homework.

This file is the front door. It points; it does not restate. Where two documents
disagree, the one named below wins.

## Read before working

> *Note, 1 October 2026:* `CLAUDE.md` carries this briefing plus *Where a session runs* (the
> working copy is a clone of GitHub `main`; code travels by git only; release builds live outside the
> repository) and the close-out rule. For a fast picture of the whole application, open
> `docs/USER_GUIDE.html` and `docs/TECHNICAL_REFERENCE.html`: they describe; the documents named
> below still win.

1. `HANDOFF.md` — state, decisions, traps. **Where things are.**
2. `docs/PROJECT_INSTRUCTIONS.md` — the 12 non-negotiables and how a layer gets built.
   **How to work.**
3. `docs/ORDER_REGISTER.md` — every work order, its status and queue position.
   **What to do next.**
4. The active order itself, then `BUILD_SPEC_V2.md` for the layer you are touching.

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

**Working version first.** No structural refactor before the feature orders are done.
Bug fixes and measured performance work are exempt.

**Every `.md` in this tree** must open with an H1 and carry the header format in
`docs/VERSIONING.md`, or `tests/unit/test_docs_versioned.py` goes red. There is no
exemption list.

**Task list before starting.** One task per item, so progress is watchable live.

## Close-out

Finish a working session by saying what changed, what is now untrue in `HANDOFF.md`
or `docs/ORDER_REGISTER.md`, and what the next thread needs to know. A state document
that has quietly gone stale is worse than none, because somebody acts on it.
