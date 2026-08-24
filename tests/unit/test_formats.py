"""Which file types are indexed, and what the config is allowed to say.

Layer: L0

The point of validating at load is that a mistake in `extractors.toml` should
cost a startup error naming the key, not three hours of a 100GB run followed by
one unexplained skip. So most of what is asserted here is the *quality of the
refusal*: that the message names the offending item, and that it says what to do.

The other half is the tier boundary. Config routes and sets policy; it cannot
describe parsing, and it cannot name an arbitrary executable. Both of those are
tested as things the loader refuses, because a boundary nobody checks is a
boundary that moves.
"""

from __future__ import annotations

import pytest

from app.core.errors import AppErrorException
from app.core.formats import (
    ConverterRule,
    ExtensionRule,
    FormatRules,
    SCHEMA_VERSION,
    load_rules,
    packaged_path,
)


def write(tmp_path, body: str, name: str = "extractors.toml"):
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    return path


def load(tmp_path, body: str, **kwargs):
    """Load `body` as if it were the packaged file."""
    return load_rules(packaged=write(tmp_path, body), **kwargs)


# -- the file that actually ships --------------------------------------------

def test_the_packaged_file_loads():
    """It is tracked in git and read at every startup. If it does not parse,
    the application does not open - which makes this the cheapest test here."""
    rules = load_rules()
    assert rules.extensions, "the shipped defaults should route something"
    assert rules.sources, "it should record where it read from"


def test_every_shipped_converter_is_disabled():
    """The binary may not be installed. A format that fails on every file is
    worse than one that says plainly it is switched off."""
    rules = load_rules()
    enabled = [rule.extension for rule in rules.converters.values() if rule.enabled]
    assert not enabled, f"these ship enabled and should not: {enabled}"


def test_no_converter_command_is_a_bare_string():
    """A string would have to be split, and splitting a command line is one step
    from handing it to a shell. Every command is a list from the start."""
    rules = load_rules()
    for rule in rules.converters.values():
        assert isinstance(rule.command, tuple)
        assert all(isinstance(part, str) for part in rule.command)


# -- routing ------------------------------------------------------------------

def test_an_extension_routes_to_its_extractor(tmp_path):
    rules = load(tmp_path, '[extensions]\n".log" = { extractor = "plaintext" }\n')
    assert rules.rule_for(".log").extractor == "plaintext"


def test_lookup_is_case_insensitive_at_the_point_of_use(tmp_path):
    """Windows hands back `.LOG` as often as `.log`."""
    rules = load(tmp_path, '[extensions]\n".log" = { extractor = "plaintext" }\n')
    assert rules.rule_for(".LOG") is not None


def test_an_unlisted_extension_is_still_enabled(tmp_path):
    """Config is an override, not an allow-list.

    Treating absence as "off" would mean every extractor added in code stopped
    working until somebody remembered to list it - a failure mode where the
    symptom (files silently not indexed) is a long way from the cause.
    """
    rules = load(tmp_path, '[extensions]\n".log" = { extractor = "plaintext" }\n')
    assert rules.is_enabled(".pdf")


def test_disabling_an_extension_reports_it_as_off(tmp_path):
    rules = load(
        tmp_path,
        '[extensions]\n".png" = { extractor = "ocr", enabled = false }\n',
    )
    assert not rules.is_enabled(".png")
    assert ".png" not in rules.enabled_extensions({".png", ".pdf"})


def test_a_per_format_size_cap_overrides_the_default(tmp_path):
    rules = load(tmp_path, """
[defaults]
max_bytes = 1000
[extensions]
".png" = { extractor = "ocr", max_bytes = 50 }
".txt" = { extractor = "plaintext" }
""")
    assert rules.max_bytes_for(".png") == 50
    assert rules.max_bytes_for(".txt") == 1000
    assert rules.max_bytes_for(".unknown") == 1000


# -- the user file over the packaged one --------------------------------------

