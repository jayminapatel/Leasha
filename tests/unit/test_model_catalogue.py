"""Every model Leasha needs: the list, whether each is here, and downloading them.

The owner, 2026-10-08: one Download button per model and one for all - in
Settings, in the installer and on the command line, all from
`app/core/model_catalogue.py`. **Nothing is downloaded here**: `model_fetch.fetch`
and `model_fetch.present` are stand-ins, or a fake child process, and every
folder is a temporary one.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from app.core import model_catalogue as mc
from app.core import model_fetch
from app.core.errors import AppErrorException


def settings_for(tmp_path, **changes):
    values = dict(embed_model="BAAI/bge-small-en-v1.5",
                  rerank_model="Xenova/ms-marco-MiniLM-L-6-v2",
                  model_cache=tmp_path / "models", state_path=tmp_path / "state",
                  transcribe_model="base", chat_model="", chat_engine="onnx")
    values.update(changes)
    return SimpleNamespace(**values)


@pytest.fixture
def settings(tmp_path):
    return settings_for(tmp_path)


@pytest.fixture
def no_fetch(monkeypatch):
    """Any real download is a failure of the test."""
    def refuse(*_a, **_k):
        raise AssertionError("a test tried to download a model")

    monkeypatch.setattr(model_fetch, "fetch", refuse)


# ---------------------------------------------------------------------------
# The list
# ---------------------------------------------------------------------------

def test_the_list_is_every_model_in_its_order(settings):
    models = mc.needed_models(settings)
    assert [m.key for m in models] == ["search", "rerank", "pictures", "photo-tags",
                                       "speech", "chat", "faces"]
    assert [m.key for m in models] == list(mc.KEYS)
    for m in models:
        assert m.title and m.purpose.endswith(".") and m.model and m.approx_mb > 0, m


def test_only_search_and_rerank_are_ticked_by_default(settings):
    assert {m.key for m in mc.needed_models(settings) if m.install_default} == {"search",
                                                                               "rerank"}


def test_faces_say_when_they_are_used(settings):
    faces = {m.key: m for m in mc.needed_models(settings)}["faces"]
    assert "Recognise people in photos on this computer" in faces.optional_note
    assert faces.model == "insightface buffalo_l"


def test_the_default_names_and_sizes(settings):
    by_key = {m.key: m for m in mc.needed_models(settings)}
    assert by_key["search"].model == "BAAI/bge-small-en-v1.5"
    assert by_key["search"].approx_mb == 67
    assert by_key["rerank"].model == "Xenova/ms-marco-MiniLM-L-6-v2"
    assert by_key["pictures"].model == ("Qdrant/clip-ViT-B-32-vision + "
                                        "Qdrant/clip-ViT-B-32-text")
    assert by_key["pictures"].approx_mb == 340 + 250
    assert by_key["photo-tags"].model == "onnx-community/Florence-2-base"
    assert by_key["speech"].model == "onnx-community/whisper-base"
    # CHAT_MODEL empty: the catalogue's recommended chat copy, the 4-bit Qwen -
    # the copy `OnnxLLM._copies` puts first.
    assert by_key["chat"].model == "onnx-community/Qwen2.5-1.5B-Instruct, 4-bit"
    assert by_key["chat"].approx_mb == 1705


def test_the_names_follow_the_settings(tmp_path):
    s = settings_for(tmp_path, embed_model="BAAI/bge-base-en-v1.5",
                     rerank_model="jinaai/jina-reranker-v1-tiny-en",
                     transcribe_model="small", chat_model="qwen2.5-1.5b-instruct",
                     chat_engine="ollama")
    by_key = {m.key: m for m in mc.needed_models(s)}
    assert (by_key["search"].model, by_key["search"].approx_mb) == ("BAAI/bge-base-en-v1.5", 210)
    assert by_key["rerank"].model == "jinaai/jina-reranker-v1-tiny-en"
    assert (by_key["speech"].model, by_key["speech"].approx_mb) == (
        "onnx-community/whisper-small", 968)
    assert by_key["chat"].model == "onnx-community/Qwen2.5-1.5B-Instruct, int8"
    assert "Ollama" in by_key["chat"].optional_note


def test_a_copy_picked_with_use_this_is_the_one_listed(settings):
    from app.ort import catalogue

    catalogue.choose("photo", "florence-2-base-int8", settings.state_path)
    photo = {m.key: m for m in mc.needed_models(settings)}["photo-tags"]
    assert photo.model == "onnx-community/Florence-2-base, int8"
    assert photo.approx_mb == 260
    assert mc._parts("photo-tags", settings) == [("onnx", "florence-2-base-int8")]


def test_what_each_key_downloads(settings):
    from app.extract.face_detect import MODEL_PACK
    from app.index.clip_embedder import CLIP_IMAGE_MODEL
    from app.search.vector import CLIP_TEXT_MODEL

    assert mc._parts("search", settings) == [("embed", "BAAI/bge-small-en-v1.5")]
    assert mc._parts("rerank", settings) == [("rerank", "Xenova/ms-marco-MiniLM-L-6-v2")]
    # Both halves of CLIP, each as the code that loads it builds it.
    assert mc._parts("pictures", settings) == [("image", CLIP_IMAGE_MODEL),
                                               ("embed", CLIP_TEXT_MODEL)]
    assert mc._parts("photo-tags", settings) == [("onnx", "florence-2-base")]
    assert mc._parts("speech", settings) == [("speech", "base")]
    assert mc._parts("chat", settings) == [("onnx", "qwen2.5-1.5b-instruct-q4")]
    assert mc._parts("faces", settings) == [("faces", MODEL_PACK)]
    assert mc._parts("nonsense", settings) == []


# ---------------------------------------------------------------------------
# On this computer?
# ---------------------------------------------------------------------------

def test_presence_asks_model_fetch_for_each_part(settings, monkeypatch):
    here = {("embed", "BAAI/bge-small-en-v1.5"), ("image", "Qdrant/clip-ViT-B-32-vision")}
    monkeypatch.setattr(model_fetch, "present",
                        lambda kind, name, **_k: (kind, name) in here)
    monkeypatch.setattr(mc, "_job_on_disk", lambda *_a: False)
    assert mc.is_present("search", settings)
    assert not mc.is_present("rerank", settings)
    assert not mc.is_present("pictures", settings), "one half of CLIP is not enough"
    here.add(("embed", "Qdrant/clip-ViT-B-32-text"))
    assert mc.is_present("pictures", settings)


def test_any_copy_on_disk_counts_for_the_onnx_jobs(settings, monkeypatch):
    """Somebody with the smaller Florence-2 has photo tags; the app loads it."""
    monkeypatch.setattr(model_fetch, "present", lambda *_a, **_k: False)
    monkeypatch.setattr(mc, "_job_on_disk", lambda job, _s: job == "photo")
    assert mc.is_present("photo-tags", settings)
    assert not mc.is_present("chat", settings)
    assert not mc.is_present("search", settings), "fastembed models have no fallback"


def test_presence_never_raises_and_unknown_is_false(settings, monkeypatch):
    def broken(*_a, **_k):
        raise OSError("disk gone")

    monkeypatch.setattr(model_fetch, "present", broken)
    monkeypatch.setattr(mc, "_job_on_disk", broken)
    for key in mc.KEYS:
        assert mc.is_present(key, settings) is False
    assert mc.is_present("nonsense", settings) is False


def test_presence_reads_fastembeds_own_folders(tmp_path):
    """The real check on a real layout: fastembed's Hugging Face folders, CLIP's
    picture half included (the new `image` kind)."""
    s = settings_for(tmp_path)
    cache = s.model_cache
    assert not mc.is_present("search", s) and not mc.is_present("pictures", s)
    for repo in ("qdrant--bge-small-en-v1.5-onnx-q", "Qdrant--clip-ViT-B-32-vision",
                 "Qdrant--clip-ViT-B-32-text"):
        snap = cache / f"models--{repo}" / "snapshots" / "abc"
        snap.mkdir(parents=True)
        (snap / "model.onnx").write_bytes(b"x")
    assert mc.is_present("search", s)
    assert mc.is_present("pictures", s)


def test_faces_are_here_only_with_insightface_and_the_whole_pack(tmp_path, monkeypatch):
    s = settings_for(tmp_path)
    monkeypatch.setattr(model_fetch, "faces_root", lambda: tmp_path / "insightface")
    pack = tmp_path / "insightface" / "models" / "buffalo_l"
    pack.mkdir(parents=True)
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: True)
    assert not mc.is_present("faces", s), "an empty pack folder is an unfinished download"
    (pack / "det_10g.onnx").write_bytes(b"x")
    assert not mc.is_present("faces", s)
    (pack / "w600k_r50.onnx").write_bytes(b"x")
    assert mc.is_present("faces", s)
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: False)
    assert not mc.is_present("faces", s), "no insightface, no faces"


# ---------------------------------------------------------------------------
# Downloading one
# ---------------------------------------------------------------------------

def _recording_fetch(calls, outcome=model_fetch.DONE):
    def fetch(kind, name, *, model_cache=None, on_progress=None, stop=None, **_k):
        calls.append((kind, name, model_cache, stop))
        return outcome
    return fetch


def test_pictures_downloads_both_halves_with_the_right_kinds(settings, monkeypatch):
    calls: list = []
    monkeypatch.setattr(model_fetch, "fetch", _recording_fetch(calls))
    monkeypatch.setattr(mc, "_part_present", lambda *_a: False)
    said: list[str] = []
    assert mc.download("pictures", settings, on_progress=said.append) == model_fetch.DONE
    assert [(k, n, c) for k, n, c, _ in calls] == [
        ("image", "Qdrant/clip-ViT-B-32-vision", settings.model_cache),
        ("embed", "Qdrant/clip-ViT-B-32-text", settings.model_cache)]
    assert all(isinstance(stop, threading.Event) for *_, stop in calls)
    assert any("ready" in line for line in said)


def test_a_half_already_here_is_not_fetched_again(settings, monkeypatch):
    calls: list = []
    monkeypatch.setattr(model_fetch, "fetch", _recording_fetch(calls))
    monkeypatch.setattr(mc, "_part_present", lambda kind, _n, _s: kind == "image")
    mc.download("pictures", settings, on_progress=lambda _t: None)
    assert [c[0] for c in calls] == ["embed"]


@pytest.mark.parametrize("key, kind, name", [
    ("search", "embed", "BAAI/bge-small-en-v1.5"),
    ("rerank", "rerank", "Xenova/ms-marco-MiniLM-L-6-v2"),
    ("photo-tags", "onnx", "florence-2-base"),
    ("speech", "speech", "base"),
    ("chat", "onnx", "qwen2.5-1.5b-instruct-q4"),
])
def test_each_key_goes_to_the_right_fetch(settings, monkeypatch, key, kind, name):
    calls: list = []
    monkeypatch.setattr(model_fetch, "fetch", _recording_fetch(calls))
    monkeypatch.setattr(mc, "_part_present", lambda *_a: False)
    mc.download(key, settings, on_progress=lambda _t: None)
    assert [(c[0], c[1]) for c in calls] == [(kind, name)]


def test_faces_go_to_the_faces_fetch_when_insightface_is_here(settings, monkeypatch):
    calls: list = []
    monkeypatch.setattr(model_fetch, "fetch", _recording_fetch(calls))
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: True)
    monkeypatch.setattr(mc, "_part_present", lambda *_a: False)
    mc.download("faces", settings, on_progress=lambda _t: None)
    assert [(c[0], c[1]) for c in calls] == [("faces", "buffalo_l")]


def test_a_stopped_download_says_so_and_goes_no_further(settings, monkeypatch):
    calls: list = []
    monkeypatch.setattr(model_fetch, "fetch", _recording_fetch(calls, model_fetch.STOPPED))
    monkeypatch.setattr(mc, "_part_present", lambda *_a: False)
    assert mc.download("pictures", settings, on_progress=lambda _t: None) == model_fetch.STOPPED
    assert len(calls) == 1


def test_should_stop_reaches_the_download_as_its_stop_event(settings, monkeypatch):
    asked = {"stop": False}

    def fetch(kind, name, *, stop, **_k):
        assert not stop.is_set()
        asked["stop"] = True                      # somebody presses Stop
        return model_fetch.STOPPED if stop.is_set() else model_fetch.DONE

    monkeypatch.setattr(model_fetch, "fetch", fetch)
    monkeypatch.setattr(mc, "_part_present", lambda *_a: False)
    assert mc.download("search", settings, on_progress=lambda _t: None,
                       should_stop=lambda: asked["stop"]) == model_fetch.STOPPED


def test_an_unknown_key_is_a_clear_error(settings, no_fetch):
    with pytest.raises(AppErrorException) as caught:
        mc.download("nonsense", settings)
    assert caught.value.error.code == "ERR_MODEL_DOWNLOAD"
    assert "search" in caught.value.error.details


def test_faces_without_insightface_say_how_to_install_it(settings, monkeypatch, no_fetch):
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: False)
    with pytest.raises(AppErrorException) as caught:
        mc.download("faces", settings)
    error = caught.value.error
    assert error.code == "ERR_MODEL_DOWNLOAD"
    assert "insightface" in error.details
    assert "pip install insightface" in error.suggestion


def test_model_fetch_itself_refuses_faces_without_insightface(tmp_path, monkeypatch):
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: False)

    def no_child(*_a, **_k):
        raise AssertionError("no child should start")

    with pytest.raises(AppErrorException) as caught:
        model_fetch.fetch("faces", "buffalo_l", model_cache=tmp_path, popen=no_child)
    assert "pip install insightface" in caught.value.error.suggestion


# ---------------------------------------------------------------------------
# The two new child programs (a fake child; nothing downloads)
# ---------------------------------------------------------------------------

class _Done:
    returncode = 0

    def poll(self):
        return 0

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_clip_pictures_download_in_a_child_running_image_embedding(tmp_path):
    started: list = []
    result = model_fetch.fetch("image", "Qdrant/clip-ViT-B-32-vision", model_cache=tmp_path,
                               popen=lambda argv, **k: started.append(argv) or _Done())
    assert result == model_fetch.DONE
    argv = started[0]
    assert "ImageEmbedding(model_name=sys.argv[1], cache_dir=sys.argv[2])" in argv[2]
    assert argv[3:] == ["Qdrant/clip-ViT-B-32-vision", str(tmp_path)]


def test_faces_download_into_insightfaces_own_folder(tmp_path, monkeypatch):
    root = tmp_path / "insightface"
    monkeypatch.setattr(model_fetch, "faces_root", lambda: root)
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: True)
    started: list = []
    model_fetch.fetch("faces", "buffalo_l", model_cache=tmp_path / "elsewhere",
                      popen=lambda argv, **k: started.append(argv) or _Done())
    argv = started[0]
    assert "FaceAnalysis(name=sys.argv[1], root=sys.argv[2]" in argv[2]
    assert argv[3:] == ["buffalo_l", str(root), "0"]


def test_an_unfinished_face_pack_is_fetched_again(tmp_path):
    (tmp_path / "models" / "buffalo_l").mkdir(parents=True)
    assert model_fetch._child_args("faces", "buffalo_l", tmp_path)[2] == "1"


def test_the_clip_names_are_fastembeds_own():
    """`image` presence looks the repository up in fastembed's own list."""
    assert model_fetch._fastembed_source("image", "Qdrant/clip-ViT-B-32-vision") == \
        "Qdrant/clip-ViT-B-32-vision"
    assert model_fetch._fastembed_source("embed", "Qdrant/clip-ViT-B-32-text") == \
        "Qdrant/clip-ViT-B-32-text"


