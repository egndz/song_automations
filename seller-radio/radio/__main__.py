"""Module entry point so that "python -m radio" runs the command line interface."""

from __future__ import annotations

import sys

from radio.cli import main

if __name__ == "__main__":
    sys.exit(main())
