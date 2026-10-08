"""Layer 8 — optional Ollama client. Never called from the search hot path.

Layer: L8. Everything here degrades to "working without it": search, indexing
and reading files never import this package (non-negotiable 1).
"""
