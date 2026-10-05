r"""Work order 0h §4, first item: the picture lane, wired end to end.

**What each test here is, said plainly, because only one of them closes the
item.**

The item asks for two things: a fixture photo of a distinctive scene found by
a text description with zero matching filename or text, and a lane failure
degrading to keyword + text-vector results plus a notice.

- `test_a_photo_is_found_by_typing_a_description_of_it` is the item's first
  half and **the only test in this file that proves it**. It loads the real
  `Qdrant/clip-ViT-B-32-vision` and `Qdrant/clip-ViT-B-32-text` towers, runs a
  real `Pipeline` over real image files, writes to a real LanceDB
  `ImageVectorStore`, and searches through the real `SearchEngine.search()`.
  Nothing about CLIP is faked. It **skips** where those two models cannot be
  loaded, naming the exact failure - see `_clip_towers_or_reason` - because a
  test that quietly asserted something smaller in that case would be a green
  tick over an unproved claim, which is worse here than an honest skip.
- `test_the_lane_carries_a_photo_from_index_to_result_with_stand_in_vectors`
  runs the identical path - real `Pipeline`, real LanceDB table, real
  `SearchEngine.search()`, real fusion, real hydration - with a deterministic
  stand-in for the two towers that reads the fixture's actual pixels. It
  proves the *plumbing* on a machine with no model, and it proves nothing at
  all about CLIP. It does not close the item and must not be read as doing so.
- `test_a_broken_picture_lane_still_returns_keyword_and_text_vector_results`
  is the item's second half, and needs no model.
  `test_engine_image_lane.py::test_a_broken_image_lane_degrades_with_a_notice_
  not_a_crash` already pins the notice, but against an **empty** text-vector
  store, so it can only assert `results == []`. The sentence this item
  actually writes down - "lane failure -> keyword+text-vector results +
  notice" - needs the other two lanes to have something to return, which is
  what this one gives them.

The photo/query pairing below is a synthetic colour field, not a photograph.
No real photograph ships in this repository, and inventing a claim about what
CLIP does with one that nobody has run would be the wrong kind of confidence:
colour is the one property a `PIL.Image.new` fixture genuinely carries, so
that is what the description asks for.
"""

from __future__ import annotations

import math
import os
import time
from pathlib import Path
from typing import Any, ClassVar, Optional, Sequence

import pytest

from app.extract import ocr as ocr_module
from app.index.clip_embedder import CLIP_IMAGE_DIM, ClipImageEmbedder
from app.index.embedder import Embedder, l2_normalise
from app.index.pipeline import Pipeline, PipelineConfig
from app.index.walker import WalkConfig
from app.search.engine import (
    NOTICE_NO_IMAGES,
    NOTICE_NO_VECTORS,
    SearchEngine,
)
from app.search.vector import CLIP_TEXT_DIM, CLIP_TEXT_MODEL
from app.storage.sqlite_store import SqliteStore
from app.storage.vector_store import ImageVectorStore, VectorStore
from tests.unit.test_search_images import ExplodingEmbedder

pytest.importorskip("lancedb")
pytest.importorskip("PIL", reason="Pillow draws this file's fixture images")

#: Long enough ago that the walker treats the fixtures as settled files.
LONG_AGO = 3600

#: The text-vector lane's width in this file. Deliberately not 384: nothing
#: here loads the FastEmbed text model, and a made-up encoder is clearer at a
#: width no real model uses.
TEXT_DIM = 8

#: A description with no word in common with either fixture's filename, and no
#: text anywhere in the corpus - so a hit can only have come from the picture
#: lane.
RED_DESCRIPTION = "a photograph of a bright red wall filling the whole frame"

#: Its opposite number. Two queries rather than one because "the red fixture
#: came back first" is a coin toss on a two-row table read once - only the
#: order **flipping** with the description shows the ranking is driven by what
#: was typed rather than by insertion order.
BLUE_DESCRIPTION = "a photograph of a deep blue wall filling the whole frame"


# --------------------------------------------------------------------------
# Fixtures on disk
# --------------------------------------------------------------------------


def _colour_photo(path: Path, colour: tuple[int, int, int]) -> Path:
    """One flat-colour image, named so the name says nothing about it."""
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (256, 256), color=colour).save(path)
    stamp = time.time() - LONG_AGO
    os.utime(path, (stamp, stamp))
    return path


class _NullTextVectors:
    """The text table, switched off. Every result must come from the picture
    table, so the other vector lane is given nothing it could contribute."""

    def delete_by_file_ids(self, file_ids: Sequence[int]) -> None:
        pass

    def add(self, **kwargs: Any) -> int:
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name: str):
        return lambda *args, **kwargs: None


class _EmptyVectorStore:
    """`VectorStore`'s search surface, always empty - the search-side twin of
    `_NullTextVectors`."""

    def search(self, vector, *, k: int = 100, where=None):
        return []


