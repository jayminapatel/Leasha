"""Layer 0 — foundation: config, errors, logging, single-instance, version.

Layer: L0. Nothing here may import from a higher layer; the one tracked
exception is `osbridge/startmenu.py`, which reads two constants from
`app.ui.tray` inside a function (see its own note).
"""
