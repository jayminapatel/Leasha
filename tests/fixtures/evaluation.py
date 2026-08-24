r"""A small corpus with known answers, and twenty sentences to ask of it.

Layer: L4 (test fixture)

The work order asks for twenty real sentences against a real corpus. The owner
deferred that until there is enough indexed for the answer to mean anything -
which is right, and leaves a gap: **nothing at all was being measured in the
meantime**, so a change to chunking or ranking could make search worse with no
way to notice.

This is the stand-in. Forty-odd documents and emails with deliberately known
content, and twenty sentences whose correct answer is written down.

**What it can and cannot tell you, stated plainly, because a benchmark believed
beyond its evidence is worse than none.**

It *can* answer: does the mechanism work? Is a plain sentence enough to reach
the right document? Are "from Chris" and "before March" honoured or ignored? Did
today's change break something that worked yesterday?

It *cannot* answer: how well search works on the owner's actual archive. A real
corpus has near-duplicates, inconsistent naming, twelve years of drift and
thousands of documents competing for the same words. Every number from here will
be *optimistic*, and the twenty real sentences remain the measurement that
matters.

**The corpus is built to make constraints decisive.** Several documents cover
the same topic and differ only in sender, date or type - so "the licence email
from Chris" cannot be answered by topic alone. That is deliberate: the whole
question is whether constraints are honoured, and a corpus where topic alone
suffices would score well and prove nothing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.search.evaluate import Question

__all__ = ["Doc", "CORPUS", "QUESTIONS", "write_corpus"]


@dataclass(frozen=True, slots=True)
class Doc:
    """One document or message, with everything the index needs."""

    path: str
    text: str
    ext: str = "txt"
    size_bytes: int = 4096
    #: Days before "today". Used to build mtime, and to make date filters real.
    age_days: int = 400
    #: Set for mail. A document has none of these.
    sender: Optional[str] = None
    recipients: tuple[str, ...] = ()
    subject: Optional[str] = None
    has_attachment: bool = False

    @property
    def is_mail(self) -> bool:
        return self.sender is not None


#: The corpus. Names and contents are invented; the *shape* is not - overlapping
#: topics, several senders discussing the same thing, and the same subject
#: appearing as both an email and a document.
CORPUS: tuple[Doc, ...] = (
    # --- the licence thread: same topic, three senders, three dates ---------
    Doc(path=r"Mail\licence-chris-quote.eml", ext="eml", age_days=120,
        sender="chris.yates@acme.com", recipients=("me@acme.com",),
        subject="Licence renewal quote", has_attachment=True,
        text="I have the quote back for buying the annual licence renewal. "
             "Twelve thousand for the site, which is up four percent. "
             "Purchase order needs raising before the end of the month."),
    Doc(path=r"Mail\licence-dave-question.eml", ext="eml", age_days=200,
        sender="dave.smith@acme.com", recipients=("chris.yates@acme.com",),
        subject="Question about the licence", has_attachment=False,
        text="Do we know whether the licence covers the second production line? "
             "Buying another one would be expensive and I would rather not."),
    Doc(path=r"Mail\licence-priya-approval.eml", ext="eml", age_days=60,
        sender="priya.n@acme.com", recipients=("me@acme.com", "chris.yates@acme.com"),
        subject="Approved: licence purchase", has_attachment=False,
        text="Approved. Go ahead and buy the licence renewal at the quoted price."),
    Doc(path=r"Mail\sent-priya-po-request.eml", ext="eml", age_days=55,
        sender="me@acme.com", recipients=("priya.n@acme.com",),
        subject="PO for the licence renewal", has_attachment=False,
        text="Priya, can you raise the purchase order for the licence renewal "
             "at the price Chris quoted. Needed before month end."),
    Doc(path=r"Docs\Licences\licence-terms-2025.pdf", ext="pdf", age_days=300,
        size_bytes=2_400_000,
        text="Software licence terms and conditions. Annual renewal, per site, "
             "covering production and test environments. Buying additional seats "
             "requires written notice."),

    # --- Leeds safety: a report, an email about it, and a draft -------------
    Doc(path=r"Docs\Leeds\safety-report-final.pdf", ext="pdf", age_days=90,
        size_bytes=5_100_000,
        text="Leeds site safety report. Findings from the annual inspection of "
             "the pump station and the valve hall. Two observations raised "
             "against guarding, one against permit to work."),
    Doc(path=r"Docs\Leeds\safety-report-draft.docx", ext="docx", age_days=110,
        text="DRAFT Leeds site safety report. Findings from the inspection of the "
             "pump station. Not for circulation."),
    Doc(path=r"Mail\leeds-safety-dave.eml", ext="eml", age_days=88,
        sender="dave.smith@acme.com", recipients=("me@acme.com",),
        subject="Leeds safety report", has_attachment=True,
        text="Attaching the final Leeds safety report from the inspection. "
             "The guarding observations need closing out before the audit."),

    # --- the audit ----------------------------------------------------------
    Doc(path=r"Docs\Audit\audit-findings-march.xlsx", ext="xlsx", age_days=150,
        text="Audit findings register. Nonconformities raised during the March "
             "surveillance audit, with owners and closure dates."),
    Doc(path=r"Mail\audit-schedule.eml", ext="eml", age_days=170,
        sender="auditor@certbody.com", recipients=("me@acme.com",),
        subject="Surveillance audit dates", has_attachment=False,
        text="Confirming the surveillance audit will take place in March. "
             "Please have the findings register available."),

    # --- the pump station: engineering, several formats ---------------------
    Doc(path=r"Docs\Engineering\pump-station-drawings.pdf", ext="pdf",
        age_days=500, size_bytes=8_800_000,
        text="Pump station general arrangement drawings. Duty and standby pumps, "
             "suction manifold, discharge valve arrangement."),
    Doc(path=r"Docs\Engineering\pump-commissioning.docx", ext="docx", age_days=430,
        text="Pump station commissioning record. Flow rates measured against the "
             "duty point, vibration readings, and the valve line-up check."),
    Doc(path=r"Docs\Engineering\valve-schedule.xlsx", ext="xlsx", age_days=420,
        text="Valve schedule for the site. Tag numbers, sizes, materials and "
             "the manifold each valve serves."),

    # --- invoices: one big, one small, similar names ------------------------
    Doc(path=r"Docs\Finance\invoice-2024-annual.pdf", ext="pdf", age_days=250,
        size_bytes=3_200_000,
        text="Annual invoice for services rendered. Licence, support and the "
             "site visits carried out during the year."),
    Doc(path=r"Docs\Finance\invoice-template.docx", ext="docx", age_days=700,
        size_bytes=48_000,
        text="Invoice template. Replace the placeholders before sending."),

    # --- onboarding and training --------------------------------------------
    Doc(path=r"Docs\HR\induction-slides.pptx", ext="pptx", age_days=180,
        text="Safety induction for new starters. Site rules, permit to work, "
             "emergency arrangements and the assembly point."),
    Doc(path=r"Docs\HR\training-matrix.xlsx", ext="xlsx", age_days=95,
        text="Training matrix. Who has completed the safety induction and when "
             "each refresher falls due."),

    # --- distractors: same words, wrong document ----------------------------
    Doc(path=r"Docs\Misc\meeting-notes-january.docx", ext="docx", age_days=320,
        text="Monthly meeting notes. Discussed the licence, the Leeds site, the "
             "pump station and the audit. Actions carried forward."),
    Doc(path=r"Docs\Misc\holiday-rota.xlsx", ext="xlsx", age_days=40,
        text="Holiday rota for the team. Cover arrangements over the summer."),
    Doc(path=r"Mail\holiday-chris.eml", ext="eml", age_days=45,
        sender="chris.yates@acme.com", recipients=("me@acme.com",),
        subject="Holiday next week", has_attachment=False,
        text="I am away next week. Chase Dave about the licence if it comes up."),
    Doc(path=r"Docs\Misc\readme.txt", ext="txt", age_days=900,
        text="Folder contents. Assorted notes, superseded documents and scans."),
)


#: Twenty sentences of the kind somebody actually types, each with the document
#: that should come back and the constraint it carries.
#:
#: **Eight are pure topic questions and twelve carry a constraint.** That
#: proportion is deliberate: the constrained ones are the hypothesis under test,
#: and a set weighted towards easy topic questions would produce a comfortable
#: number that answered nothing.
QUESTIONS: tuple[Question, ...] = (
    # -- topic only: can plain English reach the right document at all? ------
    Question("the safety report for the Leeds site",
             "safety-report-final", "",
             "The clearest case. If this misses, nothing else matters."),
    Question("drawings of the pump station",
             "pump-station-drawings", "", "Topic plus a document kind in words."),
    Question("what the licence terms actually say",
             "licence-terms-2025", "", "Terms rather than the emails about them."),
    Question("the valve schedule with tag numbers",
             "valve-schedule", "", "Distinctive vocabulary."),
    Question("safety induction for new starters",
             "induction-slides", "", "Should beat the training matrix."),
    Question("who has done their training and when it expires",
             "training-matrix", "", "Paraphrase - no shared words with the title."),
    Question("commissioning results for the pumps",
             "pump-commissioning", "", "Competes with drawings on 'pump'."),
    Question("the findings raised at the audit",
             "audit-findings-march", "", "Competes with the audit email."),

    # -- sender --------------------------------------------------------------
    Question("the email from Chris about buying a licence",
             "licence-chris-quote", "sender",
             "The owner's own example. Three people discuss licences; only "
             "Chris sent the quote."),
    Question("what Dave asked about the licence",
             "licence-dave-question", "sender",
             "Same topic, different sender - topic alone cannot separate these."),
    Question("Priya's approval to go ahead",
             "licence-priya-approval", "sender", "Sender plus a weak topic."),
    Question("the safety report Dave sent me",
             "leeds-safety-dave", "sender",
             "Must return the email, not the report it attaches."),

    # -- date ----------------------------------------------------------------
    Question("the licence quote from the last few months",
             "licence-chris-quote", "date",
             "120 days old; the others are 200 and 60."),
    Question("the audit dates we were sent about six months ago",
             "audit-schedule", "date",
             "170 days old. The question originally said 'over a year ago' "
             "about a document five months old - it contradicted its own "
             "answer, and no search could have satisfied it."),
    Question("the holiday rota from the last couple of months",
             "holiday-rota", "date", "Recent, and competes with a holiday email."),

    # -- file type -----------------------------------------------------------
    Question("the spreadsheet with the audit findings",
             "audit-findings-march", "type",
             "'Spreadsheet' should prefer the xlsx over the email."),
    Question("the draft version of the Leeds safety report",
             "safety-report-draft", "type",
             "Draft versus final - one word decides it."),
    Question("the slide deck about site rules",
             "induction-slides", "type", "'Slide deck' names a format."),

    # -- recipient and attachment --------------------------------------------
    Question("what did I send to Priya",
             "sent-priya-po-request", "recipient",
             "The only message with Priya as a RECIPIENT. This question "
             "originally pointed at a message she SENT, so it had no correct "
             "answer at all and scored zero for a reason that had nothing to "
             "do with search - a benchmark bug, found by running it."),
    Question("emails with something attached about the licence",
             "licence-chris-quote", "attachment",
             "Only Chris's quote and Dave's safety email have attachments."),
)


def write_corpus(root: Path, docs: tuple[Doc, ...] = CORPUS) -> Path:
    """Write the corpus to disk as real files, for an end-to-end index run."""
    root = Path(root)
    for doc in docs:
        target = root / doc.path.replace("\\", "/")
        target.parent.mkdir(parents=True, exist_ok=True)
        if doc.is_mail:
            headers = [
                f"From: {doc.sender}",
                f"To: {', '.join(doc.recipients)}",
                f"Subject: {doc.subject}",
            ]
            target.write_text("\n".join(headers) + "\n\n" + doc.text, encoding="utf-8")
        else:
            target.write_text(doc.text, encoding="utf-8")
    return root


def load_into(store: object, docs: tuple[Doc, ...] = CORPUS, *, now_ns: int = 0) -> None:
    """Put the corpus straight into a store, skipping extraction.

    For measuring *retrieval* rather than extraction: it keeps the harness fast
    and means a change to the parsers cannot quietly change what is being
    measured about ranking.
    """
    import time

    now_ns = now_ns or time.time_ns()
    day_ns = 86_400 * 1_000_000_000

    for doc in docs:
        file_id = store.upsert_file(                              # type: ignore[attr-defined]
            path=doc.path,
            parent_dir=str(Path(doc.path).parent),
            ext=doc.ext,
            size_bytes=doc.size_bytes,
            mtime_ns=now_ns - doc.age_days * day_ns,
            content_hash=doc.path,
            status="INDEXED",
            source_kind="eml" if doc.is_mail else "file",
        )
        body = doc.text
        if doc.is_mail:
            body = (f"Subject: {doc.subject}\nFrom: {doc.sender}\n"
                    f"To: {', '.join(doc.recipients)}\n\n{doc.text}")
        store.replace_chunks(file_id, [{                          # type: ignore[attr-defined]
            "text": body, "ordinal": 0, "char_start": 0,
            "char_end": len(body), "page": None,
        }])
        if doc.is_mail:
            store.set_message(                                    # type: ignore[attr-defined]
                file_id,
                subject=doc.subject, sender=doc.sender,
                recipients=json.dumps(list(doc.recipients)),
                has_attach=1 if doc.has_attachment else 0,
            )
