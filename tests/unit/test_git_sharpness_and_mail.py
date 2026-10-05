r"""A tree that answers the question asked, and a mail preview that is the mail.

Layer: L1 and L5

`WORKORDER-202626081801-git-sharpness-and-mail-preview.md` §2 and §3. Two
reports in the owner's own words:

> *"when search criteria is typed in git view it should only show gits that
> have documents which match not all gits"*

> *"also the mails dont preview properly"*

**§2 - the tree.** `show_repos` was given the full list and drew it, whatever
was typed. A tree is read as *"these are the repositories that have what you
asked for"*, so a term matching files in one checkout left the other three
sitting there as though they matched too. The same fault as `code_type_filter`
and the empty-list states in the code-tab order §4 and §5 - a pane showing
something the query did not ask for, with nothing saying why - and worse here,
because of what a tree implies.

The load-bearing constraint is that the count comes from **the same query the
list ran**. A separately-scheduled count is precisely how a tree and a list come
to disagree, which is that order's §5.

**§3 - the preview.** Mail previewed the *indexed* text: no headers, the quoted
thread stripped at index time with nothing saying so, and a blank line at every
chunk boundary. Reading from `chunks` is the right source and stays - there is
nothing on disk that is *this* message - but three presentation decisions had
fallen out of it rather than being taken.
"""

from __future__ import annotations

import pytest

from app.storage.sqlite_store import SqliteStore


# ---------------------------------------------------------------------------
# §2 - which repositories hold a match
# ---------------------------------------------------------------------------

def _repo_corpus(store):
    """Two repositories, one file each, matching different words."""
    alpha = store.upsert_repo(r"D:\code\alpha", kind="work")
    beta = store.upsert_repo(r"D:\code\beta", kind="work")

    one = store.upsert_file(r"D:\code\alpha\pump.py", size_bytes=10, mtime_ns=1,
                            ext="py", parent_dir=r"D:\code\alpha", repo_id=alpha)
    store.replace_chunks(one, [{"ordinal": 0, "text": "commissioning the pump",
                                "page": None, "char_start": 0, "char_end": 22}])
    store.mark_indexed(one)

    two = store.upsert_file(r"D:\code\beta\valve.py", size_bytes=10, mtime_ns=1,
                            ext="py", parent_dir=r"D:\code\beta", repo_id=beta)
    store.replace_chunks(two, [{"ordinal": 0, "text": "replacing the valve",
                                "page": None, "char_start": 0, "char_end": 19}])
    store.mark_indexed(two)
    return alpha, beta


def _parsed(text: str):
    from app.search.commands import expand_slashes
    from app.search.query import parse_query

    return parse_query(expand_slashes(text))


def test_only_repositories_holding_a_match_are_returned(tmp_path):
    """A5. Typed criteria narrow the set; the others are not in it."""
    with SqliteStore(tmp_path / "index.db") as store:
        alpha, beta = _repo_corpus(store)

        assert store.repos_with_matches(_parsed("pump").scoped("code")) == {alpha}
        assert store.repos_with_matches(_parsed("valve").scoped("code")) == {beta}


def test_a_name_match_lights_up_its_repository_too(tmp_path):
    """The list unions name and contents, so this must union them identically.

    A repository whose only match is a *filename* has to appear, or the tree and
    the list disagree about the same file.
    """
    with SqliteStore(tmp_path / "index.db") as store:
        alpha, _beta = _repo_corpus(store)

        assert alpha in store.repos_with_matches(_parsed("pump.py").scoped("code"))


def test_an_empty_box_narrows_nothing(tmp_path):
    """A6. Clearing the criteria restores every repository."""
    with SqliteStore(tmp_path / "index.db") as store:
        alpha, beta = _repo_corpus(store)

        assert store.repos_with_matches(_parsed("").scoped("code")) == {alpha, beta}


def test_a_switch_alone_still_narrows(tmp_path):
    """`/type py` with no words is a complete request, as it is everywhere else."""
    with SqliteStore(tmp_path / "index.db") as store:
        alpha, beta = _repo_corpus(store)

        assert store.repos_with_matches(_parsed("/type py").scoped("code")) == {
            alpha, beta}
        assert store.repos_with_matches(_parsed("/type cs").scoped("code")) == set()


def test_nothing_matching_is_an_empty_set_rather_than_everything(tmp_path):
    """A tree quietly falling back to "all" is the bug being fixed."""
    with SqliteStore(tmp_path / "index.db") as store:
        _repo_corpus(store)

        assert store.repos_with_matches(_parsed("nothingmatchesthis").scoped("code")) == set()


def test_the_summary_states_the_count_and_says_nothing_when_nothing_was_asked():
    """A5/A6. A tree that has silently shrunk is as bad as one that shows all."""
    from app.ui.presenter import repo_tree_summary

    assert repo_tree_summary({"a", "b"}, 4) == "2 of 4 repositories match"
    assert repo_tree_summary(set(), 4) == "No repositories match — 4 indexed"
    assert repo_tree_summary({"a", "b", "c", "d"}, 4) == "All 4 repositories match"
    assert repo_tree_summary(None, 4) == "", "claimed a count for an empty box"


