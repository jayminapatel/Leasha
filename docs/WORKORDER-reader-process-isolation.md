# Work order (One thread): the index process never dies on a file - PDF and archive members read in a reader process

**Doc version:** 1.0 · **Updated:** 2026-10-09 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/index/read_process.py`, `app/index/pipeline.py`, `app/extract/pdf.py`,
`app/extract/archive.py`, the settings registry)
**Status:** SHIPPED 2026-10-09, the same session. RELEASED by the owner 2026-10-09 ("write the work order for the reader process
isolation and do it"), to be built in the same session that wrote it.

**Where this came from.** The owner's overnight run of 2026-10-08/09 died after six and a half
hours. Windows' Application log records an APPCRASH of `pythonw.exe`: access violation
`0xC0000005` inside `mupdfcpp64.dll`, the PDF library, while reading PDFs inside zips. The
indexer's own words, from the owner: "the indexer should never crash, also the program should
always handle the errors". A native fault cannot be caught by any Python guard. The only
protection that works is to read in a process of one's own, so the fault takes that process
and the run records the file. Leasha already has that process (`read_process.py`, work order
0x section 5b) and it already does exactly this for the readers on its list. It did not help,
for three reasons this order removes:

1. **The switch ships off.** `INDEX_READ_PROCESSES` defaults to False, so no installed copy had
   any isolation at all.
2. **PDF is not on the list.** `PdfExtractor` was left in-process because its scanned-page half
   calls the OCR models, and the models, their memory and the GPU lock (`gpu_exclusive`, process-
   local) belong to the parent.
3. **A zip member is read in-process whatever its type.** The archive reader hands each member
   to `extract(temp)` on its own thread, so a PDF inside a zip never reached a reader process
   even when the switch was on and PDF had been listed.

What was checked before writing this, so nobody re-derives it:

- `read_process._serve` runs `extract()` and `chunk_document()` in the child and sends passages
  back; the parent's `ReaderProcess.read()` yields `(RemoteDocument, chunks)`. A child that dies
  costs one file (`ERR_READER_PROCESS_ENDED`), a fresh child starts for the next file; the file
  watchdog can kill a child (`kill_child`) for a time limit or Force skip. Tested in
  `tests/unit/test_read_process.py`, including a kill mid-file.
- `PdfExtractor.extract` (`app/extract/pdf.py`) reads the text layer page by page; on a wholly
  scanned PDF it calls `_ocr_pages`, which already declines in the text-first pass
  (`_pictures_held()`) and lets the caller raise `ERR_NO_TEXT_LAYER` - the row the pictures pass
  reads back (`Pipeline._is_deferred`). On a partly scanned PDF it calls `_ocr_specific_pages`
  for the empty pages. Both OCR entry points are the only reason PDF needs the parent.
- `archive._member` extracts a member to a temp file (`_extracted`) and `_read_one` runs
  `extract(temp)` over it, wrapping each document with the archive's path, `inside_archive`,
  `member` and `member_size`. Nested archives recurse on the same thread.
- 2026-10-09's `ERR_FILE_CRASHED_READER` (the in-hand notes) is the floor: a file the process
  dies on costs one restart and is never read again. This order is the ceiling: the process does
  not die.

## 1. The switch and the list

> **2026-10-09:** built. `config.index_read_processes`, the registry default and `run_setup` all say on;
> a `.env` line still turns it off (`test_reader_processes_are_on_unless_switched_off`).

- [x] **1a** `INDEX_READ_PROCESSES` defaults to **on**. The registry help says what it costs (one
      more Python process per reader thread, about 100-200 MB each while a run is going) and what
      it buys (a file that crashes its reader is skipped, not the run). The control stays, so it
      can be switched off; `.env` values already written keep their meaning.
      *Acceptance:* a fresh `.env` with no `INDEX_READ_PROCESSES` line resolves to reader
      processes on; `INDEX_READ_PROCESSES=false` still turns them off.
> **2026-10-09:** built. `base.in_reader_process()`/`set_reader_process()`; `read_process.main` sets it;
> `_ocr_pages` and `_ocr_specific_pages` decline in a child; `PdfExtractor` is on the list. A child
> reading a blank PDF raises `ERR_NO_TEXT_LAYER` and never touches OCR (`test_reader_process_isolation.py`).

- [x] **1b** `PdfExtractor` joins `PROCESS_READERS`. **In a reader process the PDF reader never
      OCRs**: `_ocr_pages` and `_ocr_specific_pages` decline when `in_reader_process()` is true,
      exactly as `_ocr_pages` already declines in the text-first pass, so a scanned PDF raises
      `ERR_NO_TEXT_LAYER` and the pictures pass reads it back in the parent as today, and a
      partly scanned PDF carries its "pages without text" warning. The child never loads an OCR
      model. `in_reader_process()` is a function in `app/extract/base.py` set by
      `read_process.main` in the child, so a Layer 2 reader knows its role without importing
      Layer 3.
      *Acceptance:* `reads_in_process(Path("report.pdf"))` is true; a PDF read through a reader
      process yields the same passages as in-process; with OCR monkeypatched to raise, the
      child's read of a scanned PDF raises `ERR_NO_TEXT_LAYER` and never calls it.

## 2. Archive members

> **2026-10-09:** built. `archive.set_member_reader`/`member_reader` (a thread-local), `_read_one`
> reads through it when installed; `Pipeline._member_reader_for` installs it beside the thread's reader
> and both teardown paths clear it.

- [x] **2a** The archive reader stays in-process (it hands pictures to OCR and streams a zip of
      zips on one thread), but **each member whose reader is on the list is read by the
      thread's reader process**. `app/extract/archive.py` gains a thread-local hook,
      `set_member_reader(fn)` / `member_reader()`, that the pipeline's extraction thread sets to
      its `ReaderProcess` when it has one and clears when the thread ends. `_read_one` uses it
      when `reads_in_process(temp)`; otherwise `extract(temp)` as today. Layer 2 does not import
      Layer 3: the pipeline installs a callable.
> **2026-10-09:** built. The request carries a sixth field, `"chunks"` or `"raw"`; a five-field
> request still means chunks. In raw mode the child sends the pickled `Document`; `read_raw` yields it.

- [x] **2b** `ReaderProcess.read_raw(path)`: the raw-document protocol. The child sends each
      document's `text` and `segments` (`("rawdoc", key, source_kind, meta, warnings, text,
      segments)`) instead of passages, because `_read_one` re-wraps the document and the
      pipeline chunks it afterwards. The request carries a mode flag; everything else - the
      sequence numbers, the frames, the kill, `ERR_READER_PROCESS_ENDED` - is shared.
> **2026-10-09:** built and tested with a real child killed on the second of three members: the
> other two are indexed, the second is recorded by name with `ERR_READER_PROCESS_ENDED`, and the
> thread's reader started a second child.

- [x] **2c** A member that kills the child costs that member: `_read_one` catches the
      `AppErrorException` as it does today and records it by name under the archive's row with
      the member named (`_by_name`), the thread gets a fresh child for the next member, and the
      rest of the archive is read.
      *Acceptance:* a zip holding a `.txt`, a member whose reader calls `os._exit(3)` inside the
      child, and another `.txt`: the run finishes, two members are indexed, one is recorded with
      `ERR_READER_PROCESS_ENDED` naming it, `stats.indexed` and the archive's row are right.

## 3. What this order does not do

- **Rendering scanned pages for OCR still happens in the parent** (the pictures pass, PyMuPDF
  `get_pixmap`). That half is the one native exposure left after this order. Phase 2, not
  ordered: the child renders and sends pixmaps over the pipe; the parent OCRs them.
- **Mail attachments** are read by `app/extract/mail_attachments.py` from bytes already in
  memory, not from a temp file handed to `extract()`; their PDFs stay in-process. Noted, not
  changed: the attachment path is the message reader's, which is on the list already when the
  mailbox is `.mbox`/`.eml`/`.msg`, and in the parent for `.pst` (Outlook, libpff).
- **The converter route** (LibreOffice) already runs out of process by construction.

## 4. Records

> **2026-10-09:** done, in the 1.0.3 release records.

- [x] **4a** `HANDOFF.md` entry; `CHANGELOG.md` under `[Unreleased]` in the product voice;
      `docs/TROUBLESHOOTING.md`'s `ERR_READER_PROCESS_ENDED` and settings rows; the user guide's
      Settings table; `docs/ORDER_REGISTER.md` row; this order's checkboxes ticked with dated
      notes, never reworded.
