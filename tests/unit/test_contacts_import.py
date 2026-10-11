"""Order 1k 6i - importing contacts: vCard, Google/Outlook/Apple CSV, any CSV by mapping.

Layer: L7

Pure functions: text or a file in, an `ImportPreview` out, nothing written
anywhere. Duplicate detection against the book is the hub's job; the preview
only finds copies inside the file itself.
"""

from __future__ import annotations

import hashlib
import os
import stat

from app.contacts import importers as imp
from app.contacts.card import Card, Field, display_name

VCARD_MANY = (
    "BEGIN:VCARD\r\nVERSION:2.1\r\nN;CHARSET=UTF-8;ENCODING=QUOTED-PRINTABLE:M=C3=BCller;J=C3=BCrgen;;;\r\n"
    "FN:J\xfcrgen M\xfcller\r\nTEL;CELL;PREF:+49 170 1234567\r\nEMAIL;INTERNET:jm@example.de\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:3.0\r\nN:Lee;Ann;;;\r\nFN:Ann Lee\r\nORG:Acme;Sales\r\nTITLE:Buyer\r\n"
    "EMAIL;TYPE=WORK:ann@acme.example\r\nTEL;TYPE=WORK,VOICE:020 7946 0018\r\nEND:VCARD\r\n"
    "BEGIN:VCARD\r\nVERSION:4.0\r\nN:Khan;Sam;;;\r\nFN:Sam Khan\r\nEMAIL;TYPE=home:sam@example.org\r\n"
    "BDAY:19900102\r\nEND:VCARD\r\n"
)


def test_vcard_21_30_40_many_per_file():
    p = imp.import_vcard(VCARD_MANY)
    assert p.format == "vcard"
    assert [display_name(c.card) for c in p.contacts] == ["J\xfcrgen M\xfcller", "Ann Lee", "Sam Khan"]
    assert [c.source_row for c in p.contacts] == [1, 2, 3]
    assert p.contacts[0].card.values("phone")[0].label == "mobile"
    assert p.contacts[1].card.get("company") == "Acme"
    assert p.contacts[2].card.values("date")[0].value == "1990-01-02"


def test_vcard_bytes_with_bom_and_latin1_fallback():
    raw = ("\ufeff" + VCARD_MANY).encode("utf-8")
    assert len(imp.import_vcard(raw).contacts) == 3
    latin = "BEGIN:VCARD\r\nVERSION:2.1\r\nN:Garc\xeda;Jos\xe9\r\nEND:VCARD\r\n".encode("latin-1")
    assert imp.import_vcard(latin).contacts[0].card.get("given") == "Jos\xe9"


def test_vcard_without_a_structured_name_does_not_invent_one():
    # vcard.dumps writes FN from the email when there is no name; reading it back
    # must not turn "ann@x.example" into a given name.
    text = ("BEGIN:VCARD\r\nVERSION:3.0\r\nN:;;;;\r\nFN:ann@x.example\r\nEMAIL:ann@x.example\r\nEND:VCARD\r\n"
            "BEGIN:VCARD\r\nVERSION:3.0\r\nN:;;;;\r\nFN:No name\r\nTEL:+441632960001\r\nEND:VCARD\r\n")
    cards = [c.card for c in imp.import_vcard(text).contacts]
    assert all(not c.get("given") and not c.get("family") for c in cards)


def _csv_text(headers, *rows):
    """Rows given as {header: value}; every other cell empty, so columns cannot drift."""
    import csv
    import io
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(headers)
    for r in rows:
        w.writerow([r.get(h, "") for h in headers])
    return out.getvalue()


_G_HEADERS = [
    "First Name", "Middle Name", "Last Name", "Name Prefix", "Name Suffix", "Nickname", "Organization Name",
    "Organization Title", "Organization Department", "Birthday", "Notes", "Photo", "Labels",
    "E-mail 1 - Label", "E-mail 1 - Value", "E-mail 2 - Label", "E-mail 2 - Value",
    "Phone 1 - Label", "Phone 1 - Value", "Address 1 - Label", "Address 1 - Formatted", "Address 1 - Street",
    "Address 1 - City", "Address 1 - PO Box", "Address 1 - Region", "Address 1 - Postal Code",
    "Address 1 - Country", "Address 1 - Extended Address", "Website 1 - Label", "Website 1 - Value",
    "Event 1 - Label", "Event 1 - Value", "Relation 1 - Label", "Relation 1 - Value",
    "Custom Field 1 - Label", "Custom Field 1 - Value"]
