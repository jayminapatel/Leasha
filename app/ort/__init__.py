r"""Leasha's own models on ONNX Runtime: photo tags, speech, and the chat model.

Layer: L2 (models) over L0 (`app.index.backends`, `app.core.gpu_serialize`).

**Why this package exists** (owner, 2026-09-29: "all should be onnx by default").
Four of Leasha's six models already ran on ONNX Runtime - meaning search,
re-ranking, CLIP and OCR. The other two did not: Florence-2 photo tags on torch,
and Whisper speech on CTranslate2. Windows' Smart App Control blocked torch on
the owner's laptop (an unsigned DLL) and will on any machine where it is on,
with no exception an administrator can add. ONNX Runtime is signed by Microsoft
and already loads everywhere Leasha runs. The chat model moves here too, as the
default beside Ollama.

**One runtime, no second native package.** Text generation is a small loop of
our own (`generate.py`) on the plain `onnxruntime` Leasha already pins, not
Microsoft's separate ONNX Runtime GenAI: that is another native library tied to
its own onnxruntime version, and `doctor.py` insists on exactly one.

**Models come from `onnx-community` on Hugging Face**, the exports
transformers.js runs: `Florence-2-base` (MIT), `whisper-base` (MIT, OpenAI's
weights), `Qwen2.5-1.5B-Instruct` (Apache-2.0). Tokenising is `tokenizers`,
already installed for fastembed. **Nothing is downloaded while indexing**:
`hub.resolve` looks in the model cache only; the Download buttons fetch.
"""
