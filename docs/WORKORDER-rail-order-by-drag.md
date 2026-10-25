# Work order (One thread): the rail's pages can be put in any order, by dragging or from a menu

**Doc version:** 1.2 · **Updated:** 2026-10-25 · **Applies to:** app v1.1.0
**Thread:** One thread (`app/ui/widgets/rail.py`, `app/ui/shell.py`, `tests/unit/`, the user guide)
**Status:** BUILT 2026-10-11, all 6, D1-D3 as written; RELEASED by the owner 2026-10-11 ("b do them all"), after asking "is there a way they can be dragged in any order ?"

**What must not change.** The rail's default order is the owner's (2026-10-11): Search, Mail, Photos, Files, Code,
Browse, Chat, Reports. Indexing (the pill) and Settings (the foot) do not move. Keyboard shortcuts open pages by
name, never by position, so they keep working whatever the order.

## Decisions (defaults the building thread may take)

- **D1 Two ways to move a page.** Drag its button up or down the rail; or right-click it for **Move up**, **Move
  down** and **Reset page order** - the same moves without a mouse drag, for anybody who finds dragging hard.
- **D2 Remembered** as `ui:rail_order` (titles, `|`-separated), applied once every page exists. A title the order
  does not name keeps its default place; a saved name with no page is ignored.
- **D3 Reset** is also in the View menu, as **Reset page order**.

## Items

- [x] **1a** `Rail.moveTab(from, to)` moves a page, its button and every map keyed by position, keeps the page in
  front in front, and says so (`tabMoved`).
- [x] **1b** Dragging a page button over another drops it there; Indexing and Settings neither move nor take a drop.
- [x] **1c** Right-click on a page button: Move up, Move down, Reset page order (D1).
- [x] **1d** The order is saved on every move and applied at start (D2); View > Reset page order (D3).
> *2026-10-11, built:* `Rail.moveTab` / `page_order` / `tabMoved` / `order_changed` / `reset_requested`; drag and the Move menu in `app/ui/widgets/rail_order.py` (`_Dragger`, `_DropTarget`, `move_up`, `move_down`); `wire`, `restore`, `apply_order`, `DEFAULT_ORDER`, `STATE_KEY = ui:rail_order`. `tests/unit/test_rail_order.py` (7), the drop driven by a real `QDropEvent`. Dragging by hand in the real window is not yet looked at.
- [x] **2a** Tests: moving, the menu, the saved order applied to a fresh window, reset, the fixed pages refused.
- [x] **2b** The user guide says how to reorder the rail.
