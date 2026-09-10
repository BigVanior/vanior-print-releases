"""Stable VANIOR PRINT workspace shared by every installed application version."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .input_safety import UnsafeInputError, file_identity, validate_model_file

WORKSPACE_ENV = "VANIOR_PRINT_HOME"
PERSONAL_MODE_ENV = "VANIOR_PRINT_PERSONAL_MODE"
DOWNLOADS_ENV = "VANIOR_PRINT_DOWNLOADS"
WORKSPACE_NAME = "VANIOR PRINT"
UPLOADS_NAME = "1. Загружаемые файлы"
READY_NAME = "2. Готов к печати"
BACKUPS_NAME = "3. Бэкап приложения"
LEARNING_NAME = "4. Файлы для обучения"
SUPPORTED_MODEL_SUFFIXES = {".stl", ".3mf"}


@dataclass(frozen=True)
class WorkspaceLayout:
    root: Path
    uploads: Path
    ready: Path
    backups: Path
    learning: Path
    system: Path
    history: Path
    print_dna: Path
    global_print_dna: Path
    learning_events: Path
    physical_comparisons: Path
    personal_mode: bool
    move_imports: bool


def _default_root() -> Path:
    override = os.environ.get(WORKSPACE_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        if (
            executable_root.name.casefold() == WORKSPACE_NAME.casefold()
            and executable_root.drive.casefold() == "s:"
        ):
            return executable_root
    local = Path(os.environ.get("LOCALAPPDATA", Path.home()))
    return (local / WORKSPACE_NAME).resolve()


def _downloads_root() -> Path:
    override = os.environ.get(DOWNLOADS_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    profile = Path(os.environ.get("USERPROFILE", Path.home())).expanduser().resolve()
    downloads = profile / "Downloads"
    return downloads if downloads.is_dir() else Path.home().resolve() / "Downloads"


def _is_personal_mode(resolved: Path, *, explicit_root: bool) -> bool:
    configured = os.environ.get(PERSONAL_MODE_ENV, "").strip().casefold()
    if configured:
        return configured in {"1", "true", "yes", "on", "personal"}
    # Explicit workspaces are used by the owner's installation, tests and
    # portable deployments. Public installers do not set an override.
    if explicit_root or os.environ.get(WORKSPACE_ENV, "").strip():
        return True
    return resolved.drive.casefold() == "s:"


def ensure_workspace(
    root: str | Path | None = None,
    *,
    personal_mode: bool | None = None,
) -> WorkspaceLayout:
    resolved = Path(root).expanduser().resolve() if root is not None else _default_root()
    private = (
        _is_personal_mode(resolved, explicit_root=root is not None)
        if personal_mode is None
        else bool(personal_mode)
    )
    if private:
        uploads = resolved / UPLOADS_NAME
        ready = resolved / READY_NAME
        backups = resolved / BACKUPS_NAME
        learning = resolved / LEARNING_NAME
        system = backups / "Рабочие данные"
    else:
        uploads = resolved / "Cache" / "Imports"
        ready = _downloads_root() / WORKSPACE_NAME
        backups = resolved / "Backups"
        learning = resolved / "Learning"
        system = resolved / "System"
    for directory in (resolved, uploads, ready, backups, learning, system):
        directory.mkdir(parents=True, exist_ok=True)
    layout = WorkspaceLayout(
        root=resolved,
        uploads=uploads,
        ready=ready,
        backups=backups,
        learning=learning,
        system=system,
        history=learning / "project-history.json",
        print_dna=learning / "print-dna-v2.json",
        global_print_dna=learning / "global-print-dna-v2.json",
        learning_events=learning / "learning-events-v1.jsonl",
        physical_comparisons=learning / "physical-comparisons-v1.jsonl",
        personal_mode=private,
        move_imports=private,
    )
    schema_path = learning / "learning-store.json"
    if not schema_path.exists():
        temporary = learning / f".learning-store.{uuid.uuid4().hex}.tmp"
        temporary.write_text(
            json.dumps(
                {
                    "schema": "vanior-learning-store-v1",
                    "scope": "local-user-shared-across-versions",
                    "events": layout.learning_events.name,
                    "print_dna": layout.print_dna.name,
                    "physical_comparisons": layout.physical_comparisons.name,
                    "raw_models_included": False,
                    "cloud_sync_enabled": False,
                    "personal_workspace_mode": private,
                    "description": (
                        "Version-independent local learning store. Future sync must be "
                        "explicitly enabled and must remove personal paths and raw geometry."
                    ),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        try:
            os.replace(temporary, schema_path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        try:
            payload = json.loads(schema_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict) and "physical_comparisons" not in payload:
                payload["physical_comparisons"] = layout.physical_comparisons.name
                temporary = learning / f".learning-store.{uuid.uuid4().hex}.tmp"
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                try:
                    os.replace(temporary, schema_path)
                finally:
                    temporary.unlink(missing_ok=True)
        except (OSError, json.JSONDecodeError):
            pass
    return layout


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def import_model(source: str | Path, layout: WorkspaceLayout) -> Path:
    """Validate an STL/3MF and move it only in the owner's personal workspace."""
    supplied = Path(source).expanduser()
    if supplied.is_symlink():
        raise ValueError("symbolic links are not accepted as model uploads")
    original = supplied.resolve()
    if not original.is_file() or original.suffix.casefold() not in SUPPORTED_MODEL_SUFFIXES:
        raise ValueError("source must be an existing STL or 3MF file")
    if _is_inside(original, layout.uploads):
        validate_model_file(original)
        return original

    try:
        validate_model_file(original)
    except UnsafeInputError as exc:
        raise ValueError(f"unsafe or malformed model: {exc}") from exc
    if not layout.move_imports:
        # Public installations must never surprise users by moving or copying
        # their source model. The pipeline re-checks content fingerprints before
        # every destructive or publication boundary.
        return original
    original_identity = file_identity(original)
    source_hash = sha256_file(original)
    destination = layout.uploads / original.name
    if destination.is_file():
        if destination.stat().st_size == original.stat().st_size and sha256_file(destination) == source_hash:
            if file_identity(original) != original_identity or sha256_file(original) != source_hash:
                raise OSError("uploaded model changed while it was being imported")
            original.unlink()
            return destination
        destination = layout.uploads / f"{original.stem}-{source_hash[:8]}{original.suffix.lower()}"
        if destination.is_file() and sha256_file(destination) == source_hash:
            if file_identity(original) != original_identity or sha256_file(original) != source_hash:
                raise OSError("uploaded model changed while it was being imported")
            original.unlink()
            return destination

    temporary = layout.uploads / f".{destination.name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(original, temporary)
        if file_identity(original) != original_identity:
            raise OSError("uploaded model changed while it was being imported")
        if sha256_file(temporary) != source_hash:
            raise OSError("uploaded model copy failed SHA-256 verification")
        validate_model_file(temporary, format_suffix=original.suffix)
        os.replace(temporary, destination)
        try:
            original.unlink()
        except OSError:
            destination.unlink(missing_ok=True)
            raise
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", value).strip(" .-")
    return cleaned[:100] or "model"


