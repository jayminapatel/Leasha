r"""Reading what git said, on a machine with no git and no repository.

Layer: L4

`gitquery` decides what to run and `gitsearch.run_query` runs it and reads the
output. The reading is where a repository search quietly goes wrong: git's
formats are terse, ambiguous in places, and different for every mode, and a
parser that is right on one repository and wrong on another produces *plausible*
results rather than an error.

The two that would have shipped broken:

* `git grep` prefixes the revision **only when one was named**, so a fixed field
  count reads `path:line:text` as `rev:path:line`;
* a commit subject can contain anything except a newline, so a header parsed on
  any separator that can appear in the data is a parser that works on your
  repository and fails on somebody else's.

Every invocation goes through `runner`, the same seam the measurement uses.
"""

from __future__ import annotations

from app.search.gitquery import parse_git_query
from app.search.gitsearch import run_query

HEADER = "a1b2c3d4e5f6\t2025-06-01\tDave Smith\tFix the customer id lookup"
HEADER_2 = "b2c3d4e5f6a1\t2025-05-01\tPriya\tAdd OrderService"


def runner_for(out: str, code: int = 0, err: str = ""):
    """A fake subprocess returning invented git output."""
    calls = []

    def run(args, cwd, timeout):
        calls.append(list(args))
        return (code, out, err)

    run.calls = calls
    return run


def found(text: str, out: str, **kwargs):
    return run_query("/repo", parse_git_query(text),
                     runner=runner_for(out, **kwargs))


# --- git grep ---------------------------------------------------------------

def test_a_working_tree_hit_is_path_line_text():
    result = found("CustomerId", "app/order.py:42:    self.CustomerId = 1\n")

    assert result.ok is True
    row = result.rows[0]
    assert (row.path, row.line_no) == ("app/order.py", 42)
    assert "CustomerId" in row.text
    assert row.commit == ""


def test_a_hit_in_a_named_revision_carries_the_revision():
    """**The prefix is there only when a revision was named**, and reading a
    fixed number of fields gets it exactly backwards on the other case."""
    result = found("CustomerId /branch develop",
                   "develop:app/order.py:42:  x = CustomerId\n")

    row = result.rows[0]
    assert row.commit == "develop"
    assert row.path == "app/order.py"
    assert row.line_no == 42


def test_a_path_containing_a_colon_is_still_read_correctly():
    """Rare on Windows and legal everywhere else. Counting fields from the left
    turns it into a revision that does not exist."""
    result = found("x", "src/a:b/order.py:7:hit\n")

    row = result.rows[0]
    assert row.line_no == 7
    assert row.text == "hit"


def test_no_matches_is_not_a_failure():
    """Exit code 1 means "found nothing" for both grep and log. Treating it as
    an error reports every unsuccessful search as a broken tool."""
    result = found("nothing", "", code=1)

    assert result.ok is True
    assert result.rows == []
    assert result.error == ""


def test_a_real_failure_is_reported_with_the_command():
    """So a surprising result can be reproduced by hand - and so a bad revision
    says so rather than looking like an empty repository."""
    result = found("x /commit nope", "", code=128, err="fatal: bad revision")

    assert result.ok is False
    assert "bad revision" in result.error
    assert "git" in result.command[0]


# --- git log ----------------------------------------------------------------

def test_a_commit_row_carries_who_when_and_why():
    result = found("CustomerId /history", HEADER + "\n" + HEADER_2 + "\n")

    assert [row.kind for row in result.rows] == ["commit", "commit"]
    first = result.rows[0]
    assert first.commit.startswith("a1b2c3d")
    assert first.date == "2025-06-01"
    assert first.author == "Dave Smith"
    assert "customer id" in first.subject


def test_a_subject_containing_a_tab_is_not_truncated():
    """Anything except a newline is legal in a subject. The header is split on
    a fixed number of leading fields and the rest is the subject, so a tab in
    the message costs nothing."""
    result = found("x /history", "abc1234567\t2025-01-01\tDave\ttidy\tup\n")

    assert result.rows[0].subject == "tidy\tup"


