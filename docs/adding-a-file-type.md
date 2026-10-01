# Adding a File Type

**Doc version:** 1.1 · **Updated:** 2026-10-01 · **Applies to:** app v0.3.3

> **The wizard does most of this for you.** Settings → File types → **Add file type…**
> asks the question below, then writes the route, the converter block, or the whole
> reader module with its registration and pin. This document is what it is doing, and
> the reference for the one part it cannot do: the parsing itself.

How to make the indexer read a format it does not read today. Three routes, and the
first question decides which one you need:

> **Can something that already exists read the bytes?**

| Answer | Route | Effort | Where |
|---|---|---|---|
| Yes, an extractor we ship already handles this kind of content | **Tier 1** | one line, no restart of anything but the app | Settings, or `extractors.toml` |
| No, but an external program can convert it to something we read | **Tier 2** | a few lines, plus one code edit if the binary is new | `extractors.toml` + `converter.py` |
| No — it needs real parsing, probably a library | **Tier 3** | ~40 lines in a new module | `app/extract/` |

Config stops at Tier 2 deliberately. A configuration format that can express arbitrary
parsing is a programming language with no debugger, no type checker and no tests —
strictly worse than the Python it set out to replace.

---

## Tier 1 — route it at an extractor that already exists

For anything that is really plain text under a different name (`.ino`, `.cfm`, a config
format, a language nobody listed), or any content one of our readers already handles.

**In the app:** Settings → File types → **Add file type…**, enter the extension, pick the
reader. The dropdown is the live registry, so it cannot offer something that is not there.

**Or by hand,** in `<DATA_PATH>\extractors.toml`:

```toml
[extensions]
".ino"  = { extractor = "plaintext" }
".cfm"  = { extractor = "plaintext", max_bytes = 5242880 }
```

> *Note, 1 October 2026:* the registry also provides `archive`, `audio`, `video`, `doc`, `ppt`, `xls`, `rtf`,
> `epub`, `mobi`, `mbox`, `emlx`, `olm`, `iwork`, `publisher` and `raw`. `leasha formats --all` lists
> them live.

Valid reader names come from the registry: `plaintext`, `pdf`, `docx`, `xlsx`, `pptx`,
`odf`, `ocr`, `visio`, `project`, `cad`, `eml`, `msg`, `pst`, `cloudstub`. A name nothing
provides is rejected **when the app starts**, naming the offending line — not three hours
into a run, on one file.

Never edit `config\extractors.toml` (the packaged file) for your own machine: it is
replaced on upgrade. The user file is merged over it, so your choices survive and new
defaults still arrive.

---

## Tier 2 — convert it with an external program

For dead formats where a converter already exists and writing a parser would be absurd.

```toml
[converters.".nsf"]
command   = ["xstexporter", "--export", "{input}", "--outdir", "{outdir}"]
produces  = "{stem}.txt"
then      = "plaintext"
timeout_s = 300
enabled   = true
```

`{input}`, `{outdir}` and `{stem}` are substituted. `produces` names the file the command
is expected to leave in `{outdir}`; `then` is the registered extractor that reads it.

> *Note, 1 October 2026:* the allow-list is now `soffice`, `libreoffice`, `libreoffice-python`, `xstexporter`,
> `tesseract`, `dwg2dxf`, `ODAFileConverter` and `dwg2SVG`; `pandoc` is **not** on it, and `ffmpeg` and
> `ffprobe` are left off deliberately. The code is the authority.

**The binary must be on the allow-list** in `app/extract/converter.py` — currently
`soffice`, `libreoffice`, `pandoc`, `xstexporter`, `tesseract`. Anything else is refused
with `ERR_CONVERTER_BLOCKED` *before* it is resolved or executed. Adding one is a
deliberate commit with a diff and a reviewer; a config file that can name any executable
is a way to run anything.

Converters ship **disabled** because the binary may not be installed. Settings shows
which binaries were found and `doctor.py` reports the rest.

---

