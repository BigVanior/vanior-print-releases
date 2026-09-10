"""Core STL geometry analysis and conservative print heuristics."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import trimesh

from .config import MATERIALS, P1S, SUPPORTED_NOZZLE_DIAMETERS_MM
from .functional_intent import infer_functional_intent
from .geometry_features import (
    analyze_geometry_features,
    build_local_modifier_plan,
    plan_support_exit,
)
from .input_safety import UnsafeInputError, validate_stl_file
from .material_protocol import settings_for_material
from .report import (
    AnalysisReport,
    BodyMetrics,
    GeometryMetrics,
    MeshHealth,
    PrintSettings,
    PurposeAssessment,
    RiskAssessment,
)
from .surface_intelligence import analyze_surfaces

MAX_REPORTED_BODIES = 100
DEBRIS_MAX_TRIANGLES = 20
DEBRIS_MAX_AREA_MM2 = 1.0
DEBRIS_MAX_DIAGONAL_MM = 1.0


class AnalysisError(RuntimeError):
    """Raised when a model cannot be analyzed safely."""


def _risk_level(value: float, medium: float, high: float) -> str:
    if value >= high:
        return "HIGH"
    if value >= medium:
        return "MEDIUM"
    return "LOW"


def _classify_model_purpose(
    mesh: trimesh.Trimesh,
    volume_mm3: float | None,
) -> PurposeAssessment:
    """Estimate CAD-like functional geometry versus organic decorative geometry."""
    areas = np.asarray(mesh.area_faces, dtype=float)
    normals = np.abs(np.asarray(mesh.face_normals, dtype=float))
    total_area = max(float(areas.sum()), 1e-9)
    strongest_axis = normals.max(axis=1)
    axis_ratio = float(areas[strongest_axis >= np.cos(np.deg2rad(8.0))].sum() / total_area)
    curved_ratio = float(areas[strongest_axis < 0.94].sum() / total_area)
    box_volume = float(np.prod(mesh.extents))
    fill_ratio = (
        min(1.0, max(0.0, volume_mm3 / box_volume))
        if volume_mm3 is not None and box_volume > 1e-9
        else None
    )
    score = 0.15 + 0.70 * axis_ratio - 0.25 * curved_ratio
    if fill_ratio is not None and fill_ratio >= 0.70:
        score += 0.10
    score = min(1.0, max(0.0, score))
    if score >= 0.62:
        classification = "functional"
    elif score <= 0.38:
        classification = "decorative"
    else:
        classification = "ambiguous"
    distance = abs(score - 0.5)
    confidence = "HIGH" if distance >= 0.30 else "MEDIUM" if distance >= 0.16 else "LOW"
    reasons: list[str] = []
    if axis_ratio >= 0.65:
        reasons.append("large axis-aligned planar surface share suggests a CAD part")
    elif curved_ratio >= 0.55:
        reasons.append("mostly curved surface suggests an organic or decorative model")
    else:
        reasons.append("mixed planar and curved geometry is not decisive")
    if fill_ratio is not None and fill_ratio >= 0.70:
        reasons.append("high bounding-box fill is typical of a solid functional part")
    if classification == "ambiguous":
        reasons.append("automatic purpose is uncertain; a user override is recommended")
    return PurposeAssessment(
        classification=classification,
        confidence=confidence,
        functional_score=score,
        axis_aligned_surface_ratio=axis_ratio,
        curved_surface_ratio=curved_ratio,
        bounding_box_fill_ratio=fill_ratio,
        reasons=tuple(reasons),
    )


def _face_components(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    """Return a compact component index per face and face counts per component."""
    face_count = len(mesh.faces)
    if face_count == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.int64)

    parent = np.arange(face_count, dtype=np.int64)

    def find(item: int) -> int:
        while parent[item] != item:
            parent[item] = parent[parent[item]]
            item = int(parent[item])
        return item

    for left, right in np.asarray(mesh.face_adjacency, dtype=np.int64):
        root_left = find(int(left))
        root_right = find(int(right))
        if root_left != root_right:
            parent[root_right] = root_left

    labels = np.fromiter(
        (find(index) for index in range(face_count)),
        dtype=np.int64,
        count=face_count,
    )
    _, component_index, component_face_counts = np.unique(
        labels,
        return_inverse=True,
        return_counts=True,
    )
    return component_index, component_face_counts


def _edge_health_by_component(
    mesh: trimesh.Trimesh,
    component_index: np.ndarray,
    component_count: int,
) -> tuple[np.ndarray, np.ndarray]:
    """Count open and non-manifold edges for every connected body."""
    faces = np.asarray(mesh.faces, dtype=np.int64)
    edges = faces[:, [[0, 1], [1, 2], [2, 0]]].reshape((-1, 2))
    edges.sort(axis=1)
    _, first_occurrence, edge_counts = np.unique(
        edges,
        axis=0,
        return_index=True,
        return_counts=True,
    )
    edge_face_index = np.repeat(np.arange(len(faces), dtype=np.int64), 3)
    edge_component = component_index[edge_face_index[first_occurrence]]
    boundary_counts = np.bincount(
        edge_component[edge_counts == 1],
        minlength=component_count,
    )
    non_manifold_counts = np.bincount(
        edge_component[edge_counts > 2],
        minlength=component_count,
    )
    return boundary_counts, non_manifold_counts


def _signed_mesh_volume(triangles: np.ndarray) -> float:
    cross = np.cross(triangles[:, 1], triangles[:, 2])
    signed = np.einsum("ij,ij->i", triangles[:, 0], cross).sum() / 6.0
    return abs(float(signed))


def _mesh_density(triangle_count: int, surface_area_mm2: float) -> tuple[str, float]:
    triangles_per_mm2 = triangle_count / max(surface_area_mm2, 1e-9)
    if triangle_count >= 500_000 or triangles_per_mm2 >= 100.0:
        return "VERY_HIGH", triangles_per_mm2
    if triangle_count >= 150_000 or triangles_per_mm2 >= 25.0:
        return "HIGH", triangles_per_mm2
    return "NORMAL", triangles_per_mm2


def _analyze_mesh_health(mesh: trimesh.Trimesh) -> tuple[MeshHealth, trimesh.Trimesh]:
    component_index, component_face_counts = _face_components(mesh)
    component_count = len(component_face_counts)
    face_areas = np.asarray(mesh.area_faces)
    component_areas = np.bincount(
        component_index,
        weights=face_areas,
        minlength=component_count,
    )
    triangles = np.asarray(mesh.triangles)
    face_minimums = triangles.min(axis=1)
    face_maximums = triangles.max(axis=1)
    component_minimums = np.full((component_count, 3), np.inf)
    component_maximums = np.full((component_count, 3), -np.inf)
    np.minimum.at(component_minimums, component_index, face_minimums)
    np.maximum.at(component_maximums, component_index, face_maximums)
    component_diagonals = np.linalg.norm(
        component_maximums - component_minimums,
        axis=1,
    )
    boundary_counts, non_manifold_counts = _edge_health_by_component(
        mesh,
        component_index,
        component_count,
    )

    debris_mask = (component_areas <= DEBRIS_MAX_AREA_MM2) & (
        (component_face_counts <= DEBRIS_MAX_TRIANGLES)
        | (component_diagonals <= DEBRIS_MAX_DIAGONAL_MM)
    )
    # The largest component is always retained, even for a deliberately tiny STL.
    debris_mask[int(np.argmax(component_areas))] = False
    meaningful_mask = ~debris_mask
    effective_face_mask = meaningful_mask[component_index]

    if np.all(effective_face_mask):
        effective_mesh = mesh
    else:
        effective_mesh = trimesh.Trimesh(
            vertices=np.asarray(mesh.vertices).copy(),
            faces=np.asarray(mesh.faces)[effective_face_mask].copy(),
            process=False,
        )
        effective_mesh.remove_unreferenced_vertices()

    effective_boundary_count = int(np.sum(boundary_counts[meaningful_mask]))
    effective_non_manifold_count = int(np.sum(non_manifold_counts[meaningful_mask]))
    meaningful_body_count = int(np.count_nonzero(meaningful_mask))
    debris_body_count = int(np.count_nonzero(debris_mask))

    effective_dimensions = np.asarray(effective_mesh.extents)
    if meaningful_body_count == 0 or np.any(effective_dimensions <= 1e-9):
        status = "INVALID"
    elif effective_boundary_count or effective_non_manifold_count or debris_body_count:
        status = "REPAIR_RECOMMENDED"
    else:
        status = "READY"

    if status == "INVALID":
        repairability = "MANUAL_REVIEW"
    elif status == "READY":
        repairability = "NOT_NEEDED"
    elif effective_non_manifold_count == 0 and effective_boundary_count <= 100:
        repairability = "LIKELY_AUTOMATIC"
    elif effective_non_manifold_count <= 100 and effective_boundary_count <= 1_000:
        repairability = "POSSIBLE"
    else:
        repairability = "MANUAL_REVIEW"

    if effective_boundary_count and effective_non_manifold_count:
        topology_status = "OPEN_AND_NON_MANIFOLD"
    elif effective_non_manifold_count:
        topology_status = "NON_MANIFOLD"
    elif effective_boundary_count:
        topology_status = "OPEN"
    else:
        topology_status = "WATERTIGHT"

    effective_area = float(effective_mesh.area)
    density, triangles_per_mm2 = _mesh_density(len(effective_mesh.faces), effective_area)
    body_order = np.argsort(component_areas)[::-1]
    body_details: list[BodyMetrics] = []
    for rank, component in enumerate(body_order[:MAX_REPORTED_BODIES], start=1):
        face_ids = np.flatnonzero(component_index == component)
        body_triangles = triangles[face_ids]
        points = body_triangles.reshape((-1, 3))
        dimensions = points.max(axis=0) - points.min(axis=0)
        watertight = bool(
            boundary_counts[component] == 0
            and non_manifold_counts[component] == 0
        )
        body_details.append(
            BodyMetrics(
                index=rank,
                triangle_count=int(component_face_counts[component]),
                surface_area_mm2=float(component_areas[component]),
                dimensions_mm=tuple(float(value) for value in dimensions),
                volume_mm3=_signed_mesh_volume(body_triangles) if watertight else None,
                boundary_edge_count=int(boundary_counts[component]),
                non_manifold_edge_count=int(non_manifold_counts[component]),
                is_watertight=watertight,
                is_debris=bool(debris_mask[component]),
            )
        )

    health = MeshHealth(
        status=status,
        topology_status=topology_status,
        repairability=repairability,
        mesh_density=density,
        triangles_per_mm2=triangles_per_mm2,
        total_body_count=component_count,
        meaningful_body_count=meaningful_body_count,
        debris_body_count=debris_body_count,
        boundary_edge_count=effective_boundary_count,
        non_manifold_edge_count=effective_non_manifold_count,
        raw_triangle_count=len(mesh.faces),
        ignored_triangle_count=int(np.sum(component_face_counts[debris_mask])),
        body_details_truncated=max(0, component_count - len(body_details)),
        bodies=tuple(body_details),
    )
    return health, effective_mesh


def _projected_xy_area(triangles: np.ndarray) -> np.ndarray:
    first = triangles[:, 1, :2] - triangles[:, 0, :2]
    second = triangles[:, 2, :2] - triangles[:, 0, :2]
    return np.abs(first[:, 0] * second[:, 1] - first[:, 1] * second[:, 0]) * 0.5


def _base_area(mesh: trimesh.Trimesh, height_mm: float) -> float:
    tolerance_mm = max(0.05, height_mm * 0.002)
    minimum_z = float(mesh.bounds[0, 2])
    triangles = np.asarray(mesh.triangles)
    on_bed = np.max(np.abs(triangles[:, :, 2] - minimum_z), axis=1) <= tolerance_mm
    if not np.any(on_bed):
        return 0.0
    return float(np.sum(_projected_xy_area(triangles[on_bed])))


def _overhang_area(mesh: trimesh.Trimesh, angle_deg: float) -> float:
    # Downward-facing surfaces steeper than `angle_deg` from vertical need support.
    downward_limit = -np.cos(np.deg2rad(angle_deg))
    tolerance_mm = max(0.05, float(mesh.extents[2]) * 0.002)
    above_bed = np.asarray(mesh.triangles_center)[:, 2] > float(mesh.bounds[0, 2]) + tolerance_mm
    mask = (np.asarray(mesh.face_normals)[:, 2] < downward_limit) & above_bed
    return float(np.sum(np.asarray(mesh.area_faces)[mask]))


def _load_stl(model_path: Path) -> trimesh.Trimesh:
    if model_path.suffix.lower() != ".stl":
        raise AnalysisError("only STL files are supported for geometry analysis")
    if not model_path.is_file():
        raise AnalysisError(f"file not found: {model_path}")

    try:
        validate_stl_file(model_path)
    except UnsafeInputError as exc:
        raise AnalysisError(f"unsafe or malformed STL: {exc}") from exc

    try:
        loaded = trimesh.load_mesh(model_path, file_type="stl", process=False)
    except Exception as exc:  # trimesh exposes loader-specific exceptions
        raise AnalysisError(f"cannot read STL: {exc}") from exc

    if isinstance(loaded, trimesh.Scene):
        meshes = [item for item in loaded.geometry.values() if isinstance(item, trimesh.Trimesh)]
        if not meshes:
            raise AnalysisError("STL contains no mesh geometry")
        loaded = trimesh.util.concatenate(meshes)

    if not isinstance(loaded, trimesh.Trimesh) or len(loaded.faces) == 0:
        raise AnalysisError("STL contains no triangles")
    if not np.all(np.isfinite(loaded.vertices)):
        raise AnalysisError("STL contains non-finite vertex coordinates")

    # Full trimesh.process(validate=True) pulls in optional SciPy through its
    # normal-repair path. Keep v0.2 truly limited to numpy + trimesh instead.
    loaded.update_faces(loaded.nondegenerate_faces())
    loaded.remove_unreferenced_vertices()
    loaded.merge_vertices()
    if len(loaded.faces) == 0:
        raise AnalysisError("STL contains no valid triangles")
    return loaded


def analyze_stl(
    path: str | Path,
    *,
    material: str = "PLA",
    overhang_angle_deg: float = 45.0,
    nozzle_diameter_mm: float = 0.4,
) -> AnalysisReport:
    """Analyze an STL in its current print orientation."""
    material_name = material.upper()
    if material_name not in MATERIALS:
        raise AnalysisError(f"unsupported material: {material}; choose PLA or PETG")
    if not any(
        abs(float(nozzle_diameter_mm) - supported) <= 1e-6
        for supported in SUPPORTED_NOZZLE_DIAMETERS_MM
    ):
        raise AnalysisError("unsupported nozzle; choose 0.2, 0.4, 0.6 or 0.8 mm")
    if not 1.0 <= overhang_angle_deg <= 89.0:
        raise AnalysisError("overhang angle must be between 1 and 89 degrees")

    model_path = Path(path).expanduser().resolve()
    raw_mesh = _load_stl(model_path)
    health, mesh = _analyze_mesh_health(raw_mesh)
    dimensions = tuple(float(value) for value in mesh.extents)
    width, depth, height = dimensions
    surface_area = float(mesh.area)
    watertight = health.boundary_edge_count == 0 and health.non_manifold_edge_count == 0
    volume = abs(float(mesh.volume)) if watertight else None
    body_count = health.meaningful_body_count
    base_area = _base_area(mesh, height)
    footprint = width * depth
    base_ratio = min(1.0, base_area / footprint) if footprint > 0 else 0.0
    overhang_area = _overhang_area(mesh, overhang_angle_deg)
    overhang_ratio = overhang_area / surface_area if surface_area > 0 else 0.0

    build = P1S.build_volume_mm
    fits = all(size <= limit + 1e-6 for size, limit in zip(dimensions, build))
    slenderness = height / max(width, depth, 1e-9)

    adhesion_score = (1.0 - base_ratio) + (0.35 if slenderness >= 2.5 else 0.0)
    adhesion_risk = _risk_level(adhesion_score, medium=0.75, high=1.15)
    overhang_risk = _risk_level(overhang_ratio, medium=0.05, high=0.20)
    tall_risk = _risk_level(slenderness, medium=2.5, high=5.0)
    supports = overhang_ratio >= 0.05
    brim = adhesion_risk != "LOW"

    profile = MATERIALS[material_name]
    layer_height = nozzle_diameter_mm * (0.4 if overhang_risk != "LOW" else 0.5)
    layer_height = round(layer_height, 3)
    wall_loops = 4 if tall_risk != "LOW" or adhesion_risk == "HIGH" else 3

    warnings: list[str] = []
    if not watertight:
        warnings.append(
            "Mesh is not watertight; volume is omitted and the STL may need repair."
        )
    if health.boundary_edge_count:
        warnings.append(f"Mesh has {health.boundary_edge_count} open boundary edges.")
    if health.non_manifold_edge_count:
        warnings.append(
            f"Mesh has {health.non_manifold_edge_count} non-manifold edges."
        )
    if health.debris_body_count:
        warnings.append(
            f"Detected {health.debris_body_count} tiny debris fragment(s); "
            "excluded from print metrics."
        )
    if body_count > 1:
        warnings.append(f"Model contains {body_count} meaningful disconnected bodies.")
    if health.mesh_density in {"HIGH", "VERY_HIGH"}:
        warnings.append(
            f"Mesh density is {health.mesh_density}; simplification may improve processing speed."
        )
    if not fits:
        warnings.append("Current orientation exceeds the 256 x 256 x 256 mm P1S build volume.")
    if base_area <= 1e-6:
        warnings.append("No planar bed-contact faces were detected at the model's minimum Z.")
    metrics = GeometryMetrics(
        dimensions_mm=dimensions,
        volume_mm3=volume,
        surface_area_mm2=surface_area,
        body_count=body_count,
        triangle_count=len(mesh.faces),
        vertex_count=len(mesh.vertices),
        is_watertight=watertight,
        base_area_mm2=base_area,
        base_area_ratio=base_ratio,
        overhang_area_mm2=overhang_area,
        overhang_area_ratio=overhang_ratio,
        overhang_angle_deg=overhang_angle_deg,
    )
    risks = RiskAssessment(
        bed_adhesion=adhesion_risk,
        overhang=overhang_risk,
        tall_object=tall_risk,
        support_requirement=overhang_risk,
    )
    purpose = _classify_model_purpose(mesh, volume)
    settings = PrintSettings(
        layer_height_mm=layer_height,
        wall_loops=wall_loops,
        top_layers=5,
        bottom_layers=4,
        supports=supports,
        brim=brim,
        nozzle_temperature_c=profile.nozzle_temperature_c,
        bed_temperature_c=profile.bed_temperature_c,
        fan_percent=profile.default_fan_percent,
        initial_layer_height_mm=round(nozzle_diameter_mm * 0.5, 3),
        line_width_mm=round(nozzle_diameter_mm * 1.05, 3),
        top_surface_line_width_mm=round(nozzle_diameter_mm, 3),
    )
    settings, _ = settings_for_material(
        settings,
        material_name,
        nozzle_diameter_mm=nozzle_diameter_mm,
    )
    surface_intelligence = analyze_surfaces(
        mesh,
        overhang_angle_deg=overhang_angle_deg,
    )
    geometry_features = analyze_geometry_features(
        mesh, nozzle_diameter_mm=nozzle_diameter_mm
    )
    support_exit_plan = plan_support_exit(
        mesh,
        geometry_features,
        overhang_angle_deg=overhang_angle_deg,
        layer_height_mm=settings.layer_height_mm,
    )
    functional_intent = infer_functional_intent(mesh, purpose)
    local_modifier_plan = build_local_modifier_plan(
        mesh,
        geometry_features,
        priority=settings.priority,
        base_layer_height_mm=settings.layer_height_mm,
        top_layers=settings.top_layers,
    )
    if any(item.severity == "HIGH" for item in geometry_features.bridges):
        warnings.append("Detected a long local bridge that requires constrained speed or support.")
    if any(item.severity == "HIGH" for item in geometry_features.thin_walls):
        warnings.append("Detected walls thinner than one nominal nozzle line.")
    if support_exit_plan.overall_risk == "HIGH":
        warnings.append("Some supports may be physically difficult to remove without surface damage.")
    return AnalysisReport(
        model_path=model_path,
        printer=P1S.name,
        material=material_name,
        fits_build_volume=fits,
        metrics=metrics,
        health=health,
        risks=risks,
        purpose=purpose,
        surface_intelligence=surface_intelligence,
        settings=settings,
        warnings=tuple(warnings),
        geometry_features=geometry_features,
        support_exit_plan=support_exit_plan,
        local_modifier_plan=local_modifier_plan,
        functional_intent=functional_intent,
    )
