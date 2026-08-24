# Work order: config-driven file types, format editor, and OCR

**Doc version:** 1.0 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

A self-contained brief. Written by a second session that deliberately did not touch the code,
because `app/extract/` had uncommitted work in flight at the time. Everything needed to build
this is below; nothing here depends on that other session.

**Commit the working tree before starting.** This brief assumes a clean tree.

---

## 1. Why

Three requests from the project owner, in one piece of work because they share a mechanism:

1. **Adding a file type should not require writing Python.** Today `REGISTRY` in
   `app/extract/base.py` maps extension to `Extractor`, which is a good design, but every new
   format is a code change and a release.
2. **A file-types editor in the app**, so formats can be enabled, disabled and routed from
   Settings rather than by editing files.
3. **OCR for images and scanned PDFs.** The owner has explicitly chosen **on by default, all
   images and scanned PDFs**, having been told it may add days to a first 100GB index run.
   That is a deliberate scope change: `ERR_NO_TEXT_LAYER` currently says OCR is not done, and
   `LOCAL_KNOWLEDGE_GRAPH_V2.md` lists OCR as out of scope for V2. Update both.

## 2. The design, and its boundary

Three tiers. The boundary matters more than the mechanism - getting it wrong produces a YAML
dialect that is secretly a programming language, untestable and worse than the Python it
replaced.

| Tier | What it is | Example |
|---|---|---|
| **1. Config only** | Routing and policy. Which extensions are on, which extractor handles them, size caps. | `.log` -> plaintext |
| **2. One generic extractor + config** | An external command converts the file, output goes through an existing extractor. Unlimited formats, no new Python. | `.doc` -> LibreOffice -> txt |
| **3. Code** | Anything needing real parsing logic. | PDF, PST, OCR |

Tier 2 is the leverage. One implementation, and LibreOffice alone then covers `.doc .xls .ppt
.rtf .wpd .pages .odt .ods .odp` and a long tail of dead formats, added by editing config.

**Do not** attempt to express Tier 3 in config.

## 3. `config/extractors.toml`

New file, **tracked in git**, shipped with defaults. User overrides go in
`<DATA_PATH>\extractors.toml`, which is machine-specific and not tracked. Load order: packaged
defaults, then user file merged over the top. Never edit the packaged file at runtime.

```toml
# config/extractors.toml
schema_version = 1

[defaults]
max_bytes = 104857600          # 100MB; per-format overrides below
enabled = true

# --- Tier 1: extension -> registered extractor name -------------------------
[extensions]
".odt"  = { extractor = "odf",       enabled = true }
".ods"  = { extractor = "odf",       enabled = true }
".odp"  = { extractor = "odf",       enabled = true }
".gdoc" = { extractor = "cloudstub", enabled = true }
".png"  = { extractor = "ocr",       enabled = true, max_bytes = 26214400 }
".conf" = { extractor = "plaintext", enabled = true }

# --- Tier 2: external converters --------------------------------------------
# {input} {outdir} {stem} are substituted. `produces` names the output file.
# `then` is the registered extractor that reads it.
[converters.".doc"]
command   = ["soffice", "--headless", "--convert-to", "txt:Text",
             "--outdir", "{outdir}", "{input}"]
produces  = "{stem}.txt"
then      = "plaintext"
timeout_s = 180
enabled   = false              # off until the binary is found; doctor.py reports it
```

**Rules.**

- Unknown keys are an `ERR_CONFIG_INVALID` naming the key, not a silent ignore.
- An extension naming an unregistered extractor is `ERR_CONFIG_INVALID`, not a crash at index
  time. Validate the whole file at load.
- `schema_version` is checked like the DB schema: refuse a **higher** version with a clear
  error rather than misreading it.
- Config **cannot** override Tier 3 internals. It routes and gates; it does not parametrise
  parsers.

### Security: the converter allow-list

`command` runs an arbitrary executable from a config file. Non-negotiable:

- An allow-list of permitted executable basenames, in code, not config:
  `soffice`, `libreoffice`, `pandoc`, `xstexporter`, `tesseract`.
- Anything else is refused with a new `ERR_CONVERTER_BLOCKED`, naming the binary.
- Resolve the executable and log the **absolute path actually invoked**, every time.
- Never pass the command through a shell. `subprocess.run(list, shell=False)`.
- Converters are **disabled by default**. `doctor.py` reports which binaries were found and
  the UI offers to enable those.

## 4. What to build

### 4.1 `app/extract/odf.py` - native ODF (Tier 3, but trivial)

