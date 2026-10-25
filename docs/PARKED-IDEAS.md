# Parked ideas

**Doc version:** 1.8 · **Updated:** 2026-10-25 · **Applies to:** app v1.1.0

> *Note, 5 October 2026:* Leasha moved from PyQt6 to **PySide6 6.11.0** (Qt's own binding, LGPL-3.0) under order `202626270238`, released by the owner that day. The Qt underneath is the same 6.11, so the window looks and behaves as before. Where this document says PyQt6, read PySide6; `pyqtSignal` is `Signal`, and `sip` is `shiboken6` (through `app/ui/qtsip.py`). The text below is left as written.

Ideas the owner has approved in discussion but **not ordered**. Nothing here may be
started, and nothing here may become a work order without the owner asking for one.

This file exists so that approved-but-unordered thinking has somewhere to live other
than a conversation nobody can find again. Everything that *has* been ordered is in
`docs/ORDER_REGISTER.md`; if an item below turns up in an order, delete it from here
rather than keeping two copies.

**Provenance.** Recovered on 2026-08-30 from a 3,635-word assistant memory note that
had accumulated across the design days of 2026-08-27 to 2026-08-29. The great bulk of
that note had already been written into orders `202626270326` and `202626270508`
through `202626270602`; what follows is the residue that had not.

---

## 1. The late-night round, 2026-08-29

Discussed, not decided. Recorded verbatim in substance.

**Catalogue-book export.** A printable map of every source and where it is — "digital
inheritance". A report over tables that already exist. The use case is a will, not a
backup.

**Space report.** pHash and content hash across sources, producing duplicate GB and,
more usefully, the inverse: *this exists nowhere else, and it is on the failing
drive*. A backup conscience rather than a cleanup tool.

**Life-timeline browsing.** "June 2015" as a destination rather than a filter — all
media of a period, browsable. Distinct from the timeline strip on results, which is a
navigation aid.

**Handwriting OCR.** A further rung on the ladder, when the models are good enough.
Letters and journals are the most emotionally valuable documents in a family archive
and currently the most invisible to search.

---

## 2. Launch personas

**Genealogists.** Era hints and the Photo Tagger were built for them without meaning
to be. An evangelist community that documents its own tools.

**Trades and small firms.** Folder-per-job already works today. What is missing is
screenshots in their language, not features.

## 3. Canned demonstrations

Three that show the product without a rehearsed corpus: finding a licence key from
2019; "descale the coffee machine"; the insurance pair — a receipt and a photograph of
the television, found together.

---

## 4. The anti-idea, recorded so it stays refused

> **2026-10-12 (owner) - an answer card on the Search page; the finder is still the headline.** Order 1r (`docs/WORKORDER-smarter-answers.md`, D3), released "do all of them": a question typed into Search gets a short answer above its results, every sentence checked against the passage it cites, with numbered sources that open the files. The results are drawn first and never wait for it, a word or a name gets no card, and Settings > Search switches it off. The refusal below still holds - the answer is a card above the finder, not the finder replaced. The text below is left as written.

**Chat or RAG over the index as the headline.** Pressure for this will arrive, and
more so if the repository is ever opened. The scope-change order already cancelled it
and was right to: *a finder with sources beats an answerer that is sometimes wrong*.
The Chat tab order (`202626270611`, HELD) is deliberately a receipts-first design and
deliberately queued after the finder is finished.

The refusal is already written into the docs. This entry exists so that nobody
re-derives it under pressure and mistakes the pressure for a new argument.

---

## 5. Held for the owner to collate

The owner's instruction of 2026-08-28 stands: hold these, do not order anything from
this collection unprompted. He will collate.

---

## 6. Leasha on macOS — parked 2026-09-13, with the census that sizes it

> **2026-09-27 - partly unparked by the owner.** Order 0x
> (`WORKORDER-overhaul-and-mac-ready.md`) makes every line it writes or moves work on macOS,
> ahead of the Windows release, for its own scope only. The hardware-specific rows below
> (CoreML, the hardware probe, Offline Media drives, hotkey/selection, packaging and
> signing, live mailboxes) stay parked, listed in that order's §P. Everything below is kept
> as written.

Asked as a scope question, answered with a count rather than a feeling, and parked
rather than ordered. **Nothing here may be started.**

**The coupling is smaller than it feels.** `app/` is 203 files and 79,726 lines. Of
those, **12 files ask what platform they are on**, **2 import pywin32/COM**
(`extract/email_files.py`, `extract/email_pst.py`), **1 shells out to PowerShell**
(`core/compute_profile.py`, for the DXGI adapter probe) and **1 touches the registry**
(`core/deeplink.py`). Thirteen further files mention PowerShell and are false
positives — they *recognise* `.ps1` as a file type, which is content, not coupling.
All 92 UI files and 32,235 lines of PyQt6 are portable as they stand.

| Tier | What | Size | Note |
|---|---|---|---|
| Portable | storage, search, index, most extractors, all of `app/ui/` | ~75,000 lines | Free |
| Mechanical | `single_instance.py` 210, `winfs.py` 90, `deeplink.py` 288, `tray.py` 240, config path defaults | ~900 lines | Named mutex → `flock`; registry URL handler → `Info.plist`; `D:\Leasha\Data` → `~/Library/Application Support` |
| Redesign | `ui/hotkey.py` 310, `ui/selection.py` 202 | ~500 lines | Both need macOS Accessibility permission — a prompt to grant and a support burden that does not exist on Windows |
| New hardware story | `core/compute_profile.py` 561 | ~300 lines | DirectML is Windows-only. macOS means the CoreML provider, a `system_profiler` adapter probe, and re-measuring every tuning rate |
| **The real work** | `email_pst.py` 748, `pst_libpff.py` 424, `email_files.py` 340 | 1,796 lines | Design, not porting — see below |
| Packaging | `install.ps1` 855, `run-install.cmd`, `doctor.py`'s Windows checks | ~1,200 lines | A `.app` bundle, an Apple Developer ID, notarisation |

**Mail is the whole of the difficulty.** `pywin32` MAPI does not exist on macOS, and
Outlook for Mac keeps no `.pst` — it has its own store and exports `.olm`. Apple Mail
is `.emlx` files. Reading a `.pst` *file* someone copied across would still work
(`libpff` builds there, `extract-msg` is pure Python), but "index the mailbox open on
this machine" has to be designed from nothing, and mail is one of the two headline
corpora. **The scope decision comes before any estimate is worth having.**

**The dependency wheels are not a blocker — checked against PyPI on 2026-09-13, at the
exact pinned versions, CPython 3.12 macOS arm64:**

| Package | Pinned | macOS arm64 |
|---|---|---|
| PyQt6 | 6.11.0 | yes (universal2) |
| lancedb | 0.37.1 | yes |
| pymupdf | 1.28.2 | yes |
| pillow | 12.3.0 | yes |
| scipy | 1.18.1 | yes (macosx_12_0) |
| PyWavelets | 1.9.0 | yes |
| psutil | 7.1.3 | yes |
| ezdxf | 1.4.2 | yes (universal2) |
| onnxruntime | 1.24.4 / 1.30.0 | yes (macosx_14_0) |
| fastembed, rapidocr-onnxruntime, extract-msg, imagehash | — | pure Python |
| **onnxruntime-directml** | 1.24.4 | **none, and there never will be** |

Two caveats on that table. `scipy` wants macOS 12 and `onnxruntime` macOS 14, which
sets the floor for a supported OS version. And whether the macOS `onnxruntime` wheel
actually offers `CoreMLExecutionProvider` was **not** verified — the wheel exists; its
provider list was not opened. **(UNCONFIRMED)**

**Magnitude, flagged as judgement rather than measurement:** 6-10 weeks to a first
macOS release with parity on files, photos and code, and mail reduced to "point me at
a `.pst` or `.olm` file". Full mail parity is a separate epoch. The permanent cost is
the part worth weighing before the one-off cost: a second CI target, a second
packaging and signing chain, every Qt behaviour re-verified on a platform the owner
does not run daily, and non-negotiable #7 (the `.ps1` BOM rule) becoming half-relevant
while a new set of platform rules appears beside it.

**Prerequisite if it is ever ordered:** the Windows release has to be finished first.
"Working version first" applies with more force here than to any refactor — a second
platform doubles the surface of an application that has not yet shipped on its first.

## 7. An Outlook contacts reader - parked 2026-10-13

The owner, 2026-10-13: "for the outlook contacts reader put it on a to do list". Approved
to remember, **not ordered**.

**What it would be.** Order 1s section 5b asked whether Outlook contact items
(`IPM.Contact`) can feed enrichment. Its finding (2026-10-12): neither PST reader looks at
an item's message class - `IPM`, `message_class` and `MessageClass` appear nowhere in
`app/` - so a contact item in an Outlook data file is indexed, if at all, as a plain
message (its notes as the body, no sender) and its email, phone, company and address
fields are never read. The "Outlook contacts" enrichment switch was taken out for that
reason, since it switched nothing.

**What an order would cover.** A contact-item reader in both PST routes
(`pst_libpff._to_document` and the Outlook COM route `_com_item`), read-only like every
other reader (non-negotiable 10); the items kept as contacts rather than messages; and
then an enrichment source matching them to Leasha's contacts the way the vCard source
(`app/contacts/enrich/vcards.py`) does, with the item and its archive as the receipt.
Whether contact items should also be imported into the contact book outright is a
separate question for the same order.

## 8. The mouse's back and forward buttons, caught for the window alone - parked 2026-10-19

From the UX review of 2026-10-14 (`docs/REVIEW-2026-10-14-ux-architecture.md`, IA-10). The owner,
2026-10-19: "park them and record it" - this one parked, the other three follow-ups built.

**What is there now.** `NavigationController` installs an event filter on the whole
application (`app/ui/controllers/navigation_controller.py`, `app.installEventFilter`) to catch the
mouse's back and forward buttons anywhere in the window. It bails out at once for every other
event, but it is a Python call for each event Qt delivers - about 4,400 during one page switch,
profiled on the laptop (~6 ms) - and a window that is closed but not destroyed keeps its filter.

**The alternative, and why it waits.** A filter on the window's own `QWindow` would see each mouse
event once, and only that window's. But `qtbot.mouseClick` on a widget delivers straight to the
widget, past the `QWindow`, so the tests that prove Back and Forward would no longer see them, and
real XButton delivery on Windows cannot be checked from a session (UNVERIFIED). Nothing is wrong
today; it is a cost measured in milliseconds. Worth doing only with a real mouse to check it, and
a test that sends the event the way Windows does.