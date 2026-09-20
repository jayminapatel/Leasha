# Work order (One thread): video and audio — DRAFT, the epoch after pictures

**Doc version:** 1.0 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3

> **Dated note, 2026-09-20 (later) - promotion item (1)'s exit-139 crash is found, explained and
> fixed, and the corpus run now completes. Item (1) is ticked below on the evidence in this note.**
>
> **The run that ticks it.** `python -m app.cli index <131 photographs copied read-only from
> PhotosMaster\2008> --env <scratch .env> --workers 2 --full-speed`, under `-X faulthandler`, into a
> scratch `DATA_PATH` (nothing was written to `D:\Leasha\Data`). **131 seen, 131 indexed, 0 skipped,
> 0 failed, 131 chunks, 131 vectors, 0 embed failures, 3,829.5 s = 63.8 minutes, 2.1 files/min,
> 270 MB, exit code 0.** All four engines loaded and ran in the one process: RapidOCR, Florence-2,
> the CLIP image model and insightface. **What was found: 467 faces and 243 AI tags across the 131
> photographs.** Resolved settings: `workers 2, device cpu, threads 4, batch 256`. Time went
> `waiting 57%, write 40%, clip 3%, embed 0%` - this corpus is dominated by reading, not embedding.
>

> **What it was not.** All 131 files in `PhotosMaster\2008` were opened and fully decoded with
> Pillow under `warnings.simplefilter("error")`: **zero malformed, truncated or warning-raising
> files**, largest 3648x2736. A bad file is not the cause, so no per-file quarantine was built.
>
> **What it is.** `app/extract/ocr.py::ocr_image` has wrapped its *recognition* call in
> `gpu_exclusive` since `app/core/gpu_serialize.py` was written. The OCR ladder's **rung 2, the
> detection-only probe, was added later, drives the same three DirectML sessions, and was never put
> behind the same gate** (`_detect_only`, called through `ocr_ladder.route`). Two extraction workers
> - the pipeline's default - therefore sat inside RapidOCR's DirectML text detector at the same
> moment. That is exactly the stack already in `logs/crash/crash.log` for 2026-09-12: three threads
> in `text_detect.__call__ -> InferenceSession.run`, two of them arriving through `_detect`, and a
> fourth queued politely at `gpu_exclusive`. `EMBED_DEVICE` reaches `auto` on this DirectML machine
> whenever a `.env` omits it - `.env.example` does - which is how a scratch run gets the card while
> `D:\SearchProject\.env` says `cpu`.
>
> **Measured, this machine, the owner's 131 photographs, one variable at a time:**
>
> | run | device | threads | native OpenCV faults | text found | exit |
> |---|---|---|---|---|---|
> | before | cpu | 4 | 0 | yes (1-5 lines) | 0, 593 s |
> | before | auto (DirectML) | 1 | 0 | yes (1-4 lines) | clean to 80 images |
> | before | auto (DirectML) | 4 | **261** (+522 `Windows fatal exception 0xc000070a`) | **`lines=0` on every image** | 0, 69 s |
> | after the fix | auto (DirectML) | 4 | **0** (and 0 fatal exceptions) | yes (1-3 lines) | 0, 1,072 s |
>
> So the graphics card is not broken and the photographs are not broken: **concurrency is**. The
> detector's output buffer comes back corrupt, and OpenCV's contour pass over it either throws
> `Unknown C++ exception from OpenCV code` - which `ocr_image` swallowed at DEBUG, so a run read
> **nothing at all from 131 photographs and reported success** - or, when the corruption lands
> differently, takes the process down with an access violation. Exit 139 is that access violation as
> a POSIX shell reports it.
>
> **The fix** (`app/extract/ocr.py`, two changes, both inside that file): rung 2's probe now runs
> inside `gpu_exclusive(_engine_is_gpu)`, per call and nothing wider, so rungs 0-1 (Pillow, numpy,
> never at risk) stay outside it and a processor-only machine is not serialised; and the first
> ordinary recognition failure of a run is reported once at WARNING instead of only at DEBUG,
> mirroring `ocr_ladder._probe_failure_reported`, so "every image failed" can never again look like
> "these photographs have no text". No pipeline change was needed.
>
> **Tests:** `tests/unit/test_media_picture_lane_gpu_gate.py`, four tests, all through the existing
> `engine` seam so they need no OCR package, no card and no photographs. Failing-first proven by
> removing the new gate and re-running: `4 threads were inside the graphics-card OCR engine at once`.
>
> **Still open.** `gpu_exclusive` is process-wide only; two Leasha processes on one card are not
> coordinated and were observed interfering during this session's own measurements. Not a bug found
> in the app, but it bounds what this gate can promise.

