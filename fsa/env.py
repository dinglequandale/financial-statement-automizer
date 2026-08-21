"""Load `.env` so the API key is present without the operator exporting it.

Deliberately stdlib-only rather than depending on `python-dotenv`: this reads
one flat `KEY=value` file and nothing more, and one less package to install is
one less thing to go wrong on a locked-down firm laptop.

Precedence is the usual way round -- a variable already in the real environment
wins, so `.env` is a default, never an override. That matters because the
packaged build will ship with a `.env` next to the executable and an analyst
setting a key in their own shell should be able to beat it.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Walk up at most this far looking for `.env`. Enough to find the repo root
#: from any working directory inside it, without wandering into C:\Users.
_MAX_DEPTH = 4


def find_dotenv(start: Path | None = None) -> Path | None:
    here = (start or Path.cwd()).resolve()
    for parent in [here, *here.parents][:_MAX_DEPTH]:
        candidate = parent / ".env"
        if candidate.is_file():
            return candidate
    # Alongside the executable/module, which is where a packaged build puts it.
    beside = Path(__file__).resolve().parent.parent / ".env"
    return beside if beside.is_file() else None


def load_dotenv(path: Path | None = None) -> list[str]:
    """Set any KEY=value not already in the environment. Returns the keys set."""
    # `find_dotenv` only ever returns a file that exists, but an explicit path
    # from a caller (or a packaged build's config) may not -- a missing .env is
    # a no-op, never a crash.
    path = Path(path) if path is not None else find_dotenv()
    if path is None or not path.is_file():
        return []

    applied: list[str] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        # Strip one matched pair of surrounding quotes; keep anything inside.
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            applied.append(key)
    return applied
