"""Which test files can see a change: the mapping behind `run_suite.py --affected`.

Layer: L0 - repo tooling, no app imports.

**The rule.** A test is affected when something it reaches has changed. "Reaches" is
static and checkable, never a coverage trace: a test file reaches every `app.*` module it
names (an import at the top, an import inside a test, a dotted string handed to
`monkeypatch` or `importlib`), and a module reaches every module that imports it,
transitively, through the import graph read with `ast`. A test file also reaches any
file whose path or name appears in its text - the guard tests read source files and
documents by path, and this is how a change to `HANDOFF.md` finds `test_handoff_current.py`.

**What always runs.** The load-bearing tests in `WORKORDER-CONVENTIONS.md` §0, parsed from
the same table `test_docs_versioned.py` checks, and the docs, hand-off, project-file and
layering tests. They are cheap and they are the ones a single-threaded project is most
tempted to skip.

**When the answer is "everything".** A change to the shared fixtures, the pytest
configuration or the requirements; or an affected set over six files in ten, which a change
to `app/core` produces honestly - pretending a smaller set would do is the vacuous-skip
lesson of order 0m S1a. The selection says why in either case.

Work order `suite-speed` §2a.
"""

from __future__ import annotations

import ast
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

#: Changed paths that mean the whole suite, whatever else changed.
WHOLE_SUITE_PATHS = ("tests/conftest.py", "tests/__init__.py", "tests/private_locks.py",
                     "pyproject.toml")
WHOLE_SUITE_PREFIXES = ("tests/fixtures/", "tests/golden/")
WHOLE_SUITE_GLOBS = (re.compile(r"^requirements[^/]*\.txt$"),)

#: Above this share of all test files, the selection is the whole suite.
WHOLE_SUITE_FRACTION = 0.6

#: Run on every `--affected` run, when they exist: the record-keeping tests and the layering
#: guard. The §0 table of `WORKORDER-CONVENTIONS.md` adds the rest.
ALWAYS = ("tests/unit/test_docs_versioned.py", "tests/unit/test_handoff_current.py",
          "tests/unit/test_vs_project.py", "tests/unit/test_layering_below_the_ui.py")

#: What a changed document selects before any mention is looked for.
DOCS_TESTS = ("tests/unit/test_docs_versioned.py", "tests/unit/test_handoff_current.py")

LOAD_BEARING_ROW = re.compile(r"^\|\s*`(test_[a-z0-9_]+)`\s*\|\s*`(test_[a-z0-9_]+\.py)`\s*\|",
                              re.MULTILINE)
DOTTED_APP = re.compile(r"\bapp(?:\.[A-Za-z_]\w*)+")


@dataclass
class Selection:
    files: list[str]                       # test files to run, sorted
    whole: bool = False                    # the whole suite instead
    why_whole: str = ""
    reasons: dict[str, list[str]] = field(default_factory=dict)   # test file -> why
    unmapped: list[str] = field(default_factory=list)             # changed, no test names it

    def describe(self, total: int) -> str:
        if self.whole:
            return f"whole suite: {self.why_whole}"
        lines = [f"{len(self.files)} of {total} test files can see this change:"]
        for name in self.files:
            why = "; ".join(self.reasons.get(name, ["always runs"]))
            lines.append(f"  {name}  <- {why}")
        if self.unmapped:
            lines.append("changed, and no test names it: " + ", ".join(self.unmapped))
        return "\n".join(lines)


# -- what changed ---------------------------------------------------------------------------

def changed_paths(root: Path, ref: str = "HEAD") -> list[str]:
    """Files changed against `ref` (committed since it, staged and unstaged) plus untracked
    files, as repository-relative POSIX paths. `.claude/` and `_Knowledge/` are not code."""
    def git(*args: str) -> list[str]:
        out = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True,
                             check=True).stdout
        return [line.strip().replace("\\", "/") for line in out.splitlines() if line.strip()]

    paths = set(git("diff", "--name-only", ref)) | set(git("ls-files", "--others",
                                                           "--exclude-standard"))
    return sorted(p for p in paths
                  if not p.startswith((".claude/", "_Knowledge/")) and (root / p).exists())


# -- the import graph -----------------------------------------------------------------------

def path_to_module(rel: str) -> str | None:
    """`app/index/pipeline.py` -> `app.index.pipeline`; `app/core/__init__.py` -> `app.core`."""
    if not rel.endswith(".py") or not rel.startswith("app/"):
        return None
    parts = rel[:-3].split("/")
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def app_modules(root: Path) -> dict[str, Path]:
    found = {}
    for path in sorted((root / "app").rglob("*.py")):
        name = path_to_module(path.relative_to(root).as_posix())
        if name:
            found[name] = path
    return found


def _resolve(name: str, modules: dict[str, Path]) -> str | None:
    """The longest known module that `name` names or lives inside."""
    while name:
        if name in modules:
            return name
        name = name.rpartition(".")[0]
    return None


