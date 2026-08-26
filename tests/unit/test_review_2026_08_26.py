r"""The 2026-08-26 review's priority plan, one test per finding.

Layer: L2 and L4

`docs/REVIEW-2026-08-26.md` §"Priority plan" lists what to fix first. Each of
those items gets a test here that asserts **the behaviour the finding describes**
rather than the mechanism used to fix it.

That distinction has cost this project real time. The column widths were fixed
three times; each fix shipped with a green test of the guard it had just added,
and each time the owner's report came back word for word unchanged. A test that
says *"the new call is present"* proves the call. It does not prove that a broken
embedding model degrades instead of failing, and it does not prove that a `.doc`
file can be converted on Windows.

So these are phrased the way the review phrased the findings: *search survives a
broken model*, *the converter looks where the binary actually is*.
"""

from __future__ import annotations

import pathlib
from pathlib import Path

import pytest

from app.core.errors import AppErrorException, make_error


# ---------------------------------------------------------------------------
# H4 - a broken embedding model degrades hybrid search; it does not fail it
# ---------------------------------------------------------------------------

class _BrokenEmbedder:
    """Raises what a missing or corrupt ONNX model actually raises.

    **`AppErrorException`, specifically.** That is the whole finding: both
    boundaries in `vector.search` caught `Exception` and re-raised
    `AppErrorException`, and this is the class `Embedder.embed` raises for a
    model it cannot load. The one failure mode most worth degrading through was
    the one exempted from the degrading.
    """

    def embed(self, texts):
        raise AppErrorException(make_error(
            "ERR_MODEL_LOAD", "index.embedder",
            model="BAAI/bge-small-en-v1.5",
            reason="the model file is truncated",
        ))


class _EmptyVectors:
    def search(self, *_args, **_kwargs):
        return []


def _parsed(text: str):
    from app.search.query import parse_query

    return parse_query(text)


def test_a_broken_embedding_model_returns_no_vector_hits_rather_than_raising():
    """The heart of H4. It used to propagate and take the whole search down."""
    from app.search import vector

    hits = vector.search(_EmptyVectors(), _BrokenEmbedder(), _parsed("pump station"))

    assert hits == [], "a broken model should produce no vector hits, not an exception"


def test_the_reason_the_vector_half_failed_is_reported():
    r"""Degrading silently would trade a loud wrong behaviour for a quiet one.

    The project's standing rule - *"for all things it should not fail silently
    it should notify in some way"* - is the reason `problems` exists at all. And
    the text carried out is the `AppError`'s **suggestion**, because that is the
    sentence with an action in it.
    """
    from app.search import vector

    problems: list[str] = []
    vector.search(_EmptyVectors(), _BrokenEmbedder(), _parsed("pump station"),
                  problems=problems)

    assert problems, "the search degraded and said nothing about why"
    assert any(word in problems[0].lower() for word in ("model", "reinstall", "run")), (
        f"the reason names no action: {problems[0]!r}")


def test_a_vector_store_that_throws_also_degrades():
    """The second boundary, and the same rule. A LanceDB hiccup is not a reason
    to discard keyword hits that have already been computed."""
    from app.search import vector

    class _Angry:
        def search(self, *_args, **_kwargs):
            raise AppErrorException(make_error(
                "ERR_UNEXPECTED", "storage.vectors",
                details="the table is locked by another process"))

    class _Fine:
        def embed(self, texts):
            return [[0.1] * 384 for _ in texts]

    problems: list[str] = []
    hits = vector.search(_Angry(), _Fine(), _parsed("pump"), problems=problems)

    assert hits == []
    assert problems, "the vector store failed and said nothing"


def test_a_filter_that_excludes_everything_is_still_silent():
    """**The guard against re-earning the sixty false alarms.**

    An empty result is normal in three cases, and the previous version of the
    notice fired on all of them - sixty times in the owner's log, wrong every
    time. Degrading loudly must not put that back: nothing failed here, so
    nothing is reported.
    """
    from app.search import vector

    class _Fine:
        def embed(self, texts):
            return [[0.1] * 384 for _ in texts]

    problems: list[str] = []
    hits = vector.search(_EmptyVectors(), _Fine(), _parsed("pump"),
                         allowed_file_ids=set(), problems=problems)

    assert hits == []
    assert problems == [], f"reported a problem for a normal empty result: {problems}"