class _NoOpEmbedder:
    """The FastEmbed text model, never asked for anything real."""

    def embed(self, texts):
        return [[0.0] for _ in texts]

    def warm_up(self) -> None:
        pass


def _made_up_text_embedder(dim: int = TEXT_DIM) -> Embedder:
    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(dim)])
            for t in texts
        ]

    return Embedder(dim=dim, encoder=encode)


@pytest.fixture()
def ocr_that_reads_nothing(monkeypatch):
    """Zero OCR text for every fixture - the item's "no matching text" half,
    made true at the source rather than assumed."""
    monkeypatch.setattr(ocr_module, "available", lambda: True)
    monkeypatch.setattr(ocr_module, "_load_engine", lambda: (lambda _s: ([], 0.0)))
    # And no AI caption either. A photo with no text used to be the end of the
    # line; with Florence installed (order 0i) it now gets a caption and is
    # *indexed* - so on a machine that has it this file's premise (the skip
    # ledger) was false and a real captioning model was loaded to find that out.
    # The same seam `test_ocr.py` uses (2026-09-20).
    from app.extract import florence_tagger

    monkeypatch.setattr(florence_tagger, "available", lambda: False)


def _assert_both_photos_reached_the_picture_lane(stats, images) -> None:
    r"""The indexing half, asserted against what the pipeline actually does.

    **Not `stats.indexed == 2`, and the reason is worth stating** - it was
    checked against a real run rather than assumed. A photograph with no
    readable text is `ERR_NO_TEXT_LAYER`, which lands in the *skip* ledger:
    `IndexStats.indexed` stays 0 and `skipped` counts both fixtures. That is
    correct and deliberate - `Pipeline._record_skip` calls
    `_maybe_embed_image` for exactly this code, with a comment saying why -
    and it is the ordinary case for a photo corpus, not an edge one. A test
    that demanded `indexed == 2` here would be asserting the opposite of this
    order's own design.
    """
    assert stats.seen == 2, "the walker never saw the fixtures"
    assert stats.skipped_by_code.get("ERR_NO_TEXT_LAYER") == 2, (
        "a photo with no text should land in the skip ledger under this code "
        "- if that changed, this file's premise needs re-reading")
    assert images.count() == 2, (
        "an uncaptioned photograph must still get a CLIP vector - the whole "
        "point of _record_skip's ERR_NO_TEXT_LAYER branch")


def _index(root: Path, store: SqliteStore, *, image_embedder, image_vectors):
    """One real `Pipeline` run over `root`, picture lane configured."""
    pipeline = Pipeline(
        store, _NullTextVectors(), _made_up_text_embedder(),
        PipelineConfig(walk=WalkConfig(roots=[root]), workers=1,
                       ocr_mode="images"),
        image_embedder=image_embedder, image_vectors=image_vectors,
    )
    return pipeline.run()


# --------------------------------------------------------------------------
# The real models, or an honest skip
# --------------------------------------------------------------------------

_TOWERS: Optional[tuple[Any, Any]] = None
_TOWER_REASON: Optional[str] = None


def _clip_towers_or_reason(cache_dir: Optional[str] = None):
    """Both CLIP towers, loaded once, or the reason they cannot be.

    Loading is the availability check - there is no lighter question worth
    asking. FastEmbed being importable says nothing about whether the ONNX
    weights are on this machine or reachable from it, and a test that skipped
    on the import alone would run on a machine that then failed to download
    and would fail for a reason unrelated to the lane.

    The reason is returned verbatim (`ERR_MODEL_LOAD`'s `details` carries the
    model name and the underlying exception) so the skip line names what is
    missing rather than saying "model unavailable".
    """
    global _TOWERS, _TOWER_REASON
    if _TOWERS is not None or _TOWER_REASON is not None:
        return _TOWERS, _TOWER_REASON

    def _why(exc: Exception) -> str:
        error = getattr(exc, "error", None)
        return str(getattr(error, "details", "") or exc)

    try:
        vision = ClipImageEmbedder(cache_dir=cache_dir)
        vision.warm_up()
    except Exception as exc:                    # noqa: BLE001 - download/disk/ONNX
        _TOWER_REASON = (
            f"the real CLIP vision tower could not be loaded here: {_why(exc)}")
        return None, _TOWER_REASON

    try:
        text = Embedder(CLIP_TEXT_MODEL, dim=CLIP_TEXT_DIM, cache_dir=cache_dir)
        text.embed(["a warm-up sentence"])
    except Exception as exc:                    # noqa: BLE001 - download/disk/ONNX
        _TOWER_REASON = (
            f"the real CLIP text tower could not be loaded here: {_why(exc)}")
        return None, _TOWER_REASON

    _TOWERS = (vision, text)
    return _TOWERS, None


