r"""The resource governor counts every Leasha process as Leasha's own load.

Layer: L3

2026-10-25: a restarted run paused itself three times in its first five minutes on "The
machine is busy (84% CPU used by other programs)", and the busiest "other program" was
`pythonw.exe` at 44% - Leasha's own window and model host. The run is `app.cli index`, a
child of the window, and only its own children were subtracted. Now the topmost Leasha
process above the run, and everything below it, is the run's own.
"""

from __future__ import annotations

from app.index import resources
from app.index.resources import SystemProbe, _is_leasha


class _Proc:
    """A psutil-shaped process: pid, command line, parent and children."""

    def __init__(self, pid, cmdline, parent=None):
        self.pid, self._cmdline, self._parent, self._children = pid, cmdline, parent, []
        if parent is not None:
            parent._children.append(self)

    def cmdline(self):
        return self._cmdline

    def parent(self):
        return self._parent

    def children(self, recursive=False):
        found = []
        for child in self._children:
            found.append(child)
            if recursive:
                found.extend(child.children(recursive=True))
        return found


def _the_owners_laptop():
    """Explorer -> the venv launcher -> the window -> its model host and the index run."""
    explorer = _Proc(1, ["C:\\Windows\\explorer.exe"])
    launcher = _Proc(2, ["D:\\Leasha\\venv\\Scripts\\pythonw.exe", "-m", "app.main"], explorer)
    window = _Proc(3, ["C:\\Python\\pythonw.exe", "-m", "app.main"], launcher)
    host = _Proc(4, ["C:\\Python\\pythonw.exe", "-m", "app.ort.llm_host", "--env", "x"], window)
    run = _Proc(5, ["C:\\Python\\pythonw.exe", "-m", "app.cli", "index"], window)
    converter = _Proc(6, ["soffice.exe", "--headless"], run)
    teams = _Proc(7, ["ms-teams.exe"], explorer)
    return locals()


def test_leasha_is_told_by_its_module_and_nothing_else():
    procs = _the_owners_laptop()
    assert [p.pid for p in procs.values() if _is_leasha(p)] == [2, 3, 4, 5]


def test_the_run_counts_the_window_its_model_host_and_its_own_children_as_its_own():
    procs = _the_owners_laptop()
    probe = SystemProbe()
    probe._process = procs["run"]
    family = {p.pid for p in probe._family(resources)}
    assert family == {2, 3, 4, 6}, "the launcher, window, model host and converter; not Teams"
    assert procs["teams"].pid not in family and procs["run"].pid not in family


def test_a_run_with_no_leasha_above_it_counts_only_its_children():
    """The command line, a test: nothing above is Leasha, so nothing above is counted."""
    shell = _Proc(1, ["cmd.exe"])
    run = _Proc(2, ["python.exe", "-m", "app.cli", "index"], shell)
    _Proc(3, ["soffice.exe"], run)
    probe = SystemProbe()
    probe._process = run
    assert {p.pid for p in probe._family(resources)} == {3}


def test_a_process_whose_command_line_cannot_be_read_is_not_leasha():
    class Hidden(_Proc):
        def cmdline(self):
            raise PermissionError("access denied")

    assert _is_leasha(Hidden(9, [])) is False
