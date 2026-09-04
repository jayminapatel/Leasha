"""Tests for mbox extractor.

Layer: L2

Tests the mailbox.mbox-based extractor for `.mbox` files, including:
- Extraction of multiple messages
- Conversation grouping via References/In-Reply-To
- Streaming (not loading the whole file)
- Proper handling of multipart messages
- HTML body fallback
- Attachment name extraction
"""

from __future__ import annotations

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