def next_project_output(source: str | Path, layout: WorkspaceLayout) -> Path:
    stem = _safe_stem(Path(source).stem)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    base = layout.ready / "Проекты" / f"{stem}-project-{stamp}"
    base.parent.mkdir(parents=True, exist_ok=True)
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = base.with_name(f"{base.name}-{index}")
        if not candidate.exists():
            return candidate
    raise RuntimeError("cannot allocate a unique project output directory")


def next_ready_file(name: str, suffix: str, layout: WorkspaceLayout) -> Path:
    extension = suffix if suffix.startswith(".") else f".{suffix}"
    safe_name = _safe_stem(name)
    normalized_extension = extension.casefold()
    base = layout.ready / f"{safe_name}{normalized_extension}"
    if not base.exists():
        return base
    for index in range(2, 1000):
        candidate = layout.ready / f"{safe_name}-{index}{normalized_extension}"
        if not candidate.exists():
            return candidate
    raise RuntimeError("cannot allocate a unique ready file name")


def append_learning_event(
    layout: WorkspaceLayout,
    event_type: str,
    payload: dict[str, Any],
    *,
    application_version: str,
) -> None:
    """Append portable, version-independent local learning data as JSONL."""
    document = {
        "schema": "vanior-learning-event-v1",
        "created_utc": datetime.now(UTC).isoformat(),
        "application_version": application_version,
        "event_type": event_type,
        "payload": payload,
    }
    layout.learning_events.parent.mkdir(parents=True, exist_ok=True)
    with layout.learning_events.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