GOOGLE_NEW = "\ufeff" + _csv_text(
    _G_HEADERS,
    {"First Name": "Ann", "Middle Name": "Marie", "Last Name": "Lee", "Name Prefix": "Dr", "Name Suffix": "PhD",
     "Nickname": "Annie", "Organization Name": "Acme", "Organization Title": "Buyer",
     "Organization Department": "Sales", "Birthday": "1980-05-17", "Notes": "Line 1\nLine 2",
     "Photo": "https://photo.example/x.jpg", "Labels": "* myContacts ::: Family ::: * starred",
     "E-mail 1 - Label": "Work", "E-mail 1 - Value": "ann@acme.example",
     "E-mail 2 - Label": "Home ::: Other", "E-mail 2 - Value": "ann@home.example ::: ann@other.example",
     "Phone 1 - Label": "Mobile ::: Work", "Phone 1 - Value": "+44 7700 900123 ::: 020 7946 0018",
     "Address 1 - Label": "Home", "Address 1 - Formatted": "1 High St, London", "Address 1 - Street": "1 High St",
     "Address 1 - City": "London", "Address 1 - Region": "Greater London", "Address 1 - Postal Code": "N1 1AA",
     "Address 1 - Country": "UK", "Address 1 - Extended Address": "Flat 2",
     "Website 1 - Label": "Work", "Website 1 - Value": "https://acme.example",
     "Event 1 - Label": "Anniversary", "Event 1 - Value": "2010-06-01",
     "Relation 1 - Label": "Spouse", "Relation 1 - Value": "Bob Lee",
     "Custom Field 1 - Label": "Pet", "Custom Field 1 - Value": "Rex"},
    {"Organization Name": "Just A Company"},
    {})


def test_google_new_layout_with_bom_and_multi_value_columns():
    p = imp.import_csv(GOOGLE_NEW)
    assert p.format == "google" and not p.needs_mapping
    assert len(p.contacts) == 2                      # the blank row is skipped, not imported
    assert p.skipped_rows == [4]
    c = p.contacts[0].card
    assert p.contacts[0].source_row == 2
    assert c.get("additional") == "Marie" and c.get("prefix") == "Dr" and c.get("department") == "Sales"
    assert c.get("notes") == "Line 1\nLine 2"
    assert [(f.value, f.label) for f in c.values("email")] == [
        ("ann@acme.example", "work"), ("ann@home.example", "home"), ("ann@other.example", "other")]
    assert [(f.value, f.label) for f in c.values("phone")] == [
        ("+44 7700 900123", "mobile"), ("020 7946 0018", "work")]
    a = c.values("address")[0]
    assert a.data == {"street": "1 High St\nFlat 2", "city": "London", "region": "Greater London",
                      "postcode": "N1 1AA", "country": "UK"}
    assert c.groups == ["Family"] and c.favourite            # "* myContacts" is Google's own, "* starred" is a star
    assert {(f.kind, f.label, f.value) for f in c.fields if f.kind in ("date", "related", "custom", "url")} == {
        ("date", "birthday", "1980-05-17"), ("date", "anniversary", "2010-06-01"),
        ("related", "spouse", "Bob Lee"), ("custom", "Pet", "Rex"), ("url", "work", "https://acme.example")}
    assert c.photo is None                                    # a link is never followed
    assert p.contacts[1].card.get("company") == "Just A Company"


def test_google_old_layout():
    text = ("Name,Given Name,Additional Name,Family Name,Name Prefix,Nickname,Notes,Group Membership,"
            "E-mail 1 - Type,E-mail 1 - Value,Phone 1 - Type,Phone 1 - Value,Organization 1 - Name,"
            "Organization 1 - Title\n"
            "Ann Lee,Ann,,Lee,,,,* My Contacts ::: Friends,* Work,ann@acme.example,* Mobile,07700 900123,Acme,Buyer\n")
    p = imp.import_csv(text)
    assert p.format == "google"
    c = p.contacts[0].card
    assert (c.get("given"), c.get("family"), c.get("company"), c.get("title")) == ("Ann", "Lee", "Acme", "Buyer")
    assert c.groups == ["Friends"]
    assert c.values("email")[0].label == "work" and c.values("phone")[0].label == "mobile"


