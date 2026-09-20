# Extraction speed

**Doc version:** 1.0 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3

How long every file type takes to read, measured, and what was done about the slow
ones. The owner's instruction of 2026-09-20 was *"speed is important and this should
be for all types of files applicable"*: a format is read **in-process by a library
(or the standard library) wherever a sound route exists**, and the external
converter stays only as the fallback for a file the in-process reader will not vouch
for. Non-negotiable 12 (`docs/PROJECT_INSTRUCTIONS.md`) is the rule; this is the
evidence.

The table at the bottom is **generated** - `tools/extract_speed.py --samples DIR
--write-docs` rewrites only what sits between its two markers. Everything above it
is written by a person and survives a re-run.

## What changed, in one table

Wall milliseconds per file, medians. **Before** is the LibreOffice route measured by
hand once per type (a cold `soffice` start each time - the audit never loops it, see
"Method"); **after** is the in-process reader. The machine was at 100% CPU from other
work throughout, so wall figures are inflated; the CPU column in the generated table
is the read itself.

| Type | Before | After | Note |
|---|---|---|---|
| `.pub` | LibreOffice: **failed 8 of 8** (Publisher opens in Draw, `txt:Text` is a Writer filter), ~2.7 s each before failing | **3-6 ms**, recall 100% | `app/extract/publisher.py` |
| `.key` | LibreOffice: **failed 8 of 8** after 7.7-9 s each (Impress has no `txt:Text`) | **15-80 ms**, recall 99.0-99.5% | `app/extract/iwork.py`; 27 `.key` files on the owner's disk were unindexed |
| `.pages` | LibreOffice: **6.2 s** | **4-85 ms**, recall 100% | same reader |
| `.numbers` | LibreOffice: **13.5 s** | **7-32 ms**, recall 92-100% (labels and text 100%; cell *numbers* not read) | same reader |
| `.mobi` / `.azw` / `.azw3` | **not indexed at all** (no reader, no converter; 390 files on the disk) | **190-540 ms** for a whole novel | `app/extract/mobi.py` |
| `.docm`, `.dotx`, `.dotm` | **failed** (python-docx refuses the content type; 83 `.dotx`, 25 `.docm` on the disk) | read, 10-900 ms | `ooxml_fast` |
| `.docx` | python-docx: 38 / 72 / 131 / 347 ms on four real files | **9 / 29 / 27 / 216 ms**, 2-6x, same words | `app/extract/ooxml_fast.py` |
| `.pptx` | python-pptx: 5.8 / 4.6 / 13.4 / 3.2 / 0.8 / 8.5 s (loaded machine) | **1.9 / 1.6 / 0.34 / 0.7 / 0.29 / 1.8 s**, 3-40x, identical words | `app/extract/ooxml_pptx.py` |
| `.xlsx` / `.xlsm` | openpyxl: 87 / 68 / 258 / 499 ms, 1.1 / 3.8 s | **34 / 28 / 102 / 330 ms, 0.84 / 2.1 s**, ~2x, **byte-identical text and anchors** | `app/extract/ooxml_xlsx.py` |
| `.sxw` `.sxc` `.sxi` `.fodt` `.fods` ... | not indexed | read by the ODF reader | `odf.py`, tested on built files only |
| `.doc` `.ppt` | LibreOffice: 5.0-10.3 s cold (the converter agent's figure) | in-process (their readers) | not this work |
| `.dwg` | `dwg2dxf`: 0.1-2.6 s a file, a process each | **unchanged** - see below | |

## Proof against the converter

`tools/reader_recall.py EXT SAMPLES REFERENCE` reads each real file with the
registered extractor and prints recall and precision of *distinct words* against what
the converter wrote for the same file (LibreOffice run once per type, by hand;
`.pub` through PDF, `.key` through pptx, `.pages` through txt, `.numbers` through
csv).

| Type | Real files | Recall | Precision | Notes |
|---|---:|---:|---:|---|
| `.pub` | 8 | **100%** on all 8 | 97-100% | the longest UTF-16 run in `Quill/QuillSub/CONTENTS` |
| `.pages` | 3 (2 distinct) | **100%** | 90-100% | LibreOffice omits some tables the reader includes |
| `.numbers` | 2 | 92.4% / **100%** | 80% / 50% | LibreOffice's CSV is the *first sheet only*, which caps precision; the 7.6% missing are numeric tokens (dates, integers) that Numbers stores inline in cell tiles |
| `.key` | 5 with text (3 picture-only decks: both agree, no text) | **99.0-99.5%** | 99-100% | |
| `.docx` vs python-docx | 6 | 99.9-100% | 89-100% | extra words are text boxes and content controls python-docx never returned |
| `.pptx` vs python-pptx | 8 | **100%** | **100%** | |
| `.xlsx` vs openpyxl | 9 (+1 generated) | text, anchors, labels and warnings **identical** | | one workbook python's openpyxl path *crashed* on (a chart sheet) is now read |

A reader whose recall was poor for a class would not have shipped for that class.
None fell below the bar; `.numbers` figures are the honest exception and the file-type
row should say "cell numbers are not read".

**Bugs the proof found in my own first versions**, both fixed with a failing-first
test: `.numbers` kept only the first table's strings (every table numbers its list
from 1, and a dict keyed on the number dropped the rest - 74% recall, no error);
`.xlsx` stripped `x005F_` from inline strings, which openpyxl does only to the
shared-string table.

