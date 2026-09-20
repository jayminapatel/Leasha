r"""The warm LibreOffice session: its logic, proven without LibreOffice.

Work order 202626270114 item 6i. Every test here drives a **fake helper** - a
small Python script speaking the same one-JSON-line-per-file protocol as
`app/extract/lo_server.py` - so nothing here starts a real `soffice`. That is
deliberate and load-bearing: on 2026-09-20 a stress loop against the real
LibreOffice crashed `soffice.bin` repeatedly and raised Windows error boxes on
the owner's desktop. Logic is tested against the fake; the real thing has one
bounded, marked test in `test_lo_session_real.py`.

What is pinned:

* a session is reused (one start for many files), recycled, and reaped when idle
* a hung file is killed at its time limit, its whole tree with it, and the
  next file works
* a session killed from outside (kill -9) is replaced and the file retried once
* a file that crashes it twice is skipped, and four such files in a row open a
  circuit so LibreOffice is left alone rather than restarted in a loop
* Stop ends a wait within a fraction of a second
* runaway memory is killed
* nothing is left running: not after close, not after the parent is killed
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path
from typing import Callable

import pytest

from app.extract import lo_server, lo_session
from app.extract.lo_session import (
    ConversionFailed,
    ConversionStopped,
    ConversionTimeout,
    ConversionTooBig,
    LoSession,
    SessionCrashed,
    SessionPool,
)

psutil = pytest.importorskip("psutil")

#: The fake helper. The *input file's first line* says what to do with it.
FAKE_HELPER = textwrap.dedent(r'''
    import json, os, subprocess, sys, time
    from pathlib import Path

    # A stand-in for soffice.bin: a child of the helper that just sleeps.
    grandchild = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1000)"])
    print(json.dumps({"ready": True, "soffice_pid": grandchild.pid, "helper": os.getpid()}), flush=True)

    for line in sys.stdin:
        request = json.loads(line)
        if request.get("quit"):
            grandchild.kill()
            break
        source = Path(request["in"])
        mode = source.read_text(encoding="utf-8").splitlines()[0].strip()
        answer = {"id": request["id"]}
        if mode == "hang":
            time.sleep(1000)
        elif mode == "die":
            os._exit(3)
        elif mode == "dieonce":
            marker = Path(str(source) + ".died")
            if not marker.exists():
                marker.write_text("x")
                os._exit(3)
            Path(request["out"]).write_text("recovered", encoding="utf-8")
            answer["ok"] = True
        elif mode == "error":
            answer["error"] = "RuntimeError: LibreOffice could not open the file"
        elif mode == "disposed":
            print(json.dumps({"id": request["id"], "error":
                  "com.sun.star.lang.DisposedException: Binary URP bridge already disposed"}), flush=True)
            print(json.dumps({"fatal": "the connection to LibreOffice was lost"}), flush=True)
            os._exit(4)
        elif mode == "big":
            hold = bytearray(400 * 1024 * 1024)
            for i in range(0, len(hold), 4096):
                hold[i] = 1
            time.sleep(1000)
        else:
            Path(request["out"]).write_text(source.read_text(encoding="utf-8").upper(), encoding="utf-8")
            answer["ok"] = True
        print(json.dumps(answer), flush=True)
''')


@pytest.fixture
def helper(tmp_path: Path) -> Callable[[str], list[str]]:
    script = tmp_path / "fake_helper.py"
    script.write_text(FAKE_HELPER, encoding="utf-8")
    return lambda _profile_url: [sys.executable, str(script)]


def _session(helper, tmp_path: Path, **kwargs) -> LoSession:
    kwargs.setdefault("start_timeout_s", 20.0)
    return LoSession(command_factory=helper, profile_root=tmp_path / "profiles", **kwargs)


def _make(tmp_path: Path, name: str, mode: str = "ok", body: str = "hello") -> Path:
    path = tmp_path / f"{name}.doc"
    path.write_text(f"{mode}\n{body}\n", encoding="utf-8")
    return path


def _alive(pid: int) -> bool:
    try:
        return psutil.Process(pid).is_running() and psutil.Process(pid).status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def _gone_within(pids: list[int], seconds: float = 8.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if not any(_alive(pid) for pid in pids):
            return True
        time.sleep(0.1)
    return False


def _tree(session: LoSession) -> list[int]:
    """The helper and everything under it, while it is alive."""
    parent = psutil.Process(session.pid)
    return [parent.pid] + [c.pid for c in parent.children(recursive=True)]


# ---------------------------------------------------------------------------
# Reuse
# ---------------------------------------------------------------------------


def test_one_process_serves_many_files(helper, tmp_path):
    session = _session(helper, tmp_path)
    try:
        for index in range(6):
            source = _make(tmp_path, f"f{index}", body=f"file number {index}")
            target = tmp_path / f"out{index}.txt"
            session.convert(source, target, "txt", timeout_s=20)
            assert target.read_text(encoding="utf-8").split()[-3:] == ["FILE", "NUMBER", str(index)]
        assert session.starts == 1, "the whole point: LibreOffice is started once, not per file"
    finally:
        session.close()


def test_a_session_is_recycled_after_its_quota(helper, tmp_path):
    session = _session(helper, tmp_path, recycle_after=2)
    try:
        for index in range(5):
            session.convert(_make(tmp_path, f"f{index}"), tmp_path / f"o{index}.txt", "txt",
                            timeout_s=20)
        # 2 + 2 + 1 files: three processes.
        assert session.starts == 3
    finally:
        session.close()


def test_a_file_that_fails_is_an_error_and_the_session_carries_on(helper, tmp_path):
    session = _session(helper, tmp_path)
    try:
        with pytest.raises(ConversionFailed, match="could not open"):
            session.convert(_make(tmp_path, "bad", "error"), tmp_path / "bad.txt", "txt",
                            timeout_s=20)
        session.convert(_make(tmp_path, "good"), tmp_path / "good.txt", "txt", timeout_s=20)
        assert session.starts == 1, "an unreadable file is the file's problem, not the session's"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Hangs, kills, crashes
# ---------------------------------------------------------------------------


def test_a_hung_file_is_killed_at_its_limit_with_its_whole_tree(helper, tmp_path):
    session = _session(helper, tmp_path)
    try:
        session.convert(_make(tmp_path, "warm"), tmp_path / "warm.txt", "txt", timeout_s=20)
        doomed = _tree(session)
        assert len(doomed) >= 2, "the fake keeps a grandchild, like soffice.bin under soffice.exe"

        started = time.monotonic()
        with pytest.raises(ConversionTimeout):
            session.convert(_make(tmp_path, "stuck", "hang"), tmp_path / "s.txt", "txt",
                            timeout_s=1.5)
        assert time.monotonic() - started < 6, "the limit must bind, not merely be advisory"
        assert _gone_within(doomed), "the hung helper's grandchild must not be left running"

        session.convert(_make(tmp_path, "next"), tmp_path / "next.txt", "txt", timeout_s=20)
        assert session.starts == 2, "the next file gets a fresh session"
    finally:
        session.close()


def test_a_session_killed_from_outside_is_replaced_between_files(helper, tmp_path):
    """The kill -9 case, between two files."""
    session = _session(helper, tmp_path)
    try:
        session.convert(_make(tmp_path, "a"), tmp_path / "a.txt", "txt", timeout_s=20)
        victims = _tree(session)
        for pid in victims:
            psutil.Process(pid).kill()
        assert _gone_within(victims)

        session.convert(_make(tmp_path, "b"), tmp_path / "b.txt", "txt", timeout_s=20)
        assert (tmp_path / "b.txt").exists()
        assert session.starts == 2
    finally:
        session.close()


def test_a_session_killed_mid_file_is_retried_once_by_the_pool(helper, tmp_path):
    pool = SessionPool(1, session_factory=lambda n: _session(helper, tmp_path, name=f"s{n}"))
    try:
        # Warm it, then the file that kills the process on its first attempt only.
        pool.convert(_make(tmp_path, "w"), tmp_path / "w.txt", "txt", timeout_s=20)
        flaky = _make(tmp_path, "flaky", "dieonce")
        pool.convert(flaky, tmp_path / "flaky.txt", "txt", timeout_s=20)
        assert (tmp_path / "flaky.txt").read_text(encoding="utf-8") == "recovered"
    finally:
        pool.close()


def test_a_file_that_kills_it_twice_is_skipped_not_looped_on(helper, tmp_path):
    session_holder: list[LoSession] = []

    def factory(n: int) -> LoSession:
        made = _session(helper, tmp_path, name=f"s{n}")
        session_holder.append(made)
        return made

    pool = SessionPool(1, session_factory=factory)
    try:
        with pytest.raises(ConversionFailed, match="twice"):
            pool.convert(_make(tmp_path, "poison", "die"), tmp_path / "p.txt", "txt",
                         timeout_s=20)
        assert session_holder[0].starts == 2, "one attempt and one retry - never a third"
        # The pool is not broken by one bad file.
        pool.convert(_make(tmp_path, "fine"), tmp_path / "f.txt", "txt", timeout_s=20)
    finally:
        pool.close()


def test_four_crashing_files_in_a_row_open_the_circuit(helper, tmp_path, monkeypatch):
    """The crash loop, ended: after that many, LibreOffice is left alone."""
    monkeypatch.setattr(lo_session, "RESTART_BACKOFF_S", 0.0)
    pool = SessionPool(1, session_factory=lambda n: _session(helper, tmp_path, name=f"s{n}"),
                       cooldown_s=60)
    try:
        for index in range(lo_session.CRASH_LIMIT):
            with pytest.raises(ConversionFailed, match="twice"):
                pool.convert(_make(tmp_path, f"p{index}", "die"), tmp_path / f"p{index}.txt",
                             "txt", timeout_s=20)
        assert pool.circuit_open

        started = time.monotonic()
        with pytest.raises(ConversionFailed, match="keeps stopping"):
            pool.convert(_make(tmp_path, "innocent"), tmp_path / "i.txt", "txt", timeout_s=20)
        assert time.monotonic() - started < 1, "an open circuit answers at once, it does not start anything"
    finally:
        pool.close()


def test_a_success_ends_the_streak(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(lo_session, "RESTART_BACKOFF_S", 0.0)
    pool = SessionPool(1, session_factory=lambda n: _session(helper, tmp_path, name=f"s{n}"))
    try:
        for round_ in range(lo_session.CRASH_LIMIT + 2):
            with pytest.raises(ConversionFailed):
                pool.convert(_make(tmp_path, f"p{round_}", "die"), tmp_path / "x.txt", "txt",
                             timeout_s=20)
            pool.convert(_make(tmp_path, f"g{round_}"), tmp_path / "y.txt", "txt", timeout_s=20)
        assert not pool.circuit_open, "crashes separated by successes are not a crash loop"
    finally:
        pool.close()


def test_a_dead_bridge_is_the_sessions_fault_and_is_replaced(helper, tmp_path):
    """The `DisposedException` cluster seen on the real run: one crash, then
    every later file failing in 60 ms. It must read as a crash, not as N bad files."""
    session = _session(helper, tmp_path)
    try:
        with pytest.raises(SessionCrashed):
            session.convert(_make(tmp_path, "crash", "disposed"), tmp_path / "c.txt", "txt",
                            timeout_s=20)
    finally:
        session.close()


def test_restarts_back_off_after_a_crash(helper, tmp_path, monkeypatch):
    monkeypatch.setattr(lo_session, "RESTART_BACKOFF_S", 0.6)
    session = _session(helper, tmp_path)
    try:
        with pytest.raises(SessionCrashed):
            session.convert(_make(tmp_path, "die", "die"), tmp_path / "d.txt", "txt", timeout_s=20)
        started = time.monotonic()
        session.convert(_make(tmp_path, "ok"), tmp_path / "o.txt", "txt", timeout_s=20)
        assert time.monotonic() - started >= 0.5, "a session that just crashed is not restarted instantly"
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Stop, and memory
# ---------------------------------------------------------------------------


def test_stop_ends_a_conversion_in_progress_quickly(helper, tmp_path):
    session = _session(helper, tmp_path)
    try:
        session.convert(_make(tmp_path, "warm"), tmp_path / "w.txt", "txt", timeout_s=20)
        doomed = _tree(session)
        flag = {"stop": False}

        import threading

        threading.Timer(0.5, lambda: flag.__setitem__("stop", True)).start()
        started = time.monotonic()
        with pytest.raises(ConversionStopped):
            session.convert(_make(tmp_path, "long", "hang"), tmp_path / "l.txt", "txt",
                            timeout_s=120, should_stop=lambda: flag["stop"])
        assert time.monotonic() - started < 3, "Stop must not wait for a conversion"
        assert _gone_within(doomed)
    finally:
        session.close()


def test_runaway_memory_is_killed(helper, tmp_path):
    session = _session(helper, tmp_path, memory_limit_mb=150)
    try:
        session.convert(_make(tmp_path, "warm"), tmp_path / "w.txt", "txt", timeout_s=20)
        doomed = _tree(session)
        with pytest.raises(ConversionTooBig, match="more than"):
            session.convert(_make(tmp_path, "huge", "big"), tmp_path / "h.txt", "txt", timeout_s=60)
        assert _gone_within(doomed)
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Leaving nothing behind
# ---------------------------------------------------------------------------


def test_closing_leaves_no_process(helper, tmp_path):
    session = _session(helper, tmp_path)
    session.convert(_make(tmp_path, "a"), tmp_path / "a.txt", "txt", timeout_s=20)
    everything = _tree(session)
    session.close(graceful=False)
    assert _gone_within(everything)
    session.close()                     # twice is harmless


def test_the_pool_reaps_a_session_that_has_been_idle(helper, tmp_path):
    pool = SessionPool(1, session_factory=lambda n: _session(helper, tmp_path, name=f"s{n}"),
                       idle_close_s=0.6)
    try:
        pool.convert(_make(tmp_path, "a"), tmp_path / "a.txt", "txt", timeout_s=20)
        pids = pool.live_pids()
        assert pids
        deadline = time.monotonic() + 15
        while pool.live_pids() and time.monotonic() < deadline:
            time.sleep(0.2)
        assert not pool.live_pids(), "an idle session must close itself: the end of a run is never announced"
        # And it comes back on demand.
        pool.convert(_make(tmp_path, "b"), tmp_path / "b.txt", "txt", timeout_s=20)
    finally:
        pool.close()


def test_closing_the_pool_does_not_wait_for_a_file_in_progress(helper, tmp_path):
    import threading

    pool = SessionPool(1, session_factory=lambda n: _session(helper, tmp_path, name=f"s{n}"))
    outcome: list[BaseException] = []

    def work() -> None:
        try:
            pool.convert(_make(tmp_path, "hang", "hang"), tmp_path / "h.txt", "txt", timeout_s=120)
        except BaseException as exc:                      # noqa: BLE001
            outcome.append(exc)

    thread = threading.Thread(target=work, daemon=True)
    thread.start()
    time.sleep(1.0)
    started = time.monotonic()
    pool.close()
    assert time.monotonic() - started < 5, "closing the application must never wait on LibreOffice"
    thread.join(timeout=10)
    assert outcome and isinstance(outcome[0], ConversionFailed)


def test_two_sessions_never_share_a_profile(helper, tmp_path):
    first = _session(helper, tmp_path, name="a")
    second = _session(helper, tmp_path, name="b")
    try:
        first.convert(_make(tmp_path, "1"), tmp_path / "1.txt", "txt", timeout_s=20)
        second.convert(_make(tmp_path, "2"), tmp_path / "2.txt", "txt", timeout_s=20)
        assert first._lease is not None and second._lease is not None
        assert first._lease.path != second._lease.path
    finally:
        first.close()
        second.close()


def test_a_released_profile_is_reused_by_the_next_session(helper, tmp_path):
    first = _session(helper, tmp_path, name="a")
    first.convert(_make(tmp_path, "1"), tmp_path / "1.txt", "txt", timeout_s=20)
    path = first._lease.path
    first.close()
    second = _session(helper, tmp_path, name="b")
    try:
        second.convert(_make(tmp_path, "2"), tmp_path / "2.txt", "txt", timeout_s=20)
        assert second._lease.path == path, "the profile is kept between sessions - that is the saving"
    finally:
        second.close()


def test_the_profile_lives_under_the_apps_data_not_a_package_cache(monkeypatch, tmp_path):
    """The default location is Leasha's own cache folder when Settings load."""
    class _Settings:
        cache_path = tmp_path / "cache"

    monkeypatch.setattr("app.core.config.load_settings", lambda **_k: _Settings())
    assert lo_session._profile_root() == tmp_path / "cache" / "lo-profile"


