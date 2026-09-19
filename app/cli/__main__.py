"""`python -m app.cli` - what `leasha.cmd` and the installer run."""

import sys

from app.cli import main

if __name__ == "__main__":
    sys.exit(main())
