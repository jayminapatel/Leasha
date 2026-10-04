"""`app.cli --version` - which version is this, from the command line. 2026-10-04.

Layer: L0

Help > About Leasha was added on 4 October; non-negotiable 8 says the command
line gets there first, and it had no way to say its version at all.
"""

from __future__ import annotations

import pytest

from app.cli import build_parser
from app.core.version import version


def test_the_command_line_says_its_version_and_stops(capsys):
    with pytest.raises(SystemExit) as stopped:
        build_parser().parse_args(["--version"])
    assert stopped.value.code == 0
    assert capsys.readouterr().out.strip() == f"Leasha {version()}"
