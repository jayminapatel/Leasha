# Third-party notices

**Doc version:** 1.1 · **Updated:** 2026-10-01 · **Applies to:** app v0.3.3

Leasha is MIT. This file lists what it depends on for **video and audio** (work order
`202626270515`), each package's licence, and where that was read. It is not yet a full list of
every dependency in `requirements.txt` - that list is a Layer 9 (distribution) job; the media
packages are here first because one of them raises a question the others do not.

**Leasha does not redistribute any of these.** Every one is a `pip` dependency the person (or
the installer) fetches from PyPI or Hugging Face. The obligations below start only if the
virtual environment, or a model, is ever *shipped* with the application - which Layer 9 has not
decided.

## Video and audio

| Package | Version | Licence | Where verified |
|---|---|---|---|
| `av` (PyAV) | 18.1.0 | BSD-3-Clause | `pip show av`, 2026-09-20 (package metadata `License-Expression`) |
| FFmpeg shared libraries bundled in the `av` wheel (`av.libs`) | libavutil 60.26 | **LGPL version 3 or later** | `avutil_license()` called through `ctypes` on the wheel's own `avutil-*.dll`, 2026-09-20; kept true by `tests/unit/test_media.py::test_the_bundled_ffmpeg_still_reports_lgpl` |
| `libx264`, `libx265` (DLLs in the same `av.libs`) | 165 / - | **GPL** | file listing of `venv\Lib\site-packages\av.libs`; see below |
| `faster-whisper` | 1.2.1 | MIT | `pip show faster-whisper`, 2026-09-20 |
| `ctranslate2` | 4.8.2 | MIT | `pip show ctranslate2`, 2026-09-20 |
| Speech model `Systran/faster-whisper-base` | `ebe41f70` | MIT | model card metadata, read from Hugging Face on 2026-09-20 (`license: mit`) |

> *Note, 1 October 2026:* since 29 September 2026 speech runs Whisper on ONNX Runtime (`app/ort/whisper.py`,
> `onnx-community/whisper-*` exports, MIT); `faster-whisper`, `ctranslate2` and
> `Systran/faster-whisper-base` in the table above are no longer used (`requirements.txt` says so).
> `tokenizers` and `huggingface-hub` now arrive with `fastembed`, and `onnxruntime` is pinned directly.
> `av` is still needed: `app/ort/whisper.py` decodes audio through PyAV. The models Leasha downloads
> on request are listed with their licences in `app/ort/catalogue.json`: Florence-2-base (MIT),
> Whisper (MIT), Qwen2.5-1.5B-Instruct (Apache-2.0), Gemma 3 1B (Gemma terms) and Llama 3.2 1B
> (Llama 3.2 Community Licence).

`tokenizers` and `huggingface-hub` (both Apache-2.0) and `onnxruntime` (MIT) arrive with
`faster-whisper`; their licences were read from the same metadata on the same day.

## The one open question: x264 and x265 inside the PyAV wheel

The wheel's FFmpeg configure line (`avutil_configuration()`) includes `--enable-libx264` and
`--enable-libx265` and does **not** include `--enable-gpl`, and it ships the two encoder DLLs.
FFmpeg normally refuses to link GPL encoders without `--enable-gpl`; the packagers' position is
theirs, and it has not been independently checked here.

What is true of Leasha:

* It **only decodes.** `app/extract/media_tools.py` opens containers and decodes video
  keyframes; `faster-whisper` decodes audio. No encoder is opened by any Leasha code
  (`tests/unit/test_media.py::test_no_module_encodes_media` fails if one is).
* It **does not redistribute** the wheel. The user's `pip` fetches it.

**Decision (owner's delegate, 2026-09-20, "video audio do"):** Leasha stays MIT and treats the
`av` wheel as a pip dependency it does not redistribute. **If the venv is ever shipped (Layer
9), this must be resolved first:** the FFmpeg LGPL notice and source offer, the DLLs kept as
replaceable shared libraries, and the x264/x265 licence question answered (or a decode-only
build of `av` substituted).

## Why there is no ffmpeg program

The first build ran `ffmpeg` and `ffprobe` as subprocesses so that FFmpeg's licence would be
mere aggregation. On 2026-09-20 that was reversed, because the separation bought nothing:
`faster-whisper` cannot decode audio without `av`, so FFmpeg's libraries are in-process on
every machine that transcribes. The measurement that made the choice, and the decision, are in
`app/extract/media_tools.py` and in the dated notes of
`docs/WORKORDER-202626270515-video-audio-DRAFT.md`.
