r"""Picture models ask for the threads the processor has left, read back from the session.

Layer: L0 (the envelope) and L2/L3 (the three model wrappers)

2026-10-10, work order model-sequencing item 1d. CLIP (`clip_embedder.py`),
RapidOCR (`ocr.py`) and insightface (`face_detect.py`) were left at
onnxruntime's default - one thread per physical core - on the `pictures`
worker and in the OCR helper, beside the extraction workers and the meaning
model, whose count the envelope already sized. Each now takes its count from
the same envelope (`envelope.picture_model_threads`), and leaves the library's
default when the envelope cannot describe the machine.

The acceptance asks for `intra_op_num_threads` to be *read back* from each
session. Where no real model is here, the fakes hold a real
`onnxruntime.SessionOptions` built from what the wrapper passed, exactly as the
library does; where the real model is here (RapidOCR always ships its own; the
face pack and CLIP when present), the real session is read.

The before-and-after measurement on an idle laptop (`pipeline_bench`) is owed
and is not something a unit test can stand in for.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.core import envelope

HAS_ORT = importlib.util.find_spec("onnxruntime") is not None
HAS_OCR = importlib.util.find_spec("rapidocr_onnxruntime") is not None
HAS_FACES = importlib.util.find_spec("insightface") is not None


def _owner_laptop() -> SimpleNamespace:
    """The owner's i7-1365U: two P-cores with hyper-threading, eight E-cores -
    ten cores, twelve logical processors. No graphics card, so every wrapper
    lands on the processor."""
    return SimpleNamespace(performance_cores=2, efficiency_cores=8, physical_cores=10,
                           logical_processors=12, ram_mb=32_000, gpus=(),
                           directml_available=False)


class _Unknown:
    """A machine nothing could be detected about."""

    gpus = ()
    directml_available = False


def _options(threads):
    """What fastembed / insightface / RapidOCR build from what they were given:
    a real SessionOptions with `intra_op_num_threads` set only when asked."""
    import onnxruntime

    options = onnxruntime.SessionOptions()
    if threads:
        options.intra_op_num_threads = int(threads)
    return options


class _Session:
    def __init__(self, options) -> None:
        self._options = options

    def get_session_options(self):
        return self._options


# --- the envelope ------------------------------------------------------------------

def test_the_owners_laptop_gets_what_the_workers_and_the_meaning_model_left():
    """12 logical processors - 4 extraction workers - 4 meaning-model threads = 4,
    and never more than the meaning model's own count (also 4 here)."""
    profile = _owner_laptop()
    assert envelope.index_workers(profile).auto == 4
    assert envelope.onnx_threads(profile).auto == 4

    assert envelope.picture_model_threads(profile) == 4


def test_extra_callers_of_one_session_are_taken_off_its_count():
    """The OCR helper reads four pictures side by side through one engine; the
    session's pool is shared and each caller works too, so 4 - 3 = 1."""
    assert envelope.picture_model_threads(_owner_laptop(), callers=4) == 1


def test_a_machine_nothing_is_known_about_keeps_the_library_default():
    assert envelope.picture_model_threads(_Unknown()) is None
    assert envelope.picture_model_threads(None) is None


def test_the_runs_own_numbers_are_used_when_the_caller_has_them():
    profile = _owner_laptop()
    # Eight hand-set workers and the meaning model at four leave nothing: the
    # floor of one, never zero or a negative count.
    assert envelope.picture_model_threads(profile, workers=8, meaning_threads=4) == 1
    # One worker and two meaning threads leave nine, but one session is never
    # given more than the envelope gives the meaning model with one worker.
    capped = envelope.onnx_threads(profile, 1).auto
    assert envelope.picture_model_threads(profile, workers=1, meaning_threads=2) == capped


def test_a_small_machine_never_gets_fewer_than_one_thread():
    dual = SimpleNamespace(physical_cores=2, logical_processors=2)
    assert envelope.picture_model_threads(dual) == 1
    assert envelope.picture_model_threads(dual, callers=4) == 1


def test_the_count_never_asks_for_more_than_the_processor_has():
    """The point of the item: workers + meaning model + one picture session fit
    the logical processors on every ordinary shape of machine."""
    shapes = [
        SimpleNamespace(physical_cores=4, logical_processors=8),
        SimpleNamespace(physical_cores=6, logical_processors=12),
        SimpleNamespace(physical_cores=8, logical_processors=16),
        _owner_laptop(),
    ]
    for profile in shapes:
        workers = envelope.index_workers(profile).auto
        meaning = envelope.onnx_threads(profile).auto
        pictures = envelope.picture_model_threads(profile)
        assert workers + meaning + pictures <= profile.logical_processors, profile


# --- CLIP ----------------------------------------------------------------------------

