# Work order (One thread): video and audio — DRAFT, the epoch after pictures

**Doc version:** 0.1 · **Updated:** 2026-08-27 · **Applies to:** app v0.3.3
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
