r"""Folder-year era hints. Work order 0i section 4b.

Layer: L2

Pure logic, no I/O beyond stat-free path string handling - these run in
milliseconds and never touch a model, a database or the OCR ladder. The
end-to-end wiring (EXIF beats a hint, a hint beats mtime, taken_at_is_hint
set correctly through both write_one and record_skip) was verified directly
against a real Pipeline this session - see the dated note on this item in
the work order for the full account, including why that verification is
not itself committed as a test (a real Florence-2 load adds 20-70s to any
scenario touching an OCR-empty image once torch/transformers are
installed, which every test in this file must not pay for what it is not
testing).
"""

from __future__ import annotations

from pathlib import Path

from app.extract.era_hints import guess_year, year_to_epoch_ns


def test_a_deliberate_album_name_is_found():
    assert guess_year(Path("D:/Photos/Diwali 2004/IMG_0007.jpg")) == 2004


def test_an_underscore_separated_year_is_found():
    assert guess_year(Path("D:/Photos/Summer_1999/scan01.jpg")) == 1999


def test_the_folder_wins_over_the_filename():
    path = Path("D:/Photos/Diwali 2004/scan_1987.jpg")
    assert guess_year(path) == 2004


def test_the_filename_is_used_when_the_folder_offers_nothing():
    path = Path("D:/Photos/misc/scan_2004_0007.jpg")
    assert guess_year(path) == 2004


def test_a_cameras_own_counter_is_not_mistaken_for_a_year():
    """img20045.jpg - "2004" glued to a fifth digit, a camera's own
    numbering, not a year anyone typed on purpose."""
    assert guess_year(Path("D:/Photos/misc/img20045.jpg")) is None


def test_a_name_with_no_year_at_all_finds_nothing():
    assert guess_year(Path("D:/Photos/misc/wedding.jpg")) is None


def test_a_year_beyond_the_current_one_is_rejected():
    assert guess_year(Path("D:/Photos/misc/photo_3000.jpg")) is None


def test_photographys_own_early_years_are_accepted():
    """A folder named for a decade, not just a single year - "1850s" still
    offers 1850, the earliest plausible photograph, not excluded by a range
    that only thought about last century."""
    assert guess_year(Path("D:/Photos/1850s/tintype.jpg")) == 1850


def test_year_to_epoch_ns_is_the_first_of_january_utc():
    import datetime

    ns = year_to_epoch_ns(2004)
    back = datetime.datetime.fromtimestamp(ns / 1_000_000_000, tz=datetime.timezone.utc)
    assert (back.year, back.month, back.day) == (2004, 1, 1)


def test_guess_year_never_raises():
    # A path this module never has to open - only the string is read - so
    # a nonexistent file is not a special case.
    assert guess_year(Path("Z:/does/not/exist 2010/x.jpg")) == 2010


# ---------------------------------------------------------------------------
# Wiring into Pipeline._photo_taken_at - mocked, so this runs in
# milliseconds rather than paying a real Florence-2 load
# ---------------------------------------------------------------------------

def _pipeline():
    from app.index.embedder import Embedder
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore
    import tempfile

    tmp = Path(tempfile.mkdtemp())
    store = SqliteStore(tmp / "index.db")

    class _V:
        def ensure_table(self):
            pass

    return Pipeline(store, _V(), Embedder(dim=8, encoder=lambda t: [[0.0] * 8 for _ in t]),
                     PipelineConfig(walk=WalkConfig(roots=[tmp])))


def test_exif_wins_over_an_era_hint(monkeypatch):
    import datetime
    from app.index.walker import Candidate

    pipeline = _pipeline()
    shot = datetime.datetime(2006, 6, 15, 10, 30, 0)
    monkeypatch.setattr(
        "app.extract.exif.read_datetime", lambda path: shot)
    monkeypatch.setattr(
        "app.extract.era_hints.guess_year", lambda path: 1999)

    candidate = Candidate(path=Path("D:/Photos/Diwali 1999/IMG.jpg"),
                           size_bytes=1, mtime_ns=1)
    ns, is_hint = pipeline._photo_taken_at(candidate)

    assert ns == int(shot.timestamp() * 1_000_000_000)
    assert is_hint is False


def test_an_era_hint_is_used_when_exif_is_absent(monkeypatch):
    from app.index.walker import Candidate
    from app.extract.era_hints import year_to_epoch_ns

    pipeline = _pipeline()
    monkeypatch.setattr("app.extract.exif.read_datetime", lambda path: None)
    monkeypatch.setattr("app.extract.era_hints.guess_year", lambda path: 2004)

    candidate = Candidate(path=Path("D:/Photos/Diwali 2004/IMG.jpg"),
                           size_bytes=1, mtime_ns=1)
    ns, is_hint = pipeline._photo_taken_at(candidate)

    assert ns == year_to_epoch_ns(2004)
    assert is_hint is True


def test_neither_exif_nor_a_hint_falls_back_to_none(monkeypatch):
    from app.index.walker import Candidate

    pipeline = _pipeline()
    monkeypatch.setattr("app.extract.exif.read_datetime", lambda path: None)
    monkeypatch.setattr("app.extract.era_hints.guess_year", lambda path: None)

    candidate = Candidate(path=Path("D:/Photos/misc/wedding.jpg"),
                           size_bytes=1, mtime_ns=1)
    ns, is_hint = pipeline._photo_taken_at(candidate)

    assert ns is None
    assert is_hint is False


def test_a_non_image_gets_no_date_at_all(monkeypatch):
    from app.index.walker import Candidate

    pipeline = _pipeline()
    monkeypatch.setattr("app.extract.era_hints.guess_year",
                         lambda path: 2004)  # must never even be asked

    candidate = Candidate(path=Path("D:/Docs/report 2004.pdf"),
                           size_bytes=1, mtime_ns=1)
    ns, is_hint = pipeline._photo_taken_at(candidate)

    assert ns is None
    assert is_hint is False