def imports_of(path: Path, module: str, modules: dict[str, Path]) -> set[str]:
    """Every known `app.*` module this file imports, relative imports resolved."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return set()
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    seen: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve(alias.name, modules)
                if target:
                    seen.add(target)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[:len(base) - (node.level - 1)] if node.level > 1 else base
                prefix = ".".join(base + ([node.module] if node.module else []))
            else:
                prefix = node.module or ""
            for alias in node.names:
                target = _resolve(f"{prefix}.{alias.name}" if prefix else alias.name, modules)
                if target:
                    seen.add(target)
    seen.discard(module)
    return seen


def module_graph(root: Path) -> dict[str, set[str]]:
    """module -> the modules that import it (the reverse graph, which is the one needed)."""
    modules = app_modules(root)
    importers: dict[str, set[str]] = {name: set() for name in modules}
    for name, path in modules.items():
        for target in imports_of(path, name, modules):
            importers.setdefault(target, set()).add(name)
    return importers


def reverse_closure(importers: dict[str, set[str]], start: Iterable[str]) -> set[str]:
    """`start` and everything that imports it, transitively."""
    seen = set(start)
    stack = list(seen)
    while stack:
        for dependant in importers.get(stack.pop(), ()):
            if dependant not in seen:
                seen.add(dependant)
                stack.append(dependant)
    return seen


# -- what a test file reaches ---------------------------------------------------------------

def modules_named_in(text: str, modules: dict[str, Path]) -> set[str]:
    """Every known module a test names: imports anywhere in the file (`from app.ui import
    top` names `app.ui.top`, which no regex over the text would see) and dotted strings
    (`monkeypatch.setattr("app.other.thing", ...)`, `importlib.import_module("app.x")`)."""
    named = {m for m in (_resolve(match, modules) for match in DOTTED_APP.findall(text)) if m}
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return named
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and not node.level:
            for alias in node.names:
                target = _resolve(f"{node.module}.{alias.name}", modules)
                if target:
                    named.add(target)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = _resolve(alias.name, modules)
                if target:
                    named.add(target)
    return named


def always_run(root: Path) -> list[str]:
    """The load-bearing tests of `WORKORDER-CONVENTIONS.md` §0 plus `ALWAYS`, those that exist."""
    names = set(ALWAYS)
    conventions = root / "docs" / "WORKORDER-CONVENTIONS.md"
    if conventions.is_file():
        for _test, filename in LOAD_BEARING_ROW.findall(conventions.read_text(encoding="utf-8")):
            names.add(f"tests/unit/{filename}")
    return sorted(n for n in names if (root / n).is_file())


def _wants_whole_suite(path: str) -> bool:
    return (path in WHOLE_SUITE_PATHS or path.startswith(WHOLE_SUITE_PREFIXES)
            or any(g.match(path) for g in WHOLE_SUITE_GLOBS))


def select(changed: list[str], root: Path, all_tests: list[str], *,
           always: Iterable[str] = (), importers: dict[str, set[str]] | None = None,
           modules: dict[str, Path] | None = None) -> Selection:
    """The test files that can see `changed`. Pure apart from reading the files named."""
    importers = module_graph(root) if importers is None else importers
    modules = app_modules(root) if modules is None else modules
    all_tests = sorted(all_tests)
    reasons: dict[str, list[str]] = {}

    def add(test: str, why: str) -> None:
        reasons.setdefault(test, []).append(why)

    for test in always:
        if test in all_tests:
            add(test, "always runs")

    for path in changed:
        if _wants_whole_suite(path):
            return Selection(files=all_tests, whole=True,
                             why_whole=f"{path} changed, and every test depends on it")

    texts = {test: (root / test).read_text(encoding="utf-8", errors="replace")
             for test in all_tests if (root / test).is_file()}
    changed_modules = {m for m in (path_to_module(p) for p in changed) if m}
    reached = reverse_closure(importers, changed_modules) if changed_modules else set()
    if reached:
        for test, text in texts.items():
            hit = modules_named_in(text, modules) & reached
            if hit:
                add(test, "imports " + ", ".join(sorted(hit)[:3])
                    + (f" and {len(hit) - 3} more" if len(hit) > 3 else ""))

    unmapped = []
    for path in changed:
        if path in texts:                                   # a changed test file runs itself
            add(path, "changed")
            continue
        if path.endswith((".md", ".html", ".rst")):
            for test in DOCS_TESTS:
                if test in texts:
                    add(test, f"{Path(path).name} changed")
        name = Path(path).name
        found = False
        for test, text in texts.items():
            if path in text or path.replace("/", "\\\\") in text or (
                    len(name) > 6 and name in text and path not in changed_modules):
                add(test, f"names {name}")
                found = True
        if not found and path_to_module(path) is None and path not in texts:
            unmapped.append(path)

    files = sorted(reasons)
    if all_tests and len(files) > WHOLE_SUITE_FRACTION * len(all_tests):
        return Selection(files=all_tests, whole=True,
                         why_whole=(f"{len(files)} of {len(all_tests)} test files can see the "
                                    f"change, which is most of them"))
    return Selection(files=files, reasons=reasons, unmapped=unmapped)