def test_a_line_that_is_not_a_header_is_ignored_rather_than_guessed_at():
    """git prints warnings and blank lines. A parser that turns them into rows
    produces results nobody can act on."""
    result = found("x /history",
                   "warning: something\n\n" + HEADER + "\n")

    assert len(result.rows) == 1


# --- patches ----------------------------------------------------------------

PATCH = "\n".join([
    HEADER,
    "diff --git a/app/order.py b/app/order.py",
    "index 111..222 100644",
    "--- a/app/order.py",
    "+++ b/app/order.py",
    "@@ -10 +10 @@",
    "-    old = CustomerId",
    "+    new = CustomerId",
    "",
])


def test_only_deleted_lines_come_back_for_removed_only():
    """**git has no switch for this.** `--diff-filter` selects files, not
    lines, so "removed only" showing added lines under a heading that says
    removed is exactly what happens if the reader does not do it."""
    result = found("CustomerId /history /removed-only", PATCH)

    assert [row.text.strip() for row in result.rows] == ["old = CustomerId"]
    assert result.rows[0].status == "-"


def test_only_added_lines_come_back_for_added_only():
    result = found("CustomerId /history /added-only", PATCH)

    assert [row.text.strip() for row in result.rows] == ["new = CustomerId"]


def test_a_changed_line_is_attributed_to_its_commit_and_file():
    """A diff line with no commit and no file is a string nobody can act on."""
    row = found("CustomerId /history /added-only", PATCH).rows[0]

    assert row.commit.startswith("a1b2c3d")
    assert row.path == "app/order.py"
    assert row.author == "Dave Smith"


def test_diff_furniture_is_not_mistaken_for_content():
    """`--- a/file` and `+++ b/file` begin with the same characters as a
    deleted and an added line, and are the first thing a naive reader returns."""
    texts = [row.text for row in found("x /history /removed-only", PATCH).rows]

    assert not any(text.startswith("- a/") for text in texts)
    assert "-- a/app/order.py" not in texts


# --- a file's life ----------------------------------------------------------

def test_file_history_reads_the_status_letters():
    out = "\n".join([
        HEADER, "M\tapp/order.py", "",
        HEADER_2, "A\tapp/order.py", "",
    ])
    result = found("/file-history app/order.py", out)

    assert [row.status for row in result.rows] == ["M", "A"]
    assert all(row.kind == "change" for row in result.rows)


def test_a_rename_names_where_it_came_from():
    """The whole point of following renames is knowing the old name."""
    out = HEADER + "\nR100\tapp/old.py\tapp/new.py\n"
    row = found("/file-history app/new.py", out).rows[0]

    assert row.status == "R"
    assert row.path == "app/new.py"
    assert "app/old.py" in row.text


# --- the guarantees ---------------------------------------------------------

def test_introduced_keeps_one_row_even_though_git_returned_many():
    """git cannot do this itself - see `test_gitquery`. If the trimming is not
    done here, `/introduced` answers with the whole history reversed."""
    result = found("x /introduced", HEADER + "\n" + HEADER_2 + "\n")

    assert len(result.rows) == 1
    assert result.rows[0].commit.startswith("a1b2c3d")
    assert result.truncated is False, "one row by design is not a truncated list"


def test_a_capped_result_says_it_was_capped():
    """A truncated list that does not say so is a wrong answer."""
    out = "".join(f"app/f{n}.py:1:hit\n" for n in range(50))
    result = run_query("/repo", parse_git_query("hit"), limit=10,
                       runner=runner_for(out))

    assert len(result.rows) == 10
    assert result.truncated is True


def test_the_result_carries_the_sentence_saying_what_was_searched():
    """It has to travel with the answer: "no matches" means nothing until you
    know whether it looked at one commit or nine thousand."""
    result = found("x /history /depth 300", "")

    assert "300 commits" in result.explain


def test_the_command_is_recorded_so_a_surprise_can_be_reproduced():
    result = found("CustomerId /history", "")

    assert "-SCustomerId" in result.command


def test_a_timeout_is_a_result_rather_than_an_exception():
    """A search that did not finish has answered the question, and this is
    reached from a button - a traceback there is a bug report about something
    that is not a bug."""
    result = found("x /history", "", code=124, err="timed out after 120s")

    assert result.ok is False
    assert "timed out" in result.error
