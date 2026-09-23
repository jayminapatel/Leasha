# Work order (Backend): the skip that says OCR does not exist

**Doc version:** 1.0 · **Updated:** 2026-09-21 · **Applies to:** app v0.3.3
**Thread:** Backend
**Status:** DRAFT — raised 2026-09-21, not authorised. Nobody may start it.

## Context

`ERR_NO_TEXT_LAYER` tells the user that OCR is not a thing this application
does. It has been doing OCR for some time.

The exact string, `app/core/errors.py:554-562`:

> Scanned documents and photographs hold text as pixels, not characters, so
> there is nothing to index without OCR - which V2 does not do. The file is
> left in place and counted, so it can be found again if OCR is added later.

Against that, in the same tree:

- `app/index/pipeline.py:170` — `OCR_MODES = ("both", "text", "images")`.
- `app/index/pipeline.py:1803` — `_ocr_gate`, which holds a file for the
  images pass rather than settling it.
- `app/core/errors.py:310` — `ERR_OCR_HELD`, *"Held for the images pass:
  '{path}' is a picture of text."*
- `app/core/errors.py:198` — `ERR_OCR_UNAVAILABLE`, whose `action_payload`
  installs `rapidocr-onnxruntime`.
- `app/cli/index.py:448` — `_ocr_mode`, reading `INDEX_OCR_MODE` and
  `INDEX_OCR_PASS`.

**This is not a new discovery, and that is the point.** `app/extract/pdf.py:17-23`
already records the contradiction, in the past tense, as something that was
found and half-fixed:

> **And it can now be read, if the time is worth spending.** That skip message
> said "there is nothing to index without OCR - which V2 does not do", while
> the OCR engine sat loaded in the same process reading `.png` files. Both
> halves were true: OCR was wired to image *extensions*, and nothing here ever
> called it.

The PDF path was fixed. The sentence that provoked the fix was never touched,
so it is still what the user reads. `app/extract/pdf.py:141-145` carries the
same account again, naming a real run that produced dozens of these against
ISA-95 standards and PI Server manuals.

**Why it matters beyond tidiness.** The advice is not merely out of date, it
is actively wrong in the direction that stops someone acting. A user with
10,000 scanned PDFs reads "OCR — which V2 does not do" and concludes the
corpus is unreachable. The correct next step, running the images pass, is the
one thing the message rules out.

## 1. The three instances

`ERR_NO_TEXT_LAYER` carries one meaning in the registry and is raised for at
least three different situations. Each needs its own sentence; one default
cannot serve all three, which is why `plaintext.py` already overrides it.

| # | Where | Situation | Today |
|---|---|---|---|
| 1 | `app/core/errors.py:554` | Registry default — a scan or photograph | Says OCR does not exist |
| 2 | `app/extract/office.py:546-548` | A slide whose words are inside a picture | *"V2 does not read words out of pictures"* — same stale claim |
| 3 | `app/extract/plaintext.py:218-232` | A binary file with a text extension | **Correct already.** Overrides the default, never mentions OCR |

Instance 3 is the working precedent and the shape to copy: raise with an
explicit `suggestion=` at the call site when the default does not fit.

**What must be true when this is done:**

- [ ] The registry default for `ERR_NO_TEXT_LAYER` no longer claims OCR is
      unavailable, and names the images pass as the way to read the file —
      the way `ERR_OCR_HELD` and `ERR_MEDIA_HELD` already do.
- [ ] `app/extract/office.py`'s override no longer says *"V2 does not read
      words out of pictures"*.
- [ ] `app/extract/plaintext.py`'s binary override is left exactly as it is.

## 2. The wording rule applies, and it constrains the fix

Non-negotiable: **existing text is not reworded; corrections go in a dated
note above the item.** These are user-visible strings, which is the case the
rule exists for.

This order therefore does **not** authorise editing the sentences in place.
The owner decides which of these it is, and the item stays open until he does:

- [ ] **[FINALISE]** Does a factually false user-facing string count as a
      correction (new text, dated note above it recording what it said and
      why it changed) or as a reword (forbidden, leave it)?

The distinction is real. The rule was written to stop renaming a control
someone had learned. Nobody learned this sentence; they were misinformed by
it. But that is the owner's call, not this thread's, and guessing it is how a
rule gets quietly relaxed.

