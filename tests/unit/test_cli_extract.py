"""Layer 2's CLI entry point.

Ground rule 7 in BUILD_SPEC_V2.md: *every layer ships a CLI entry point before
it ships UI, so it can be tested headless.* Layer 2 shipped without one, which
meant extraction could only ever be checked against synthetic fixtures - never
against the user's own documents, which is where the surprises are.

The two properties that matter:

  * it is **read-only** - no store is opened, nothing is written;
  * a bad file is reported and the run continues, exactly as a 100GB index run
    must behave. The CLI is where that behaviour becomes visible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.cli import build_parser, main


def run(capsys: pytest.CaptureFixture[str], *argv: str) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@pytest.fixture()
def env(temp_env: Path) -> list[str]:
    """CLI arguments pointing at a throwaway .env, so no real index is touched."""
    return ["--env", str(temp_env)]


# --- the command exists and is wired up -------------------------------------

def test_extract_is_a_registered_subcommand() -> None:
    args = build_parser().parse_args(["extract", "somefile.pdf"])
    assert args.command == "extract"
    assert args.paths == ["somefile.pdf"]


def test_extract_with_no_target_explains_itself(
    capsys: pytest.CaptureFixture[str], env: list[str]
) -> None:
    """`paths` became optional when `--mailbox` arrived, so argparse no longer
    rejects a bare `extract`. It must still say what is missing."""
    code, _out, err = run(capsys, *env, "extract")
    assert code == 1
    assert "--mailbox" in err


def test_mailbox_is_a_registered_flag() -> None:
    assert build_parser().parse_args(["extract", "--mailbox"]).mailbox is True


# --- happy path -------------------------------------------------------------

def test_extracting_a_pdf_reports_pages_and_chunks(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    code, out, _ = run(capsys, *env, "extract", str(fixture_root / "pdf/healthy.pdf"))
    assert code == 0
    assert "OK" in out
    assert "pages 1-3" in out
    assert "chunk(s)" in out


def test_chunks_flag_lists_offsets_and_tokens(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    code, out, _ = run(
        capsys, *env, "extract", str(fixture_root / "pdf/healthy.pdf"), "--chunks"
    )
    assert code == 0
    assert "#0" in out and "tok" in out
    assert "Commissioning report" in out


def test_text_flag_prints_the_whole_document(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    _code, out, _ = run(
        capsys, *env, "extract", str(fixture_root / "plaintext/utf8.txt"), "--text"
    )
    assert "full text" in out
    assert "Two isolation valves require replacement" in out


# --- failure is reported, not fatal -----------------------------------------

def test_a_bad_file_is_reported_with_its_fix(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    code, out, _ = run(capsys, *env, "extract", str(fixture_root / "pdf/scanned.pdf"))
    assert code == 1, "every named file was skipped, so the command failed"
    assert "SKIP" in out
    assert "ERR_NO_TEXT_LAYER" in out
    assert "FIX:" in out, "a skip without advice is half an error"


def test_one_bad_file_does_not_stop_the_others(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    """The layer's whole purpose, made visible at the command line."""
    code, out, _ = run(
        capsys, *env, "extract",
        str(fixture_root / "corrupt/lies.pdf"),
        str(fixture_root / "plaintext/utf8.txt"),
    )
    assert code == 0, "one good file means the run succeeded"
    assert "SKIP" in out and "OK" in out
    assert "1 extracted, 1 skipped" in out


