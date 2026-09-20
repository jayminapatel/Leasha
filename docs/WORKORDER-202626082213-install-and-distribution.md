# Work order (DRAFT - to be finalised): install and distribution

**Doc version:** 0.2 · **Updated:** 2026-09-20 · **Applies to:** app v0.3.3
**Created:** 2026-08-26 22:13 · **Layer:** L9 - packaging, `install.ps1`, a new `packaging/`

**Thread:** the single merged thread

**Decisions made 2026-09-20, on the owner's delegation - see the dated note at the end of this
file.** All five [FINALISE] questions and the signing question now have an answer, so the gate
in the paragraph below no longer holds. The paragraph is left as written.

**Status: DRAFT.** Three decisions are made and recorded in §1. Five are open and marked
**[FINALISE]** in §7 - none of them can be settled from the code, and each changes what gets
built. **Do not start §4 until §7 is answered.**

Getting Leasha onto a machine that has never had Python, for a user who will not read
anything.

---

## 1. Decisions already taken

| | Decision | Consequence |
|---|---|---|
| **Unsigned** | No code-signing certificate | Every user sees SmartScreen's *"Windows protected your PC"* and must click **More info -> Run anyway**. This is a real cost and it is accepted deliberately |
| **winget** | Distribute through `microsoft/winget-pkgs` | `winget install Leasha`. Verified 2026-08-26: the submission policy gates on installer *format* (MSIX, MSI, APPX, .exe), not on licence, so a private repository is no obstacle. Microsoft reserves the right to refuse any submission |
| **Models on install** | The installer downloads the embedding and rerank models | The user is already waiting and a progress bar is expected. First run is then offline and instant, which is what "fully local" should feel like |

**On staying unsigned.** Free OSS signing exists (SignPath Foundation) but requires a public
repository under a recognised licence, and this one is private. Paid signing is about $10/month
and - per Microsoft's own guidance - still does not remove the first-download SmartScreen
prompt, which accrues with reputation rather than with a certificate. So unsigned is not much
worse than the cheap paid option, and the decision can be revisited without rework.

**But say so out loud.** This application reads a person's entire document archive. A tool that
asks for that and then trips a security warning has to explain itself. The release notes and
the download page must state, in one sentence, that it is unsigned, why, and that everything
stays on the machine.

## 2. What makes this harder than a normal Python app

Verified against `requirements.txt` on 2026-08-26. 22 dependencies; five complicate freezing.

| Dependency | Why |
|---|---|
| `PyQt6==6.11.0` | ~150MB of Qt DLLs, and plugins that go missing at *runtime* rather than at build time |
| `lancedb==0.37.1` | Native Rust extension plus `pyarrow`; hidden imports PyInstaller will not find alone |
| `fastembed==0.8.0` | Bundles `onnxruntime`, another native blob, and is what downloads the models |
| `pymupdf==1.28.2` | Native, well-behaved |
| `pywin32==312` | COM for Outlook MAPI; needs post-install registration that freezing skips |

Expect **400-600MB** installed before models. That is normal for this stack.

Three further complications specific to Leasha:

* **Optional external binaries.** `ALLOWED_BINARIES` in `converter.py` names `soffice`,
  `libreoffice`, `dwg2dxf`, `tesseract`, `xstexporter`, `ODAFileConverter`. None can be
  bundled - LibreOffice alone is 400MB with its own installer and licence. See §5.
* **`required_free_gb = 150`.** The installer must check it and say what it is for, or a user
  with a full SSD discovers the problem three hours into their first index run.
* **`doctor.py` already exists** and is the readiness check. It should run at the end of the
  install rather than being a command nobody knows about.

## 3. What stays as it is

`install.ps1` (651 lines) is the **developer** path: venv, requirements, `.env`, `doctor.py`.
It is not replaced and not deprecated. Two audiences, two installers, and conflating them is
how the developer path acquires a GUI nobody wanted.

## 4. What to build

### 4.1 The build

```
packaging/
  leasha.spec          PyInstaller, one-folder
  installer.iss        Inno Setup
  build.ps1            spec -> dist -> installer, one command
  fetch-models.ps1     used by the installer, and testable on its own
```