**Recommendation, for what it is worth:** treat it as a correction. A dated
note above the entry, quoting the old sentence in full, keeps the diff
honest and the history readable — which is what the rule is protecting.

## 3. The test that will go red, and must not be adjusted

`tests/unit/test_cli_extract.py:118-125`:

```python
def test_binary_file_advice_is_not_about_scanning(...):
    """ERR_NO_TEXT_LAYER's default suggestion mentions OCR, which would be
    nonsense for a renamed database. Wrong advice is worse than none."""
    _code, out, _ = run(capsys, *env, "extract", str(fixture_root / "plaintext/binary.log"))
    assert "binary" in out.lower()
    assert "OCR" not in out
```

This asserts the *absence* of OCR from the binary path. It passes today and
must keep passing: a new default that mentions the images pass is still wrong
for a renamed database.

- [ ] `test_binary_file_advice_is_not_about_scanning` still passes, by the
      override still being in place — not by weakening the assertion.

`tests/unit/test_cli_extract.py:97-101` asserts only that `FIX:` is present,
not what it says, so it is unaffected.

**Nothing in the suite asserts the text of the default suggestion.** That is
why this survived: the string is read by users and by no test. Consider
whether it should be — see §5.

## 4. The third meaning, unresolved

`app/index/pipeline.py:1790-1801` is explicit that the code means opposite
things depending on the pass:

> `ERR_NO_TEXT_LAYER` is the case that needs the mode: it is a settled answer
> during a text pass - nothing there can read a scanned page - and it is
> precisely the work during any pass that can run OCR.

So one code, one message, and two states that are the opposite of each other:
*settled* and *queued*. `ERR_OCR_HELD` exists for the queued case and is the
right answer where the gate fires, but a file skipped as `ERR_NO_TEXT_LAYER`
during an `ocr_mode="text"` run is re-queued by that same predicate and never
gets the held wording.

- [ ] Establish, by reading the code and not by reasoning about it, whether a
      user running a text pass over scanned PDFs sees `ERR_NO_TEXT_LAYER` or
      `ERR_OCR_HELD`. **(UNCONFIRMED — not traced for this order.)**
- [ ] If it is the former, decide whether that path should raise
      `ERR_OCR_HELD` instead, or whether the message needs to cover both.

**Related, and probably the same root.** `docs/REVIEW-2026-08-26.md:183-185`
raised M15 — a transient OCR engine-load failure latches `_engine_failed`
forever and every image after it is recorded `ERR_NO_TEXT_LAYER`, "a broken
engine misdiagnosed as thousands of blank photographs" — and recommended
`ERR_OCR_UNAVAILABLE`. That code now exists. Whether M15 was closed is not
established here.

- [ ] Check M15's current state before starting §4; if it is still open, the
      two want doing together.

## 5. Stopping it happening again

Four user-facing strings made a claim about what the application does. Three
were wrong for weeks after the behaviour changed, and the one place the
contradiction was noticed (`pdf.py`) fixed the code and left the sentence.

- [ ] A test that fails when a registry suggestion claims a capability the
      application has. The cheap version: no `_Spec.suggestion` in
      `ERROR_REGISTRY` may contain the literal `"V2 does not"`. It is narrow,
      but it is the exact sentence that shipped wrong three times, and a
      narrow test that fires beats a broad one nobody writes.

Grep for the phrase before writing it — `app/core/errors.py:558` and
`app/extract/office.py:548` are the two known hits and both must be gone for
this to pass.

## 6. Out of scope

Not in this order, and not to be started under it:

- Changing OCR behaviour, defaults, budgets or the ladder.
- Renaming `ERR_NO_TEXT_LAYER`. It is written to the `files` table as a skip
  code and read back by `--only-ocr`; renaming it is a migration.
- Any other registry entry's wording.

## Done means

Change + tests + suite green + committed by name.

Acceptance sentence: a person indexes a folder of scanned PDFs with a text
pass, reads the skip advice, and is told how to read those files — not that
this application cannot.

**Not startable as written.** The `[FINALISE]` in §2 gates §1 and §2; §4
carries an UNCONFIRMED that wants tracing first. Both are owner decisions.

## Verification note

Every line reference above was read in the tree at `37e7580` on 2026-09-21,
not recalled. Nothing here was run against the Windows venv — the suite
claims in §3 are read from the test source, not from a green run, and want
confirming there before the order is authorised.