`.odt .ods .odp .ott .ots .otp`

Stdlib only: `zipfile` + `xml.etree.ElementTree`. Open the zip, read `content.xml`, collect
text from `text:p` and `text:h` (and `table:table-cell` for spreadsheets). Same shape as the
existing OOXML handling in `office.py`.

**Do not add `odfpy`** - it is sdist-only on PyPI, no wheel, which breaks the rule at the top
of `requirements.txt`. Verified 2026-08-24.

A corrupt or non-zip file is `ERR_FILE_CORRUPT` / `SKIP_CONTINUE`, as everywhere else.

### 4.2 `app/extract/cloudstub.py` - Google Workspace pointers

`.gdoc .gsheet .gslides .gdraw .gform .gjam .gsite`

**These files contain no document content.** They are JSON shortcuts holding a document ID, a
URL and the creating account. Google Drive for Desktop stores real bytes for `.docx` and
`.jpg`, but for anything created in Google Workspace it stores only a pointer. Verified
against the format documentation, 2026-08-24.

Indexing one naively yields a filename and a URL while *looking* successful. That is the same
failure shape as the OneDrive placeholder problem already solved with `ERR_CLOUD_ONLY`: quietly
wrong, which is worse than loudly broken.

Behaviour: parse the JSON, emit a document carrying the **title and URL only**, so the pointer
is findable by name, and attach a warning with new code **`ERR_CLOUD_STUB`**
(`SKIP_CONTINUE`) whose suggestion is:

> This is a link to a Google Docs file, not the document itself, so there is no text to index.
> To make it searchable: open it in Drive and choose File > Download > Word (.docx) or
> OpenDocument, or set Drive for Desktop to sync Workspace files as Office formats.

Never fetch the URL. That would break the offline promise.

### 4.3 `app/extract/converter.py` - the generic Tier 2 extractor

One class, driven entirely by `[converters]`. Per file:

1. Make a temp dir. Never write next to the source; the corpus is read-only.
2. Substitute `{input} {outdir} {stem}`, check the allow-list, run with `timeout_s`.
3. On timeout, non-zero exit, or missing output: `ERR_CONVERTER_FAILED` (`SKIP_CONTINUE`),
   detail carrying the last 2000 characters of stderr. **One bad file never halts the run.**
4. Hand the produced file to the `then` extractor.
5. Delete the temp dir in a `finally`. A 100GB run must not leak temp files.

Concurrency note: LibreOffice serialises on a per-user profile by default. Either run it with
`-env:UserInstallation=file:///{temp_profile}` per worker, or cap converter concurrency at 1.
Pick one and write down which, or parallel indexing will mysteriously stall.

### 4.4 `app/extract/ocr.py` - OCR (Tier 3)

**Engine: `rapidocr-onnxruntime`.** Pure-Python wheel, runs on **onnxruntime, already in the
stack via FastEmbed**. No external binary, no separate install, models cached once then fully
offline. Verified on PyPI 2026-08-24.

Rejected, with reasons worth keeping: `pytesseract` is only a wrapper and needs the Tesseract
binary installed separately; `easyocr` pulls in PyTorch (~2GB).

Scope, as chosen by the owner: **all images and scanned PDFs, enabled by default.**

- Extensions: `.png .jpg .jpeg .tif .tiff .bmp .webp .gif`
- **PDF pages with no text layer**: `pdf.py` already detects these and raises
  `ERR_NO_TEXT_LAYER`. Change that path to render the page with PyMuPDF and OCR the bitmap.
  This is the highest-value case - scanned contracts, signed PDFs - so it must not be missed.
- Emit `ocr = true` and a mean `ocr_confidence` in `Document.meta`, so results can be marked
  in the UI. OCR text is not as trustworthy as extracted text and the user should see that.
- Below a confidence floor (start at 0.5, configurable), keep the text but warn with
  `ERR_OCR_LOW_CONFIDENCE` (`AUTO_FIX`).
- A failed OCR is `SKIP_CONTINUE`, never fatal.
- Cap image dimensions before OCR. A 20000x20000 TIFF will otherwise exhaust memory.

**Say this plainly in the UI and the docs:** OCR is seconds per image against milliseconds for
text extraction. On a corpus with many images this can dominate the entire index run. The
owner accepted that trade knowingly, but the indexing view must show OCR'd counts and time
separately so the cost is visible rather than mysterious.

Two things that make it survivable, both already in the architecture: indexing is resumable, so
a long run can be stopped and continued; and the skipped-files panel groups by error code, so
`ERR_OCR_*` volume is one glance.

