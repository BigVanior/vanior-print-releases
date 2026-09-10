"""Read-only release and environment diagnostics."""

from __future__ import annotations

import importlib.metadata
import platform
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_new_text
from .schema import SCHEMAS, load_schema
from .slicer_backend import INDEPENDENT_BACKEND
from .version import __version__


@dataclass(frozen=True)
class DiagnosticCheck:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class DiagnosticReport:
    ok: bool
    application_version: str
    python_version: str
    platform: str
    checks: tuple[DiagnosticCheck, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def run_diagnostics(executable: str | Path | None = None) -> DiagnosticReport:
    """Check the self-contained runtime, dependencies, schemas and file publishing."""
    del executable  # Retained for backwards-compatible callers; no external engine is used.
    checks: list[DiagnosticCheck] = []
    python_ok = sys.version_info >= (3, 11)
    checks.append(
        DiagnosticCheck(
            "python",
            python_ok,
            f"{platform.python_version()} (requires >=3.11)",
        )
    )
    try:
        distribution = importlib.metadata.distribution("ai-print-optimizer")
        installed_version = distribution.version
        runtime_path = Path(__file__).resolve()
        distribution_root = Path(distribution.locate_file("")).resolve()
        try:
            runtime_is_installed_distribution = runtime_path.is_relative_to(distribution_root)
        except ValueError:
            runtime_is_installed_distribution = False
        frozen = bool(getattr(sys, "frozen", False))
        running_from_source_tree = runtime_path.parent.parent.name.casefold() == "src"
        metadata_matches = installed_version == __version__
        metadata_is_authoritative = (
            runtime_is_installed_distribution and not frozen and not running_from_source_tree
        )
        check_ok = metadata_matches or not metadata_is_authoritative
        if frozen:
            detail = f"frozen application={__version__}"
        elif metadata_is_authoritative:
            detail = f"runtime={__version__}, installed={installed_version}"
        else:
            detail = (
                f"source checkout={__version__}; unrelated installed metadata="
                f"{installed_version} ignored"
            )
        checks.append(DiagnosticCheck("package-metadata", check_ok, detail))
    except importlib.metadata.PackageNotFoundError:
        checks.append(DiagnosticCheck("package-metadata", False, "package is not installed"))
    for dependency in (
        "numpy",
        "trimesh",
        "fast-simplification",
        "shapely",
        "Pillow",
        "PySide6",
        "paho-mqtt",
    ):
        try:
            version = importlib.metadata.version(dependency)
            checks.append(DiagnosticCheck(f"dependency:{dependency}", True, version))
        except importlib.metadata.PackageNotFoundError:
            checks.append(DiagnosticCheck(f"dependency:{dependency}", False, "not installed"))
    for schema_name in SCHEMAS:
        try:
            schema = load_schema(schema_name)
            checks.append(
                DiagnosticCheck(
                    f"schema:{schema_name}",
                    schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
                    str(schema.get("$id", "missing $id")),
                )
            )
        except Exception as exc:  # noqa: BLE001 -- diagnostic checks must remain isolated
            checks.append(DiagnosticCheck(f"schema:{schema_name}", False, str(exc)))
    try:
        with tempfile.TemporaryDirectory() as directory:
            probe = Path(directory) / "atomic-probe.txt"
            atomic_write_new_text(probe, "ok\n")
            atomic_ok = probe.read_text(encoding="utf-8") == "ok\n"
        checks.append(DiagnosticCheck("atomic-write", atomic_ok, "same-directory hard-link publish"))
    except Exception as exc:  # noqa: BLE001 -- report platform-specific filesystem failures
        checks.append(DiagnosticCheck("atomic-write", False, str(exc)))
    checks.append(
        DiagnosticCheck(
            "independent-engine",
            INDEPENDENT_BACKEND.independent,
            f"{INDEPENDENT_BACKEND.name}; external slicer is not required",
        )
    )
    return DiagnosticReport(
        ok=all(check.ok for check in checks),
        application_version=__version__,
        python_version=platform.python_version(),
        platform=platform.platform(),
        checks=tuple(checks),
    )