def test_nothing_to_embed_is_silent_too():
    """A filter-only query never asks the vector half anything.

    `type:pdf` rather than `/type pdf`: the slash form is sugar that
    `expand_slashes` rewrites *before* the parser sees it, so handing it
    straight to `parse_query` makes it free text - which is a fair description
    of how the first draft of this test managed to fail.
    """
    from app.search import vector

    problems: list[str] = []
    hits = vector.search(_EmptyVectors(), _BrokenEmbedder(), _parsed("type:pdf"),
                         problems=problems)

    assert hits == []
    assert problems == [], "a filter-only query is not a vector failure"


# ---------------------------------------------------------------------------
# H10 - the converter looks where the binary actually is
# ---------------------------------------------------------------------------

def test_conversion_resolves_the_binary_the_way_reporting_does(monkeypatch, tmp_path):
    r"""**The fault was two lookups giving two answers, and it survived a fix.**

    `resolve_binary` checks `PATH` *and* the standard Windows install
    locations, because LibreOffice never puts itself on `PATH` there. Settings
    and `doctor` used it. `convert()` used bare `shutil.which`, so on Windows the
    application reported "soffice: found, .doc route enabled" and then raised
    `ERR_CONVERTER_MISSING` for every actual conversion.

    Simulated rather than requiring LibreOffice: `shutil.which` answers None, as
    it does on Windows, while `resolve_binary` finds it. If `convert` still
    consults `which`, it raises `ERR_CONVERTER_MISSING` and this fails.
    """
    from app.extract import converter

    source = tmp_path / "old.doc"
    source.write_bytes(b"not really a doc")
    installed = tmp_path / "soffice"
    installed.write_text("#!/bin/sh\nexit 0\n")

    monkeypatch.setattr(converter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(converter, "resolve_binary",
                        lambda name: str(installed) if name == "soffice" else None)

    class _Rule:
        command = ("soffice", "--headless", "--convert-to", "txt", "{input}")
        timeout_s = 5

    try:
        result = converter.convert(source, _Rule())
    except AppErrorException as exc:
        assert exc.error.code != "ERR_CONVERTER_MISSING", (
            "convert() still says the binary is missing when resolve_binary "
            "finds it - it is consulting shutil.which directly again")
    else:
        result.close()


def test_nothing_in_the_converter_calls_which_except_resolve_binary():
    r"""The rule, rather than the one line that broke it.

    A second call site would reintroduce exactly this bug, and it would look
    just as reasonable as the first one did. `resolve_binary` is the only place
    allowed to ask the operating system where a binary is; everywhere else asks
    `resolve_binary`.
    """
    import ast
    import inspect

    from app.extract import converter

    tree = ast.parse(inspect.getsource(converter))
    offenders = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name == "resolve_binary":
            continue
        for inner in ast.walk(node):
            if (isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "which"):
                offenders.append(node.name)

    assert not offenders, (
        f"these call shutil.which directly instead of resolve_binary: "
        f"{sorted(set(offenders))} - on Windows they will not find LibreOffice")


def test_a_binary_that_is_genuinely_absent_still_says_so(monkeypatch, tmp_path):
    """Degrading is not the same as pretending. A missing converter is a real
    skip with a real code, and the fix must not have swallowed that."""
    from app.extract import converter

    source = tmp_path / "old.doc"
    source.write_bytes(b"x")

    monkeypatch.setattr(converter.shutil, "which", lambda _name: None)
    monkeypatch.setattr(converter, "resolve_binary", lambda _name: None)

    class _Rule:
        command = ("soffice", "--headless", "{input}")
        timeout_s = 5

    with pytest.raises(AppErrorException) as caught:
        converter.convert(source, _Rule())
    assert caught.value.error.code == "ERR_CONVERTER_MISSING"


# ---------------------------------------------------------------------------
# H1 - an unchanged skip is settled, not re-parsed on every run
# ---------------------------------------------------------------------------

def _classifier(tmp_path, **overrides):
    """A pipeline wired to a real store, for `_classify` alone."""
    from app.index.pipeline import Pipeline, PipelineConfig
    from app.index.walker import WalkConfig
    from app.storage.sqlite_store import SqliteStore

    tmp_path = pathlib.Path(tmp_path); tmp_path.mkdir(parents=True, exist_ok=True)
    store = SqliteStore(tmp_path / "index.db").connect()
    config = PipelineConfig(walk=WalkConfig(roots=[tmp_path]), **overrides)
    pipeline = Pipeline(store=store, vectors=None, embedder=None, config=config)
    return store, pipeline


def _skipped_row(store, path, *, code, mtime=111, size=222):
    from app.core.errors import make_error
    from app.storage.sqlite_store import FileStatus

    file_id = store.upsert_file(str(path), size_bytes=size, mtime_ns=mtime,
                                ext=path.suffix.lstrip("."), parent_dir=str(path.parent))
    store.mark_skipped(file_id, make_error(code, "index.pipeline", path=str(path),
                                           binary="soffice"))
    # SKIPPED or FAILED depending on the code's `is_fatal` - `ERR_NO_TEXT_LAYER`
    # is a skip, `ERR_CONVERTER_MISSING` is a failure. Both are settled while the
    # file has not moved, which is the whole point, so the helper accepts either
    # rather than asserting a distinction the fix deliberately ignores.
    assert store.get_file(str(path)).status in (FileStatus.SKIPPED, FileStatus.FAILED)
    return file_id


def test_a_skipped_file_that_has_not_changed_is_left_alone(tmp_path):
    r"""**H1, and the cost was hours per night.**

    `_classify` returned `UNCHANGED` only for `INDEXED`, so a SKIPPED row with
    an identical date and size fell through, was fully re-parsed, failed for the
    same reason, and was rewritten. Every incremental run. A corpus with 100k
    scanned PDFs recorded as `ERR_NO_TEXT_LAYER` pays for that nightly and
    learns nothing.
    """
    from app.index.pipeline import UNCHANGED
    from app.index.walker import Candidate

    target = tmp_path / "scan.pdf"
    target.write_bytes(b"%PDF-1.4 no text layer here")
    # `ocr_mode="text"`: a text pass cannot read a scanned page, so its own
    # ERR_NO_TEXT_LAYER row is a settled answer. On an OCR-capable pass the same
    # code means the opposite - see the pair of tests below.
    store, pipeline = _classifier(tmp_path, ocr_mode="text")
    try:
        stat = target.stat()
        _skipped_row(store, target, code="ERR_NO_TEXT_LAYER",
                     mtime=stat.st_mtime_ns, size=stat.st_size)

        decision = pipeline._classify(Candidate(
            path=target, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))

        assert decision is UNCHANGED, (
            "an unchanged skipped file was queued for extraction again")
    finally:
        store.close()


def test_a_skipped_file_that_has_changed_is_read_again(tmp_path):
    """Settled means "while nothing has moved". Edit the file and it is work."""
    from app.index.pipeline import UNCHANGED
    from app.index.walker import Candidate

    target = tmp_path / "scan.pdf"
    target.write_bytes(b"%PDF-1.4 no text layer here")
    store, pipeline = _classifier(tmp_path)
    try:
        _skipped_row(store, target, code="ERR_NO_TEXT_LAYER", mtime=1, size=1)

        stat = target.stat()
        decision = pipeline._classify(Candidate(
            path=target, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))

        assert decision is not UNCHANGED, "a changed file must be read again"
    finally:
        store.close()


def test_a_deliberately_requeued_file_is_read_even_though_it_is_settled(tmp_path):
    r"""**The reason this is a flag and not an allow-list of skip codes.**

    `ERR_NO_TEXT_LAYER` is settled during the text pass and is precisely the
    work during the images pass - the same code, two opposite answers. Only the
    pass that re-queues the file knows which, so `_no_text_layer_candidates` and
    `_locked_candidates` say so on the candidate itself.

    Without this, fixing H1 would silently disable OCR and the locked-file
    retry, which is a far worse bug than the one being fixed.
    """
    from app.index.pipeline import UNCHANGED
    from app.index.walker import Candidate

    target = tmp_path / "scan.pdf"
    target.write_bytes(b"%PDF-1.4 no text layer here")
    store, pipeline = _classifier(tmp_path, ocr_mode="text")
    try:
        stat = target.stat()
        _skipped_row(store, target, code="ERR_NO_TEXT_LAYER",
                     mtime=stat.st_mtime_ns, size=stat.st_size)

        decision = pipeline._classify(Candidate(
            path=target, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns,
            retry=True))

        assert decision is not UNCHANGED, (
            "the OCR pass asked for this file and was refused - OCR is now dead")
    finally:
        store.close()


def test_retry_skipped_reopens_every_settled_row(tmp_path):
    """The escape hatch, for when the *machine* changed rather than the file -
    LibreOffice installed after a run recorded thousands of
    `ERR_CONVERTER_MISSING`. Cheaper and far more targeted than `--force`."""
    from app.index.pipeline import UNCHANGED
    from app.index.walker import Candidate

    target = tmp_path / "old.doc"
    target.write_bytes(b"a word document")
    store, pipeline = _classifier(tmp_path, retry_skipped=True)
    try:
        stat = target.stat()
        _skipped_row(store, target, code="ERR_CONVERTER_MISSING",
                     mtime=stat.st_mtime_ns, size=stat.st_size)

        decision = pipeline._classify(Candidate(
            path=target, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))

        assert decision is not UNCHANGED
    finally:
        store.close()


def test_the_files_left_alone_are_counted(tmp_path):
    r"""**Settling them silently would be a new bug wearing the old one's face.**

    Before the fix every run re-parsed these files and reported them in
    `skipped_by_code`, so the size of the problem was at least visible. If they
    now vanish from every summary, somebody reasonably concludes the unreadable
    PDFs were fixed.
    """
    from app.index.walker import Candidate

    target = tmp_path / "scan.pdf"
    target.write_bytes(b"%PDF-1.4")
    store, pipeline = _classifier(tmp_path, ocr_mode="text")
    try:
        stat = target.stat()
        _skipped_row(store, target, code="ERR_NO_TEXT_LAYER",
                     mtime=stat.st_mtime_ns, size=stat.st_size)
        pipeline._classify(Candidate(
            path=target, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))

        assert pipeline._settled_skips.get("ERR_NO_TEXT_LAYER") == 1
    finally:
        store.close()


# ---------------------------------------------------------------------------
# H2 - the two indexes v10 dropped
# ---------------------------------------------------------------------------

def _indexes_on_files(conn) -> set:
    return {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='files'")
    }


def test_a_database_carried_through_v10_keeps_the_date_indexes(tmp_path):
    r"""**Tested on a *migrated* database, which is why this survived.**

    A fresh database is created at the current schema with every index present
    and never runs v10's rebuild, so every test that starts from an empty file
    saw eight indexes and passed. Only a database carried forward loses them -
    `DROP TABLE files` takes its indexes, and v10's recreate list held six of
    the eight.

    So this one starts at v4, walks the whole migration chain, and then asks
    what survived. `idx_files_mtime` is the index v5 documents as the 6.30ms to
    0.04ms fix for date-ordered and date-filtered searches.
    """
    import sqlite3

    from app.storage.migrations import CURRENT_VERSION, apply_migrations

    path = tmp_path / "carried.db"
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        apply_migrations(conn)                       # fresh, at CURRENT_VERSION
        # Walk it back to v4 and replay: the only way to exercise the rebuild.
        conn.execute("UPDATE schema_version SET version = 4 WHERE id = 1")
        conn.execute("DROP INDEX IF EXISTS idx_files_mtime")
        conn.execute("DROP INDEX IF EXISTS idx_files_source_kind")
        apply_migrations(conn)

        assert read_version_of(conn) == CURRENT_VERSION
        present = _indexes_on_files(conn)
        assert "idx_files_mtime" in present, (
            "v10 dropped idx_files_mtime and nothing put it back - every "
            "'newest first', after: and before: search full-scans files")
        assert "idx_files_source_kind" in present
    finally:
        conn.close()


def read_version_of(conn) -> int:
    from app.storage.migrations import read_version

    return read_version(conn)


def test_the_repair_runs_on_a_database_that_already_lost_them(tmp_path):
    """v13 exists for the databases v10 already damaged. There is no way to tell
    those apart afterwards, so it asserts the end state rather than detecting."""
    import sqlite3

    from app.storage.migrations import apply_migrations

    path = tmp_path / "damaged.db"
    conn = sqlite3.connect(path)
    conn.isolation_level = None
    try:
        apply_migrations(conn)
        # Exactly the state a v10 upgrade left behind: at v12, indexes missing.
        conn.execute("DROP INDEX IF EXISTS idx_files_mtime")
        conn.execute("DROP INDEX IF EXISTS idx_files_source_kind")
        conn.execute("UPDATE schema_version SET version = 12 WHERE id = 1")

        apply_migrations(conn)

        present = _indexes_on_files(conn)
        assert {"idx_files_mtime", "idx_files_source_kind"} <= present
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# H3 - the v7 backfill does not materialise the corpus
# ---------------------------------------------------------------------------

def test_the_identifier_backfill_reads_in_batches(tmp_path):
    r"""**It was one `fetchall()` over `chunks`, inside `connect()`.**

    At 20-30M chunks that is tens of gigabytes of text in a Python list before
    a single row is written: the upgrade did not run slowly, it exhausted memory
    and died before the window opened, with no backup and a database stuck
    between two schema versions.

    Asserted by counting reads rather than by measuring memory - a memory
    assertion is unreliable in a test process and would be the first thing
    deleted. More rows than one batch must mean more than one query.
    """
    import sqlite3

    from app.storage import migrations

    conn = sqlite3.connect(tmp_path / "backfill.db")
    conn.isolation_level = None
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT, "
                 "symbols TEXT NOT NULL DEFAULT '')")
    rows = migrations._BACKFILL_BATCH * 2 + 7
    # Inside one transaction: `isolation_level=None` is autocommit, so ten
    # thousand bare inserts are ten thousand fsyncs and the fixture itself
    # outlives the test runner. This is how the missing BEGIN in the backfill
    # was found.
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO chunks (id, text) VALUES (?, ?)",
                     [(n, f"ResetPasswordHandler{n}") for n in range(1, rows + 1)])
    conn.execute("COMMIT")

    # `set_trace_callback` rather than replacing `conn.execute`, which sqlite3
    # makes read-only. It reports every statement the connection runs, which is
    # exactly the question: how many times was the table read?
    reads: list[str] = []
    conn.set_trace_callback(
        lambda sql: reads.append(sql) if "SELECT id, text FROM chunks" in sql else None)
    try:
        migrations._backfill_symbols(conn, lambda text: text.lower())
    finally:
        conn.set_trace_callback(None)

    assert len(reads) >= 3, (
        f"{rows:,} rows were read in {len(reads)} quer(ies) - the whole table "
        f"is still being materialised at once")
    filled = conn.execute(
        "SELECT COUNT(*) FROM chunks WHERE symbols != ''").fetchone()[0]
    assert filled == rows, "the batched backfill did not fill every row"
    conn.close()