# ---------------------------------------------------------------------------
# Downloading all
# ---------------------------------------------------------------------------

def test_download_all_skips_what_is_here_and_carries_on_past_a_failure(settings, monkeypatch):
    monkeypatch.setattr(mc, "is_present", lambda key, _s=None: key == "search")
    tried: list[str] = []

    def download(key, _settings, **_k):
        tried.append(key)
        if key == "rerank":
            from app.core.errors import make_error
            raise AppErrorException(make_error("ERR_MODEL_DOWNLOAD", "test", model="x",
                                               details="no network"))
        return model_fetch.DONE

    monkeypatch.setattr(mc, "download", download)
    said: list[str] = []
    results = mc.download_all(settings, on_progress=said.append,
                              keys=["search", "rerank", "pictures"])
    assert tried == ["rerank", "pictures"]
    assert results["search"] == "present"
    assert results["rerank"].startswith("failed: ") and "no network" in results["rerank"]
    assert results["pictures"] == "done"
    assert any("did not download" in line for line in said)


def test_download_all_honours_stop(settings, monkeypatch):
    monkeypatch.setattr(mc, "is_present", lambda *_a: False)
    pressed = {"stop": False}

    def download(key, _settings, **_k):
        pressed["stop"] = True
        return model_fetch.STOPPED

    monkeypatch.setattr(mc, "download", download)
    results = mc.download_all(settings, on_progress=lambda _t: None,
                              should_stop=lambda: pressed["stop"])
    assert results == {key: "stopped" for key in mc.KEYS}