### 4.5 `app/core/formats.py` - load, validate, expose

Loads packaged + user TOML, validates, merges, and exposes a typed view the rest of the app
reads. `REGISTRY` in `base.py` stays the mechanism; this module *feeds* it. Do not build a
second registry.

`app.cli formats` lists every known extension with its extractor, enabled state, size cap and
whether its dependency is present. This is the headless equivalent of the editor, and it is how
the acceptance tests inspect the result.

### 4.6 The file-types editor - `app/ui/settings_view.py`

A table: **Extension | Handled by | Enabled | Max size | Status**.

- Toggle a row on or off; edit the size cap.
- **Status** is the honest column: `ready`, `needs LibreOffice`, `no local content` (Google
  stubs), `OCR - slow`.
- Add and remove custom extension rows.
- Filter box, because there will be seventy-odd rows.
- **Test button**: pick a file, run the configured extractor, show the first 500 characters
  extracted. This is the difference between an editor people trust and one they poke at
  hopefully.
- Writes to the **user** TOML, never the packaged one. "Reset to defaults" per row.
- Changing a mapping bumps the index generation, so the search cache cannot serve results from
  the old routing.

**Follow the existing UI split**: all decisions live in `app/ui/presenter.py`, which is
forbidden by an existing test from importing Qt. The view is wiring only. Presenter logic gets
unit tests; the view does not.

## 5. Formats to add

Already registered (52): `.bat .c .cfg .cmd .cpp .cs .css .csv .docm .docx .eml .go .h .htm
.html .ini .java .js .json .log .md .mht .mhtml .mpp .mpt .msg .ost .pdf .php .pptm .pptx .ps1
.pst .py .rb .rs .rst .sh .sql .toml .ts .tsv .txt .vsd .vsdm .vsdx .xlsm .xlsx .xltx .xml
.yaml .yml`

Add:

| Group | Extensions | Route |
|---|---|---|
| OpenDocument | `.odt .ods .odp .ott .ots .otp` | `odf` (new, stdlib) |
| Google stubs | `.gdoc .gsheet .gslides .gdraw .gform .gjam .gsite` | `cloudstub` (new) |
| Images / OCR | `.png .jpg .jpeg .tif .tiff .bmp .webp .gif` | `ocr` (new) |
| Legacy Office | `.doc .xls .ppt .rtf .wpd .pub` | `converter` -> LibreOffice |
| Apple | `.pages .numbers .key` | `converter` -> LibreOffice |
| Ebook | `.epub .fb2` | `converter` -> pandoc, or a small native zip+XHTML reader |
| Text-ish | `.conf .env .properties .tex .bib .adoc .org .srt .vtt .ics .vcf` | `plaintext` |
| Code | `.kt .swift .scala .r .m .pl .lua .dart .vb .asm .make .dockerfile .gradle .tf` | `plaintext` |
| Data | `.jsonl .ndjson .parquet? .avro?` | `plaintext`; leave the binary ones out for now |
| Archives | `.zip .7z .tar .gz` | **Not now.** Recursive extraction needs its own depth limits, zip-bomb guards and identity scheme. Separate work order. |

`.pub` and `.wpd` conversion quality is poor. Ship them disabled with an honest status.

## 6. Error codes to add

Register in `ERROR_REGISTRY` with message, suggestion and action type, per the contract:

| Code | Action | When |
|---|---|---|
| `ERR_CLOUD_STUB` | `SKIP_CONTINUE` | Google Workspace pointer with no local content |
| `ERR_CONVERTER_MISSING` | `RUN_COMMAND` | Converter configured, binary not found |
| `ERR_CONVERTER_FAILED` | `SKIP_CONTINUE` | Converter ran and failed or timed out |
| `ERR_CONVERTER_BLOCKED` | `USER_RETRY` | Command not on the allow-list |
| `ERR_OCR_UNAVAILABLE` | `USER_RETRY` | OCR enabled, engine or model missing |
| `ERR_OCR_FAILED` | `SKIP_CONTINUE` | OCR ran and failed on one image |
| `ERR_OCR_LOW_CONFIDENCE` | `AUTO_FIX` | Text kept, quality flagged |

## 7. Acceptance tests

Not done until all of these pass.

**Config**
- [ ] Packaged TOML loads; a user TOML merges over it without mutating the packaged file.
- [ ] Unknown key, unknown extractor name, and a higher `schema_version` each produce
      `ERR_CONFIG_INVALID` naming the offending item - not a traceback.