def test_the_user_file_wins(tmp_path):
    packaged = write(tmp_path, '[extensions]\n".log" = { extractor = "plaintext" }\n')
    data = tmp_path / "data"
    data.mkdir()
    (data / "extractors.toml").write_text(
        '[extensions]\n".log" = { extractor = "plaintext", enabled = false }\n',
        encoding="utf-8",
    )

    rules = load_rules(data, packaged=packaged)

    assert not rules.is_enabled(".log")
    assert len(rules.sources) == 2, "it should record both files it read"


def test_the_user_file_adds_without_removing(tmp_path):
    """An upgrade brings new defaults; a user file must not silently drop them."""
    packaged = write(tmp_path, """
[extensions]
".log" = { extractor = "plaintext" }
".cfg" = { extractor = "plaintext" }
""")
    data = tmp_path / "data"
    data.mkdir()
    (data / "extractors.toml").write_text(
        '[extensions]\n".ini" = { extractor = "plaintext" }\n', encoding="utf-8"
    )

    rules = load_rules(data, packaged=packaged)

    assert set(rules.extensions) == {".log", ".cfg", ".ini"}


def test_no_user_file_is_not_an_error(tmp_path):
    """The common case: nobody has changed anything."""
    packaged = write(tmp_path, '[extensions]\n".log" = { extractor = "plaintext" }\n')
    rules = load_rules(tmp_path / "nonexistent", packaged=packaged)
    assert rules.rule_for(".log") is not None


# -- refusals, and the quality of them ----------------------------------------

def test_an_unknown_key_names_itself(tmp_path):
    """A silently ignored typo is a setting somebody believes in that does
    nothing at all - the worst kind of configuration bug."""
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, '[extensions]\n".log" = { extracter = "plaintext" }\n')

    rendered = caught.value.error.render()
    assert "extracter" in rendered, "the message must name the key that is wrong"
    assert "extractor" in rendered, "and list what was expected"


def test_an_unknown_extractor_name_is_caught_at_load(tmp_path):
    with pytest.raises(AppErrorException) as caught:
        load(
            tmp_path,
            '[extensions]\n".log" = { extractor = "telepathy" }\n',
            known_extractors={"plaintext", "pdf"},
        )

    rendered = caught.value.error.render()
    assert "telepathy" in rendered
    assert "plaintext" in rendered, "it should say what names are available"


def test_a_newer_schema_is_refused_rather_than_guessed_at(tmp_path):
    """Same rule as the database's version check. An older build reading a newer
    file would misconfigure which files get indexed, and would do it quietly."""
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, f"schema_version = {SCHEMA_VERSION + 1}\n")

    assert str(SCHEMA_VERSION + 1) in caught.value.error.render()


def test_an_extension_without_a_dot_is_refused_with_the_correction(tmp_path):
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, '[extensions]\nlog = { extractor = "plaintext" }\n')

    assert '".log"' in caught.value.error.render()


def test_an_uppercase_extension_is_refused(tmp_path):
    """`".PDF"` and `".pdf"` in one file are two keys TOML would both accept and
    only one of which could win. Refusing is the only answer that stays true."""
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, '[extensions]\n".LOG" = { extractor = "plaintext" }\n')

    assert '".log"' in caught.value.error.render()


def test_one_extension_cannot_be_in_both_sections(tmp_path):
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, """
[extensions]
".doc" = { extractor = "plaintext" }
[converters.".doc"]
command  = ["soffice", "{input}"]
produces = "{stem}.txt"
then     = "plaintext"
""")

    assert ".doc" in caught.value.error.render()


def test_a_converter_command_given_as_a_string_is_refused(tmp_path):
    """The important one. A string has to be split to be run, and the obvious
    way to split it is a shell - which is a way to run anything."""
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, """
[converters.".doc"]
command  = "soffice --headless {input}"
produces = "{stem}.txt"
then     = "plaintext"
""")

    assert "shell" in caught.value.error.render().lower()