def test_download_all_reports_faces_without_insightface_and_does_the_rest(settings,
                                                                         monkeypatch):
    monkeypatch.setattr(model_fetch, "faces_installed", lambda: False)
    monkeypatch.setattr(mc, "_part_present", lambda *_a: False)
    monkeypatch.setattr(mc, "_job_on_disk", lambda *_a: False)
    calls: list = []
    monkeypatch.setattr(model_fetch, "fetch", _recording_fetch(calls))
    results = mc.download_all(settings, on_progress=lambda _t: None, keys=["faces", "search"])
    assert results["faces"].startswith("failed: ") and "pip install insightface" in results["faces"]
    assert results["search"] == "done"
    assert [c[0] for c in calls] == ["embed"]


def test_download_all_never_raises_even_without_settings(monkeypatch):
    import app.core.config as config

    def broken(*_a, **_k):
        raise RuntimeError("no .env")

    monkeypatch.setattr(config, "load_settings", broken)
    results = mc.download_all(on_progress=lambda _t: None, keys=["search"])
    assert results["search"].startswith("failed: ") and "no .env" in results["search"]


def test_download_all_names_an_unknown_key_as_failed(settings, no_fetch):
    results = mc.download_all(settings, on_progress=lambda _t: None, keys=["nonsense"])
    assert results["nonsense"].startswith("failed: ")


