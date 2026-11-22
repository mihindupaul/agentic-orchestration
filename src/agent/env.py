"""Read KEY=value lines from a `.env` file into the environment, without a new dependency.

Secrets such as ANTHROPIC_API_KEY live in `.env` at the repository root (ignored by git; `.env.example` shows
the names). A variable already set in the environment wins over the file, so a key exported in the shell or
set by CI is never overridden.
"""

from __future__ import annotations

import os
from pathlib import Path


def load_env(start: Path | None = None) -> Path | None:
    """Load the nearest `.env` at or above `start` (default: the current directory). Returns its path."""
    for folder in [(start or Path.cwd()).resolve(), *(start or Path.cwd()).resolve().parents]:
        path = folder / ".env"
        if path.is_file():
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("'\""))
            return path
    return None
