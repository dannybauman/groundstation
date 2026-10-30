#!/usr/bin/env python3
"""Build a field-test page from a cases JSON. The logic lives in the package.

    uv run scripts/field_test.py docs/field-test-2026-07-22.cases.json
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from groundstation.field_test import build  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: field_test.py <cases.json>")
    build(sys.argv[1])