**One-folder, not one-file.** One-file unpacks 500MB to `%TEMP%` on every launch: seconds of
startup and an antivirus scan each time, against a stated budget of *first search under 3s*.

**Inno Setup, not MSI.** Free, scriptable, per-user install without admin rights, and it can
ask the `DATA_PATH` question the existing installer already asks.

### 4.2 The install flow

1. Welcome, stating plainly: local-only, nothing leaves the machine, unsigned.
2. **Where to put the index** - `DATA_PATH`. Default `%LOCALAPPDATA%\Leasha\Data`.
   Check free space against `required_free_gb` and say what it is for.
3. **Which folders to index** - optional, skippable. It can be done in the window.
4. **Download the models** (~220MB) with a progress bar, cancel, and retry. On failure: say so,
   finish the install, and let the app fetch them on first search. **A failed model download
   must not fail the installation.**
5. Optional extras - see §5.
6. Run `doctor.py`, show the result. A user who installs and then finds it does not work has no
   way to tell whose fault that is.

### 4.3 Uninstall

Removes the program. **Asks about the index separately**, defaulting to *keep*. It is derived
data and rebuilding it is hours; deleting it silently on uninstall is the kind of thing that
gets found out once.

## 5. LibreOffice, through winget

The 153 `ERR_CONVERTER_MISSING` failures observed on 2026-08-26 were all one missing binary.
Bundling LibreOffice is not possible; asking for it is.

A tickbox during install - *"Also read .doc, .ppt and other older Office formats (installs
LibreOffice, ~400MB)"* - running:

```
winget install --id TheDocumentFoundation.LibreOffice --silent
```

**Off by default, and it must never block.** winget may be absent, blocked by policy, or fail;
all three end with the box unticked and the install continuing. `app.cli formats` already
reports what is missing, so the failure has an answer.

## 6. Releasing

GitHub Releases holds the `.exe`. The winget manifest points at that URL and its SHA256. A
release is:

1. `build.ps1` produces the installer and prints its hash.
2. Attach it to a GitHub release tagged from `VERSION`.
3. `wingetcreate update` submits the manifest bump.

Manifests are reviewed by Windows Package Manager moderators; a submission with problems gets
seven days before the bot closes it.

## 7. To finalise - none of these can be answered from the code

**[FINALISE 1] PyInstaller, or `uv` plus an embedded Python?**
Freezing gives one self-contained artefact and the highest chance of working on a stranger's
machine; `uv` gives a ~60MB download and updates that are one command, at the cost of needing
the network at install time and having more that can go wrong. §4 assumes PyInstaller.
*Recommendation: freeze for the first release. Fewer ways to fail on a machine nobody can see.*

**[FINALISE 2] Per-user or per-machine?**
Per-user needs no admin - a real advantage in a corporate setting, and it dodges the UAC prompt
that follows the SmartScreen one. Per-machine is what IT departments expect.
*Recommendation: per-user by default, per-machine as an option.*

**[FINALISE 3] Where does the index go by default?**
`%LOCALAPPDATA%\Leasha\Data` is correct for Windows but sits on C:, and this thing wants 150GB.
The current install asks. Does the packaged one ask too, or default and let Settings move it?

**[FINALISE 4] How does it update?**
`winget upgrade` handles it if the manifest is maintained. Does the app also check and tell the
user? An application that reads a whole archive and never mentions an update is a security
posture, not an oversight - decide which.

**[FINALISE 5] What is the supported floor?**
Windows 10 21H2? 11 only? winget itself needs Windows 10 1809+ with App Installer. Every
answer changes what has to be tested.

## 8. Acceptance

| | Criterion |
|---|---|
| A1 | Installs and runs on a **clean Windows machine that has never had Python**. Not a development VM |
| A2 | First search works with the network disconnected after install |
| A3 | A blocked or failed model download still leaves a working install, with an honest message |
| A4 | Uninstall leaves the index unless the user says otherwise |
| A5 | `winget install Leasha` works from the published manifest |
| A6 | `winget upgrade` moves an installed copy from one version to the next without losing the index or the settings |
| A7 | The install completes without admin rights in the per-user mode |
| A8 | `doctor.py` prints READY at the end of the install, on that clean machine |
| A9 | The SmartScreen prompt is documented with a screenshot, so it is expected rather than alarming |

