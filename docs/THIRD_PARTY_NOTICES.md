# Third-party notices

**Doc version:** 1.4 · **Updated:** 2026-10-07 · **Applies to:** app v0.3.5

Leasha is MIT. This file lists the software it is built on whose licence asks something of a
copy that is handed to someone else: Qt for Python, which draws the window, and what Leasha
uses for video and audio. Each entry says where its licence was read. It is not yet a full list
of every dependency in `requirements.txt`; these are here first because they are the ones with
obligations or an open question.

**How they reach a machine.** Run from source, Leasha fetches each with `pip` from PyPI, and
the models from Hugging Face when somebody presses Download (or ticks the installer's model
box). The Windows installer (`packaging/`, Leasha-Setup-<version>.exe) **carries the packages
inside it**. That installer is for the owner's own machines: no copy goes to anyone else (the
owner, 2026-10-04). Before one does, the obligations below have to be met and the x264/x265
question answered.

## The window: Qt for Python (PySide6)

| Package | Version | Licence | Where verified |
|---|---|---|---|
| `PySide6` (and its parts `PySide6_Essentials`, `PySide6_Addons`) | 6.11.0 | **LGPL-3.0-only** OR GPL-2.0-only OR GPL-3.0-only | `importlib.metadata`, `License-Expression`, 2026-10-05 |
| `shiboken6` | 6.11.0 | **LGPL-3.0-only** OR GPL-2.0-only OR GPL-3.0-only | the same, 2026-10-05 |
| The Qt 6.11 libraries inside those wheels | 6.11.0 | LGPL-3.0 (Qt's open-source licence, as offered with the wheels) | the wheels' licence expression above; Qt's own notices ship inside the wheels |

Leasha uses them under the **LGPL-3.0**, which allows Leasha itself to stay MIT. What the LGPL
asks of a copy handed to anyone else: the Qt libraries kept replaceable, their licence text
shipped, and their source offered. The installer's one-folder build ships the wheels' files
unmodified as separate libraries, which meets the first; the other two are to do before any
copy leaves the owner. (Leasha used PyQt6 until 5 October 2026. PyQt6 is GPL-3.0 only, so a
distributed build would have had to be GPL.)

## Video and audio

| Package | Version | Licence | Where verified |
|---|---|---|---|
| `av` (PyAV) | 18.1.0 | BSD-3-Clause | `pip show av`, 2026-09-20 (package metadata `License-Expression`) |
| FFmpeg shared libraries bundled in the `av` wheel (`av.libs`) | libavutil 60.26 | **LGPL version 3 or later** | `avutil_license()` called through `ctypes` on the wheel's own `avutil-*.dll`, 2026-09-20; kept true by `tests/unit/test_media.py::test_the_bundled_ffmpeg_still_reports_lgpl` |
| `libx264`, `libx265` (DLLs in the same `av.libs`) | 165 / - | **GPL** | file listing of `venv\Lib\site-packages\av.libs`; see below |
| `onnxruntime` | 1.24.4 | MIT | pinned in `requirements.txt`; runs every model Leasha runs itself |
| `tokenizers`, `huggingface-hub` (they arrive with `fastembed`) | - | Apache-2.0 | package metadata classifiers, 2026-10-06 |

Speech runs Whisper on ONNX Runtime inside Leasha (`app/ort/whisper.py`). The audio it hears is
decoded by PyAV in `app/extract/media_tools.py` (`decode_audio`), which also decodes video
keyframes. The models Leasha downloads on request, and their licences, are listed in
`app/ort/catalogue.json`: Florence-2-base (MIT), Whisper tiny to large-v3 (MIT),
Qwen2.5-1.5B-Instruct (Apache-2.0), Gemma 3 1B (Gemma terms) and Llama 3.2 1B (Llama 3.2
Community Licence).

## The one open question: x264 and x265 inside the PyAV wheel

The wheel's FFmpeg configure line (`avutil_configuration()`) includes `--enable-libx264` and
`--enable-libx265` and does **not** include `--enable-gpl`, and it ships the two encoder DLLs.
FFmpeg normally refuses to link GPL encoders without `--enable-gpl`; the packagers' position is
theirs, and it has not been independently checked here.

What is true of Leasha:

* It **only decodes.** `app/extract/media_tools.py` opens containers, decodes video keyframes
  and decodes audio. No encoder is opened by any Leasha code
  (`tests/unit/test_media.py::test_no_module_encodes_media` fails if one is).
* **Run from source, it does not redistribute the wheel**: `pip` fetches it.
* **The Windows installer does carry it** when PyAV is installed on the machine that builds it
  (`packaging/leasha.spec` includes it, as on the owner's laptop), DLLs and all.

**Decision (owner's delegate, 2026-09-20, "video audio do"), and what it now means:** Leasha
stays MIT. Before any installer is handed to anyone else: the FFmpeg LGPL notice and source
offer, the DLLs kept as replaceable shared libraries (they are, in the one-folder build), and the
x264/x265 licence question answered - or a decode-only build of `av` substituted, or video and
audio left out of that build.

## Why there is no ffmpeg program

The first build ran `ffmpeg` and `ffprobe` as subprocesses so that FFmpeg's licence would be
mere aggregation. On 2026-09-20 that was reversed, because the separation bought nothing: audio
cannot be decoded for speech without FFmpeg's libraries, so they are in-process on every machine
that transcribes. The measurement that made the choice, and the decision, are in
`app/extract/media_tools.py` and in the dated notes of
`docs/WORKORDER-202626270515-video-audio-DRAFT.md`.
