"""Which file types are indexed, and what reads them - from configuration.

Layer: L0

`REGISTRY` in `app/extract/base.py` remains the mechanism: extension to
extractor, one dictionary, resolved at extraction time. **This module feeds it
rather than replacing it.** A second registry would be two sources of truth
about the same question, which is how a file type ends up enabled in one place
and invisible in the other.

**Three tiers, and the boundary is the whole design:**

| Tier | What config can say | Example |
|---|---|---|
| 1 | routing and policy - which extensions, which extractor, size caps | `.log` -> plaintext |
| 2 | run an external converter, then read its output | `.doc` -> LibreOffice -> txt |
| 3 | nothing. This is code. | PDF, PST, OCR |

Tier 2 is where the leverage is: one implementation, and LibreOffice alone then
covers a dozen dead formats, added by editing a text file. **Tier 3 is
deliberately out of reach.** A configuration format that can express arbitrary
parsing is a programming language with no debugger, no type checker and no
tests - strictly worse than the Python it set out to replace.

**Two files, one merged over the other.** The packaged `config/extractors.toml`
ships with the application and is never written to at runtime; the user's
`<DATA_PATH>/extractors.toml` holds their changes. So an upgrade delivers new
defaults without discarding anybody's choices, and deleting the user file
restores shipped behaviour exactly.

**Everything is validated at load.** An unknown key, an extractor name nothing
provides, a `schema_version` from the future - each is an `ERR_CONFIG_INVALID`
naming the offending item, raised while the app is starting. The alternative is
discovering it three hours into a 100GB run, on one file, as a traceback.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from app.core.errors import AppErrorException, make_error
from app.core.logging import logger

__all__ = [
    "FormatRules",
    "ExtensionRule",
    "ConverterRule",
    "load_rules",
    "packaged_path",
    "user_path",
    "SCHEMA_VERSION",
    "save_overrides",
    "differences",
    "added_routes",
    "append_converter",
    "changed_limits",
    "with_override",
    "with_route",
    "DEFAULT_MAX_BYTES",
]

log = logger.bind(component="core.formats")

#: The schema this build writes and understands. Checked like the database's,
#: and for the same reason: an older build opening a newer file must refuse
#: rather than misread it.
SCHEMA_VERSION = 1

DEFAULT_MAX_BYTES = 100 * 1024 * 1024

#: Keys permitted on an `[extensions]` entry. Anything else is a typo, and a
#: silently ignored typo means a setting somebody believes in that does nothing.
_EXTENSION_KEYS = frozenset({"extractor", "enabled", "max_bytes", "note"})

#: Keys permitted on a `[converters.".x"]` entry.
_CONVERTER_KEYS = frozenset({
    "command", "produces", "then", "timeout_s", "enabled", "note",
})

_TOP_LEVEL_KEYS = frozenset({"schema_version", "defaults", "extensions", "converters"})
_DEFAULTS_KEYS = frozenset({"max_bytes", "enabled"})


def _load_toml(path: Path) -> dict[str, Any]:
    """Parse a TOML file. `tomllib` on 3.11+, `tomli` before it.

    The application targets 3.12, where `tomllib` is in the standard library.
    The fallback exists so the test suite runs on older interpreters rather than
    silently skipping every test in this module.
    """
    try:
        import tomllib as toml_reader          # noqa: PLC0415 - stdlib on 3.11+
    except ImportError:                        # pragma: no cover - 3.10 and older
        try:
            import tomli as toml_reader        # noqa: PLC0415
        except ImportError:
            raise AppErrorException(make_error(
                "ERR_CONFIG_INVALID", "core.formats",
                key=str(path), reason="no TOML parser is available",
                suggestion=(
                    "Python 3.11 and later include one. On an older Python: "
                    "pip install tomli"
                ),
            )) from None

    try:
        with path.open("rb") as handle:
            return toml_reader.load(handle)
    except OSError as exc:
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.formats",
            key=str(path), reason=f"could not be read ({exc.__class__.__name__})",
            details=str(exc),
        )) from exc
    except Exception as exc:                   # noqa: BLE001 - tomllib's own error type
        raise AppErrorException(make_error(
            "ERR_CONFIG_INVALID", "core.formats",
            key=str(path), reason=f"is not valid TOML: {exc}",
            suggestion=(
                "Fix the syntax, or delete the file to fall back to the shipped "
                "defaults - nothing else is lost by deleting it."
            ),
        )) from exc


@dataclass(frozen=True, slots=True)
class ExtensionRule:
    """One extension's routing and policy."""

    extension: str
    extractor: str
    enabled: bool = True
    max_bytes: int = DEFAULT_MAX_BYTES
    note: str = ""
    #: True when this rule came from configuration rather than from an
    #: extractor's own `extensions` tuple. Only these can be removed in the
    #: editor - the built-ins belong to the code that parses them.
    from_config: bool = True


