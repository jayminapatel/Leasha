# Work order: libraries first, converters only where none exists

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3
**Layer:** Backend - `app/extract/`, `config/extractors.toml`, `requirements.txt`

A new standing rule from the owner:

> Use libraries where you can, and only LibreOffice where it cannot.

Commit the working tree before starting.

---

## 1. The rule, and why the second half matters

**Every format is read by a Python library if one exists. An external converter is the
fallback, never the first choice, and each one must justify why no library will do.**

This is not preference. A library is in-process, has a pinned version, ships a wheel, works on
a machine with nothing else installed, and fails in a way the error contract can describe. A
converter is a subprocess: it needs a separate install, it can be missing, it can hang, it
needs a timeout and a temp directory, it serialises on a user profile, and it turns one file
into two disk writes.

The current state was arrived at honestly - the Tier 2 converter mechanism was built first
because it unlocked a dozen formats at once - but "it unlocked a dozen formats" is not the same
as "a converter is the right way to read those formats".

**The reduction is real, not cosmetic.** Nine formats currently route to `soffice`. Four of
them have libraries, verified on PyPI on 2026-08-25.

## 2. Where things stand

| Format | Today | Library available? |
|---|---|---|
| `.doc` | soffice | **No.** `olefile` gives the OLE2 container, not the document |
| `.ppt` | soffice | **No** |
| `.pub` | soffice | **No.** Conversion quality is poor anyway |
| `.wpd` | soffice | **No** |
| `.pages`, `.key` | soffice | **No.** Modern iWork is protobuf-in-zip |
| `.xls` | soffice | **Yes** - `xlrd` 2.0.2, pure-Python wheel |
| `.rtf` | soffice | **Yes** - `striprtf` 0.0.33, pure-Python wheel |
| `.numbers` | soffice | **Yes** - `numbers-parser` 4.19.0, pure-Python wheel |
| `.epub` | pandoc | **Yes** - `ebooklib` 0.20, pure-Python wheel, or stdlib |
| `.fb2` | pandoc | **Yes** - it is plain XML; stdlib `ElementTree` |
| `.dwg` | dwg2dxf | No, and out of scope - not LibreOffice |

`xlrd` deserves a note: **since 2.0 it reads `.xls` only**, having deliberately dropped
`.xlsx`. That is exactly the split wanted here - `openpyxl` keeps `.xlsx`, `xlrd` takes the
legacy binary, and neither overlaps.

## 3. What to build

Four extractors, in this order. Each ends with a green suite.

**3.1 `.fb2` - stdlib, no dependency.** FictionBook is a single XML document. Parse with
`xml.etree.ElementTree`, take `<body>` text, keep `<title-info>` for metadata. Same shape as
the ODF reader already written. **Do this first**: it proves the pattern with nothing to
install.

**3.2 `.rtf` - `striprtf`.** One call, returns text.

**3.3 `.xls` - `xlrd`.** Mirror what `office.py` already does for `.xlsx`: per-sheet cell text
with sheet names, honouring the existing `MAX_SHEET_ROWS` and `MAX_SHEET_COLUMNS` caps so a
1998 spreadsheet with 60,000 rows behaves like a modern one.

**3.4 `.epub` - `ebooklib`, or stdlib.** EPUB is a zip of XHTML with a manifest. `ebooklib`
handles EPUB2 and EPUB3; a stdlib reader is perhaps sixty lines and adds no dependency.
**Try stdlib first** and only take the dependency if the manifest handling proves fiddly - the
same call that was made for ODF, and it went the right way.

**`.numbers` - decide, do not assume.** `numbers-parser` reads Apple Numbers, but it is a
larger dependency for a format most corpora contain none of. Check whether the corpus has any
`.numbers` files at all before adding it. If none: leave the converter and record that.

Then **remove the corresponding `[converters]` entries** from `config/extractors.toml`, and add
each new pin to `requirements.txt` with the verification note the file's conventions require.

## 4. Making the rule stick

The settings registry's exemption pattern worked; use it again. In `app/extract/converter.py`
or alongside the config:

```python
#: Why each format still needs an external converter. A format with a usable
#: library must not appear here - see docs/WORKORDER-libraries-before-converters.md.
#: "Nobody has written one" is a reason. "The converter was easier" is not.
CONVERTER_JUSTIFIED: dict[str, str] = {
    ".doc":  "no pure-Python reader for OLE2 Word; olefile gives the container only",
    ".ppt":  "no pure-Python reader for OLE2 PowerPoint",
    ".pub":  "no reader exists in any language worth depending on",
    ".wpd":  "WordPerfect; no maintained Python reader",
    ".pages": "modern iWork is protobuf inside a zip; no reader",
    ".key":  "modern iWork is protobuf inside a zip; no reader",
    ".dwg":  "binary CAD; converted to DXF by dwg2dxf, then read by cad.py",
}
```

**Tests:**

