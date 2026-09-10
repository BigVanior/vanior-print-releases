"""Small atomic writers for derived reports and manifests."""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any


def _publish_new(path: str | Path, content: bytes) -> Path:
    target = Path(path).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"refusing to overwrite existing file: {target}")
    if not target.parent.is_dir():
        raise FileNotFoundError(f"output parent not found: {target.parent}")
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, target)
    finally:
        if temporary != target:
            temporary.unlink(missing_ok=True)
    return target


def atomic_write_new_bytes(path: str | Path, content: bytes) -> Path:
    """Atomically publish a new binary file without replacing an existing path."""
    return _publish_new(path, content)


def atomic_write_new_text(path: str | Path, content: str) -> Path:
    """Atomically publish a new UTF-8 file without replacing an existing path."""
    return _publish_new(path, content.encode("utf-8"))


def atomic_write_new_json(path: str | Path, payload: Any) -> Path:
    return atomic_write_new_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
    )