@dataclass(frozen=True, slots=True)
class ConverterRule:
    """An external command that turns one format into something readable."""

    extension: str
    command: tuple[str, ...]
    produces: str
    then: str
    timeout_s: int = 180
    enabled: bool = False
    note: str = ""

    @property
    def binary(self) -> str:
        """The executable this would run, for the allow-list and for `doctor`."""
        return self.command[0] if self.command else ""


@dataclass(frozen=True, slots=True)
class FormatRules:
    """Everything the application knows about file types, already validated."""

    extensions: Mapping[str, ExtensionRule] = field(default_factory=dict)
    converters: Mapping[str, ConverterRule] = field(default_factory=dict)
    default_max_bytes: int = DEFAULT_MAX_BYTES
    sources: tuple[Path, ...] = ()

    # -- the questions the rest of the app asks -----------------------------

    def rule_for(self, extension: str) -> Optional[ExtensionRule]:
        """The configured rule for `extension` (with its dot), or None when
        configuration says nothing about it."""
        return self.extensions.get(extension.lower())

    def is_enabled(self, extension: str) -> bool:
        """Unknown extensions are enabled - the built-in registry decides them.

        Config is an override, not an allow-list. Treating absence as "off"
        would mean any extractor added in code stopped working until somebody
        remembered to list it.
        """
        rule = self.rule_for(extension)
        return True if rule is None else rule.enabled

    def max_bytes_for(self, extension: str) -> int:
        """The size cap for one extension: its own rule's, else `[defaults]`."""
        rule = self.rule_for(extension)
        return rule.max_bytes if rule is not None else self.default_max_bytes

    def enabled_extensions(self, known: Iterable[str]) -> frozenset[str]:
        """`known` minus anything configuration has switched off."""
        return frozenset(ext for ext in known if self.is_enabled(ext))

    def converter_for(self, extension: str) -> Optional[ConverterRule]:
        """The Tier 2 converter for `extension`, or None. Only consulted when
        the extractor registry has no reader for it."""
        return self.converters.get(extension.lower())

    def describe(self, registry: Optional[Mapping[str, Any]] = None) -> list[dict[str, Any]]:
        """Every supported file type as plain data, for the CLI and the editor.

        **Pass the registry.** Configuration only lists extensions that need to
        differ from an extractor's own defaults, so the rules alone describe a
        fraction of what the application reads - `.pdf`, `.docx` and `.txt` are
        claimed in code and appear in no TOML file anywhere. Listing only the
        configured ones gave a "File types" table with no PDF in it and no way
        to switch one off, which is a settings page that quietly omits the
        settings people came for.
        """
        known = set(self.extensions) | set(self.converters)
        if registry:
            known |= {str(extension).lower() for extension in registry}

        rows: list[dict[str, Any]] = []
        for extension in sorted(known):
            rule = self.extensions.get(extension)
            converter = self.converters.get(extension)
            built_in = (registry or {}).get(extension)
            rows.append({
                "extension": extension,
                "extractor": (
                    # A policy-only rule carries no reader, so the registry is
                    # asked - it is the authority either way.
                    (rule.extractor if rule and rule.extractor else "")
                    or str(getattr(built_in, "name", "") or "")
                    or (f"converter -> {converter.then}" if converter else "?")
                ),
                "enabled": (
                    rule.enabled if rule
                    else True if built_in is not None
                    else bool(converter and converter.enabled)
                ),
                "max_bytes": self.max_bytes_for(extension),
                "converter": converter.binary if converter else "",
                "note": (rule.note if rule and rule.note
                         else converter.note if converter else ""),
                #: False for anything claimed in code: the editor may switch it
                #: off, but the route itself is not the config file's to remove.
                "from_config": rule is not None and built_in is None,
            })
        return rows


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def packaged_path() -> Path:
    """`config/extractors.toml` beside the application, shipped and tracked."""
    return Path(__file__).resolve().parents[2] / "config" / "extractors.toml"