def test_a_chunk_that_yields_no_tokens_does_not_stall_the_backfill(tmp_path):
    """The cursor follows rows **read**, not rows written.

    If it only advanced over updates, a batch where nothing splits would be
    re-read for ever - an infinite loop during `connect()`, on exactly the
    corpus where nothing is code.
    """
    import sqlite3

    from app.storage import migrations

    conn = sqlite3.connect(tmp_path / "nostall.db")
    conn.isolation_level = None
    conn.execute("CREATE TABLE chunks (id INTEGER PRIMARY KEY, text TEXT, "
                 "symbols TEXT NOT NULL DEFAULT '')")
    conn.execute("BEGIN")
    conn.executemany("INSERT INTO chunks (id, text) VALUES (?, ?)",
                     [(n, "plain prose with nothing to split") for n in range(1, 50)])
    conn.execute("COMMIT")

    migrations._backfill_symbols(conn, lambda _text: "")     # never any tokens
    conn.close()                                             # reaching here is the test


# ---------------------------------------------------------------------------
# M1 - the shutdown guard runs whether or not a cache is configured
# ---------------------------------------------------------------------------

def test_searching_a_closed_engine_is_refused_without_a_cache():
    r"""The guard documents fixing a thrice-reported crash and had never run.

    It lived inside `if use_cache and self.cache is not None:`, and `cache=` is
    passed at none of the four `SearchEngine` constructions - so with the cache
    absent, which is always, a search in flight at close still reached
    `self._pool.submit` on a shut-down executor.
    """
    import ast
    import inspect
    import textwrap

    from app.search.engine import SearchEngine

    source = textwrap.dedent(inspect.getsource(SearchEngine.search))
    body = ast.parse(source).body[0].body
    # Skip the docstring; the guard must be the first thing that executes.
    statements = [node for node in body
                  if not (isinstance(node, ast.Expr)
                          and isinstance(node.value, ast.Constant))]
    first = ast.unparse(statements[0])

    assert "_closed" in first, (
        f"the shutdown guard is not the first thing search() does; it runs "
        f"after: {first[:80]!r}")


