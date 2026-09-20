r"""`converter.convert` and the warm session: the seam, and the leaks it closed.

Nothing here starts LibreOffice. The session is replaced by a stand-in with the
same `convert_warm` signature, so what is tested is `converter.py`'s own
decisions: when it uses the session, when it steps aside for the cold command,
what it raises, and what it cleans up.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

from app.core.errors import AppErrorException
from app.extract import converter, lo_session

psutil = pytest.importorskip("psutil")


class _Rule:
    command = ("soffice", "--headless", "--convert-to", "txt:Text", "--outdir", "{outdir}", "{input}")
    produces = "{stem}.txt"
    then = "plaintext"
    timeout_s = 30


@pytest.fixture
def fake_soffice(monkeypatch, tmp_path):
    binary = tmp_path / "soffice.exe"
    binary.write_text("stand-in", encoding="utf-8")
    monkeypatch.setattr(converter, "resolve_binary",
                        lambda name: str(binary) if name == "soffice" else None)
    return binary


def _source(tmp_path: Path) -> Path:
    source = tmp_path / "old.doc"
    source.write_bytes(b"x")
    return source


def test_a_warm_conversion_returns_the_same_result_shape(fake_soffice, monkeypatch, tmp_path):
    calls: list[tuple] = []

    def warm(source, target, kind, *, timeout_s, should_stop=None):
        calls.append((source.name, target.name, kind, timeout_s))
        target.write_text("converted text", encoding="utf-8")
        return 0.05

    monkeypatch.setattr(lo_session, "convert_warm", warm)
    with converter.convert(_source(tmp_path), _Rule()) as result:
        assert result.path.read_text(encoding="utf-8") == "converted text"
        assert result.path.name == "old.txt", "the name `produces` promised"
        assert Path(result.binary).is_absolute()
        folder = result.path.parent
    assert not folder.exists(), "the temporary directory is still removed"
    assert calls and calls[0][2] == "txt"


def test_the_settings_time_limit_bounds_a_warm_conversion(fake_soffice, monkeypatch, tmp_path):
    seen: list[float] = []
    monkeypatch.setattr(lo_session, "wanted", lambda: (1, 45))

    def warm(source, target, kind, *, timeout_s, should_stop=None):
        seen.append(timeout_s)
        target.write_text("x", encoding="utf-8")
        return 0.0

    monkeypatch.setattr(lo_session, "convert_warm", warm)
    converter.convert(_source(tmp_path), _Rule()).close()          # the rule says 30
    assert seen == [30], "a rule asking for less than the setting is honoured"

    class _Slow(_Rule):
        timeout_s = 200

    converter.convert(_source(tmp_path), _Slow()).close()
    assert seen[-1] == 45, "the Settings limit is the ceiling for a warm conversion"


def test_an_unavailable_session_steps_aside_for_the_cold_command(fake_soffice, monkeypatch, tmp_path):
    def unavailable(*_a, **_k):
        raise lo_session.SessionUnavailable("no LibreOffice python")

    monkeypatch.setattr(lo_session, "convert_warm", unavailable)
    ran: list[list[str]] = []

    def cold(command, timeout, cwd):
        ran.append(command)
        (Path(cwd) / "old.txt").write_text("cold text", encoding="utf-8")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(converter, "_run_tree", cold)
    with converter.convert(_source(tmp_path), _Rule()) as result:
        assert result.path.read_text(encoding="utf-8") == "cold text"
    assert ran and ran[0][0] == str(fake_soffice)
    assert any(part.startswith("-env:UserInstallation=") for part in ran[0])


def test_a_file_the_session_failed_is_an_error_and_is_not_retried_cold(fake_soffice, monkeypatch, tmp_path):
    """Converting it again from cold would spend another 5-10 s to fail the same way."""
    def failed(*_a, **_k):
        raise lo_session.ConversionFailed("LibreOffice could not open the file")

    monkeypatch.setattr(lo_session, "convert_warm", failed)
    monkeypatch.setattr(converter, "_run_tree",
                        lambda *a, **k: pytest.fail("the cold command must not run"))
    with pytest.raises(AppErrorException) as caught:
        converter.convert(_source(tmp_path), _Rule())
    assert caught.value.error.code == "ERR_CONVERTER_FAILED"
    assert "could not open" in caught.value.error.details


def test_a_rule_the_session_has_no_filter_for_uses_the_cold_command(fake_soffice, monkeypatch, tmp_path):
    class _Png(_Rule):
        command = ("soffice", "--headless", "--convert-to", "png", "--outdir", "{outdir}", "{input}")
        produces = "{stem}.png"

    monkeypatch.setattr(lo_session, "convert_warm",
                        lambda *a, **k: pytest.fail("no filter, so no session"))

    def cold(command, timeout, cwd):
        (Path(cwd) / "old.png").write_bytes(b"png")
        return subprocess.CompletedProcess(command, 0, b"", b"")

    monkeypatch.setattr(converter, "_run_tree", cold)
    converter.convert(_source(tmp_path), _Png()).close()


def test_the_temporary_directory_is_removed_when_the_session_fails(fake_soffice, monkeypatch, tmp_path):
    made: list[Path] = []
    original = converter.tempfile.TemporaryDirectory

    def tracking(*a, **k):
        holder = original(*a, **k)
        made.append(Path(holder.name))
        return holder

    monkeypatch.setattr(converter.tempfile, "TemporaryDirectory", tracking)
    monkeypatch.setattr(lo_session, "convert_warm",
                        lambda *a, **k: (_ for _ in ()).throw(lo_session.ConversionTimeout("too slow")))
    with pytest.raises(AppErrorException):
        converter.convert(_source(tmp_path), _Rule())
    assert made and not made[0].exists()


def test_a_sharing_violation_on_cleanup_does_not_replace_the_real_error():
    """`WinError 32` - a killed LibreOffice still holding a file - used to be
    raised from the error path, so the file was reported as broken for the wrong
    reason. Seen on the real run."""
    class _Stubborn:
        name = "does-not-exist-anywhere"
        attempts = 0

        def cleanup(self):
            _Stubborn.attempts += 1
            raise PermissionError(32, "The process cannot access the file")

    converter._drop(_Stubborn())                    # must not raise
    assert _Stubborn.attempts >= 2, "it retries before giving up"


def test_a_killed_converter_takes_its_children_with_it(tmp_path):
    """The leak that `subprocess.run(timeout=...)` had.

    On timeout it killed the launcher (`soffice.exe`) and left the process it had
    started (`soffice.bin`) running, at whatever memory that had reached. Here the
    launcher is a Python script that starts a sleeping child and then waits.
    """
    launcher = tmp_path / "launcher.py"
    launcher.write_text(textwrap.dedent('''
        import subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1000)"])
        print(child.pid, flush=True)
        time.sleep(1000)
    '''), encoding="utf-8")

    seen: list[int] = []
    real_popen = subprocess.Popen

    class _Spy(real_popen):
        def communicate(self, *a, **k):
            try:
                return super().communicate(*a, **k)
            finally:
                pass

    with pytest.raises(subprocess.TimeoutExpired):
        converter._run_tree([sys.executable, str(launcher)], 1.5, str(tmp_path))

    # The child's pid went to the launcher's stdout, which `_run_tree` captured
    # and discarded; find survivors by their command line instead.
    survivors = [p for p in psutil.process_iter(["cmdline"])
                 if "time.sleep(1000)" in " ".join(p.info["cmdline"] or [])
                 and p.ppid() != os.getpid() or False]
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        survivors = [p for p in psutil.process_iter(["cmdline", "create_time"])
                     if "time.sleep(1000)" in " ".join(p.info["cmdline"] or [])
                     and p.info["create_time"] > time.time() - 30]
        if not survivors:
            break
        time.sleep(0.2)
    for straggler in survivors:                     # never leave one behind, pass or fail
        try:
            straggler.kill()
        except psutil.Error:
            pass
    assert not survivors, "a timed-out converter's child process was left running"


def test_the_allow_list_gained_only_the_one_program_the_session_needs():
    assert "libreoffice-python" in converter.ALLOWED_BINARIES
    assert not {"cmd", "powershell", "taskkill", "python", "sh"} & converter.ALLOWED_BINARIES
    assert "libreoffice-python" in converter._WINDOWS_LOCATIONS


def test_the_session_and_the_cold_path_share_one_way_to_start_children():
    source = Path(lo_session.__file__).read_text(encoding="utf-8")
    code = "\n".join(line for line in source.splitlines() if not line.strip().startswith("#"))
    assert "shell=True" not in code and "os.system" not in code
    assert "shell=False" in code