def user_path(data_path: Path) -> Path:
    """`<DATA_PATH>/extractors.toml` - this machine's overrides."""
    return Path(data_path) / "extractors.toml"


def load_rules(
    data_path: Optional[Path] = None,
    *,
    packaged: Optional[Path] = None,
    known_extractors: Optional[Iterable[str]] = None,
) -> FormatRules:
    """Load, validate and merge the packaged and user files.

    `known_extractors` is the set of registered extractor names. When given,
    every rule naming something outside it is an error **at load** rather than a
    surprise on one file three hours into a run.
    """
    sources: list[Path] = []
    merged: dict[str, Any] = {}

    packaged_file = packaged or packaged_path()
    if packaged_file.is_file():
        merged = _load_toml(packaged_file)
        sources.append(packaged_file)
    else:
        log.debug("no packaged extractors.toml at {}", packaged_file)

    if data_path is not None:
        override = user_path(data_path)
        if override.is_file():
            merged = _merge(merged, _load_toml(override), source=override)
            sources.append(override)

    return _validate(merged, sources=tuple(sources), known_extractors=known_extractors)


def _merge(base: dict[str, Any], over: dict[str, Any], *, source: Path) -> dict[str, Any]:
    """User file over packaged: sections merged, and rules **patched**.

    A user entry updates the packaged rule key by key rather than replacing it.
    This was the other way round at first - replace outright, on the reasoning
    that half a rule from each file matches neither and cannot be reasoned about.

    The editor showed that to be wrong in the case that matters most. Turning
    `.png` off is one key; writing the whole rule to express it pins `extractor`
    and `max_bytes` at today's values forever, so a later release improving
    either would arrive with the improvement silently discarded. **A patch is
    what "I changed this one thing" actually means**, and it is the only shape
    that survives an upgrade.

    A rule for an extension the packaged file does not have must still be
    complete, because there is nothing to patch.
    """
    out = dict(base)
    for key, value in over.items():
        if key in ("extensions", "converters") and isinstance(value, dict):
            section = dict(out.get(key) or {})
            for extension, patch in value.items():
                existing = section.get(extension)
                if isinstance(existing, dict) and isinstance(patch, dict):
                    merged_rule = dict(existing)
                    merged_rule.update(patch)
                    section[extension] = merged_rule
                else:
                    section[extension] = patch
            out[key] = section
        elif key == "defaults" and isinstance(value, dict):
            defaults = dict(out.get(key) or {})
            defaults.update(value)
            out[key] = defaults
        else:
            out[key] = value
    log.debug("merged {} over the packaged defaults", source)
    return out


def _fail(key: str, reason: str, *, suggestion: Optional[str] = None) -> None:
    raise AppErrorException(make_error(
        "ERR_CONFIG_INVALID", "core.formats",
        key=key, reason=reason, suggestion=suggestion,
    ))