# ---------------------------------------------------------------------------
# The command line (rule 8: a CLI before the UI)
# ---------------------------------------------------------------------------

@pytest.fixture
def cli_env(tmp_path, monkeypatch):
    data = tmp_path / "data"
    env = tmp_path / ".env"
    env.write_text(f"DATA_PATH={data}\nPROJECT_PATH={tmp_path}\nLOG_PATH={data / 'logs'}\n",
                   encoding="utf-8")
    return str(env)


def run_cli(argv):
    from app import cli

    return cli.main(argv)


def test_models_is_a_registered_command():
    from app import cli

    parser = cli.build_parser()
    choices = next(a.choices for a in parser._actions if getattr(a, "choices", None)
                   and "models" in a.choices)
    assert "usage:" in choices["models"].format_help()


def test_models_list_prints_each_model_and_whether_it_is_here(cli_env, monkeypatch, capsys):
    monkeypatch.setattr(mc, "is_present", lambda key, _s=None: key == "search")
    assert run_cli(["models", "list", "--env", cli_env]) == 0
    out = capsys.readouterr().out
    for key in mc.KEYS:
        assert key in out
    assert "search      here" in out and "rerank      missing" in out
    assert "models download rerank" in out


def test_models_list_json(cli_env, monkeypatch, capsys):
    import json

    monkeypatch.setattr(mc, "is_present", lambda *_a: True)
    assert run_cli(["models", "list", "--json", "--env", cli_env]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["key"] for r in rows] == list(mc.KEYS) and all(r["present"] for r in rows)


