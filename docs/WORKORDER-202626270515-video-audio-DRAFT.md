# Work order (One thread): video and audio — DRAFT, the epoch after pictures

**Doc version:** 0.2 · **Updated:** 2026-09-19 · **Applies to:** app v0.3.3

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

- [ ] 0508–0512 landed and the picture stack proven on the owner's corpus.
- [ ] ffmpeg present on the owner's machine (winget) and detected.
- [ ] Whisper throughput measured once on the fixture (mins of audio per
  hour of CPU) so the trickle defaults are set from numbers, not guesses.
- [ ] Owner bumps this to 1.0 and registers it in HANDOFF.