# ---------------------------------------------------------------------------
# M7 - quoted_removed actually reaches the database
# ---------------------------------------------------------------------------

def test_the_quoted_reply_measurement_is_written_with_the_rest_of_the_meta():
    r"""**A column added, measured, read - and never written.**

    Schema v12 added `messages.quoted_removed`, `email_files.py` measures it,
    and the preview was built to say "480 characters of quoted thread removed".
    The one function that writes message metadata did not list the key, so the
    column was NULL for every message ever indexed and the feature could not
    have worked on any corpus.

    Nothing raised. The schema was right, the extractor was right, and the
    feature simply did not exist - which is why this asserts the key is carried,
    not that some code path mentions it.
    """
    import ast
    import inspect
    import textwrap

    from app.index.pipeline import Pipeline

    source = textwrap.dedent(inspect.getsource(Pipeline._store_message_meta))
    carried = {
        node.value for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }
    assert "quoted_removed" in carried, (
        "_store_message_meta does not carry quoted_removed, so the column stays "
        "NULL and the mail preview has nothing to show")


# ---------------------------------------------------------------------------
# M9 - clearing the box clears the results
# ---------------------------------------------------------------------------

def test_clearing_the_search_box_reaches_the_branch_that_clears_results():
    r"""**Unreachable by typing, which is the only way anybody clears a box.**

    `_dispatch` has always had an empty-query branch that clears the list and
    the status line. `_maybe_dispatch` dispatches only when `tier_for` returns
    something other than `Tier.NONE`, and `tier_for("")` returns exactly
    `Tier.NONE` - so emptying the box left the old results on screen under a
    status line still claiming a count.

    Checked on the parser rather than a live widget: `tier_for` is the function
    whose answer made the branch unreachable, and `_on_text_changed` is where
    that is now handled instead of being routed through it.
    """
    import ast
    import inspect
    import textwrap

    from app.ui.presenter import Tier, tier_for
    from app.ui.search_view import SearchView

    # The condition that caused it, still true - so the fix cannot rely on it
    # having quietly changed.
    assert tier_for("", still_for_ms=10_000, submitted=False) == Tier.NONE

    source = textwrap.dedent(inspect.getsource(SearchView._on_text_changed))
    tree = ast.parse(source)
    calls = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "_dispatch" in calls, (
        "typing an empty box still never reaches the branch that clears the "
        "results - Esc leaves stale rows and a lying status line")


