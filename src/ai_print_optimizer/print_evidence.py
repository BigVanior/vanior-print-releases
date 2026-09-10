"""Physical A/B print evidence stored independently of application versions."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class PrintEvidenceError(RuntimeError):
    """Raised when physical comparison evidence cannot be validated."""


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_photos(
    photos: Iterable[str | Path],
    destination: Path,
) -> tuple[dict[str, Any], ...]:
    destination.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    for supplied in photos:
        source = Path(supplied).expanduser().resolve()
        if not source.is_file() or source.suffix.casefold() not in {".jpg", ".jpeg", ".png", ".webp"}:
            raise PrintEvidenceError(f"unsupported or missing print photo: {source}")
        sha256 = _digest(source)
        target = destination / f"{sha256[:24]}{source.suffix.casefold()}"
        if not target.exists():
            temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
            try:
                shutil.copyfile(source, temporary)
                if _digest(temporary) != sha256:
                    raise PrintEvidenceError("physical print photo copy failed verification")
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        records.append(
            {
                "path": str(target),
                "sha256": sha256,
                "size_bytes": target.stat().st_size,
            }
        )
    return tuple(records)


@dataclass(frozen=True)
class PhysicalPrintComparison:
    comparison_id: str
    project_id: str
    source_name: str
    reference_photos: tuple[str | Path, ...]
    vanior_photos: tuple[str | Path, ...]
    observations: dict[str, Any]
    source_settings: dict[str, Any]
    vanior_settings: dict[str, Any]


def record_physical_comparison(
    learning_directory: str | Path,
    comparison: PhysicalPrintComparison,
) -> dict[str, Any]:
    """Idempotently persist paired author/VANIOR photos and observations."""
    root = Path(learning_directory).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    comparison_id = "".join(
        char for char in comparison.comparison_id if char.isalnum() or char in {"-", "_"}
    )[:100]
    if not comparison_id:
        raise PrintEvidenceError("comparison id is empty")
    photo_root = root / "physical_print_photos" / comparison_id
    reference = _copy_photos(comparison.reference_photos, photo_root / "author-reference")
    result = _copy_photos(comparison.vanior_photos, photo_root / "vanior-result")
    document = {
        "schema": "vanior-physical-print-comparison-v1",
        "comparison_id": comparison_id,
        "project_id": comparison.project_id,
        "source_name": comparison.source_name,
        "created_utc": datetime.now(UTC).isoformat(),
        "reference_photos": list(reference),
        "vanior_photos": list(result),
        "observations": dict(comparison.observations),
        "source_settings": dict(comparison.source_settings),
        "vanior_settings": dict(comparison.vanior_settings),
    }
    store = root / "physical-comparisons-v1.jsonl"
    existing: list[str] = []
    if store.exists():
        existing = store.read_text(encoding="utf-8").splitlines()
        for line in existing:
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict) and item.get("comparison_id") == comparison_id:
                return item
    with store.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return document
