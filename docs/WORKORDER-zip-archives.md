# Work order: every file findable, and reading inside .zip

**Doc version:** 1.0 · **Updated:** 2026-08-25 · **Applies to:** app v0.3.3
**Layer:** L2 - `app/extract/`, `config/extractors.toml`, `app/core/settings_registry.py`

Asked: *"does the program search zipped files and its contents"*. It does not,
and not even by name.

Commit the working tree before starting.

---

## 1. Where it stands

| Format | Today |
|---|---|
| `.zip` `.7z` `.rar` `.tar` `.gz` `.tgz` `.cab` `.iso` `.jar` `.nupkg` | **Not walked at all** |
| `.pst` | Walked, and read as a container yielding many documents |
| `.docx` `.odt` `.epub` `.vsdx` | Walked - these *are* zips, read as one document each |

The walker skips any file whose suffix is not in `resolved_extensions()`:

```python
if path.suffix.lower() not in extensions:
    continue
```

So a `.zip` gets **no row at all**. It is not indexed by name, it is not in the
skip ledger, and nothing anywhere says it was passed over. It is invisible, which
is the worst of the three possible answers - worse than "indexed by name only",
because that at least tells the truth.

For a fifteen-year archive heading for 1.5TB this matters. A corpus of that age
is full of `project_backup_2014.zip`.

## 1a. Files search must list every file, not only the readable ones

**Added by the owner, and it is the larger half of this work order.** It also
makes the `.zip` gap a special case of a general one:

> *the files search should include all files, not just the ones we have read the
> content of - all files in the search folder.*

**Today the index records a file only if its extension is routed somewhere.**
The walk is:

```python
if path.suffix.lower() not in extensions:
    continue
```

So `backup.zip`, `holiday.mp4`, `installer.exe`, `photo.raw`, `archive.7z` and
every other unrouted type produce **no row, no name-index entry and no skip
ledger line**. From the Files tab they do not exist. Somebody who knows the file
is there concludes the index is broken, and they are not wrong to.

Note the contrast with a file that *is* routed but yields nothing - a `.vsd`,
for instance. That already works correctly and already says so:

> *"contains no text that can be extracted. The file is indexed by name and by
> its document properties, so it will still be found - but nothing inside it is
> searchable."*

That is exactly the right behaviour. It simply never applied to the types that
were never walked.

### What to change

- [ ] **The walker records every file it sees.** Extension routing decides
      whether the *contents* are read, not whether the file exists. A file with
      no extractor becomes a row with its name, path, size, mtime and extension,
      and `files_fts` gets its name so filename search finds it.
- [ ] **Extraction stays exactly as selective as it is now.** This must not
      turn into "try to read a 40GB ISO". The cost of a name-only row is one
      `stat()` the walk already does and one INSERT - nothing is opened.
- [ ] **A distinct status, not a failure.** `INDEXED` means content was read;
      these need something like `NAME_ONLY` so the two are never confused. A row
      with no chunks that claims to be INDEXED is exactly the bug that made
      `--force` necessary, and it must not be recreated deliberately.
- [ ] **The skip ledger says why**, grouped: *"18,402 files indexed by name only
      - no reader for .mp4, .exe, .iso"*, with the count per extension. That
      turns an invisible absence into a number somebody can act on, and it is
      how you discover that a corpus is 30% `.dwg`.
- [ ] **The Files tab shows them plainly.** They are ordinary rows; the only
      difference is that opening a preview says the contents were not read.
      Search results from content queries are unaffected - a file with no chunks
      cannot match a content search and does not need to.
- [ ] **Cheap to switch off.** Somebody indexing a media drive may not want two
      million video files in their index. One setting, defaulting to on, because
      *"where is that file"* is the most common question anybody asks a search
      tool.

### What this costs

One row per file rather than per readable file. On a corpus where most bytes are
media but most *files* are documents, the row count grows by perhaps a third; on
a photo archive it could multiply. The `files` table is small next to `chunks` -
no text, no vectors - so this is tens of bytes per file, not kilobytes.

**Worth measuring in the `scan` pass** (see the terabyte work order) before
switching it on across 1.5TB: the number that matters is how many unrouted files
exist, and nobody knows it yet.

### Why this comes first

