# Test coverage map

**Doc version:** 1.1 · **Updated:** 2026-10-25 · **Applies to:** app v1.1.0

What the test suite runs of `app/`, measured once for work order 1u §4c (`docs/WORKORDER-test-suite-faster.md`).
It is a map for deciding where tests are thin, not a score to chase: a line that runs is not a line that is checked.

## How it was measured

- **The suite:** the full `scripts/run_suite.py` selection on `main` at 08aadaa (order 1u, #92), in four parts on the
  laptop's venv, offscreen Qt, under `pytest-cov` with branch coverage (`--cov=app --cov-branch`), the four data files
  combined. Then the eight `tests/unit/test_cov_*.py` files of §4d (#93) were measured alone and added on top.
- **Cover** is coverage.py's figure with branches on: (lines run + branches taken) / (lines + branches).
  **Branch** is branches taken / branches. The order's targets are on **Branch**.
- **In this process only.** Code that runs in a child process - the index child, the read and OCR processes, the
  watcher child, the converters - is reached by real tests that start those processes, but coverage does not follow
  them, so those modules read lower than they are tested.
- **Not on Qt's worker threads either.** coverage.py follows threads Python starts; a `QRunnable` on Qt's
  `QThreadPool` (`app/ui/workers.py`, every `job(...)` in the windows) is not traced. So a function the windows run
  in the background reads as not run even where a test drives it: `book_actions.commit_merge`, `accept_conflicts`
  and `build_import_rows` are run by `test_contacts_tools_ui.py` and `test_contacts_conflicts_ui.py` through the merge, conflicts and import dialogs, and
  show as missed. Read a low module that the windows call as *unmeasured*, not *untested*, until checked.
- **Two runs, one map.** The first run lost part 1 twice: `test_clip_lane_wiring` downloads the real CLIP towers,
  and the download hung past the 900 s test limit, once with pytest's cache off and once from a half-finished
  download in the cache. Part 1 was run a third time with `LEASHA_REAL_EMBED_CACHE` pointing at a complete copy.
- **Workaround in the run, not in the repo:** PySide6's import hook stalls coverage while `phonenumbers` loads its
  geodata, so a small pytest plugin imported `phonenumbers.geodata` before coverage started.

## The critical paths (order 1u §4d: 80% branch)

| Package | What | Files | Branches | Branch, suite | Branch, with §4d | Met |
|---|---|---|---|---|---|---|
| `app/index` | indexing | 46 | 3,798 | 82.5% | 82.5% | yes |
| `app/search` | search | 33 | 1,806 | 91.3% | 91.3% | yes |
| `app/storage` | storage and migrations | 7 | 1,022 | 80.6% | 81.0% | yes |
| `app/accounts` | account sync writes and the delete brake | 21 | 2,254 | 78.6% | 79.1% | **no** |
| `app/contacts` | contact merges and enrichment | 45 | 2,744 | 80.2% | 80.6% | yes |
| `app/core/errors.py` | the error contract | 1 | 12 | 91.7% | 91.7% | yes |

Modules on these paths under 80% branch, with at least ten branches:

| Module | Branches | Branch |
|---|---|---|
| `app/accounts/http.py` | 68 | 75.0% |
| `app/accounts/mail_sync.py` | 104 | 79.8% |
| `app/accounts/providers/carddav.py` | 244 | 68.0% |
| `app/accounts/providers/google_people.py` | 236 | 74.6% |
| `app/accounts/providers/graph_contacts.py` | 262 | 68.3% |
| `app/accounts/providers/imap_mail.py` | 234 | 78.6% |
| `app/accounts/service.py` | 212 | 73.1% |
| `app/accounts/store.py` | 60 | 78.3% |
| `app/contacts/_pdf.py` | 30 | 73.3% |
| `app/contacts/book_actions.py` | 160 | 23.8% |
| `app/contacts/enrich/declines.py` | 34 | 73.5% |
| `app/contacts/enrich/history.py` | 36 | 72.2% |
| `app/contacts/enrich/linkedin.py` | 30 | 73.3% |
| `app/contacts/enrich/localfiles.py` | 44 | 77.3% |
| `app/contacts/enrich/measure.py` | 36 | 77.8% |
| `app/contacts/enrich/remember.py` | 48 | 64.6% |
| `app/contacts/hub.py` | 292 | 65.4% |
| `app/contacts/phone.py` | 12 | 58.3% |
| `app/index/card_pass.py` | 30 | 76.7% |
| `app/index/embed_bench.py` | 26 | 46.2% |
| `app/index/index_bench.py` | 24 | 20.8% |
| `app/index/ocr_process.py` | 58 | 48.3% |
| `app/index/offline_media.py` | 126 | 75.4% |
| `app/index/photo_metadata.py` | 100 | 72.0% |
| `app/index/pipeline_bench.py` | 94 | 72.3% |
| `app/index/read_process.py` | 78 | 67.9% |
| `app/index/stages.py` | 20 | 70.0% |
| `app/index/watch_child.py` | 36 | 63.9% |
| `app/search/evaluate.py` | 22 | 63.6% |
| `app/storage/sqlite_store.py` | 676 | 78.7% |
| `app/storage/vector_store.py` | 144 | 76.4% |

## Every package

| Package | Files | Lines | Branches | Cover | Branch |
|---|---|---|---|---|---|
| `app (top level)` | 2 | 320 | 30 | 22.6% | 30.0% |
| `app/accounts` | 21 | 6,178 | 2,254 | 85.6% | 79.1% |
| `app/chat` | 25 | 5,180 | 1,876 | 89.4% | 85.4% |
| `app/cli` | 28 | 4,064 | 1,336 | 59.2% | 48.4% |
| `app/contacts` | 45 | 6,861 | 2,744 | 87.4% | 80.6% |
| `app/core` | 52 | 5,216 | 1,474 | 85.4% | 82.0% |
| `app/extract` | 58 | 9,485 | 2,928 | 80.4% | 74.9% |
| `app/index` | 46 | 11,984 | 3,798 | 85.9% | 82.5% |
| `app/llm` | 6 | 815 | 210 | 84.8% | 77.6% |
| `app/ort` | 12 | 1,867 | 506 | 74.9% | 69.2% |
| `app/reports` | 5 | 768 | 196 | 95.4% | 96.4% |
| `app/search` | 33 | 5,086 | 1,806 | 93.4% | 91.3% |
| `app/serve` | 3 | 758 | 170 | 89.3% | 87.6% |
| `app/shell` | 3 | 147 | 46 | 69.4% | 63.0% |
| `app/storage` | 7 | 3,639 | 1,022 | 87.4% | 81.0% |
| `app/tidy` | 4 | 367 | 80 | 84.1% | 73.8% |
| `app/ui` | 317 | 46,856 | 10,862 | 85.9% | 76.5% |
| **all of `app/`** | 667 | 109,591 | 31,338 | 84.7% | 78.2% |

## Modules no test runs at all

3 of 667 modules (each line 0%):

- `app/cli/__main__.py`
- `app/ort/llm_host.py`
- `app/ui/startup_timing.py`

## Modules under 60% branch

91 modules. A module run only in a child process (above) is among them for that reason.

| Module | Lines | Branches | Cover | Branch |
|---|---|---|---|---|
| `app/accounts/provider.py` | 69 | 2 | 98.6% | 50.0% |
| `app/cli/bench.py` | 248 | 80 | 35.1% | 17.5% |
| `app/cli/cards.py` | 93 | 26 | 17.6% | 0.0% |
| `app/cli/contacts.py` | 212 | 90 | 68.2% | 56.7% |
| `app/cli/desktop.py` | 93 | 28 | 22.3% | 0.0% |
| `app/cli/evaluate.py` | 147 | 36 | 45.4% | 36.1% |
| `app/cli/formats.py` | 85 | 26 | 61.3% | 42.3% |
| `app/cli/mail_dedupe.py` | 87 | 18 | 22.9% | 0.0% |
| `app/cli/mcp_server.py` | 21 | 2 | 39.1% | 0.0% |
| `app/cli/media.py` | 155 | 48 | 21.7% | 6.2% |
| `app/cli/offline_media.py` | 193 | 76 | 46.5% | 31.6% |
| `app/cli/ollama.py` | 82 | 16 | 42.9% | 31.2% |
| `app/cli/photos.py` | 63 | 14 | 29.9% | 0.0% |
| `app/cli/repos.py` | 314 | 138 | 60.0% | 50.0% |
| `app/cli/scan.py` | 73 | 16 | 67.4% | 50.0% |
| `app/cli/search.py` | 182 | 60 | 51.7% | 38.3% |
| `app/cli/tidy.py` | 97 | 40 | 65.0% | 52.5% |
| `app/cli/watch.py` | 168 | 64 | 54.3% | 40.6% |
| `app/contacts/book_actions.py` | 391 | 160 | 40.3% | 23.8% |
| `app/contacts/phone.py` | 52 | 12 | 78.1% | 58.3% |
| `app/core/osbridge/filelock.py` | 29 | 6 | 71.4% | 50.0% |
| `app/core/osbridge/outlook.py` | 51 | 20 | 47.9% | 45.0% |
| `app/core/osbridge/recycle.py` | 142 | 54 | 70.9% | 55.6% |
| `app/core/osbridge/startmenu.py` | 54 | 8 | 43.5% | 37.5% |
| `app/core/osbridge/stdio.py` | 25 | 10 | 65.7% | 50.0% |
| `app/core/osbridge/volumes.py` | 112 | 32 | 63.2% | 56.2% |
| `app/core/version.py` | 41 | 8 | 59.2% | 25.0% |
| `app/extract/diagrams.py` | 191 | 54 | 61.2% | 51.9% |
| `app/extract/face_detect.py` | 154 | 24 | 57.3% | 50.0% |
| `app/extract/florence_tagger.py` | 113 | 18 | 55.0% | 38.9% |
| `app/extract/lo_server.py` | 184 | 50 | 10.7% | 2.0% |
| `app/extract/mobi.py` | 144 | 62 | 69.4% | 58.1% |
| `app/extract/office.py` | 285 | 104 | 60.7% | 47.1% |
| `app/extract/publisher.py` | 71 | 14 | 64.7% | 57.1% |
| `app/extract/raw.py` | 48 | 10 | 50.0% | 10.0% |
| `app/index/embed_bench.py` | 135 | 26 | 54.0% | 46.2% |
| `app/index/index_bench.py` | 110 | 24 | 16.4% | 20.8% |
| `app/index/ocr_process.py` | 281 | 58 | 55.2% | 48.3% |
| `app/main.py` | 319 | 30 | 22.3% | 30.0% |
| `app/ort/florence.py` | 114 | 8 | 54.9% | 25.0% |
| `app/ort/session.py` | 70 | 12 | 34.1% | 0.0% |
| `app/search/rerank_bench.py` | 72 | 8 | 82.5% | 50.0% |
| `app/shell/repl.py` | 90 | 28 | 51.7% | 46.4% |
| `app/ui/account_runtime.py` | 165 | 38 | 56.2% | 28.9% |
| `app/ui/accounts/panel.py` | 538 | 84 | 78.8% | 54.8% |
| `app/ui/accounts/state.py` | 54 | 8 | 32.3% | 0.0% |
| `app/ui/contacts/loader.py` | 87 | 14 | 61.4% | 21.4% |
| `app/ui/contacts/person_photo.py` | 68 | 14 | 35.4% | 42.9% |
| `app/ui/contacts/smart.py` | 130 | 36 | 74.7% | 47.2% |
| `app/ui/contacts_tools/__init__.py` | 58 | 14 | 63.9% | 57.1% |
| `app/ui/contacts_tools/cleanup_dialog.py` | 308 | 70 | 81.2% | 57.1% |
| `app/ui/contacts_tools/export_dialog.py` | 173 | 54 | 63.4% | 46.3% |
| `app/ui/contacts_tools/merge_dialog.py` | 300 | 82 | 76.4% | 57.3% |
| `app/ui/contacts_tools/print_dialog.py` | 271 | 58 | 68.1% | 37.9% |
| `app/ui/contacts_tools/qr_dialog.py` | 148 | 32 | 75.6% | 50.0% |
| `app/ui/controllers/interpret_controller.py` | 117 | 30 | 76.2% | 53.3% |
| `app/ui/controllers/mcp_controller.py` | 119 | 22 | 64.5% | 36.4% |
| `app/ui/controllers/settings_controller.py` | 559 | 142 | 60.5% | 55.6% |
| `app/ui/photos_view.py` | 202 | 24 | 85.4% | 50.0% |
| `app/ui/presenter/sync_status.py` | 49 | 28 | 46.8% | 42.9% |
| `app/ui/presenter/tidy.py` | 94 | 34 | 60.9% | 44.1% |
| `app/ui/qtsip.py` | 18 | 2 | 85.0% | 50.0% |
| `app/ui/scheduler.py` | 63 | 10 | 78.1% | 50.0% |
| `app/ui/settings_view.py` | 159 | 6 | 92.1% | 16.7% |
| `app/ui/splash.py` | 259 | 40 | 74.2% | 37.5% |
| `app/ui/tasks.py` | 683 | 152 | 58.1% | 53.9% |
| `app/ui/thumbnail_loader.py` | 88 | 20 | 46.3% | 35.0% |
| `app/ui/widgets/add_file_type.py` | 242 | 44 | 57.3% | 15.9% |
| `app/ui/widgets/answer_card.py` | 230 | 26 | 88.3% | 57.7% |
| `app/ui/widgets/archived_roots.py` | 27 | 2 | 89.7% | 50.0% |
| `app/ui/widgets/browse_table.py` | 129 | 16 | 87.6% | 56.2% |
| `app/ui/widgets/chat_commands.py` | 77 | 18 | 77.9% | 55.6% |
| `app/ui/widgets/code_types_box.py` | 83 | 14 | 76.3% | 57.1% |
| `app/ui/widgets/environment_box.py` | 181 | 10 | 72.8% | 50.0% |
| `app/ui/widgets/face_crops.py` | 98 | 28 | 63.5% | 46.4% |
| `app/ui/widgets/file_types.py` | 466 | 96 | 58.0% | 41.7% |
| `app/ui/widgets/history_pass.py` | 19 | 2 | 47.6% | 50.0% |
| `app/ui/widgets/interpret.py` | 37 | 10 | 10.6% | 0.0% |
| `app/ui/widgets/machine_card.py` | 57 | 2 | 93.2% | 50.0% |
| `app/ui/widgets/mini_search_frame.py` | 141 | 52 | 51.8% | 32.7% |
| `app/ui/widgets/model_box.py` | 243 | 46 | 77.2% | 58.7% |
| `app/ui/widgets/model_manager.py` | 276 | 44 | 74.4% | 59.1% |
| `app/ui/widgets/offline_media_dialogs.py` | 77 | 2 | 98.7% | 50.0% |
| `app/ui/widgets/page_frame.py` | 43 | 4 | 89.4% | 50.0% |
| `app/ui/widgets/photo_info.py` | 199 | 42 | 68.9% | 45.2% |
| `app/ui/widgets/photo_tagger_page.py` | 709 | 164 | 59.9% | 50.0% |
| `app/ui/widgets/photo_viewer.py` | 109 | 32 | 78.0% | 53.1% |
| `app/ui/widgets/photo_write_dialog.py` | 165 | 34 | 70.9% | 38.2% |
| `app/ui/widgets/search_home.py` | 97 | 20 | 79.5% | 45.0% |
| `app/ui/widgets/tidy_parts.py` | 88 | 8 | 61.5% | 37.5% |
| `app/ui/widgets/vision_model.py` | 100 | 14 | 86.0% | 57.1% |

## Measuring it again

Branch coverage of the whole suite takes about 20 minutes in four parts on the laptop. It is not part of CI or the
suite runner: the map is measured when it is needed, and this file is rewritten then to say what is true at that time.
