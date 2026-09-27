# Checking Leasha on a real Mac

**Doc version:** 1.4 · **Updated:** 2026-09-27 · **Applies to:** app v0.3.3

Nobody working on Leasha has a Mac. Order 0x (`WORKORDER-overhaul-and-mac-ready.md`)
writes every change to work on macOS as well as Windows, and checks it three ways that
do not need one: on Linux (which shares most of macOS's plumbing), with tests that
pretend to be a Mac, and on GitHub's `macos-14` runner. What none of those can do is
open the window on a real screen, grant a macOS permission, or read a real Apple Mail
folder. **This list is those things**, in the order worth doing them on a first session.

Every entry says what to run, what you should see, and which order item it proves. Tick
it, or write what happened underneath with the date. Anything marked
**(UNCONFIRMED on macOS)** in the code is listed here.

## Before you start

- A Mac with Apple Silicon (M1 or later) and **macOS 14 or later**. onnxruntime 1.24.4
  publishes no wheel for anything older, so the install stops on macOS 13.
- Python 3.12 (`brew install python@3.12`, or the python.org installer).
- The repository cloned anywhere, for example `~/SearchProject`.

## 1. Install

- [ ] **1.1** Create the environment and install:
      ```bash
      cd ~/SearchProject
      python3.12 -m venv venv
      venv/bin/python -m pip install -r requirements.txt -r requirements-dev.txt
      ```
      Expect: no error. pywin32 and pywinauto are skipped by their `sys_platform`
      markers (order 0x §0b).
- [ ] **1.2** `venv/bin/python doctor.py`. Expect: the "Windows platform" line passes and
      says mail from a running Outlook needs Windows (§0d). Write down every other line
      that fails; each is a Mac gap to order.

## 2. The suite

- [ ] **2.1** `QT_QPA_PLATFORM=offscreen venv/bin/python -m pytest tests -q -m "not jvm and not e2e and not slow"`.
      Compare the failures with the latest `test-macos` job summary on GitHub. They
      should match; anything that fails here but passes there is about this machine.
- [ ] **2.2** The same without `QT_QPA_PLATFORM=offscreen`, so Qt draws on the real
      screen. Note any test that passes offscreen but fails on screen.

## 3. The window

- [ ] **3.1** Start the app and check the first screen draws with the system font (SF Pro),
      the menu bar sits at the top of the screen (not inside the window), and
      *Leasha → Settings…* and *Leasha → Quit Leasha* are where macOS puts them
      (UI redesign §7a).
- [ ] **3.2** ⌘ shortcuts (Qt maps the code's `Ctrl+` to ⌘ on a Mac; `app/ui/shell.py`
      `_bind_shortcuts`): ⌘K and ⌘F focus search, ⌘, opens Settings, ⌘I the Indexing
      page, ⌘P Files, ⌘E Code, ⌘Q quits (UI redesign §2c).
- [ ] **3.3** **A known clash to decide on the day:** the Mail shortcut is `Ctrl+M`, which
      becomes ⌘M on a Mac - and ⌘M is macOS's own "minimise window" everywhere. Check
      which one wins. ⌘P (Files) is also Print by convention on a Mac. Windows keeps its
      shortcuts either way (Windows is platform one); a Mac-only alternative would be a
      new decision, not a change to the existing keys.
- [ ] **3.4** Light and dark mode both draw correctly; switch while the app is open.

## 4. Platform operations (order 0x §1)

- [ ] **4.1** Open a result: it opens in its default Mac app. **(UNCONFIRMED on macOS)**
- [ ] **4.2** "Show in folder": Finder opens with the file selected. **(UNCONFIRMED on macOS)**
- [ ] **4.3** With LibreOffice in `/Applications`, index a `.doc` that needs conversion;
      it converts. Same for Tesseract from Homebrew. **(UNCONFIRMED on macOS)**
- [ ] **4.4** With "Optimise iCloud storage" on, index a folder holding a file that is not
      downloaded. Leasha skips it and says so, and it stays not downloaded.
      **(UNCONFIRMED on macOS)**
- [ ] **4.5** Start an index and open Activity Monitor: the indexer runs at lowered
      priority and the machine stays responsive. **(UNCONFIRMED on macOS)**
- [ ] **4.6** With no `DATA_PATH` chosen, the index goes to
      `~/Library/Application Support/Leasha`. **(UNCONFIRMED on macOS)**

## 6. Mail files from a Mac (order 0x §8)

- [ ] **6.1 Apple Mail folder.** Give Terminal (or the Python that runs Leasha) Full Disk
      Access in System Settings → Privacy & Security, then add `~/Library/Mail/V10/` (or
      whichever `V*` exists) as a folder and index it. Expect: `.emlx` files indexed as mail;
      sender, recipients, subject and date match Mail.app for five sampled messages; a reply's
      quoted thread is stripped; a subject word finds the message.
- [ ] **6.2 Without Full Disk Access.** Revoke it and index again. Expect: the files are
      recorded as `ERR_FILE_LOCKED` skips - not a crash, not silence.
- [ ] **6.3 Partial messages.** Find a `*.partial.emlx` (an account set to download
      attachments "None" or "Recent"). Expect: its text is searchable, attachment names
      appear, and the run counts `ERR_MAIL_ATTACHMENTS_NOT_DOWNLOADED`.
- [ ] **6.4 `.emlxpart`.** Note whether any exist under `~/Library/Mail`; they are not read
      yet.
- [ ] **6.5 A real `.olm` export.** Outlook for Mac → File → Export (mail only). Run
      `unzip -l export.olm | head -50` and record the real member paths; open one
      `message_*.xml` and record the element names for subject, from, to, cc, sent time,
      body, HTML body, attachments, message id, in-reply-to and references. Compare them
      with the table in `app/extract/email_olm.py`'s docstring. **(UNCONFIRMED on macOS)**
- [ ] **6.6 Index the export.** Expect: message count roughly matches the exported folders;
      five sampled messages show the right sender, recipients, subject, date (check the time
      zone) and body; HTML-only mail reads as text; the folder name is shown.
- [ ] **6.7 Resume.** Stop the indexer part-way through the `.olm` and start again. Expect:
      it carries on near where it stopped, not at the first message, with no duplicates.
- [ ] **6.8 A big export.** Index an `.olm` over 2GB. Expect: it is read, not dropped by the
      file-size ceiling.

- [ ] **4.7** Open a video result at a moment in it (a search hit inside a transcript) with VLC
      and with mpv installed as Mac apps: does it start at that moment? The start-time arguments
      are the Windows ones. **(UNCONFIRMED on macOS)**
- [ ] **4.8** With VS Code in `/Applications`, "Open in editor" on a code result opens it.
      **(UNCONFIRMED on macOS)**

## 7. The Indexing page (order 0x §4)

- [ ] **7.1** Indexing → Status during a run: the bar glides between updates, and shows a moving
      block while the total is not known yet. **(UNCONFIRMED on macOS)**
- [ ] **7.2** Minimise to the Dock mid-run and restore: the bar is right at once, with no catch-up
      slide, and Activity Monitor shows no CPU from Leasha's animation while it was minimised.
      **(UNCONFIRMED on macOS:** whether a Dock minimise sends Qt a hide event; the code also
      checks on every step.)
- [ ] **7.3** Run log → Copy, then paste into TextEdit: every line starts with its HH:MM:SS time.
- [ ] **7.4** Tab through the Status shelf (turn on System Settings → Keyboard → Keyboard
      navigation first): filter, Copy, log, Start, Scan, Stop, Pause, Reset.

- [ ] **7.5** Index a large `.mbox` and a zip of documents: the page names the archive and "message
      n of m" / "member n of m", one line per reader, and "last activity" keeps moving.

## 5. Parked for a later order (hardware-specific; see order 0x §P)

Not expected to work yet. Note what you see, so each can be ordered from a fact.

- [ ] **5.1** `venv/bin/python -c "import onnxruntime as o; print(o.get_available_providers())"`.
      Does the list include `CoreMLExecutionProvider`?
- [ ] **5.2** Settings → Index Tuning: what hardware does it report (cores, GPU, disk)?
- [ ] **5.3** Plug in a USB drive: does Offline Media see it?
- [ ] **5.4** The global hotkey and "search the selected text": expected to say they are
      Windows features.

This list grows with every section of order 0x. Each new entry carries the item it
proves.
