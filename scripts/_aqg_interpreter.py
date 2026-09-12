"""Choose the absolute interpreter recorded in host-owned hook commands."""
from __future__ import annotations

import os
import sys
from pathlib import Path


def hook_interpreter() -> str:
    """Return argv[0] for a managed host hook command."""
    value = sys.executable
    path = Path(value) if value else None
    if path is None or not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
        raise RuntimeError('cannot install managed hooks without an absolute executable Python path')
    return value