def _fake_image_embedding(seen: dict):
    class FakeImageEmbedding:
        """fastembed's shape: `threads=` becomes the session's intra-op count,
        and the session is at `.model.model`."""

        def __init__(self, model_name, cache_dir=None, **kwargs):
            seen["kwargs"] = kwargs
            self.model = SimpleNamespace(model=_Session(_options(kwargs.get("threads"))))

        def embed(self, images):
            return [[1.0] + [0.0] * 7 for _ in images]

    return FakeImageEmbedding


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_clip_asks_for_the_envelopes_count_and_the_session_holds_it(monkeypatch):
    from app.index.clip_embedder import ClipImageEmbedder

    seen: dict = {}
    monkeypatch.setattr("fastembed.ImageEmbedding", _fake_image_embedding(seen))

    embedder = ClipImageEmbedder(dim=8, device="cpu", profile=_owner_laptop())
    embedder.warm_up()

    assert seen["kwargs"].get("threads") == 4
    assert "providers" not in seen["kwargs"], "the processor path asks for no providers"
    assert embedder.threads == 4
    assert embedder.session_threads() == 4


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_clip_on_an_unknown_machine_is_built_as_before(monkeypatch):
    from app.index.clip_embedder import ClipImageEmbedder

    seen: dict = {}
    monkeypatch.setattr("fastembed.ImageEmbedding", _fake_image_embedding(seen))

    embedder = ClipImageEmbedder(dim=8, device="cpu", profile=_Unknown())
    embedder.warm_up()

    assert "threads" not in seen["kwargs"]
    assert embedder.threads is None
    assert embedder.session_threads() == 0, "onnxruntime's own default, untouched"


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_clip_takes_an_explicit_count_and_zero_means_the_library_default(monkeypatch):
    from app.index.clip_embedder import ClipImageEmbedder

    seen: dict = {}
    monkeypatch.setattr("fastembed.ImageEmbedding", _fake_image_embedding(seen))

    ClipImageEmbedder(dim=8, device="cpu", profile=_owner_laptop(), threads=2).warm_up()
    assert seen["kwargs"].get("threads") == 2

    ClipImageEmbedder(dim=8, device="cpu", profile=_owner_laptop(), threads=0).warm_up()
    assert "threads" not in seen["kwargs"]


def test_an_injected_encoder_has_no_session_to_read():
    from app.index.clip_embedder import ClipImageEmbedder

    embedder = ClipImageEmbedder(dim=8, encoder=lambda paths: [[1.0] + [0.0] * 7 for _ in paths])
    assert embedder.session_threads() is None


REAL_CACHE = os.environ.get("LEASHA_REAL_EMBED_CACHE", "")


@pytest.mark.slow
@pytest.mark.skipif(not REAL_CACHE,
                    reason="set LEASHA_REAL_EMBED_CACHE to a folder holding the CLIP model")
def test_the_real_clip_session_holds_the_envelopes_count(monkeypatch):
    """The real fastembed session, read back. Offline: `local_files_only`, so a
    cache without CLIP skips rather than downloads."""
    import fastembed

    from app.index.clip_embedder import ClipImageEmbedder

    real = fastembed.ImageEmbedding

    def offline(*args, **kwargs):
        try:
            return real(*args, local_files_only=True, **kwargs)
        except Exception as exc:                     # noqa: BLE001 - not here
            pytest.skip(f"CLIP is not in {REAL_CACHE}: {exc}")

    monkeypatch.setattr("fastembed.ImageEmbedding", offline)
    embedder = ClipImageEmbedder(cache_dir=REAL_CACHE, device="cpu", profile=_owner_laptop())
    embedder.warm_up()

    assert embedder.session_threads() == 4


# --- RapidOCR -------------------------------------------------------------------------

@pytest.fixture
def fresh_ocr(monkeypatch):
    """An OCR module with no engine yet, on the owner's laptop, on the processor."""
    from app.extract import ocr

    monkeypatch.setattr(ocr, "_engine", None)
    monkeypatch.setattr(ocr, "_engine_failed", False)
    monkeypatch.setattr(ocr, "_engine_attempts", 0)
    monkeypatch.setattr(ocr, "_engine_is_gpu", False)
    monkeypatch.setattr(ocr, "_device", "cpu")
    monkeypatch.setattr(ocr, "_callers", 1)
    monkeypatch.setattr(ocr, "_profile", _owner_laptop)
    return ocr