@pytest.fixture(scope="module")
def clip_towers(tmp_path_factory):
    towers, reason = _clip_towers_or_reason(
        cache_dir=str(tmp_path_factory.mktemp("model_cache")))
    if towers is None:
        pytest.skip(reason)
    return towers


# --------------------------------------------------------------------------
# The item's first half, on the real models
# --------------------------------------------------------------------------


@pytest.mark.slow
def test_a_photo_is_found_by_typing_a_description_of_it(
    tmp_path, clip_towers, ocr_that_reads_nothing,
):
    r"""Work order 0h's acceptance sentence, search half, nothing faked.

    Two fixtures are indexed by a real `Pipeline` through the real CLIP
    vision tower into a real LanceDB table. `IMG_0001` is red, `IMG_0002` is
    blue; neither filename contains a word of the query, neither file has a
    single character of text, and the text-vector lane is empty by
    construction. A result appearing at all therefore came from the picture
    lane, and the red one appearing **first** is the lane doing its job
    rather than returning the table in arbitrary order.

    Two assertions, deliberately separate, because they fail for different
    reasons and a single combined one would hide which: the first is the
    wiring (a photo reaches a `SearchResult` from a typed description at
    all), the second is the model discriminating between two fixtures.
    """
    vision, text_tower = clip_towers
    root = tmp_path / "photos"
    red = _colour_photo(root / "IMG_0001.png", (220, 20, 20))
    blue = _colour_photo(root / "IMG_0002.png", (20, 20, 220))

    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=CLIP_IMAGE_DIM) as images:
        stats = _index(root, store, image_embedder=vision, image_vectors=images)
        _assert_both_photos_reached_the_picture_lane(stats, images)

        engine = SearchEngine(
            store, _EmptyVectorStore(), _NoOpEmbedder(),
            image_vectors=images, clip_text_embedder=text_tower,
        )
        try:
            response = engine.search(RED_DESCRIPTION)
            other = engine.search(BLUE_DESCRIPTION)
        finally:
            engine.close()

    assert response.image_count >= 1, (
        "a typed description returned nothing from the picture lane")
    paths = [result.path for result in response.results]
    assert str(red) in paths, (
        "the described photo was not found by describing it")
    assert str(blue) in paths, (
        "the blue fixture is the control - it should still be in the table")
    assert not any(n.code == NOTICE_NO_IMAGES for n in response.notices)

    assert paths[0] == str(red), (
        f"the red fixture must outrank the blue one for {RED_DESCRIPTION!r}; "
        f"got {paths}")
    other_paths = [result.path for result in other.results]
    assert other_paths[:1] == [str(blue)], (
        f"the order must follow the description - {BLUE_DESCRIPTION!r} put "
        f"{other_paths} back, which means the ranking is not coming from "
        f"what was typed")


# --------------------------------------------------------------------------
# The same path with stand-in vectors: plumbing only, no model
# --------------------------------------------------------------------------


class _MeanColourVision:
    """`ClipImageEmbedder`'s shape over the fixture's actual pixels.

    Not a CLIP model and never described as one. It exists so the real
    `Pipeline` -> real `ImageVectorStore` -> real `SearchEngine.search()`
    path can be exercised on a machine with no model at all. The vector is a
    function of the **image content**, not of the path, so an image swap
    changes the answer the way a real embedder would.
    """

    def embed(self, paths):
        from PIL import Image

        out = []
        for path in paths:
            # 2026-10-05: the pipeline hands a decoded picture (one decode per
            # photo, `extract.picture`), as the real `ClipImageEmbedder` takes.
            if hasattr(path, "convert"):
                small = path.convert("RGB").resize((1, 1))
                red, green, blue = small.getpixel((0, 0))
                out.append(_colour_vector(red, green, blue))
                continue
            with Image.open(str(path)) as frame:
                small = frame.convert("RGB").resize((1, 1))
                red, green, blue = small.getpixel((0, 0))
            out.append(_colour_vector(red, green, blue))
        return out

    def warm_up(self) -> None:
        pass


class _ColourWordText:
    """The query side of the same stand-in: a colour word becomes the vector
    a photo of that colour would land on."""

    WORDS: ClassVar[dict[str, tuple[int, int, int]]] = {
        "red": (255, 0, 0), "green": (0, 255, 0), "blue": (0, 0, 255)}

    def embed(self, texts):
        out = []
        for text in texts:
            lowered = str(text).lower()
            channels = next(
                (rgb for word, rgb in self.WORDS.items() if word in lowered),
                (1, 1, 1),
            )
            out.append(_colour_vector(*channels))
        return out

    def warm_up(self) -> None:
        pass


def _colour_vector(red: int, green: int, blue: int) -> list[float]:
    """A unit vector at the real table's width, carrying colour in 3 of 512."""
    values = [float(red), float(green), float(blue)] + [0.0] * (CLIP_IMAGE_DIM - 3)
    return l2_normalise(values)