- [ ] Disabling an extension means the walker skips those files entirely.

**ODF**
- [ ] Round-trip `.odt`, `.ods`, `.odp` fixtures; text and structure extracted.
- [ ] A truncated `.odt` yields `ERR_FILE_CORRUPT` and the run continues.

**Google stubs**
- [ ] A real-shaped `.gdoc` JSON yields title and URL, plus `ERR_CLOUD_STUB`.
- [ ] Nothing in the code path performs a network call - assert it, do not assume it.

**Converter**
- [ ] A fake converter script (`sys.executable` writing a known file) round-trips end to end.
- [ ] Timeout, non-zero exit, and missing-output each yield `ERR_CONVERTER_FAILED`
      (`SKIP_CONTINUE`), and the batch finishes.
- [ ] A command not on the allow-list yields `ERR_CONVERTER_BLOCKED` and **never executes**.
- [ ] Temp directories are removed even when the converter is killed.

**OCR**
- [ ] A generated PNG containing known text is OCR'd and the text is found by search.
- [ ] A text-layer-free PDF page is rendered and OCR'd; `ERR_NO_TEXT_LAYER` no longer fires
      when OCR is enabled.
- [ ] `ocr = true` and a confidence value reach `Document.meta`.
- [ ] OCR disabled restores the previous `ERR_NO_TEXT_LAYER` behaviour exactly.
- [ ] An oversized image is refused before it can exhaust memory.
- [ ] **Timing recorded**: mean seconds per image on the test fixtures, written into the
      changelog. This is the number the owner needs to judge the default.

**Editor**
- [ ] Presenter tests for enable, disable, edit cap, add and remove a custom extension.
- [ ] The presenter still does not import Qt - the existing guard test must stay green.
- [ ] Changing a mapping bumps the index generation.
- [ ] The Test button returns extracted text for a good file and a rendered `AppError` for a
      bad one.

**Whole suite**
- [ ] All existing tests still pass. Nothing here should change current behaviour for the 52
      extensions already handled, except image files, which previously were not indexed.

## 8. Also update

Project convention requires these; `pytest tests/unit/test_docs_versioned.py` and
`test_handoff_current.py` enforce parts of it.

- `requirements.txt` - add `rapidocr-onnxruntime`, pinned, with a comment on why not
  pytesseract or easyocr. Verify the pin has a wheel before committing it.
- `doctor.py` - report OCR engine availability, model presence, and which converter binaries
  were found. Optional components must **WARN**, never **FAIL**.
- `LOCAL_KNOWLEDGE_GRAPH_V2.md` - remove "OCR is out of scope for V2"; add the tier model and
  the Google stub finding.
- `BUILD_SPEC_V2.md` - fold this into Layer 2, with these acceptance criteria.
- `docs/TROUBLESHOOTING.md` - entries for each new error code, in the same plain style.
- `HANDOFF.md` - new decisions: the three tiers and why config stops at Tier 2; RapidOCR over
  Tesseract; OCR on by default and the cost accepted; the converter allow-list; archives
  deferred.
- `CHANGELOG.md` and `VERSION` - this is a layer-completing change, so `0.4.0` once Layer 2 is
  signed off. **Do not bump to 0.4.0 while the real-Outlook check in `HANDOFF.md` is still
  outstanding.**

## 9. Order

Each step ends green, so it can be abandoned at any point without leaving a mess.

1. `app/core/formats.py` + TOML + validation + `app.cli formats`. No new extractors yet.
2. `odf.py` - smallest real win, no dependency, proves the config path.
3. `cloudstub.py` - prevents a silent-wrongness class of bug.
4. `converter.py` + allow-list. Unlocks the long tail permanently.
5. `ocr.py` - the largest and riskiest; **measure and record throughput**.
6. The editor. Last, because by then there is something real to edit.

## 10. Decisions made deliberately

Recorded so they are not silently reversed:

- **Config stops at Tier 2.** A config format that can express arbitrary parsing is a
  programming language with no debugger and no tests.
- **RapidOCR over Tesseract**, because onnxruntime is already a dependency and Tesseract needs
  a separate binary install.
- **OCR on by default** - the owner's explicit choice, made after being told it may add days to
  a first 100GB run. Make the cost visible in the UI rather than reversing the decision.
- **Google stubs are detected, not fetched.** Fetching means OAuth and a network round trip,
  and breaks the offline promise.
- **Archives are deferred.** Recursion, depth limits, zip-bomb guards and per-entry identity
  are a separate piece of work.
- **`odfpy` rejected** - sdist-only, no wheel.
