"""Controller paths stay stable regardless of the launch directory."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def state_path(environment_name: str, default: str) -> Path:
    value = os.environ.get(environment_name, default)
    if not value.strip():
        raise ValueError(f"{environment_name} must be a nonempty file or directory path")
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path
