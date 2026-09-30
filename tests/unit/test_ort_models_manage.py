"""Managing the ONNX models: the catalogue, the inventory, removal, and the Hugging Face list.

Layer: L1. No network and no real models: a temporary model folder in the
Hugging Face cache layout, and made-up repository metadata.
Owner, 2026-09-30: delete, update, add new models, tested models as defaults,
and a button that refreshes the list from Hugging Face.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.ort import catalogue, discover, hub, inventory
from app.ort.llm import format_prompt


def _place(cache: Path, entry: catalogue.Entry, *, revision: str = "rev1", content: bytes = b"x") -> Path:
    """Write every file `entry` needs into a snapshot folder, as a download would."""
    snap = cache / ("models--" + entry.repo.replace("/", "--")) / "snapshots" / revision
    for name in entry.model().files():
        (snap / name).parent.mkdir(parents=True, exist_ok=True)
        (snap / name).write_bytes(content)
    return snap


@pytest.fixture()
def state(tmp_path, monkeypatch):
    folder = tmp_path / "state"
    folder.mkdir()
    monkeypatch.setattr(catalogue, "_state_cache", [folder])
    return folder


# -- the catalogue ---------------------------------------------------------------------

def test_the_shipped_catalogue_recommends_the_models_that_were_checked(state):
    cat = catalogue.load(state)
    assert cat.recommended("photo").key == "florence-2-base"
    assert cat.recommended("chat").key == "qwen2.5-1.5b-instruct-q4"
    assert cat.recommended("speech", "base").key == "whisper-base"
    int8 = cat.by_key("qwen2.5-1.5b-instruct")
    assert not int8.is_verified and not int8.offered and "wrong answers" in int8.note


def test_every_shipped_entry_has_a_description_and_the_checked_ones_a_pinned_revision(state):
    for entry in catalogue.load(state).entries:
        assert entry.description, entry.key
        if entry.is_verified:
            assert entry.revision and entry.sha256, entry.key


def test_the_list_from_hugging_face_is_shipped_and_merged_without_duplicates(state):
    cat = catalogue.load(state)
    found = [e for e in cat.entries if e.source == "huggingface"]
    assert found and all(not e.is_verified for e in found)
    pairs = [(e.repo.lower(), e.suffix) for e in cat.entries]
    assert len(pairs) == len(set(pairs)), "one copy of one repository is one entry"


def test_a_fetched_newer_list_is_used_and_a_broken_one_is_ignored(state):
    shipped = json.loads(catalogue.BUNDLED.read_text(encoding="utf-8"))
    newer = dict(shipped, version="2099-01-01.1")
    newer["models"] = shipped["models"] + [dict(shipped["models"][0], key="brand-new")]
    catalogue.fetched_path(state).write_text(json.dumps(newer), encoding="utf-8")
    assert catalogue.load(state).by_key("brand-new") is not None
    catalogue.fetched_path(state).write_text("{not json", encoding="utf-8")
    assert catalogue.load(state).version == shipped["version"]


def test_use_this_wins_when_that_model_is_on_disk(state, tmp_path):
    cache = tmp_path / "models"
    cat = catalogue.load(state)
    _place(cache, cat.by_key("florence-2-base"))
    _place(cache, cat.by_key("florence-2-base-int8"))
    assert catalogue.best("photo", cache, catalogue=cat)[0].key == "florence-2-base"
    catalogue.choose("photo", "florence-2-base-int8", state)
    assert catalogue.chosen("photo", state) == "florence-2-base-int8"
    assert catalogue.best("photo", cache, catalogue=cat)[0].key == "florence-2-base-int8"
    catalogue.choose("photo", "", state)
    assert catalogue.best("photo", cache, catalogue=cat)[0].key == "florence-2-base"


def test_a_file_that_does_not_match_its_checksum_is_named(state, tmp_path):
    entry = catalogue.load(state).by_key("whisper-base")
    snap = _place(tmp_path / "models", entry)
    assert set(catalogue.verify_files(entry, snap)) == set(entry.sha256)


# -- the inventory ---------------------------------------------------------------------

def _settings(cache: Path, state: Path, **kw):
    base = dict(model_cache=cache, state_path=state, chat_engine="onnx", transcribe_model="base",
                embed_model="BAAI/bge-small-en-v1.5", rerank_model="Xenova/ms-marco-MiniLM-L-6-v2",
                embed_quantised=False)
    base.update(kw)
    return SimpleNamespace(**base)


def test_the_inventory_says_what_is_in_use_and_what_is_not(state, tmp_path):
    cache = tmp_path / "models"
    cat = catalogue.load(state)
    for key in ("florence-2-base", "florence-2-base-int8", "qwen2.5-1.5b-instruct-q4",
                "whisper-base"):
        _place(cache, cat.by_key(key))
    (cache / "whisper").mkdir()
    (cache / "whisper" / "model.bin").write_bytes(b"old")
    (cache / "models--Qdrant--clip-ViT-B-32-vision").mkdir()
    (cache / "models--someone--old-reranker").mkdir()
    items = {i.key: i for i in inventory.scan(cache, _settings(cache, state), catalogue=cat)}
    assert items["florence-2-base"].in_use and not items["florence-2-base-int8"].in_use
    assert items["qwen2.5-1.5b-instruct-q4"].in_use and items["whisper-base"].in_use
    assert items["folder:models--Qdrant--clip-ViT-B-32-vision"].in_use
    assert not items["folder:whisper"].in_use and "old speech engine" in items["folder:whisper"].note
    assert not items["folder:models--someone--old-reranker"].in_use
    spare = {i.key for i in inventory.unused(list(items.values()))}
    assert spare == {"florence-2-base-int8", "folder:whisper", "folder:models--someone--old-reranker"}


def test_the_chat_model_is_not_in_use_when_ollama_is_the_engine(state, tmp_path):
    cache = tmp_path / "models"
    cat = catalogue.load(state)
    _place(cache, cat.by_key("qwen2.5-1.5b-instruct-q4"))
    items = inventory.scan(cache, _settings(cache, state, chat_engine="ollama"), catalogue=cat)
    assert not next(i for i in items if i.key == "qwen2.5-1.5b-instruct-q4").in_use


def test_removing_one_copy_leaves_the_other_copys_files(state, tmp_path):
    cache = tmp_path / "models"
    cat = catalogue.load(state)
    snap = _place(cache, cat.by_key("florence-2-base"))
    _place(cache, cat.by_key("florence-2-base-int8"))
    items = {i.key: i for i in inventory.scan(cache, _settings(cache, state), catalogue=cat)}
    freed = inventory.remove(items["florence-2-base-int8"])
    assert freed > 0
    assert (snap / "onnx/vision_encoder.onnx").is_file(), "the full copy is untouched"
    assert not (snap / "onnx/vision_encoder_int8.onnx").exists()


def test_a_model_in_use_is_removed_only_when_the_person_said_so(state, tmp_path):
    cache = tmp_path / "models"
    cat = catalogue.load(state)
    _place(cache, cat.by_key("whisper-base"))
    item = next(i for i in inventory.scan(cache, _settings(cache, state), catalogue=cat)
                if i.key == "whisper-base")
    with pytest.raises(PermissionError):
        inventory.remove(item)
    inventory.remove(item, force=True)
    assert not (cache / "models--onnx-community--whisper-base").exists(), \
        "a repository with no model graphs left is removed"


def test_a_verified_model_not_downloaded_is_offered(state, tmp_path):
    cache = tmp_path / "models"
    cache.mkdir()
    items = inventory.scan(cache, _settings(cache, state), catalogue=catalogue.load(state))
    missing = {i.key for i in items if not i.present}
    assert {"florence-2-base", "qwen2.5-1.5b-instruct-q4", "whisper-base"} <= missing


# -- the list from Hugging Face ------------------------------------------------------

def _info(repo: str, files: dict, **card):
    return {"id": repo, "sha": "abc123", "downloads": 42, "cardData": card,
            "siblings": [{"rfilename": name, "size": size} for name, size in files.items()]}


SIDE = {"config.json": 1, "generation_config.json": 1, "tokenizer.json": 1,
        "tokenizer_config.json": 1}


def test_a_chat_model_is_listed_in_its_4_bit_copy_with_its_prompt_format():
    info = _info("onnx-community/SomeChat-1B-Instruct", {**SIDE, "onnx/model_q4.onnx": 2 ** 30,
                                                          "onnx/model_int8.onnx": 2 ** 30},
                 license="apache-2.0", base_model="org/SomeChat-1B")
    rows = discover.entry_for(info, '{"chat_template": "<|start_header_id|>..."}')
    assert [r["suffix"] for r in rows] == ["_q4"], "4-bit only; int8 failed on the owner's laptop"
    row = rows[0]
    assert row["prompt_format"] == "llama3" and row["revision"] == "abc123"
    assert row["verified"] is None and row["source"] == "huggingface"
    assert "Not checked" in row["description"] and "org/SomeChat-1B" in row["description"]


def test_a_chat_model_with_a_template_we_do_not_speak_is_left_out():
    info = _info("onnx-community/Odd-Chat", {**SIDE, "onnx/model_q4.onnx": 10})
    assert discover.entry_for(info, '{"chat_template": "### Human:"}') == []


def test_english_only_whisper_is_left_out_and_others_listed_in_both_copies():
    files = {**SIDE, "onnx/encoder_model.onnx": 10, "onnx/decoder_model_merged.onnx": 10,
             "onnx/encoder_model_int8.onnx": 5, "onnx/decoder_model_merged_int8.onnx": 5}
    assert discover.entry_for(_info("onnx-community/whisper-small.en", files)) == []
    rows = discover.entry_for(_info("onnx-community/kb-whisper-small-ONNX", files, language=["sv"]))
    assert sorted(r["suffix"] for r in rows) == ["", "_int8"]
    assert all(r["job"] == "speech" and "sv" in r["description"] for r in rows)


def test_prompt_formats_are_read_from_the_template():
    assert discover.prompt_format_of("<|im_start|>") == "chatml"
    assert discover.prompt_format_of("<start_of_turn>") == "gemma"
    assert discover.prompt_format_of("<|user|> <|assistant|>") == "phi3"
    assert discover.prompt_format_of("nothing") == ""


def test_each_prompt_format_opens_the_assistant_turn():
    messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    assert format_prompt("llama3", messages).endswith("<|start_header_id|>assistant<|end_header_id|>\n\n")
    gemma = format_prompt("gemma", messages)
    assert gemma.startswith("<bos>") and "S\n\nhi" in gemma and gemma.endswith("<start_of_turn>model\n")
    assert format_prompt("phi3", messages).endswith("<|assistant|>\n")
    assert format_prompt("unknown", messages).startswith("<|im_start|>system\nS")


def test_hub_by_key_finds_a_model_only_the_catalogue_knows(state):
    key = next(e.key for e in catalogue.load(state).entries if e.source == "huggingface")
    model = hub.by_key(key)
    assert model is not None and model.revision
