"""Put the repo's contracts/ + scripts/ dirs on sys.path so tests import
`ledger.*` (contracts/ledger/ is the neutral contract package) and the v2 hook
writer (`ledger_hook_writer`, which lives in scripts/)."""
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent
for _sub in ("contracts", "scripts"):
    _p = str(_ROOT / _sub)
    if _p not in sys.path:
        sys.path.insert(0, _p)
