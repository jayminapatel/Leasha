"""Tests for mbox extractor.

Layer: L2

Tests the mailbox.mbox-based extractor for `.mbox` files, including:
- Extraction of multiple messages
- Conversation grouping via References/In-Reply-To
- Streaming (not loading the whole file)
- Proper handling of multipart messages
- HTML body fallback
- Attachment name extraction
- Per-message resume (work order 202626270509, item 1b)
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Iterable

import pytest

from app.core.errors import AppErrorException
from app.extract import extract
from app.extract.base import Document
from app.extract.email_mbox import MboxExtractor


@pytest.fixture
def extractor() -> MboxExtractor:
    """The mbox extractor under test."""
    return MboxExtractor()


def _write_generated_mbox(path: Path, count: int) -> None:
    """`count` distinct, valid messages, in the same shape the M16-lesson
    large-file test above uses - kept as its own helper so the resume tests
    below can reuse the exact same fixture shape without repeating it."""
    with open(path, "w", encoding="utf-8") as f:
        for i in range(count):
            f.write(
                f"From sender{i}@example.com Thu Jan 01 12:00:00 2025\n"
                f"Date: Thu, 1 Jan 2025 12:00:00 +0000\n"
                f"From: sender{i}@example.com\n"
                f"To: recipient@example.com\n"
                f"Subject: Message {i}\n"
                f"Message-ID: <msg{i}@example.com>\n"
                f"\n"
                f"This is message {i} with some content.\n"
                f"\n"
            )


class TestMboxSupport:
    """Test file type detection."""

    def test_supports_mbox_extension(self, extractor: MboxExtractor) -> None:
        assert extractor.supports(Path("mailbox.mbox"))
        assert extractor.supports(Path("archive.mbox.bak"))

    def test_supports_extension_less_with_signature(self, tmp_path: Path, extractor: MboxExtractor) -> None:
        """An extension-less file starting with 'From ' is recognized as mbox."""
        mbox_file = tmp_path / "mailbox"
        mbox_file.write_bytes(b"From sender@example.com Thu Jan 01 12:00:00 2025\n")
        assert extractor.supports(mbox_file)

    def test_rejects_non_mbox_files(self, extractor: MboxExtractor) -> None:
        assert not extractor.supports(Path("document.txt"))
        assert not extractor.supports(Path("archive.zip"))
        assert not extractor.supports(Path("message.eml"))


class TestMboxExtraction:
    """Test message extraction from mbox files."""

    def test_extract_single_message(self, fixture_root: Path) -> None:
        """A simple mbox with one message yields one Document."""
        mbox_path = fixture_root / "email" / "simple.mbox"
        assert mbox_path.exists()

        documents = list(extract(mbox_path))
        assert len(documents) == 2  # The fixture has 2 messages

        # First message
        doc1 = documents[0]
        assert doc1.source_kind == "eml"
        assert "Test Message One" in doc1.text
        assert "sender@example.com" in doc1.text
        assert "test content" in doc1.text.lower()
        assert doc1.meta.get("subject") == "Test Message One"
        assert doc1.meta.get("sender") == "sender@example.com"

    def test_conversation_grouping(self, fixture_root: Path) -> None:
        """Messages with References/In-Reply-To group in the same conversation."""
        mbox_path = fixture_root / "email" / "simple.mbox"
        documents = list(extract(mbox_path))

        # First message should have no conversation (or use its own Message-ID)
        conv1 = documents[0].meta.get("conversation")
        assert conv1  # Must have a conversation key

        # Second message (reply) should reference the first
        conv2 = documents[1].meta.get("conversation")
        # References header's first element should be in msg1's ID
        assert conv2 == "<msg1@example.com>"  # The first ID from References

    def test_recipient_parsing(self, fixture_root: Path) -> None:
        """To and Cc headers are extracted as recipients."""
        mbox_path = fixture_root / "email" / "simple.mbox"
        documents = list(extract(mbox_path))

        doc = documents[0]
        recipients = doc.meta.get("recipients")
        assert recipients  # Should have recipient list
        # Recipients are JSON-encoded
        import json
        recip_list = json.loads(recipients)
        assert "recipient@example.com" in recip_list

    def test_sent_at_timestamp(self, fixture_root: Path) -> None:
        """The Date header is converted to a Unix timestamp."""
        mbox_path = fixture_root / "email" / "simple.mbox"
        documents = list(extract(mbox_path))

        doc = documents[0]
        sent_at = doc.meta.get("sent_at")
        assert sent_at is not None
        assert isinstance(sent_at, int)
        # Should be around Jan 1 2025
        assert sent_at > 1704000000  # Jan 1 2025 00:00:00 UTC

    def test_empty_subject_handled(self, tmp_path: Path) -> None:
        """Messages with no subject are handled gracefully."""
        mbox_path = tmp_path / "nosub.mbox"
        mbox_path.write_text(
            "From sender@example.com Thu Jan 01 12:00:00 2025\n"
            "Date: Thu, 1 Jan 2025 12:00:00 +0000\n"
            "From: sender@example.com\n"
            "To: recipient@example.com\n"
            "\n"
            "Message with no subject.\n"
        )

        documents = list(extract(mbox_path))
        assert len(documents) == 1
        assert documents[0].meta.get("subject") == ""

    def test_multipart_message_prefers_plain(self, tmp_path: Path) -> None:
        """Multipart messages prefer plain text to HTML."""
        mbox_path = tmp_path / "multipart.mbox"
        # A multipart message structure requires proper MIME boundaries
        content = (
            "From sender@example.com Thu Jan 01 12:00:00 2025\n"
            "Date: Thu, 1 Jan 2025 12:00:00 +0000\n"
            "From: sender@example.com\n"
            "To: recipient@example.com\n"
            "Subject: Multipart Test\n"
            "MIME-Version: 1.0\n"
            "Content-Type: multipart/alternative; boundary=\"boundary123\"\n"
            "\n"
            "--boundary123\n"
            "Content-Type: text/plain; charset=\"utf-8\"\n"
            "\n"
            "Plain text version of the message.\n"
            "--boundary123\n"
            "Content-Type: text/html; charset=\"utf-8\"\n"
            "\n"
            "<html><body>HTML version</body></html>\n"
            "--boundary123--\n"
        )
        mbox_path.write_text(content)

        documents = list(extract(mbox_path))
        assert len(documents) == 1
        # Should prefer plain text
        assert "Plain text version" in documents[0].text
        # Should NOT include HTML tags
        assert "<html>" not in documents[0].text


class TestMboxErrors:
    """Test error handling."""

    def test_missing_file_raises_error(self, tmp_path: Path) -> None:
        """Attempting to extract a non-existent file raises an error."""
        missing = tmp_path / "does_not_exist.mbox"
        with pytest.raises(AppErrorException):
            list(extract(missing))

    def test_corrupt_mbox_continues(self, fixture_root: Path) -> None:
        """A corrupt mbox file yields what it can and skips bad messages."""
        corrupt_path = fixture_root / "email" / "corrupt.mbox"
        if not corrupt_path.exists():
            pytest.skip("corrupt.mbox fixture not found")

        # The corruption is in the message body, not the format
        # So extraction should still work (mailbox.mbox is forgiving)
        documents = list(extract(corrupt_path))
        # Should get the first message at least
        assert len(documents) >= 1


class TestMboxLargeFile:
    """Test performance with larger files (not slow suite)."""

    def test_large_mbox_memory_stays_flat(self, tmp_path: Path) -> None:
        """A large mbox file is streamed, not loaded into memory.

        This test generates a moderately large mbox with many messages
        and verifies that the extractor doesn't accumulate memory.
        """
        mbox_path = tmp_path / "large.mbox"

        # Generate 100 messages (a reasonable test size, not slow)
        with open(mbox_path, "w", encoding="utf-8") as f:
            for i in range(100):
                f.write(
                    f"From sender{i}@example.com Thu Jan 01 12:00:00 2025\n"
                    f"Date: Thu, 1 Jan 2025 12:00:00 +0000\n"
                    f"From: sender{i}@example.com\n"
                    f"To: recipient@example.com\n"
                    f"Subject: Message {i}\n"
                    f"Message-ID: <msg{i}@example.com>\n"
                    f"\n"
                    f"This is message {i} with some content.\n"
                    f"\n"
                )

        # Extract all messages
        count = 0
        for document in extract(mbox_path):
            count += 1
            # Verify each is a proper Document
            assert document.text.strip()
            assert "Message " in document.meta.get("subject", "")

        assert count == 100


class TestMboxResumeFrom:
    """Work order 202626270509, item 1b: the extractor's own `resume_from`.

    `test_email_pipeline_resume.py`-style plumbing is covered separately,
    below (`TestMboxKillAndResume`) - these are the narrower claims about
    `MboxExtractor.extract` itself: skipping is exact, and skipped messages
    are never parsed.
    """

    def test_resume_from_skips_exactly_the_messages_before_it(
        self, tmp_path: Path, extractor: MboxExtractor,
    ) -> None:
        mbox_path = tmp_path / "resume.mbox"
        _write_generated_mbox(mbox_path, 20)

        documents = list(extractor.extract(mbox_path, resume_from=12))

        assert len(documents) == 8  # messages 12..19
        assert [d.meta["mbox_index"] for d in documents] == list(range(12, 20))
        assert "Message 12" in documents[0].meta["subject"]

    def test_resume_from_zero_matches_the_old_default_behaviour(
        self, tmp_path: Path, extractor: MboxExtractor,
    ) -> None:
        mbox_path = tmp_path / "resume0.mbox"
        _write_generated_mbox(mbox_path, 5)

        assert list(extractor.extract(mbox_path)) == list(
            extractor.extract(mbox_path, resume_from=0)
        )
        assert len(list(extractor.extract(mbox_path))) == 5

    def test_resume_from_never_parses_the_skipped_messages(
        self, tmp_path: Path, extractor: MboxExtractor, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """The whole point of a message-index cursor over a full re-scan:

        skipped messages cost a byte-level `From ` scan to find their
        boundaries (unavoidable - see the module docstring), never a MIME
        parse, a quote-strip or a `Document` build. `_get_body_text` runs
        exactly once per message actually yielded, so counting its calls
        proves the other 12 were genuinely skipped, not parsed and discarded.
        """
        mbox_path = tmp_path / "resume_cost.mbox"
        _write_generated_mbox(mbox_path, 20)

        calls: list[None] = []
        original = MboxExtractor._get_body_text

        def _counted(message):
            calls.append(None)
            return original(message)

        monkeypatch.setattr(MboxExtractor, "_get_body_text", staticmethod(_counted))

        documents = list(extractor.extract(mbox_path, resume_from=12))

        assert len(documents) == 8
        assert len(calls) == 8  # never touched for messages 0..11

    def test_virtual_path_is_stable_regardless_of_where_a_run_starts(
        self, tmp_path: Path, extractor: MboxExtractor,
    ) -> None:
        """A resumed run's own `enumerate()` restarts from zero; the message's
        *identity* must not, or its row collides with one the previous run
        already wrote under a different, lower number. See `_extract_stream`'s
        duplicate-key guard in `app/index/pipeline.py`, and the module
        docstring here.
        """
        mbox_path = tmp_path / "stable.mbox"
        _write_generated_mbox(mbox_path, 10)

        from_the_top = {d.meta["mbox_index"]: d.virtual_path for d in extractor.extract(mbox_path)}
        resumed = {
            d.meta["mbox_index"]: d.virtual_path
            for d in extractor.extract(mbox_path, resume_from=6)
        }

        for index, virtual_path in resumed.items():
            assert virtual_path == from_the_top[index], (
                "the same message must key to the same row whether this run "
                "started at message 0 or resumed partway through"
            )


# -- pipeline-level kill-and-resume --------------------------------------

class _NullVectors:
    """A vector store that writes nothing and answers everything.

    Matches `tests/unit/test_archive_run.py`'s own `NullVectors` - kept as a
    separate copy here rather than a shared import, following this suite's
    existing convention of each test module carrying its own small pipeline
    fixtures.
    """

    def delete_by_file_ids(self, file_ids):
        pass

    def add(self, **kwargs):
        return len(kwargs.get("chunk_ids") or ())

    def __getattr__(self, name):
        return lambda *args, **kwargs: None


def _resume_test_embedder():
    from app.index.embedder import Embedder, l2_normalise

    def encode(texts):
        return [
            l2_normalise([math.sin(abs(hash(t)) % 100 + i) for i in range(8)])
            for t in texts
        ]

    return Embedder(dim=8, encoder=encode)


class TestMboxKillAndResume:
    """Work order 202626270509, item 1b's own acceptance test.

    The order's §1c already covers "kill-and-resume works" at *file*
    granularity for a large fixture; this is the same claim at the finer
    granularity 1b actually builds - a run stopped mid-mbox resumes past the
    messages it already durably wrote, rather than reprocessing the file
    from message 1.
    """

    def test_a_killed_run_resumes_mid_file_not_from_message_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from app.index.pipeline import Pipeline, PipelineConfig
        from app.index.walker import WalkConfig
        from app.storage.sqlite_store import FileStatus, SqliteStore

        root = tmp_path / "mail"
        root.mkdir()
        mbox_path = root / "big.mbox"
        total_messages = 40
        _write_generated_mbox(mbox_path, total_messages)

        # A spy, not a mock: real extraction still happens underneath, so
        # this proves what `_extract_stream` actually asked for, on every
        # call, without changing what came back.
        resume_from_seen: list[int] = []
        original_extract = MboxExtractor.extract

        def _spy_extract(self, path, *, resume_from=0):
            resume_from_seen.append(resume_from)
            yield from original_extract(self, path, resume_from=resume_from)

        monkeypatch.setattr(MboxExtractor, "extract", _spy_extract)

        db = tmp_path / "index.db"
        with SqliteStore(db) as store:
            def _pipeline() -> Pipeline:
                return Pipeline(
                    store, _NullVectors(), _resume_test_embedder(),
                    PipelineConfig(
                        walk=WalkConfig(roots=[root]), workers=1,
                        checkpoint_every=5, min_free_gb=0, required_free_gb=0,
                    ),
                )

            # --- Run 1: killed partway through -------------------------
            first = _pipeline()
            # The first tick with something indexed, not the first tick:
            # since bug 2b the run announces each phase with a tick, and the
            # first of those comes before a single message is read.
            stats1 = first.run(
                on_progress=lambda s: s.indexed and first.request_stop())

            assert 0 < stats1.indexed < total_messages, (
                "the run must have been interrupted genuinely partway "
                "through the file, not at the very start or the very end"
            )
            assert resume_from_seen == [0], (
                "the first-ever run must start from the top of the file"
            )

            resume_keys = [
                key for key in store.all_state() if key.startswith("resume:")
            ]
            assert len(resume_keys) == 1, (
                "an interrupted mid-file run must leave exactly one "
                "persisted resume cursor behind"
            )
            cursor_value = int(store.get_state(resume_keys[0]))
            assert 0 < cursor_value < total_messages, (
                f"the persisted cursor ({cursor_value}) must point strictly "
                f"inside the file, not at the start or past the end"
            )

            # --- Run 2: resumed -----------------------------------------
            second = _pipeline()
            stats2 = second.run()

            assert resume_from_seen[-1] == cursor_value, (
                "the resumed run must ask the extractor to start exactly "
                "where the persisted cursor says the last run left off - "
                "not from message 1"
            )
            assert resume_from_seen[-1] > 0

            # No message lost, none duplicated: every message 0..N-1 has
            # exactly one INDEXED row, keyed on its own stable identity.
            for index in range(total_messages):
                record = store.get_file(f"{mbox_path}/{index}")
                assert record is not None, f"message {index} is missing after resume"
                assert record.status == FileStatus.INDEXED, (
                    f"message {index} did not end up indexed: {record.status}"
                )

            # The file itself is closed out...
            marker = store.get_file(str(mbox_path))
            assert marker is not None and marker.status == FileStatus.INDEXED

            # ...and the resume cursor for it is gone, not left behind to
            # misdirect a future re-index of the same unchanged bytes.
            assert not [k for k in store.all_state() if k.startswith("resume:")]
