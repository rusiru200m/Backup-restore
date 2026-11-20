"""Controller paths stay stable regardless of the launch directory."""
from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def state_path(environment_name: str, default: str) -> Path:
    value = os.environ.get(environment_name, default)
    if not value.strip():
        raise ValueError(f"{environment_name} must be a nonempty file or directory path")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{environment_name} must not contain control characters")
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def positive_integer_setting(environment_name: str, default: str) -> int:
    """Validate numeric configuration before starting a remote operation."""
    value = os.environ.get(environment_name, default)
    try:
        number = int(value)
    except ValueError:
        raise ValueError(f"{environment_name} must be a positive whole number in .env") from None
    if number <= 0:
        raise ValueError(f"{environment_name} must be a positive whole number in .env")
    return number
