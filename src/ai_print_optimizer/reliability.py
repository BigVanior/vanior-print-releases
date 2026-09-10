"""Crash recovery, verified slice cache, backups and support diagnostics."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .input_safety import read_zip_member, validate_zip_archive


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_replace_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class JobJournal:
    """Small atomic checkpoint that survives application or machine restarts."""

    def __init__(self, path: str | Path, job_id: str, request: dict[str, Any]) -> None:
        self.path = Path(path).expanduser().resolve()
        self.payload: dict[str, Any] = {
            "schema_version": 1,
            "job_id": job_id,
            "status": "running",
            "request": request,
            "stages": [],
            "created_utc": datetime.now(UTC).isoformat(),
            "updated_utc": datetime.now(UTC).isoformat(),
        }
        _atomic_replace_json(self.path, self.payload)

    def checkpoint(self, stage: str, message: str, percent: int) -> None:
        self.payload["stages"].append(
            {
                "stage": stage,
                "message": message,
                "percent": max(0, min(100, int(percent))),
                "utc": datetime.now(UTC).isoformat(),
            }
        )
        self.payload["stages"] = self.payload["stages"][-100:]
        self.payload["updated_utc"] = datetime.now(UTC).isoformat()
        _atomic_replace_json(self.path, self.payload)

    def finish(self, *, manifest: str | None = None, error: str | None = None) -> None:
        self.payload["status"] = "failed" if error else "complete"
        self.payload["error"] = error
        self.payload["manifest"] = manifest
        self.payload["updated_utc"] = datetime.now(UTC).isoformat()
        _atomic_replace_json(self.path, self.payload)


class VerifiedSliceCache:
    """Content-addressed cache containing only hash-verified slicer artifacts."""

    REQUIRED = ("result.json", "plate_1.gcode", "ready-to-print.3mf")

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(prepared_project: Path, slicer: Path) -> str:
        stat = slicer.stat()
        digest = hashlib.sha256()
        # Generated Bambu profiles contain volatile provenance metadata.  Hash
        # only the geometry and effective settings that can change slicing.
        with zipfile.ZipFile(prepared_project) as archive:
            validate_zip_archive(archive)
            relevant = [
                name
                for name in archive.namelist()
                if name in {
                    "3D/3dmodel.model",
                    "Metadata/model_settings.config",
                    "Metadata/project_settings.config",
                    "Metadata/layer_config_ranges.xml",
                }
                or name.startswith("3D/Objects/")
            ]
            for name in sorted(relevant):
                digest.update(name.encode("utf-8"))
                digest.update(read_zip_member(archive, name))
        digest.update(str(slicer.resolve()).encode("utf-8"))
        digest.update(f"|{stat.st_size}|{stat.st_mtime_ns}|cache-v2".encode())
        return digest.hexdigest()

    def restore(self, key: str, destination: Path) -> bool:
        entry = self.root / key
        metadata_path = entry / "cache.json"
        if not metadata_path.is_file():
            return False
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            hashes = metadata["sha256"]
            for name in self.REQUIRED:
                source = entry / name
                if not source.is_file() or sha256_file(source) != hashes.get(name):
                    return False
            for name in self.REQUIRED:
                shutil.copyfile(entry / name, destination / name)
            return True
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            return False

    def store(self, key: str, source: Path) -> None:
        if not all((source / name).is_file() for name in self.REQUIRED):
            return
        entry = self.root / key
        if entry.exists():
            return
        temporary = self.root / f".{key}.{uuid.uuid4().hex}.tmp"
        temporary.mkdir()
        try:
            hashes: dict[str, str] = {}
            for name in self.REQUIRED:
                shutil.copyfile(source / name, temporary / name)
                hashes[name] = sha256_file(temporary / name)
            _atomic_replace_json(
                temporary / "cache.json",
                {
                    "schema_version": 1,
                    "created_utc": datetime.now(UTC).isoformat(),
                    "sha256": hashes,
                },
            )
            try:
                os.replace(temporary, entry)
            except FileExistsError:
                pass
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)


def create_backup(files: Iterable[str | Path], output: str | Path) -> Path:
    target = Path(output).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"backup already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        manifest: list[dict[str, Any]] = []
        for raw in files:
            path = Path(raw).expanduser().resolve()
            if not path.is_file():
                continue
            archive.write(path, f"data/{path.name}")
            manifest.append({"name": path.name, "sha256": sha256_file(path)})
        archive.writestr(
            "backup.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "created_utc": datetime.now(UTC).isoformat(),
                    "files": manifest,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    os.replace(temporary, target)
    return target


def create_application_backup(
    application_dir: str | Path,
    output: str | Path,
    *,
    data_files: Iterable[str | Path] = (),
    data_roots: Iterable[str | Path] = (),
    excluded_names: Iterable[str] = (),
) -> Path:
    """Create a rollback archive containing the application and shared state.

    Workspace data folders can live beside the executable, so callers pass
    their names through ``excluded_names`` to avoid recursively archiving
    uploaded models, ready projects, prior backups and learning photos.
    """
    source_root = Path(application_dir).expanduser().resolve()
    target = Path(output).expanduser().resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"application directory not found: {source_root}")
    if target.exists():
        raise FileExistsError(f"backup already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    excluded = {str(name).casefold() for name in excluded_names}
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    manifest: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(source_root.rglob("*")):
                relative = path.relative_to(source_root)
                if any(part.casefold() in excluded for part in relative.parts):
                    continue
                if not path.is_file() or path.is_symlink() or path.resolve() == target:
                    continue
                archive_name = f"application/{relative.as_posix()}"
                archive.write(path, archive_name)
                manifest.append({"path": archive_name, "sha256": sha256_file(path)})
            for raw in data_files:
                path = Path(raw).expanduser().resolve()
                if not path.is_file():
                    continue
                archive_name = f"data/{path.name}"
                archive.write(path, archive_name)
                manifest.append({"path": archive_name, "sha256": sha256_file(path)})
            for raw in data_roots:
                root = Path(raw).expanduser().resolve()
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("*")):
                    if not path.is_file() or path.is_symlink():
                        continue
                    relative = path.relative_to(root)
                    archive_name = f"data/{root.name}/{relative.as_posix()}"
                    archive.write(path, archive_name)
                    manifest.append({"path": archive_name, "sha256": sha256_file(path)})
            archive.writestr(
                "backup.json",
                json.dumps(
                    {
                        "schema_version": 2,
                        "kind": "application-rollback",
                        "created_utc": datetime.now(UTC).isoformat(),
                        "application_dir": str(source_root),
                        "files": manifest,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def restore_application_data_backup(
    backup: str | Path,
    *,
    data_files: Mapping[str, str | Path],
    data_roots: Mapping[str, str | Path] | None = None,
) -> tuple[Path, ...]:
    """Restore verified user data from an application backup.

    Executables are deliberately not replaced while the application is
    running.  Every selected member is hash-checked before any destination is
    touched and individual files are then replaced atomically.
    """
    source = Path(backup).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"backup not found: {source}")
    staged: list[tuple[Path, bytes]] = []
    with zipfile.ZipFile(source) as archive:
        validate_zip_archive(archive)
        payload = json.loads(read_zip_member(archive, "backup.json").decode("utf-8"))
        if payload.get("kind") != "application-rollback":
            raise ValueError("unsupported backup kind")
        entries = payload.get("files")
        if not isinstance(entries, list):
            raise TypeError("backup manifest has no file list")
        hashes = {
            str(item.get("path", "")): str(item.get("sha256", ""))
            for item in entries
            if isinstance(item, dict)
        }
        for name, destination_raw in data_files.items():
            archive_name = f"data/{name}"
            expected = hashes.get(archive_name)
            if not expected:
                continue
            content = read_zip_member(archive, archive_name)
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValueError(f"backup checksum mismatch: {archive_name}")
            staged.append((Path(destination_raw).expanduser().resolve(), content))
        for root_name, destination_raw in (data_roots or {}).items():
            prefix = f"data/{root_name}/"
            destination_root = Path(destination_raw).expanduser().resolve()
            for archive_name, expected in hashes.items():
                if not archive_name.startswith(prefix):
                    continue
                relative = Path(archive_name[len(prefix) :])
                if relative.is_absolute() or ".." in relative.parts:
                    raise ValueError(f"unsafe backup member: {archive_name}")
                content = read_zip_member(archive, archive_name)
                if hashlib.sha256(content).hexdigest() != expected:
                    raise ValueError(f"backup checksum mismatch: {archive_name}")
                staged.append(((destination_root / relative).resolve(), content))

    restored: list[Path] = []
    for destination, content in staged:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, destination)
            restored.append(destination)
        finally:
            temporary.unlink(missing_ok=True)
    return tuple(restored)


def build_diagnostic_bundle(
    output: str | Path,
    *,
    documents: Iterable[str | Path] = (),
    environment: dict[str, Any] | None = None,
) -> Path:
    """Create a support ZIP without models, G-code, photos or account secrets."""
    target = Path(output).expanduser().resolve()
    if target.exists():
        raise FileExistsError(f"diagnostic bundle already exists: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    allowed_suffixes = {".json", ".log", ".txt"}
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    with zipfile.ZipFile(temporary, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        included: list[str] = []
        for raw in documents:
            path = Path(raw).expanduser().resolve()
            if not path.is_file() or path.suffix.casefold() not in allowed_suffixes:
                continue
            archive.write(path, f"diagnostics/{path.name}")
            included.append(path.name)
        archive.writestr(
            "diagnostics.json",
            json.dumps(
                {
                    "schema_version": 1,
                    "created_utc": datetime.now(UTC).isoformat(),
                    "included": included,
                    "environment": environment or {},
                    "privacy": "Models, G-code and PrintDNA photos are intentionally excluded.",
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    os.replace(temporary, target)
    return target
