r"""A small deterministic archive, and eighty questions to put to Chat about it.

Layer: L8b (test fixture) - work order `202626270611-chat-tab`, section 4a.

`evaluate --chat` (`app/chat/evaluate.py`) runs every `QA` below through the real
`ChatEngine` over this corpus and scores what comes back. The corpus is small on
purpose: forty-odd documents and emails with **fixed dates and fixed contents**, so
a question's right answer is a fact about this file, not about today's date or the
owner's archive.

**What it can and cannot tell you**, stated plainly because a benchmark believed
beyond its evidence is worse than none. It measures the *mechanism* - does the
router pick the right machine, does the loop find the passage, does verification let
only supported sentences through, are the counts exact, does "nothing found" say what
was searched. It cannot measure how a model behaves on a real archive with twelve
years of near-duplicates. Every number from here is optimistic.

**The questions are in five families, and each is scored differently** (see
`QA.outcome`):

  * LOOKUP / FOLLOWUP - a fact inside a document. Scored: the fact is in the answer
    and a receipt names the right document. A few are **traps**: the corpus holds a
    tempting wrong answer (an expired tenancy with a different deposit, a draft
    report with a different count), and the answer must not contain it.
  * AGGREGATE - a count or list. `count` is **computed here, in plain Python over the
    `Doc` list**, independently of any SQL, so the harness compares two derivations.
  * FIND - the answer is a result set; `find` names paths that must be in it.
  * ABSENCE - "do I have...". Planted absences (nothing in the corpus matches) must
    answer with the protocol; the rest ("do I have the MOT certificate?") find it.
  * SYNTHESIS - across documents; scored loosely, no floor at v1.

"Today" for the whole set is `TODAY`, so "last year" means the same thing forever.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time as clock
from typing import Callable, Optional

__all__ = ["Doc", "CORPUS", "QA", "QUESTIONS", "TODAY", "load_into", "expected_count", "human_size"]

#: The date "today" is for every question.
TODAY = date(2026, 9, 19)


@dataclass(frozen=True)
class Doc:
    path: str
    text: str
    ext: str
    when: str                        # YYYY-MM-DD
    size: int = 65_536
    sender: Optional[str] = None     # set for mail
    recipients: tuple[str, ...] = ()
    subject: Optional[str] = None
    attach: bool = False

    @property
    def is_mail(self) -> bool:
        return self.sender is not None

    @property
    def year(self) -> int:
        return int(self.when[:4])

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]


def _mail(name: str, when: str, sender: str, to: tuple[str, ...], subject: str, text: str,
          attach: bool = False) -> Doc:
    return Doc(f"C:/Archive/Mail/{name}", text, "eml", when, 12_288, sender, to, subject, attach)


def _doc(folder: str, name: str, when: str, text: str, size: int = 65_536) -> Doc:
    ext = name.rsplit(".", 1)[-1]
    return Doc(f"C:/Archive/{folder}/{name}", text, ext, when, size)


ME = "me@acme.com"

CORPUS: tuple[Doc, ...] = (
    # --- the tenancy: a current agreement, an expired one, the landlord's letters ------
    _doc("Tenancy", "landlord-deposit-letter.docx", "2024-02-10",
         "Dear tenant, this letter confirms the agreement about your tenancy deposit. "
         "The deposit is 950 pounds and it is held in a government approved scheme. "
         "The deposit will be returned within 10 days of the tenancy ending, less any "
         "agreed deductions. Signed, Margaret Okafor, Landlord.", 48_000),
    _mail("landlord-deposit-reply.eml", "2024-02-14", "margaret.okafor@homelets.co.uk", (ME,),
          "Re: Deposit agreement",
          "Thank you for signing the agreement. We agreed that the deposit stays at 950 pounds "
          "and that the boiler service is the landlord's responsibility. The inventory check "
          "is booked for the 3rd of March."),
    _doc("Tenancy", "tenancy-agreement-2024.pdf", "2024-01-20",
         "Assured shorthold tenancy agreement. The term is twelve months starting on 1 March "
         "2024. Rent is 1,200 pounds per month, payable on the first of each month. The tenant "
         "must give two months notice. Pets are not allowed without written consent.", 210_000),
    _doc("Tenancy", "tenancy-agreement-2023-expired.pdf", "2023-01-18",
         "Assured shorthold tenancy agreement for the previous flat. The term was six months. "
         "Rent was 1,050 pounds per month. The deposit was 800 pounds.", 198_000),

    # --- the licence thread: one topic, four senders, four dates ------------------------
    _mail("licence-chris-quote.eml", "2025-06-02", "chris.yates@acme.com", (ME,),
          "Licence renewal quote",
          "I have the quote back for the annual licence renewal. Twelve thousand pounds for the "
          "site, which is up four percent. A purchase order needs raising before the end of "
          "the month.", attach=True),
    _mail("licence-dave-question.eml", "2025-03-05", "dave.smith@acme.com",
          ("chris.yates@acme.com",), "Question about the licence",
          "Do we know whether the licence covers the second production line? Buying another one "
          "would be expensive."),
    _mail("licence-priya-approval.eml", "2025-07-10", "priya.n@acme.com",
          (ME, "chris.yates@acme.com"), "Approved: licence purchase",
          "Approved. Go ahead and buy the licence renewal at the quoted price."),
    _mail("sent-priya-po-request.eml", "2025-07-12", ME, ("priya.n@acme.com",),
          "PO for the licence renewal",
          "Priya, can you raise the purchase order for the licence renewal at the price Chris "
          "quoted. It is needed before month end."),
    _doc("Licences", "licence-terms-2025.pdf", "2025-01-15",
         "Software licence terms and conditions. Annual renewal per site, covering production "
         "and test environments. Buying additional seats requires written notice of thirty "
         "days.", 2_400_000),

    # --- Leeds safety: a final report, a draft with a different count, a covering email -
    _doc("Leeds", "safety-report-final.pdf", "2025-05-20",
         "Leeds site safety report. Findings from the annual inspection of the pump station and "
         "the valve hall. Two observations were raised against guarding and one against permit "
         "to work. The inspection was carried out on 14 April 2025 by Helen Marsh.", 5_100_000),
    _doc("Leeds", "safety-report-draft.docx", "2025-04-25",
         "DRAFT Leeds site safety report. Findings from the inspection of the pump station. "
         "Three observations raised against guarding. Not for circulation.", 61_000),
    _mail("leeds-safety-dave.eml", "2025-05-22", "dave.smith@acme.com", (ME,),
          "Leeds safety report",
          "Attaching the final Leeds safety report from the inspection. The guarding "
          "observations need closing out before the audit on 9 June.", attach=True),

    # --- the audit ----------------------------------------------------------------
    _doc("Audit", "audit-findings-march.xlsx", "2025-03-28",
         "Audit findings register. Nonconformities raised during the March surveillance audit, "
         "with owners and closure dates. Seven nonconformities were raised and four are "
         "closed.", 88_000),
    _mail("audit-schedule.eml", "2025-02-11", "auditor@certbody.com", (ME,),
          "Surveillance audit dates",
          "Confirming the surveillance audit will take place in March. Please have the findings "
          "register available."),

    # --- engineering --------------------------------------------------------------
    _doc("Engineering", "pump-station-drawings.pdf", "2024-05-01",
         "Pump station general arrangement drawings. Duty and standby pumps, suction manifold, "
         "discharge valve arrangement. Drawing number GA-4471, revision C.", 8_800_000),
    _doc("Engineering", "pump-commissioning.docx", "2024-06-15",
         "Pump station commissioning record. Flow rate measured at 42 litres per second against "
         "the duty point of 40. Vibration readings were within limits. The valve line-up check "
         "was completed by Tomasz Kowalski.", 72_000),
    _doc("Engineering", "valve-schedule.xlsx", "2024-06-20",
         "Valve schedule for the site. Tag numbers, sizes, materials and the manifold each valve "
         "serves. There are 18 valves on the discharge manifold.", 54_000),

    # --- finance ------------------------------------------------------------------
    _doc("Finance", "invoice-2024-annual.pdf", "2024-12-01",
         "Annual invoice for services rendered. Licence, support and site visits carried out "
         "during the year. Total amount due 8,450 pounds, payable within 30 days.", 3_200_000),
    _doc("Finance", "invoice-template.docx", "2023-02-10",
         "Invoice template. Replace the placeholders before sending.", 48_000),

    # --- Dave's mail across the years, for counting -----------------------------------
    _mail("dave-2019-05-budget.eml", "2019-05-07", "dave.smith@acme.com", (ME,), "Budget figures",
          "The budget figures for the next quarter are attached. Please review the spreadsheet.",
          attach=True),
    _mail("dave-2019-08-rota.eml", "2019-08-19", "dave.smith@acme.com", (ME,), "August shutdown",
          "The rota for the August shutdown is agreed. Cover is arranged for weeks two and three."),
    _mail("dave-2019-11-training.eml", "2019-11-04", "dave.smith@acme.com", (ME,), "Training",
          "Training for the new starters is booked for the second week of December."),
    _mail("dave-2020-02-audit.eml", "2020-02-17", "dave.smith@acme.com", (ME,), "Audit preparation",
          "Audit preparation is under way. The findings register from last year is attached.",
          attach=True),

    # --- people and site ------------------------------------------------------------
    _doc("HR", "induction-slides.pptx", "2024-09-10",
         "Safety induction for new starters. Site rules, permit to work, emergency arrangements "
         "and the assembly point is by the north gate.", 1_900_000),
    _doc("HR", "training-matrix.xlsx", "2025-02-25",
         "Training matrix. Who has completed the safety induction and when each refresher falls "
         "due. Refresher training is due every 12 months.", 41_000),

    # --- distractors: same words, wrong document ---------------------------------------
    _doc("Misc", "meeting-notes-january.docx", "2025-01-31",
         "Monthly meeting notes. Discussed the licence, the Leeds site, the pump station and the "
         "audit. Actions carried forward.", 39_000),
    _doc("Misc", "holiday-rota.xlsx", "2025-08-05",
         "Holiday rota for the team. Cover arrangements over the summer.", 30_000),
    _mail("holiday-chris.eml", "2025-08-10", "chris.yates@acme.com", (ME,), "Holiday next week",
          "I am away next week. Chase Dave about the licence if it comes up."),
    _doc("Misc", "readme.txt", "2022-03-01",
         "Folder contents. Assorted notes, superseded documents and scans.", 2_048),

    # --- home life ------------------------------------------------------------------
    _doc("Recipes", "lemon-cake.txt", "2023-04-02",
         "Lemon cake. Cream 200 grams of butter with 200 grams of sugar. Add four eggs and 250 "
         "grams of flour. Bake for 40 minutes at 180 degrees.", 1_024),
    _doc("Recipes", "dal-tadka.txt", "2023-11-12",
         "Dal tadka. Boil one cup of toor dal until soft. Temper with cumin, garlic and dried "
         "red chillies. Serve with rice.", 1_024),
    _doc("Travel", "lisbon-itinerary.docx", "2024-08-01",
         "Lisbon trip itinerary. The flight lands on 12 September at 14:35. The hotel is on Rua "
         "Augusta. Dinner on the first night is booked at 20:00.", 45_000),
    _doc("Health", "dentist-appointment.txt", "2025-09-01",
         "Dentist appointment confirmation. Thursday 18 September at 09:15 with Dr Anita Rao. "
         "Bring the insurance card.", 2_048),
    _doc("Car", "mot-certificate.pdf", "2025-03-14",
         "MOT test certificate. The vehicle passed the test on 14 March 2025. The next test is "
         "due by 13 March 2026. Mileage recorded 48,210.", 320_000),
    _doc("Car", "insurance-renewal.pdf", "2025-10-02",
         "Car insurance renewal. Annual premium 612 pounds. Excess is 250 pounds. The policy "
         "renews on 1 November 2025.", 270_000),

    # --- photographs (their captions are what the index holds) -------------------------
    *(_doc("Photos/Diwali2019", f"IMG_200{i}.jpg", "2019-10-27",
           "Photo. Diwali celebration at home with diyas and rangoli, October 2019.", 3_400_000)
      for i in range(1, 6)),
    *(_doc("Photos/Beach2015", f"kids-beach-0{i}.jpg", "2015-08-14",
           "Photo. The kids playing on the beach, August 2015.", 2_900_000)
      for i in range(1, 4)),
    _doc("Photos/Holiday2024", "lisbon-tram.jpg", "2024-09-13",
         "Photo. A yellow tram climbing a hill in Lisbon, September 2024.", 4_100_000),
    _doc("Photos/Holiday2024", "lisbon-castle.jpg", "2024-09-13",
         "Photo. The castle above Lisbon at sunset, September 2024.", 3_900_000),
    _doc("Photos/Diwali2020", "IMG_3001.jpg", "2020-11-14",
         "Photo. Diwali lights at the front door, November 2020.", 3_000_000),
    _doc("Photos/Diwali2020", "IMG_3002.jpg", "2020-11-14",
         "Photo. Diwali sweets on a plate, November 2020.", 3_100_000),
)


# --------------------------------------------------------------------------- ground truth

def _matches(doc: Doc, *, ext=(), mail=None, sender=None, year=None, attach=None,
             words=(), documents=False) -> bool:
    """Whether `doc` satisfies a counting question, in plain Python.

    Written independently of `app.storage.filters` on purpose: the harness
    compares this against the SQL, so the two must not share code.
    """
    if ext and doc.ext not in ext:
        return False
    if mail is True and not doc.is_mail:
        return False
    if documents and doc.is_mail:
        return False
    if sender and (doc.sender or "").split("@")[0].split(".")[0] != sender:
        return False
    if year and doc.year != year:
        return False
    if attach is not None and doc.attach != attach:
        return False
    for word in words:
        if word.lower() not in (doc.text + " " + doc.path).lower():
            return False
    return True


def expected_count(**criteria) -> int:
    return sum(1 for doc in CORPUS if _matches(doc, **criteria))


_PHOTO = ("jpg", "jpeg", "png", "heic")
_SHEET = ("xlsx", "xls", "csv", "ods")


def human_size(n: int) -> str:
    """Bytes as the answer states them (the fixture's own formatter)."""
    size = float(n)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1024:
            return f"{int(size)} bytes" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


# --------------------------------------------------------------------------- the questions

@dataclass(frozen=True)
class QA:
    id: str
    question: str
    #: The class the router should choose.
    cls: str
    #: What kind of turn should come back: answer | aggregate | find | absence.
    outcome: str
    #: Every one of these must appear (case-insensitively) in the answer.
    expect_all: tuple[str, ...] = ()
    #: At least one of these must appear (loose scoring for synthesis).
    expect_any: tuple[str, ...] = ()
    #: A fragment of the path of a document a receipt must name.
    cite: str = ""
    #: Aggregate ground truth: the count the answer must state.
    count: Optional[int] = None
    #: FIND: path fragments that must be among the results.
    find: tuple[str, ...] = ()
    #: Earlier questions asked first, in order (for follow-ups).
    history: tuple[str, ...] = ()
    #: The tempting wrong answer: must NOT appear.
    forbid: tuple[str, ...] = ()
    #: A question whose tempting answer is wrong.
    trap: bool = False
    #: A planted absence: nothing in the corpus matches.
    planted: bool = False
    note: str = ""


def _a(id, q, cls, expect, cite, **kw):
    return QA(id, q, cls, "answer", expect_all=tuple(expect), cite=cite, **kw)


def _agg(id, q, count, *, expect=(), **kw):
    return QA(id, q, "AGGREGATE", "aggregate", count=count, expect_all=tuple(expect), **kw)


QUESTIONS: tuple[QA, ...] = (
    # ---- LOOKUP: a fact inside a document ------------------------------------------------
    _a("L01", "What did we agree with the landlord about the deposit?", "LOOKUP", ("950",), "landlord-deposit"),
    _a("L02", "How long does the landlord have to return the deposit?", "LOOKUP", ("10 days",), "landlord-deposit-letter"),
    _a("L03", "Who is my landlord?", "LOOKUP", ("Margaret Okafor",), "landlord-deposit-letter"),
    _a("L04", "How much is the rent per month on the 2024 agreement?", "LOOKUP", ("1,200",), "tenancy-agreement-2024",
       forbid=("1,050",), trap=True, note="the expired agreement says 1,050"),
    _a("L05", "When does the tenancy start?", "LOOKUP", ("1 March 2024",), "tenancy-agreement-2024"),
    _a("L06", "How much notice must the tenant give?", "LOOKUP", ("two months",), "tenancy-agreement-2024"),
    _a("L07", "Who is responsible for the boiler service?", "LOOKUP", ("landlord",), "landlord-deposit-reply"),
    _a("L08", "How much was Chris's quote for the licence renewal?", "LOOKUP", ("Twelve thousand",), "licence-chris-quote"),
    _a("L09", "Did Priya approve the licence purchase?", "LOOKUP", ("Go ahead",), "licence-priya-approval"),
    _a("L10", "How much written notice is needed to buy additional licence seats?", "LOOKUP", ("thirty days",), "licence-terms-2025"),
    _a("L11", "How many observations were raised against guarding in the final Leeds safety report?", "LOOKUP",
       ("Two observations",), "safety-report-final", forbid=("Three observations",), trap=True,
       note="the draft says three"),
    _a("L12", "Who carried out the Leeds safety inspection?", "LOOKUP", ("Helen Marsh",), "safety-report-final"),
    _a("L13", "When is the audit that the guarding observations must be closed before?", "LOOKUP", ("9 June",), "leeds-safety-dave"),
    _a("L14", "How many nonconformities were raised in the March audit?", "LOOKUP", ("Seven",), "audit-findings-march"),
    _a("L15", "What is the drawing number of the pump station arrangement?", "LOOKUP", ("GA-4471",), "pump-station-drawings"),
    _a("L16", "What flow rate was measured at pump commissioning?", "LOOKUP", ("42 litres per second",), "pump-commissioning"),
    _a("L17", "Who completed the valve line-up check?", "LOOKUP", ("Tomasz Kowalski",), "pump-commissioning"),
    _a("L18", "How many valves are on the discharge manifold?", "LOOKUP", ("18 valves",), "valve-schedule"),
    _a("L19", "What is the total amount due on the annual invoice?", "LOOKUP", ("8,450",), "invoice-2024-annual"),
    _a("L20", "Where is the assembly point?", "LOOKUP", ("north gate",), "induction-slides"),
    _a("L21", "How often is refresher training due?", "LOOKUP", ("12 months",), "training-matrix"),
    _a("L22", "When does the flight to Lisbon land?", "LOOKUP", ("14:35",), "lisbon-itinerary"),
    _a("L23", "When is the dentist appointment?", "LOOKUP", ("18 September",), "dentist-appointment"),
    _a("L24", "When is the next MOT test due?", "LOOKUP", ("13 March 2026",), "mot-certificate"),
    _a("L25", "What is the car insurance excess?", "LOOKUP", ("250 pounds",), "insurance-renewal"),
    _a("L26", "How much butter goes in the lemon cake?", "LOOKUP", ("200 grams",), "lemon-cake"),
    _a("L27", "How long do I bake the lemon cake?", "LOOKUP", ("40 minutes",), "lemon-cake"),

    # ---- FOLLOWUP: needs the conversation --------------------------------------------------
    _a("F01", "And how long do they have to return it?", "FOLLOWUP", ("10 days",), "landlord-deposit-letter",
       history=("What did we agree with the landlord about the deposit?",)),
    _a("F02", "And when does the tenancy start?", "FOLLOWUP", ("1 March 2024",), "tenancy-agreement-2024",
       history=("How much is the rent per month on the 2024 agreement?",)),
    _a("F03", "And when was that?", "FOLLOWUP", ("14 April 2025",), "safety-report-final",
       history=("Who carried out the Leeds safety inspection?",)),
    _a("F04", "And the annual premium?", "FOLLOWUP", ("612 pounds",), "insurance-renewal",
       history=("What is the car insurance excess?",)),
    _a("F05", "And where is the hotel?", "FOLLOWUP", ("Rua Augusta",), "lisbon-itinerary",
       history=("When does the flight to Lisbon land?",)),
    _a("F06", "Who approved it?", "FOLLOWUP", ("Go ahead",), "licence-priya-approval",
       history=("How much was Chris's quote for the licence renewal?",),
       note="the shelf holds Chris's quote, which does not say - so the corpus must be searched"),

    # ---- FIND: the answer is the results ----------------------------------------------------
    QA("D01", "Show me the photos of the kids at the beach in 2015", "FIND", "find", find=("kids-beach-01", "kids-beach-03")),
    QA("D02", "Find the Leeds safety report", "FIND", "find", find=("safety-report-final",)),
    QA("D03", "Show me the invoices", "FIND", "find", find=("invoice-2024-annual",)),
    QA("D04", "Where is the valve schedule?", "FIND", "find", find=("valve-schedule",)),
    QA("D05", "Show me emails from Dave about the audit", "FIND", "find", find=("dave-2020-02-audit",)),
    QA("D06", "Find the recipe for dal", "FIND", "find", find=("dal-tadka",)),
    QA("D07", "Pull up the MOT certificate", "FIND", "find", find=("mot-certificate",)),
    QA("D08", "Show me the Lisbon photos", "FIND", "find", find=("lisbon-tram", "lisbon-castle")),
    QA("D09", "Find the tenancy agreement", "FIND", "find", find=("tenancy-agreement-2024",)),

    # ---- AGGREGATE: computed, and compared with plain Python ---------------------------------
    _agg("A01", "How many photos from Diwali 2019 do I have?", expected_count(ext=_PHOTO, year=2019, words=("diwali",))),
    _agg("A02", "How many emails did Dave send in 2019?", expected_count(mail=True, sender="dave", year=2019)),
    _agg("A03", "How many emails from Dave are there?", expected_count(mail=True, sender="dave")),
    _agg("A04", "How many PDFs do I have?", expected_count(ext=("pdf",))),
    _agg("A05", "How many spreadsheets are in my archive?", expected_count(ext=_SHEET)),
    _agg("A06", "How many emails have an attachment?", expected_count(mail=True, attach=True)),
    _agg("A07", "How many photos do I have?", expected_count(ext=_PHOTO)),
    _agg("A08", "How many Word documents are there?", expected_count(ext=("docx", "doc", "odt"))),
    _agg("A09", "How many files are indexed in total?", len(CORPUS)),
    _agg("A10", "How many emails did Priya send?", expected_count(mail=True, sender="priya")),
    _agg("A11", "How many emails did Chris send in 2025?", expected_count(mail=True, sender="chris", year=2025)),
    _agg("A12", "How much space do my PDFs take up?", expected_count(ext=("pdf",)),
         expect=(human_size(sum(d.size for d in CORPUS if d.ext == "pdf")),)),
    _agg("A13", "List the emails from Priya", expected_count(mail=True, sender="priya")),
    _agg("A14", "When was the latest email from Dave?", expected_count(mail=True, sender="dave"),
         expect=("leeds-safety-dave",)),
    _agg("A15", "How many emails did Dave send in 2019 with an attachment?",
         expected_count(mail=True, sender="dave", year=2019, attach=True)),
    _agg("A16", "How many invoices are there?", expected_count(words=("invoice",))),
    _agg("A17", "How many photos are from 2015?", expected_count(ext=_PHOTO, year=2015)),
    _agg("A18", "How many PDFs are from 2025?", expected_count(ext=("pdf",), year=2025)),
    _agg("A19", "How many documents are there?", expected_count(documents=True)),

    # ---- ABSENCE, planted: nothing in the corpus matches ----------------------------------------
    QA("B01", "Do I have my passport scan?", "ABSENCE", "absence", planted=True),
    QA("B02", "Is there a contract with a solicitor called Whitfield?", "ABSENCE", "absence", planted=True),
    QA("B03", "Did I ever get an email from HMRC?", "ABSENCE", "absence", planted=True),
    QA("B04", "Do I have any photos from the Alps?", "ABSENCE", "absence", planted=True),
    QA("B05", "Do I have the mortgage statement?", "ABSENCE", "absence", planted=True),
    QA("B06", "Is there anything about the loft conversion?", "ABSENCE", "absence", planted=True),
    QA("B07", "Do I have a birth certificate?", "ABSENCE", "absence", planted=True),
    QA("B08", "Did I ever receive an invoice from Acme Plumbing?", "ABSENCE", "absence", planted=True,
       note="'invoice' and 'Acme' both exist in the corpus - only the plumbing is missing"),
    # ...and the same shape of question when the thing IS there
    QA("B09", "Do I have the MOT certificate?", "ABSENCE", "find", find=("mot-certificate",)),
    QA("B10", "Do I have anything from Priya?", "ABSENCE", "find", find=("licence-priya-approval",)),
    QA("B11", "Is there a recipe for lemon cake?", "ABSENCE", "find", find=("lemon-cake",)),

    # ---- traps: retrieval finds words, but no passage answers ------------------------------------
    QA("T01", "What is the rent for the Lisbon hotel?", "LOOKUP", "absence", trap=True,
       forbid=("1,200",), note="rent and the hotel are in different documents"),
    QA("T02", "How much did the MOT cost?", "LOOKUP", "absence", trap=True,
       note="the certificate gives a mileage, not a price"),
    QA("T03", "How much did the dentist charge?", "LOOKUP", "absence", trap=True,
       forbid=("Anita Rao",), note="the appointment names the dentist and gives no price"),

    # ---- SYNTHESIS: across documents (no floor at v1) -----------------------------------------------
    QA("S01", "Summarise everything about the Leeds safety inspection", "SYNTHESIS", "answer",
       expect_any=("guarding", "pump station"), cite="safety-report"),
    QA("S02", "Summarise what the emails say about the licence", "SYNTHESIS", "answer",
       expect_any=("quote", "Approved", "licence"), cite="licence-"),
    QA("S03", "Compare the two tenancy agreements", "SYNTHESIS", "answer",
       expect_any=("1,200", "1,050"), cite="tenancy-agreement"),
    QA("S04", "What do my documents say about the audit?", "SYNTHESIS", "answer",
       expect_any=("March", "findings"), cite="audit"),
    QA("S05", "Give me an overview of the pump station documents", "SYNTHESIS", "answer",
       expect_any=("pump station",), cite="pump-"),
)


# --------------------------------------------------------------------------- loading

def _mtime_ns(when: str) -> int:
    y, m, d = (int(part) for part in when.split("-"))
    return int(datetime.combine(date(y, m, d), clock(12, 0)).timestamp() * 1_000_000_000)


def load_into(store: object, docs: tuple[Doc, ...] = CORPUS) -> dict[str, int]:
    """Put the corpus straight into a store, skipping extraction. Returns
    `{path: file_id}`. Retrieval is what is being measured, not the readers."""
    ids: dict[str, int] = {}
    for doc in docs:
        parent = doc.path.rsplit("/", 1)[0]
        file_id = store.upsert_file(                                # type: ignore[attr-defined]
            path=doc.path, parent_dir=parent, ext=doc.ext, size_bytes=doc.size,
            mtime_ns=_mtime_ns(doc.when), content_hash=doc.path, status="INDEXED",
            source_kind="eml" if doc.is_mail else "file")
        body = doc.text
        if doc.is_mail:
            body = (f"Subject: {doc.subject}\nFrom: {doc.sender}\n"
                    f"To: {', '.join(doc.recipients)}\n\n{doc.text}")
        store.replace_chunks(file_id, [{                            # type: ignore[attr-defined]
            "text": body, "ordinal": 0, "char_start": 0, "char_end": len(body), "page": None}])
        if doc.is_mail:
            store.set_message(                                      # type: ignore[attr-defined]
                file_id, subject=doc.subject, sender=doc.sender,
                recipients=json.dumps(list(doc.recipients)), has_attach=1 if doc.attach else 0)
        ids[doc.path] = file_id
    return ids


def _self_check() -> None:                                          # pragma: no cover
    seen = set()
    for qa in QUESTIONS:
        assert qa.id not in seen, qa.id
        seen.add(qa.id)


_self_check()