def test_a_converter_must_say_what_reads_its_output(tmp_path):
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, """
[converters.".doc"]
command  = ["soffice", "{input}"]
produces = "{stem}.txt"
""")

    assert "then" in caught.value.error.render()


def test_a_negative_size_cap_is_refused(tmp_path):
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, '[extensions]\n".log" = { extractor = "x", max_bytes = -1 }\n')

    assert "max_bytes" in caught.value.error.render()


def test_broken_toml_says_so_and_says_what_to_do(tmp_path):
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, '[extensions\n".log" = {')

    rendered = caught.value.error.render()
    assert "TOML" in rendered
    assert "delete" in rendered.lower(), "deleting the file is always a safe way out"


def test_an_unknown_top_level_section_is_refused(tmp_path):
    with pytest.raises(AppErrorException) as caught:
        load(tmp_path, '[extractors]\n".log" = { extractor = "plaintext" }\n')

    assert "extractors" in caught.value.error.render()


# -- feeding the real registry ------------------------------------------------

def test_config_points_an_extension_at_a_registered_extractor():
    """The whole mechanism: a TOML line makes a file type indexable, using the
    registry that already exists rather than a second one beside it."""
    from app.core.formats import apply_to_registry

    class Fake:
        name = "plaintext"

    fake = Fake()
    registry = {".txt": fake}
    rules = FormatRules(extensions={".log": ExtensionRule(".log", "plaintext")})

    changed = apply_to_registry(rules, registry)

    assert registry[".log"] is fake
    assert changed == [".log"]


def test_a_missing_extractor_loses_one_format_rather_than_the_app():
    """By this point the file has been validated, so the only way here is an
    optional extractor that failed to import. Refusing to start over one format
    would be a self-inflicted outage."""
    from app.core.formats import apply_to_registry

    registry: dict = {}
    rules = FormatRules(extensions={".png": ExtensionRule(".png", "ocr")})

    assert apply_to_registry(rules, registry) == []
    assert registry == {}


def test_every_registered_extractor_has_a_name():
    """Config names extractors by name. One without a name cannot be configured,
    and would fail as an unhelpful "no extractor named ''"."""
    import app.extract  # noqa: F401 - importing populates the registry
    from app.extract.base import REGISTRY

    nameless = [ext for ext, e in REGISTRY.items() if not getattr(e, "name", "")]
    assert not nameless, f"these extensions have a nameless extractor: {nameless}"


def test_a_disabled_rule_may_name_an_extractor_that_does_not_exist_yet(tmp_path):
    """The exemption that lets config and code ship in separate releases.

    A disabled route opens no files, so the name it does not use cannot be wrong
    yet. Validating it anyway would mean adding a format required changing the
    TOML and writing the extractor in one commit - or the application would
    refuse to start. The check runs the instant the line is enabled.
    """
    rules = load(
        tmp_path,
        '[extensions]\n".odt" = { extractor = "odf", enabled = false }\n',
        known_extractors={"plaintext"},
    )
    assert not rules.is_enabled(".odt")


def test_enabling_a_rule_for_a_missing_extractor_is_still_refused(tmp_path):
    """The other half of the exemption: turning it on turns the check on."""
    with pytest.raises(AppErrorException) as caught:
        load(
            tmp_path,
            '[extensions]\n".odt" = { extractor = "odf", enabled = true }\n',
            known_extractors={"plaintext"},
        )

    rendered = caught.value.error.render()
    assert "odf" in rendered
    assert "enabled = false" in rendered, "and it should say how to defer it"


def test_every_enabled_route_in_the_packaged_file_works_today():
    """The guard that stops the shipped file promising what the code cannot do.

    An enabled route to a missing extractor is not a warning - `load_rules` is
    called at startup, so it stops the application opening. This is that failure
    caught in CI instead.
    """
    import app.extract  # noqa: F401 - importing populates the registry
    from app.extract.base import extractor_names

    known = set(extractor_names())
    rules = load_rules()

    broken = {
        rule.extractor for rule in rules.extensions.values()
        if rule.enabled and rule.extractor not in known
    } | {
        rule.then for rule in rules.converters.values()
        if rule.enabled and rule.then not in known
    }
    assert not broken, f"enabled routes name extractors nothing provides: {broken}"