def test_models_download_exits_0_when_everything_asked_for_is_here(cli_env, monkeypatch,
                                                                  capsys):
    here: set[str] = set()
    monkeypatch.setattr(mc, "is_present", lambda key, _s=None: key in here)

    def download(key, _settings, **_k):
        here.add(key)
        return model_fetch.DONE

    monkeypatch.setattr(mc, "download", download)
    assert run_cli(["models", "download", "search", "rerank", "--env", cli_env]) == 0
    assert here == {"search", "rerank"}
    assert "Everything asked for is on this computer." in capsys.readouterr().out


def test_models_download_all_exits_1_and_says_why_when_one_fails(cli_env, monkeypatch, capsys):
    here: set[str] = set()
    monkeypatch.setattr(mc, "is_present", lambda key, _s=None: key in here)

    def download(key, _settings, **_k):
        if key == "chat":
            raise OSError("the connection dropped")
        here.add(key)
        return model_fetch.DONE

    monkeypatch.setattr(mc, "download", download)
    assert run_cli(["models", "download", "all", "--env", cli_env]) == 1
    out = capsys.readouterr().out
    assert "chat: failed: the connection dropped" in out
    assert "Not on this computer: chat." in out
    assert here == set(mc.KEYS) - {"chat"}


def test_models_download_of_an_unknown_key_is_refused(cli_env, monkeypatch, capsys, no_fetch):
    assert run_cli(["models", "download", "everything", "--env", cli_env]) == 1
    assert "Choose from: search" in capsys.readouterr().out


def test_photo_tags_may_be_typed_with_an_underscore(cli_env, monkeypatch):
    """The installer's task is `models\\photo_tags` (Inno allows no "-"), and the
    same spelling should work on the command line."""
    asked: list = []
    monkeypatch.setattr(mc, "is_present", lambda key, _s=None: key in asked)
    monkeypatch.setattr(mc, "download", lambda key, _s, **_k: asked.append(key) or "done")
    assert run_cli(["models", "download", "photo_tags", "--env", cli_env]) == 0
    assert asked == ["photo-tags"]