**A1 is the only one that matters and the only one that is hard.** Frozen applications fail by
silently picking up a DLL from the developer's PATH, and that failure appears only somewhere
else.

## 9. Sequencing

After the search-quality order. Today's evidence - an index run that found 8 files, embedding at
0.3/sec against a 4.4/sec reference, and search parsing repaired the same afternoon - says the
application is not yet ready for people who did not build it.

**Packaging something users can install is a week. Packaging something they keep using needs
the search to be good first.**

---

## Note appended 2026-08-27 — [FINALISE 2] and [FINALISE 3] partly answered

`WORKORDER-202626270257-privacy-defaults.md` settled where the index goes by
default, and that answer carries part of the per-user question with it:

* **[FINALISE 3]** the default is now `%LOCALAPPDATA%\Leasha`, chosen at
  install exactly as before. Any other path is still accepted.
* **[FINALISE 2]** per-account follows from it for the *index* - Windows' own
  ACLs on `%LOCALAPPDATA%` separate two accounts with no extra machinery. It
  does **not** settle where the *application* is installed, which is still open
  here.

An existing `.env` with a valid `DATA_PATH` is respected without a prompt, so
nothing about this changes an install that already exists.

This is a note, not an edit: the [FINALISE] items above are left exactly as the
owner wrote them.

---

## Note appended 2026-09-20 - the five [FINALISE] questions and the signing question, decided

The owner delegated these ("make a decision and inform"). Each follows the recommendation
this order already gave unless it says otherwise. A note, not an edit: §7 is left as written.

| | Decision | Why |
|---|---|---|
| **[FINALISE 1]** | **PyInstaller, one-folder, for the first release.** | Fewest ways to fail on a machine nobody can see. `uv` plus an embedded Python stays open for later, if download size ever matters more than certainty. |
| **[FINALISE 2]** | **Per-user by default, per-machine as an option.** | No admin prompt on top of SmartScreen's. Together with the 2026-08-27 note (the index is per-account through the ACLs on `%LOCALAPPDATA%`), the application and its data are then both per-account. |
| **[FINALISE 3]** | **The packaged installer asks, exactly as `install.ps1` does.** Default `%LOCALAPPDATA%\Leasha\Data`; it checks free space against the configured requirement (`REQUIRED_FREE_GB`, currently 300 on the owner's machine, not the 150 written in §2) and, if C: is short and another fixed drive is not, offers that drive by name. Settings can move it later. | A default that silently fills C: is the failure this order exists to prevent, and a prompt costs one screen. |
| **[FINALISE 4]** | **No update check inside the application.** Updates arrive through `winget upgrade` only, and the download page and README say so in one sentence. | Leasha's claim is that it is fully offline. An application that reads a whole archive and then phones a server on its own, even to ask a version number, breaks the claim it is sold on. Silence is the chosen posture, stated rather than accidental. |
| **[FINALISE 5]** | **Supported: Windows 11 and Windows 10 22H2.** Tested: Windows 11 only, until a second machine exists. | winget needs 1809+ with App Installer, so 22H2 is comfortably inside it; saying "tested" and "supported" apart keeps the claim honest. |
| **Signing** (the 'sixth decision', ORDER_REGISTER §5) | **Stay unsigned for the first release. Revisit only if the repository is made public.** | `LICENSE` is MIT, so the licence half of SignPath's condition is met, but SignPath Foundation also needs the codebase to be public and it is private. Publishing it is the owner's decision, not a packaging one. |

**A sequencing consequence.** The installed `PyQt6 6.11.0` declares `License-Expression:
GPL-3.0-only` in its own metadata, while `LICENSE` is MIT. The PySide6 order
(`WORKORDER-202626270238`) already records why this matters: handing a frozen PyQt6 build to
other people makes the combined work GPL. Nothing is distributed today, so nothing is wrong yet,
but that order was "deferred behind the working version" with no end date. **It now has one: it
is the first item of Layer 9 and must be done before the first packaged release, not after it.**

**Not started, and why.** §4 needs the PySide6 order first, and acceptance A1 (a clean Windows
machine that has never had Python) cannot be met on this machine.