def test_describe_covers_both_sections():
    rules = FormatRules(
        extensions={".log": ExtensionRule(".log", "plaintext")},
        converters={".doc": ConverterRule(".doc", ("soffice",), "x.txt", "plaintext")},
    )
    extensions = {row["extension"] for row in rules.describe()}
    assert extensions == {".log", ".doc"}


# ---------------------------------------------------------------------------
# The editor: saving choices without freezing today's defaults
# ---------------------------------------------------------------------------

def test_only_the_differences_are_stored(tmp_path):
    """**The property that makes an upgrade work.**

    The packaged file is replaced on upgrade, so writing the whole current state
    would pin today's defaults forever: a format added in a later release would
    arrive switched off, or with an old size limit, and nobody would know why.
    """
    from app.core.formats import differences, with_override

    rules = load_rules(tmp_path)
    changed = with_override(rules, ".png", enabled=False)

    assert differences(changed) == {".png": False}


def test_no_changes_means_no_overrides_at_all(tmp_path):
    from app.core.formats import differences

    assert differences(load_rules(tmp_path)) == {}


def test_a_saved_override_survives_a_reload(tmp_path):
    from app.core.formats import differences, save_overrides, with_override

    rules = load_rules(tmp_path)
    save_overrides(tmp_path, differences(with_override(rules, ".png", enabled=False)))

    assert load_rules(tmp_path).is_enabled(".png") is False


def test_an_override_patches_the_rule_rather_than_replacing_it(tmp_path):
    """The decision that was reversed, and why.

    A user entry originally replaced the packaged rule outright, on the
    reasoning that half a rule from each file matches neither. The editor showed
    that wrong in the case that matters: turning `.png` off is one key, and
    writing the whole rule to express it would pin `extractor` and `max_bytes`
    at today's values - so a later release improving either would have the
    improvement silently discarded.
    """
    from app.core.formats import save_overrides

    save_overrides(tmp_path, {".png": False})
    rule = load_rules(tmp_path).rule_for(".png")

    assert rule.enabled is False
    assert rule.extractor == "ocr", "the extractor came from the packaged file"
    assert rule.max_bytes < load_rules(tmp_path).default_max_bytes, "so did the cap"


def test_a_brand_new_extension_must_still_be_complete(tmp_path):
    """There is nothing to patch, so a partial rule is a mistake worth naming."""
    (tmp_path / "extractors.toml").write_text(
        '[extensions]\n".brandnew" = { enabled = true }\n', encoding="utf-8")

    with pytest.raises(AppErrorException) as caught:
        load_rules(tmp_path)
    assert "extractor" in caught.value.error.render()


def test_the_override_file_can_always_be_deleted(tmp_path):
    """Stated in the file's own header, and true: deleting it restores the
    shipped behaviour exactly, which makes it a safe thing to try."""
    from app.core.formats import save_overrides, user_path

    save_overrides(tmp_path, {".png": False})
    assert load_rules(tmp_path).is_enabled(".png") is False

    user_path(tmp_path).unlink()
    assert load_rules(tmp_path).is_enabled(".png") is True


def test_saving_is_atomic(tmp_path):
    """A half-written config that fails to parse stops the application starting,
    and the person who caused it was only trying to turn off `.png`."""
    from app.core.formats import save_overrides, user_path

    save_overrides(tmp_path, {".png": False})
    written = user_path(tmp_path)

    assert written.is_file()
    assert not list(tmp_path.glob("*.tmp")), "no temporary file should be left behind"
    load_rules(tmp_path)                        # parses, therefore complete