def test_a_missing_libreoffice_is_reported_as_unavailable_not_as_a_bad_file(tmp_path):
    session = LoSession(command_factory=lambda _u: (_ for _ in ()).throw(
        lo_session.SessionUnavailable("LibreOffice (soffice) was not found")),
        profile_root=tmp_path / "p")
    with pytest.raises(lo_session.SessionUnavailable):
        session.convert(_make(tmp_path, "a"), tmp_path / "a.txt", "txt", timeout_s=5)
    session.close()


def test_the_default_command_needs_libreoffices_own_python_beside_soffice(monkeypatch, tmp_path):
    """A stand-in `soffice` elsewhere is not something the helper can drive."""
    from app.extract import converter

    fake_soffice = tmp_path / "a" / "soffice.exe"
    fake_python = tmp_path / "b" / "python.exe"
    monkeypatch.setattr(converter, "resolve_binary",
                        lambda name: str(fake_soffice) if name == "soffice" else str(fake_python))
    with pytest.raises(lo_session.SessionUnavailable, match="same folder"):
        lo_session.default_command("file:///x")


def test_kind_for_reads_the_rules_convert_to():
    assert lo_session.kind_for(("soffice", "--headless", "--convert-to", "txt:Text", "{input}")) == "txt"
    assert lo_session.kind_for(("soffice", "--convert-to", "pptx", "{input}")) == "pptx"
    assert lo_session.kind_for(("soffice", "--convert-to", "csv", "{input}")) == "csv"
    assert lo_session.kind_for(("soffice", "--convert-to", "png", "{input}")) is None
    assert lo_session.kind_for(("soffice", "{input}")) is None


