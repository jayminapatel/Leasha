# Work order (One thread): privacy defaults — per-account index, empty roots, the honest paragraph

**Doc version:** 1.0 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
**Thread:** One thread (Install + Core config + docs)
**Status:** RELEASED by the owner 2026-08-27 (registered in HANDOFF.md §"What
is Next"). Small and self-contained — it touches the installer, first-run
defaults and docs, not the pipeline or search, so it may be scheduled into
any gap between the larger orders at the thread's discretion.

## The decisions (owner, 2026-08-27 — these are settled, do not relitigate)

1. **Default index location is `%LOCALAPPDATA%\Leasha`**, choosable at
   install exactly as today. Per-account by default means separate Windows
   accounts get separate, ACL-protected indexes with zero extra machinery —
   Windows' own permissions on `%LOCALAPPDATA%` do the separating.
2. **Roots start empty.** First run suggests the user's own profile folders
   (Documents, Desktop, Downloads, Pictures) as one-click additions and
   never adds anything unasked. No default ever reaches another user's
   profile or a whole drive; those remain deliberate choices.
3. **Shared-login stance is the honest sentence, not a mechanism.** One
   login = one user as far as Leasha can know; the paragraph below says so
   plainly and that is the whole answer.
4. **The owner's install is grandfathered, untouched.** `D:\Leasha\Data`
   and his current roots stay exactly as they are — these defaults apply to
   *new* installs only. Nothing migrates, nothing prompts, nothing warns on
   an existing `.env` with a valid `DATA_PATH`.

## 1. Install and first run

- [x] **1a** `install.ps1` / `run-install.cmd`: the index-location question's
  default becomes `%LOCALAPPDATA%\Leasha` (expanded, shown as a real path).
  The prompt keeps its current wording apart from the default; an existing
  `.env` with a `DATA_PATH` is respected without asking (decision 4 — this
  also partially answers the per-user-vs-per-machine [FINALISE] question in
  `WORKORDER-202626082213-install-and-distribution.md` §7; note it there as
  an appended dated note, never an edit).
- [x] **1b** first run with zero roots: the roots panel offers the profile-
  folder suggestions as individual one-click adds with a "add all four"
  affordance, plain-labelled with full paths. Declining leaves roots empty
  and the app says, in plain words, that nothing is indexed until a folder
  is chosen. Existing installs (roots already present) never see this.
- [x] **1c** no root that resolves inside another user's profile
  (`C:\Users\<other>`) is *suggested* — ever. Typing or browsing to one
  remains allowed (the machine is the user's), but it is a choice, not a
  default.

## 2. The paragraph

- [x] **2a** this text (owner-approved wording; edit only with the owner) is
  added to the README and shown by the installer on the index-location
  step:

  > **Leasha and shared computers.** Everything Leasha indexes and
  > everything you search stays on this computer — nothing is ever sent
  > anywhere. On a computer with separate Windows accounts, each account
  > gets its own private index: you find your files, others find theirs,
  > and Windows keeps them apart. On a computer where people share one
  > login, Leasha works like the rest of that login — anyone using it can
  > find anything it can read. If that matters in your home, give each
  > person their own Windows account before installing, or choose the
  > folders Leasha indexes so shared spaces stay shared and private ones
  > stay out.

- [x] **2b** `doctor.py` reports the index location and whether it sits in a
  per-account path or a shared one — one line, factual, no judgement
  ("Index: C:\Users\jaymin\AppData\Local\Leasha (private to this
  account)" / "Index: D:\Leasha\Data (shared location)").

## 3. Tests

- [x] fresh install → `.env` gains the `%LOCALAPPDATA%` default, expanded
  correctly for the running account.
- [x] existing `.env` with `DATA_PATH` set → installer leaves it alone and
  asks nothing (the grandfather rule as a regression test — the owner's
  machine is the fixture this protects).
- [x] first-run suggestions: appear with zero roots, never with any root
  present; declining indexes nothing; no suggestion points into another
  profile.
- [x] the installer's parse-check-first discipline (`run-install.cmd`)
  still passes with the new prompt text.

## Done means

Change + tests + `pytest tests -q` green + committed by name; the paragraph
verbatim in README and installer; a dated note (not an edit) on the install
order's [FINALISE] list; CHANGELOG under `[Unreleased]`. When ticked, the
household-privacy gate in `docs/CHECKLIST-A-PLUS.md` is marked closed with a
pointer here.

---

## Delivered 2026-08-27 — all 9 boxes

`tests/unit/test_privacy_defaults.py`, 44 tests.

**1a** `install.ps1` reads `.env` for a `DATA_PATH` *before* it offers
anything, and if one is there it says so and asks nothing - the grandfather
rule, with the owner's own machine as the fixture it protects. The default when
there is no existing install is `Join-Path $env:LOCALAPPDATA "Leasha"`.

**1b/1c** the roots list already started empty; what was missing was the offer.
`RootsBox` now shows one button per profile folder that exists plus "add all
four", and hides the whole offer the moment a root is present, so an existing
install never sees it. The list is computed by `presenter.suggested_roots`,
which will not return anything outside this account's profile - the rule lives
there rather than in the view, because a view deciding it for itself is how a
default ends up in somebody else's folder. `owns_path` is separator- and
case-insensitive, and `C:\Users\jaymin-two` does not count as inside
`C:\Users\jaymin`, which is the case a `startswith` gets wrong.

**2a** the paragraph is in README.md and in the installer, shown on the
index-location step, and a test holds the two to each other sentence by
sentence. A promise about privacy that says two different things in two places
is worse than one that says nothing.

**2b** `doctor` reports `Index: <path> (private to this account)` or
`(shared location)`. It always passes: a shared location is a legitimate
choice - the owner's own is `D:\Leasha\Data` and decision 4 says it stays -
so `doctor` failing over it would be `doctor` having an opinion about somebody
else's machine.

The install order's [FINALISE] list has a dated note pointing here, appended
rather than edited.
