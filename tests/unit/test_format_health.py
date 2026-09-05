"""Whether each file type actually works, and whether it says so.

Layer: L0

The failure this exists to prevent: `.doc` switched on, correctly routed to a
converter, failing on every single file because LibreOffice is not installed -
silently, once per file, three hours into a run. The person sees an empty result
set and concludes the search is bad.

Every test below asserts the same property from a different angle: **a format
that cannot read anything must say so, and say what would fix it.**
"""

from __future__ import annotations

from app.core.format_health import (
    BLOCKED,
    DEGRADED,
    OFF,
    READY,
    FormatStatus,
    Requirement,
    format_health,
    module_present,
    summarise,
)


class FakeRules:
    """Just the two questions `format_health` asks of configuration."""

    def __init__(self, disabled=(), converters=None):
        self._disabled = set(disabled)
        self.converters = converters or {}

    def is_enabled(self, extension: str) -> bool:
        return extension not in self._disabled


class FakeConverter:
    def __init__(self, binary="soffice", then="plaintext", enabled=True):
        self.binary = binary
        self.then = then
        self.enabled = enabled


def extractor(name, *requires):
    """An extractor stand-in: only `name` and `requires` are ever read."""
    return type("Fake", (), {"name": name, "requires": tuple(requires)})()


PRESENT = Requirement("sys", "sys", provides="the standard library")
ABSENT_HARD = Requirement("no_such_module_xyz", "ghost-pkg",
                          provides="everything", hard=True)
ABSENT_SOFT = Requirement("no_such_module_xyz", "ghost-pkg",
                          provides="the good bits", hard=False)


# --- probing ---------------------------------------------------------------

def test_module_present_answers_without_importing():
    assert module_present("json") is True
    assert module_present("no_such_module_xyz") is False


def test_a_broken_parent_package_counts_as_missing():
    """`find_spec` raises ValueError for some half-installed trees. A package
    that cannot be resolved is not usable, which is the only thing we asked."""
    assert module_present("json.nonexistent.deep") is False


# --- extractor states ------------------------------------------------------

def test_an_extractor_with_no_requirements_is_ready():
    statuses = format_health(FakeRules(), {".txt": extractor("plaintext")})
    assert [s.state for s in statuses] == [READY]


def test_a_missing_hard_requirement_blocks_and_names_the_fix():
    statuses = format_health(FakeRules(), {".msg": extractor("msg", ABSENT_HARD)})
    status = statuses[0]

    assert status.state == BLOCKED
    assert not status.ok
    assert "no_such_module_xyz" in status.detail
    assert "pip install ghost-pkg" in status.fix, "the row must carry the command"


def test_a_missing_soft_requirement_degrades_rather_than_blocks():
    """`.vsd` without olefile still indexes. Reporting that as broken would
    send somebody chasing a fix for something that is working."""
    statuses = format_health(FakeRules(), {".vsd": extractor("visio", ABSENT_SOFT)})
    status = statuses[0]

    assert status.state == DEGRADED
    assert status.ok, "degraded still reads files"
    assert "the good bits" in status.detail
    assert "pip install ghost-pkg" in status.fix


def test_a_hard_miss_wins_over_a_soft_one():
    """Reporting the optional gap while the mandatory one is unmet would send
    somebody to install the wrong thing."""
    statuses = format_health(
        FakeRules(), {".x": extractor("mixed", ABSENT_SOFT, ABSENT_HARD)}
    )
    assert statuses[0].state == BLOCKED


def test_a_requirement_only_applies_to_the_extensions_it_names():
    """One extractor, two formats with different needs. Telling a `.vsd` owner
    to install `vsdx` is a fix that changes nothing - and a column full of
    fixes that change nothing is a column nobody reads."""
    only_vsdx = Requirement("no_such_module_xyz", "ghost-pkg",
                            provides="shape text", hard=False,
                            extensions=(".vsdx",))
    reader = extractor("visio", only_vsdx)

    statuses = {
        s.extension: s
        for s in format_health(FakeRules(), {".vsdx": reader, ".vsd": reader})
    }

    assert statuses[".vsdx"].state == DEGRADED
    assert statuses[".vsd"].state == READY, (
        "a package irrelevant to this extension must not mark it degraded"
    )


def test_present_requirements_do_not_degrade_anything():
    statuses = format_health(FakeRules(), {".x": extractor("fine", PRESENT)})
    assert statuses[0].state == READY