def _validate(
    data: Mapping[str, Any],
    *,
    sources: tuple[Path, ...],
    known_extractors: Optional[Iterable[str]],
) -> FormatRules:
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        _fail(
            "extractors.toml", f"unknown section(s): {', '.join(sorted(unknown))}",
            suggestion=f"Expected only: {', '.join(sorted(_TOP_LEVEL_KEYS))}.",
        )

    version = data.get("schema_version", SCHEMA_VERSION)
    if not isinstance(version, int):
        _fail("schema_version", f"must be a whole number, got {version!r}")
    if version > SCHEMA_VERSION:
        _fail(
            "schema_version",
            f"this file is version {version}; this build understands {SCHEMA_VERSION}",
            suggestion=(
                "Update the application, or delete your extractors.toml to fall back "
                "to the shipped defaults. Reading a newer file with an older build "
                "would misconfigure which files get indexed, so it is refused."
            ),
        )

    defaults = data.get("defaults") or {}
    if not isinstance(defaults, dict):
        _fail("defaults", "must be a table")
    unknown_defaults = set(defaults) - _DEFAULTS_KEYS
    if unknown_defaults:
        _fail("defaults", f"unknown key(s): {', '.join(sorted(unknown_defaults))}")

    default_max = defaults.get("max_bytes", DEFAULT_MAX_BYTES)
    if not isinstance(default_max, int) or default_max <= 0:
        _fail("defaults.max_bytes", f"must be a positive whole number, got {default_max!r}")
    default_enabled = bool(defaults.get("enabled", True))

    names = set(known_extractors) if known_extractors is not None else None

    extensions: dict[str, ExtensionRule] = {}
    for raw_ext, body in (data.get("extensions") or {}).items():
        extension = _clean_extension(raw_ext, section="extensions")
        if not isinstance(body, dict):
            _fail(f"extensions.{raw_ext}", "must be a table, for example "
                                          '{ extractor = "plaintext" }')
        unknown_keys = set(body) - _EXTENSION_KEYS
        if unknown_keys:
            _fail(
                f"extensions.{raw_ext}",
                f"unknown key(s): {', '.join(sorted(unknown_keys))}",
                suggestion=f"Expected only: {', '.join(sorted(_EXTENSION_KEYS))}.",
            )
        # **Optional, and its absence means "whatever already reads this".**
        # An entry that only turns a format off or re-caps it has no business
        # restating the reader: `.pdf` is claimed in code, so requiring the name
        # here would copy a fact that lives in the registry into a text file
        # where it can go stale - rename the extractor and every user's config
        # silently routes nowhere. Settings writes these policy-only entries,
        # which is why turning off PDFs must not need the word "pdf".
        extractor = body.get("extractor", "")
        if not isinstance(extractor, str):
            _fail(f"extensions.{raw_ext}.extractor",
                  f"must be an extractor name, got {extractor!r}")

        enabled = bool(body.get("enabled", default_enabled))
        # A disabled rule routes nothing, so the name it does not use cannot be
        # wrong yet. This is what lets the shipped file carry a route for an
        # extractor that arrives in a later release: the line is visible, off,
        # and checked the moment somebody switches it on - which is when the
        # answer starts mattering. Validating it while it is off would mean an
        # upgrade that adds a format has to add config and code in one commit,
        # or the application refuses to start.
        if enabled and extractor and names is not None and extractor not in names:
            _fail(
                f"extensions.{raw_ext}.extractor",
                f"no extractor named {extractor!r} is registered",
                suggestion=(
                    f"Known extractors: {', '.join(sorted(names))}. "
                    f"If {extractor!r} is not built yet, set enabled = false."
                ),
            )
        max_bytes = body.get("max_bytes", default_max)
        if not isinstance(max_bytes, int) or max_bytes <= 0:
            _fail(f"extensions.{raw_ext}.max_bytes",
                  f"must be a positive whole number, got {max_bytes!r}")

        if not extractor and enabled:
            # Nothing to route, so this only makes sense as an override of
            # something that already claims the extension. Logged rather than
            # refused: whether anything claims it is the registry's business,
            # and this module is a layer below the registry deliberately. A
            # rule for an extension nothing reads is inert, not dangerous.
            log.debug(
                "{} has a rule with no extractor - it will adjust whatever "
                "already reads that extension, and do nothing if none does",
                extension,
            )

        extensions[extension] = ExtensionRule(
            extension=extension,
            extractor=extractor,
            enabled=enabled,
            max_bytes=max_bytes,
            note=str(body.get("note", "")),
        )

    converters: dict[str, ConverterRule] = {}
    for raw_ext, body in (data.get("converters") or {}).items():
        extension = _clean_extension(raw_ext, section="converters")
        if not isinstance(body, dict):
            _fail(f"converters.{raw_ext}", "must be a table")
        unknown_keys = set(body) - _CONVERTER_KEYS
        if unknown_keys:
            _fail(
                f"converters.{raw_ext}",
                f"unknown key(s): {', '.join(sorted(unknown_keys))}",
                suggestion=f"Expected only: {', '.join(sorted(_CONVERTER_KEYS))}.",
            )

        command = body.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(part, str) for part in command
        ):
            _fail(f"converters.{raw_ext}.command",
                  "must be a non-empty list of strings, never a single string - "
                  "a command line split by this application is never passed to a shell")
        produces = body.get("produces")
        if not isinstance(produces, str) or not produces:
            _fail(f"converters.{raw_ext}.produces",
                  "is required - name the file the command leaves in {outdir}")
        then = body.get("then")
        if not isinstance(then, str) or not then:
            _fail(f"converters.{raw_ext}.then",
                  "is required - name the extractor that reads the converted file")
        if names is not None and then not in names:
            _fail(f"converters.{raw_ext}.then",
                  f"no extractor named {then!r} is registered",
                  suggestion=f"Known extractors: {', '.join(sorted(names))}.")
        timeout_s = body.get("timeout_s", 180)
        if not isinstance(timeout_s, int) or timeout_s <= 0:
            _fail(f"converters.{raw_ext}.timeout_s",
                  f"must be a positive number of seconds, got {timeout_s!r}")

        converters[extension] = ConverterRule(
            extension=extension,
            command=tuple(command),
            produces=produces,
            then=then,
            timeout_s=timeout_s,
            # Converters are off unless explicitly enabled: the binary may not
            # be installed, and a format that fails on every file is worse than
            # one that says plainly it is switched off.
            enabled=bool(body.get("enabled", False)),
            note=str(body.get("note", "")),
        )

    overlap = set(extensions) & set(converters)
    if overlap:
        _fail(
            "extractors.toml",
            f"{', '.join(sorted(overlap))} appears under both [extensions] and "
            "[converters]",
            suggestion=(
                "One extension, one route. Two would mean the answer depended on "
                "which section happened to be read first."
            ),
        )

    return FormatRules(
        extensions=extensions,
        converters=converters,
        default_max_bytes=default_max,
        sources=sources,
    )


