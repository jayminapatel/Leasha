"""Leasha — search everything on this machine. Embedded, single-process, local.

Layer: L0 (the package root). It re-exports the version and nothing else, so
`import app` stays cheap: every layer is imported by the module that needs it.
"""
from app.core.version import __version__  # noqa: F401
