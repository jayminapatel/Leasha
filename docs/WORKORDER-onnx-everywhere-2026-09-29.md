# Work order: every model inside Leasha on ONNX Runtime, Chat switchable to Ollama

**Doc version:** 1.1 · **Updated:** 2026-09-30 · **Applies to:** app v0.3.3
**Created:** 2026-09-29 · **Layer:** L2 (new package `app/ort/`), with L1/L5 wiring
**Thread:** the local laptop session of 2026-09-29 (order 1a's), branch `feat/onnx-everywhere`
**Status:** ACTIVE *(owner, 2026-09-29: "i want you to do this now all should be onnx be default ... and for chat it should be configurable to use ollama or Onnx... go for it do it all any decisions make them")*. Written after the work started, as the record of it: the owner authorised the work directly and asked for the decisions to be made, and they are all below.

## Why

Four of Leasha's six models already ran on ONNX Runtime (meaning search, re-ranking, CLIP, OCR).
Florence-2 photo tags ran on **torch** and Whisper speech on **CTranslate2**. On the owner's
laptop Windows **Smart App Control** blocked torch's unsigned `torch_global_deps.dll`
(Code Integrity events 3033/3077/3118, 2026-09-29), so photo tags stopped; it also blocked
`rawpy`. An administrator cannot add an exception - the only way round it is turning Smart App
Control off, which many people cannot do and cannot undo. ONNX Runtime is signed by Microsoft
and loads everywhere Leasha runs.

## Decisions (made in the session, as the owner asked)

- **D1 One runtime.** Plain `onnxruntime` (the pinned `onnxruntime-directml`), not Microsoft's
  separate ONNX Runtime GenAI: that is a second native library tied to its own onnxruntime
  version, and `doctor.py` insists on exactly one. Generation is a small loop of our own,
  `app/ort/generate.py`, reading the key/value-cache input names from each session.
- **D2 Models** from `onnx-community` on Hugging Face (the transformers.js exports):
  `Florence-2-base` (MIT, the same weights as before), `whisper-<size>` (MIT),
  `Qwen2.5-1.5B-Instruct` (Apache-2.0 - the same model Interpret used through Ollama;
  Llama 3.2's licence is awkward for a sold product).
- **D3 Where each graph runs** (`app/ort/session.py`), from measurement, not assumption:
  quantised graphs and decoders with a key/value cache **always run on the processor**. On
  DirectML the Florence-2 int8 graphs produced nonsense, and whisper-base's full-precision
  merged decoder returned NaN from its second step. Encoders and the vision graph may use the
  graphics card.
- **D4 `CHAT_ENGINE`** = `onnx` (default) | `ollama`, one choice for Chat, Interpret and
  Describe (`app/llm/engines.py`). The ONNX chat model is shared by Interpret and Chat - one
  1.5 GB load, not two. Describe on ONNX is Florence-2's `<MORE_DETAILED_CAPTION>`; the chat
  model cannot see pictures. The background caption trickle does not run on ONNX: Florence-2
  already writes each photo's AI description while indexing.
- **D5 Own error codes** for the model inside Leasha (`ERR_LOCAL_MODEL_MISSING`, `_FAILED`,
  `_TIMEOUT`): the Ollama ones say "Ollama is not running" and "ollama serve". Callers that
  branched on `ERR_OLLAMA_TIMEOUT` treat the local one the same.
- **D6 Nothing downloads while indexing.** Models are fetched by Download buttons only
  (`model_fetch` kinds `onnx` and `speech`), in a child process so Stop is real, over plain
  HTTPS with a 30 s stall limit - the Hugging Face "xet" transfer stalled for good on the
  owner's link.
- **D7 False user-facing sentences are corrected, not kept.** Several strings told people to
  install faster-whisper, which nothing uses now. The open question in `no-text-layer-advice`
  (whether fixing a false string is a correction or a forbidden reword) was answered for this
  order as *a correction*, with a dated comment beside each keeping the old wording. The owner
  may reverse this.
- **D8 Settings.** The engine choice is always visible in the Chat box. With ONNX, Ollama's own
  controls (role model lists, Look again, its Download) are hidden and **Settings contacts
  nothing**; the Chat box, Interpret's box and the photo model field each show the ONNX model
  with its Download.

## Measured on the owner's laptop (i7-1365U, Iris Xe, 2026-09-29)

| Model | Engine | Result |
|---|---|---|
| Florence-2 base, 4 photos | torch (before) | load 22.4 s, 11.4-14.3 s a photo |
| Florence-2 base int8 | ONNX, processor | load **4.4 s**, 11.4-13.6 s a photo, **identical tags**, captions of the same quality in different words |
| Florence-2 base int8 | ONNX, DirectML | 17-48 s a photo, **nonsense** - hence D3 |
| Whisper base, 80 s clip (40 s silence) | ONNX, encoder DirectML + decoder CPU | **4.1-4.2 s**, every word right, language detected, resume from 5 s correct |
| Whisper base | ONNX, all CPU | 5.9-6.2 s |
| Whisper base int8 | ONNX, CPU | 7.0-9.5 s (smaller, not faster) |
| Whisper log-mel | numpy vs faster-whisper | largest difference **0.0** (80 and 128 bins) |

## Items

- [x] **1** `app/ort/` - `session.py` (device choice through `backends.choose`/`with_fallback`,
  `gpu_exclusive`, run-log provider; D3), `hub.py` (files per model, offline `resolve`,
  `fetch`), `generate.py` (cache loop, rules: forced first token, no-repeat n-gram, suppress,
  sampling). Tests: `test_ort_generate.py`.
- [x] **2** Photo tags on ONNX: `app/ort/florence.py`; `florence_tagger.py` keeps its API
  (`available`, `tag_image`, `_load`) and gains `describe`. Tests: `test_ort_florence.py`,
  `test_florence_tagger.py` (real model runs when downloaded).
- [x] **3** Speech on ONNX: `app/ort/whisper.py` (numpy log-mel, timestamp rules, seek, silence
  skip), `transcribe.py` uses it, `media_tools.decode_audio`. Tests: `test_ort_whisper.py`
  (28, real model included).
- [x] **4** Chat model on ONNX: `app/ort/llm.py` (`OnnxLLM`, the `LLM` protocol), `app/llm/
  engines.py`, `CHAT_ENGINE` (registry, config, `ChatSettings`), chat engine, Interpret,
  Describe, pipeline. Tests: `test_ort_llm.py`, and the Ollama tests now name their engine.
- [x] **5** Settings: engine row and ONNX Downloads (Chat box, Interpret box, photo model field);
  `model_fetch` `onnx` kind and `speech` fetching the ONNX export; `doctor.py` reports each
  ONNX model.
- [x] **6** False strings corrected (D7); `requirements.txt` dated notes (torch, transformers,
  faster-whisper not needed; PyAV still is).
- [ ] **7** Chat and Interpret measured on the real Qwen model (it was still downloading at
  ~75 KB/s): tokens per second on the processor, first-token time, one Interpret sentence, one
  Chat answer, the router and planner (JSON) on it.
  > **2026-09-30, int8 measured - not good enough yet; left open.** Load 3.3-5.7 s, first word
  > 0.45-0.77 s, 4.8-5.8 tokens/s on the processor; an ordinary Chat answer was sensible.
  > **Interpret failed** (prose, or the sentence back, where Ollama's `qwen2.5:1.5b` on the same
  > laptop gave `from:chris license after:2025-12-31`), and JSON came back as an echo of the
  > instruction. Two real faults found and fixed on the way: (1) at ONNX Runtime's full graph
  > optimisation the cached step chose different words from a from-scratch recompute ("phrase"
  > for "three") - the chat model now opens at `basic`, where they agree
  > (`session.load_session(optimise=)`); (2) JSON now starts the reply with `{` for the model,
  > since plain decoding cannot be constrained the way Ollama's `format: json` is - it then
  > parsed. What remains is most likely the **int8 file**: it quantises activations on every
  > step; Ollama's copy is 4-bit weights with full-precision activations. The export's
  > `model_q4.onnx` is quantised that way; the owner approved the download (1.7 GB, ~85 KB/s -
  > hours). The chat model prefers it when present (`hub.QWEN_1_5B_Q4`) - **unmeasured; this
  > item closes only when q4 is measured against Ollama on the same sentences**
  > (`tools/measure_onnx_chat.py` and `tools/measure_ollama_chat.py`; photos: `tools/measure_onnx_photo.py`).
  > Until then Interpret on ONNX is unreliable: on a machine where Ollama works, `CHAT_ENGINE`
  > `ollama` gives the better Interpret today.
- [x] **8** Florence-2 full precision measured against int8: vision encoder on DirectML
  (the decoder stays on the processor, D3), caption quality on the same four photos.
  > **2026-09-30.** Full precision, vision graph on the graphics card: **3.6-5.8 s a photo**;
  > all on the processor 7.8-8.7 s; int8 (processor only) 11.4-13.6 s. The graphics card's
  > captions matched the processor's word for word, and read better than int8's ("a blue
  > sculpture on a gray background ... a human head" where int8 said "a white background").
  > Full precision is now the default when it is on disk (`hub.FLORENCE`), int8 the fallback
  > (`hub.FLORENCE_INT8`); Download fetches full precision (about 1 GB).
- [ ] **9** One real index run with photo tags and speech on, read from its logs (order 1a §3).
- [ ] **10** HANDOFF and the register updated; CHANGELOG entry; PR; the owner merges.

## Not in this order

- `rawpy` (camera RAW) is also unsigned and was blocked by Smart App Control; it is not a model
  and cannot move to ONNX. Worth a plain-words doctor/Settings line of its own.
- Removing torch, transformers and faster-whisper from the venv: nothing imports them now, but
  uninstalling is the owner's call.
- OpenVINO as an execution provider for Intel graphics (discussed 2026-09-29): a later
  measurement, not part of this change.
