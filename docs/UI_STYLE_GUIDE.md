# UI style guide - One Leasha

**Doc version:** 1.2 · **Updated:** 2026-10-14 · **Applies to:** app v1.0.3

How Leasha's window looks and behaves, as it is in the code today. Every rule names the
module that carries it and the test that fails when it is broken. Where this guide and the
code disagree, the code is what ships and this guide is wrong - fix the guide.

The owner's brief (2026-10-11): *make it all consistent from tool tips to performance to
unified look ... icons are must.* Every screen should feel like one app.

## 1. Colours, text sizes, weights and corners - the theme

**Module:** `app/ui/theme.py`.

- **One set of token names, two sets of values.** `PALETTES` holds the light and the dark
  theme under the same keys (`window`, `surface`, `surface_alt`, `text`, `text_dim`,
  `text_faint`, `accent`, `accent_pressed`, `danger`, ...). Every stylesheet rule names a token;
  Python substitutes the values before the sheet reaches Qt, because Qt stylesheets have no
  variables. *Held by* `tests/unit/test_theme.py`
  (`test_both_themes_define_exactly_the_same_tokens`,
  `test_the_stylesheet_has_no_unsubstituted_tokens`).
- **No colour is written anywhere else.** A widget asks `theme_colours()` for a token.
  *Held by* `tests/unit/test_ui_one_app_colours.py`
  (`test_no_colour_in_the_window_is_a_literal_outside_the_theme`), which also checks every
  status colour reads on a card and on the window.