def _fake_rapidocr(monkeypatch, built: list):
    class FakeRapidOCR:
        """RapidOCR 1.4.4's shape: `intra_op_num_threads` goes to all three
        sessions; each is wrapped in an `OrtInferSession` held at
        `text_det.infer`, `text_cls.infer` and `text_rec.session`."""

        def __init__(self, **kwargs):
            built.append(kwargs)
            for part, held_at in (("text_det", "infer"), ("text_cls", "infer"),
                                  ("text_rec", "session")):
                session = _Session(_options(kwargs.get("intra_op_num_threads")))
                wrapper = SimpleNamespace(session=session)
                setattr(self, part, SimpleNamespace(**{held_at: wrapper}))

    fake = types.ModuleType("rapidocr_onnxruntime")
    fake.RapidOCR = FakeRapidOCR
    monkeypatch.setitem(sys.modules, "rapidocr_onnxruntime", fake)


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_ocr_asks_for_the_envelopes_count_on_all_three_sessions(fresh_ocr, monkeypatch):
    built: list = []
    _fake_rapidocr(monkeypatch, built)

    assert fresh_ocr._load_engine() is not None

    assert built == [{"intra_op_num_threads": 4}]
    assert fresh_ocr.session_threads() == [4, 4, 4]


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_the_ocr_helpers_four_callers_are_taken_off(fresh_ocr, monkeypatch):
    from app.index import ocr_process

    built: list = []
    _fake_rapidocr(monkeypatch, built)

    ocr_process._size_engine()
    assert fresh_ocr._callers == ocr_process.HELPER_THREADS
    fresh_ocr._load_engine()

    assert fresh_ocr.session_threads() == [1, 1, 1]


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_ocr_on_an_unknown_machine_is_built_as_before(fresh_ocr, monkeypatch):
    built: list = []
    _fake_rapidocr(monkeypatch, built)
    monkeypatch.setattr(fresh_ocr, "_profile", _Unknown)

    fresh_ocr._load_engine()

    assert built == [{}], "no new argument at all"
    assert fresh_ocr.session_threads() == [0, 0, 0]


@pytest.mark.slow
@pytest.mark.skipif(not HAS_OCR, reason="OCR is not installed")
def test_the_real_rapidocr_sessions_hold_the_envelopes_count(fresh_ocr):
    """RapidOCR ships its three models inside the package, so the real sessions
    can always be read back here. It ignores a count above `os.cpu_count()`."""
    fresh_ocr._load_engine()
    expected = 4 if (os.cpu_count() or 1) >= 4 else 0

    assert fresh_ocr.session_threads() == [expected] * 3


# --- insightface ----------------------------------------------------------------------

@pytest.fixture
def fresh_faces(monkeypatch):
    from app.core import compute_profile
    from app.extract import face_detect
    from app.index import backends

    monkeypatch.setattr(face_detect, "_engine", None)
    monkeypatch.setattr(face_detect, "_engine_failed", False)
    monkeypatch.setattr(face_detect, "_engine_attempts", 0)
    monkeypatch.setattr(face_detect, "_engine_is_gpu", False)
    monkeypatch.setattr(face_detect, "_choice", lambda: backends.choose(None, backends.CPU))
    monkeypatch.setattr(compute_profile, "detect", lambda *a, **k: _owner_laptop())
    return face_detect


def _fake_insightface(monkeypatch, built: list):
    class FakeFaceAnalysis:
        """insightface's shape: `sess_options` reaches every session `model_zoo`
        builds, each at `models[task].session`."""

        def __init__(self, name, **kwargs):
            built.append(kwargs)
            options = kwargs.get("sess_options") or _options(None)
            self.models = {task: SimpleNamespace(session=_Session(options))
                           for task in ("detection", "recognition")}

        def prepare(self, ctx_id, det_size):
            pass

    package = types.ModuleType("insightface")
    app = types.ModuleType("insightface.app")
    app.FaceAnalysis = FakeFaceAnalysis
    package.app = app
    monkeypatch.setitem(sys.modules, "insightface", package)
    monkeypatch.setitem(sys.modules, "insightface.app", app)


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_faces_pass_session_options_with_the_envelopes_count(fresh_faces, monkeypatch):
    built: list = []
    _fake_insightface(monkeypatch, built)

    assert fresh_faces._load() is not None

    assert built[0]["providers"] == ["CPUExecutionProvider"]
    assert built[0]["sess_options"].intra_op_num_threads == 4
    assert fresh_faces.session_threads() == [4, 4]


@pytest.mark.skipif(not HAS_ORT, reason="onnxruntime is not installed")
def test_faces_on_an_unknown_machine_are_built_as_before(fresh_faces, monkeypatch):
    from app.core import compute_profile

    built: list = []
    _fake_insightface(monkeypatch, built)
    monkeypatch.setattr(compute_profile, "detect", lambda *a, **k: _Unknown())

    fresh_faces._load()

    assert "sess_options" not in built[0]
    assert fresh_faces.session_threads() == [0, 0]


_PACK = Path(os.path.expanduser("~/.insightface")) / "models" / "buffalo_l"


@pytest.mark.slow
@pytest.mark.skipif(not (HAS_FACES and _PACK.is_dir()),
                    reason="insightface and its buffalo_l pack are not both here")
def test_the_real_face_pack_sessions_hold_the_envelopes_count(fresh_faces):
    """The real `FaceAnalysis`, read back - proof that `sess_options` really does
    reach the sessions insightface builds inside `model_zoo`."""
    assert fresh_faces._load() is not None

    found = fresh_faces.session_threads()
    assert found and all(count == 4 for count in found), found