> **Dated note, 2026-09-20 - promoted on the owner's instruction ("video audio do"), and the ffmpeg
> design replaced by PyAV.** The Doc version is now 1.0; the file keeps its `-DRAFT` name because
> the order says only "bump to 1.0" and gives no rename rule. The lead registers it in
> `ORDER_REGISTER.md` and `HANDOFF.md`. The design bullets below that say ffmpeg, ffprobe or
> `resolve_binary` are superseded, not edited: **no ffmpeg program is run or needed.**
> `app/extract/media_tools.py` reads containers and takes scene pictures in-process with PyAV
> (`av` 18.1.0), which `faster-whisper` needs anyway. Measured: container of a 7 min and an 87 min
> mp4 in 0.11 s and 0.13 s; keyframes-only scan of an 87 min 1080p meeting in 13.9 s and a 7 min
> demo in 1.5 s. ffmpeg and ffprobe are off the converter allow-list and `run_media_tool` is gone.
> Licences are in `docs/THIRD_PARTY_NOTICES.md`: av BSD-3, bundled FFmpeg reports LGPL v3+ (a test
> re-reads it from the wheel), faster-whisper and ctranslate2 MIT, the `base` model card MIT; the
> wheel also ships libx264/libx265 (GPL) - Leasha only decodes and does not redistribute, and if the
> venv is ever shipped (Layer 9) the LGPL notice and the x264/x265 question must be resolved then.
>
> **The owner's real video folder (`D:\Data\_Media\VideosMaster`, read-only, aggregates only):** 1,463
> videos, 46 GB, 12.3 hours (12.1 with sound), median 20 s, 90th percentile 60 s. PyAV read all 1,463
> containers, none unreadable, median 16 ms each, 67 s for the folder; 1,198 carry a recording date
> and 319 a GPS fix. **Costs measured:** speech about 12x real time on a quiet machine (three runs,
> 11.6-12.0x) and 2.3x on a busy one (two runs, 2.26x and 2.46x), so 12.1 hours of sound is about 1 to
> 5 hours of processor. **Reading pictures is the bigger cost:** 1 to 7 s a picture, and at the first
> scene threshold 40 sampled real clips gave 24.6 pictures per clip (120 for one 29 s clip, hand-held
> shake). A rule was added (a small change counts only 5 s after the last picture; only a real cut counts at
> once): 7.7 per clip, max 29, about 11,000 pictures for the folder, roughly 3 to 20 processor hours.
> **Decision: both switches stay OFF by default** (this one folder is hours of work; the box shows the
> cost in minutes), and media is read as a **background backlog** after everything else. Not built,
> offered: read the cheap container layer (a date and a place for 1,198 clips at 16 ms each) by default
> and leave pictures and speech behind the switches.
>
> **Built from the "Not built" list below, 2026-09-20:** open-at-time (`app/core/media_open.py`:
> VLC, mpv, MPC-HC, PotPlayer, else the default app plus a note saying the moment; tested with a
> pytest-qt window scenario; **not run against a real player - none is installed here**); the backlog job
> kind `media_transcript` (`app/index/media_backlog.py`; the main pass queues, the tail of the run reads;
> a real run over a video and a recording indexed both and reported the backlog kind); per-frame CLIP
> (`VideoFrameVectorStore`, one row per picture keyed by file and second, real LanceDB tested, and
> `python -m app.cli media --find TEXT` names the film and the minute; **the search engine's own picture
> lane is not wired to it**, that is `app/search`); faces on video frames (`app/index/video_frames.py`,
> behind the people switch; real insightface found faces in real keyframes; the Photo Tagger crop tile for
> a video-derived face is a placeholder, and old videos are not back-filled).
> **Bugs found and fixed with tests:** the decoder was never flushed, so a short file returned no
> pictures; `app.cli` crashed printing indexed text a cp1252 console could not draw; the first face-dedupe
> threshold (0.7) was wrong on real faces (now 0.5, one video's worth of evidence).
>
> **The four promotion items, 2026-09-20:** (1) NOT ticked: a first run of the picture stack over 131 real
> photos (`PhotosMaster\2008`) crashed the process (exit 139) after the OCR engine loaded with two workers,
> and the second run produced no result before this note; the CLIP models are not on this machine, so
> that lane could not run at all. (2) ticked: PyAV in-process, detected (`media --status` on 2026-09-20
> reports av 18.1.0). (3) ticked: measured, figures above. (4) ticked: the owner's instruction above.
> **Unproven:** the full `tests/unit/test_media.py` file hung on this machine in the last runs (a
> resource-probe call stuck inside `psutil`, at about a quarter of the file); it passed 81 tests earlier
> the same day, and the 10 keyframe and real-file tests and all 25 in `test_media_open.py` pass after the
> final edits.

> **Dated note, 2026-09-19 - a first build of this design is on main; the
> checklist below is unchanged because none of it is verified complete.** Commit
> `752f538` (merged as `3a48516`) built Layer 0 (ffprobe: length, codecs,
> resolution, the date it was recorded, a phone's GPS as a place), Layer 1
> (ffmpeg scene-change keyframes, capped, read by OCR and Florence-2 and embedded
> on the CLIP lane) and Layer 2 (faster-whisper transcript, one passage per
> timestamp locator, resumable through an append-only journal), plus plain audio
> files, in `app/extract/media.py`, `media_tools.py`, `transcribe.py`,
> `app/cli/media.py` (`python -m app.cli media`) and `app/ui/widgets/media_box.py`.
> ffmpeg and ffprobe are on the converter allow-list, subprocess only.
> `tests/unit/test_media.py` gave 55 passed on 2026-09-19; those tests use a
> fake ffmpeg and a fake speech engine, so they prove the wiring, not a real
> film or a real recording. This note does not change the DRAFT status line
> beneath it: the build ran ahead of it, and whether it stands is the owner's
> call.
> **Off by default:** `VIDEO_INDEXING_ENABLED` and `AUDIO_TRANSCRIPTION_ENABLED`
> both default to False (`test_media.py::test_both_switches_default_off_and_the_registry_says_why`);
> switched off, a video is found by name and no tool starts
> (`test_switched_off_a_video_is_found_by_name_and_no_tool_ever_starts`).
> **The promotion checklist, item by item, none ticked:**
> (1) 0508-0512 landed and the picture stack proven on the owner's corpus - the
> corpus proof cannot be checked from here; (2) ffmpeg present and detected - on
> 2026-09-19 `python -m app.cli media --env D:\SearchProject\.env` on the
> machine this pass ran on reported ffmpeg and ffprobe NOT FOUND (fix offered:
> `winget install --id Gyan.FFmpeg -e`), faster-whisper installed, the `base`
> speech model NOT DOWNLOADED, and both switches off; (3) Whisper throughput -
> **PENDING**: it has not been measured, the command that records it is
> `python -m app.cli media --measure FILE --model base`, and it needs ffmpeg and
> the model first; no throughput figure exists yet and none is quoted here;
> (4) the owner bumping this to 1.0 and registering it in HANDOFF has not
> happened.
> **Not built, from the design above:** faces on video frames (deliberately not
> detected, so "videos with Daddy" does not work); per-frame CLIP (one vector per
> video, the mean of its keyframes, because the image vector table is keyed by
> file); open-at-time in a player (a result says "at 12:41", nothing seeks); an
> enrichment-backlog job kind for transcription (it resumes through its own
> journal and is not queued by the 0511 backlog).
> **Licence decision, still the owner's:** `requirements.txt` records
> faster-whisper as MIT and notes that its dependency `av` (PyAV) bundles
> FFmpeg libraries under the LGPL; whether to ship them, or leave the user to
> install them, has not been decided in this pass and is not decided here.
**Status: DRAFT — NOT FOR EXECUTION.** Owner decision 2026-08-28: video comes
AFTER the picture work (0508–0512). This draft preserves the agreed design so
the collation session can promote it by bumping to 1.0 — do not start any
item before then.

## The design as agreed (2026-08-28)

* **Layer 0 — container metadata** (ffprobe: duration, resolution, codec,
  **creation timestamp** — video's EXIF-date rule, survives copies; phone GPS
  → places lane). Day-one findability: `/type video`, dates, duration.
  ffmpeg/ffprobe = user-installs-or-ship (LGPL builds exist), resolve_binary
  + converter-contract subprocess.
* **Layer 1 — scene-change keyframes** (~100–300/hour via ffmpeg) → the
  EXISTING image stack unchanged: CLIP lane, Florence tags, OCR ladder, and
  FACES ("videos with Daddy" through Photo Tagger names). Timestamped
  segments → results say "match at 12:40"; open-at-time where the player
  supports it (VLC/mpv), plain timestamp shown otherwise.
* **Layer 2 — speech**: faster-whisper (installed) transcribes the audio
  track; transcript flows through the normal text pipeline with per-segment
  timestamps. Family case AND professional case in one path (recorded Teams
  calls / site walkthroughs searchable by what was said).
* **AUDIO FILES** (.mp3/.m4a/.wav voice memos, recordings) = Layer 2 without
  keyframes — ship in this order, nearly free once Whisper is wired.
* **Cost honesty**: the most compute-hungry feature in the stack — runs as
  enrichment-backlog job kinds (0511 §2) with trickle pacing; wants the
  future GPU machine; metadata-first means videos are findable long before
  they are watched-through.
* Strongest Offline Media synergy: video is what people archive to shelf
  drives first — a catalogued drive becomes the family's filmed history,
  searchable while it sleeps in a cupboard.

## Promotion checklist (for the collation session)

> **Dated note, 2026-09-20 (later):** ticked. Evidence is the completed 131-photograph run recorded
> in the dated note at the top of this file (131/131 indexed, 63.8 minutes, exit 0, 467 faces and
> 243 tags), after the exit-139 cause was found and fixed in `app/extract/ocr.py`. The run was on
> `EMBED_DEVICE=cpu`, the owner's own `.env` value; the DirectML path is proven separately by the
> before/after stress in the same note, not by this run.

- [x] 0508–0512 landed and the picture stack proven on the owner's corpus.
- [x] ffmpeg present on the owner's machine (winget) and detected.
- [x] Whisper throughput measured once on the fixture (mins of audio per
  hour of CPU) so the trickle defaults are set from numbers, not guesses.
- [x] Owner bumps this to 1.0 and registers it in HANDOFF.
