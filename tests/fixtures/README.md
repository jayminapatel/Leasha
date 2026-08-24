# Test fixtures

**Doc version:** 1.1 · **Updated:** 2026-08-24 · **Applies to:** app v0.3.2

Layer 2 acceptance requires **one healthy and one deliberately corrupt file of each type**.

| Folder | Healthy | Corrupt / awkward |
|---|---|---|
| `pdf/` | text-layer PDF, multi-page | truncated PDF, password-protected PDF, image-only scan |
| `office/` | .docx with tables, .xlsx multi-sheet, .pptx with notes | zero-byte .docx, .xlsx locked open by Excel |
| `email/` | small .pst, .eml, .msg | .pst needing scanpst repair |
| `plaintext/` | UTF-8, UTF-8-BOM, cp1252, large .csv | undecodable bytes, 200MB single-line file |
| `corrupt/` | — | files whose extension lies about their content |

Keep fixtures small — a few KB each, except where size is the point of the test.
Anything generated at test time belongs in a `generated/` subfolder (gitignored).
