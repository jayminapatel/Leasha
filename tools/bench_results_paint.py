r"""Paint time of the results list, for UI Redesign 9j - run it against two trees.

    venv\Scripts\python.exe tools\bench_results_paint.py <tree> [rows]

`<tree>` is a checkout to measure (this one, or a `git worktree` of the commit
before the redesign, `3da478a`). Prints the **minimum** of twelve paints at the
top, middle and end of the list: on a laptop chip with other programs running,
two runs of identical code differed 3-4x on 2026-09-20, and the minimum is the
one figure that noise cannot make worse. Run each tree three times, alternating,
on an idle machine, and compare the smallest.

The order asks for 10,000 rows; the default is 2,000 because building them takes
about 3.5 ms a row here, and the paint time per page does not depend on the count.
"""

import os
import sys
import time
from pathlib import Path

tree = Path(sys.argv[1]).resolve()
n = int(sys.argv[2]) if len(sys.argv) > 2 else 2000
os.environ.setdefault("QT_QPA_PLATFORM", "windows")
sys.path.insert(0, str(tree))
os.chdir(tree)

from PyQt6.QtWidgets import QApplication  # noqa: E402

app = QApplication([])
from app.ui import theme  # noqa: E402

app.setStyleSheet(theme.stylesheet("light", detected="light"))
from app.search.engine import SearchResult  # noqa: E402
from app.ui.results_view import ResultsView  # noqa: E402

rows = [
    SearchResult(
        chunk_id=i, file_id=i, path=rf"D:\Archive\Projects\2019\file{i}.pdf",
        text=f"chunk {i} pump station commissioning report for the boiler house " * 3,
        score=0.9, rank=i, ext="pdf", mtime_ns=1_700_000_000_000_000_000 + i,
    )
    for i in range(1, n + 1)
]
view = ResultsView()
view.resize(760, 900)
view.show()
for _ in range(10):
    app.processEvents()
started = time.perf_counter()
view.show_results(rows, ["pump"])
for _ in range(5):
    app.processEvents()
build = (time.perf_counter() - started) * 1000

bar = view._list.verticalScrollBar()
best = []
for fraction in (0.0, 0.5, 1.0):
    bar.setValue(int(bar.maximum() * fraction))
    app.processEvents()
    timings = []
    for _ in range(12):
        started = time.perf_counter()
        view._list.viewport().grab()
        timings.append((time.perf_counter() - started) * 1000)
    best.append(min(timings))
print(f"{tree.name} rows={n} build_ms={build:.0f} "
      f"paint_min_ms={[round(x) for x in best]} average={sum(best) / 3:.0f}", flush=True)
os._exit(0)
