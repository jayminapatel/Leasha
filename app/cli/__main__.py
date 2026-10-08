"""`python -m app.cli` - what `leasha.cmd` and the installer run.

Layer: L0

Kept to three lines on purpose: `leasha-cli.exe` (the packaged build) reaches
this through `runpy.run_module("app.cli", run_name="__main__")`, so it is the
one entry `python -m app.cli` and the installer share. The logic lives in
`app.cli.main`, where a test can call it without starting a process.
"""

import sys

from app.cli import main

if __name__ == "__main__":
    sys.exit(main())