## Tier 3 — a new parser, and probably a new library

**Use the wizard**: Settings → File types → Add file type… → *Write a new reader*. Give it
the extension, a reader name, the Python import name and the pip package. It shows you
exactly what it will write, then creates the module, inserts the import into
`app/extract/__init__.py`, pins the package in `requirements.txt`, and installs the
library. You write the parsing where the `TODO` is and restart.

It refuses, with a reason, anything that would not work: a name already taken, an
extension another reader claims, a package named with no import name (they are often
different — `python-docx` imports as `docx`). Nothing is overwritten, ever.

The rest of this section is what it generates, for when you would rather do it by hand.
Three files change. Nothing else.

### 1. `app/extract/<yourformat>.py`

```python
from app.core.errors import raise_error
from app.core.format_health import Requirement
from app.extract.base import Document, DocumentBuilder, register


class CadExtractor:
    name = "cad"                                   # the name config will use
    extensions = frozenset({".dxf", ".dwg"})
    reads_externally = False                       # True only for Outlook-style access

    #: Declared, so Settings and doctor can say "CAD files are name-only because
    #: ezdxf is missing" instead of leaving somebody to infer it from nothing.
    requires = (
        Requirement("ezdxf", "ezdxf", provides="text inside drawings", hard=True),
    )

    def supports(self, path):
        return path.suffix.lower() in self.extensions

    def extract(self, path):
        try:
            import ezdxf                           # LAZY — never at module import
        except ImportError:
            raise_error(
                "ERR_UNSUPPORTED_TYPE", "extract.cad",
                path=str(path), ext=path.suffix.lower(),
                suggestion="Reading CAD drawings needs ezdxf: "
                           "venv\\Scripts\\pip install ezdxf",
            )

        builder = DocumentBuilder(path)
        for number, layout in enumerate(ezdxf.readfile(str(path)).layouts, start=1):
            builder.add(_text_of(layout), page=number,
                        label=layout.name, prefix_label=True)
        yield builder.build()


register(CadExtractor())
```

### 2. `app/extract/__init__.py`

```python
from app.extract import cad as cad  # noqa: F401,E402
```

Without this the module is never imported, so `register()` never runs, so the extension
is unsupported and nothing says why.

### 3. `requirements.txt`

Pin it, and check it publishes a Windows wheel — a package that needs a C++ toolchain
turns a one-command install into a support call.

That is all. The extension now works with no TOML entry at all: config is an override,
not an allow-list. Users can still switch it off or cap it:

```toml
".dwg" = { extractor = "cad", enabled = false }
```

---

## The four things that bite

**Lazy-import the library inside `extract()`.** A top-level import makes the whole
application fail to start when the package is missing, instead of one file type reporting
a fix.

**Declare `requires`.** It is what makes the format appear correctly in Settings and in
`doctor.py`. Without it a missing library is invisible until somebody searches for text
they know is in a file and finds nothing. `hard=True` means *nothing* can be read without
it (red, "Cannot read"); `hard=False` means it still works but does less (amber,
"Limited").

**Failure is a value, not an exception.** A corrupt file in a 100GB run is Tuesday.
Unreadable → `raise_error(...)` with a suggestion; degraded but usable →
`builder.warn(...)` and index it anyway. Never let an exception escape: one bad file must
not end the run.

**Empty output is not success.** Yield nothing and the caller records
`ERR_NO_TEXT_LAYER` automatically — do not invent an empty document to avoid it.

---

## Checking your work

```powershell
venv\Scripts\python.exe doctor.py          # every format's real state, with fixes
venv\Scripts\python.exe -m app.cli formats # the routing table
venv\Scripts\python.exe -m pytest tests\unit\test_format_health.py -q
```

In the app: **Settings → File types**. Your format should appear with status **Ready**.
If it says *Cannot read*, the row carries the command that fixes it — right-click to copy.
**Reset to defaults** deletes the user override file, restoring every shipped type,
reader and limit exactly as packaged.