- [ ] Every `[converters]` entry in the shipped config has an entry in `CONVERTER_JUSTIFIED`.
      **A new converter cannot be added without stating why no library will do.**
- [ ] Every justification is substantive - the same length check the settings exemptions use.
- [ ] No format has both a converter entry and a registered library extractor. Two routes to
      one format is how a file gets read differently depending on config.
- [ ] `.xls`, `.rtf`, `.fb2` and `.epub` no longer appear in `[converters]`.
- [ ] Each new extractor: a healthy fixture yields text; a corrupt one yields
      `ERR_FILE_CORRUPT` as `SKIP_CONTINUE` and the run continues.
- [ ] `.xls` honours the sheet caps, and a 1998 workbook with 60,000 rows does not exhaust
      memory.
- [ ] `xlrd` is never handed an `.xlsx` - it will refuse, and the refusal must not reach the
      user as a traceback.
- [ ] `format_health.py` reports the four formats as ready **without LibreOffice installed** -
      run the check on a machine, or with the binary hidden from PATH.

## 4a. Considered and rejected: Office automation via pywin32

**Decided 2026-08-25 by the owner: LibreOffice remains the only converter for Office formats.
Do not build a COM path.** Recorded here so the question is not reopened without new evidence.

`pywin32==312` is already pinned (`requirements.txt` line 26, for Outlook MAPI), so
`win32com.client.Dispatch("Word.Application")` would have cost no new dependency. That is the
whole of the case for it, and it is weaker than it sounds:

- **It is a COM bridge, not a format reader.** It drives the Word you have installed, so it does
  not remove an external requirement - it **swaps LibreOffice for Microsoft Office**.
- **Microsoft does not support unattended Office automation** (KB257757). Office assumes an
  interactive desktop and, on an unexpected error, *prompts with a dialog* - which on an
  invisible instance mid-index is a hang, not an error. One `.doc` with a missing font stalls
  the run.
- **Word modifies a document by opening it** - field updates, recent-files, conversion prompts -
  which collides with the read-only non-negotiable.
- **Macro-enabled `.doc` files run `AutoOpen` on load.** LibreOffice will not run VBA by default.
- **Its advantage is formatting fidelity, and the output here is plain text for an index.**
  Layout, styles and images are discarded either way, so the thing COM does better is the thing
  being thrown away.

The one place COM would genuinely have won is `.pub`, whose LibreOffice import is poor. Not
enough to justify a subprocess-kill timeout, PID tracking and a forced macro-security setting
for a format that is rare in most corpora.

**What would reopen this:** a corpus with enough `.pub` or enough `.doc`-that-LibreOffice-garbles
to make the extraction quality, not the install, the binding constraint. Measure before
arguing.

## 5. What this changes for the person installing

Before: `.xls` and `.rtf` need LibreOffice. Those are not exotic - a fifteen-year archive is
full of them, and `.rtf` in particular turns up wherever anything ever pasted between
applications.

After: **LibreOffice is needed only for `.doc`, `.ppt` and a handful of genuinely dead
formats.** Still worth installing for a long archive, but no longer the difference between
reading half your spreadsheets and none of them.

Update `doctor.py` and the Settings status text to say which formats actually depend on it,
rather than the current blanket "`.doc`, `.xls`, `.ppt` and friends".

## 6. The rule for a new format, from here

1. Is there a maintained Python library with a wheel? **Use it.**
2. Is the format a documented container - zip, XML, JSON? **Read it with the standard
   library.** ODF, `.fb2` and Google Drive stubs all went this way and none needed a
   dependency.
3. Only if neither: a converter, with its reason written into `CONVERTER_JUSTIFIED`.

Step 2 is the one people skip. `odfpy` was rejected for being sdist-only and the ODF reader
turned out to be about forty lines of `zipfile` and `ElementTree`.

## 7. Recorded

- **Libraries first, converters as the justified exception.** In-process, pinned, wheel-shipped,
  no separate install, and failures the error contract can describe.
- **A converter must say why no library will do**, enforced by a test, because otherwise the
  easy path silently becomes the default one.
- **`xlrd` 2.0+ reads `.xls` only** - it dropped `.xlsx` deliberately, which makes the split
  with `openpyxl` clean rather than overlapping.
- **Try the standard library before taking a dependency** on a container format.
- **`.numbers` is a decision, not an assumption** - check whether the corpus has any before
  adding a library for it.
- **`.dwg` is out of scope**: it uses `dwg2dxf`, not LibreOffice, and no library replaces it.
- **Office COM was considered and rejected**, 2026-08-25. Free in dependency terms, but it
  substitutes one external requirement for another, Microsoft does not support it unattended,
  and its advantage is formatting fidelity - which a plain-text index discards anyway. See 4a
  for what would reopen it.
- **LibreOffice is the single converter for Office formats.** One fallback path, not two, is
  itself the point: two routes to `.doc` means a file reads differently depending on the
  machine.