## What was *not* done, and why

| Type | Why not |
|---|---|
| `.dwg` | Proprietary and undocumented. `ezdxf` reads DXF only, by its own statement. LibreDWG's Python bindings are GPL (the project is MIT and `test_cad` forbids it). `dwg2dxf` stays, unchanged; its cost is a process per file, 0.1-2.6 s. |
| `.wpd`, `.wps`, `.hwp` | No file of these types exists on the measured disk, so there is nothing to prove a reader against, and a reader that has never met a real file is a guess. The `.wpd` converter stays as it was. |
| `.chm` | 74 files, unindexed today. Needs an LZX decompressor; no wheel exists. Not attempted in the time. |
| `.vsd`, `.mpp` | 2,738 and 650 files, indexed by name and properties only. The shape text is in an undocumented chunk tree; `mpxj` needs a JVM. Unchanged. |
| `.abw`, `.hwpx`, `.xps` | Trivial XML, but none exists on the disk to prove against. Not built. |
| pdf, rtf, xls, eml, msg, zip, exif | Measured and already fast (see the table: 4-80 ms CPU). Nothing changed. |

## Method

* **Samples**: real files copied from the owner's disk to a scratch folder
  (`SAMPLES/<ext>/`); types with no real file get a small generated one
  (`SAMPLES/generated/<ext>/`, marked `gen` in the table, a floor rather than a
  corpus figure).
* **Timing**: one untimed warm-up per type, then the **minimum of 3** per file; the
  row is the median across files. Wall time on a machine doing other work is
  contention plus the read, so **CPU time is reported beside it** (the read alone;
  it excludes child processes, so for `.dwg` wall time is the one to read).
* **Subprocess**: observed by counting `subprocess.Popen` during the read, not read
  from config.
* **LibreOffice is never looped by the audit.** A run of cold conversions crash-looped
  `soffice.bin` on the owner's machine on 2026-09-20 and produced a stream of error
  dialogs. `extract_speed.py` therefore makes LibreOffice look uninstalled for the
  whole audit (`_forbid_libreoffice`), so a file an in-process reader declines
  reports its error code instead of starting a converter, and `--baseline` refuses to
  run without `--allow-soffice`. Cold LibreOffice cost was measured by hand, once per
  type.

<!-- BEGIN MEASURED -->
Generated by `tools/extract_speed.py` - **do not edit by hand**; re-run it. Every figure is the minimum of the repeats for that file (noise only ever adds time), and the row shows the median across its files. `Spawns` means `subprocess.Popen` was called while reading, observed and not read from config. Measured on: Windows-11-10.0.26200-SP0, Python 3.12.10, 12 logical CPUs. Rows marked `gen` used a small generated sample (no real file of that type to hand), so their milliseconds are a floor, not a corpus figure.

**Slowest first.**

