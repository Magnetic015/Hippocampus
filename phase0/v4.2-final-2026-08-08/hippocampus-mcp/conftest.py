import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for p in (_HERE / "src", _HERE.parent / "scanner", _HERE.parent / "hindsight-lab"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)
