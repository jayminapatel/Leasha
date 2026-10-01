# Versioning

**Doc version:** 1.3 · **Updated:** 2026-10-01 · **Applies to:** app v0.3.3

## Scheme

`MAJOR.MINOR.PATCH`, semantic-versioning shaped but mapped onto the build layers, because
this is a single-user desktop app with no public API and no external consumers.

| Part | Bumped when |
|---|---|
| **MAJOR** | The index format changes in a way that cannot be migrated — a full re-index is required. |
| **MINOR** | A layer from `BUILD_SPEC_V2.md` is completed and its acceptance tests pass. |
| **PATCH** | Fixes, tuning, and dependency bumps that change nothing about the schema. |

`0.x` means pre-first-usable-release. The first genuinely useful build is the end of
**Layer 4** — a headless search that returns correct results in under 300ms. That is `0.5.0`.

### Planned map

> *Note, 1 October 2026:* Layers 6 and 7 (graph and Office builder) were removed and cancelled, so `0.7.0` will
> not mean what this map says. Layer 8 (Interpret and Chat) is built and runs on ONNX Runtime inside
> Leasha by default. The app is still `0.3.3`; when it next moves, this map needs the owner's
> renumbering. The map below is kept as written.

| Version | Milestone |
|---|---|
| `0.1.0` | Environment, installer, doctor, build spec, project skeleton |
| `0.2.0` | Layer 0 — foundation |
| `0.3.0` | Layer 1 — storage |
| `0.4.0` | Layers 2–3 — extraction and the indexing pipeline |
| `0.5.0` | Layer 4 — search meets the <300ms budget |
| `0.6.0` | Layer 5 — UI shell |
| `0.7.0` | Layers 6–7 — graph and Office builder |
| `0.8.0` | Layer 8 — optional RAG answers |
| `0.9.0` | Layer 9 — hardening and packaging |
| `1.0.0` | 24-hour soak passed, force-kill recovery verified, cold start under 5s |

## Single source of truth

The version lives in the top-level **`VERSION`** file. Nothing else hardcodes it.

- Python reads it via `app.core.version` → `__version__`, `build_info()`
- PowerShell reads it with `Get-Content .\VERSION`
- `build_info()` also returns `git describe --tags --always --dirty` when a repo is present,
  so a build can always be traced to a commit — and never raises when `.git` is absent.

## Document versions

Every `.md` in the repo carries its own version. The specs drive the build, so a spec that
changed silently is a spec nobody can trust — and "which version of the plan were we working
to?" has to be answerable from the file itself, not from `git log`.

Immediately under the H1, every document carries exactly this line:

```
**Doc version:** 2.1 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2
```

| Field | Meaning |
|---|---|
| **Doc version** | `MAJOR.MINOR`, the document's own. **MAJOR** on a rewrite that voids the previous version — the V1→V2 architecture change is why the two big specs start at `2.0`. **MINOR** on any change to content that a reader would act on differently. Typos and formatting do not bump it. |
| **Updated** | ISO date of that bump. |
| **Applies to** | The `VERSION` the document was last checked against. It may lag the app version; when it does, the gap is the honest signal that the document needs a review. |

Document versions are **independent of the app version**. A doc at `2.1` alongside an app at
`0.3.1` is normal and correct — they count different things.

`tests/unit/test_docs_versioned.py` enforces the header on every tracked `.md`, so a new
document cannot be added without one. A doc-version bump is recorded in `CHANGELOG.md` under
a `### Docs` heading in the same release as the change it describes.

## Schema version

The index schema carries **its own integer**, in the `schema_version` table, independent of
the app version. Migrations in `app/storage/migrations.py` step it forward one at a time.

Rules:

- The app refuses to open an index with a **higher** schema version than it understands, and
  says so with an `AppError` rather than corrupting it.
- LanceDB is a **derived** store. If it disagrees with SQLite, SQLite wins and the vectors are
  rebuilt from `chunks`. Never the reverse. A vector-format change therefore needs no MAJOR
  bump — only a rebuild.

## Git conventions

> *Note, 1 October 2026:* in practice since late September, helper threads work in `git worktree`s and one
> thread merges, and some work is committed straight to `main` on the owner's word (recorded in
> `HANDOFF.md` §3). GitHub `jayminapatel/Leasha`, branch `main`, is the only master; see `CLAUDE.md`.
> The current schema version is **30**.

**Branches** — `main` is always in a state where `doctor.py` passes and the test suite is
green. Work happens on `layer/<n>-<name>` (e.g. `layer/1-storage`) or `fix/<short-name>`,
and merges to `main` when the layer's acceptance tests pass.

**Commits** — Conventional Commits, so the changelog can be assembled from history:

```
feat(storage): add FTS5 sync triggers for chunks
fix(installer): treat winget UPDATE_NOT_APPLICABLE as success
perf(search): push ext filter into the LanceDB query
docs(spec): regenerate layer plan against V2 architecture
chore(deps): bump lancedb to 0.37.1
test(extract): add corrupt-PDF fixture
```

> *Note, 1 October 2026:* `graph` and `office` are dead packages; the live ones also include `ort`, `chat`,
> `reports`, `shell` and `cli`.

Scopes match the package names: `core`, `storage`, `extract`, `index`, `search`, `graph`,
`office`, `llm`, `ui`, `installer`, `deps`, `spec`.

**Tags** — annotated, `v`-prefixed, one per version bump:

```powershell
git tag -a v0.1.0 -m "Environment, installer, doctor, build spec, skeleton"
```

## Release checklist

1. All acceptance tests for the layer pass (`BUILD_SPEC_V2.md`).
2. `doctor.py` prints **READY** on a clean install.
3. Performance budget re-measured and recorded — never assumed.
4. `VERSION` bumped.
5. Every document changed since the last release has its **Doc version** and **Updated**
   bumped, and its **Applies to** set to the new `VERSION`.
   `pytest tests/unit/test_docs_versioned.py` is green.
6. **`HANDOFF.md` updated** - current state, the next layer, and any new decision or trap
   worth recording. `pytest tests/unit/test_handoff_current.py` is green.
7. `CHANGELOG.md` `[Unreleased]` section promoted to the new version with today's date,
   including its `### Docs` subsection.
8. Commit `chore(release): v<x.y.z>`, then tag.

Steps 5 and 6 are enforced by tests rather than trusted to memory. Documentation that has
quietly gone stale is worse than none: it is confidently wrong, and someone acts on it.

## Never committed

`.env` (machine-specific paths), `venv/`, `logs/`, and anything under `DATA_PATH`.
`.env.example` is the shared template. See `.gitignore`.
