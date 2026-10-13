# UI style guide - One Leasha

**Doc version:** 1.0 · **Updated:** 2026-10-13 · **Applies to:** app v1.0.3

How Leasha's window looks and behaves, as it is in the code today. Every rule names the
module that carries it and the test that fails when it is broken. Where this guide and the
code disagree, the code is what ships and this guide is wrong - fix the guide.

The owner's brief (2026-10-11): *make it all consistent from tool tips to performance to
unified look ... icons are must.* Every screen should feel like one app.

## 1. Colours, text sizes and corners - the theme

**Module:** `app/ui/theme.py`.

- **One set of token names, two sets of values.** `PALETTES` holds the light and the dark
  theme under the same keys (`window`, `surface`, `surface_alt`, `text`, `text_dim`,
  `text_faint`, `accent`, `danger`, ...). Every stylesheet rule names a token; Python
  substitutes the values before the sheet reaches Qt, because Qt stylesheets have no variables.
  *Held by* `tests/unit/test_theme.py` (`test_both_themes_define_exactly_the_same_tokens`,
  `test_the_stylesheet_has_no_unsubstituted_tokens`).
- **No colour is written anywhere else.** A widget asks `theme_colours()` for a token.
  *Held by* `tests/unit/test_ui_one_app_colours.py`
  (`test_no_colour_in_the_window_is_a_literal_outside_the_theme`), which also checks every
  status colour reads on a card and on the window.
- **Text sizes are steps of one body size**, in points, never pixels: `small`, `body`,
  `large` and `display` (`SCALE`), all following the system font and "Make text bigger".
  *Held by* `tests/unit/test_theme.py` (`test_no_font_size_is_measured_in_pixels`).
- **Corners** come from `RADIUS` (`radius_input` 5px, `radius_control` 8px, `radius_box` 10px,
  and the pill radius, which follows the text size - `pill_radius`).
- **The system theme is followed**, with a manual override (`SCHEMES`: system, light, dark).

## 2. Buttons

**Module:** `app/ui/widgets/buttons.py` (its docstring has the rules in full).

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

## 3. Right-click menus

**Module:** `app/ui/widgets/menus.py` - every list's menu is built by `build` / `show`.

1. Every line carries a 16px Lucide icon - the matching button's icon where a button does the
   same thing, otherwise `MENU_ICONS`. A line with no icon is refused when the menu is built.
2. What a double-click or Enter does is the first line, in bold.
3. A line shows its key where one exists (Enter, Ctrl+C, Delete, F2).
4. Lines are grouped in the order of `GROUPS`: open, act, copy, remember, danger - the
   destructive line last, its icon in `danger`.
5. Words are never changed by the builder.

*Held by* `tests/unit/test_menu_system.py`, including
`test_no_file_builds_a_right_click_menu_by_hand`.

## 4. List keys

**Module:** `app/ui/widgets/list_keys.py` - a list installs it once and names its actions.

- Double-click and Enter run the default action, from one signal (`activated`).
- Space shows the preview where the page has one; on a line with a tick box, Space ticks it.
- Delete runs the destructive line, through the same function and confirmation as the menu.
- F2 renames where a rename exists. Ctrl+A selects all; Ctrl+C copies the path or paths.
- Keys a list does not use are left to Qt.

*Held by* `tests/unit/test_list_keys.py` (`test_every_listed_list_installs_the_behaviour`).

## 5. Icons

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
  `tests/unit/test_ui_style_rules.py`; the Contacts files still do, and are listed there
  until the Contacts wave moves them.
- **The `/` menus show a Lucide icon for every switch.** `command_icon.PICTURES` names one for
  each switch in every catalogue (Search, Code and its git switches, Mail, Files, `/saved`), and
  `CONTACT_PICTURES` for the Contacts box, where `/name` is a person rather than a file. Values
  carry `chevron-right`, recent and saved searches `rotate-ccw`. The painted character in
  `Command.icon` stays as the fallback and as the terminal's mark (`app.cli`, `app/shell`).
  *Held by* `tests/unit/test_ui_style_rules.py`.

## 6. Tooltips

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

## 7. Spacing

**Module:** `app/ui/theme.py` - the 8px rhythm: `SPACE_XS` 4, `SPACE_S` 8, `SPACE_M` 12,
`SPACE_L` 16, `SPACE_XL` 24 (`SPACING`), and 0 for flush.

- A gap or margin the scale can name is written by its name. *Held by*
  `tests/unit/test_ui_style_rules.py`, over every file in `app/ui` except the Contacts files
  listed there.
- **Numbers off the scale are still in use** - mostly 6, 2 and 10, about ninety layout calls
  in all, and the brand's clear space round the About box. They are older than the scale, and
  moving one changes the picture, so each waits for a pass that looks at the window. A new
  layout uses the scale.

## 8. Confirm dialogs

**Module:** `app/ui/widgets/confirm.py` - `confirm(parent, title, text, verb, ...)`.

- Cancel is the default and the Escape button: Enter pressed to get the box out of the way
  never goes ahead.
- The other button names what it does ("Delete", "Move to Recycle Bin"), never Yes; it is
  drawn as `danger` (or `primary` for a question that destroys nothing).
- A second, smaller paragraph (`informative`) says what is and is not at risk.

*Held by* `tests/unit/test_ui_one_app_settings.py`
(`test_a_destructive_question_defaults_to_cancel_and_names_its_verb`).

## 9. The 250-line view budget

Every `app/ui/*_view.py` module, and `app/ui/indexing_settings.py`, stays under 250 lines of
code (blank lines and comments not counted). A long view is where untested logic hides: the
logic belongs in `app/ui/presenter/`, which has no Qt and is tested without a window.
*Held by* `tests/unit/test_presenter.py`
(`test_every_qt_view_keeps_its_logic_in_the_presenter`).

## 10. Words

Labels, tooltips and descriptions already released are never reworded (`CLAUDE.md`). A
tooltip found to be false is reported, and corrected only on the owner's word.