It is a smaller change than reading inside archives, it needs no new dependency
and no new guards, and it delivers most of what the `.zip` question was really
asking - *"why can I not find my file"*. Ship it first, then decide how much of
section 3 the corpus justifies.

## 2. The shape already exists - do not invent a second one

**`.pst` solved this exact problem**: one file on disk producing thousands of
documents. Follow it rather than building a parallel mechanism.

- `files.source_kind` already has an `archive` value beside `file`,
  `pst_message` and `eml`.
- `IndexStats` already separates `indexed` from `unchanged_documents`, which is
  the number that makes re-reading a changed archive cheap.
- The pipeline already stores a container's children with a `key` and a
  per-document text digest rather than a file hash - see `pipeline.py` around
  `source_kind="archive"`.
- `Document.path` for a child already uses the `container/member` convention
  (`email_pst.py` writes `f"{message_key}/{name}"`).

A `.zip` member is a child with a path of `D:\a\backup.zip\reports\q3.docx`. The
UI, the skip ledger and the change detector all handle that today.

## 3. What to build

`app/extract/archive.py`, registering `.zip` and `.jar`/`.nupkg` (both are zips).

- [ ] Enumerate members with `zipfile.ZipFile.infolist()`.
- [ ] For each member whose extension has a registered extractor, extract it to
      a temp file and run the normal `extract()` path over it. **Reuse the
      extractor registry**; an archive reader that reimplements text extraction
      is a second, worse copy of Layer 2.
- [ ] Yield one `Document` per member, with `source_kind` set so the pipeline
      treats it as a container child.
- [ ] Members whose type has no extractor are recorded by name, not silently
      dropped: an archive of `.dwg` files should say what is in it.

`zipfile` is in the standard library, and `odf.py`, `ebook.py` and `diagrams.py`
already read zips safely. **No new dependency for `.zip`.**

**`.7z` and `.rar` need one, and are out of scope for the first pass.** `py7zr`
and `rarfile` (which shells out to `unrar`) are the candidates. Ship `.zip`
first, measure how many of the others actually exist in the corpus, and decide
then - non-negotiable 12 applies, and so does not taking a dependency for a
format nobody has.

## 4. The guards, and none of them are optional

Every one of these is a way an archive takes down a run that would otherwise
have finished.

### 4.1 Zip bombs

A 40KB file that expands to 5GB. This is not theoretical: it is a well-known
denial-of-service shape, and a 600GB corpus of unknown provenance will
eventually contain one by accident if not by malice.

- [ ] **Check `ZipInfo.file_size` before reading**, not after. The header
      carries the uncompressed size, so the gigabyte is refused without being
      allocated. `odf.py` already does exactly this - copy the reasoning.
- [ ] **Per-member ceiling**, defaulting to the existing `max_bytes` for that
      member's type. A `.txt` inside a zip gets the same cap as a `.txt` on disk.
- [ ] **Per-archive ceiling on total uncompressed bytes**, because a thousand
      members of 50MB each passes every per-member check and still costs 50GB.
- [ ] **Compression-ratio ceiling.** A member whose `file_size / compress_size`
      exceeds roughly 200:1 is refused on that basis alone, with its own skip
      code. Ordinary text compresses about 5:1; 1000:1 is a bomb and nothing
      else.
- [ ] Refusal is `ERR_ARCHIVE_TOO_LARGE` with `SKIP_CONTINUE`, naming the member
      and the number. **The run continues** - one bad archive must not end a
      five-day index.

### 4.2 Nesting

A zip inside a zip inside a zip. Without a limit an index run never finishes,
and with a naive limit a legitimate `nupkg` inside a `zip` stops working.

- [ ] **Depth limit, default 2**, counted from the file on disk. Depth 1 is the
      archive itself; depth 2 is an archive inside it. Deeper is recorded by name
      with `ERR_ARCHIVE_TOO_DEEP`.
- [ ] **A member that is a zip is not automatically recursed into.** It is
      recursed only if depth allows *and* the total-bytes budget for the outer
      archive has room left. The budget is shared, not per level.
- [ ] **Guard against self-reference.** A zip whose member is itself, and quines,
      exist. The depth limit covers this, which is why the limit is absolute
      rather than "keep going while it looks reasonable".

### 4.3 Encrypted members

- [ ] Detected from the flag bits, **before** attempting to read. An encrypted
      member is `ERR_ARCHIVE_ENCRYPTED` as `SKIP_CONTINUE`, indexed by name.
