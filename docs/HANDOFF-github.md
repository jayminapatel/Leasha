# Handoff: putting this repository on GitHub

**Doc version:** 1.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3

The project is local-only: 144 commits, no remote. This is what it takes to change that, what
was checked before recommending it, and the one thing nobody can automate.

`scripts\setup-github.ps1` does the automatable part. **It reports by default and changes
nothing without `-Apply`**, which is the same rule the rest of this project applies to anything
destructive.

---

## 1. Why now, and not later

A `git reset` on 2026-08-26 destroyed a day's uncommitted work across eleven files. There was
no remote to recover from, and the only reason anything survived is that a copy happened to
exist outside the repository.

That is the argument. Not tidiness, not collaboration - **the absence of a second copy**. A
`git push` at the end of a session makes that class of loss much harder to arrive at, and it
costs seconds.

## 2. What was checked before recommending it

Every one of these was verified on 2026-08-26 rather than assumed.

| Question | Answer |
|---|---|
| Is `.env` tracked? | **No** - `.gitignore:16`. It names `D:\SearchData` and `D:\Leasha\Data` |
| Does `.env` hold credentials? | **No** - 24 keys, all paths, model names and tuning numbers. No token, password or key |
| Are `venv`, `logs`, `Backup`, `Leasha` ignored? | **Yes**, all four |
| How big is the history? | **2.68 MiB**, 144 commits. Seconds to push |
| Is `big.pst` real email? | **No** - 5,242,880 bytes of the single letter `x`. A fixture, referenced by nothing |
| Does `big.pst` bloat the history? | **No** - 5MB of one repeated byte compresses to **5,116 bytes**. Removing it is tidiness, not a size fix, and **needs no history rewrite** |
| Is the `D:\KnowledgeGraphData` folder tracked? | **No** - untracked litter, safe to delete |

**So there is nothing to sanitise and no `filter-repo` to run.** That is unusual and worth
saying plainly, because the normal advice for "put an existing project on GitHub" assumes
otherwise and costs an afternoon.

## 3. What the script does

```powershell
.\scripts\setup-github.ps1                 # report only
.\scripts\setup-github.ps1 -Apply          # do it, private repository
.\scripts\setup-github.ps1 -Apply -Public  # do it, public
```

1. **Re-checks the secrets question every run.** `.env` untracked, and a grep for credential
   words across tracked non-documentation files. The check that matters most is the cheapest to
   get wrong, so it is asserted rather than remembered.
2. **Removes three strays**: `big.pst`, `docs\feature_.md` (which fails three tests in
   `test_docs_versioned`), and `forget-repo.py` (superseded by `repos --forget`). Plus the
   literal `D:\KnowledgeGraphData` folder sitting inside the project.
3. **Creates `.gitattributes`.** There is none, and this project has a non-negotiable about
   `.ps1` files being ASCII with a BOM. A collaborator with a different `core.autocrlf` rewrites
   every file on clone, and that rule is encoding-sensitive - it has already killed the
   installer once, silently, with no log.
4. **Refuses to push a dirty tree.** It reports what is uncommitted and stops. Pushing half a
   change is worse than not pushing.
5. **Runs `git gc`** if loose temp objects are present. There are some today, left by the
   interrupted git operations of 2026-08-26.
6. **Creates the repository and pushes**, via `gh` when it is installed. Without `gh` it stops
   before the push and prints the two commands to run by hand, rather than guessing at a URL.

## 4. The part nobody can automate

**Authentication, and the decision.**

`gh auth login` opens a browser and needs a human. So does deciding whether this is public.

Recommendation: **private, at least at first.** Nothing in the repository is secret, but
`README.md`, `HANDOFF.md` and `.env.example` together are a fairly complete description of one
person's machine and their document archive. Public is a decision to take deliberately, once,
not a default to discover afterwards.

If `gh` is not installed: `winget install GitHub.cli`.

## 5. The branch question

The current branch is `layer/2-extraction` with 144 commits. `main` exists locally and is
behind. GitHub makes the first branch pushed the default.

Decide before pushing, not after. Either push `layer/2-extraction` and rename on GitHub later,
or `git branch -M main` first - the script takes `-Branch main` for that.

## 6. Keeping them in step

Git does not sync. It pushes when told, and that is the point - but with **two sessions writing
to this tree at once**, as happened all through 2026-08-26, the usual single-writer habits are
not enough.

| When | Do |
|---|---|
| Starting work | `git fetch; git status` - and look at it |
| Before anything that touches the whole tree | `git add -A; git commit -m "wip: checkpoint"` |
| Finishing | `git push` |

The middle one is the lesson of the reset, and it is worth repeating that `git add -A` is
exactly what `WORKORDER-CONVENTIONS.md` §5 forbids. That prohibition was written for two
threads sweeping up each other's half-finished state. With one thread the risk it guarded
against is smaller than the risk of losing everything, and §5 should be updated to say so
rather than left as a rule everybody has a good reason to ignore.

## 7. Claude and GitHub

There is a GitHub connector available to Claude, and it is **not authorised** for this session -
so nothing here was done through it. Authorising it (claude.ai connector settings) would let a
future session read issues and pull requests directly. It is not needed for any of the above:
`scripts\setup-github.ps1` uses `git` and `gh` on your machine, and nothing in this handoff
depends on that connector existing.

## 8. Afterwards

Two small things worth doing once the remote exists, neither urgent:

* **A LICENSE.** There is none, so strictly nobody may use the code - including you, on another
  machine, in a year. MIT is the usual answer for a personal tool.
* **`big.pst` was a fixture referenced by nothing.** If a large-file test is wanted later,
  generate it in the test rather than committing it. Five megabytes of one byte in the history
  cost almost nothing, but a real one would not.