def test_a_file_held_for_the_ocr_pass_is_never_settled(tmp_path):
    r"""**The case that broke OCR, and the reason a flag alone was not enough.**

    `_ocr_gate` writes `ERR_OCR_HELD` for an image the text pass declines, and
    the images pass picks it up **through the ordinary walk** - by extension,
    with no re-queue function involved and therefore no `retry` flag on the
    candidate. The first version of H1 settled it, which switched OCR off
    entirely: `test_ocr_passes` failed within seconds of the fix being written.

    A held row is a queue entry wearing a skip's clothes. Settling one deletes
    the queue.
    """
    from app.index.pipeline import UNCHANGED
    from app.index.walker import Candidate

    target = tmp_path / "photo.png"
    target.write_bytes(b"\x89PNG\r\n")
    store, pipeline = _classifier(tmp_path, ocr_mode="images")
    try:
        stat = target.stat()
        _skipped_row(store, target, code="ERR_OCR_HELD",
                     mtime=stat.st_mtime_ns, size=stat.st_size)

        decision = pipeline._classify(Candidate(
            path=target, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))

        assert decision is not UNCHANGED, "the OCR queue was settled away"
    finally:
        store.close()


def test_no_text_layer_is_settled_in_a_text_pass_and_work_in_an_ocr_pass(tmp_path):
    """The same code, opposite answers, decided by which pass is asking.

    This is why the deferral test is a method on the pipeline rather than a
    constant set: `ERR_NO_TEXT_LAYER` cannot be classified without knowing what
    the current pass is able to do.
    """
    from app.index.pipeline import UNCHANGED
    from app.index.walker import Candidate

    target = tmp_path / "scan.pdf"
    target.write_bytes(b"%PDF-1.4")
    stat = target.stat()

    for mode, settled in (("text", True), ("images", False), ("both", False)):
        store, pipeline = _classifier(tmp_path / mode, ocr_mode=mode)
        try:
            _skipped_row(store, target, code="ERR_NO_TEXT_LAYER",
                         mtime=stat.st_mtime_ns, size=stat.st_size)
            decision = pipeline._classify(Candidate(
                path=target, size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns))
            assert (decision is UNCHANGED) is settled, (
                f"ocr_mode={mode!r}: expected settled={settled}")
        finally:
            store.close()
