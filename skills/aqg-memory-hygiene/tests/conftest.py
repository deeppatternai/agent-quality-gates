"""Put the skill's scripts/ dir on sys.path so tests can import the entry script
(`aqg_memory_hygiene`) and the core module (`_mh_core`) directly, independent of
import order within a test file."""

from __future__ import annotations

import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))
