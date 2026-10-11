"""The Contacts page's parts.

Layer: L5

Order 1k section 6. `app/ui/contacts_view.py` is the page; everything it is made of -
the list and card models, the sidebar, the editor, the history panel, the bulk bar and
the openers the tool dialogs register under - lives here. Nothing in this package
imports Qt at import time except the modules that are widgets, so `openers` can be
imported by the dialogs without a display.
"""
