"""Layer 1 — storage: SQLite/FTS5 metadata store and LanceDB vector store.

Layer: L1. SQLite is the authority; the LanceDB tables are derived from
`chunks` and can be dropped and rebuilt (`vector_store.py`'s module note).
"""