def _clean_extension(raw: str, *, section: str) -> str:
    """Normalise and check one extension key.

    Leading dot required, lowercase enforced. `".PDF"` and `"pdf"` both meaning
    `.pdf` sounds friendly and is a trap: it makes two spellings of the same key
    possible in one file, and TOML will happily accept both.
    """
    if not isinstance(raw, str) or not raw:
        _fail(f"{section}", f"{raw!r} is not a valid extension key")
    if not raw.startswith("."):
        _fail(
            f"{section}.{raw}",
            "extensions must start with a dot",
            suggestion=f'Write ".{raw}" rather than "{raw}".',
        )
    if raw != raw.lower():
        _fail(
            f"{section}.{raw}",
            "extensions must be lowercase",
            suggestion=(
                f'Write "{raw.lower()}". Two spellings of one extension in the same '
                "file would both be accepted and only one would win."
            ),
        )
    return raw


def apply_to_registry(rules: FormatRules, registry: dict[str, Any]) -> list[str]:
    """Point configured extensions at their extractors. Returns what changed.

    Runs against `base.REGISTRY` by name, so an extension listed in TOML becomes
    indexable without any code knowing it exists. An extension whose extractor
    is not registered is skipped with a warning rather than raising: by this
    point the file has already been validated, so the only way to get here is an
    optional extractor that failed to import - and losing one format is better
    than refusing to start.
    """
    by_name: dict[str, Any] = {}
    for extractor in registry.values():
        by_name.setdefault(getattr(extractor, "name", ""), extractor)

    applied: list[str] = []
    for extension, rule in rules.extensions.items():
        if not rule.extractor:
            # A policy-only rule: on/off and size cap for whatever already
            # claims the extension. There is no route to apply.
            continue
        extractor = by_name.get(rule.extractor)
        if extractor is None:
            log.warning(
                "{} is configured to use the {!r} extractor, which is not available - "
                "these files will not be indexed",
                extension, rule.extractor,
            )
            continue
        if registry.get(extension) is not extractor:
            registry[extension] = extractor
            applied.append(extension)
    return applied


def with_override(
    rules: FormatRules,
    extension: str,
    *,
    extractor: Optional[str] = None,
    **changes: Any,
) -> FormatRules:
    """A copy with one extension's rule changed. Used by the editor.

    **An extension with no rule yet is normal, not an error.** Configuration
    lists only what differs from an extractor's own defaults, so `.pdf` has no
    entry until somebody changes something about it. Raising `KeyError` here
    meant the editor could not switch off, or re-cap, any of the formats claimed
    in code - which is most of them. A rule is created instead, and `extractor`
    names the reader it should record (defaulting to the extension itself only
    when the caller genuinely has nothing better, which `load_rules` will then
    reject loudly rather than accept silently).

    Returns a new object because `FormatRules` is frozen and shared: mutating it
    in place would change what a running index run believes about file types
    half-way through.
    """
    extension = extension.lower()
    updated = dict(rules.extensions)
    existing = updated.get(extension)

    if existing is None:
        # `extractor=""` is a policy-only rule: switch it off, or re-cap it,
        # without restating who reads it. That is what Settings writes for the
        # formats claimed in code, which is most of them.
        updated[extension] = ExtensionRule(
            extension=extension,
            extractor=extractor or "",
            max_bytes=rules.default_max_bytes,
            from_config=True,
        )
        existing = updated[extension]

    updated[extension] = replace(existing, **changes)
    return replace(rules, extensions=updated)