def test_the_lane_carries_a_photo_from_index_to_result_with_stand_in_vectors(
    tmp_path, ocr_that_reads_nothing,
):
    r"""Everything between the two towers, on a machine with no model.

    **This does not close §4's first item** - see this module's docstring.
    What it does prove, and what nothing in the suite proved before it, is
    that the pieces between the model and the answer actually join up on real
    components rather than fakes: `Pipeline._maybe_embed_image` writing
    through `_flush_pending_images` into a real 512-wide LanceDB table, that
    table's ANN search reached from `SearchEngine.search()`, the
    `"img:<file_id>"` namespacing surviving `fuse_hits`, `hydrate_images`
    joining `files` for a photo that has no `chunks` row, and a `SearchResult`
    coming back with the right path.

    `test_engine_image_lane.py` proves the engine's side of that against a
    `FakeImageVectorStore` and no pipeline at all; this is the same claim
    with the fakes taken out from under it.
    """
    root = tmp_path / "photos"
    red = _colour_photo(root / "IMG_0001.png", (255, 0, 0))
    blue = _colour_photo(root / "IMG_0002.png", (0, 0, 255))

    with SqliteStore(tmp_path / "index.db") as store, \
         ImageVectorStore(tmp_path / "img_vectors", dim=CLIP_IMAGE_DIM) as images:
        stats = _index(root, store, image_embedder=_MeanColourVision(),
                       image_vectors=images)
        _assert_both_photos_reached_the_picture_lane(stats, images)

        engine = SearchEngine(
            store, _EmptyVectorStore(), _NoOpEmbedder(),
            image_vectors=images, clip_text_embedder=_ColourWordText(),
        )
        try:
            response = engine.search(RED_DESCRIPTION)
            other = engine.search(BLUE_DESCRIPTION)
        finally:
            engine.close()

    assert response.image_count == 2, "both photos should reach the results"
    paths = [result.path for result in response.results]
    assert paths[0] == str(red)
    assert [result.path for result in other.results][:1] == [str(blue)], (
        "the order must follow the description, not insertion order")
    result = response.results[0]
    assert result.chunk_id == result.file_id, (
        "the namespaced fused key must have been resolved back to a plain int")
    assert not any(n.code == NOTICE_NO_IMAGES for n in response.notices)


# --------------------------------------------------------------------------
# The item's second half: H4, with the other two lanes holding something
# --------------------------------------------------------------------------


def test_a_broken_picture_lane_still_returns_keyword_and_text_vector_results(
    tmp_path,
):
    r"""The item's own words: "lane failure -> keyword+text-vector results +
    notice".

    The existing engine test for a broken picture lane runs against an empty
    text-vector store, so the strongest thing it can say is that the search
    did not crash. Here both other lanes have real work to do - a real
    `SqliteStore` with real chunks behind FTS5, and a real `VectorStore` with
    real rows - so "the other two lanes still answer" is an assertion about
    results rather than about the absence of an exception.
    """
    embedder = _made_up_text_embedder()
    store = SqliteStore(tmp_path / "index.db").connect()
    vectors = VectorStore(tmp_path / "vectors", dim=TEXT_DIM).connect()
    vectors.ensure_table()

    chunk_ids: list[int] = []
    file_ids: list[int] = []
    texts: list[str] = []
    for n in range(4):
        file_id = store.upsert_file(path=f"/docs/report{n}.txt", size_bytes=100,
                                    mtime_ns=n, source_kind="file")
        body = "the northern pump station was commissioned in March"
        new_ids = store.replace_chunks(file_id, [{"text": body}])
        chunk_ids.extend(new_ids)
        file_ids.extend([file_id] * len(new_ids))
        texts.extend([body] * len(new_ids))
    vectors.add(chunk_ids, file_ids, embedder.embed(texts))

    engine = SearchEngine(
        store, vectors, embedder,
        image_vectors=object(),                 # never reached: the tower dies first
        clip_text_embedder=ExplodingEmbedder(),
    )
    try:
        response = engine.search("pump station")
    finally:
        engine.close()
        vectors.close()
        store.close()

    assert response.results, (
        "a broken picture lane took the whole search with it - the exact H4 "
        "failure this order forbids")
    assert response.keyword_count > 0, "the keyword lane must still answer"
    assert response.vector_count > 0, "the text-vector lane must still answer"
    assert response.image_count == 0

    codes = [notice.code for notice in response.notices]
    assert NOTICE_NO_IMAGES in codes, "a broken picture lane must say so"
    assert NOTICE_NO_VECTORS not in codes, (
        "the text-vector lane worked - conflating the two failures under one "
        "code is what NOTICE_NO_IMAGES exists to prevent")
