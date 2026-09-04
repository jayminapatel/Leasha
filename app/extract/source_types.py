r"""Every source, script and configuration file type this application reads.

Layer: L2

Asked for: *"for the extensions do the research and add all types of code files
from microsoft, oracle etc, and others which are stored and configure them and
select by default."*

**All of these are plain text, and that is the whole reason the list can be
this long.** There is no parser here and no dependency: a `.pkb` and a `.csproj`
and an `.rpgle` are read exactly as a `.txt` is, so the cost of adding one is a
line in a set. What the list buys is the difference between "your code is
indexed" and "the twelve languages somebody happened to think of are indexed" -
and the second is indistinguishable from the first until the day you search for
something in a `.cbl` and get nothing.

**Grouped, and each group is a decision.** A flat set of three hundred
extensions is unreviewable: nobody can tell whether `.pkb` is missing or whether
`.dat` should never have been there. Grouped by ecosystem, each group can be
read by somebody who knows that ecosystem and corrected.

---------------------------------------------------------------------------
What is deliberately absent, and why
---------------------------------------------------------------------------

**Anything usually binary.** `.dat`, `.idx`, `.db`, `.bin`, `.dll`, `.pdb`,
`.fmb`, `.rdf`, `.acd`, `.mlx`, `.msapp`, `.accdb`. The plaintext reader has a
NUL sniff and reports `ERR_NO_TEXT_LAYER` for a file that turns out to be
binary, so a mistake here fails loudly rather than filling the index with
rubbish - but a type that is *usually* binary should not be routed here at all,
because the honest report would then be the common case.

**Generated output.** `.map` (source maps), `.lock`, `.sum`, `.min.js`. Real
text, no information a person searches for, and enormous. Indexing them makes
the results worse rather than more complete.

**Filenames without an extension** - `Makefile`, `Dockerfile`, `CMakeLists.txt`,
`.gitignore` - are matched by *name* rather than suffix, which routing by
extension cannot express. `NAMED_FILES` below carries them, and
`base.extractor_for` checks it; see the note there.

**Secrets.** `.env`, `.tfstate` and `.ora` can hold credentials, and they are
indexed anyway. That is deliberate and consistent: the index lives on this
machine, never leaves it, and a search tool that silently skipped the files
somebody most needs to find would be lying about its coverage. `diagnose`
redacts them; the index does not contain them any less than the disk does.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "ALL_SOURCE_EXTENSIONS",
    "BY_ECOSYSTEM",
    "NAMED_FILES",
    "indexed_ext",
]


# ---------------------------------------------------------------------------
# Microsoft
# ---------------------------------------------------------------------------

#: C#, VB.NET, F#, C++ and the project system around them.
#:
#: The project and build files matter as much as the sources: `.csproj` holds
#: the package versions and the file list, `.props` and `.targets` hold the
#: build rules a team argues about, and "which project references that" is a
#: question people ask constantly and cannot answer from the code alone.
DOTNET = frozenset({
    ".cs", ".csx", ".vb", ".vbs", ".fs", ".fsi", ".fsx", ".fsscript",
    ".cpp", ".cc", ".cxx", ".c", ".h", ".hpp", ".hxx", ".hh", ".inl", ".ipp",
    # Project, solution and build
    ".csproj", ".vbproj", ".fsproj", ".vcxproj", ".vcproj", ".shproj",
    ".sqlproj", ".wixproj", ".njsproj", ".pyproj", ".dcproj", ".esproj",
    ".proj", ".msbuild", ".props", ".targets", ".nuspec", ".pubxml",
    ".sln", ".slnf", ".slnx", ".filters", ".user", ".rsp",
    ".runsettings", ".ruleset", ".editorconfig",
    # Configuration, resources and markup
    ".config", ".settings", ".resx", ".resw", ".xaml", ".axaml",
    ".manifest", ".rc", ".rc2", ".def", ".idl", ".odl",
    # VBA and VB6 module exports
    ".bas", ".cls", ".frm", ".ctl",
})

#: PowerShell, batch and Windows Script Host.
POWERSHELL = frozenset({
    ".ps1", ".psm1", ".psd1", ".ps1xml", ".pssc", ".psrc", ".cdxml",
    ".bat", ".cmd", ".wsf", ".wsh", ".hta",
})

#: SQL Server and the Microsoft BI stack. All of these are XML or SQL on disk.
#:
#: `.dtsx` (SSIS packages) and `.rdl` (SSRS reports) are the ones worth having:
#: a package's connection strings, expressions and SQL live inside the XML, and
#: "which package writes that table" is otherwise unanswerable without opening
#: each one in Visual Studio.
MSSQL = frozenset({
    ".sql", ".dtsx", ".dtproj", ".rdl", ".rdlc", ".rptproj", ".smdl",
    ".mdx", ".dax", ".bim", ".asdatabase", ".xmla",
})

#: Azure, Windows installers and packaging.
MS_PLATFORM = frozenset({
    ".bicep", ".bicepparam",
    ".reg", ".inf",
    ".wxs", ".wxi", ".wxl",          # WiX
    ".nsi", ".nsh",                  # NSIS
    ".iss",                          # Inno Setup
    ".al",                           # Dynamics 365 Business Central
    ".xlf", ".xliff",                # translation units
})


# ---------------------------------------------------------------------------
# Oracle
# ---------------------------------------------------------------------------

#: PL/SQL, as SQL Developer and Toad write it out.
#:
#: **The package split is the important part.** A package's specification
#: (`.pks`) and body (`.pkb`) are separate files, and a shop that keeps them
#: that way had *none* of its stored procedure code indexed while only `.sql`
#: was read - which on an Oracle site is most of the logic in the system.
ORACLE_PLSQL = frozenset({
    ".pls", ".plb", ".pks", ".pkb", ".prc", ".fnc", ".trg", ".vw",
    ".tps", ".tpb", ".spc", ".bdy", ".typ", ".tab", ".seq", ".syn",
    ".pck", ".pkg", ".sps", ".sbd",
})

#: Oracle tooling, E-Business Suite and the database's own text files.
#:
#: `.ctl` is SQL*Loader's control file and `.ldt`/`.lct` are FNDLOAD's - the
#: places an EBS customisation is actually defined. `.ora` is `tnsnames.ora`
#: and friends: how this machine reaches which database, which is exactly the
#: thing somebody hunts for when a connection breaks.
ORACLE_TOOLS = frozenset({
    ".ora", ".ctl", ".ldt", ".lct", ".sqlplus",
    ".pld",                          # Forms library, text export
    ".fmt", ".mmt", ".rex",          # Forms/Menu/Reports text exports
    ".trc",                          # trace files - text, and often the answer
    ".jsff", ".amx",                 # ADF / MAF fragments
})


# ---------------------------------------------------------------------------
# IBM and the mainframe
# ---------------------------------------------------------------------------

#: COBOL, RPG, JCL, PL/I and REXX. Still running most of what pays for things.
#:
#: `.cpy` (copybooks) is the one people forget: the record layouts live there,
#: so "which programs use that field" fails without it even when every `.cbl`
#: is indexed.
MAINFRAME = frozenset({
    ".cbl", ".cob", ".cobol", ".cpy", ".ccp",
    ".rpg", ".rpgle", ".sqlrpgle", ".rpgmod", ".clle", ".clp",
    ".jcl", ".pli", ".pl1", ".rexx", ".mac", ".hlasm",
})


# ---------------------------------------------------------------------------
# Java and the JVM
# ---------------------------------------------------------------------------

JVM = frozenset({
    ".java", ".jav", ".jsp", ".jspx", ".jspf", ".tag", ".tld",
    ".scala", ".sc", ".kt", ".kts", ".groovy", ".gvy", ".gradle",
    ".clj", ".cljs", ".cljc", ".edn", ".aj",
    ".ftl", ".vm",                   # FreeMarker, Velocity
})


# ---------------------------------------------------------------------------
# SAP
# ---------------------------------------------------------------------------

#: ABAP and the CAP/CDS model files. Relevant here beyond the general case:
#: this application is used in plants where SAP is the system of record.
SAP = frozenset({".abap", ".cds", ".asddls", ".behdef"})


# ---------------------------------------------------------------------------
# Databases other than Oracle and SQL Server
# ---------------------------------------------------------------------------

DATABASES = frozenset({
    ".pgsql", ".psql", ".pgc",       # PostgreSQL
    ".db2", ".ddl", ".dml",
    ".cql", ".cypher",               # Cassandra, Neo4j
    ".bteq",                         # Teradata
    ".hql",                          # Hive
    ".4gl", ".per",                  # Informix
    ".prql", ".sparql", ".rq",
})


# ---------------------------------------------------------------------------
# Web
# ---------------------------------------------------------------------------

WEB = frozenset({
    ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".mts", ".cts",
    ".coffee", ".vue", ".svelte", ".astro",
    ".css", ".scss", ".sass", ".less", ".styl", ".pcss",
    ".html", ".htm", ".xhtml", ".shtml",
    ".hbs", ".handlebars", ".mustache", ".ejs", ".pug", ".jade",
    ".haml", ".slim", ".liquid", ".njk", ".twig", ".phtml",
    ".json5", ".jsonc", ".webmanifest",
    ".graphql", ".gql",
    ".asp", ".aspx", ".ascx", ".asmx", ".ashx", ".asax", ".master",
    ".cshtml", ".vbhtml", ".razor", ".erb",
})


# ---------------------------------------------------------------------------
# Scripting, systems and everything else with a compiler
# ---------------------------------------------------------------------------

LANGUAGES = frozenset({
    ".py", ".pyi", ".pyw", ".pyx", ".pxd", ".pxi", ".ipynb",
    ".rb", ".rake", ".gemspec", ".ru",
    ".pl", ".pm", ".pod", ".t",
    ".php", ".lua", ".tcl", ".tk", ".awk",
    ".go", ".rs", ".swift", ".m", ".mm", ".dart",
    ".hs", ".lhs", ".erl", ".hrl", ".ex", ".exs",
    ".ml", ".mli", ".nim", ".zig", ".cr", ".d", ".v",
    ".adb", ".ads",                  # Ada
    ".f", ".f77", ".f90", ".f95", ".f03", ".for", ".ftn",
    ".pas", ".dpr", ".dpk", ".dfm", ".lpr", ".inc",
    ".asm", ".s",
    ".scm", ".ss", ".lisp", ".lsp", ".el", ".prolog",
    ".sol",                          # Solidity
    ".sh", ".bash", ".zsh", ".ksh", ".csh", ".fish",
})

#: Statistics and scientific computing - the tools an engineering business has
#: as much of as it has application code.
ANALYTICS = frozenset({
    ".r", ".rmd", ".rnw", ".qmd",    # R, R Markdown, Quarto
    ".jl",                           # Julia
    ".sas",                          # SAS
    ".do", ".ado",                   # Stata
    ".sps",                          # SPSS syntax (`.spv` output is binary)
})


# ---------------------------------------------------------------------------
# Industrial control and hardware description
# ---------------------------------------------------------------------------

#: PLC, SCADA and HDL sources.
#:
#: **Included because of what this application is for.** The owner's work is
#: MES and plant systems, and a plant's logic lives in `.st`, `.scl`, `.awl`
#: and `.l5x` - none of which any general-purpose indexer would think to
#: include, and all of which are plain text. `.l5x` is Rockwell's Logix export:
#: XML, with the rung comments and tag descriptions in it, which is where the
#: process knowledge actually is.
AUTOMATION = frozenset({
    ".st", ".scl", ".awl", ".exp",   # Structured Text, Siemens SCL/STL, CODESYS
    ".l5x", ".l5k",                  # Rockwell Logix export
    ".vhd", ".vhdl", ".sv", ".svh", ".vh",
})


# ---------------------------------------------------------------------------
# Infrastructure and build
# ---------------------------------------------------------------------------

INFRASTRUCTURE = frozenset({
    ".tf", ".tfvars", ".tfstate", ".hcl", ".nomad",
    ".pp",                           # Puppet
    ".sls",                          # Salt
    ".mk", ".mak", ".am", ".ac", ".m4",
    ".cmake", ".bzl", ".bazel",
    ".dockerfile", ".containerfile",
    ".service", ".timer", ".socket", ".mount",   # systemd units
    ".env", ".conf", ".properties",
    ".gradle", ".sbt",
})


# ---------------------------------------------------------------------------
# Documentation and data that sits beside code
# ---------------------------------------------------------------------------

DOCS_AND_DATA = frozenset({
    ".txt", ".md", ".markdown", ".mdx", ".rst", ".adoc", ".org",
    ".tex", ".bib", ".ltx", ".sty", ".cls",
    ".log", ".csv", ".tsv", ".psv",
    ".json", ".jsonl", ".ndjson", ".yaml", ".yml", ".xml", ".xsd", ".xsl",
    ".xslt", ".dtd", ".wsdl",
    ".ini", ".cfg", ".toml",
    ".srt", ".vtt", ".ics", ".vcf",
    ".diff", ".patch",
    ".proto", ".thrift", ".avsc",    # interface definitions
    ".http", ".rest",                # request collections
})


#: Every group, by name, for `app.cli formats --groups` and for the tests.
BY_ECOSYSTEM: dict[str, frozenset[str]] = {
    "Microsoft .NET and C++": DOTNET,
    "PowerShell and Windows scripting": POWERSHELL,
    "SQL Server and Microsoft BI": MSSQL,
    "Azure, installers and Dynamics": MS_PLATFORM,
    "Oracle PL/SQL": ORACLE_PLSQL,
    "Oracle tooling and E-Business Suite": ORACLE_TOOLS,
    "IBM mainframe and IBM i": MAINFRAME,
    "Java and the JVM": JVM,
    "SAP": SAP,
    "Other databases": DATABASES,
    "Web": WEB,
    "Languages and shells": LANGUAGES,
    "Statistics and scientific computing": ANALYTICS,
    "Industrial control and hardware description": AUTOMATION,
    "Infrastructure and build": INFRASTRUCTURE,
    "Documentation and data": DOCS_AND_DATA,
}

#: The union. What `PlainTextExtractor` claims.
ALL_SOURCE_EXTENSIONS: frozenset[str] = frozenset().union(*BY_ECOSYSTEM.values())


#: Files whose *name* is the type - they have no extension to route on.
#:
#: `Path("Makefile").suffix` is `""`, so extension routing cannot see any of
#: these, and a repository indexed without them is missing the file that says
#: how it is built. Matched case-insensitively on the whole name.
#:
#: Dotfiles are here for the same reason and a subtler one:
#: `Path(".gitignore").suffix` is `""` too - Python treats a leading dot as the
#: start of the stem, not as an extension separator. A rule listing
#: `".gitignore"` as an extension would therefore never match anything, which
#: is precisely the sort of entry that looks correct in a config file for years.
NAMED_FILES: frozenset[str] = frozenset({
    "makefile", "gnumakefile", "dockerfile", "containerfile", "vagrantfile",
    "jenkinsfile", "rakefile", "gemfile", "podfile", "brewfile", "procfile",
    "cmakelists.txt", "build", "workspace", "meson.build", "sconstruct",
    # `go.mod` declares dependencies and is worth searching. `go.sum` and
    # `Cargo.lock` are checksum lists - the same "generated output" excluded
    # above by extension, and it would be inconsistent to admit them here.
    "go.mod",
    ".gitignore", ".gitattributes", ".gitmodules", ".gitconfig",
    ".editorconfig", ".dockerignore", ".npmrc", ".nvmrc", ".babelrc",
    ".eslintrc", ".prettierrc", ".stylelintrc", ".flake8", ".pylintrc",
    ".bashrc", ".bash_profile", ".zshrc", ".profile", ".vimrc",
})


def indexed_ext(path: Any) -> str:
    r"""What `files.ext` should hold for this file, without the leading dot.

    Ordinarily the suffix. **For a named file it is the name**, and that is the
    whole of this function.

    `NAMED_FILES` made `Dockerfile`, `Makefile` and the dotfiles indexable, and
    in doing so made them unfilterable: `files.ext` was `''` for every one of
    them, `distinct_values` filters `WHERE ext <> ''`, and `type:` matches on
    that column - so they were in the index, searchable by content, and could
    not be narrowed to, offered in the `/type` menu, or named in a query at
    all. A gap created by fixing something else, which is the usual way.

    Storing the name is the cheapest of the three options and the only one that
    also fixes the *menu*: `makefile` and `dockerfile` arrive through
    `distinct_values` like any other type, ordered by how many the corpus
    actually holds, rather than needing a hand-maintained group that would
    drift from `NAMED_FILES` within a month.

    A leading dot is stripped so `.gitignore` is filtered as `gitignore` -
    consistent with every other value in the column, none of which carry one,
    and with what somebody would type.

    **Existing rows keep their empty `ext` until the file is re-indexed.** It
    is a derived column and the next pass over an unchanged file does not
    rewrite it, so `app.cli index --force` is what backfills a corpus indexed
    before this. Said out loud because "the setting did not work" is what this
    otherwise looks like.
    """
    #: **Split on both separators, not `Path.name`.** These paths are written
    #: on Windows and this runs anywhere: `PurePosixPath(r"D:\Repo\Dockerfile")`
    #: has a `name` of the whole string, so the membership test below silently
    #: never matches off Windows and every test of this passes for the wrong
    #: reason. The fifth time this project has been caught by that; see
    #: `walker.own_paths` and `sqlite_store._basename`.
    text = str(path).replace("\\", "/").rstrip("/")
    name = text.rpartition("/")[2].lower()

    stem, dot, suffix = name.rpartition(".")
    if dot and stem:                     # a real extension, not a leading dot
        return suffix
    # Only names this application deliberately indexes. Every extensionless
    # file the walker admits is one of these, but the check is cheap and it
    # keeps a stray `ext` out of the column if that ever stops being true.
    return name.lstrip(".") if name in NAMED_FILES else ""
