# Work order: manage the ONNX models - a catalogue, a Models box, removal, updates

**Doc version:** 1.0 · **Updated:** 2026-09-30 · **Applies to:** app v0.3.3
**Created:** 2026-09-30 · **Layer:** L1 (`app/ort/catalogue.py`, `inventory.py`, `discover.py`) and L5 (`app/ui/widgets/model_manager.py`)
**Thread:** the laptop session of 2026-09-29/30, branch `feat/onnx-everywhere` (builds on order 1b)
**Status:** ACTIVE *(owner, 2026-09-30: "a way to delete the model ... how do you add if new models are released ... a mechanism to manage onnx models", then "the models we know and have tested which work and are best make them default ... do it all", "in the catalog also have description and put the list of all compatible models available today", "can this list not be dynamic from hugging face ... a copy but a button to update local list")*

## Why

Settings could download a model and nothing else: no way to see what was on disk, remove
it, update it or add a new one; the ONNX models were constants in code. The model folder
held 7.2 GB of which 3.4 GB nothing used (an int8 chat copy that answered wrongly, three
rerankers not chosen, the old faster-whisper files, superseded int8 copies).

## Decisions

- **D1 A catalogue that is data.** `app/ort/catalogue.json`: per copy its job, runner, repo,
  **pinned revision**, files with **sha256**, size, licence, rank, a plain **description**, and
  a **verified** record (when, which machine, what was measured). Only verified entries are
  recommended. A new model of a kind Leasha runs is an entry; a new kind needs a runner.
- **D2 Tested models are the defaults.** Photo: Florence-2 base full precision; speech: Whisper
  base; chat: Qwen 2.5 1.5B 4-bit; meaning `BAAI/bge-small-en-v1.5`, rerank
  `Xenova/ms-marco-MiniLM-L-6-v2` (unchanged); `OLLAMA_MODEL` default `qwen2.5:1.5b` (was
  `mistral`, never checked). "Restore defaults" and "Use recommended models" bring them back.
  The meaning model is never changed silently - that re-reads every file.
- **D3 The Hugging Face list is dynamic, with a shipped copy.** `app/ort/discover.py` asks
  Hugging Face (`onnx-community`) which models the three runners can load - decided from the
  files and the chat template, not the name - and saves the list in the state folder.
  `catalogue_hf.json` ships today's (220 models: 102 chat, 110 speech, 8 photo). Found models
  are pinned to the revision seen, never verified, never recommended. Chat models are offered
  **4-bit only** (the int8 Qwen copy failed; fp16 is slow on a processor); English-only
  Whisper is left out.
- **D4 Four chat formats.** ChatML, Llama 3, Gemma, Phi-3 (`app/ort/llm.py`), each with its
  end-of-turn tokens, so a found chat model is prompted the way it was trained.
- **D5 "Use this" per job** (`catalogue.choose`), honoured by photo tags, speech and chat.
- **D6 Removal is permanent in the app and says so first**; a model in use says what stops and
  is removed only on a yes. A catalogue copy is its files (copies share snapshot folders); a
  repository with no graphs left is removed. **This session's own clean-up went to the Recycle
  Bin** (3.4 GB), not a permanent delete - emptying it is the owner's.
- **D7 Downloads fetch the pinned revision and are checked against the sha256**; a mismatch
  fails the download.

## Items

- [x] **1** `catalogue.py` + `catalogue.json` (11 curated entries, 6 verified with checksums),
  `load` merging a fetched newer catalogue and the Hugging Face list (curated wins, no
  duplicates), `best`, `recommended`, `choose`/`chosen`, `verify_files`, `check_for_update`.
- [x] **2** `inventory.py`: everything in the model folder, what it is for, in use or not,
  sizes, `unused`, `remove`.
- [x] **3** `discover.py` and the shipped `catalogue_hf.json`; measured live: 220 models in
  361 s on the owner's link.
- [x] **4** The Models box (Settings, Models): on this computer (in use, size, Delete,
  "Remove copies nothing uses", "Use this"), available (filter, description, Download),
  "Use recommended models", "Update the list from Hugging Face".
- [x] **5** Chat prompt formats; `OLLAMA_MODEL` default; downloads pinned and checksummed.
- [x] **6** Tests: `test_ort_models_manage.py` (17), `test_model_manager_qt.py` (4); the wider
  affected set 2,413 passed.
- [x] **7** This laptop cleaned: 7.2 GB -> 3.7 GB, every model in use untouched.
  > **2026-09-30, closed.** Both chat formats work on real models: Gemma 3 1B and Llama 3.2 1B
  > (4-bit, downloaded on the owner's approval) answer Chat coherently and their JSON parses.
  > Neither beats Qwen at Interpret. Gemma: 7.9 words/s, but "last year" stayed as words.
  > Llama: 15.3 words/s, but it returned searches copied from its prompt's examples. Neither
  > is `verified`; both are curated entries with the result as their note (catalogue
  > 2026-09-30.3): Gemma offered, Llama not. Found on the way: `OnnxLLM(cache, key)` never
  > read `key`, so the first run measured Qwen three times - fixed (`_asked`), with a test.
  > `tools/measure_onnx_chat.py` takes a catalogue key as its argument.
- [x] **8** Try one found model of each new chat format (Gemma, Llama 3) on a real machine with
  `tools/measure_onnx_chat.py`; a pass is a `verified` entry in `catalogue.json`.
  > **2026-09-30, closed.** Merged to `main` as `bea16e7`; the button on this laptop read
  > GitHub's copy and answered "No new models: this computer already has the latest list
  > (2026-09-30.2)".
- [x] **9** "Check for new models" of the curated catalogue reads this repository's `main` on
  GitHub (checked 2026-09-30: public, answers). It has something to read once
  `app/ort/catalogue.json` is merged to `main`; from then on, a verified entry added there
  reaches every copy of Leasha that presses the button.
  > **2026-09-30, closed.** No PR: on the owner's word ("first merge push and update local") the
  > branch was merged straight to `main` as `bea16e7`, pushed, and the laptop updated.
- [x] **10** PR with order 1b; the owner merges.
