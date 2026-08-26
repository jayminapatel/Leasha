r"""Every repository-search switch, checked by the command it produces.

Layer: L4

`GitSearch.txt` is a specification for a repository intelligence platform, and
the owner asked for all of it *"as switches … the / style we have in the app"*.
The way to make that testable was to split the decision from the doing:
`gitquery` builds the exact `git` argument list and runs nothing, so every
switch in the specification can be asserted on a machine with no repository and
without waiting for git.

Three kinds of test here, and the middle one is the point:

* that a switch reaches the right git option at all;
* **that a switch nobody can see being ignored is not ignored** - a query
  carrying `/author` planned as a `git grep` would silently answer a wider
  question, and the only symptom would be more results than there should be;
* that ordinary text which looks like a switch is left alone, because a search
  box that rewrites what somebody typed is one they stop trusting.
"""

from __future__ import annotations

import pytest

from app.search.gitquery import (
    GIT_COMMANDS,
    KIND_GREP,
    KIND_LOG,
    KIND_NAME_STATUS,
    KIND_PATCH,
    build,
    git_command_for,
    git_matching,
    parse_git_query,
)


def plan(text: str, **kwargs):
    return build(parse_git_query(text, **kwargs))


def argv(text: str, **kwargs) -> str:
    return " ".join(plan(text, **kwargs).argv)


# --- the catalogue ----------------------------------------------------------

def test_every_switch_has_an_icon_a_summary_and_an_example():
    """The `/` menu draws all three. A row missing one reads as a fault."""
    for command in GIT_COMMANDS:
        assert command.icon.strip(), command.name
        assert command.summary.strip(), command.name
        assert command.example.startswith("/"), command.name


def test_no_two_switches_share_a_spelling():
    """An alias claimed twice means one of them silently never resolves."""
    seen: dict[str, str] = {}
    for command in GIT_COMMANDS:
        for spelling in command.spellings:
            assert spelling not in seen, (
                f"'{spelling}' is claimed by {seen.get(spelling)} and {command.name}")
            seen[spelling] = command.name


def test_the_menu_narrows_as_letters_are_typed():
    assert [c.name for c in git_matching("hist")] == ["history"]
    assert len(git_matching("")) == len(GIT_COMMANDS)


def test_an_alias_resolves_to_its_switch():
    assert git_command_for("hist").name == "history"
    assert git_command_for("/b").name == "branch"
    assert git_command_for("ext").name == "extension"


# --- where ------------------------------------------------------------------

def test_a_plain_search_reads_the_checkout_and_says_so():
    """The cheap case, and the default. `git grep` over one revision costs the
    size of the checkout and nothing to do with history."""
    made = plan("CustomerId")

    assert made.kind == KIND_GREP
    assert made.slow is False
    assert "git grep" in " ".join(made.argv)
    assert "checkout" in made.explain


def test_branches_are_searched_by_name():
    assert argv("CustomerId /branch develop").endswith("develop")
    assert argv("x /branch develop,release").endswith("develop release")


def test_all_branches_and_remote_branches_are_different_things():
    assert "--branches" in argv("x /all-branches")
    assert "--remotes" in argv("x /remote-branches")


def test_a_commit_a_tag_and_a_range_each_reach_git_as_a_revision():
    assert argv("x /commit a1b2c3d").endswith("a1b2c3d")
    assert argv("x /tag v5.1").endswith("v5.1")
    assert "v5.0..v6.0" in argv("x /range v5.0..v6.0")


def test_history_is_the_slow_plan_and_is_marked_as_one():
    """**The flag the UI needs.** History search is seconds to minutes, and the
    first non-negotiable is that nothing unbounded sits behind Enter. A caller
    that cannot tell the two apart cannot honour that."""
    made = plan("CustomerId /history")

    assert made.kind == KIND_LOG
    assert made.slow is True
    assert "git log" in " ".join(made.argv)


def test_a_history_search_is_always_bounded():
    """`git log -S` diffs every commit it walks. Measured on this project:
    1.59s for 75 commits. Unbounded, it is a search that never returns."""
    assert "--max-count=2000" in argv("x /history")
    assert "--max-count=500" in argv("x /history /depth 500")


def test_lifetime_is_every_branch_and_tag():
    assert "--all" in argv("x /lifetime")


# --- how --------------------------------------------------------------------

def test_plain_text_is_a_fixed_string_not_a_pattern():
    """Somebody searching for `a.b(c)` means those characters. Reading it as a
    regex quietly returns the wrong thing rather than nothing, which is worse."""
    assert "--fixed-strings" in argv("a.b(c)")


def test_regex_and_whole_word_and_case_reach_git():
    assert "--extended-regexp" in argv("Order[A-Z]+ /regex")
    assert "--word-regexp" in argv("Id /word")
    assert " -i " in " " + argv("id /ignore-case") + " "


