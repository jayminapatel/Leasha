"""The Contacts tools' writers now live in `app.contacts.book_actions` (Qt-free, so the command line can use them).

Layer: L5. This name stays for the dialogs: importing it gives the same module object, not a copy, so a
test that patches `actions.something` patches what the code runs.
"""

import sys

from app.contacts import book_actions

sys.modules[__name__] = book_actions