OUTLOOK = (
    "Title;First Name;Middle Name;Last Name;Suffix;E-mail Address;E-mail 2 Address;Home Phone;Business Phone;"
    "Mobile Phone;Business Fax;Company;Job Title;Department;Business Street;Business City;Business State;"
    "Business Postal Code;Business Country/Region;Home Street;Birthday;Anniversary;Categories;Notes;Spouse\r\n"
    "Mr;Sam;J;Khan;Jr;sam@x.example;sam2@x.example;01632 960001;020 7946 0018;07700 900124;020 7946 0999;"
    "Khan Ltd;Director;Ops;5 Park Rd;Leeds;W Yorks;LS1 4AB;UK;1 High St;1990-01-02;2015-05-05;"
    "\"Family;Work friends\";\"Two\r\nlines\";Jo Khan\r\n"
)


def test_outlook_layout_semicolon_delimited():
    p = imp.import_csv(OUTLOOK)
    assert p.format == "outlook"
    c = p.contacts[0].card
    assert (c.get("prefix"), c.get("given"), c.get("additional"), c.get("family"), c.get("suffix")) == (
        "Mr", "Sam", "J", "Khan", "Jr")
    assert c.get("title") == "Director" and c.get("company") == "Khan Ltd"
    assert {(f.label, f.value) for f in c.values("phone")} == {
        ("home", "01632 960001"), ("work", "020 7946 0018"), ("mobile", "07700 900124"),
        ("work fax", "020 7946 0999")}
    assert [f.value for f in c.values("email")] == ["sam@x.example", "sam2@x.example"]
    work = [f for f in c.values("address") if f.label == "work"][0]
    assert work.data["city"] == "Leeds" and work.data["region"] == "W Yorks"
    assert [f.label for f in c.values("address")].count("home") == 1
    assert c.groups == ["Family", "Work friends"] and c.get("notes") == "Two\r\nlines".replace("\r\n", "\n")
    assert ("related", "spouse", "Jo Khan") in {(f.kind, f.label, f.value) for f in c.fields}


APPLE = ("First Name,Last Name,Middle Name,Prefix,Company,Job Title,Home Email,Work Email,Mobile Phone,"
         "Home Phone,Home Street,Home City,Home State,Home ZIP,Home Country,Note,Birthday\n"
         "Ann,Lee,,Ms,Acme,Buyer,ann@home.example,ann@acme.example,07700 900123,020 7946 0018,"
         "1 High St,London,,N1 1AA,UK,hello,1980-05-17\n")


def test_apple_layout_detected():
    p = imp.import_csv(APPLE)
    assert p.format == "apple"
    c = p.contacts[0].card
    assert {(f.label, f.value) for f in c.values("email")} == {("home", "ann@home.example"), ("work", "ann@acme.example")}
    assert {(f.label, f.value) for f in c.values("phone")} == {("mobile", "07700 900123"), ("home", "020 7946 0018")}
    assert c.values("address")[0].data["postcode"] == "N1 1AA" and c.get("notes") == "hello"


def test_unknown_csv_asks_for_a_mapping_then_imports_with_it():
    text = "Full name;Mail;Mobile no;Employer;Town;Memo;Weird\nAnn Lee;ann@x.example;07700 900123;Acme;Leeds;hi;zzz\n"
    p = imp.import_csv(text)
    assert p.needs_mapping and p.format == "unknown" and p.contacts == []
    assert p.headers[0] == "Full name"
    guess = imp.suggest_mapping(p.headers)
    assert guess["Full name"] == "name" and guess["Mail"] == "email"
    assert guess["Mobile no"].startswith("phone") and guess["Employer"] == "company"
    assert guess["Town"] == "address.city" and guess["Memo"] == "notes" and guess["Weird"] == ""
    mapping = dict(guess, Weird="custom:Weird")
    q = imp.import_csv(text, mapping)
    assert q.format == "mapped" and len(q.contacts) == 1
    c = q.contacts[0].card
    assert (c.get("given"), c.get("family"), c.get("company")) == ("Ann", "Lee", "Acme")
    assert c.values("email")[0].value == "ann@x.example"
    assert c.values("custom")[0].label == "Weird" and c.values("address")[0].data == {"city": "Leeds"}


def test_mapping_targets_and_cell_splitting():
    text = "who,where,tags,flag,addr\n\"Lee, Ann\",a@x.example ::: b@x.example,Family;Friends,yes,\"1 High St\"\n"
    m = {"who": "name", "where": "email:work", "tags": "group", "flag": "favourite", "addr": "address.street:home"}
    c = imp.import_csv(text, m).contacts[0].card
    assert (c.get("given"), c.get("family")) == ("Ann", "Lee")           # "Family, Given" order
    assert [(f.value, f.label) for f in c.values("email")] == [("a@x.example", "work"), ("b@x.example", "work")]
    assert c.groups == ["Family", "Friends"] and c.favourite
    assert c.values("address")[0].label == "home"


