r"""Offline places: EXIF GPS to a town name, no network. Work order 0i 4a.

Layer: L2 and L1

app/extract/exif.py::read_gps and app/extract/places.py::reverse_geocode
are proven directly here, including the no-network requirement the item's
own §5 test list names. The /place operator (parsing, filter SQL, the
distinct-values vocabulary) is proven against a real SqliteStore, the same
shape tests/unit/test_photo_tags.py already uses for /shows.
"""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import pytest

from app.extract.exif import read_gps


def _gps_photo(path: Path, lat_ref="N", lat_dms=None, lon_ref="W", lon_dms=None):
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    exif = Image.Exif()
    gps_ifd = dict()
    if lat_dms is not None:
        gps_ifd[1] = lat_ref
        gps_ifd[2] = lat_dms
        gps_ifd[3] = lon_ref
        gps_ifd[4] = lon_dms
        exif[34853] = gps_ifd
    Image.new("RGB", (64, 64), "white").save(path, exif=exif)
    return path


LONDON_LAT = (Fraction(51), Fraction(30), Fraction(2664, 100))
LONDON_LON = (Fraction(0), Fraction(7), Fraction(4008, 100))


def test_read_gps_extracts_a_real_coordinate(tmp_path):
    photo = _gps_photo(tmp_path / "london.jpg", lat_dms=LONDON_LAT, lon_dms=LONDON_LON)
    lat, lon = read_gps(photo)
    assert lat == pytest.approx(51.5074, abs=1e-3)
    assert lon == pytest.approx(-0.1278, abs=1e-3)


def test_read_gps_signs_south_and_west_correctly(tmp_path):
    # Sydney: 33.8688 S, 151.2093 E
    lat_dms = (Fraction(33), Fraction(52), Fraction(768, 100))
    lon_dms = (Fraction(151), Fraction(12), Fraction(3348, 100))
    photo = _gps_photo(tmp_path / "sydney.jpg", lat_ref="S", lat_dms=lat_dms,
                        lon_ref="E", lon_dms=lon_dms)
    lat, lon = read_gps(photo)
    assert lat < 0
    assert lon > 0


def test_read_gps_returns_none_with_no_gps_block(tmp_path):
    photo = _gps_photo(tmp_path / "plain.jpg")
    assert read_gps(photo) is None


def test_read_gps_never_raises_on_a_corrupt_file(tmp_path):
    broken = tmp_path / "broken.jpg"
    broken.write_bytes(b"not a jpeg")
    assert read_gps(broken) is None


# ---------------------------------------------------------------------------
# Offline reverse geocoding - real, not mocked (0i section 5's own test:
# "no network syscalls in the geocode path (asserted)")
# ---------------------------------------------------------------------------

@pytest.mark.slow
def test_a_real_coordinate_resolves_to_a_real_place():
    # Optional (requirements.txt says "Install to turn a photo's GPS into a place
    # name"): skipped, and said, where it is not installed (2026-10-05, the
    # first Linux run on the laptop had it missing and failed instead).
    pytest.importorskip("reverse_geocoder")
    from app.extract.places import reverse_geocode

    place = reverse_geocode(51.5074, -0.1278)
    assert place == "London"


@pytest.mark.slow
def test_reverse_geocode_makes_no_network_call(monkeypatch):
    # Optional (requirements.txt says "Install to turn a photo's GPS into a place
    # name"): skipped, and said, where it is not installed (2026-10-05, the
    # first Linux run on the laptop had it missing and failed instead).
    pytest.importorskip("reverse_geocoder")
    import socket

    def blocked_connect(self, *a, **k):
        raise AssertionError("reverse_geocode attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", blocked_connect)

    from app.extract.places import reverse_geocode

    place = reverse_geocode(53.8008, -1.5491)  # Leeds
    assert place == "Leeds"


def test_reverse_geocode_never_raises_on_nonsense_input():
    from app.extract.places import reverse_geocode

    assert reverse_geocode(999.0, 999.0) is None or isinstance(
        reverse_geocode(999.0, 999.0), str)


def test_available_never_raises():
    from app.extract.places import available

    assert available() in (True, False)


# ---------------------------------------------------------------------------
# Storage: schema v21, upsert_file, distinct_value_counts
# ---------------------------------------------------------------------------

from app.storage.migrations import CURRENT_VERSION
from app.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def test_schema_v21_adds_the_place_column(store):
    assert CURRENT_VERSION >= 21
    columns = set(row[1] for row in store.conn.execute("PRAGMA table_info(files)"))
    assert "place" in columns


def test_upsert_file_stores_the_place(store):
    file_id = store.upsert_file(
        path="/p/london.jpg", size_bytes=1, mtime_ns=1, source_kind="file",
        place="London")
    row = store.get_file_by_id(file_id)
    assert row.place == "London"


def test_a_caller_that_knows_nothing_about_place_does_not_blank_it(store):
    path = "/p/london.jpg"
    store.upsert_file(path=path, size_bytes=1, mtime_ns=1, source_kind="file",
                       place="London")
    store.upsert_file(path=path, size_bytes=2, mtime_ns=2, source_kind="file")
    assert store.get_file(path).place == "London"