- **Text sizes are steps of one body size**, in points, never pixels: `small`, `body`,
  `large`, `title` (1.25 times body - a page's title) and `display` (`SCALE`), all following the
  system font and "Make text bigger". A painter takes a step with
  `paint_kit.scaled_font(base, "title")`; a sheet names it as `{title}`. No file calls
  `setPixelSize`. *Held by* `tests/unit/test_theme.py` (`test_no_font_size_is_measured_in_pixels`)
  and `tests/unit/test_paint_kit.py` (`test_no_font_is_sized_in_pixels`).
- **Three weights** (`WEIGHT`, read through `weight(name)`): `regular` 400, `semibold` 600 (a
  heading, a chosen tab, a result's name) and `bold` 700 (the stronger of two marks). In a sheet
  as `{weight_semibold}`; a painter uses `paint_kit.set_weight(font, name)`.
- **Corners** come from `RADIUS`: `radius_small` 4px (menu lines, tick boxes), `radius_input` 5px,
  `radius_menu` 6px (menus, a segment), `radius_control` 8px (controls and rows),
  `radius_box` 10px (group boxes and cards), `radius_window` 14px (the quick search card), and the
  pill radius, which follows the text size (`pill_radius`). A painter reads the same steps with
  `radius_px(name)`. *Held by* `tests/unit/test_paint_kit.py`
  (`test_the_sheet_writes_no_step_radius_as_a_number`,
  `test_the_painters_read_their_radii_from_the_table`).
- **States.** Every control has a rest, hover, pressed and focus look: the primary button presses
  to `accent_pressed`, the danger button has a pressed look, a segment a hover and the rail a
  pressed one, and `QTableView` and `QTreeView` rows take the same hover and selection rules as `QTableWidget`
  and `QListView`. *Held by* `tests/unit/test_paint_kit.py` (`test_the_missing_states_have_rules`,
  `test_the_sheet_names_table_and_tree_views_for_hover_and_selection`).
- **The system theme is followed**, with a manual override (`SCHEMES`: system, light, dark).

## 2. Focus

**Modules:** `app/ui/theme.py` (`FOCUS_WIDTH`), `app/ui/widgets/focus_visible.py`.

- **One focus width.** Every `:focus` rule draws a `FOCUS_WIDTH` (2px) edge, and the padding
  gives back the extra pixel, so a control never moves or grows when it takes the focus. A ring
  that only changed hue would be invisible to somebody who cannot tell the hues apart.
  *Held by* `tests/unit/test_paint_kit.py` (`test_every_focus_border_is_the_one_width`,
  `test_a_focused_button_keeps_its_size`).
- **Focus shows when the keyboard put it there.** Qt has no `:focus-visible`, so one
  application-wide filter sets the property `pointerFocus` on a button or tab bar that took the
  focus from a click, and the theme's `[pointerFocus="true"]:focus` rules draw that ring in the
  control's resting edge colour. Text boxes, drop-downs and lists keep the ring however the focus
  arrived. *Held by* `tests/unit/test_paint_kit.py`
  (`test_a_click_marks_pointer_focus_and_the_keyboard_does_not`).
- **A painted list rings its current row** when it has the keyboard (section 6).

## 3. Buttons and dialogs

**Modules:** `app/ui/widgets/buttons.py` (its docstring has the rules in full),
`app/ui/widgets/dialog_frame.py`.

1. A button is as wide as its words and icon, never stretched.
2. Buttons that belong together sit in one row (`button_row`), `ROW_SPACING` apart (8px,
   `theme.SPACE_S`), from the left. A dialog's row is the one order everywhere: Cancel first, the
   primary on the right (`button-layout: 3` on `QDialogButtonBox` and `QMessageBox` in `theme.py`).
3. One height and one padding for all of them, from `theme.BUTTON`.
4. Every action button carries a 16px Lucide icon before its words; `BUTTONS` is the one
   table of every button's words, icon and kind. A button on every line of a list shows its
   icon alone (`icon_button`).
5. Three kinds only: `primary` (at most one per page or dialog), `danger` (deletes or clears),
   `secondary` (everything else).
6. Icon colours come from theme tokens; `retint_all` redraws them when the theme changes.

Labels are never changed by the system: a button is looked up by the words it already has.
*Held by* `tests/unit/test_button_system.py` (a button not in `BUTTONS` fails it) and
`tests/unit/test_ui_consistency.py` (every button on every page of the real window has a
tooltip, a name and an icon).

**Every dialog is built in the one frame.** `frame(dialog, title)` gives the title and the
layout; `buttons(dialog, primary=...)` is the OK and Cancel row, its OK renamed to say what it
does (the smart group dialog's says "Save smart group"), the primary the default, or Cancel with
`danger=True`. Only the About box (the brand's clear space) and the photo viewer (a photo
filling a window) keep frames of their own. No file builds its own
`QDialogButtonBox(Ok | Cancel)`. *Held by* `tests/unit/test_ui_frames.py`
(`test_every_dialog_is_built_in_the_one_frame`) and `tests/unit/test_ux_guards.py`
(`test_no_dialog_builds_its_own_ok_cancel_row`).

## 4. Menus

**Module:** `app/ui/widgets/menus.py` - every menu is built here: a list's right-click menu by
`build` / `show`, a button's drop-down by `attach(button, items)`, a menu a view pops up itself by
`kept(parent, items)`. `refill` puts new lines in one, `retint` repaints its icons (an attached
or kept menu does so each time it opens, so a theme switch reaches it), and `lines(menu)` finds
its lines by `Item.key`. `Item.choice` makes a line one of several, ticked one at a time.

1. Every line carries a 16px Lucide icon - the matching button's icon where a button does the
   same thing, otherwise `MENU_ICONS`. A line with no icon is refused when the menu is built.
2. What a double-click or Enter does is the first line, in bold.
3. A line shows its key where one exists (Enter, Ctrl+C, Delete, F2).
4. Lines of a right-click menu are grouped in the order of `GROUPS`: open, act, copy, remember,
   danger - the destructive line last, its icon in `danger`. A drop-down keeps the order its view
   gave.
5. Words are never changed by the builder.

The few `QMenu`s still built by hand - the tray's menu and its Pause submenu, the View menus, the
Search toolbar's More menu and its model submenu - are listed with their reasons in
`QMENU_ALLOWED`, counted per file. *Held by* `tests/unit/test_menu_system.py`
(`test_no_file_builds_a_right_click_menu_by_hand`,
`test_no_menu_anywhere_is_hand_built_outside_the_allowlist`).

**A trap in this PySide6.** `QAction.menu()` on a menu-bar action returns a wrapper whose menu
reads as deleted, and taking it spoils the good wrapper of the same menu too. Keep the `QMenu`
object you made (the Go menu is held as `nav.go_menu`); never look it up again through its action.

## 5. Keys

### In a list

**Module:** `app/ui/widgets/list_keys.py` - a list installs it once and names its actions.

- Double-click and Enter run the default action, from one signal (`activated`).
- **Space is quick look** on every result list (Search, Files, Mail, Code, Browse, Tidy) and on
  the Search page's photo grid: the current row opens in a window of its own, and Space again -
  in the list or in that window - closes it (`widgets/quick_look.py`). The docked preview pane is
  on Ctrl+Shift+P and the View menu. On a line with a tick box, Space ticks it. Chat, which has a
  pane and no quick look, keeps Space on its Preview toggle.
- **Photos:** Space opens the viewer and **I** shows or hides the info panel.
- Delete runs the destructive line, through the same function and confirmation as the menu.
- F2 renames where a rename exists. Ctrl+A selects all; Ctrl+C copies the path or paths.
- Keys a list does not use are left to Qt.

*Held by* `tests/unit/test_list_keys.py` (`test_every_listed_list_installs_the_behaviour`) and
`tests/unit/test_quick_look.py`.

### From a box to its list

**Module:** `app/ui/widgets/arrow_keys.py` - `forward_arrows(box, view)`, installed beside
`list_keys.install` on Files, Mail, Code and Photos; Search and the quick search do the same in
their own code. Down, Up, Page Down and Page Up in the box move the list; Enter opens the current
row once an arrow has moved it, and until the text changes - so a box whose Enter searches (Code)
still does after typing. On the Search page the arrows reach the photo grid when it is in front.

### In the window

**Module:** `app/ui/controllers/navigation_controller.py` (the window, `shell.py`, only binds).

| Key | Does |
|---|---|
| Ctrl+K | The command palette (`widgets/command_palette.py`, matching in `presenter/actions.py`): every rail page in rail order, every Settings category, the menu bar's verbs and Sync now. A line starting `>` shows the verbs only; plain text always ends with "Search for '…'". Also Go > Command palette… |
| Esc | An open find bar closes first; then the box of the page in front is cleared. A page in the background keeps what was typed in it |
| Ctrl+F | A preview's find when one has the focus; else the page's own box; else Search |
| Ctrl+1 … Ctrl+9 | The rail page at that place, top to bottom, in the order the pages were dragged to; a hidden page is skipped |
| Alt+Left / Alt+Right, the mouse's back and forward buttons | Back and forward between the pages visited, with the Photos naming page and the Settings category put back (`presenter/page_history.py`, 50 places) |
| Ctrl+P, Ctrl+M, Ctrl+E, Ctrl+I, Ctrl+, | Files, Mail, Code, Indexing, Settings |
| Ctrl+Shift+P | The preview pane, on whichever list is in front; also Preview in the quick search box, beside its own Ctrl+P |

The **Go menu** lists every rail page in rail order, Search and Chat included, then Offline,
People in photos (Photos' own naming page), Back, Forward and Command palette…. A page hidden from
the rail (its right-click menu, "Hide from the rail"; Search always stays) is still in Go and the
palette; "Show hidden pages" in that menu and in View puts them back (`widgets/rail_order.py`,
state `ui:rail_hidden`). *Held by* `tests/unit/test_ux_keys_and_palette.py`,
`tests/unit/test_page_history.py`, `tests/unit/test_palette_actions.py` and
`tests/unit/test_rail_hide.py`.

## 6. Painted rows and cards

**Modules:** `app/ui/widgets/paint_kit.py`, `app/ui/widgets/card.py`.

- **A list that paints itself paints its row state through the kit.** `paint_row_state(painter,
  rect, option, colours)` draws the rounded hover or selection fill (selection before hover, one
  row radius) and, when the row is the current one of a view that has the keyboard
  (`State_HasFocus`), a `FOCUS_WIDTH` ring in `focus_ring`. `paint_badge` draws the kind badge,
  `scaled_font` and `set_weight` the fonts, `tok` the tokens. The Search results, the quick search
  rows, the contacts list and cards and the timeline use it; the side lists ring their current
  line with `paint_focus_ring`. *Held by* `tests/unit/test_paint_kit.py`
  (`test_every_painted_selection_goes_through_the_kit`).
- **One card.** `Card` is a `QFrame` marked `card="true"`; the theme gives it the surface, the
  `border` hairline and `radius_box`, and its layout takes `CARD_MARGINS` (`SPACE_M` all round).
  `card_title(text)` is its heading. The answer above the search results, the mail header, the
  contact card and This machine are all one. *Held by* `tests/unit/test_paint_kit.py`
  (`test_the_four_cards_are_one_card`).

## 7. Icons

**Modules:** `app/ui/widgets/icons.py`, `app/ui/widgets/command_icon.py`.

- **Lucide only** (ISC licence, `assets/icons/` with its `LICENSE`). `ICON_NAMES` lists every
  glyph that ships; `icon(name, colour)` draws one in a theme colour, cached by name, colour and
  size. A missing file gives an empty icon, never a crash.
  *Held by* `tests/unit/test_button_system.py` (`test_every_icon_the_table_names_is_shipped`)
  and `tests/unit/test_menu_system.py`.
- **One table of sizes**, `ICON_SIZES`, read through `icon_size(role)`:

  | Role | Size | Where |
  |---|---|---|
  | `inline` | 14px | a glyph inside a field or a chip |
  | `menu` | 16px | a context menu or a `/` menu line |
  | `list` | 16px | a side list, a tree, a table row |
  | `button` | 16px | an action button |
  | `toolbar` | 16px | an icon-only toggle on a page's toolbar |
  | `rail` | 20px | the rail and the Chat side panel's tabs |
  | `field` | 22px | the magnifier leading the quick search box |

  These are the sizes the window used when the table was made, so moving onto it changed no
  pixel. A size given as a number in `setIconSize(QSize(n, n))` fails
  `tests/unit/test_ui_style_rules.py`; three Contacts files still do, and are listed there
  until the Contacts wave moves them (one of them, the contact photo, is a picture, not an icon).
- **The `/` menus show a Lucide icon for every switch.** `command_icon.PICTURES` names one for
  each switch in every catalogue (Search, Code and its git switches, Mail, Files, `/saved`), and
  `CONTACT_PICTURES` for the Contacts box, where `/name` is a person rather than a file. Values
  carry `chevron-right`, recent and saved searches `rotate-ccw`. The painted character in
  `Command.icon` stays as the fallback and as the terminal's mark (`app.cli`, `app/shell`).
  *Held by* `tests/unit/test_ui_style_rules.py`.

## 8. Tooltips

**Look:** one `QToolTip` rule, in `app/ui/theme.py`, from the `TOOLTIP` tokens (`surface_alt`
ground, `text` ink, `border_strong` edge) and `TOOLTIP_SIZES` (5px by 8px padding, 5px corners),
in the small text size. No other file styles a tooltip.

**What every control says** (`tests/unit/test_tooltips.py`): every actionable control has a
tooltip or a placeholder, and a placeholder is preferred where one fits - it needs no hover.
Its words state the effect: what pressing it does and what happens at the limit, in plain words.

**The wording rule** (`tests/unit/test_ui_style_rules.py`, static over every literal tooltip):

- plain words - no rich text (`<b>`, `<br>`, entities), so every tooltip is drawn alike;
- starts with a capital letter (the one exception, named in the test, is the date-format
  tooltip, which opens with the lower-case format codes it accepts);
- no white space at either end;
- never only the control's own words, and never a description of the control ("This button ...").

Every literal tooltip in `app/ui` met this rule when it was written down, so none was reworded.
Tooltips built at run time from data (a file's path, a count) are held only by
`tests/unit/test_ui_consistency.py`, which requires that every button has one.

## 9. Spacing

**Module:** `app/ui/theme.py` - the 8px rhythm: `SPACE_XS` 4, `SPACE_S` 8, `SPACE_M` 12,
`SPACE_L` 16, `SPACE_XL` 24 (`SPACING`), and 0 for flush.

- **A layout gap or margin is written by its `theme.SPACE_*` name.** A call whose every number
  the scale can name fails `tests/unit/test_ui_style_rules.py`, over every file in `app/ui`
  except the Contacts files listed there (a list that may only shrink).
- **What is still off the scale is held by a ceiling.** A call with any number off the scale -
  a 6, a 2, a 10 - is counted, mixed calls included, by `tests/unit/test_ux_guards.py`
  (`test_gaps_off_the_scale_never_grow`) against `OFF_SCALE_CEILING`. The ceiling may only go
  down: a new off-scale gap fails it, and moving a file onto the scale lowers it. The rest are
  older than the scale or deliberate - the brand's clear space round the About box among them.

## 10. Questions and errors

**Module:** `app/ui/widgets/confirm.py` - `confirm(parent, title, text, verb, ...)`.

- **Every question is asked through `confirm`.** Cancel is the default and the Escape button:
  Enter pressed to get the box out of the way never goes ahead.
- The other button names what it does ("Delete", "Move to Recycle Bin", "Allow and delete 12"),
  never Yes; it is drawn as `danger` (or `primary` for a question that destroys nothing, with
  `danger=False`). Its icon is the button table's for those words, or `glyph=` when given, else a
  bin for a destructive question and a tick for any other.
- A second, smaller paragraph (`informative`) says what is and is not at risk.

*Held by* `tests/unit/test_ui_one_app_settings.py`
(`test_a_destructive_question_defaults_to_cancel_and_names_its_verb`) and
`tests/unit/test_ux_guards.py` (`test_no_question_is_asked_outside_confirm`, which fails on a
`QMessageBox.question`, a Qt Yes button, or a `warning`/`critical`/`information` given buttons,
anywhere but `confirm.py`).

**The error box is the other kind.** `app/ui/widgets/error_box.py` - `show_error(parent, error)`,
which is what `MainWindow._show_error` calls.

- A question waits for its answer (`exec()`) and is asked only from a click. An error box is
  never waited for: it opens with `open()`, modal to the window, and the caller returns at once,
  because most errors arrive from a worker or a timer, where a nested event loop is a hang.
- One box at a time. An error that arrives while one is up waits behind it; one with the same
  words as the box up, or as one already waiting, is not queued again.
- A failure that can repeat per keystroke is never a box: it is said on the page's own line
  (`chips.say_failed`) or in Search's notice bar (`SearchView._search_failed`).

*Held by* `tests/unit/test_error_box.py`.

## 11. Saying what is happening

- **Search says when it is working.** Once rows are on screen, a thin moving line shows
  `BUSY_DELAY_MS` (120 ms) after a search goes out and goes when that search's finished answer
  lands (`widgets/search_flight.py`, decisions in `presenter/search_feedback.py`). An empty list
  shows the skeleton instead.
- **Search says when it found nothing.** The rows kept on screen are dimmed and the status line
  says "No matches for “…” - showing the previous results"; once the full search has answered
  with nothing, the list empties.
- **Sync says what it is doing, where you are.** The sync chip on the Contacts toolbar and on the
  Mail page, and a line in the tray's menu (`widgets/sync_chip.py`, words in
  `presenter/sync_chip.py`), each open the Sync window.

## 12. The 250-line view budget

Every `app/ui/*_view.py` module, and `app/ui/indexing_settings.py`, stays under 250 lines of
code (blank lines and comments not counted). A long view is where untested logic hides: the
logic belongs in `app/ui/presenter/`, which has no Qt and is tested without a window.
*Held by* `tests/unit/test_presenter.py`
(`test_every_qt_view_keeps_its_logic_in_the_presenter`).

## 13. Words

Labels, tooltips and descriptions already released are never reworded (`CLAUDE.md`). A
tooltip found to be false is reported, and corrected only on the owner's word.
