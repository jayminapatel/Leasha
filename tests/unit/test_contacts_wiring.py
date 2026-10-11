"""Order 1k: the Contacts page is wired to the account service, enrichment and the photo picker.

Layer: L5
"""

from __future__ import annotations

import pytest

from app.contacts import people
from app.contacts.hub import ContactHub
from app.storage.sqlite_store import SqliteStore
from app.ui import account_runtime
from app.ui.contacts import openers, person_photo
from tests.unit.accounts_fakes import person


@pytest.fixture
def hub(tmp_path):
    store = SqliteStore(tmp_path / "k.db").connect()
    yield ContactHub(store)
    store.close()


class Shell:
    def __init__(self, store, runtime=None):
        self._store = store
        self.account_runtime = runtime


def test_looking_for_a_hook_never_builds_the_service(hub):
    """Rule 5 and start-up: the page asks `getattr(service, 'pick_photo_from_person')` on the window's thread."""
    shell = Shell(hub.store)
    service = account_runtime.LazyService(shell)
    assert getattr(service, "pick_photo_from_person", None) is None
    assert callable(service.push_accounts) and callable(service.request_sync)
    assert getattr(hub.store, "_account_service", None) is None


def test_sync_now_door_asks_the_scheduler_and_nothing_else(hub):
    asked = []

    class Scheduler:
        def sync_all_now(self):
            asked.append(1)
            return True

    class Runtime:
        scheduler = Scheduler()

    service = account_runtime.LazyService(Shell(hub.store, Runtime()))
    openers.ensure_tools_loaded()
    assert openers.is_registered("sync")
    openers.OPENERS["sync"](None, openers.Ctx(hub=hub, accounts=None, service=service))
    assert asked == [1]


def test_the_service_is_built_with_mail_attached_and_push_accounts_exists(hub):
    from app.accounts.service import AccountService
    svc = account_runtime.service_for_store(hub.store)
    assert isinstance(svc, AccountService) and svc.mail_sync is not None
    assert svc.remove_mail_hook is not None
    assert svc.push_accounts([]) == []


def test_the_photo_picker_says_why_when_the_contact_is_not_linked(hub):
    cid = hub.create(person("Ann", "Lee"))
    message, faces = person_photo.candidate_faces(hub, cid)
    assert faces == [] and "not linked" in message


def test_the_photo_picker_lists_faces_of_the_linked_person(hub, tmp_path):
    from PIL import Image
    path = tmp_path / "party.jpg"
    Image.new("RGB", (400, 300), (200, 120, 60)).save(path)
    store = hub.store
    pile = store.create_pile("Ann Lee")
    fid = store.upsert_file(str(path), parent_dir=str(tmp_path), ext="jpg", size_bytes=1, mtime_ns=1,
                            status="INDEXED", source_kind="file")
    for box in [(10, 10, 20, 20), (100, 60, 120, 120)]:
        store.assign_face(store.add_face(fid, box, b"\0" * 8), pile)
    cid = hub.create(person("Ann", "Lee"))
    people.link_to_pile(hub, cid, pile)
    name, faces = person_photo.candidate_faces(hub, cid)
    assert name == "Ann Lee" and len(faces) == 2
    assert all(p.mime == "image/jpeg" and p.data for _label, p in faces)
