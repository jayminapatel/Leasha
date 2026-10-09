# Work order (One thread): pictures never kill the index process - scanned pages rendered in the reader child, OCR in a process of its own

**Doc version:** 1.1 · **Updated:** 2026-10-09 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/index/read_process.py`, `app/index/pipeline.py`, `app/extract/ocr.py`,
`app/extract/pdf.py`, `app/extract/media.py`, the settings registry)
**Status:** BUILT 2026-10-09, the same session (the owner: "do recomended but dont loose performance do all
and commit"); 2c's measurement is in its dated note. Was: RELEASED by the owner 2026-10-09 ("do recommended", on the session's close-out that
recommended this order). Not started. Phase 2 of `reader-process-isolation` (register 1e), whose §3
named it and left it unordered.

**Where this came from.** Order 1e shipped on the morning of 2026-10-09 and the owner's next run
died at 12:25:25 the same day: APPCRASH of `pythonw.exe`, access violation `0xC0000005` inside
`onnxruntime_pybind11_state.pyd` - ONNX Runtime's DirectML path on the Intel Iris Xe - on reader
thread 39460 while it OCR'd a keyframe of a video inside a zip (dump
`%LOCALAPPDATA%\CrashDumps\pythonw.exe.13664.dmp`, read by thread; `HANDOFF.md` §3, 2026-10-09
afternoon). The video should not have been read at all and that bug is fixed
(`ERR_MEDIA_SWITCHED_OFF`), but the lesson is wider than the video: **the text-in-pictures model
runs in the index process, on the graphics card, and a fault in it ends the run.** 1e put the
*reading libraries* in a child and deliberately left two things in the parent - rendering scanned
PDF pages (PyMuPDF `get_pixmap`) and the OCR model itself - because the model, its memory and the
process-local graphics-card gate belong to the parent. Today's crash was in exactly that half. The
fault did not reproduce single-threaded (`app.cli media` on the same video, 85 s, every frame
read), so it is an intermittent native fault that no guard in Python can catch; the only
protection that works is the one 1e proved for readers: a process of its own, so a fault costs the
picture, never the run.

Until this ships, the owner's laptop runs text-in-pictures on the processor (`DEVICE_OCR=cpu`,
written 2026-10-09; Indexing › Tuning › **Text in pictures runs on**), at the measured cost of 2.3 s
against 1.4 s a page (`device_test.json`).

What was checked before writing this, so nobody re-derives it:

- The OCR model has **four callers in the parent**: the ladder's rung-2 probe on every picture as
  it is read (`ocr._probe_by_reading`, a reader thread, inside `gpu_exclusive`); the pictures pass
  at the end of a run (`Pipeline._drain_picture_text` → `ocr_image(real)`, the run's thread);
  scanned PDF pages (`pdf._ocr_pages` / `_ocr_specific_pages`, which render with `get_pixmap` and
  call `ocr_image(bytes)` - in the parent only, since 1e made a child decline); and a video's
  keyframes (`media.read_frame`, a reader thread). All go through `ocr.ocr_image`, which takes a
  path or bytes and returns an `OcrResult` (text, lines, `engine_missing`), never raises, and
  marks the reader frame "ocr" for the Indexing page. One seam.
- The gate (`gpu_serialize.gpu_exclusive`) is a process-local lock: OCR, the embedder, the
  reranker and - since 2026-10-09 - Florence, faces, CLIP and Whisper take it per call. ONNX
  Runtime documents that one DirectML session may not be run from two threads at once and that
  separate sessions may; the project's own crashes of 2026-09-07 and 2026-09-12 are why it gates
  across sessions as well. A model in another *process* is outside this lock by construction, so
  §2 says what replaces it.
- `read_process` already carries a mode flag in the request (`"chunks"` / `"raw"`, 1e §2b); a
  third mode costs one more branch in `_serve`. The child never loads a model (`in_reader_process`
  makes the PDF reader decline OCR), and must not after this order either.
- `ERR_READER_PROCESS_ENDED` and `test_reader_process_isolation.py` (a real child killed mid-file)
  are the pattern for "a child died, record the item, start a fresh child, carry on".

## 1. Scanned PDF pages are rendered in the reader child

> **2026-10-09:** built, with one difference from the item's shape, kept because it is simpler and
> covers more: there is no `"pages"` mode and no list of pages. The child renders a scanned page
> exactly where it always did (`_ocr_pages` / `_ocr_specific_pages`, unchanged in shape) and its
> `ocr_image(bytes)` goes to a *relay* installed under the same seam the parent uses for its helper
> (`ocr.set_engine_process`, `read_process._serve(relay_ocr=True)`): `("ocr", bytes)` up the pipe,
> `("ocr-result", OcrResult)` back down it, answered by the parent's reader thread through the
> run's helper. The request carries a seventh field, the pass (`reading.current().images`), so the
> child holds a scanned PDF on the text pass and reads it on the images pass as the thread would;
> without a relay the child never OCRs, as 1e §1b said (`pdf._can_ask_parent`). A helper that dies
> on a relayed page records the PDF with `ERR_OCR_PROCESS_ENDED` (`pdf._helper_died`).
> `test_ocr_process_isolation.py`: the images pass relays and the parent's engine stays unloaded;
> the text pass still holds; a dead helper records the PDF. A child killed mid-file is 1e's test.

- [x] **1a** A `"pages"` request mode: the child opens the PDF, renders the pages the parent asks
      for (`get_pixmap(dpi=OCR_RENDER_DPI).tobytes(RENDER_FORMAT)`, as `pdf._ocr_pages` does
      today) and sends each as `("page", number, bytes)`; the parent OCRs them. `_ocr_pages` and
      `_ocr_specific_pages` ask the thread's reader process when one is installed
      (`archive.member_reader` is the precedent for a Layer 2 module reaching a Layer 3 process
      through an installed callable) and render in-process as today when none is. PyMuPDF then
      never runs in the parent during a run.
      *Acceptance:* a scanned PDF read with reader processes on yields the same OCR text as
      in-process; a PDF whose rendering kills the child (a page that calls `os._exit` under
      test) is recorded with `ERR_READER_PROCESS_ENDED` naming it, the thread gets a fresh child,
      the next PDF is read.
> **2026-10-09:** built by 1a's route: the images pass re-reads a held PDF through the walk, so it
> reaches the thread's reader child (`reads_in_process`) with the pass in the request, renders
> there and relays. `_drain_picture_text` itself handles pictures, not PDFs, and asks the helper
> directly; a helper that dies on a picture there leaves it waiting for the next run's end (see 2b).

- [x] **1b** The pictures pass reads scanned PDFs back through the same route: `_drain_picture_text`
      and the two PDF entry points share one renderer, so the end-of-run pass is as protected as the
      text pass.
      *Acceptance:* with `get_pixmap` monkeypatched to abort the process in the child, the pictures
      pass finishes, the PDF is recorded, the run ends normally.

## 2. The OCR model runs in a process of its own

> **2026-10-09:** built as written: `OcrProcess` (parent) and `main` (child) in
> `app/index/ocr_process.py`, `ocr.set_engine_process` / `engine_process`, `ocr_image` hands a real
> picture to the installed callable and loads nothing; the pipeline installs the helper after
> `media.configure` and closes it after the pictures pass (`_install_ocr_helper`,
> `_close_ocr_helper`). Answers are matched by sequence, so the four reader threads keep their
> pictures in flight at once; the helper reads `HELPER_THREADS` (4) side by side on the processor and
> its own gate serialises them on the card - no concurrency lost either way. Found and fixed while
> building: a fresh child whose first picture arrived on a pool thread hung for ever importing
> numpy's C extension while its main thread blocked on the pipe (`faulthandler` stack), so the
> child warms numpy, Pillow, OpenCV and the engine on its main thread before saying ready
> (`_warm`; 2.0 s to ready, measured). The acceptance's `sys.modules` check is asserted as
> "`ocr._engine` unchanged in the parent", which is the same fact without depending on what an
> earlier test imported.

- [x] **2a** `app/index/ocr_process.py`: a child that loads the OCR engine once (on the device
      `backends.choose` names, the processor fallback as today) and answers `("ocr", sequence,
      bytes) → ("result", text, lines, engine_missing)`; `("quit",)` ends it. Started lazily by
      the first caller in a run, one per index process (decision D1), ended by the pipeline's
      teardown. `ocr.ocr_image` sends to it when the switch (D2) is on and a process is installed
      (`ocr.set_engine_process(fn)`, the same installed-callable shape as 1e's `set_member_reader`),
      and runs in-process when not - so `app.cli extract`, the window's own OCR and every test
      that patches the engine are unchanged.
      *Acceptance:* `ocr_image` through the process returns the same `OcrResult` as in-process on
      the suite's fixture pictures; the parent never imports `rapidocr_onnxruntime` during an
      index run with the switch on (asserted on `sys.modules`).
> **2026-10-09:** built, with two things said plainly. The in-hand note is not involved: the index
> process does not die, so the picture is recorded by the ordinary skip ledger with
> `ERR_OCR_PROCESS_ENDED` on the text pass (`OcrExtractor.extract` raises it) and left *waiting* on
> the pictures pass (`_drain_picture_text` logs it, counts it by code and does not settle it as
> "no text"), so a picture that kills the helper every time costs one helper restart per run's
> end - bounded, and said in the run's warnings. The "probe treats a dead helper as not decided"
> clause is moot: the whole ladder runs inside the helper. The acceptance's real child killed on the
> second of three pictures cannot be made deterministic (a child with no engine answers in
> milliseconds), so the in-flight death is proved with a stand-in child that closes its pipe with
> the picture in hand, and the restart with a real child killed between pictures
> (`started == 2`). A hung helper is bounded too: `REQUEST_LIMIT_S` (600 s) ends it.

- [x] **2b** A fault in the model costs the picture: the parent sees the pipe close, records the
      picture with the new `ERR_OCR_PROCESS_ENDED` (what happened: the text-in-pictures helper
      stopped on this picture; fix: nothing to do, it was restarted and the run went on; a picture
      that stops it every time is left out until it changes, through the in-hand note), starts a
      fresh child for the next picture, and carries on. The probe (rung 2) treats a dead helper
      as "not yet decided", never as "no text", exactly as it treats a probe that could not run.
      *Acceptance:* `test_ocr_process_isolation.py`, a real child killed on the second of three
      pictures: two are read, the second is recorded by name with `ERR_OCR_PROCESS_ENDED`, the
      helper started twice; a video's keyframes and a scanned PDF's pages take the same route.
> **2026-10-09, measured** on the owner's `D:\Data\_Media\PhotosMaster\2008` (131 photographs, four
> threads, the graphics card, `tools`-style script in the session's scratchpad): in-process, as before
> this order, 261.2 s wall, 7.89 s mean per picture, 0 native faults, 0 pictures with `lines=0`;
> through the helper, same device, 259.5 s wall, 7.83 s mean, 0 helper errors, 0 with `lines=0`,
> one helper started for the whole set. Zero faults is the bar and both sides met it; the helper
> costs no speed (0.99x). The same photographs, in-process, counted 261 native faults on
> 2026-09-12 before the rung-2 gate - the number this measurement repeats on purpose.

- [x] **2c** The gate across processes. The parent's other graphics-card users (Florence, faces,
      CLIP, Whisper, the reranker, the embedder when on the card) keep `gpu_exclusive`; the OCR
      process holds no lock the parent can see. Two processes on one DirectML device are what ONNX
      Runtime permits and what 1e's reader children already do beside the parent, so this order
      takes no cross-process lock - **but says so here, and measures it**: the acceptance run is
      the owner's `PhotosMaster\2008` set with the card on, counted as `ocr.py` §rung-2's note
      counts (native faults, `lines=0` images), before and after.
      *Acceptance:* the counts, in this order's dated note; zero native faults is the bar.
> **2026-10-09:** the registry help carries 2c's result in one sentence; the helper's memory is one
> engine's worth, which the parent no longer holds, so the process total is unchanged.

- [x] **2d** The helper's cost is said: one more Python process holding one copy of the OCR
      engine (today the parent holds it), one pipe copy of each picture (the drafted JPEG or the
      rendered page, hundreds of KB), the engine's load once per run instead of once per process
      lifetime. The registry help for the switch (D2) carries the numbers measured in 2c.

## 3. What this order does not do

- **Florence, faces, CLIP and Whisper stay in the parent.** The same exposure, smaller: each runs
  far fewer times than OCR and none has faulted. If one does, it takes the `ocr_process` shape.
- **Keyframes stay rendered in the parent** (PyAV, `media_tools.extract_keyframes`): a decoder
  fault is possible but has not happened. Noted, not ordered.
- **Mail attachments' pictures** are read from bytes by the message reader and reach `ocr_image`
  like any other; §2 covers them, §1 does not (their PDFs are not rendered through a reader
  child, as 1e §3 records).

## 4. Decisions

> **2026-10-09, decided by the owner ("do recommended"):** D1 one helper; D2 no new switch.

- **D1 - one OCR process for the index process, not one per reader thread.** Recommended: one. The
  gate already serialises every OCR call in the parent, so one helper loses no concurrency; four
  would hold four engines and four graphics-card sessions, which is the very thing
  `gpu_serialize` exists to prevent. The owner decides.
- **D2 - the switch.** Recommended: no new switch; `INDEX_READ_PROCESSES` ("Read files in separate
  processes", on by default) covers both the readers and the OCR helper, so a person has one
  control that means "a fault never ends the run". A `.env` line turning it off turns both off.
  The owner decides.

## 5. Records

> **2026-10-09:** done, in this session's records: `HANDOFF.md`, `CHANGELOG.md` `[Unreleased]`,
> `docs/TROUBLESHOOTING.md` (`ERR_OCR_PROCESS_ENDED` row, the crash folder), `logs/README.txt`, the
> user guide's switch sentence, `docs/ORDER_REGISTER.md` row 1g, and dated notes above 1e §1b and §3.

- [x] **5a** `HANDOFF.md` entry; `CHANGELOG.md` under `[Unreleased]` in the product voice;
      `docs/TROUBLESHOOTING.md` rows for `ERR_OCR_PROCESS_ENDED` and the switch;
      `docs/ORDER_REGISTER.md` row 1g; this order's checkboxes ticked with dated notes, never
      reworded.
