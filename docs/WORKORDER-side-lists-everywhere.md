# Work order (One thread): one left-hand list for every page, drawn the Contacts way

**Doc version:** 1.0 · **Updated:** 2026-10-11 · **Applies to:** app v1.0.3
**Thread:** One thread (`app/ui/widgets/side_list.py`, `app/ui/widgets/mail_folders.py`,
`app/ui/widgets/photo_sidebar.py`, `app/ui/files_view.py`, `app/ui/code_view.py`,
`app/ui/timeline_view.py`, `app/ui/presenter/`, `app/storage/sqlite_store.py`, `tests/unit/`, the user guide)
**Status:** RELEASED by the owner 2026-10-11 ("yes write the order and build it for all and build it"), after
"i like the look of contacts panel with icons etc can that be standardised ?" and the thread's ideas for the
three pages that had no list (Files, Code, Browse).

**Where this came from.** The Contacts page (order 1k, on `feat/accounts-1k`) has the most finished left list in
the app: sections under quiet bold headings, an icon on every line (coloured where colour means something), a
faint count at the right, a tooltip, a right-click menu per entry, and "doors" that open a tool rather than
narrow. Photos and Mail (1n) have simpler lists; Files, Code and Browse none. The owner asked for one standard.

**What must not change.** Every list stays optional (the panel icon and View menu, remembered) and a click still
does what it does today on Photos and Mail. No store migration. Contacts' own panel is moved onto the shared
widget by the Mail & Contacts thread after `main` is merged into its branch, not by this thread.

---

## Decisions (defaults the building thread may take)

- **D1 One widget.** `SideList`, drawn from the Contacts design: a page gives it entries - section, icon, colour,
  label, count, tooltip, what a click does, right-click actions - and it draws them. Ticked entries for a list
  whose clicks toggle (Photos).
- **D2 One rule for a click.** It narrows the page together with whatever is typed in its box; the same entry
  again, or **All ...** at the top, takes it away. Files and Code write the switch into the box (`path:`, `on:`,
  `type:`, `repo:`), as Photos already does, so a click and a typed filter are the same thing.
- **D3 What each list holds.**
  - **Files:** Places (the indexed folders as a tree, a few levels deep, with counts), Drives (catalogued drives,
    plugged in or not), Kinds (the commonest file types).
  - **Code:** Repositories (with file counts), Languages (the commonest types among them).
  - **Browse:** Years, each opening to its months, with counts; Show (Everything, Photos, Files, Mail).
  - **Mail** and **Photos** keep what they hold, drawn by `SideList`.
- **D4 Switched the same way everywhere**: the `panel-left` icon beside Preview / View and a View-menu entry,
  remembered as `ui:<page>_side`; on by default.

## Items

- [ ] **1a** `SideList` and its `SideEntry` (D1), with icons, colours, counts, tooltips, sections, the menu and
  "doors".
- [ ] **1b** Mail's folders on `SideList`.
- [ ] **1c** Photos' list on `SideList`, its ticks kept.
- [ ] **2a** Files: Places, Drives, Kinds (D3), read on a worker, a click into the box (D2), switched (D4).
- [ ] **2b** Code: Repositories, Languages (D3), the same way.
- [ ] **2c** Browse: Years and months, Show (D3); a click opens that year or month, or that kind.
- [ ] **3a** Tests: the widget; each page's list, its click and its switch; counts read off the window's thread.
- [ ] **3b** The user guide's Files, Code, Browse, Mail and Photos sections, and the Mail & Contacts thread told
  how to move Contacts onto `SideList`.

## Close-out

Tick each box only when built and tested. Update this order's Status line, the register row
(`docs/ORDER_REGISTER.md`, 1p) and `HANDOFF.md`, and name which of D1-D4 were taken as written.