def test_every_kind_the_shipped_rules_ask_for_has_a_filter():
    """A rule asking LibreOffice for something the helper cannot write would fall
    back to a cold start on every file - correct, but silently slow."""
    from app.core.formats import load_rules

    for extension, rule in load_rules().converters.items():
        if rule.command[0] not in ("soffice", "libreoffice"):
            continue
        assert lo_session.kind_for(tuple(rule.command)) is not None, (
            f"{extension}: {rule.command} names an output the warm session has no filter for")


# ---------------------------------------------------------------------------
# Nothing survives the parent
# ---------------------------------------------------------------------------

_PARENT = textwrap.dedent(r'''
    import sys, time
    sys.path.insert(0, {root!r})
    from pathlib import Path
    from app.extract.lo_session import LoSession
    script, work = sys.argv[1], Path(sys.argv[2])
    session = LoSession(command_factory=lambda _u: [sys.executable, script],
                        profile_root=work / "profiles")
    (work / "in.doc").write_text("ok\nhi\n", encoding="utf-8")
    session.convert(work / "in.doc", work / "out.txt", "txt", timeout_s=30)
    import psutil
    kids = [c.pid for c in psutil.Process(session.pid).children(recursive=True)]
    print(session.pid, *kids, flush=True)
    time.sleep(1000)
''')


