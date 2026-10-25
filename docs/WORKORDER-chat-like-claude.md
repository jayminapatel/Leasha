# Work order (One thread): Chat looks and works like a modern assistant: one calm column, actions on hover and right-click

**Doc version:** 1.2 · **Updated:** 2026-10-25 · **Applies to:** app v1.1.0
**Thread:** One thread (`app/ui/chat_view.py`, `app/ui/widgets/chat_*.py`, `app/ui/theme.py`, `tests/unit/`, the user guide)
**Status:** BUILT 2026-10-11, 5 of 6 (2b's fresh guide screenshot not taken); D1-D4 as written; RELEASED by the owner 2026-10-11 ("b do them all"), after asking "the chat interface can it be like claude for example as the current one looks dated may be buttons are not required just the context menu"

**What must not change.** Every source number still opens its passage; the conversations list, the sources panel,
the preview and every action that exists today (Copy, Regenerate, Try again, Edit, Stop) all stay - only where they
are shown changes. No engine, store or controller change.

*Added 2026-10-11 (owner, while this was being built): "the search page should have a similar look and feel too", then "i.e. the app should be consistent across tabs depending on function". Read as: pages that ask and find (Search, Chat) share one look, and list pages (Files, Mail, Code, Photos) share another. Search's home page was already the calm centred column with a rounded box, so Chat was brought to it - its empty page uses Search's headline and greeting styles (`emptyHeadline`, `emptyGreeting`) and its box the search box's corners (`radius_input`) - rather than Search being changed. The same guidelines were sent to the Mail and Contacts session.*

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

- [x] **1a** The centred column (D1), resizing with the window.
- [x] **1b** Bubbles and answers (D2) in both themes.
- [x] **1c** Actions on hover and in the right-click menu (D3); keyboard users reach them from the menu key.
- [x] **1d** The box (D4).
> *2026-10-11, built:* `chat_bubbles.COLUMN_MAX` (760) and `centred()`; `AnswerBubble.offers` / `set_hover`, `UserBubble.set_hover` / `is_last`; `app/ui/widgets/chat_message_menu.py` (`answer_menu`, `user_menu`, `install`, `watch_text` - each prose piece gets the menu as it is drawn); `MessageBox.frame` (`#chatComposer`), `footer`, `add_to_footer` (model and speed moved out of the head row). `tests/unit/test_chat_like_claude.py` (8). Found and fixed on the way: deleting a conversation could be undone by the start-up read or a save still on its way landing after it (`ChatController._deleted`); it made `test_a_conversation_deleted_while_its_title_waits_is_not_brought_back` fail whenever another test ran first.
- [x] **2a** Tests: every action reachable from the menu; the hover row shows and hides; the column's width cap.
- [ ] **2b** The user guide's Chat section, and a fresh screenshot.