def with_route(
    rules: FormatRules, extension: str, extractor: str, *, enabled: bool = True
) -> FormatRules:
    """A copy with one extension routed to an existing extractor.

    Adds the rule if it is new, re-points it if it is not. The extractor name is
    **not** validated here - `load_rules` does that against the live registry on
    the next load, which is the one check that cannot go stale.
    """
    extension = extension.lower()
    updated = dict(rules.extensions)
    existing = updated.get(extension)
    if existing is None:
        updated[extension] = ExtensionRule(
            extension=extension,
            extractor=extractor,
            enabled=enabled,
            max_bytes=rules.default_max_bytes,
            from_config=True,
        )
    else:
        updated[extension] = replace(existing, extractor=extractor, enabled=enabled)
    return replace(rules, extensions=updated)


def without_route(rules: FormatRules, extension: str) -> FormatRules:
    """A copy with a user-added extension removed entirely."""
    extension = extension.lower()
    updated = dict(rules.extensions)
    updated.pop(extension, None)
    return replace(rules, extensions=updated)


def changed_limits(rules: FormatRules, packaged: Optional[FormatRules] = None) -> dict[str, int]:
    """Extensions whose size cap differs from the shipped default.

    Separate from `differences` so that turning a format off and raising its cap
    stay independent facts: writing one must not silently restore the other.
    """
    baseline = packaged or _packaged_only()
    out: dict[str, int] = {}
    for extension, rule in rules.extensions.items():
        shipped = baseline.extensions.get(extension)
        if shipped is None:
            if rule.max_bytes != rules.default_max_bytes:
                out[extension] = rule.max_bytes
        elif shipped.max_bytes != rule.max_bytes:
            out[extension] = rule.max_bytes
    return out


def save_overrides(
    data_path: Path,
    changes: Mapping[str, bool],
    routes: Optional[Mapping[str, str]] = None,
    limits: Optional[Mapping[str, int]] = None,
) -> Path:
    """Write the user's file-type choices to `<DATA_PATH>/extractors.toml`.

    **Only the differences.** The packaged file is replaced on upgrade, so a
    user file that copied everything would freeze today's defaults forever - a
    new format added in a later release would arrive switched off, or with an
    old size limit, and nobody would know why.

    `routes` carries extensions the user has *added*: `{".ino": "plaintext"}`,
    a Tier 1 route to an extractor that already exists. That is the only kind of
    new file type configuration can introduce, and the reason it is safe to
    expose in Settings - it chooses among readers that shipped, it cannot invent
    one. Anything needing a new parser is code, and stays code.

    Written whole and atomically: a half-written config that fails to parse
    stops the application starting, and the person who caused it was only trying
    to turn off `.png`.
    """
    entries: dict[str, dict[str, Any]] = {}
    for extension, enabled in changes.items():
        entries.setdefault(extension.lower(), {})["enabled"] = bool(enabled)
    for extension, extractor in (routes or {}).items():
        entry = entries.setdefault(extension.lower(), {})
        entry["extractor"] = str(extractor)
        entry.setdefault("enabled", True)
    for extension, max_bytes in (limits or {}).items():
        entry = entries.setdefault(extension.lower(), {})
        entry["max_bytes"] = int(max_bytes)
        entry.setdefault("enabled", True)

    target = user_path(data_path)
    lines = [
        "# Your file-type choices. Written by Settings.",
        "#",
        "# Only differences from the packaged defaults are stored here, so an",
        "# upgrade still brings you new formats and better limits. Deleting this",
        "# file restores the shipped behaviour exactly - it is always safe.",
        "",
        f"schema_version = {SCHEMA_VERSION}",
        "",
        "[extensions]",
    ]
    for extension in sorted(entries):
        entry = entries[extension]
        parts = []
        if "extractor" in entry:
            parts.append(f'extractor = "{entry["extractor"]}"')
        parts.append(f'enabled = {"true" if entry.get("enabled", True) else "false"}')
        if "max_bytes" in entry:
            parts.append(f'max_bytes = {entry["max_bytes"]}')
        lines.append(f'"{extension}" = {{ {", ".join(parts)} }}')

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".toml.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8")
    temporary.replace(target)
    log.info("wrote {} override(s) to {}", len(entries), target)
    return target