- [ ] **Not a failure and not silence.** The skip ledger groups these, so
      "412 files in encrypted archives" is one row with a plain explanation.
      Somebody with the password can decide whether to extract them by hand; the
      indexer does not prompt and does not guess.

### 4.4 Path traversal

- [ ] A member named `..\..\Windows\System32\evil.dll` must never be written
      outside the temp directory. **Resolve the target and confirm it is inside**
      before writing, rather than trusting the name - `zipfile.extract` sanitises
      in modern Python, but this code writes its own temp files and the check
      belongs where the write is.
- [ ] Absolute member paths and drive letters are refused the same way.

### 4.5 Change detection

- [ ] The archive's own row keeps mtime and size, as any file does. A changed
      mtime means re-reading members.
- [ ] **Each member gets a text digest**, exactly as PST messages do, so
      re-reading a changed archive re-writes only the members whose text moved.
      Reported as `unchanged_documents` - the number that makes a 2GB archive
      with one new file cheap rather than a full re-index of its contents.
- [ ] A member that has disappeared from the archive is deleted from the index
      when its container is re-read.

### 4.6 Cost, and the ability to say no

- [ ] **Off by default is wrong and on by default is dangerous**, so this is a
      setting with a real number attached: *"Read inside archives up to N MB"*,
      default something like 100MB. A 40GB backup zip is skipped by name with a
      message saying why and how to change it.
- [ ] Non-negotiable 11: it appears in Settings, not only in `extractors.toml`.
- [ ] Members are extracted to the system temp directory and **deleted
      immediately after reading**, one at a time. Never the whole archive at
      once: that is how a 20GB zip becomes 20GB of temp files on the index drive.
- [ ] Respect the resource governor between members, as the pipeline does
      between files. An archive is not an excuse to ignore the memory ceiling.

## 5. Tests

- [ ] A zip of three documents yields three documents, with the right paths.
- [ ] A nested zip is read at depth 2 and refused at depth 3, by code.
- [ ] **A zip bomb is refused without allocating its expansion** - assert on the
      ratio check, and use a real high-ratio fixture rather than a mock.
- [ ] The per-archive byte budget stops a thousand-member archive.
- [ ] An encrypted member is skipped with its own code and the others in the
      same archive are still read.
- [ ] A traversal name cannot write outside the temp directory.
- [ ] A member with no extractor is recorded by name rather than dropped.
- [ ] Re-indexing an archive with one changed member reports one indexed and the
      rest `unchanged_documents`.
- [ ] A corrupt or truncated zip is `ERR_FILE_CORRUPT`, `SKIP_CONTINUE`, and the
      run continues.
- [ ] `.zip` appears in `resolved_extensions()` - the guard that would have
      caught the whole tier-1/tier-2 walker bug.

## 6. Do this after the first full index

**Not because it is unimportant - because the cost is unknown.** The `scan`
command in `docs/WORKORDER-terabyte-scale.md` reports how many archives exist and
how large. If it turns out to be a handful of small ones, the cheap half of this
work order - index by name, flag as *"contents not read"* - is most of the value
for almost none of the effort, and can ship on its own.

Deciding before that number exists is guessing, and guessing at 1.5TB is
expensive.

## 7. Recorded

- **Every file in an indexed folder must be findable by name** (section 1a),
  whether or not anything can read it. Extension routing decides what is *read*,
  not what *exists*. This is the larger half of the work order and should ship
  first.
- **Invisible is the worst answer.** A `.zip` currently produces no row at all,
  so nothing tells anybody its contents were not read. Even name-only indexing
  would be an improvement.
- **Name-only is a status, not a failure.** `NAME_ONLY` beside `INDEXED`, so a
  row with no chunks never claims its contents were read - the confusion that
  made `--force` necessary in the first place.
- **The container shape already exists**, built for `.pst`: `source_kind`,
  per-document keys, per-document digests, `unchanged_documents`. Reuse it.
- **Every guard here corresponds to a way a run dies**: bombs, nesting,
  encryption, traversal, and temp-space exhaustion. None is hypothetical.
- **Check the header, not the read.** Uncompressed size and compression ratio are
  both known before a single byte is decompressed.
- **`.zip` needs no dependency; `.7z` and `.rar` do** - so they wait for evidence
  that the corpus contains them.
