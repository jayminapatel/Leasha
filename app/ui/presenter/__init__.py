"""Everything the UI does that is not drawing — and therefore everything testable.

Layer: L5

Qt widgets cannot be instantiated without a display, so if the interesting logic
lives inside them it can only ever be verified by a person clicking. That is not
a standard this project holds anywhere else, and there is no reason for the UI
to be the exception.

So the layer is split. This module holds the decisions:

  * which search tier to run for a given keystroke,
  * how to cut a 1,600-character chunk down to a snippet centred on the match,
  * where the highlight ranges fall,
  * how to say "3 hours remaining" from a throughput measurement,
  * how to turn 4,000 skipped files into a handful of actionable rows.

The widgets in the sibling modules are thin: they draw what these functions
return and forward events back. What is left unverified is wiring, which a
person notices immediately; what is verified is the logic, which a person would
not notice being subtly wrong.

Nothing here imports Qt.

The package is split by domain; this file re-exports every name so that
`from app.ui.presenter import X` keeps working for every caller:

  formatting  sizes, dates, counts, addresses, breadcrumbs
  snippets    cutting a chunk down to a snippet around the match
  results     result rows and groups, tooltips, accessible text
  explain     "why is this here?" and how a result matched
  rows        the Files and Mail tables
  search      tiers, options, notices, hints, chips, the git pass
  commands    the `/` menu, scopes and value suggestions
  repos       repository rows and the repository filter
  code        the Code tab's routing, git rows and wording
  indexing    progress, runs started elsewhere, skips, index summary
  settings    Settings-page text and suggested folders
  offline     catalogued drives and online-only files

Anything that reads a store, the disk or a subprocess lives in `app.ui.tasks`
and is re-exported here lazily.
"""

# ruff: noqa: F401 - this file exists to re-export

from __future__ import annotations

from app.ui.presenter.commands import _log
from app.ui.presenter.code import (
    RepoFileRow,
    repo_file_rows,
    REPO_FILE_LIMIT,
    repo_files_summary,
    CodeRoute,
    _git_only,
    GIT_ONLY,
    GitScope,
    git_rows_matching,
    code_type_filter,
    code_route,
    repo_root_for,
    preset_label,
    repo_list_empty,
    code_preset,
    code_summary,
    git_summary,
    git_result_row,
)
from app.ui.presenter.commands import (
    _switch_catalogue,
    ALL_COMMANDS,
    FILES_COMMANDS,
    MAIL_COMMANDS,
    CODE_COMMANDS,
    VALUE_LIMIT,
    VALUE_LIMITS,
    CATALOGUE_PREFIX_CHARS,
    CATALOGUE_LIMIT_PREFIXED,
    catalogue_limit,
    _CATALOGUE,
    clear_format_catalogue,
    enabled_extensions,
    SlashContext,
    slash_context,
    _settled,
    value_suggestions,
    _counted_values,
    ALL_VALUES_NOTE,
    _note_all,
    scope_for,
    _SCOPE_FIELDS,
    _FILTER_FIELDS,
    VALUE_ROW,
    VALUE_NOUNS,
    CUSTOM_ROW,
    kind_expansion,
    value_page,
    value_row,
    value_rows,
    as_typed_value,
    ALL_LOCATIONS,
    _VOLUME_FILTER_TERM,
    set_volume_filter,
    volume_picker_options,
    resolved_date,
    scope_key,
    _scope_repo,
    _takes_repo,
)
from app.ui.presenter.explain import (
    RECENT_ENOUGH,
    _matched_words,
    _when,
    why_result,
    explain_for,
    MEANING_MARKER,
    why_lines,
    explain_switch_on,
    match_marker,
)
from app.ui.presenter.formatting import (
    shorten_path,
    format_count,
    format_eta,
    BREADCRUMB_PARTS,
    breadcrumb,
    format_size,
    format_when,
    _exact_date,
    _exact_date_from_epoch,
    RECIPIENTS_SHOWN,
    format_address,
    format_recipients,
    format_sent,
)
from app.ui.presenter.indexing import (
    SkipGroup,
    group_skips,
    progress_for,
    STALE_RUN_S,
    external_snapshot,
    external_is_live,
    external_run_text,
    start_blocked_reason,
    progress_text,
    finished_text,
    GRAPH_TABLE_LIMIT,
    StatRow,
    index_summary,
    _by_status,
    when_text,
    mail_summary,
    archive_summary,
)
from app.ui.presenter.offline import (
    offline_volume_note,
    online_only_note,
    VolumeRow,
    volume_rows,
    offline_media_empty_state,
    offline_media_help_text,
    rename_suggestion_text,
    delete_volume_confirmation,
    offline_media_run_summary,
)
from app.ui.presenter.repos import (
    REPO_KINDS,
    RepoRow,
    repo_rows,
    _as_ns,
    repo_summary,
    repo_empty_state,
    repo_tree_summary,
    RepoFilter,
    repo_visibility,
    repo_filter,
    repo_filter_summary,
)
from app.ui.presenter.results import (
    ResultRow,
    cell_location,
    to_row,
    to_rows,
    GROUP_FETCH_MULTIPLIER,
    fetch_depth,
    ResultGroup,
    group_results,
    _path_pieces,
    _distinguish_twins,
    _twin_breadcrumb,
    _build_group,
    _ext_of,
    KIND_LABELS,
    row_identity,
    kind_tag,
    CODE_EXTENSIONS,
    is_code_kind,
    why,
    group_subtitle,
    result_tooltip,
    accessible_text,
    results_terminator,
    Terminator,
)
from app.ui.presenter.rows import (
    FileRow,
    _STATUS_NOTES,
    file_rows,
    file_summary,
    MailRow,
    mail_rows,
    mail_filters,
)
from app.ui.presenter.search import (
    TYPING_DEBOUNCE_MS,
    IDLE_DEBOUNCE_MS,
    MIN_FULL_SEARCH_CHARS,
    Tier,
    tier_for,
    notice_register_for,
    interpret_message,
    file_query,
    search_options,
    results_message,
    status_line,
    search_shape,
    semantic_health,
    GitPass,
    git_pass,
    federated_summary,
    _DOCUMENT_NOUNS,
    kind_suggestion,
    interpret_hint,
    NOTICE_KIND_SUGGESTION,
    NOTICE_INTERPRET_HINT,
    NOTICE_FILTER_OFFER,
    filter_offers,
    _Hint,
    result_view_state,
    window_notices,
    notice_line,
    chips_for,
)
from app.ui.presenter.settings import (
    history_label_text,
    pst_status_text,
    doctor_lines,
    logs_cleared_message,
    SUGGESTED_FOLDERS,
    owns_path,
    suggested_roots,
    nothing_indexed_yet,
    cleared_message,
)
from app.ui.presenter.snippets import (
    SNIPPET_CHARS,
    Snippet,
    _term_pattern,
    build_snippet,
    _densest,
    _snap_back,
    _snap_forward,
    _find_sentence_start,
    _find_sentence_end,
    _head,
)

