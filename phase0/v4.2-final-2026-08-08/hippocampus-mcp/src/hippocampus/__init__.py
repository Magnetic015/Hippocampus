import sys
from pathlib import Path

# scanner/ lives beside hippocampus-mcp/ in the Phase 0 workspace (plan 13.2)
_SCANNER_DIR = Path(__file__).resolve().parents[3] / "scanner"
if str(_SCANNER_DIR) not in sys.path:
    sys.path.insert(0, str(_SCANNER_DIR))
