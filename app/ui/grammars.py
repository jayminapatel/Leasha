r"""What the preview pane knows about code, with no Qt in it.

Layer: L5

The tables and the patterns live here for the same reason `presenter.py` exists:
a regex that hangs, over-matches or fails to compile is the kind of bug that has
to be caught by a test, and a test that needs a display is a test that does not
run. `widgets/highlight.py` takes these strings, compiles them and paints; this
file decides what a grammar *is*.

**Grouped by grammar, not by language.** `.ts` and `.java` differ in ways a
preview pane cannot see, and pretending otherwise means maintaining forty rule
sets that are all the same rule set. An extension not listed gets no colouring
at all, which is the honest outcome: guessing at a grammar puts arbitrary words
in keyword blue, and that reads as a rendering fault rather than a guess.
"""

from __future__ import annotations

import re
from typing import Optional

__all__ = [
    "LANGUAGES", "language_for", "KEYWORDS", "CONSTANTS", "COMMENTS",
    "STRINGS", "BACKTICK", "NUMBER", "patterns_for", "BLOCK_COMMENTS",
]

_C_FAMILY = (
    "c", "h", "cpp", "cc", "cxx", "hpp", "cs", "java", "js", "jsx", "ts", "tsx",
    "go", "rs", "swift", "kt", "kts", "scala", "php", "m", "mm", "dart", "groovy",
)
_HASH_FAMILY = (
    "py", "pyw", "rb", "sh", "bash", "zsh", "ps1", "psm1", "r", "pl",
    "yml", "yaml", "toml", "ini", "cfg", "conf", "dockerfile", "mk",
)
_MARKUP = ("html", "htm", "xml", "xhtml", "svg", "vue", "xaml", "csproj", "props")
_SQL = ("sql", "psql", "ddl")

#: Extension to grammar. The one place a new file type gets colouring.
LANGUAGES: dict[str, str] = {
    **{ext: "c" for ext in _C_FAMILY},
    **{ext: "hash" for ext in _HASH_FAMILY},
    **{ext: "markup" for ext in _MARKUP},
    **{ext: "sql" for ext in _SQL},
    "json": "json",
    "css": "css",
    "scss": "css",
    "less": "css",
}

#: Words worth colouring, per grammar. **Deliberately incomplete.** A keyword
#: list that chases every dialect ends up colouring ordinary identifiers, which
#: is worse than missing one. These are the words that carry the structure.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "c": (
        "abstract", "as", "async", "await", "break", "case", "catch", "class",
        "const", "continue", "default", "defer", "delete", "do", "else", "enum",
        "export", "extends", "extern", "final", "finally", "fn", "for", "func",
        "function", "go", "goto", "if", "impl", "implements", "import", "in",
        "instanceof", "interface", "let", "match", "mut", "namespace", "new",
        "package", "private", "protected", "public", "return", "static",
        "struct", "super", "switch", "this", "throw", "trait", "try", "type",
        "typedef", "union", "unsafe", "use", "var", "void", "while", "yield",
    ),
    "hash": (
        "and", "as", "assert", "async", "await", "begin", "break", "case",
        "class", "continue", "def", "del", "do", "elif", "else", "elsif", "end",
        "esac", "except", "fi", "finally", "for", "from", "function", "global",
        "if", "import", "in", "is", "lambda", "local", "module", "nonlocal",
        "not", "or", "param", "pass", "raise", "return", "self", "then", "try",
        "unless", "until", "while", "with", "yield",
    ),
    "sql": (
        "alter", "and", "as", "asc", "begin", "between", "by", "case", "commit",
        "create", "delete", "desc", "distinct", "drop", "else", "end", "exists",
        "from", "group", "having", "in", "index", "inner", "insert", "into",
        "is", "join", "left", "like", "limit", "not", "null", "on", "or",
        "order", "outer", "primary", "references", "rollback", "select", "set",
        "table", "then", "union", "unique", "update", "values", "view", "when",
        "where", "with",
    ),
    "css": (),
    "json": (),
    "markup": (),
}

#: Literal values, coloured apart from keywords because they are what somebody
#: scanning a config file is usually looking for.
CONSTANTS = ("true", "false", "null", "none", "nil", "undefined", "True",
             "False", "None", "NULL", "NaN")