#: Worker bodies live in `app.ui.tasks`; they are re-exported lazily because
#: `tasks` imports the modules above and an eager import here would be a cycle.
_TASK_NAMES = frozenset({
    "filter_offer_notices",
    "repo_health_notes",
    "search_check_lines",
    "settings_labels",
    "decorate_results",
    "offline_volume_marks",
    "placeholder_marks",
    "record_open",
    "missing_paths",
    "_ATTACHMENT_MARKER",
    "_attachment_parent_path",
    "mail_details",
    "_read_external_run",
    "_scan_and_save",
    "DOCTOR_TIMEOUT_S",
    "doctor_report",
    "install_package",
    "read_index_summary",
    "folder_size",
    "LOG_SUFFIXES",
    "log_files",
    "logs_summary",
    "clear_logs",
    "index_bytes",
    "index_counts",
    "read_repo_files",
    "_comparable",
    "code_rows_for",
    "code_rows_and_repos",
    "matching_repos",
    "resolve_open_path",
})


def __getattr__(name: str):
    if name in _TASK_NAMES:
        from app.ui import tasks

        return getattr(tasks, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | _TASK_NAMES)


__all__ = [
    "cell_location",
    "GIT_ONLY",
    "CodeRoute",
    "code_route",
    "code_type_filter",
    "GitScope",
    "git_rows_matching",
    "code_rows_for",
    "code_summary",
    "git_result_row",
    "git_summary",
    "repo_root_for",
    "notice_line",
    "Tier",
    "tier_for",
    "Snippet",
    "build_snippet",
    "shorten_path",
    "format_eta",
    "format_count",
    "SkipGroup",
    "group_skips",
    "ResultRow",
    "to_row",
    "to_rows",
    "ResultGroup",
    "group_results",
    "breadcrumb",
    "fetch_depth",
    "GROUP_FETCH_MULTIPLIER",
    "FileRow",
    "file_rows",
    "RepoRow",
    "repo_rows",
    "repo_summary",
    "repo_empty_state",
    "RepoFilter",
    "repo_filter",
    "repo_filter_summary",
    "REPO_KINDS",
    "StatRow",
    "index_summary",
    "read_index_summary",
    "folder_size",
    "when_text",
    "archive_summary",
    "mail_summary",
    "doctor_report",
    "doctor_lines",
    "install_package",
    "search_shape",
    "status_line",
    "results_message",
    "mail_details",
    "missing_paths",
    "file_summary",
    "search_options",
    "decorate_results",
    "record_open",
    "why",
    "row_identity",
    "kind_tag",
    "group_subtitle",
    "result_tooltip",
    "KIND_LABELS",
    "file_query",
    "interpret_message",
    "progress_for",
    "progress_text",
    "finished_text",
    "mail_rows",
    "mail_filters",
    "MailRow",
    "semantic_health",
    "format_size",
    "format_when",
    "results_terminator",
    "Terminator",
    "CODE_EXTENSIONS",
    "is_code_kind",
    "notice_register_for",
    "SNIPPET_CHARS",
    "TYPING_DEBOUNCE_MS",
    "IDLE_DEBOUNCE_MS",
    "value_suggestions",
    "rename_suggestion_text",
    "offline_media_help_text",
    "placeholder_marks",
    "online_only_note",
    "enabled_extensions",
    "clear_format_catalogue",
    "VALUE_LIMIT",
    "VALUE_LIMITS",
]