def test_binary_file_advice_is_not_about_scanning(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    """ERR_NO_TEXT_LAYER's default suggestion mentions OCR, which would be
    nonsense for a renamed database. Wrong advice is worse than none."""
    _code, out, _ = run(capsys, *env, "extract", str(fixture_root / "plaintext/binary.log"))
    assert "binary" in out.lower()
    assert "OCR" not in out


# --- folders ----------------------------------------------------------------

def test_a_folder_is_walked_and_summarised(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    code, out, _ = run(capsys, *env, "extract", str(fixture_root))
    assert code == 0
    assert "extracted," in out and "skipped," in out
    assert "Nothing was written" in out


def test_limit_caps_the_work(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    _code, out, _ = run(capsys, *env, "extract", str(fixture_root), "--limit", "3")
    payload = [line for line in out.splitlines() if "extracted," in line][0]
    assert payload.split()[0].isdigit()
    assert "3" in payload.split(",")[0] or int(payload.split()[0]) <= 3


def test_unsupported_types_are_filtered_not_reported(
    capsys: pytest.CaptureFixture[str], env: list[str], tmp_path: Path
) -> None:
    """Listing every .dll in a folder as 'skipped' would bury the real failures."""
    (tmp_path / "keep.txt").write_text("real content here", encoding="utf-8")
    (tmp_path / "ignore.dll").write_bytes(b"\x00\x01")
    (tmp_path / "ignore.exe").write_bytes(b"MZ")

    _code, out, _ = run(capsys, *env, "extract", str(tmp_path))
    assert "1 extracted, 0 skipped" in out
    assert "ignore.dll" not in out


def test_nothing_supported_is_a_clean_error(
    capsys: pytest.CaptureFixture[str], env: list[str], tmp_path: Path
) -> None:
    (tmp_path / "only.dll").write_bytes(b"\x00")
    code, _out, err = run(capsys, *env, "extract", str(tmp_path))
    assert code == 1
    assert "no supported files" in err


# --- json -------------------------------------------------------------------

def test_json_output_is_machine_readable(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    _code, out, _ = run(
        capsys, *env, "--json", "extract", str(fixture_root / "office/healthy.xlsx"), "--chunks"
    )
    payload = json.loads(out)
    assert payload["summary"]["extracted"] == 1
    assert payload["summary"]["chunks"] >= 1

    document = payload["results"][0]["documents"][0]
    assert document["segments"] == 2
    assert document["pages"] == [1, 2]
    assert document["chunk_detail"][0]["char_start"] == 0


def test_json_reports_skips_by_code(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    _code, out, _ = run(
        capsys, *env, "--json", "extract", str(fixture_root / "pdf/encrypted.pdf")
    )
    payload = json.loads(out)
    assert payload["summary"]["skipped_by_code"] == {"ERR_FILE_CORRUPT": 1}


# --- read-only --------------------------------------------------------------

def test_extract_opens_no_store(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Building the index is Layer 3's job. If `extract` ever touches a store,
    it stops being a safe thing to point at anything."""
    import app.storage.sqlite_store as sqlite_store
    import app.storage.vector_store as vector_store

    def forbidden(*_args, **_kwargs):
        raise AssertionError("extract must not open a store")

    monkeypatch.setattr(sqlite_store, "SqliteStore", forbidden)
    monkeypatch.setattr(vector_store, "VectorStore", forbidden)

    code, _out, _ = run(capsys, *env, "extract", str(fixture_root / "plaintext/utf8.txt"))
    assert code == 0


def test_cloud_placeholders_are_skipped_without_being_read(
    capsys: pytest.CaptureFixture[str], env: list[str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reading a placeholder is what triggers the download, so the check must
    happen before the file is opened - not after."""
    import app.core.winfs as winfs
    from app.core.winfs import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

    target = tmp_path / "cloud.txt"
    target.write_text("would be downloaded on read", encoding="utf-8")

    monkeypatch.setattr(
        winfs, "file_attributes", lambda _path: FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    )

    code, out, _ = run(capsys, *env, "extract", str(target))
    assert code == 1
    assert "ERR_CLOUD_ONLY" in out
    assert "Always keep on this device" in out


def test_include_cloud_opts_in(
    capsys: pytest.CaptureFixture[str], env: list[str], tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.core.winfs as winfs
    from app.core.winfs import FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS

    target = tmp_path / "cloud.txt"
    target.write_text("content that is actually present", encoding="utf-8")
    monkeypatch.setattr(
        winfs, "file_attributes", lambda _path: FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
    )

    code, out, _ = run(capsys, *env, "extract", str(target), "--include-cloud")
    assert code == 0
    assert "OK" in out


# --- flag placement ---------------------------------------------------------

def test_json_works_after_the_subcommand(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    """`extract PATH --json` is the order people type. argparse would normally
    reject it, because --json is declared on the parent parser."""
    _code, out, _ = run(
        capsys, *env, "extract", str(fixture_root / "plaintext/utf8.txt"), "--json"
    )
    assert json.loads(out)["summary"]["extracted"] == 1


def test_json_still_works_before_the_subcommand(
    capsys: pytest.CaptureFixture[str], env: list[str], fixture_root: Path
) -> None:
    """The subparser must not overwrite a global flag that was already parsed -
    the argparse default-clobbering trap that `default=SUPPRESS` avoids."""
    _code, out, _ = run(
        capsys, *env, "--json", "extract", str(fixture_root / "plaintext/utf8.txt")
    )
    assert json.loads(out)["summary"]["extracted"] == 1


def test_env_works_after_the_subcommand(temp_env: Path, fixture_root: Path) -> None:
    args = build_parser().parse_args(
        ["extract", str(fixture_root / "plaintext/utf8.txt"), "--env", str(temp_env)]
    )
    assert args.env == str(temp_env)


# --- Outlook archives -------------------------------------------------------

def test_a_pst_is_always_reported_with_a_reason(
    capsys: pytest.CaptureFixture[str], env: list[str], tmp_path: Path
) -> None:
    """`.pst` is registered, so an archive is always named - never filtered out
    of a folder walk like a `.dll`.

    Which reason it gets depends on the machine, and the test must not care:
    without Outlook it is `ERR_OUTLOOK_MISSING` and points at XstReader; with
    Outlook it is `ERR_FILE_CORRUPT`, because Outlook was asked to open eighteen
    bytes of nonsense and rightly refused. Asserting one of them made the suite
    pass on a machine without Outlook and fail on the machine this is built for
    - which is precisely backwards.
    """
    archive = tmp_path / "mail.pst"
    archive.write_bytes(b"not a real archive")

    code, out, _ = run(capsys, *env, "extract", str(archive))
    assert code == 1
    assert "mail.pst" in out, "the archive is named, not silently filtered out"
    assert "ERR_OUTLOOK_MISSING" in out or "ERR_FILE_CORRUPT" in out
    assert "FIX:" in out, "and whichever reason it is, it comes with a fix"


def test_pst_appears_in_a_folder_walk(
    capsys: pytest.CaptureFixture[str], env: list[str], tmp_path: Path
) -> None:
    """One unreadable archive must not cost the ordinary files beside it."""
    (tmp_path / "notes.txt").write_text("ordinary content", encoding="utf-8")
    (tmp_path / "archive.pst").write_bytes(b"x" * 100)

    code, out, _ = run(capsys, *env, "extract", str(tmp_path))
    assert code == 0
    assert "1 extracted, 1 skipped" in out
    assert "archive.pst" in out, "the archive is named, not silently filtered out"
