# Versioning

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
5. `CHANGELOG.md` `[Unreleased]` section promoted to the new version with today's date.
6. Commit `chore(release): v<x.y.z>`, then tag.

## Never committed

`.env` (machine-specific paths), `venv/`, `logs/`, and anything under `DATA_PATH`.
`.env.example` is the shared template. See `.gitignore`.
