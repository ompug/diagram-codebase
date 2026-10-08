"""Small shared helpers: atomic JSON IO, time, ids, hashing."""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


class DCError(Exception):
    """User-facing error with a clear message (no traceback needed)."""


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        if default is not None:
            return default
        raise
    except json.JSONDecodeError as exc:
        raise DCError(f"{path} is not valid JSON: {exc}") from exc


def write_json(path: Path, data: Any) -> None:
    """Write JSON atomically (temp file + rename) so a crash never leaves half a file."""
    write_text(path, json.dumps(data, indent=2, sort_keys=False, ensure_ascii=False) + "\n")


def write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slug(text: str, sep: str = "-") -> str:
    """Lowercase, ascii-ish identifier fragment. Stable for the same input."""
    s = _SLUG_RE.sub(sep, text.lower()).strip(sep)
    return s or "x"


def stable_id(*parts: str) -> str:
    """Model identifiers: 'kind:path-ish' strings built from stable parts."""
    return ":".join(p for p in parts if p)


def rel_posix(path: Path, root: Path) -> str:
    return Path(os.path.relpath(path, root)).as_posix()
