#!/usr/bin/env python3
"""Entry point for the auto-fix plugin.

Kept tiny: it only makes the sibling package importable, so the script can be
run directly from a plugin checkout (`python3 scripts/autofix.py …`) in CI,
without an install step.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from autofix.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
