# Reference: which models run where, and moving them to Ollama

**Doc version:** 1.4 · **Updated:** 2026-10-06 · **Applies to:** app v0.3.4

Kept at the owner's request ("keep this as a reference for future") after a
conversation on 2026-09-29 about moving every model to Ollama. **Reference, not a
decision and not a work order.** Nothing here is ordered; see
`docs/ORDER_REGISTER.md` for what is.

Every model Leasha runs itself runs on **ONNX Runtime**, in process: the owner chose that on
2026-09-29 ("all should be onnx by default") after Smart App Control blocked torch. Interpret,
Describe and Chat can be moved to Ollama with one setting (`CHAT_ENGINE`); nothing else can,
and nothing in Leasha uses torch or transformers. The laptop has Intel graphics, which Ollama
does not officially accelerate, so Ollama would run vision models on the processor.

## What runs each model today

| Job | Model | Runs through | Could it move to Ollama? |
|---|---|---|---|
| Meaning search (text vectors) | `EMBED_MODEL` | fastembed (ONNX), in process — `app/index/embedder.py` | **Possible**: Ollama serves embedding models (nomic-embed-text, mxbai-embed-large, bge-m3) |
| Re-ranking results | `RERANK_MODEL` (cross-encoder) | fastembed (ONNX) — `app/search/rerank.py` | **No**: Ollama has no rerank endpoint (as far as known on 2026-09-29) |
| Pictures by meaning (CLIP) | CLIP | fastembed `ImageEmbedding` — `app/index/clip_embedder.py` | **No**: Ollama gives no image vectors |
| Text in pictures (OCR) | RapidOCR | ONNX — `app/extract/ocr.py` | **Not sensibly**: a vision model can read text, far slower and can invent it |
| Speech to text | Whisper | ONNX Runtime — `app/ort/whisper.py` (audio decoded by PyAV) | **No**: Ollama does no speech |
| Photo tags and caption | Florence-2-base | ONNX Runtime — `app/ort/florence.py`; fp32 by default, int8 optional; the vision graph on the graphics card where DirectML works | **Yes**: Ollama vision models (moondream, llava, qwen2.5vl, minicpm-v) |
| Interpret, Describe, Chat | the chat model chosen in Settings (Qwen2.5-1.5B 4-bit on the owner's laptop) | ONNX Runtime inside Leasha by default (`CHAT_ENGINE=onnx`); Ollama when chosen | already a choice |


## Pros and cons of Ollama

For:

- One place to download and manage models (fits the model drop-downs with Download).
- Ollama manages the GPU; Leasha needs no CUDA/DirectML set-up of its own.
- Changing a vision model becomes a drop-down choice, not code.

Against:

- **Core search would need a separate service.** Search and indexing work today
  with Ollama absent ("Search never uses this"). Moving embeddings would make
  indexing and meaning search stop whenever Ollama is not running, and break
  "one process, fully offline" (still local, but not one process).
- Bulk embedding in process avoids an HTTP round trip per batch; over ~100 GB
  that matters. **Not measured.**
- A different embedding model means re-embedding the whole index.
- Less control over threads, quantisation and the indexer's CPU limits.
- Rerank, CLIP, speech and OCR cannot move, so ONNX stays regardless.

## Performance of Florence-2 vs Ollama vision (estimates, **not measured**)

Per photo:

| | CPU only | Laptop NVIDIA GPU (Ollama) |
|---|---|---|
| Florence-2 today (in process, CPU) | ~1–3 s | n/a (not wired for GPU) |
| moondream (1.8B) | ~3–6 s | ~0.5–1 s |
| qwen2.5vl 3B | ~8–15 s | ~1–2 s |
| llava / qwen2.5vl 7B | ~15–40 s | ~1–3 s |

- With a supported GPU, Ollama matches or beats Florence-2 and gives richer captions.
- Without one it is 2–10× slower per photo — days rather than hours over tens of
  thousands of photos, though in the background and resumable.
- Ollama supports NVIDIA and some AMD GPUs; Intel integrated graphics are not
  officially supported and fall back to CPU.
- Leasha's own embedder may also use the GPU while indexing; `gpu_serialize`
  takes turns inside Leasha but does not cover Ollama.

## The recommendation given

1. Move **Florence-2 to an Ollama vision model** (optional, off by default as
   today) and drop torch/transformers.
2. **Keep** embeddings, rerank, CLIP, OCR and speech in process.
3. Optionally offer Ollama as a *second* embedding choice, not a replacement.

Before building: a side-by-side on the owner's laptop — Florence-2 against
moondream and qwen2.5vl over ~50 of their own photos, seconds per photo and
caption quality. Open question to the owner: which GPU the laptop has.
