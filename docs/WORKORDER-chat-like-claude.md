# Work order (One thread): Chat looks and works like a modern assistant: one calm column, actions on hover and right-click

**Doc version:** 1.0 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/ui/chat_view.py`, `app/ui/widgets/chat_*.py`, `app/ui/theme.py`, `tests/unit/`, the user guide)
**Status:** RELEASED by the owner 2026-10-11 ("b do them all"), after asking "the chat interface can it be like claude for example as the current one looks dated may be buttons are not required just the context menu"

**What must not change.** Every source number still opens its passage; the conversations list, the sources panel,
the preview and every action that exists today (Copy, Regenerate, Try again, Edit, Stop) all stay - only where they
are shown changes. No engine, store or controller change.

## Decisions (defaults the building thread may take)

- **D1 One column.** The conversation and the message box share one centred column, at most about 760 pixels wide,
  with generous space around it; the conversations list and the sources panel stay either side.
- **D2 Messages.** Your messages in a soft rounded bubble on the right; Leasha's answers as plain text on the page,
  no frame, with the source numbers as they are.
- **D3 Actions.** The row of buttons under each answer goes. The same actions appear as small icons when the pointer
  is over a message, and all of them - plus **Copy as plain text** - are in the message's right-click menu.
- **D4 The box.** A rounded box with the send (or stop) icon inside it on the right, and the model and Fast /
  Thoughtful choices in a quiet row along its bottom edge, as the box's own controls.

## Items

- [ ] **1a** The centred column (D1), resizing with the window.
- [ ] **1b** Bubbles and answers (D2) in both themes.
- [ ] **1c** Actions on hover and in the right-click menu (D3); keyboard users reach them from the menu key.
- [ ] **1d** The box (D4).
- [ ] **2a** Tests: every action reachable from the menu; the hover row shows and hides; the column's width cap.
- [ ] **2b** The user guide's Chat section, and a fresh screenshot.
