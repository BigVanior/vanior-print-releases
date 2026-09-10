"""Conservative STL decimation with geometry invariants."""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

from .analyzer import _analyze_mesh_health, _load_stl, analyze_stl


class SimplificationError(RuntimeError):
    """Raised when a simplified copy would violate a safety invariant."""


@dataclass(frozen=True)
class SimplificationResult:
    source_path: Path
    output_path: Path
    source_sha256: str
    output_sha256: str
    triangles_before: int
    triangles_after: int
    reduction_percent: float
    max_dimension_error_mm: float
    volume_error_percent: float
    surface_area_error_percent: float
    final_status: str

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_path"] = str(self.source_path)
        result["output_path"] = str(self.output_path)
        return result


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def simplify_stl(
    source: str | Path,
    output: str | Path,
    *,
    target_faces: int,
    max_dimension_error_mm: float = 0.1,
    max_volume_error_percent: float = 1.0,
    max_surface_area_error_percent: float = 3.0,
) -> SimplificationResult:
    """Write a decimated STL only if topology and global geometry remain safe."""
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path.suffix.lower() != ".stl" or not source_path.is_file():
        raise SimplificationError("simplification source must be an existing STL")
    if output_path.suffix.lower() != ".stl":
        raise SimplificationError("simplification output must use the .stl extension")
    if output_path == source_path or output_path.exists():
        raise SimplificationError(
            f"simplification output already exists or is the source: {output_path}"
        )
    if not output_path.parent.is_dir():
        raise SimplificationError(
            f"simplification output parent not found: {output_path.parent}"
        )
    if target_faces < 100:
        raise SimplificationError("target_faces must be at least 100")
    if min(
        max_dimension_error_mm,
        max_volume_error_percent,
        max_surface_area_error_percent,
    ) <= 0:
        raise SimplificationError("simplification safety tolerances must be positive")

    source_hash = _sha256(source_path)
    raw = _load_stl(source_path)
    health_before, mesh = _analyze_mesh_health(raw)
    triangles_before = len(mesh.faces)
    if health_before.status != "READY" or not mesh.is_watertight:
        raise SimplificationError("only READY watertight meshes may be simplified")
    if target_faces >= triangles_before:
        raise SimplificationError(
            f"target_faces ({target_faces}) must be below source count ({triangles_before})"
        )

    try:
        simplified = mesh.simplify_quadric_decimation(face_count=target_faces)
    except (ImportError, ModuleNotFoundError) as exc:
        raise SimplificationError(
            "fast-simplification is required for mesh decimation"
        ) from exc
    except Exception as exc:
        raise SimplificationError(f"mesh decimation failed: {exc}") from exc
    simplified.remove_unreferenced_vertices()
    health_after, simplified = _analyze_mesh_health(simplified)
    if health_after.status != "READY" or not simplified.is_watertight:
        raise SimplificationError("decimation produced a non-READY or open mesh")
    if health_after.meaningful_body_count != health_before.meaningful_body_count:
        raise SimplificationError("decimation changed the number of meaningful bodies")

    dimension_error = float(np.max(np.abs(mesh.extents - simplified.extents)))
    source_volume = abs(float(mesh.volume))
    simplified_volume = abs(float(simplified.volume))
    volume_error = (
        abs(simplified_volume - source_volume) / source_volume * 100.0
        if source_volume > 0
        else float("inf")
    )
    source_area = float(mesh.area)
    area_error = (
        abs(float(simplified.area) - source_area) / source_area * 100.0
        if source_area > 0
        else float("inf")
    )
    violations: list[str] = []
    if dimension_error > max_dimension_error_mm:
        violations.append(
            f"dimension error {dimension_error:.4f} mm exceeds {max_dimension_error_mm:.4f} mm"
        )
    if volume_error > max_volume_error_percent:
        violations.append(
            f"volume error {volume_error:.4f}% exceeds {max_volume_error_percent:.4f}%"
        )
    if area_error > max_surface_area_error_percent:
        violations.append(
            f"surface area error {area_error:.4f}% exceeds "
            f"{max_surface_area_error_percent:.4f}%"
        )
    if violations:
        raise SimplificationError("; ".join(violations))

    simplified.export(output_path, file_type="stl")
    try:
        exported = analyze_stl(output_path)
        if exported.health.status != "READY" or not exported.metrics.is_watertight:
            raise SimplificationError("exported simplified STL failed topology validation")
        if _sha256(source_path) != source_hash:
            raise SimplificationError("source STL changed during simplification")
    except Exception:
        output_path.unlink(missing_ok=True)
        raise
    triangles_after = exported.metrics.triangle_count
    return SimplificationResult(
        source_path=source_path,
        output_path=output_path,
        source_sha256=source_hash,
        output_sha256=_sha256(output_path),
        triangles_before=triangles_before,
        triangles_after=triangles_after,
        reduction_percent=(1.0 - triangles_after / triangles_before) * 100.0,
        max_dimension_error_mm=dimension_error,
        volume_error_percent=volume_error,
        surface_area_error_percent=area_error,
        final_status=exported.health.status,
    )
