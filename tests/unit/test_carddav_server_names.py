"""Adding a CardDAV server that sits behind a bot filter, typed the way people type it (dav.privateemail.com).

The owner (2026-10-12): adding dav.privateemail.com failed three ways. (1) Leasha introduced itself as
"Python-urllib/3.12", which Cloudflare answers with "error code: 1010" before CardDAV is reached - so discovery
saw a 403 and said "the server did not say where the address books are". (2) The server pasted as a URL was "not a
server name". (3) An address book given by its name ("Default Address Book/") was taken for an address.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from app.accounts.http import AllowedHosts, UrllibTransport, normalise_server


def test_a_server_can_be_typed_as_a_name_an_address_or_with_a_port_and_path():
    assert normalise_server("dav.privateemail.com") == ("dav.privateemail.com", None, "")
    assert normalise_server("https://dav.privateemail.com") == ("dav.privateemail.com", None, "")
    assert normalise_server("https://DAV.privateemail.com/") == ("dav.privateemail.com", None, "")
    assert normalise_server("dav.example.com:8443/dav.php/") == ("dav.example.com", 8443, "/dav.php/")
    assert normalise_server("") == ("", None, "")


def test_every_request_introduces_leasha_not_pythons_default():
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_PROPFIND(self):                                   # noqa: N802
            seen.append(self.headers.get("User-Agent"))
            self.send_response(207)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):                               # noqa: D401 - silence
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        transport = UrllibTransport(AllowedHosts(()))
        transport.request("PROPFIND", f"http://127.0.0.1:{server.server_port}/")
        transport.request("PROPFIND", f"http://127.0.0.1:{server.server_port}/", headers={"User-Agent": "Mine/1"})
    finally:
        server.shutdown()
    assert seen[0].startswith("Leasha/") and "Python-urllib" not in seen[0]
    assert seen[1] == "Mine/1"                                    # a caller that names itself is not overruled


def test_a_pasted_server_address_and_an_address_book_name_reach_the_provider_correctly(tmp_path):
    from tests.unit.accounts_ui_fakes import make_service

    store, svc = make_service(tmp_path)
    try:
        acct = svc.add_password_account("carddav", "me@x.com", "pw", "Mail", server="https://dav.privateemail.com/dav.php/",
                                        options={"addressbook_url": "Default Address Book/"}, test=False)
        assert acct.server == "dav.privateemail.com"
        assert acct.option("server_url") == "https://dav.privateemail.com/dav.php/"       # the path came along
        provider = svc.contact_provider(acct)
        assert provider._book == "" and provider._book_name == "Default Address Book"      # a name, to be matched
        again = svc.add_password_account("carddav", "me2@x.com", "pw", "Plain", server="dav.privateemail.com",
                                         test=False)
        assert again.server == "dav.privateemail.com" and again.option("server_url") in (None, "")
    finally:
        store.close()