@pytest.mark.parametrize("switch,expected", [
    ("class", "class|record|struct"),
    ("interface", "interface|protocol|trait"),
    ("function", "def|fn|func|function|sub"),
])
def test_a_declaration_switch_becomes_a_pattern_for_the_declaration(switch, expected):
    """`/class OrderService` looks for where it is *declared*, not every line
    that mentions it - which on a real codebase is the difference between one
    result and four hundred."""
    line = argv(f"/{switch} OrderService")

    assert expected in line
    assert "OrderService" in line
    assert "--extended-regexp" in line


def test_the_name_can_come_before_or_after_the_switch():
    """`OrderService /class` is how somebody types it after the fact, and
    dropping the switch there made it a plain text search that quietly answered
    a different question."""
    assert argv("/class OrderService") == argv("OrderService /class")


def test_the_declaration_name_is_escaped():
    """A name with a dot in it is a name, not "any character"."""
    assert r"Order\.Service" in argv("/class Order.Service")


# --- which files ------------------------------------------------------------

def test_paths_and_extensions_become_pathspecs():
    line = argv("x /path src/services /extension cs")

    assert " -- " in " " + line
    assert "src/services/**" in line
    assert "*.cs" in line


def test_exclusions_are_magic_pathspecs():
    assert ":(exclude)node_modules/**" in argv("x /exclude-path node_modules")
    assert ":(exclude)*.generated.cs" in argv("x /exclude-file *.generated.cs")


def test_a_bare_file_name_matches_it_anywhere_in_the_tree():
    """`--file OrderService.cs` means the file, wherever it lives. A pathspec
    without `**/` matches it only at the repository root, which is almost never
    where it is."""
    assert "**/OrderService.cs" in argv("x /file OrderService.cs")


def test_pathspecs_are_separate_arguments_so_a_space_needs_no_quoting():
    made = plan('x /path "My Documents"')

    assert "My Documents/**" in made.argv


# --- who and when -----------------------------------------------------------

def test_asking_who_or_when_makes_it_a_history_search():
    """**The switch that would have been silently dropped.** An author and a
    date are properties of a *commit*; `git grep` has nowhere to put them. A
    query carrying them and planned as a grep answers a wider question than the
    one typed, and the only symptom is too many results.
    """
    for text in ("x /author dave", "x /since 2025-01-01", "x /merges none"):
        made = plan(text)
        assert made.kind == KIND_LOG, f"{text} was planned as a {made.kind}"


def test_author_and_dates_reach_git():
    line = argv('x /author "John Smith" /since 2025-01-01 /until 2025-12-31')

    assert "--author=John Smith" in line
    assert "--since=2025-01-01" in line
    assert "--until=2025-12-31" in line


def test_merges_can_be_included_excluded_or_alone():
    assert "--merges" in argv("x /merges only")
    assert "--no-merges" in argv("x /merges none")
    assert "--merges" not in argv("x /history")


def test_a_nonsense_merges_value_falls_back_rather_than_reaching_git():
    """git would reject it, and a typo in a filter should not become an error
    dialog about a command the person never typed."""
    assert parse_git_query("x /merges sideways").merges == "any"


# --- what happened ----------------------------------------------------------

def test_introduced_reverses_the_walk_and_keeps_one_row():
    """**`--max-count=1 --reverse` returns nothing at all.**

    Measured, not assumed: `git log -Snotice_line --reverse --max-count=1` on
    this repository prints nothing, while the same command without the count
    prints the commit that introduced it. So keeping one row is this
    application's job, and the obvious spelling would have shipped a switch
    that silently found nothing.
    """
    made = plan("notice_line /introduced")

    assert "--reverse" in made.argv
    assert "--max-count=1" not in made.argv
    assert made.first_only is True


def test_removed_asks_for_the_commits_that_deleted_it():
    assert "--diff-filter=D" in argv("x /removed")


def test_changed_uses_G_rather_than_S():
    """`-S` is "the number of occurrences changed" - when it appeared or
    disappeared. `-G` is "the diff mentions it" - every touch. `/changed` is
    exactly the request for the second."""
    assert "-Gx" in argv("x /changed")
    assert "-Sx" in argv("x /history")


def test_added_and_removed_lines_need_the_patch_and_are_filtered_here():
    """git has no switch for "only the deleted lines" - `--diff-filter` selects
    files, not lines - so the plan says which side to keep, and the reader
    honours it. Without that, "removed only" shows added lines too."""
    made = plan("x /history /removed-only")

    assert made.kind == KIND_PATCH
    assert "--patch" in made.argv
    assert made.line_filter == "-"
    assert build(parse_git_query("x /history /added-only")).line_filter == "+"


def test_file_history_follows_renames():
    made = plan("/file-history src/Order.cs")

    assert made.kind == KIND_NAME_STATUS
    assert "--follow" in made.argv
    assert "--name-status" in made.argv
    assert made.argv[-1] == "src/Order.cs"


