"""Publish one verified user-facing 3MF without diagnostic clutter."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from .input_safety import UnsafeInputError, validate_zip_archive


class DeliveryError(RuntimeError):
    """Raised when the compact user-facing result cannot be published safely."""


def final_project_name(source: str | Path) -> str:
    """Return a readable and bounded result filename derived from the model."""
    source_path = Path(source)
    stem = source_path.stem.strip(" .") or "model"
    return f"{stem[:120]}_готово.3mf"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _require_embedded_gcode(project: Path) -> None:
    try:
        with ZipFile(project) as archive:
            validate_zip_archive(archive)
            names = archive.namelist()
    except (BadZipFile, OSError, UnsafeInputError) as exc:
        raise DeliveryError(f"ready project is not a readable 3MF: {project}") from exc
    if not any(
        name.casefold().startswith("metadata/plate_")
        and name.casefold().endswith(".gcode")
        for name in names
    ):
        raise DeliveryError("ready 3MF does not contain embedded plate G-code")


def publish_single_3mf(
    ready_project: str | Path,
    output: str | Path,
    source_model: str | Path,
) -> Path:
    """Atomically publish a directory containing exactly one ready-to-print 3MF."""
    project = Path(ready_project).expanduser().resolve()
    output_dir = Path(output).expanduser().resolve()
    if project.suffix.casefold() != ".3mf" or not project.is_file():
        raise DeliveryError(f"verified ready 3MF not found: {project}")
    if output_dir.exists():
        raise DeliveryError(f"result output already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise DeliveryError(f"result output parent not found: {output_dir.parent}")
    _require_embedded_gcode(project)

    staging = Path(
        tempfile.mkdtemp(
            prefix=f".{output_dir.name}-publish-",
            dir=output_dir.parent,
        )
    )
    try:
        destination = staging / final_project_name(source_model)
        shutil.copy2(project, destination)
        if _sha256(project) != _sha256(destination):
            raise DeliveryError("published 3MF failed SHA-256 verification")
        staging.rename(output_dir)
        return output_dir / destination.name
    except Exception:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        raise
