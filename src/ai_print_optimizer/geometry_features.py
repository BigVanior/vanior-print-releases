"""Local geometry intelligence for bridges, thin walls and removable supports."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING, Any

import numpy as np
import trimesh

if TYPE_CHECKING:
    from .report import PrintSettings


@dataclass(frozen=True)
class BridgeRegion:
    index: int
    area_mm2: float
    estimated_span_mm: float
    z_min_mm: float
    z_max_mm: float
    direction_xy: tuple[float, float]
    severity: str


@dataclass(frozen=True)
class ThinWallRegion:
    index: int
    estimated_thickness_mm: float
    z_min_mm: float
    z_max_mm: float
    sampled_faces: int
    severity: str


@dataclass(frozen=True)
class GeometryFeatureAnalysis:
    version: int
    confidence: str
    nozzle_diameter_mm: float
    bridges: tuple[BridgeRegion, ...]
    thin_walls: tuple[ThinWallRegion, ...]
    bridge_area_ratio: float
    thin_wall_sample_ratio: float
    minimum_estimated_wall_mm: float | None
    recommendations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SupportExitRegion:
    index: int
    contact_area_mm2: float
    z_min_mm: float
    z_max_mm: float
    access_direction: str
    accessibility_score: float
    trapped_risk: str
    visible_surface_risk: str
    recommendation: str


@dataclass(frozen=True)
class SupportExitPlan:
    version: int
    overall_risk: str
    accessibility_score: float
    regions: tuple[SupportExitRegion, ...]
    recommended_strategy: str
    interface_gap_mm: float
    interface_spacing_mm: float
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LocalModifierRange:
    index: int
    z_min_mm: float
    z_max_mm: float
    role: str
    settings: dict[str, str]
    confidence: str
    reason: str


@dataclass(frozen=True)
class LocalModifierPlan:
    version: int
    mode: str
    ranges: tuple[LocalModifierRange, ...]
    protected_surface_ratio: float
    accelerated_hidden_ratio: float
    decisions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def retune_local_modifier_plan(
    plan: LocalModifierPlan | None,
    settings: PrintSettings | None,
) -> LocalModifierPlan | None:
    """Bind geometric roles to one candidate without erasing its speed budget."""
    if plan is None or settings is None:
        return plan
    finish_limits = {
        "quality": (80, 70),
        "balanced": (140, 120),
        "fast": (180, 150),
    }
    outer_limit, top_limit = finish_limits.get(settings.priority, (140, 120))
    tuned: list[LocalModifierRange] = []
    for item in plan.ranges:
        values: dict[str, str] = {"layer_height": f"{settings.layer_height_mm:g}"}
        if item.role == "detail-protected":
            target_layer = (
                min(settings.layer_height_mm, 0.12)
                if settings.priority == "quality"
                else min(settings.layer_height_mm, 0.16)
            )
            values.update(
                layer_height=f"{target_layer:g}",
                outer_wall_speed=str(min(settings.outer_wall_speed_mm_s, outer_limit)),
            )
        elif item.role == "bridge-protected":
            values.update(
                bridge_speed=str(min(settings.bridge_speed_mm_s, 45)),
                detect_thin_wall="1",
            )
        elif item.role == "top-protected":
            values.update(
                top_surface_speed=str(min(settings.top_surface_speed_mm_s, top_limit)),
                top_shell_layers=str(max(5, settings.top_layers)),
            )
        else:
            values.update(
                inner_wall_speed=str(settings.inner_wall_speed_mm_s),
                sparse_infill_speed=str(settings.sparse_infill_speed_mm_s),
            )
        tuned.append(replace(item, settings=values))
    return replace(
        plan,
        ranges=tuple(tuned),
        decisions=plan.decisions
        + (f"Диапазоны адаптированы к кандидату {settings.priority}.",),
    )


def _masked_components(mesh: trimesh.Trimesh, mask: np.ndarray) -> list[np.ndarray]:
    face_ids = np.flatnonzero(mask)
    if len(face_ids) == 0:
        return []
    membership = np.full(len(mesh.faces), -1, dtype=np.int64)
    membership[face_ids] = np.arange(len(face_ids), dtype=np.int64)
    parent = np.arange(len(face_ids), dtype=np.int64)

    def find(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = int(parent[value])
        return value

    for left, right in np.asarray(mesh.face_adjacency, dtype=np.int64):
        a = int(membership[int(left)])
        b = int(membership[int(right)])
        if a < 0 or b < 0:
            continue
        root_a = find(a)
        root_b = find(b)
        if root_a != root_b:
            parent[root_b] = root_a
    labels = np.fromiter((find(i) for i in range(len(face_ids))), dtype=np.int64)
    return [face_ids[labels == label] for label in np.unique(labels)]


def _severity(value: float, medium: float, high: float) -> str:
    if value >= high:
        return "HIGH"
    if value >= medium:
        return "MEDIUM"
    return "LOW"


def _bridge_regions(
    mesh: trimesh.Trimesh,
    *,
    nozzle_diameter_mm: float,
) -> tuple[tuple[BridgeRegion, ...], np.ndarray]:
    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    centers = np.asarray(mesh.triangles_center, dtype=np.float64)
    triangles = np.asarray(mesh.triangles, dtype=np.float64)
    minimum_z = float(mesh.bounds[0, 2])
    above_bed = centers[:, 2] > minimum_z + max(0.2, nozzle_diameter_mm)
    # Near-horizontal downward ceilings are bridges. Steeper faces remain
    # ordinary overhangs and are handled by the support planner.
    mask = (normals[:, 2] <= -0.72) & above_bed
    regions: list[BridgeRegion] = []
    for face_ids in _masked_components(mesh, mask):
        area = float(np.asarray(mesh.area_faces)[face_ids].sum())
        if area < nozzle_diameter_mm * nozzle_diameter_mm:
            continue
        points = triangles[face_ids].reshape((-1, 3))
        extents = np.ptp(points, axis=0)
        positive_xy = [float(value) for value in extents[:2] if value > 1e-5]
        span = min(positive_xy) if positive_xy else 0.0
        direction = (1.0, 0.0) if extents[0] >= extents[1] else (0.0, 1.0)
        regions.append(
            BridgeRegion(
                index=len(regions) + 1,
                area_mm2=area,
                estimated_span_mm=span,
                z_min_mm=float(points[:, 2].min()),
                z_max_mm=float(points[:, 2].max()),
                direction_xy=direction,
                severity=_severity(span, 6.0, 18.0),
            )
        )
    regions.sort(key=lambda item: item.area_mm2, reverse=True)
    regions = [
        BridgeRegion(index=index, **{k: v for k, v in asdict(item).items() if k != "index"})
        for index, item in enumerate(regions[:24], start=1)
    ]
    return tuple(regions), mask


def _thin_wall_samples(
    mesh: trimesh.Trimesh,
    *,
    nozzle_diameter_mm: float,
    sample_limit: int = 2400,
) -> tuple[np.ndarray, np.ndarray]:
    triangles = np.asarray(mesh.triangles, dtype=np.float64)
    centers_all = np.asarray(mesh.triangles_center, dtype=np.float64)
    normals_all = np.asarray(mesh.face_normals, dtype=np.float64)
    if len(centers_all) == 0:
        return np.empty(0, dtype=np.int64), np.empty(0, dtype=np.float64)
    if len(centers_all) > sample_limit:
        ids = np.linspace(0, len(centers_all) - 1, sample_limit, dtype=np.int64)
    else:
        ids = np.arange(len(centers_all), dtype=np.int64)
    centers = centers_all[ids]
    normals = normals_all[ids]
    threshold = max(0.72, nozzle_diameter_mm * 1.8)

    # For ordinary-sized meshes, measure the real distance to the opposite
    # surface with ray/triangle intersections.  The former centre-to-centre
    # approximation missed broad thin plates because the centres of the two
    # triangulated sides are laterally offset even though the walls overlap.
    if len(triangles) <= 12_000:
        edge1 = triangles[:, 1] - triangles[:, 0]
        edge2 = triangles[:, 2] - triangles[:, 0]
        vertex0 = triangles[:, 0]
        minimum_distance = max(0.04, nozzle_diameter_mm * 0.12)
        found_ids: list[int] = []
        found_thickness: list[float] = []
        for start in range(0, len(ids), 32):
            batch_ids = ids[start : start + 32]
            origins = centers_all[batch_ids]
            directions = -normals_all[batch_ids]
            h = np.cross(directions[:, None, :], edge2[None, :, :])
            determinant = np.einsum("tj,btj->bt", edge1, h)
            valid = np.abs(determinant) > 1e-10
            inverse = np.divide(
                1.0,
                determinant,
                out=np.zeros_like(determinant),
                where=valid,
            )
            delta = origins[:, None, :] - vertex0[None, :, :]
            u = inverse * np.einsum("btj,btj->bt", delta, h)
            q = np.cross(delta, edge1[None, :, :])
            v = inverse * np.einsum("bj,btj->bt", directions, q)
            distance = inverse * np.einsum("tj,btj->bt", edge2, q)
            valid &= (u >= -1e-8) & (v >= -1e-8) & (u + v <= 1.0 + 1e-8)
            valid &= (distance >= minimum_distance) & (distance <= threshold)
            valid[np.arange(len(batch_ids)), batch_ids] = False
            candidates = np.where(valid, distance, np.inf)
            nearest = candidates.min(axis=1)
            for offset in np.flatnonzero(np.isfinite(nearest)):
                found_ids.append(int(batch_ids[int(offset)]))
                found_thickness.append(float(nearest[int(offset)]))
        return (
            np.asarray(found_ids, dtype=np.int64),
            np.asarray(found_thickness, dtype=np.float64),
        )

    # Very large meshes use the bounded approximation to keep analysis time
    # and memory predictable.
    found_ids: list[int] = []
    found_thickness: list[float] = []
    for start in range(0, len(ids), 96):
        local_centers = centers[start : start + 96]
        local_normals = normals[start : start + 96]
        delta = centers[None, :, :] - local_centers[:, None, :]
        inward = -local_normals
        projected = np.einsum("ijk,ik->ij", delta, inward)
        distance_sq = np.einsum("ijk,ijk->ij", delta, delta)
        lateral_sq = np.maximum(0.0, distance_sq - projected * projected)
        opposing = local_normals @ normals.T <= -0.55
        lateral_limit = np.maximum(nozzle_diameter_mm * 0.65, projected * 0.45)
        valid = (
            opposing
            & (projected >= max(0.04, nozzle_diameter_mm * 0.12))
            & (projected <= threshold)
            & (lateral_sq <= lateral_limit * lateral_limit)
        )
        candidates = np.where(valid, projected, np.inf)
        minimum = candidates.min(axis=1)
        selected = np.isfinite(minimum)
        for offset in np.flatnonzero(selected):
            found_ids.append(int(ids[start + int(offset)]))
            found_thickness.append(float(minimum[int(offset)]))
    return np.asarray(found_ids, dtype=np.int64), np.asarray(found_thickness, dtype=np.float64)


def _thin_wall_regions(
    mesh: trimesh.Trimesh,
    *,
    nozzle_diameter_mm: float,
) -> tuple[tuple[ThinWallRegion, ...], float, float | None]:
    face_ids, thickness = _thin_wall_samples(mesh, nozzle_diameter_mm=nozzle_diameter_mm)
    if len(face_ids) == 0:
        return (), 0.0, None
    centers = np.asarray(mesh.triangles_center, dtype=np.float64)[face_ids]
    z_min = float(mesh.bounds[0, 2])
    height = max(float(mesh.extents[2]), nozzle_diameter_mm)
    bin_size = max(nozzle_diameter_mm * 2.0, height / 18.0)
    bins = np.floor((centers[:, 2] - z_min) / bin_size).astype(np.int64)
    regions: list[ThinWallRegion] = []
    for label in np.unique(bins):
        selected = bins == label
        minimum = float(thickness[selected].min())
        regions.append(
            ThinWallRegion(
                index=len(regions) + 1,
                estimated_thickness_mm=minimum,
                z_min_mm=float(centers[selected, 2].min()),
                z_max_mm=float(centers[selected, 2].max()),
                sampled_faces=int(np.count_nonzero(selected)),
                severity=(
                    "HIGH"
                    if minimum < nozzle_diameter_mm
                    else "MEDIUM"
                    if minimum < nozzle_diameter_mm * 1.5
                    else "LOW"
                ),
            )
        )
    regions.sort(key=lambda item: (item.estimated_thickness_mm, -item.sampled_faces))
    ratio = len(face_ids) / min(len(mesh.faces), 2400)
    return tuple(regions[:24]), float(ratio), float(thickness.min())


def analyze_geometry_features(
    mesh: trimesh.Trimesh,
    *,
    nozzle_diameter_mm: float = 0.4,
) -> GeometryFeatureAnalysis:
    """Detect local bridge ceilings and wall pairs narrower than two lines."""
    bridges, bridge_mask = _bridge_regions(mesh, nozzle_diameter_mm=nozzle_diameter_mm)
    thin_walls, thin_ratio, minimum_wall = _thin_wall_regions(
        mesh, nozzle_diameter_mm=nozzle_diameter_mm
    )
    total_area = max(float(mesh.area), 1e-9)
    bridge_ratio = float(np.asarray(mesh.area_faces)[bridge_mask].sum() / total_area)
    recommendations: list[str] = []
    if bridges:
        longest = max(item.estimated_span_mm for item in bridges)
        recommendations.append(
            f"Обнаружено мостов: {len(bridges)}; максимальный расчётный пролёт {longest:.1f} мм."
        )
    if thin_walls:
        recommendations.append(
            f"Тонкие области требуют Arachne; минимальная оценка {minimum_wall:.2f} мм."
        )
    if not recommendations:
        recommendations.append("Опасные локальные мосты и тонкие стенки не обнаружены.")
    confidence = "HIGH" if mesh.is_watertight and len(mesh.faces) >= 500 else "MEDIUM"
    return GeometryFeatureAnalysis(
        version=2,
        confidence=confidence,
        nozzle_diameter_mm=nozzle_diameter_mm,
        bridges=bridges,
        thin_walls=thin_walls,
        bridge_area_ratio=bridge_ratio,
        thin_wall_sample_ratio=thin_ratio,
        minimum_estimated_wall_mm=minimum_wall,
        recommendations=tuple(recommendations),
    )


def plan_support_exit(
    mesh: trimesh.Trimesh,
    features: GeometryFeatureAnalysis,
    *,
    overhang_angle_deg: float = 45.0,
    layer_height_mm: float = 0.2,
) -> SupportExitPlan:
    """Estimate whether generated supports have a physical removal path."""
    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    centers = np.asarray(mesh.triangles_center, dtype=np.float64)
    triangles = np.asarray(mesh.triangles, dtype=np.float64)
    minimum_z = float(mesh.bounds[0, 2])
    mask = (
        (normals[:, 2] < -np.cos(np.deg2rad(overhang_angle_deg)))
        & (centers[:, 2] > minimum_z + max(layer_height_mm, 0.1))
    )
    bounds = np.asarray(mesh.bounds, dtype=np.float64)
    model_xy = np.maximum(bounds[1, :2] - bounds[0, :2], 1e-6)
    regions: list[SupportExitRegion] = []
    for face_ids in _masked_components(mesh, mask):
        area = float(np.asarray(mesh.area_faces)[face_ids].sum())
        if area < 0.5:
            continue
        points = triangles[face_ids].reshape((-1, 3))
        center = points.mean(axis=0)
        side_distances = np.asarray(
            [
                center[0] - bounds[0, 0],
                bounds[1, 0] - center[0],
                center[1] - bounds[0, 1],
                bounds[1, 1] - center[1],
            ]
        )
        directions = ("−X", "+X", "−Y", "+Y")
        nearest = int(np.argmin(side_distances))
        normalized_depth = float(side_distances[nearest] / max(model_xy.max(), 1e-6))
        enclosed = normalized_depth > 0.12 and center[2] < bounds[0, 2] + mesh.extents[2] * 0.72
        score = 100.0 - min(65.0, normalized_depth * 240.0) - min(20.0, area / 25.0)
        if enclosed:
            score -= 15.0
        score = float(np.clip(score, 0.0, 100.0))
        trapped = "HIGH" if score < 35 else "MEDIUM" if score < 65 else "LOW"
        visible = "HIGH" if center[2] >= bounds[0, 2] + mesh.extents[2] * 0.35 else "MEDIUM"
        access_direction = "вниз" if not enclosed else directions[nearest]
        if trapped == "HIGH":
            recommendation = "Локальный support enforcer с минимальным интерфейсом и ручной проверкой доступа."
        elif trapped == "MEDIUM":
            recommendation = "Древовидная поддержка с направлением удаления " + access_direction + "."
        else:
            recommendation = "Поддержка доступна; использовать отделяемый интерфейс."
        regions.append(
            SupportExitRegion(
                index=len(regions) + 1,
                contact_area_mm2=area,
                z_min_mm=float(points[:, 2].min()),
                z_max_mm=float(points[:, 2].max()),
                access_direction=access_direction,
                accessibility_score=round(score, 2),
                trapped_risk=trapped,
                visible_surface_risk=visible,
                recommendation=recommendation,
            )
        )
    regions.sort(key=lambda item: (item.accessibility_score, -item.contact_area_mm2))
    regions = [
        SupportExitRegion(index=index, **{k: v for k, v in asdict(item).items() if k != "index"})
        for index, item in enumerate(regions[:24], start=1)
    ]
    overall_score = min((item.accessibility_score for item in regions), default=100.0)
    overall = "HIGH" if overall_score < 35 else "MEDIUM" if overall_score < 65 else "LOW"
    strategy = "tree" if regions and overall != "LOW" else "normal" if regions else "none"
    reasons = [
        f"Проанализировано зон контакта поддержек: {len(regions)}.",
        f"Минимальная доступность удаления: {overall_score:.0f}/100.",
    ]
    if overall == "HIGH":
        reasons.append("Есть риск запертой поддержки; требуется особенно малый контакт.")
    return SupportExitPlan(
        version=1,
        overall_risk=overall,
        accessibility_score=round(overall_score, 2),
        regions=tuple(regions),
        recommended_strategy=strategy,
        interface_gap_mm=max(0.20, layer_height_mm),
        interface_spacing_mm=0.45 if overall != "LOW" else 0.40,
        reasons=tuple(reasons),
    )


def build_local_modifier_plan(
    mesh: trimesh.Trimesh,
    features: GeometryFeatureAnalysis,
    *,
    priority: str,
    base_layer_height_mm: float,
    top_layers: int,
) -> LocalModifierPlan:
    """Build real Bambu height-range modifiers from local surface roles."""
    centers = np.asarray(mesh.triangles_center, dtype=np.float64)
    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    areas = np.asarray(mesh.area_faces, dtype=np.float64)
    if len(centers) == 0:
        return LocalModifierPlan(1, "height-ranges", (), 0.0, 0.0, ())
    z0 = float(mesh.bounds[0, 2])
    z1 = float(mesh.bounds[1, 2])
    height = max(z1 - z0, base_layer_height_mm)
    bin_count = int(np.clip(round(height / max(4.0, base_layer_height_mm * 24)), 4, 18))
    # Bambu range coordinates are heights above the object's bottom, not raw
    # source-mesh Z coordinates (which may be centred around zero).
    edges = np.linspace(0.0, height + 1e-6, bin_count + 1)
    curved_mask = np.max(np.abs(normals), axis=1) < 0.94
    top_mask = normals[:, 2] >= 0.82
    precision_mask = np.abs(normals[:, 2]) <= 0.22
    bridge_bands = [
        (item.z_min_mm - z0, item.z_max_mm - z0) for item in features.bridges
    ]
    ranges: list[LocalModifierRange] = []
    protected_area = 0.0
    accelerated_area = 0.0
    total_area = max(float(areas.sum()), 1e-9)
    for index in range(bin_count):
        low = float(edges[index])
        high = float(edges[index + 1])
        absolute_low = low + z0
        absolute_high = high + z0
        selected = (centers[:, 2] >= absolute_low) & (
            centers[:, 2] <= absolute_high
            if index == bin_count - 1
            else centers[:, 2] < absolute_high
        )
        area = float(areas[selected].sum())
        if area <= 1e-8:
            continue
        curved = float(areas[selected & curved_mask].sum() / area)
        top = float(areas[selected & top_mask].sum() / area)
        precision = float(areas[selected & precision_mask].sum() / area)
        bridge = any(not (maximum < low or minimum > high) for minimum, maximum in bridge_bands)
        # Bambu Studio's layer-range model config must always carry an explicit
        # layer_height.  Other object overrides without it are accepted by the
        # XML reader but crash the CLI during slicing in current releases.
        settings: dict[str, str] = {"layer_height": f"{base_layer_height_mm:g}"}
        role = "hidden-efficient"
        reason = "Скрытый или простой диапазон допускает ускорение внутренних линий."
        confidence = "MEDIUM"
        if bridge:
            role = "bridge-protected"
            settings.update(bridge_speed="35", detect_thin_wall="1")
            reason = "Диапазон содержит мост; поток и скорость ограничены против провисания."
            protected_area += area
            confidence = features.confidence
        elif curved + precision >= 0.48:
            role = "detail-protected"
            target_layer = 0.12 if priority == "quality" else min(0.16, base_layer_height_mm)
            settings.update(layer_height=f"{target_layer:g}", outer_wall_speed="90")
            reason = "В диапазоне преобладают криволинейные или размерно-критичные поверхности."
            protected_area += area
            confidence = "HIGH"
        elif top >= 0.22:
            role = "top-protected"
            settings.update(top_surface_speed="70", top_shell_layers=str(max(6, top_layers)))
            reason = "В диапазоне сосредоточены видимые верхние поверхности."
            protected_area += area
            confidence = "HIGH"
        else:
            settings.update(inner_wall_speed="340", sparse_infill_speed="330")
            accelerated_area += area
        ranges.append(
            LocalModifierRange(
                index=len(ranges) + 1,
                z_min_mm=round(low, 4),
                z_max_mm=round(high, 4),
                role=role,
                settings=settings,
                confidence=confidence,
                reason=reason,
            )
        )
    decisions = (
        f"Локальных диапазонов: {len(ranges)}.",
        f"Защищено поверхностей: {protected_area / total_area * 100:.1f}%.",
        f"Ускоряемых диапазонов: {accelerated_area / total_area * 100:.1f}%.",
    )
    return LocalModifierPlan(
        version=1,
        mode="height-ranges",
        ranges=tuple(ranges),
        protected_surface_ratio=float(protected_area / total_area),
        accelerated_hidden_ratio=float(accelerated_area / total_area),
        decisions=decisions,
    )