def test_deleted_files_only_asks_for_deletions():
    assert "--diff-filter=D" in argv("x /deleted-files")


# --- text that looks like a switch ------------------------------------------

def test_a_path_is_not_a_switch():
    """**`api` is an alias of `endpoint`.** Without an anchored pattern,
    `/api/orders` - plainly a route somebody is searching for - parsed as the
    `/api` switch carrying `/orders`, and the search silently became something
    else. On a tab about source code this is the single most likely thing to be
    typed.
    """
    query = parse_git_query("/api/orders")

    assert query.text == "/api/orders"
    assert query.declaration == ""


def test_an_explicit_equals_is_unambiguous_and_is_honoured():
    assert parse_git_query("/endpoint=/api/orders").declaration == "endpoint"


def test_an_unknown_switch_stays_as_search_text():
    """The same rule the index's parser follows. Rejecting it would refuse a
    query; swallowing it would answer a different one."""
    assert "/wibble" in parse_git_query("/wibble thing").text


def test_a_half_typed_query_does_not_raise():
    """This is parsed on every keystroke while somebody is still typing."""
    for text in ("", "/", "/bra", 'x /author "unclosed', "//", "x /"):
        assert parse_git_query(text) is not None


def test_quotes_hold_a_value_together():
    assert parse_git_query('/author "John Smith"').author == "John Smith"


# --- the sentence above the results -----------------------------------------

def test_the_plan_says_what_it_searched():
    """**Not decoration.** "No matches" means nothing until you know whether it
    looked at one commit or nine thousand, and the commonest way a repository
    search misleads is by answering a narrower question than the one asked."""
    made = plan("x /history /depth 5000 /author dave /extension cs")

    assert "5,000 commits" in made.explain
    assert "dave" in made.explain
    assert ".cs" in made.explain


def test_a_history_plan_never_claims_to_be_the_checkout():
    """`/file-history` and `/lifecycle` reach into the past without naming a
    revision, and a line saying "the current checkout" above nine thousand
    commits of results is the kind of wrong nobody double-checks."""
    assert "checkout" not in plan("/file-history a.py").explain
    assert "checkout" not in plan("x /lifecycle").explain


# -- /message: the commit's own words ----------------------------------------
#
# From `docs/WORKORDER-202626081801-git-sharpness-and-mail-preview.md` §1.
# Raised by the owner: *"the code does not search ability to search through the
# search commit messages"*. It could not: `-S` and `-G` search the content of a
# diff, `--grep` searches what the commit said about itself, and only the first
# had a switch. `GitSearch.txt` UC-020 asks for both.

def test_a_message_search_greps_the_log_rather_than_the_diff():
    plan = build(parse_git_query("/message licence"))

    assert "--grep=licence" in plan.argv
    assert not any(a.startswith(("-S", "-G")) for a in plan.argv), (
        "a message search must not walk diffs")


def test_a_message_search_is_history_but_is_not_slow():
    """**The one history search that costs nothing much.** `--grep` reads the
    commit header; `-S` diffs every commit it walks - 1.59s for 75 commits, the
    measurement that made history a separate explicit job. Billing a header
    scan as slow puts a confirmation in front of a search that does not need
    one."""
    query = parse_git_query("/message licence")

    assert query.wants_history(), "git grep has nowhere to put a commit message"
    assert query.is_message_only()
    assert build(query).slow is False


def test_a_message_and_a_pattern_ask_git_for_both():
    """`/message licence CustomerId` is "said licence *and* touched
    CustomerId". git ANDs them, which is the useful reading."""
    plan = build(parse_git_query("CustomerId /message licence"))

    assert "--grep=licence" in plan.argv
    assert "-SCustomerId" in plan.argv
    assert plan.slow is True, "it walks diffs again, so it costs again"


def test_a_message_search_composes_with_who_when_and_where():
    plan = build(parse_git_query(
        "/message licence /author dave /since 2025-01-01 /branch develop"))

    assert "--grep=licence" in plan.argv
    assert "--author=dave" in plan.argv
    assert "--since=2025-01-01" in plan.argv
    assert "develop" in plan.argv


def test_the_aliases_work():
    for line in ("/msg fix", "/subject fix"):
        assert "--grep=fix" in build(parse_git_query(line)).argv, line


def test_the_explanation_says_which_question_was_asked():
    """"No matches" means nothing until you know whether it read messages or
    diffs."""
    explain = build(parse_git_query("/message licence")).explain

    assert "message" in explain.lower()
    assert "licence" in explain


def test_a_message_switch_is_git_only():
    """It has no meaning against the index, so the router must send it to git
    rather than searching the corpus for the word "message"."""
    from app.search.gitquery import GIT_ONLY

    assert "message" in GIT_ONLY
