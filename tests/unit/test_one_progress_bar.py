r"""Every progress bar in the window is the Indexing page's kind (`ShimmerBar`).

Layer: L0 - reads the source.

2026-10-16, the owner: "progress bar should be standard like on indexing page in the app
they are not do them too". Three windows still built Qt's own `QProgressBar` - the Contacts
tools dialogs, the activity table and "Write names into photos" - a flat block beside the
gradient, sweeping bar the Indexing page has had since 2026-09-30.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

#: Where a plain `QProgressBar(` may still be built: `ShimmerBar` itself.
ALLOWED = {"app/ui/widgets/shimmer_bar.py"}


def test_no_window_builds_qts_own_progress_bar():
    built = []
    for path in sorted((ROOT / "app" / "ui").rglob("*.py")):
        name = path.relative_to(ROOT).as_posix()
        if name in ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for n, line in enumerate(text.splitlines(), 1):
            if re.search(r"\bQProgressBar\(", line) and not line.lstrip().startswith("#"):
                built.append(f"{name}:{n}")
    assert not built, "use ShimmerBar, the Indexing page's bar: " + ", ".join(built)


def test_the_guard_sees_a_plain_bar(tmp_path, monkeypatch):
    """The scan is proved against a file of its own, so it cannot pass by reading nothing."""
    ui = tmp_path / "app" / "ui"
    ui.mkdir(parents=True)
    (ui / "dialog.py").write_text("bar = QProgressBar()\n", encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    try:
        test_no_window_builds_qts_own_progress_bar()
    except AssertionError as exc:
        assert "app/ui/dialog.py:1" in str(exc)
    else:
        raise AssertionError("the guard passed a plain QProgressBar")
