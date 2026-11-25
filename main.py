#!/usr/bin/env python3
"""Conventional project entry; delegates to :mod:`edge_gateway.cli`."""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from edge_gateway.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
