"""Allows ``python -m release_monitor``."""

import sys

from release_monitor.cli import main

if __name__ == "__main__":
    sys.exit(main())