def test_switched_off_is_a_choice_not_a_fault():
    statuses = format_health(
        FakeRules(disabled={".png"}), {".png": extractor("ocr", ABSENT_HARD)}
    )
    status = statuses[0]

    assert status.state == OFF, "an off format must never be reported as broken"
    assert status.fix == "", "nothing to fix - it was turned off deliberately"


# --- converter states ------------------------------------------------------

def test_an_enabled_converter_without_its_binary_is_blocked():
    """The exact case that was invisible: on, routed, and reading nothing."""
    rules = FakeRules(converters={".doc": FakeConverter(binary="soffice")})
    statuses = format_health(rules, {}, binaries={"soffice": None})
    status = statuses[0]

    assert status.state == BLOCKED
    assert "soffice" in status.detail
    assert "LibreOffice" in status.fix


def test_an_enabled_converter_with_its_binary_is_ready():
    rules = FakeRules(converters={".doc": FakeConverter(binary="soffice")})
    statuses = format_health(rules, {}, binaries={"soffice": "C:/lo/soffice.exe"})

    assert statuses[0].state == READY


def test_a_disabled_converter_says_whether_turning_it_on_would_work():
    """Settings offers to enable exactly the converters whose binary exists;
    this is the fact that promise rests on."""
    rules = FakeRules(converters={".doc": FakeConverter(enabled=False)})

    with_binary = format_health(rules, {}, binaries={"soffice": "C:/x"})[0]
    without = format_health(rules, {}, binaries={"soffice": None})[0]

    assert with_binary.state == OFF and "can be turned on" in with_binary.detail
    assert without.state == OFF and "not installed" in without.detail


def test_an_extractor_beats_a_converter_for_the_same_extension():
    """`extract()` only reaches Tier 2 when the registry has no answer, so a
    converter for a claimed extension is dead config and must not be reported
    as if it were live."""
    rules = FakeRules(converters={".pdf": FakeConverter()})
    statuses = format_health(rules, {".pdf": extractor("pdf")}, binaries={})

    assert len(statuses) == 1
    assert statuses[0].reader == "pdf"


# --- aggregate -------------------------------------------------------------

def test_summarise_counts_every_state():
    counts = summarise([
        FormatStatus(".a", "x", READY),
        FormatStatus(".b", "x", BLOCKED),
        FormatStatus(".c", "x", BLOCKED),
        FormatStatus(".d", "x", OFF),
    ])
    assert counts == {READY: 1, DEGRADED: 0, BLOCKED: 2, OFF: 1}


def test_health_never_raises_on_broken_rules():
    """Settings calls this while opening, and doctor while diagnosing a broken
    install. An exception is the least useful possible answer in both."""
    class Hostile:
        converters = {}

        def is_enabled(self, _extension):
            raise RuntimeError("configuration is in pieces")

    statuses = format_health(Hostile(), {".txt": extractor("plaintext")})
    assert statuses[0].state == READY, "an unreadable rule falls back to enabled"


def test_the_four_moved_formats_are_ready_with_libreoffice_absent():
    """docs/WORKORDER-libraries-before-converters.md item 8.

    `.xls`, `.rtf`, `.fb2` and `.epub` moved off LibreOffice onto `xlrd`,
    `striprtf` and the standard library. Their health must not depend on
    LibreOffice at all - simulating "not on this machine" for every converter
    binary must not move any of the four off READY, or the whole point of the
    work is unverified.
    """
    import app.extract  # noqa: F401 - registration side effects
    from app.core.formats import load_rules

    no_libreoffice = {name: None for name in
                      ("soffice", "libreoffice", "pandoc", "tesseract",
                       "xstexporter", "dwg2dxf", "ODAFileConverter")}
    statuses = format_health(load_rules(None), binaries=no_libreoffice)
    by_extension = {s.extension: s for s in statuses}

    for extension in (".xls", ".rtf", ".fb2", ".epub"):
        status = by_extension[extension]
        assert status.state == READY, (
            f"{extension} is {status.state!r} with no converter binaries present "
            f"({status.detail}) - it must read via its own library alone"
        )


def test_the_real_registry_reports_every_extension():
    """Against the live registry, so a new extractor that forgets to declare
    itself sensibly shows up here rather than in somebody's index run."""
    import app.extract  # noqa: F401 - registration side effects
    from app.core.formats import load_rules

    statuses = format_health(load_rules(None))
    by_extension = {s.extension: s for s in statuses}

    assert ".pdf" in by_extension
    assert ".dxf" in by_extension, "AutoCAD drawings should be routed"
    for status in statuses:
        assert status.state in (READY, DEGRADED, BLOCKED, OFF)
        if status.state == BLOCKED:
            assert status.fix, f"{status.extension} is blocked with no fix offered"