@pytest.mark.skipif(os.name != "nt", reason="the kill-on-close job object is Windows'")
def test_killing_the_parent_abruptly_leaves_no_child_process(tmp_path):
    """**The guarantee that no orphan `soffice.exe` outlives Leasha.**

    The parent is killed with TerminateProcess - no `atexit`, no `finally`, no
    chance to clean up - and the operating system must remove the helper and
    everything under it because they share a kill-on-close job.
    """
    script = tmp_path / "fake_helper.py"
    script.write_text(FAKE_HELPER, encoding="utf-8")
    driver = tmp_path / "parent.py"
    root = str(Path(__file__).resolve().parents[2])
    driver.write_text(_PARENT.format(root=root), encoding="utf-8")

    parent = subprocess.Popen([sys.executable, str(driver), str(script), str(tmp_path)],
                              stdout=subprocess.PIPE, text=True)
    try:
        pids = [int(p) for p in parent.stdout.readline().split()]
        assert len(pids) >= 2, "expected the helper and its child"
        assert all(_alive(pid) for pid in pids)
        psutil.Process(parent.pid).kill()
        assert _gone_within(pids, 10), f"processes survived the parent: {[p for p in pids if _alive(p)]}"
    finally:
        parent.kill()


_SERVER_DRIVER = textwrap.dedent(r'''
    import subprocess, sys, time
    sys.path.insert(0, {root!r})
    from app.extract import lo_server

    def fake_start(soffice, profile_url, pipe):
        return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1000)"])

    lo_server._start_soffice = fake_start
    lo_server._connect = lambda pipe, child, give_up_s=90.0: object()
    lo_server._convert = lambda desktop, source, target, kind: time.sleep(1000)
    sys.exit(lo_server.main(["lo_server", "soffice", "file:///profile"]))
''')