def append_converter(data_path: Path, rule: Mapping[str, Any]) -> Path:
    """Add one converter block to the user's file, keeping everything else.

    **Appended, never rewritten.** `save_overrides` writes the `[extensions]`
    section whole, which is safe because it is generated from the switches; the
    converters are not, and rewriting the file from a partial view of it would
    drop any block this editor did not create.

    The binary is checked against the allow-list here as well as at run time.
    Refusing at run time is what keeps the application safe; refusing here is
    what stops somebody saving a route that will never fire and waiting until
    the next index run to find out.
    """
    extension = _clean_extension(str(rule["extension"]), section="converters")
    command = [str(part) for part in rule["command"]]
    if not command:
        _fail(f"converters.{extension}.command", "is empty")

    try:
        from app.extract.converter import ALLOWED_BINARIES
    except ImportError:                          # pragma: no cover - partial install
        ALLOWED_BINARIES = frozenset()

    if ALLOWED_BINARIES and command[0] not in ALLOWED_BINARIES:
        _fail(
            f"converters.{extension}.command",
            f"{command[0]!r} is not an allowed converter",
            suggestion=(
                "Converters may only run programs on a fixed list held in the "
                "application's own code - a configuration file that could name "
                f"any executable would be a way to run anything. Allowed: "
                f"{', '.join(sorted(ALLOWED_BINARIES))}."
            ),
        )

    target = user_path(data_path)
    existing = target.read_text(encoding="utf-8-sig") if target.is_file() else ""
    if not existing.strip():
        existing = f"schema_version = {SCHEMA_VERSION}\n"
    if f'[converters."{extension}"]' in existing:
        _fail(
            f"converters.{extension}",
            "already has a converter in your settings file",
            suggestion=f"Edit or remove it in {target}, then add it again.",
        )

    rendered = ", ".join(f'"{part}"' for part in command)
    block = (
        f'\n[converters."{extension}"]\n'
        f"command   = [{rendered}]\n"
        f'produces  = "{rule.get("produces", "{stem}.txt")}"\n'
        f'then      = "{rule["then"]}"\n'
        f"timeout_s = {int(rule.get('timeout_s', 180))}\n"
        f"enabled   = {'true' if rule.get('enabled', True) else 'false'}\n"
    )

    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".toml.tmp")
    temporary.write_text(existing.rstrip("\n") + "\n" + block, encoding="utf-8")
    temporary.replace(target)
    log.info("added a converter for {} to {}", extension, target)
    return target


def added_routes(rules: FormatRules, packaged: Optional[FormatRules] = None) -> dict[str, str]:
    """Extensions this machine has added, as `{extension: extractor}`.

    These are the only rows the editor may delete: a built-in route belongs to
    the code that parses it, and removing it from a text file would not stop the
    extractor claiming the extension anyway - it would just look as though it had.
    """
    baseline = packaged or _packaged_only()
    return {
        extension: rule.extractor
        for extension, rule in rules.extensions.items()
        # A policy-only rule (no reader named) is an override of something that
        # already exists, not a route this machine invented.
        if extension not in baseline.extensions and rule.extractor
    }


def differences(rules: FormatRules, packaged: Optional[FormatRules] = None) -> dict[str, bool]:
    """Which extensions are set differently from the shipped defaults.

    This is what `save_overrides` should be given: storing the whole current
    state would pin every default at today's value.
    """
    baseline = packaged or _packaged_only()
    out: dict[str, bool] = {}
    for extension, rule in rules.extensions.items():
        shipped = baseline.extensions.get(extension)
        if shipped is None or shipped.enabled != rule.enabled:
            out[extension] = rule.enabled
    return out


def _packaged_only() -> FormatRules:
    """The shipped defaults with no user file merged over them."""
    return load_rules(None)
