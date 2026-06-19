"""Pytest configuration — make the in-tree package importable."""

import sys
from pathlib import Path

# Add src/ to sys.path so tests can `import autoxpost` without installing.
ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
