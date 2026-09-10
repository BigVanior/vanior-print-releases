"""Axis and dominant-surface orientation search."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np
import trimesh

from .analyzer import AnalysisError, _analyze_mesh_health, _load_stl
from .config import P1S
from .report import OrientationAnalysis, OrientationCandidate


@dataclass(frozen=True)
class OrientationExportResult:
    source_path: Path
    output_path: Path
    rotation_deg: tuple[float, float, float]
    score: float
    dimensions_mm: tuple[float, float, float]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_path"] = str(self.source_path)
        result["output_path"] = str(self.output_path)
        return result


@dataclass(frozen=True)
class _EvaluatedOrientation:
    candidate: OrientationCandidate
    matrix: np.ndarray


def _align_vector_to_down(normal: np.ndarray) -> np.ndarray:
    source = np.asarray(normal, dtype=float)
    source /= np.linalg.norm(source)
    target = np.array([0.0, 0.0, -1.0])
    cross = np.cross(source, target)
    sine = float(np.linalg.norm(cross))
    cosine = float(np.clip(np.dot(source, target), -1.0, 1.0))
    if sine <= 1e-12:
        if cosine > 0.0:
            return np.eye(3)
        helper = np.array([1.0, 0.0, 0.0])
        if abs(source[0]) > 0.9:
            helper = np.array([0.0, 1.0, 0.0])
        axis = np.cross(source, helper)
        axis /= np.linalg.norm(axis)
        return -np.eye(3) + 2.0 * np.outer(axis, axis)

    skew = np.array(
        [
            [0.0, -cross[2], cross[1]],
            [cross[2], 0.0, -cross[0]],
            [-cross[1], cross[0], 0.0],
        ]
    )
    return np.eye(3) + skew + (skew @ skew) * ((1.0 - cosine) / (sine * sine))


def _matrix_to_euler_xyz(matrix: np.ndarray) -> tuple[float, float, float]:
    sine_y = float(np.clip(-matrix[2, 0], -1.0, 1.0))
    y = float(np.arcsin(sine_y))
    cosine_y = float(np.cos(y))
    if abs(cosine_y) > 1e-8:
        x = float(np.arctan2(matrix[2, 1], matrix[2, 2]))
        z = float(np.arctan2(matrix[1, 0], matrix[0, 0]))
    else:
        x = float(np.arctan2(-matrix[1, 2], matrix[1, 1]))
        z = 0.0

    values = np.rad2deg([x, y, z])
    normalized = ((values + 180.0) % 360.0) - 180.0
    normalized[np.isclose(normalized, 0.0, atol=1e-8)] = 0.0
    return tuple(float(round(value, 3)) for value in normalized)


def _dominant_surface_normals(
    mesh: trimesh.Trimesh,
    *,
    maximum: int = 8,
) -> list[np.ndarray]:
    normals = np.asarray(mesh.face_normals)
    areas = np.asarray(mesh.area_faces)
    quantized = np.rint(normals / 0.1).astype(np.int8)
    _, inverse = np.unique(quantized, axis=0, return_inverse=True)
    cluster_count = int(inverse.max()) + 1
    cluster_areas = np.bincount(inverse, weights=areas, minlength=cluster_count)
    vector_sums = np.column_stack(
        [
            np.bincount(
                inverse,
                weights=areas * normals[:, axis],
                minlength=cluster_count,
            )
            for axis in range(3)
        ]
    )
    total_area = float(np.sum(areas))
    selected: list[np.ndarray] = []
    for cluster in np.argsort(cluster_areas)[::-1]:
        if cluster_areas[cluster] < total_area * 0.005:
            break
        vector = vector_sums[cluster]
        length = float(np.linalg.norm(vector))
        if length <= 1e-12:
            continue
        normal = vector / length
        if any(float(np.dot(normal, existing)) > np.cos(np.deg2rad(5.0)) for existing in selected):
            continue
        selected.append(normal)
        if len(selected) >= maximum:
            break
    return selected


def _candidate_normals(mesh: trimesh.Trimesh) -> list[tuple[str, np.ndarray]]:
    axis_normals = [
        np.array([0.0, 0.0, -1.0]),
        np.array([0.0, 0.0, 1.0]),
        np.array([-1.0, 0.0, 0.0]),
        np.array([1.0, 0.0, 0.0]),
        np.array([0.0, -1.0, 0.0]),
        np.array([0.0, 1.0, 0.0]),
    ]
    result: list[tuple[str, np.ndarray]] = [("AXIS", value) for value in axis_normals]
    for normal in _dominant_surface_normals(mesh):
        if any(float(np.dot(normal, existing)) > np.cos(np.deg2rad(5.0)) for _, existing in result):
            continue
        result.append(("SURFACE", normal))
    return result


def _score_candidate(
    dimensions: np.ndarray,
    base_ratio: float,
    overhang_ratio: float,
    support_volume_ratio: float,
    bed_surface_ratio: float = 0.0,
    protect_visible_surfaces: bool = False,
    exposed_support_ratio: float = 0.0,
    prefer_layer_strength: bool = False,
) -> float:
    width, depth, height = dimensions
    slenderness = height / max(width, depth, 1e-9)
    adhesion_score = 30.0 * min(base_ratio / 0.35, 1.0)
    support_area_score = 30.0 * (1.0 - min(overhang_ratio / 0.25, 1.0))
    # Equal overhang areas can have radically different print cost: a ceiling
    # near the bed needs little support, while the same ceiling near the top
    # creates a tall forest. Weight unsupported area by its normalized height.
    support_volume_score = 20.0 * (
        1.0 - min(support_volume_ratio / 0.18, 1.0)
    )
    height_score = 8.0 * (1.0 - min(height / P1S.build_volume_mm[2], 1.0))
    stability_score = 12.0 * (1.0 - min(slenderness / 4.0, 1.0))
    visible_bed_penalty = 0.0
    visible_support_penalty = 0.0
    if protect_visible_surfaces:
        # A decorative figure lying on its back may be stable and cheap to
        # support, yet permanently emboss a large visible skin with the plate
        # texture. Foot-sized contact is welcome; body-sized contact is not.
        visible_bed_penalty = min(
            34.0,
            max(0.0, (bed_surface_ratio - 0.008) / 0.04) * 34.0,
        )
        # Thin decorative shells have two geometrically similar sides. A pure
        # overhang score may therefore turn a mask face-down to save a small
        # amount of support. Prefer the placement whose downward faces are
        # recessed inside the shape rather than exposed on its outer envelope.
        visible_support_penalty = min(
            30.0,
            max(0.0, exposed_support_ratio) * 500.0,
        )
    geometric_score = (
        adhesion_score
        + support_area_score
        + support_volume_score
        + height_score
        + stability_score
        - visible_bed_penalty
        - visible_support_penalty
    )
    if prefer_layer_strength:
        # Without a user-supplied load vector no orientation can guarantee
        # strength.  Prefer a broad XY cross-section, which reduces reliance
        # on the weaker Z-layer bonds for common bending and tensile loads.
        strength_score = 100.0 * min(width, depth) / max(width, depth, height, 1e-9)
        geometric_score = geometric_score * 0.88 + strength_score * 0.12
    return float(np.clip(geometric_score, 0.0, 100.0))


def _evaluate_candidate(
    mesh: trimesh.Trimesh,
    kind: str,
    normal: np.ndarray,
    overhang_angle_deg: float,
    protect_visible_surfaces: bool = False,
    prefer_layer_strength: bool = False,
) -> _EvaluatedOrientation:
    matrix = _align_vector_to_down(normal)
    vertices = np.asarray(mesh.vertices)
    triangles = np.asarray(mesh.triangles)
    face_normals = np.asarray(mesh.face_normals)
    face_areas = np.asarray(mesh.area_faces)
    centers = np.asarray(mesh.triangles_center)

    rotated_vertices = vertices @ matrix.T
    dimensions = np.ptp(rotated_vertices, axis=0)
    minimum_z = float(np.min(rotated_vertices[:, 2]))
    height = float(dimensions[2])
    tolerance_mm = max(0.05, height * 0.002)
    triangle_z = np.einsum("fvc,c->fv", triangles, matrix[2])
    normal_z = face_normals @ matrix[2]
    center_z = centers @ matrix[2]

    on_bed = np.max(np.abs(triangle_z - minimum_z), axis=1) <= tolerance_mm
    base_area = float(np.sum(face_areas[on_bed] * np.abs(normal_z[on_bed])))
    footprint = float(dimensions[0] * dimensions[1])
    base_ratio = min(1.0, base_area / footprint) if footprint > 0 else 0.0

    downward_limit = -np.cos(np.deg2rad(overhang_angle_deg))
    overhang_mask = (normal_z < downward_limit) & (
        center_z > minimum_z + tolerance_mm
    )
    overhang_area = float(np.sum(face_areas[overhang_mask]))
    surface_area = float(np.sum(face_areas))
    bed_surface_ratio = base_area / surface_area if surface_area > 0 else 0.0
    overhang_ratio = overhang_area / surface_area if surface_area > 0 else 0.0
    normalized_support_height = np.clip(
        (center_z - minimum_z) / max(height, 1e-9), 0.0, 1.0
    )
    support_volume_ratio = (
        float(np.sum(face_areas[overhang_mask] * normalized_support_height[overhang_mask]))
        / surface_area
        if surface_area > 0
        else 0.0
    )
    projected_vertices = vertices @ normal
    projected_centers = centers @ normal
    projection_span = float(np.ptp(projected_vertices))
    envelope_depth = float(np.max(projected_vertices)) - projected_centers
    # A face close to the supporting plane in its own outward direction is
    # likely part of the visible exterior. Faces deep behind the rim of a
    # hollow shell are more suitable places for support contact.
    exposure_scale_mm = max(0.5, projection_span * 0.12)
    envelope_exposure = np.exp(-envelope_depth / exposure_scale_mm)
    exposed_support_ratio = (
        float(np.sum(face_areas[overhang_mask] * envelope_exposure[overhang_mask]))
        / surface_area
        if surface_area > 0
        else 0.0
    )
    fits = all(
        float(size) <= limit + 1e-6
        for size, limit in zip(dimensions, P1S.build_volume_mm)
    )
    score = (
        _score_candidate(
            dimensions,
            base_ratio,
            overhang_ratio,
            support_volume_ratio,
            bed_surface_ratio,
            protect_visible_surfaces,
            exposed_support_ratio,
            prefer_layer_strength,
        )
        if fits
        else 0.0
    )
    if fits:
        # Preserve an already sensible author orientation when two placements
        # are effectively tied. Rotating a decorative surface onto the bed can
        # save a little support while creating a much more visible scar.
        rotation_angle = float(
            np.rad2deg(
                np.arccos(np.clip((np.trace(matrix) - 1.0) / 2.0, -1.0, 1.0))
            )
        )
        score = max(0.0, score - min(6.0, 6.0 * rotation_angle / 180.0))
    candidate = OrientationCandidate(
        rank=0,
        kind=kind,
        rotation_deg=_matrix_to_euler_xyz(matrix),
        dimensions_mm=tuple(float(value) for value in dimensions),
        base_area_mm2=base_area,
        base_area_ratio=base_ratio,
        overhang_area_mm2=overhang_area,
        overhang_area_ratio=overhang_ratio,
        fits_build_volume=fits,
        score=round(score, 2),
    )
    return _EvaluatedOrientation(candidate=candidate, matrix=matrix)


def _evaluate_orientations(
    mesh: trimesh.Trimesh,
    *,
    overhang_angle_deg: float,
    top_count: int,
    protect_visible_surfaces: bool = False,
    prefer_layer_strength: bool = False,
) -> tuple[OrientationAnalysis, _EvaluatedOrientation]:
    evaluated = [
        _evaluate_candidate(
            mesh,
            kind,
            normal,
            overhang_angle_deg,
            protect_visible_surfaces,
            prefer_layer_strength,
        )
        for kind, normal in _candidate_normals(mesh)
    ]
    ranked = sorted(
        enumerate(evaluated),
        key=lambda item: (-item[1].candidate.score, item[0]),
    )
    ranked_evaluations: list[_EvaluatedOrientation] = []
    for rank, (_, evaluation) in enumerate(ranked, start=1):
        ranked_evaluations.append(
            _EvaluatedOrientation(
                candidate=replace(evaluation.candidate, rank=rank),
                matrix=evaluation.matrix,
            )
        )

    current_score = evaluated[0].candidate.score
    best = ranked_evaluations[0]
    second_score = ranked_evaluations[1].candidate.score if len(ranked_evaluations) > 1 else best.candidate.score
    score_gap = best.candidate.score - second_score
    if score_gap >= 10.0:
        confidence = "HIGH"
    elif score_gap >= 4.0:
        confidence = "MEDIUM"
    else:
        confidence = "LOW"
    analysis = OrientationAnalysis(
        candidates_evaluated=len(evaluated),
        current_score=current_score,
        best_score=best.candidate.score,
        score_improvement=round(best.candidate.score - current_score, 2),
        confidence=confidence,
        top_candidates=tuple(
            item.candidate for item in ranked_evaluations[: max(1, top_count)]
        ),
    )
    return analysis, best


def optimize_orientation(
    source: str | Path,
    *,
    overhang_angle_deg: float = 45.0,
    top_count: int = 3,
    protect_visible_surfaces: bool = False,
    prefer_layer_strength: bool = False,
) -> OrientationAnalysis:
    source_path = Path(source).expanduser().resolve()
    raw_mesh = _load_stl(source_path)
    health, mesh = _analyze_mesh_health(raw_mesh)
    if health.status == "INVALID":
        raise AnalysisError("orientation search requires a printable 3D mesh")
    analysis, _ = _evaluate_orientations(
        mesh,
        overhang_angle_deg=overhang_angle_deg,
        top_count=top_count,
        protect_visible_surfaces=protect_visible_surfaces,
        prefer_layer_strength=prefer_layer_strength,
    )
    return analysis


def orient_stl(
    source: str | Path,
    output: str | Path,
    *,
    overhang_angle_deg: float = 45.0,
    top_count: int = 3,
    protect_visible_surfaces: bool = False,
    prefer_layer_strength: bool = False,
) -> tuple[OrientationAnalysis, OrientationExportResult]:
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path == output_path:
        raise AnalysisError("orientation output must be different from the source STL")
    if output_path.suffix.lower() != ".stl":
        raise AnalysisError("orientation output must use the .stl extension")
    if output_path.exists():
        raise AnalysisError(f"orientation output already exists: {output_path}")
    if not output_path.parent.is_dir():
        raise AnalysisError(f"orientation output directory not found: {output_path.parent}")

    raw_mesh = _load_stl(source_path)
    health, mesh = _analyze_mesh_health(raw_mesh)
    if health.status == "INVALID":
        raise AnalysisError("orientation search requires a printable 3D mesh")
    analysis, best = _evaluate_orientations(
        mesh,
        overhang_angle_deg=overhang_angle_deg,
        top_count=top_count,
        protect_visible_surfaces=protect_visible_surfaces,
        prefer_layer_strength=prefer_layer_strength,
    )

    # A flipped figurine can improve visible surfaces, but an ambiguous score
    # must not silently override the author's sensible upright placement.
    # The flipped placement remains visible among the reported candidates.
    if (
        protect_visible_surfaces
        and best.candidate.rotation_deg != (0.0, 0.0, 0.0)
        and analysis.confidence == "LOW"
        and analysis.score_improvement < 12.0
    ):
        best = _evaluate_candidate(
            mesh,
            "CURRENT",
            np.array([0.0, 0.0, -1.0]),
            overhang_angle_deg,
            protect_visible_surfaces,
            prefer_layer_strength,
        )

    transform = np.eye(4)
    transform[:3, :3] = best.matrix
    oriented = mesh.copy()
    oriented.apply_transform(transform)
    oriented.apply_translation(-oriented.bounds[0])
    oriented.export(output_path, file_type="stl")
    result = OrientationExportResult(
        source_path=source_path,
        output_path=output_path,
        rotation_deg=best.candidate.rotation_deg,
        score=best.candidate.score,
        dimensions_mm=best.candidate.dimensions_mm,
    )
    return analysis, result
