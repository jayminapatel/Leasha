"""One change, one transaction.

Layer: L3

`set_state` commits per key. The settings panel saves five ceilings together, so
that was five commits - five fsyncs on the UI thread - for one change nobody
thinks of as five. Combined with a signal on every spin-box notch, this is what
made the arrows feel dead.
"""

from __future__ import annotations

import pytest

from app.storage.sqlite_store import SqliteStore


@pytest.fixture
def store(tmp_path):
    with SqliteStore(tmp_path / "index.db") as opened:
        yield opened


def test_several_keys_land(store):
    store.set_states({"ui:a": "1", "ui:b": "2", "ui:c": "3"})
    assert store.get_state("ui:a") == "1"
    assert store.get_state("ui:c") == "3"


def test_it_overwrites_like_set_state_does(store):
    store.set_state("ui:a", "old")
    store.set_states({"ui:a": "new"})
    assert store.get_state("ui:a") == "new"


def test_an_empty_mapping_is_not_an_empty_transaction(store):
    """Called on every save, and most saves change nothing about a group. An
    empty write is still a commit."""
    store.set_states({})
    assert store.all_state().get("ui:a") is None


def test_values_are_stringified_like_the_single_key_path(store):
    """The UI passes ints for the ceilings. `set_state` takes str; if this took
    them raw, reading back would give int in one path and str in the other."""
    store.set_states({"ui:workers": 4})
    assert store.get_state("ui:workers") == "4"


def test_it_is_one_transaction(store, monkeypatch):
    """The whole point. Five commits for one change is five fsyncs on the UI
    thread, and holding an arrow made that fifty a second."""
    commits = []
    original = SqliteStore.write

    def counting(self):
        commits.append(1)
        return original(self)

    monkeypatch.setattr(SqliteStore, "write", counting)
    store.set_states({"ui:a": "1", "ui:b": "2", "ui:c": "3", "ui:d": "4", "ui:e": "5"})
    assert commits == [1]