def test_the_helper_takes_libreoffice_with_it_when_its_parent_goes_away(tmp_path):
    """The parent-death watchdog, in the real helper's own main loop.

    Uses the real `lo_server.main` with `soffice` and the conversion faked, so it
    needs no LibreOffice. The parent's end of the pipe closes *while a
    conversion is running* - which is exactly what a killed parent looks like to
    the child - and the helper and its `soffice` stand-in must both be gone.
    """
    driver = tmp_path / "server.py"
    driver.write_text(_SERVER_DRIVER.format(root=str(Path(__file__).resolve().parents[2])),
                      encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(driver)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        ready = proc.stdout.readline()
        assert '"ready": true' in ready
        stand_in = [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
        assert stand_in, "the fake soffice should be running"
        proc.stdin.write('{"id": 1, "in": "x", "out": "y", "filter": "txt"}\n')
        proc.stdin.flush()
        time.sleep(0.8)                                   # now mid-"conversion"
        proc.stdin.close()                                # the parent is gone
        assert _gone_within([proc.pid] + stand_in, 8), "the helper or its soffice outlived its parent"
    finally:
        proc.kill()


def test_a_dead_bridge_makes_the_real_helper_report_fatal_and_stop(tmp_path):
    """After a `DisposedException` the helper says so and exits, taking `soffice`
    with it - instead of failing every later request in 60 ms."""
    driver = tmp_path / "server2.py"
    body = _SERVER_DRIVER.replace(
        "lo_server._convert = lambda desktop, source, target, kind: time.sleep(1000)",
        "def boom(*a):\n    raise RuntimeError('com.sun.star.lang.DisposedException: Binary URP bridge already disposed')\n"
        "lo_server._convert = boom")
    driver.write_text(body.format(root=str(Path(__file__).resolve().parents[2])), encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(driver)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, text=True)
    try:
        proc.stdout.readline()
        stand_in = [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
        proc.stdin.write('{"id": 7, "in": "x", "out": "y", "filter": "txt"}\n')
        proc.stdin.flush()
        error = proc.stdout.readline()
        fatal = proc.stdout.readline()
        assert "DisposedException" in error and '"id": 7' in error
        assert "fatal" in fatal
        assert _gone_within([proc.pid] + stand_in, 8)
    finally:
        proc.kill()


def test_quiet_errors_sets_and_restores_the_error_mode():
    if os.name != "nt":
        pytest.skip("Windows error mode")
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32")
    before = kernel32.SetErrorMode(0)
    kernel32.SetErrorMode(before)
    with lo_session.quiet_errors():
        inside = kernel32.SetErrorMode(0)
        kernel32.SetErrorMode(inside)
        assert inside & 0x0002, "SEM_NOGPFAULTERRORBOX must be set for a child started here"
        assert inside & 0x0001
    after = kernel32.SetErrorMode(0)
    kernel32.SetErrorMode(after)
    assert after == before, "this process's own error mode must be put back"


def test_the_helper_sets_the_error_mode_for_soffice_itself():
    source = Path(lo_server.__file__).read_text(encoding="utf-8")
    assert "SetErrorMode" in source and "_quiet()" in source