#: Single-line comment openers, per grammar.
COMMENTS: dict[str, tuple[str, ...]] = {
    "c": (r"//[^\n]*",),
    "hash": (r"#[^\n]*",),
    "sql": (r"--[^\n]*",),
    "css": (),
    "markup": (),
    "json": (),
}

#: (opener, closer) for comments that span lines. These cannot be ordinary
#: rules: a regex is applied one block at a time, so `/*` on line one has to
#: colour line two through a state machine rather than through a match.
BLOCK_COMMENTS: dict[str, tuple[str, str]] = {
    "c": (r"/\*", r"\*/"),
    "css": (r"/\*", r"\*/"),
    "sql": (r"/\*", r"\*/"),
    "markup": (r"<!--", r"-->"),
}

#: Escape-aware and newline-bounded, so `"a\"b"` is one string and an unclosed
#: quote colours one line rather than the rest of the file.
STRINGS = (
    r'"(?:[^"\\\n]|\\.)*"',
    r"'(?:[^'\\\n]|\\.)*'",
)
BACKTICK = r"`(?:[^`\\]|\\.)*`"

NUMBER = r"\b(?:0[xXbBoO][0-9a-fA-F_]+|\d[\d_]*(?:\.\d+)?(?:[eE][+-]?\d+)?)\b"


def language_for(ext: str) -> Optional[str]:
    """The grammar for an extension, or None to leave the text alone."""
    return LANGUAGES.get((ext or "").lower().lstrip("."))


def patterns_for(language: Optional[str]) -> list[tuple[str, str]]:
    """`(pattern, role)` pairs in **priority** order, highest first.

    **Priority, not paint order, and the difference is the whole correctness
    argument.** Layering rules and letting the last one win cannot express what
    is actually wanted, because the two requirements point opposite ways: a
    keyword inside a string must lose to the string, and `//` inside a string
    must lose to the string too - but under last-wins, whichever of `string`
    and `comment` is applied second wins everywhere, including where it is
    wrong. `url = "http://x"` came out as a comment, and there is a test for it.

    So these are scanned **once, left to right, first match wins** - see
    `combined`. At the `"` the string alternative matches and consumes the
    whole literal, `//` inside it is never reached, and a `#` swallows the rest
    of its line before any keyword on that line is examined. Tokenising rather
    than layering, which is what a highlighter has to do.
    """
    if language is None:
        return []

    rules: list[tuple[str, str]] = []

    # Comments outrank everything: inside one, nothing else is what it says.
    for pattern in COMMENTS.get(language, ()):
        rules.append((pattern, "comment"))

    # A JSON key is a string in a particular position, so it has to be offered
    # before the general string rule or it never matches.
    if language == "json":
        rules.append((r'"(?:[^"\\]|\\.)*"(?=\s*:)', "keyword"))

    for pattern in STRINGS:
        rules.append((pattern, "string"))
    if language in ("c", "hash", "markup"):
        rules.append((BACKTICK, "string"))

    rules.append((NUMBER, "number"))
    rules.append((r"\b(?:%s)\b" % "|".join(map(re.escape, CONSTANTS)), "constant"))

    words = KEYWORDS.get(language, ())
    if words:
        rules.append((r"\b(?:%s)\b" % "|".join(map(re.escape, words)), "keyword"))

    if language == "markup":
        # The tag name, not the whole tag: the attributes are the part somebody
        # is usually reading, and colouring everything colours nothing.
        rules.append((r"</?\s*[A-Za-z_][\w:.-]*", "keyword"))
    elif language == "css":
        rules.append((r"[-a-zA-Z]+(?=\s*:)", "constant"))
        rules.append((r"^[^{}\n]+(?=\{)", "keyword"))

    return rules


def combined(language: Optional[str]) -> tuple[str, list[str]]:
    """One alternation over every rule, plus the role each group carries.

    Returns `(pattern, roles)` where group *n+1* of a match belongs to
    `roles[n]`. One scan, leftmost-first, so priority is expressed by position
    in the alternation and no span is ever painted twice.

    Group names are positional (`g0`, `g1`) rather than role names because two
    rules can share a role - a grammar has several string forms - and duplicate
    group names are a compile error in every engine involved.
    """
    rules = patterns_for(language)
    if not rules:
        return "", []
    pattern = "|".join(
        f"(?P<g{index}>{expression})" for index, (expression, _role) in enumerate(rules)
    )
    return pattern, [role for _expression, role in rules]