def test_warnings_for_rows_worth_a_second_look():
    text = "First Name,Last Name,Email,Phone\nAnn,Lee,not-an-email,12\n,,x@y.example,\n"
    p = imp.import_csv(text, imp.suggest_mapping(["First Name", "Last Name", "Email", "Phone"]))
    w0 = " ".join(p.contacts[0].warnings).lower()
    assert "email" in w0 and "phone" in w0
    assert len(p.contacts) == 2 and p.contacts[1].card.get("given") == ""


def test_import_file_is_read_only_and_leaves_the_source_alone(tmp_path):
    f = tmp_path / "contacts.csv"
    f.write_bytes(GOOGLE_NEW.encode("utf-8"))
    os.chmod(f, stat.S_IRUSR)                               # a write would raise
    before = (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns)
    p = imp.import_file(f)
    assert p.format == "google" and p.source_name == "contacts.csv"
    assert (hashlib.sha256(f.read_bytes()).hexdigest(), f.stat().st_mtime_ns) == before
    v = tmp_path / "a.vcf"
    v.write_text(VCARD_MANY, encoding="utf-8")
    assert len(imp.import_file(v).contacts) == 3
    assert sorted(os.listdir(tmp_path)) == ["a.vcf", "contacts.csv"]       # no temp or sidecar files


def test_import_xlsx_reads_the_plain_layout(tmp_path):
    from app.contacts import exporters as exp
    from tests.unit.contacts_samples import person
    cards = [person("Ann", "Lee", email="ann@x.example", phone="+441632960001")]
    path = tmp_path / "book.xlsx"
    exp.export_xlsx(cards, path)
    before = path.read_bytes()
    p = imp.import_file(path)
    assert p.contacts[0].card.get("family") == "Lee" and path.read_bytes() == before


def test_internal_duplicates_by_email_and_phone():
    a = Card(scalars={"given": "A"}, fields=[Field("email", "Ann@X.example")])
    b = Card(scalars={"given": "B"}, fields=[Field("email", " ann@x.example ")])
    c = Card(scalars={"given": "C"}, fields=[Field("phone", "07700 900123")])
    d = Card(scalars={"given": "D"}, fields=[Field("phone", "+44 7700 900123")])
    e = Card(scalars={"given": "E"}, fields=[Field("email", "e@x.example")])
    groups = imp.find_internal_duplicates([a, b, c, d, e])
    assert [g.indices for g in groups] == [[0, 1], [2, 3]]
    assert "email" in groups[0].reasons[0] and "phone" in groups[1].reasons[0]
    assert imp.find_internal_duplicates([a, e]) == []


def test_delimiter_and_encoding_helpers():
    assert imp.import_csv("a;b\n1;2\n").headers == ["a", "b"]
    assert imp.import_csv("a\tb\n1\t2\n").headers == ["a", "b"]
    utf16 = ("First Name,Last Name,Email\nAnn,Lee,a@x.example\n").encode("utf-16")
    assert imp.import_csv(utf16, imp.suggest_mapping(["First Name", "Last Name", "Email"])).contacts


def test_classic_outlook_headers_country_and_street_lines():
    headers = ["First Name", "Last Name", "E-mail Address", "Business Phone", "ISDN", "Home Street", "Home Street 2",
               "Home City", "Home Postal Code", "Home Country", "Birthday", "Anniversary", "Company"]
    text = _csv_text(headers, {"First Name": "Ann", "Last Name": "Lee", "E-mail Address": "a@x.example",
                               "Business Phone": "020 7946 0018", "ISDN": "020 7946 0100",
                               "Home Street": "1 High St", "Home Street 2": "Flat 2", "Home City": "London",
                               "Home Postal Code": "N1 1AA", "Home Country": "UK", "Birthday": "0/0/00",
                               "Anniversary": "2010-06-01", "Company": "Acme"})
    p = imp.import_csv(text)
    c = p.contacts[0].card
    assert p.format == "outlook"
    assert c.values("address")[0].data == {"street": "1 High St\nFlat 2", "city": "London", "postcode": "N1 1AA",
                                           "country": "UK"}
    assert {(f.label, f.value) for f in c.values("phone")} == {("work", "020 7946 0018"), ("isdn", "020 7946 0100")}
    assert [(f.label, f.value) for f in c.values("date")] == [("anniversary", "2010-06-01")]   # "0/0/00" is Outlook's "none"
