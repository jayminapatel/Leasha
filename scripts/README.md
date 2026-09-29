# scripts

**Doc version:** 1.0 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

Helper scripts that are not part of the application package.

- `pst_field_test.py` — reads your own `.pst` files with the libpff reader, read-only,
  one process per archive, and writes a report of speed, status counts and any
  archive that stalled or failed (order 0z lane C). `--outlook` adds the archives open
  in Outlook; `--stall` sets how long a reader may stand still.

Planned:
- `bump-version.ps1` — bump `VERSION`, promote the `[Unreleased]` changelog section, tag
- `rebuild-vectors.ps1` — drop and rebuild LanceDB from SQLite `chunks`
- `make-fixtures.py` — generate the corrupt test fixtures deterministically
