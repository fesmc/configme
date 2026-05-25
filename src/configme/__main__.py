"""Allow `python -m configme` to behave like the `configme` command."""

import sys

from configme.cli import main

if __name__ == "__main__":
    sys.exit(main())
