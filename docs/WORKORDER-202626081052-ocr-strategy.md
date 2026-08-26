# Work order: OCR where it pays, and nowhere else

**Doc version:** 1.0 · **Updated:** 2026-08-26 · **Applies to:** app v0.3.3
**Created:** 2026-08-26 10:52 · **Layer:** L2/L3 - `app/extract/`, `app/index/pipeline.py`

Follows the run over `D:\SearchData` that produced dozens of lines reading
*"there is nothing to index without OCR - which V2 does not do"* immediately
after *"OCR engine loaded in 0.8s"*, and the question of whether pictures inside
Office documents should be read too.

Commit the working tree before starting.

---

## 1. The decision, and the number behind it

**OCR scanned PDFs. Do not OCR pictures embedded in Office documents.**

They look like one problem and they are not. The difference is **value density** -
how much searchable text you get per second of OCR.

A scanned manual is a document whose *entire* content is unreachable. `ISA 95
Part 1 October 2012.pdf` is two hundred pages of standard that no search can
touch. An embedded Office picture is almost always a logo, an icon, a screenshot
of a dialog, or a chart whose labels are already in the slide text beside it. A
deck with forty pictures has thirty-nine you do not want.

At the 3.6 seconds per page this project measured:

| Work | Volume | Cost |
|---|---|---|
| Scanned PDFs, 20-page budget | ~200 docs x 20 pages | **~4 hours** |
| The same PDFs, every page | ~200 docs x 300 pages | ~60 hours |
| Office embedded images | ~5,000 decks x 30 images | **~150 hours** |

Four hours to make a reference library searchable is an easy yes. A hundred and
fifty hours for company logos is an easy no.

**Every figure above is an estimate on a borrowed measurement.** 3.6s/page came
from different hardware and different scans, and nobody has counted how many
scanned PDFs this corpus holds. Section 3 exists to replace all of it with
measurement before anything is committed.

## 2. What already exists

Do not rebuild these.

- **`pdf.py` OCRs image-only PDFs**, on a page budget, as of commit `c89e2ce`.
  `LEASHA_PDF_OCR_PAGES=n` turns it on; 0 (the default) leaves the old
  `ERR_NO_TEXT_LAYER` behaviour exactly as it was.
- **The two-pass split exists**: `--skip-ocr` and `--only-ocr`, with `ocr_mode`
  on `PipelineConfig` and `OCR_MODES` in `pipeline.py`.
- **`reads_by_ocr(path)`** answers "is this file read by looking at it", asked of
  the resolved extractor rather than a hard-coded list.
- **`_warn_if_mostly_pictures`** already flags a picture-heavy `.pptx`. That
  warning is the artefact section 5 depends on.

## 3. Measure before committing anything

**One folder, timed. Nothing else in this document is worth doing first.**

`D:\SearchData\SE Backup\_JPDocs\MES\S88-S95` is roughly twenty scanned
standards - a real sample of the worst case.

```powershell
$env:LEASHA_PDF_OCR_PAGES=20
.\leasha index "D:\SearchData\SE Backup\_JPDocs\MES\S88-S95" --force
```

- [ ] **Time it.** Wall clock, and `ocr_seconds` from the document metadata.
      That replaces 3.6s/page with a number from this machine and these scans.
- [ ] **Search for something you know is inside one**, on page 3 and on page 30.
      This is the question the page budget turns on: does twenty pages make a
      manual findable, or only its cover?
- [ ] **Count the corpus.** How many PDFs have no text layer at all? The skip
      ledger already groups `ERR_NO_TEXT_LAYER`, so the text pass answers this
      for free - which is another reason it goes first.
- [ ] Record all three in this document before proceeding.

**If twenty pages is not enough, raise the budget rather than abandoning it.**
The failure mode to avoid is all-or-nothing: full OCR of every scan is the
sixty-hour column.

## 4. The order of the passes

- [ ] **Text pass first**, over everything: `--skip-ocr`. Search becomes useful
      in a day or two rather than a fortnight, and the skip ledger tells you what
      the corpus actually contains.
- [ ] **Then the OCR pass**, with the budget calibrated in section 3.
- [ ] **`--only-ocr` must retry `ERR_NO_TEXT_LAYER` rows**, not walk for image
      extensions. This is the open piece of wiring: whether a PDF needs OCR is
      **not knowable from its extension**, so `reads_by_ocr` is False for every
      PDF and the images pass currently narrows the walk to `.png`/`.jpg` and
      never revisits a scanned manual. The text pass records exactly the right
      rows; the OCR pass has to read them back rather than re-walking.
- [ ] While OCR is running, the progress line should say so - it is seconds per
      page, and a run that looks stalled gets killed.

## 5. Office pictures: measure, then almost certainly decline

- [ ] **List the affected documents rather than reading them.** The `.pptx`
      reader already warns when a deck is mostly pictures. Surface that as a
      count and a list - *"412 decks are mostly images"* - which is a five-minute
      change and the input to the decision.
- [ ] **Then decide with the number in hand.** Twenty decks: open them. Two
      thousand: no OCR strategy was going to help, and the honest answer is that
      those decks are findable by name and title only.
- [ ] **If it is ever built**, it needs a size floor - something like 50KB - so a
      thousand 4KB bullet graphics per deck are never rendered. Without that the
      cost is the 150-hour column and the yield is icons.
- [ ] `python-pptx` and `python-docx` both expose image parts as bytes, and
      `ocr_image` takes bytes, so the mechanism is the same shape as `pdf.py`.
      **The mechanism is not the hard part; the decision is.**

## 6. What must stay true

- [ ] **OCR text is labelled.** `read_by=ocr` in the document metadata, already
      set by `pdf.py`. OCR output carries errors a text layer does not, and a
      result nobody can account for is a result nobody trusts.
- [ ] **A capped read says what it read.** "OCR read the first 20 of 312 pages"
      is honest; silence is the same half-answer this project keeps finding.
- [ ] **One unreadable scan costs one file, never the run.** Five hours of work
      has already been lost to a crash in an optional integration.
- [ ] **Off by default stays off by default.** Turning OCR on globally converts
      an index run into an OCR run, which is exactly what the two passes exist
      to prevent.
- [ ] Non-negotiable 11: the page budget belongs in Settings as well as the
      environment variable, once it has a measured default.

## 7. Recorded

- **Value density decides this, not capability.** Both jobs are technically the
  same; a scanned manual is entirely unreachable text and an embedded picture is
  usually a logo.
- **A page budget beats a switch.** The first twenty pages of a manual are the
  title, contents and introduction - most of what makes it findable - for about
  a minute a document.
- **Every number here is borrowed until section 3 replaces it.** 3.6s/page is
  from other hardware, and the count of scanned PDFs in this corpus is unknown.
- **The open wiring is `--only-ocr` retrying skip rows**, because whether a PDF
  needs OCR cannot be known from its name.
- **The `.pptx` picture warning is already collecting the evidence** for the
  Office decision. Read it before building anything.

---

*Filename carries the creation time as `yyyyddmmhhnn` per the owner's
convention. Note that day-before-month does not sort chronologically within a
year - `202626081052` sorts before `202603091200` - so if these are ever listed
by name, `yyyymmddhhnn` would order them. Left as instructed.*