| Type | Reader | Route | Files | Sample KB (median) | wall ms / file (median) | CPU ms / file (median) | wall ms (max) | Spawns | Sample |
|---|---|---|---:|---:|---:|---:|---:|:-:|:-:|
| `.dwg` | converter | converter (dwg2dxf.EXE) | 6 | 546 | 4,778.5 | 2,484.4 | 13,833.1 | yes | real |
| `.docm` | docx | in-process | 2 | 6,774 | 2,308.8 | 1,859.4 | 2,310.2 | no | real |
| `.xlsm` | xlsx | in-process | 3 | 527 | 1,212.8 | 1,140.6 | 4,300.0 | no | real |
| `.vsdx` | visio | in-process | 4 | 114 | 1,167.2 | 703.1 | 2,627.5 | no | real |
| `.mobi` | mobi | in-process | 4 | 794 | 838.1 | 726.6 | 1,637.8 | no | real |
| `.mbox` | mbox | in-process | 1 | 116 | 425.9 | 359.4 | 425.9 | no | gen |
| `.pptm` | pptx | in-process | 2 | 3,508 | 313.7 | 296.9 | 516.8 | no | real |
| `.rtf` | rtf | in-process | 6 | 68 | 227.7 | 85.9 | 2,571.1 | no | real |
| `.pptx` | pptx | in-process | 6 | 1,402 | 191.6 | 164.1 | 890.5 | no | real |
| `.epub` | epub | in-process | 4 | 365 | 163.6 | 156.2 | 396.0 | no | real |
| `.pdf` | pdf | in-process | 6 | 332 | 136.2 | 78.1 | 819.3 | no | real |
| `.dxf` | cad | in-process | 1 | 57 | 131.8 | 78.1 | 131.8 | no | gen |
| `.xlsx` | xlsx | in-process | 6 | 73 | 125.5 | 125.0 | 1,847.6 | no | real |
| `.docx` | docx | in-process | 6 | 585 | 99.0 | 85.9 | 662.0 | no | real |
| `.eml` | eml | in-process | 4 | 91 | 60.2 | 54.7 | 62.5 | no | real |
| `.pages` | iwork | in-process | 3 | 2,958 | 55.9 | 31.2 | 85.1 | no | real |
| `.msg` | msg | in-process | 5 | 204 | 54.0 | 15.6 | 66.8 | no | real |
| `.key` | iwork | in-process | 8 | 6,146 | 51.9 | 31.2 | 112.4 | no | real |
| `.xls` | xls | in-process | 6 | 197 | 34.6 | 15.6 | 92.3 | no | real |
| `.numbers` | iwork | in-process | 2 | 952 | 22.6 | 7.8 | 33.4 | no | real |
| `.ppt` | ppt | in-process | 6 | 1,189 | 20.5 | 31.2 | 146.8 | no | real |
| `.ods` | odf | in-process | 1 | 76 | 19.7 | 15.6 | 19.7 | no | gen |
| `.dotx` | docx | in-process | 2 | 982 | 17.7 | 15.6 | 18.9 | no | real |
| `.mpp` | project | in-process | 4 | 422 | 10.8 | 0.0 | 12.1 | no | real |
| `.log` | plaintext | in-process | 1 | 204 | 10.7 | 0.0 | 10.7 | no | gen |
| `.doc` | doc | in-process | 6 | 164 | 7.8 | 0.0 | 10.2 | no | real |
| `.json` | plaintext | in-process | 1 | 44 | 7.7 | 0.0 | 7.7 | no | gen |
| `.csv` | plaintext | in-process | 1 | 104 | 7.5 | 0.0 | 7.5 | no | gen |
| `.pub` | publisher | in-process | 8 | 574 | 7.3 | 0.0 | 35.9 | no | real |
| `.txt` | plaintext | in-process | 1 | 23 | 6.9 | 0.0 | 6.9 | no | gen |
| `.py` | plaintext | in-process | 1 | 17 | 6.0 | 0.0 | 6.0 | no | gen |
| `.html` | plaintext | in-process | 1 | 10 | 5.1 | 0.0 | 5.1 | no | gen |
| `.vsd` | visio | in-process | 5 | 232 | 4.5 | 0.0 | 5.6 | no | real |
| `.xml` | plaintext | in-process | 1 | 11 | 4.1 | 0.0 | 4.1 | no | gen |
| `.md` | plaintext | in-process | 1 | 23 | 4.0 | 0.0 | 4.0 | no | gen |
| `.fb2` | fb2 | in-process | 1 | 9 | 2.7 | 0.0 | 2.7 | no | gen |
| `.gdoc` | cloudstub | in-process | 1 | 0 | 2.2 | 0.0 | 2.2 | no | gen |
| `.odt` | odf | in-process | 1 | 9 | 2.2 | 0.0 | 2.2 | no | gen |
| `.odp` | odf | in-process | 1 | 9 | 2.0 | 0.0 | 2.0 | no | gen |

## Files that raised

Expected for some: a raise is the correct answer for a file with no text or a damaged one.

| Type | File | Code |
|---|---|---|
| `.key` | real1_Keynote Icon Library (Mac Users Only).key | ERR_NO_TEXT_LAYER |
| `.key` | real2_Keynote Icon Library (Mac Users Only).key | ERR_NO_TEXT_LAYER |
| `.key` | real3_Keynote Icon Library (Mac Users Only).key | ERR_NO_TEXT_LAYER |
| `.ppt` | real1_Plant-Wide Information Software Challeng.ppt | ERR_NO_TEXT_LAYER |
| `.xls` | real2_2 & 4.xls | ERR_FILE_CORRUPT |
<!-- END MEASURED -->