# ---------------------------------------------------------------------------
# /place: parsing, filter SQL, the vocabulary - real SQLite, no mocks
# ---------------------------------------------------------------------------

from app.search.commands import command_for, expand_slashes
from app.search.query import parse_query
from app.storage.filters import file_filter_sql


def test_place_is_a_real_offered_command():
    command = command_for("place")
    assert command is not None
    assert command.source == "place"
    assert expand_slashes("/place leeds") == "place:leeds"
    assert expand_slashes("/near leeds") == "place:leeds"


def test_the_vocabulary_is_browsable_with_counts(store):
    a = store.upsert_file(path="/p/a.jpg", size_bytes=1, mtime_ns=1,
                           source_kind="file", place="Leeds")
    b = store.upsert_file(path="/p/b.jpg", size_bytes=1, mtime_ns=1,
                           source_kind="file", place="Leeds")
    c = store.upsert_file(path="/p/c.jpg", size_bytes=1, mtime_ns=1,
                           source_kind="file", place="York")
    counts = dict((row.value, row.count) for row in store.distinct_value_counts("place"))
    assert counts["Leeds"] == 2
    assert counts["York"] == 1


def test_a_photo_is_found_by_its_place_end_to_end(store):
    leeds = store.upsert_file(path="/p/leeds.jpg", size_bytes=1, mtime_ns=1,
                               source_kind="file", place="Leeds")
    york = store.upsert_file(path="/p/york.jpg", size_bytes=1, mtime_ns=1,
                              source_kind="file", place="York")

    parsed = parse_query(expand_slashes("/place leeds"))
    assert parsed.place == ("leeds",)
    assert parsed.has_filters

    where, params = file_filter_sql(parsed)
    rows = store.conn.execute(
        "SELECT id FROM files f WHERE 1=1 " + where, params).fetchall()
    ids = set(row["id"] for row in rows)
    assert ids == set([leeds])


def test_a_file_with_no_place_is_not_excluded_by_a_negated_place(store):
    leeds = store.upsert_file(path="/p/leeds.jpg", size_bytes=1, mtime_ns=1,
                               source_kind="file", place="Leeds")
    unplaced = store.upsert_file(path="/p/doc.txt", size_bytes=1, mtime_ns=1,
                                  source_kind="file")

    parsed = parse_query("-place:leeds")
    where, params = file_filter_sql(parsed)
    rows = store.conn.execute(
        "SELECT id FROM files f WHERE 1=1 " + where, params).fetchall()
    ids = set(row["id"] for row in rows)
    assert ids == set([unplaced])


# ---------------------------------------------------------------------------
# Wiring into Pipeline._photo_place - mocked, so this runs in milliseconds
# ---------------------------------------------------------------------------

def _pipeline():
    import tempfile
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig

    tmp = Path(tempfile.mkdtemp())
    store = SqliteStore(tmp / "index.db")

    class _V:
        def ensure_table(self):
            pass

    return Pipeline(store, _V(), Embedder(dim=8, encoder=lambda t: [[0.0] * 8 for _ in t]),
                     PipelineConfig(walk=WalkConfig(roots=[tmp])))


def test_photo_place_uses_gps_and_the_geocoder(monkeypatch):
    from app.index.walker import Candidate

    pipeline = _pipeline()
    monkeypatch.setattr("app.extract.exif.read_gps", lambda path: (51.5, -0.1))
    monkeypatch.setattr("app.extract.places.available", lambda: True)
    monkeypatch.setattr("app.extract.places.reverse_geocode",
                         lambda lat, lon: "London")

    candidate = Candidate(path=Path("D:/Photos/trip.jpg"), size_bytes=1, mtime_ns=1)
    assert pipeline._photo_place(candidate) == "London"


def test_photo_place_is_none_with_no_gps(monkeypatch):
    from app.index.walker import Candidate

    pipeline = _pipeline()
    monkeypatch.setattr("app.extract.exif.read_gps", lambda path: None)
    monkeypatch.setattr("app.extract.places.available", lambda: True)

    candidate = Candidate(path=Path("D:/Photos/trip.jpg"), size_bytes=1, mtime_ns=1)
    assert pipeline._photo_place(candidate) is None


def test_photo_place_is_none_when_the_geocoder_is_unavailable(monkeypatch):
    from app.index.walker import Candidate

    pipeline = _pipeline()
    monkeypatch.setattr("app.extract.exif.read_gps", lambda path: (51.5, -0.1))
    monkeypatch.setattr("app.extract.places.available", lambda: False)

    candidate = Candidate(path=Path("D:/Photos/trip.jpg"), size_bytes=1, mtime_ns=1)
    assert pipeline._photo_place(candidate) is None


def test_a_non_image_gets_no_place(monkeypatch):
    from app.index.walker import Candidate

    pipeline = _pipeline()
    monkeypatch.setattr("app.extract.places.available",
                         lambda: (_ for _ in ()).throw(AssertionError(
                             "must never even be asked for a non-image")))

    candidate = Candidate(path=Path("D:/Docs/report.pdf"), size_bytes=1, mtime_ns=1)
    assert pipeline._photo_place(candidate) is None