def test_the_tree_hides_rather_than_greys(tmp_path):
    """A tree of four with three inert rows is the noise being removed."""
    pytest.importorskip("PySide6")
    from PySide6.QtWidgets import QApplication

    from app.ui.widgets.git_tree import GitTree

    QApplication.instance() or QApplication([])
    tree = GitTree()
    repos = [{"id": 1, "name": "alpha", "root_path": r"D:\a", "files": 3},
             {"id": 2, "name": "beta", "root_path": r"D:\b", "files": 4}]

    tree.show_repos(repos, {"alpha"})

    labels = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert any("alpha" in one for one in labels)
    assert not any("beta" in one for one in labels)

    tree.show_repos(repos, None)                 # nothing asked: all of them
    labels = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert any("beta" in one for one in labels)


def test_narrowing_the_tree_runs_no_subprocess():
    """A7. It happens on a keystroke, so it may not shell out to git.

    The guard that already exists for this - `test_nothing_that_runs_on_a_
    keystroke_imports_this` - caught the first version of the git tree doing
    exactly that. This is the same rule aimed at the new function.
    """
    import ast
    import inspect

    from app.ui import presenter

    # **The code, not the prose.** The first version of this grepped the source
    # text and failed on the docstring explaining why there is no subprocess -
    # a test that cannot tell an explanation from the thing it explains is a
    # test that will be silenced rather than believed.
    tree = ast.parse(inspect.getsource(presenter.matching_repos).strip())
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else
        getattr(node.func, "id", "")
        for node in ast.walk(tree) if isinstance(node, ast.Call)
    }
    imported = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }

    assert "subprocess" not in imported
    assert not {"run_query", "search_history", "scope_rows"} & called
    assert not {name for name in imported if "gitsearch" in name}


# ---------------------------------------------------------------------------
# §3 - the preview is the message
# ---------------------------------------------------------------------------

class _Chunk:
    def __init__(self, text: str) -> None:
        self.text = text


class _Store:
    def __init__(self, chunks) -> None:
        self._chunks = chunks

    def chunks_for_file(self, _file_id):
        return self._chunks


class _Row:
    file_id = 7
    sender = "dave@acme.com"
    recipients = "priya@acme.com"
    sent = "2024-06-01 09:14"
    subject = "Pump station commissioning"
    attachment = "Yes"
    quoted_removed = 38_609
    name = "Pump station commissioning"
    path = r"D:\mail.pst\E1"


def test_the_preview_carries_the_headers():
    """A8. From, To, Sent and Subject above the body.

    They are columns in the table already, so a message read on its own had no
    context at all - the one view where context matters most.
    """
    from app.ui.preview_loader import mail_body

    body = mail_body(_Store([_Chunk("The valves arrive Tuesday.")]), _Row())

    assert "From: dave@acme.com" in body
    assert "To: priya@acme.com" in body
    assert "Sent: 2024-06-01 09:14" in body
    assert "Subject: Pump station commissioning" in body
    assert "Attached: Yes" in body
    assert "The valves arrive Tuesday." in body


def test_the_header_block_sits_above_the_body_not_inside_it():
    from app.ui.preview_loader import mail_body

    body = mail_body(_Store([_Chunk("BODY-MARKER")]), _Row())

    assert body.index("From:") < body.index("BODY-MARKER")


def test_a_message_with_no_stored_text_still_shows_its_headers():
    """Better than a blank pane: the row's own facts are worth reading."""
    from app.ui.preview_loader import mail_body

    body = mail_body(_Store([]), _Row())

    assert "From: dave@acme.com" in body


def test_the_preview_says_what_was_stripped_and_how_much():
    r"""A9. **The honesty this pane owed and did not pay.**

    Quoted replies are stripped at index time - correctly, or a thread quoted
    twenty times is indexed twenty times - so a reply previews with the
    conversation it answers gone. With nothing saying so it reads as a message
    sent without context.
    """
    from app.ui.preview_loader import quoted_notice

    note = quoted_notice(38_609)

    assert "38,609" in note
    assert "quoted" in note.lower()


def test_nothing_is_claimed_for_a_message_that_does_not_know():
    r"""**`None` is not zero.**

    A message indexed before schema v12 genuinely does not know how much was
    removed, and "nothing was removed" would be an invention.
    """
    from app.ui.preview_loader import quoted_notice

    assert quoted_notice(None) == ""
    assert quoted_notice(0) == ""
    assert quoted_notice("") == ""


def test_a_message_spanning_chunks_reads_as_continuous_prose():
    """A10. Chunk boundaries are an indexing decision and must not be visible."""
    from app.ui.preview_loader import mail_body

    body = mail_body(
        _Store([_Chunk("The first part of a long sentence "),
                _Chunk("that was split by the chunker.")]),
        _Row())

    assert "long sentence that was split" in body
    assert "\n\n\n" not in body


def test_the_amount_survives_a_round_trip_through_the_store(tmp_path):
    """A9 needs the number persisted, not just logged - hence schema v12."""
    import json

    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(r"D:\mail.pst\E1", size_bytes=10, mtime_ns=1,
                                    source_kind="pst_message")
        store.set_message(file_id, subject="Re: valves", sender="dave",
                          recipients=json.dumps([]), sent_at=1,
                          quoted_removed=38_609)

        row = store.browse_messages()[0]

        assert row["quoted_removed"] == 38_609


def test_a_message_indexed_before_v12_reads_as_unknown(tmp_path):
    import json

    with SqliteStore(tmp_path / "index.db") as store:
        file_id = store.upsert_file(r"D:\mail.pst\E2", size_bytes=10, mtime_ns=1,
                                    source_kind="pst_message")
        store.set_message(file_id, subject="old", sender="dave",
                          recipients=json.dumps([]), sent_at=1)

        assert store.browse_messages()[0]["quoted_removed"] is None
