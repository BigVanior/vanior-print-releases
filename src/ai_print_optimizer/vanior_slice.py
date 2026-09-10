"""Independent one-material slicing core developed for VANIOR PRINT.

This module deliberately shares no Bambu Studio process, profile or project
format.  It evaluates no-support, normal and tree support plans itself and
publishes only the selected, audited toolpath. Unsupported jobs fail closed
instead of producing speculative printer output.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from itertools import pairwise
from pathlib import Path
from threading import Event

import numpy as np
import trimesh
from shapely import make_valid
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPolygon,
    Polygon,
)
from shapely.geometry.base import BaseGeometry
from shapely.ops import linemerge, unary_union
from trimesh.intersections import mesh_plane

from .gcode_audit import GCodeAudit, audit_gcode
from .input_safety import UnsafeInputError, validate_stl_file
from .io_utils import atomic_write_new_text
from .progress import ProgressCallback, check_cancelled, emit_progress
from .report import PrintSettings


class VaniorSliceError(RuntimeError):
    """Raised when the independent engine cannot safely slice a model."""


@dataclass(frozen=True)
class VaniorMachineProfile:
    name: str
    build_size_mm: tuple[float, float, float]
    printable_margin_mm: float = 5.0


@dataclass(frozen=True)
class VaniorSliceResult:
    engine: str
    engine_stage: str
    source_path: Path
    gcode_path: Path
    layer_count: int
    line_count: int
    extrusion_length_mm: float
    estimated_mass_g: float
    estimated_print_time_s: float
    bounds_mm: tuple[float, float, float]
    support_strategy: str
    support_extrusion_length_mm: float
    estimated_support_mass_g: float
    estimated_support_time_s: float
    support_candidates: tuple[dict[str, object], ...]
    support_recommendation_reason: str
    audit: GCodeAudit

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["source_path"] = str(self.source_path)
        payload["gcode_path"] = str(self.gcode_path)
        payload["audit"] = self.audit.to_dict()
        return payload


P1S_INDEPENDENT = VaniorMachineProfile(
    name="Bambu Lab P1S (совместимый профиль VANIOR)",
    build_size_mm=(256.0, 256.0, 256.0),
)

_MATERIAL_DENSITY_G_CM3 = {"PLA": 1.24, "PETG": 1.27}
_DEFAULT_BRIM_WIDTH_MM = 5.0
_SUPPORT_STRATEGIES = {"auto", "none", "normal", "tree"}
_XY_PATH_RESOLUTION_MM = 0.012


def _simplify_toolpath(
    path: Sequence[tuple[float, float]],
    *,
    tolerance_mm: float = _XY_PATH_RESOLUTION_MM,
) -> list[tuple[float, float]]:
    """Remove sub-resolution mesh tessellation from one printable path."""
    points = list(path)
    if len(points) < 3 or tolerance_mm <= 0:
        return points
    closed = points[0] == points[-1]
    simplified = [
        (float(x), float(y))
        for x, y in LineString(points).simplify(
            tolerance_mm, preserve_topology=False
        ).coords
    ]
    if closed and simplified and simplified[0] != simplified[-1]:
        simplified.append(simplified[0])
    return simplified if len(simplified) >= 2 else points


def _motion_time_s(length_mm: float, speed_mm_s: float, acceleration_mm_s2: float) -> float:
    """Conservative stop-to-stop trapezoidal motion estimate for one move."""
    length = max(0.0, float(length_mm))
    speed = max(1.0, float(speed_mm_s))
    acceleration = max(100.0, float(acceleration_mm_s2))
    distance_to_speed = speed * speed / acceleration
    if length <= distance_to_speed:
        return 2.0 * math.sqrt(length / acceleration)
    return 2.0 * speed / acceleration + (length - distance_to_speed) / speed


def _iter_lines(geometry: BaseGeometry) -> Iterator[LineString]:
    if geometry.is_empty:
        return
    if isinstance(geometry, LineString):
        yield geometry
        return
    if isinstance(geometry, (MultiLineString, GeometryCollection)):
        for child in geometry.geoms:
            yield from _iter_lines(child)


def _iter_polygons(geometry: BaseGeometry) -> Iterator[Polygon]:
    if geometry.is_empty:
        return
    if isinstance(geometry, Polygon):
        yield geometry
        return
    if isinstance(geometry, (MultiPolygon, GeometryCollection)):
        for child in geometry.geoms:
            yield from _iter_polygons(child)


def _closed_rings(linework: BaseGeometry) -> Iterator[Polygon]:
    merged = linemerge(linework)
    for line in _iter_lines(merged):
        if not line.is_closed or len(line.coords) < 4:
            continue
        polygon = make_valid(Polygon(line.coords))
        for item in _iter_polygons(polygon):
            if item.area > 1e-6:
                yield item


def _slice_region(mesh: trimesh.Trimesh, z_mm: float) -> BaseGeometry:
    segments = mesh_plane(
        mesh=mesh,
        plane_normal=np.array([0.0, 0.0, 1.0]),
        plane_origin=np.array([0.0, 0.0, z_mm]),
    )
    if len(segments) == 0:
        return GeometryCollection()
    lines = [
        LineString(
            (
                (round(float(segment[0, 0]), 6), round(float(segment[0, 1]), 6)),
                (round(float(segment[1, 0]), 6), round(float(segment[1, 1]), 6)),
            )
        )
        for segment in segments
        if np.linalg.norm(segment[1, :2] - segment[0, :2]) > 1e-7
    ]
    if not lines:
        return GeometryCollection()
    noded = unary_union(lines)
    # Cross-section contours obey the even/odd fill rule. Symmetric difference
    # preserves holes and nested islands without relying on mesh winding order.
    region: BaseGeometry = GeometryCollection()
    for ring in _closed_rings(noded):
        region = region.symmetric_difference(ring)
    region = make_valid(region)
    polygons = [item for item in _iter_polygons(region) if item.area > 0.01]
    return unary_union(polygons) if polygons else GeometryCollection()


def _contour_paths(geometry: BaseGeometry) -> list[list[tuple[float, float]]]:
    paths: list[list[tuple[float, float]]] = []
    for polygon in _iter_polygons(geometry):
        paths.append([(float(x), float(y)) for x, y in polygon.exterior.coords])
        for interior in polygon.interiors:
            paths.append([(float(x), float(y)) for x, y in interior.coords])
    return paths


def _brim_paths(
    region: BaseGeometry,
    *,
    line_width_mm: float,
    width_mm: float = _DEFAULT_BRIM_WIDTH_MM,
) -> list[list[tuple[float, float]]]:
    """Create concentric first-layer paths outside the model footprint."""
    paths: list[list[tuple[float, float]]] = []
    offset = line_width_mm * 0.5
    while offset <= width_mm + 1e-9:
        expanded = make_valid(region.buffer(offset))
        for polygon in _iter_polygons(expanded):
            paths.append(
                [(float(x), float(y)) for x, y in polygon.exterior.coords]
            )
        offset += line_width_mm
    return paths


def _line_paths(geometry: BaseGeometry) -> list[list[tuple[float, float]]]:
    return [
        [(float(x), float(y)) for x, y in line.coords]
        for line in _iter_lines(geometry)
        if line.length > 1e-5
    ]


def _infill_paths(
    region: BaseGeometry,
    *,
    spacing_mm: float,
    angle_degrees: int,
) -> list[list[tuple[float, float]]]:
    if region.is_empty or spacing_mm <= 0:
        return []
    min_x, min_y, max_x, max_y = region.bounds
    diagonal = math.hypot(max_x - min_x, max_y - min_y) + 10.0
    center_x = (min_x + max_x) * 0.5
    center_y = (min_y + max_y) * 0.5
    radians = math.radians(angle_degrees)
    direction = (math.cos(radians), math.sin(radians))
    normal = (-direction[1], direction[0])
    count = math.ceil(diagonal / spacing_mm) + 2
    paths: list[list[tuple[float, float]]] = []
    reverse = False
    for index in range(-count, count + 1):
        offset = index * spacing_mm
        cx = center_x + normal[0] * offset
        cy = center_y + normal[1] * offset
        line = LineString(
            (
                (cx - direction[0] * diagonal, cy - direction[1] * diagonal),
                (cx + direction[0] * diagonal, cy + direction[1] * diagonal),
            )
        )
        for path in _line_paths(region.intersection(line)):
            if reverse:
                path.reverse()
            paths.append(path)
            reverse = not reverse
    return paths


def _solid_mask(
    regions: Sequence[BaseGeometry],
    index: int,
    *,
    top_layers: int,
    bottom_layers: int,
    tolerance_mm: float,
) -> BaseGeometry:
    current = regions[index]
    solid: BaseGeometry = GeometryCollection()
    for probe in range(index, min(len(regions), index + max(1, top_layers))):
        above = regions[probe + 1] if probe + 1 < len(regions) else GeometryCollection()
        exposure = regions[probe].difference(above.buffer(tolerance_mm))
        solid = solid.union(exposure.intersection(current))
    for probe in range(index, max(-1, index - max(1, bottom_layers)), -1):
        below = regions[probe - 1] if probe > 0 else GeometryCollection()
        exposure = regions[probe].difference(below.buffer(tolerance_mm))
        solid = solid.union(exposure.intersection(current))
    return make_valid(solid)


def _support_regions(
    regions: Sequence[BaseGeometry],
    settings: PrintSettings,
    *,
    overhang_angle_deg: float,
    contacts: Sequence[tuple[int, Polygon]] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> tuple[list[BaseGeometry], list[BaseGeometry]]:
    """Project contacts down in one descending sweep.

    The previous implementation descended once for every overhang island.  A
    tall detailed model could therefore perform hundreds of thousands of
    increasingly large polygon unions while the UI remained at 44 percent.
    Merging contacts per layer and carrying one active footprint makes the
    operation scale approximately with the layer count instead.
    """
    interface_layers = max(0, int(settings.support_interface_top_layers))
    xy_gap = max(0.0, float(settings.support_object_xy_distance_mm))
    minimum_area = max(0.04, float(settings.line_width_mm) ** 2)
    supports: list[BaseGeometry] = [GeometryCollection() for _ in regions]
    interfaces: list[BaseGeometry] = [GeometryCollection() for _ in regions]
    resolved_contacts = list(contacts) if contacts is not None else _support_islands(
        regions,
        settings,
        overhang_angle_deg=overhang_angle_deg,
        cancel_event=cancel_event,
    )
    grouped = _group_support_contacts(resolved_contacts, float(settings.line_width_mm))
    active: BaseGeometry = GeometryCollection()
    last_progress = -1
    for completed, layer_index in enumerate(range(len(regions) - 1, -1, -1), 1):
        check_cancelled(cancel_event)
        new_contact = grouped.get(layer_index)
        if new_contact is not None:
            active = make_valid(active.union(new_contact))
        if not active.is_empty:
            active = _clip_support_to_free_space(
                active,
                regions[layer_index],
                xy_gap=xy_gap,
                minimum_area=minimum_area,
            )
            supports[layer_index] = active
            if interface_layers > 0 and not active.is_empty:
                interface_sources = [
                    grouped[probe]
                    for probe in range(
                        layer_index,
                        min(len(regions), layer_index + interface_layers),
                    )
                    if probe in grouped
                ]
                if interface_sources:
                    interfaces[layer_index] = make_valid(
                        active.intersection(unary_union(interface_sources))
                    )
        progress = 47 + int(3 * completed / max(1, len(regions)))
        if progress != last_progress:
            emit_progress(
                progress_callback,
                "vanior-supports-normal",
                f"Обычные поддержки: {completed}/{len(regions)} слоёв",
                progress,
            )
            last_progress = progress
    return supports, interfaces


def _support_islands(
    regions: Sequence[BaseGeometry],
    settings: PrintSettings,
    *,
    overhang_angle_deg: float,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> list[tuple[int, Polygon]]:
    """Return printable overhang contacts shared by every support strategy."""
    layer_height = float(settings.layer_height_mm)
    if not 1.0 <= overhang_angle_deg <= 89.0:
        raise VaniorSliceError("Угол поддержек должен находиться в диапазоне 1–89°.")
    allowed_step = layer_height / max(
        math.tan(math.radians(overhang_angle_deg)), 1e-6
    )
    gap_layers = max(
        0, math.ceil(float(settings.support_top_z_distance_mm) / layer_height)
    )
    minimum_area = max(0.04, float(settings.line_width_mm) ** 2)
    contacts: list[tuple[int, Polygon]] = []
    last_progress = -1
    for upper_index in range(1, len(regions)):
        check_cancelled(cancel_event)
        unsupported = make_valid(
            regions[upper_index].difference(
                regions[upper_index - 1].buffer(allowed_step)
            )
        )
        unsupported = make_valid(
            unsupported.buffer(-float(settings.line_width_mm) * 0.15)
        )
        contact_layer = upper_index - gap_layers
        if contact_layer < 0:
            continue
        contacts.extend(
            (contact_layer, polygon)
            for polygon in _iter_polygons(unsupported)
            if polygon.area >= minimum_area
        )
        progress = 44 + int(3 * upper_index / max(1, len(regions) - 1))
        if progress != last_progress:
            emit_progress(
                progress_callback,
                "vanior-support-contacts",
                f"Поиск нависаний: {upper_index}/{len(regions) - 1} слоёв",
                progress,
            )
            last_progress = progress
    return contacts


def _group_support_contacts(
    contacts: Sequence[tuple[int, Polygon]],
    line_width: float,
) -> dict[int, BaseGeometry]:
    """Merge contact islands at the same height before vertical propagation."""
    buckets: dict[int, list[Polygon]] = {}
    for layer_index, polygon in contacts:
        buckets.setdefault(layer_index, []).append(polygon)
    return {
        layer_index: make_valid(unary_union(polygons).buffer(line_width * 0.35))
        for layer_index, polygons in buckets.items()
    }


def _clip_support_to_free_space(
    support: BaseGeometry,
    model_region: BaseGeometry,
    *,
    xy_gap: float,
    minimum_area: float,
) -> BaseGeometry:
    free_space = make_valid(support.difference(model_region.buffer(xy_gap)))
    parts = [
        polygon
        for polygon in _iter_polygons(free_space)
        if polygon.area >= minimum_area
    ]
    return make_valid(unary_union(parts)) if parts else GeometryCollection()


def _tree_support_regions(
    regions: Sequence[BaseGeometry],
    settings: PrintSettings,
    *,
    overhang_angle_deg: float,
    contacts: Sequence[tuple[int, Polygon]] | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> tuple[list[BaseGeometry], list[BaseGeometry]]:
    """Build bounded-cost tapered branches in one descending layer sweep."""
    line_width = float(settings.line_width_mm)
    interface_layers = max(1, int(settings.support_interface_top_layers))
    xy_gap = max(0.0, float(settings.support_object_xy_distance_mm))
    minimum_area = max(0.04, line_width**2)
    branch_radius = max(0.9, line_width * 2.25)
    lean_step = min(branch_radius * 0.18, float(settings.layer_height_mm) * 0.65)
    supports: list[BaseGeometry] = [GeometryCollection() for _ in regions]
    interfaces: list[BaseGeometry] = [GeometryCollection() for _ in regions]
    resolved_contacts = list(contacts) if contacts is not None else _support_islands(
        regions,
        settings,
        overhang_angle_deg=overhang_angle_deg,
        cancel_event=cancel_event,
    )
    grouped = _group_support_contacts(resolved_contacts, line_width)
    active: BaseGeometry = GeometryCollection()
    last_progress = -1
    for completed, layer_index in enumerate(range(len(regions) - 1, -1, -1), 1):
        check_cancelled(cancel_event)
        new_contact = grouped.get(layer_index)
        if new_contact is not None:
            active = make_valid(active.union(new_contact))
        if not active.is_empty:
            printable = _clip_support_to_free_space(
                active,
                regions[layer_index],
                xy_gap=xy_gap,
                minimum_area=minimum_area,
            )
            supports[layer_index] = printable
            interface_sources = [
                grouped[probe]
                for probe in range(
                    layer_index,
                    min(len(regions), layer_index + interface_layers),
                )
                if probe in grouped
            ]
            if interface_sources and not printable.is_empty:
                interfaces[layer_index] = make_valid(
                    printable.intersection(unary_union(interface_sources))
                )
            tapered_parts: list[BaseGeometry] = []
            for polygon in _iter_polygons(printable):
                tapered = make_valid(polygon.buffer(-lean_step))
                if tapered.is_empty or tapered.area < minimum_area:
                    tapered = polygon.representative_point().buffer(
                        branch_radius, quad_segs=6
                    )
                tapered_parts.append(tapered)
            active = (
                make_valid(unary_union(tapered_parts))
                if tapered_parts
                else GeometryCollection()
            )
        progress = 50 + int(4 * completed / max(1, len(regions)))
        if progress != last_progress:
            emit_progress(
                progress_callback,
                "vanior-supports-tree",
                f"Древовидные поддержки: {completed}/{len(regions)} слоёв",
                progress,
            )
            last_progress = progress
    return supports, interfaces


def _paths_length(paths: Sequence[Sequence[tuple[float, float]]]) -> float:
    return sum(
        math.hypot(x2 - x1, y2 - y1)
        for path in paths
        for (x1, y1), (x2, y2) in pairwise(path)
    )


def _support_paths_for_layer(
    body: BaseGeometry,
    interface: BaseGeometry,
    *,
    strategy: str,
    line_width: float,
    interface_spacing: float,
    layer_index: int,
) -> tuple[list[list[tuple[float, float]]], list[list[tuple[float, float]]]]:
    body_paths = _infill_paths(
        body,
        spacing_mm=max(line_width * (2.8 if strategy == "tree" else 4.0), 1.2 if strategy == "tree" else 1.6),
        angle_degrees=0 if layer_index % 2 == 0 else 90,
    )
    if strategy == "tree" and not body.is_empty:
        body_paths = _contour_paths(make_valid(body.buffer(-line_width * 0.5))) + body_paths
    interface_paths = _infill_paths(
        interface,
        spacing_mm=max(line_width, interface_spacing),
        angle_degrees=45 if layer_index % 2 == 0 else 135,
    )
    return body_paths, interface_paths


def _support_candidate(
    strategy: str,
    support_regions: Sequence[BaseGeometry],
    interfaces: Sequence[BaseGeometry],
    settings: PrintSettings,
    *,
    demand_area_mm2: float,
    material_density_g_cm3: float,
) -> dict[str, object]:
    """Estimate a support plan from its printable area without full toolpaths.

    Exact paths are generated later only for the selected strategy.  Candidate
    comparison therefore stays responsive even for very tall models.
    """
    line_width = float(settings.line_width_mm)
    body_length = 0.0
    interface_length = 0.0
    body_seconds = 0.0
    interface_seconds = 0.0
    retraction_seconds = 0.0
    for layer_index, support in enumerate(support_regions):
        interface = interfaces[layer_index]
        body = make_valid(support.difference(interface))
        body_spacing = max(
            line_width * (2.8 if strategy == "tree" else 4.0),
            1.2 if strategy == "tree" else 1.6,
        )
        interface_spacing = max(
            line_width, float(settings.support_interface_spacing_mm)
        )
        layer_body_length = body.area / body_spacing
        if strategy == "tree" and not body.is_empty:
            layer_body_length += body.length
        layer_interface_length = interface.area / interface_spacing
        body_length += layer_body_length
        interface_length += layer_interface_length
        acceleration = (
            settings.initial_layer_acceleration_mm_s2
            if layer_index == 0
            else settings.default_acceleration_mm_s2
        )
        body_segments = max(1, math.ceil(layer_body_length / 25.0))
        interface_segments = max(1, math.ceil(layer_interface_length / 25.0))
        body_seconds += body_segments * _motion_time_s(
            layer_body_length / body_segments,
            settings.support_speed_mm_s,
            acceleration,
        )
        interface_seconds += interface_segments * _motion_time_s(
            layer_interface_length / interface_segments,
            settings.support_interface_speed_mm_s,
            acceleration,
        )
        path_groups = sum(1 for _ in _iter_polygons(body)) + sum(
            1 for _ in _iter_polygons(interface)
        )
        retraction_seconds += path_groups * 2.0 * settings.retraction_length_mm / max(
            1.0, settings.retraction_speed_mm_s
        )
    volume = (body_length + interface_length) * line_width * float(settings.layer_height_mm)
    mass = volume / 1000.0 * material_density_g_cm3
    seconds = body_seconds + interface_seconds + retraction_seconds
    return {
        "strategy": strategy,
        "eligible": True,
        "support_path_length_mm": round(body_length + interface_length, 3),
        "estimated_support_mass_g": round(mass, 3),
        "estimated_support_time_s": round(seconds, 3),
        "detected_overhang_area_mm2": round(demand_area_mm2, 3),
    }


def _choose_support_strategy(
    requested: str,
    settings: PrintSettings,
    normal: dict[str, object],
    tree: dict[str, object],
    *,
    demand_area_mm2: float,
    support_exit_risk: str = "UNKNOWN",
    support_accessibility_score: float = 100.0,
) -> tuple[str, str, tuple[dict[str, object], ...]]:
    none_eligible = not settings.supports or demand_area_mm2 < max(0.5, settings.line_width_mm**2 * 3)
    none: dict[str, object] = {
        "strategy": "none",
        "eligible": none_eligible,
        "support_path_length_mm": 0.0,
        "estimated_support_mass_g": 0.0,
        "estimated_support_time_s": 0.0,
        "detected_overhang_area_mm2": round(demand_area_mm2, 3),
    }
    exit_risk = str(support_exit_risk).upper()
    accessibility = max(0.0, min(100.0, float(support_accessibility_score)))
    none["removal_accessibility_score"] = 100.0
    normal["removal_accessibility_score"] = round(accessibility, 2)
    tree["removal_accessibility_score"] = round(min(100.0, accessibility + 35.0), 2)
    if requested != "auto":
        selected = requested
        reason = {
            "none": "Пользователь явно отключил поддержки; риск нависаний всё равно рассчитан.",
            "normal": "Пользователь явно выбрал обычные поддержки.",
            "tree": "Пользователь явно выбрал древовидные поддержки.",
        }[selected]
    elif none_eligible:
        selected = "none"
        reason = "Проверены три стратегии: опасных нависаний не найдено, поддержки не добавлены."
    elif exit_risk == "HIGH" or accessibility < 30.0:
        selected = "tree"
        normal["eligible"] = False
        normal["rejection_reason"] = (
            "Обычные опоры отклонены: расчёт показал высокий риск запирания "
            f"и доступность удаления {accessibility:.0f}/100."
        )
        reason = (
            "Проверены без поддержек, обычные и древовидные. Без опор вариант "
            "отклонён по геометрии, а обычные опоры — из-за риска невозможного "
            f"извлечения ({accessibility:.0f}/100); выбраны древовидные."
        )
    else:
        candidates = {"normal": normal, "tree": tree}
        minimum_time = max(1e-9, min(float(item["estimated_support_time_s"]) for item in candidates.values()))
        minimum_mass = max(1e-9, min(float(item["estimated_support_mass_g"]) for item in candidates.values()))
        for item in candidates.values():
            item["score"] = round(
                0.6 * float(item["estimated_support_time_s"]) / minimum_time
                + 0.4 * float(item["estimated_support_mass_g"]) / minimum_mass,
                6,
            )
        selected = min(candidates, key=lambda name: float(candidates[name]["score"]))
        reason = (
            "Проверены без поддержек, обычные и древовидные. Вариант без поддержек "
            "отклонён по геометрии; между опорами выбран лучший баланс 60% времени и 40% материала."
        )
    for candidate in (none, normal, tree):
        candidate["selected"] = candidate["strategy"] == selected
    return selected, reason, (none, normal, tree)


def _nearest_path(
    paths: list[list[tuple[float, float]]],
    point: tuple[float, float],
) -> list[tuple[float, float]]:
    best_index = 0
    best_reverse = False
    best_distance = math.inf
    for index, path in enumerate(paths):
        for reverse, endpoint in ((False, path[0]), (True, path[-1])):
            distance = (endpoint[0] - point[0]) ** 2 + (endpoint[1] - point[1]) ** 2
            if distance < best_distance:
                best_distance = distance
                best_index = index
                best_reverse = reverse
    selected = paths.pop(best_index)
    if best_reverse:
        selected.reverse()
    return selected


def _load_mesh(source: Path) -> trimesh.Trimesh:
    try:
        validate_stl_file(source)
        loaded = trimesh.load_mesh(source, file_type="stl", process=False)
    except (OSError, ValueError, UnsafeInputError) as exc:
        raise VaniorSliceError(f"Не удалось безопасно прочитать STL: {exc}") from exc
    if not isinstance(loaded, trimesh.Trimesh) or loaded.faces.size == 0:
        raise VaniorSliceError("STL не содержит печатаемой треугольной сетки.")
    mesh = loaded.copy()
    mesh.remove_infinite_values()
    mesh.merge_vertices()
    mesh.remove_unreferenced_vertices()
    if not mesh.is_watertight:
        raise VaniorSliceError(
            "Собственный движок пока принимает только замкнутую геометрию; "
            "сначала требуется восстановление модели."
        )
    if not np.isfinite(mesh.vertices).all():
        raise VaniorSliceError("Модель содержит нечисловые координаты.")
    return mesh


def _position_mesh(mesh: trimesh.Trimesh, machine: VaniorMachineProfile) -> None:
    minimum, maximum = mesh.bounds
    size = maximum - minimum
    allowed_x = machine.build_size_mm[0] - 2 * machine.printable_margin_mm
    allowed_y = machine.build_size_mm[1] - 2 * machine.printable_margin_mm
    if size[0] > allowed_x or size[1] > allowed_y or size[2] > machine.build_size_mm[2] - 2:
        raise VaniorSliceError(
            "Модель выходит за безопасную область печати собственного профиля P1S."
        )
    target_x = machine.build_size_mm[0] * 0.5
    target_y = machine.build_size_mm[1] * 0.5
    mesh.apply_translation(
        (target_x - (minimum[0] + maximum[0]) * 0.5,
         target_y - (minimum[1] + maximum[1]) * 0.5,
         -minimum[2])
    )


def load_positioned_mesh(
    source: str | Path,
    machine: VaniorMachineProfile = P1S_INDEPENDENT,
) -> trimesh.Trimesh:
    """Load a validated STL and place it exactly as VANIOR Slice does."""
    mesh = _load_mesh(Path(source).expanduser().resolve())
    _position_mesh(mesh, machine)
    return mesh


def _header(settings: PrintSettings, material: str, support_strategy: str) -> list[str]:
    return [
        "; generated by VANIOR Slice",
        "; engine_stage=engineering-preview",
        "; single_material=1",
        f"; material={material}",
        f"; support_strategy={support_strategy}",
        "G90 ; absolute XYZ",
        "M83 ; relative extrusion",
        f"M140 S{settings.bed_temperature_c}",
        f"M104 S{settings.nozzle_temperature_c}",
        "G28",
        f"M190 S{settings.bed_temperature_c}",
        f"M109 S{settings.nozzle_temperature_c}",
        "G92 E0",
        "G1 Z5 F1200",
    ]


def slice_stl_to_gcode(
    source: str | Path,
    output: str | Path,
    settings: PrintSettings,
    *,
    material: str = "PLA",
    nozzle_diameter_mm: float = 0.4,
    overhang_angle_deg: float = 45.0,
    support_strategy: str = "auto",
    support_exit_risk: str = "UNKNOWN",
    support_accessibility_score: float = 100.0,
    machine: VaniorMachineProfile = P1S_INDEPENDENT,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> VaniorSliceResult:
    """Slice one watertight STL without invoking or reading another slicer."""
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    normalized_material = material.upper()
    if source_path.suffix.lower() != ".stl":
        raise VaniorSliceError("Первая независимая версия поддерживает только STL.")
    if output_path.suffix.lower() != ".gcode":
        raise VaniorSliceError("Результат собственного движка должен иметь расширение .gcode.")
    if output_path.exists():
        raise VaniorSliceError("Собственный движок не перезаписывает существующий результат.")
    if normalized_material not in _MATERIAL_DENSITY_G_CM3:
        raise VaniorSliceError("Собственный движок пока поддерживает только PLA и PETG.")
    if not math.isclose(nozzle_diameter_mm, 0.4, abs_tol=1e-6):
        raise VaniorSliceError("Собственный профиль пока проверен только для сопла 0,4 мм.")
    if not (0.08 <= settings.layer_height_mm <= nozzle_diameter_mm * 0.8):
        raise VaniorSliceError("Высота слоя выходит за проверяемый диапазон сопла.")
    if settings.wall_loops < 1 or settings.line_width_mm <= 0:
        raise VaniorSliceError("Некорректные параметры стенок собственного слайсера.")
    requested_support_strategy = support_strategy.strip().casefold()
    if requested_support_strategy not in _SUPPORT_STRATEGIES:
        raise VaniorSliceError(
            "Стратегия поддержек должна быть auto, none, normal или tree."
        )

    emit_progress(progress_callback, "vanior-load", "Чтение геометрии VANIOR Slice", 5)
    mesh = load_positioned_mesh(source_path, machine)
    check_cancelled(cancel_event)

    model_height = float(mesh.bounds[1, 2])
    first_height = min(settings.initial_layer_height_mm, model_height)
    z_values: list[float] = []
    print_z = first_height
    while print_z <= model_height + 1e-7:
        sample_z = max(1e-5, print_z - (first_height if not z_values else settings.layer_height_mm) * 0.5)
        z_values.append(min(sample_z, model_height - 1e-5))
        print_z += settings.layer_height_mm
    if not z_values:
        raise VaniorSliceError("Высота модели недостаточна для выбранного слоя.")

    regions: list[BaseGeometry] = []
    last_section_progress = -1
    for index, z_value in enumerate(z_values):
        check_cancelled(cancel_event)
        region = _slice_region(mesh, z_value)
        regions.append(region)
        section_progress = 10 + int(33 * (index + 1) / len(z_values))
        if section_progress != last_section_progress:
            emit_progress(
                progress_callback,
                "vanior-sections",
                f"Построение слоёв VANIOR Slice: {index + 1}/{len(z_values)}",
                section_progress,
            )
            last_section_progress = section_progress
    if not any(not region.is_empty for region in regions):
        raise VaniorSliceError("Не удалось построить ни одного печатаемого слоя.")

    line_width = float(settings.line_width_mm)
    filament_area = math.pi * (1.75 * 0.5) ** 2
    flow_ratio = float(settings.filament_flow_ratio)
    emit_progress(
        progress_callback,
        "vanior-supports",
        "Сравнение поддержек: поиск нависаний",
        44,
    )
    contacts = _support_islands(
        regions,
        settings,
        overhang_angle_deg=overhang_angle_deg,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    demand_area = sum(polygon.area for _layer, polygon in contacts)
    empty_supports = [GeometryCollection() for _ in regions]
    minimum_support_area = max(0.5, settings.line_width_mm**2 * 3)
    evaluate_supports = (
        settings.supports
        and requested_support_strategy != "none"
        and demand_area >= minimum_support_area
    )
    if evaluate_supports and requested_support_strategy in {"auto", "normal"}:
        normal_regions, normal_interfaces = _support_regions(
            regions,
            settings,
            overhang_angle_deg=overhang_angle_deg,
            contacts=contacts,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
    else:
        normal_regions = list(empty_supports)
        normal_interfaces = list(empty_supports)
    if evaluate_supports and requested_support_strategy in {"auto", "tree"}:
        tree_regions, tree_interfaces = _tree_support_regions(
            regions,
            settings,
            overhang_angle_deg=overhang_angle_deg,
            contacts=contacts,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
    else:
        tree_regions = list(empty_supports)
        tree_interfaces = list(empty_supports)
    emit_progress(
        progress_callback,
        "vanior-support-estimates",
        "Оценка времени и материала вариантов поддержек",
        55,
    )
    material_density = _MATERIAL_DENSITY_G_CM3[normalized_material]
    normal_candidate = _support_candidate(
        "normal",
        normal_regions,
        normal_interfaces,
        settings,
        demand_area_mm2=demand_area,
        material_density_g_cm3=material_density,
    )
    tree_candidate = _support_candidate(
        "tree",
        tree_regions,
        tree_interfaces,
        settings,
        demand_area_mm2=demand_area,
        material_density_g_cm3=material_density,
    )
    if requested_support_strategy == "normal":
        tree_candidate["eligible"] = False
        tree_candidate["rejection_reason"] = "Не рассчитывалось: выбран обычный тип."
    elif requested_support_strategy == "tree":
        normal_candidate["eligible"] = False
        normal_candidate["rejection_reason"] = "Не рассчитывалось: выбран древовидный тип."
    selected_support_strategy, support_reason, support_candidates = _choose_support_strategy(
        requested_support_strategy,
        settings,
        normal_candidate,
        tree_candidate,
        demand_area_mm2=demand_area,
        support_exit_risk=support_exit_risk,
        support_accessibility_score=support_accessibility_score,
    )
    if selected_support_strategy == "normal":
        support_regions, support_interfaces = normal_regions, normal_interfaces
    elif selected_support_strategy == "tree":
        support_regions, support_interfaces = tree_regions, tree_interfaces
    else:
        support_regions = [GeometryCollection() for _ in regions]
        support_interfaces = [GeometryCollection() for _ in regions]

    gcode = _header(settings, normalized_material, selected_support_strategy)
    current_xy = (machine.build_size_mm[0] * 0.5, machine.build_size_mm[1] * 0.5)
    total_e = 0.0
    support_e = 0.0
    support_time = 0.0
    movement_time = 0.0
    emitted_lines = 0
    retracted = False
    current_z = 5.0

    last_toolpath_progress = -1
    for layer_index, region in enumerate(regions):
        check_cancelled(cancel_event)
        layer_height = first_height if layer_index == 0 else settings.layer_height_mm
        layer_z = first_height + layer_index * settings.layer_height_mm
        layer_extrusion_start = emitted_lines
        nominal_layer_time = 0.0
        gcode.extend((f";LAYER:{layer_index}", f"G1 Z{layer_z:.3f} F1200"))
        z_distance = abs(layer_z - current_z)
        if z_distance > 0:
            movement_time += _motion_time_s(
                z_distance,
                20.0,
                settings.default_acceleration_mm_s2,
            )
            nominal_layer_time += z_distance / 20.0
            current_z = layer_z
        if layer_index == 1:
            fan_pwm = round(max(0, min(100, settings.fan_percent)) * 2.55)
            gcode.append(f"M106 S{fan_pwm}")

        outer_wall_paths = _contour_paths(
            make_valid(region.buffer(-0.5 * line_width))
        )
        inner_wall_paths: list[list[tuple[float, float]]] = []
        for loop in range(settings.wall_loops - 1, 0, -1):
            centerline = make_valid(region.buffer(-(0.5 + loop) * line_width))
            inner_wall_paths.extend(_contour_paths(centerline))

        inner = make_valid(region.buffer(-settings.wall_loops * line_width))
        solid = _solid_mask(
            regions,
            layer_index,
            top_layers=settings.top_layers,
            bottom_layers=settings.bottom_layers,
            tolerance_mm=line_width * 0.15,
        ).intersection(inner)
        above = regions[layer_index + 1] if layer_index + 1 < len(regions) else GeometryCollection()
        top_surface = make_valid(
            region.difference(above.buffer(line_width * 0.15)).intersection(inner)
        )
        below = regions[layer_index - 1] if layer_index > 0 else GeometryCollection()
        bottom_surface = make_valid(
            region.difference(below.buffer(line_width * 0.15)).intersection(inner)
        )
        if layer_index > 0:
            allowed_bridge_step = layer_height / max(
                math.tan(math.radians(overhang_angle_deg)), 1e-6
            )
            bridge_domain = make_valid(
                inner.difference(regions[layer_index - 1].buffer(allowed_bridge_step))
            )
        else:
            bridge_domain = GeometryCollection()
        top_surface = make_valid(top_surface.difference(bridge_domain))
        bottom_surface = make_valid(
            bottom_surface.difference(bridge_domain).difference(
                top_surface.buffer(line_width * 0.2)
            )
        )
        remaining_solid = make_valid(
            solid.difference(top_surface.buffer(line_width * 0.2))
            .difference(bottom_surface.buffer(line_width * 0.2))
            .difference(bridge_domain.buffer(line_width * 0.2))
        )
        top_surface_paths = _infill_paths(
            top_surface,
            spacing_mm=max(float(settings.top_surface_line_width_mm) * 0.92, 0.1),
            angle_degrees=0,
        )
        solid_paths = _infill_paths(
            remaining_solid,
            spacing_mm=max(line_width * 0.90, 0.1),
            angle_degrees=0 if layer_index % 2 == 0 else 90,
        )
        bottom_surface_paths = _infill_paths(
            bottom_surface,
            spacing_mm=max(line_width * 0.90, 0.1),
            angle_degrees=90,
        )
        sparse_domain = make_valid(
            inner.difference(solid.buffer(line_width * 0.45)).difference(bridge_domain)
        )
        density = max(1.0, min(100.0, float(settings.sparse_infill_percent)))
        sparse_paths = _infill_paths(
            sparse_domain,
            spacing_mm=max(line_width, line_width * 100.0 / density),
            angle_degrees=45 if layer_index % 2 == 0 else 135,
        )
        bridge_paths = _infill_paths(
            bridge_domain,
            spacing_mm=max(line_width * 0.92, 0.1),
            angle_degrees=0 if layer_index % 2 == 0 else 90,
        )
        support_interface = support_interfaces[layer_index]
        support_body = support_regions[layer_index].difference(support_interface)
        support_paths, support_interface_paths = _support_paths_for_layer(
            support_body,
            support_interface,
            strategy=selected_support_strategy,
            line_width=line_width,
            interface_spacing=float(settings.support_interface_spacing_mm),
            layer_index=layer_index,
        )
        categories: list[tuple[str, list[list[tuple[float, float]]], float]] = []
        if layer_index == 0 and settings.brim:
            categories.append(
                (
                    "SKIRT-BRIM",
                    _brim_paths(region, line_width_mm=line_width),
                    settings.initial_layer_speed_mm_s,
                )
            )
        categories.extend((
            ("INNER-WALL", inner_wall_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.inner_wall_speed_mm_s),
            ("OUTER-WALL", outer_wall_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.outer_wall_speed_mm_s),
            ("TOP-SURFACE", top_surface_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.top_surface_speed_mm_s),
            ("BOTTOM-SURFACE", bottom_surface_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.internal_solid_infill_speed_mm_s),
            ("SOLID-INFILL", solid_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.internal_solid_infill_speed_mm_s),
            ("SPARSE-INFILL", sparse_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.sparse_infill_speed_mm_s),
            ("BRIDGE", bridge_paths, settings.bridge_speed_mm_s),
            ("SUPPORT", support_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.support_speed_mm_s),
            ("SUPPORT-INTERFACE", support_interface_paths, settings.initial_layer_speed_mm_s if layer_index == 0 else settings.support_interface_speed_mm_s),
        ))
        for feature, paths, speed in categories:
            # A profile speed is only a request.  The independent engine owns
            # the final volumetric-flow gate and lowers every extrusion move
            # before writing it, leaving 10% headroom for numerical rounding
            # and short segment dynamics.
            volumetric_speed_limit = (
                float(settings.max_volumetric_speed_mm3_s)
                / max(line_width * layer_height * flow_ratio, 1e-9)
                * 0.90
            )
            effective_speed = min(float(speed), volumetric_speed_limit)
            gcode.append(f"; FEATURE: {feature}")
            remaining = [
                simplified
                for path in paths
                if len(path) >= 2
                for simplified in (_simplify_toolpath(path),)
                if len(simplified) >= 2
            ]
            while remaining:
                path = _nearest_path(remaining, current_xy)
                start = path[0]
                travel = math.hypot(start[0] - current_xy[0], start[1] - current_xy[1])
                if travel > 1.5 and not retracted:
                    gcode.append(
                        f"G1 E{-settings.retraction_length_mm:.4f} "
                        f"F{settings.retraction_speed_mm_s * 60:.0f} ; retract"
                    )
                    movement_time += settings.retraction_length_mm / max(
                        1.0, settings.retraction_speed_mm_s
                    )
                    nominal_layer_time += settings.retraction_length_mm / max(
                        1.0, settings.retraction_speed_mm_s
                    )
                    retracted = True
                gcode.append(
                    f"G0 X{start[0]:.3f} Y{start[1]:.3f} F{settings.travel_speed_mm_s * 60:.0f}"
                )
                if settings.travel_speed_mm_s > 0:
                    movement_time += _motion_time_s(
                        travel,
                        settings.travel_speed_mm_s,
                        settings.travel_acceleration_mm_s2,
                    )
                    nominal_layer_time += travel / settings.travel_speed_mm_s
                if retracted:
                    gcode.append(
                        f"G1 E{settings.retraction_length_mm:.4f} "
                        f"F{settings.retraction_speed_mm_s * 60:.0f} ; unretract"
                    )
                    movement_time += settings.retraction_length_mm / max(
                        1.0, settings.retraction_speed_mm_s
                    )
                    nominal_layer_time += settings.retraction_length_mm / max(
                        1.0, settings.retraction_speed_mm_s
                    )
                    retracted = False
                previous = start
                for x, y in path[1:]:
                    length = math.hypot(x - previous[0], y - previous[1])
                    if length <= 1e-6:
                        continue
                    extrusion = length * line_width * layer_height * flow_ratio / filament_area
                    gcode.append(
                        f"G1 X{x:.3f} Y{y:.3f} E{extrusion:.5f} "
                        f"F{effective_speed * 60:.0f}"
                    )
                    total_e += extrusion
                    if layer_index == 0:
                        acceleration = settings.initial_layer_acceleration_mm_s2
                    elif feature == "OUTER-WALL":
                        acceleration = settings.outer_wall_acceleration_mm_s2
                    elif feature == "TOP-SURFACE":
                        acceleration = settings.top_surface_acceleration_mm_s2
                    else:
                        acceleration = settings.default_acceleration_mm_s2
                    segment_time = _motion_time_s(
                        length, effective_speed, acceleration
                    )
                    movement_time += segment_time
                    nominal_layer_time += length / max(effective_speed, 1e-9)
                    if feature in {"SUPPORT", "SUPPORT-INTERFACE"}:
                        support_e += extrusion
                        support_time += segment_time
                    emitted_lines += 1
                    previous = (x, y)
                current_xy = previous
        minimum_layer_time = max(0.0, float(settings.slow_down_layer_time_s))
        layer_has_extrusion = emitted_lines > layer_extrusion_start
        if layer_has_extrusion and nominal_layer_time < minimum_layer_time:
            if not retracted:
                gcode.append(
                    f"G1 E{-settings.retraction_length_mm:.4f} "
                    f"F{settings.retraction_speed_mm_s * 60:.0f} ; cooling retract"
                )
                retraction_time = settings.retraction_length_mm / max(
                    1.0, settings.retraction_speed_mm_s
                )
                movement_time += retraction_time
                nominal_layer_time += retraction_time
                retracted = True
            cooling_z = min(machine.build_size_mm[2] - 1.0, layer_z + 0.6)
            lift_distance = max(0.0, cooling_z - layer_z)
            one_way_lift_time = lift_distance / 20.0
            if lift_distance > 0:
                gcode.append(f"G1 Z{cooling_z:.3f} F1200 ; cooling lift")
                movement_time += one_way_lift_time
                nominal_layer_time += one_way_lift_time
                current_z = cooling_z
            dwell_s = max(
                0.0,
                minimum_layer_time - nominal_layer_time - one_way_lift_time,
            )
            if dwell_s > 0:
                gcode.append(f"G4 P{round(dwell_s * 1000)} ; minimum layer time")
                movement_time += dwell_s
                nominal_layer_time += dwell_s
            if lift_distance > 0:
                gcode.append(f"G1 Z{layer_z:.3f} F1200 ; cooling return")
                movement_time += one_way_lift_time
                nominal_layer_time += one_way_lift_time
                current_z = layer_z
        toolpath_progress = 56 + int(34 * (layer_index + 1) / len(regions))
        if toolpath_progress != last_toolpath_progress:
            emit_progress(
                progress_callback,
                "vanior-toolpath",
                f"Траектории VANIOR Slice: {layer_index + 1}/{len(regions)}",
                toolpath_progress,
            )
            last_toolpath_progress = toolpath_progress

    if emitted_lines == 0 or total_e <= 0:
        raise VaniorSliceError("Собственный движок не создал печатаемых траекторий.")
    safe_z = min(machine.build_size_mm[2] - 2, model_height + 5)
    gcode.extend(
        (
            "; VANIOR END",
            f"G1 E{-settings.retraction_length_mm:.4f} F{settings.retraction_speed_mm_s * 60:.0f}",
            f"G1 Z{safe_z:.3f} F1200",
            f"G0 X{machine.printable_margin_mm:.1f} Y{machine.build_size_mm[1] - machine.printable_margin_mm:.1f} F12000",
            "M104 S0",
            "M140 S0",
            "M106 S0",
            "M84",
            "; end of VANIOR Slice output",
        )
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_new_text(output_path, "\n".join(gcode) + "\n")
    audit = audit_gcode(
        output_path,
        filament_diameters_mm=(1.75,),
        maximum_volumetric_speed_mm3_s=settings.max_volumetric_speed_mm3_s,
        maximum_coordinate_abs_mm=max(machine.build_size_mm) + 1,
    )
    if audit.status == "BLOCKED":
        output_path.unlink(missing_ok=True)
        raise VaniorSliceError(
            "Независимая проверка заблокировала G-code: "
            + "; ".join(audit.blocking_warnings)
        )
    filament_volume_mm3 = total_e * filament_area
    estimated_mass = filament_volume_mm3 / 1000.0 * material_density
    support_mass = support_e * filament_area / 1000.0 * material_density
    emit_progress(progress_callback, "vanior-done", "G-code VANIOR Slice проверен", 100)
    return VaniorSliceResult(
        engine="VANIOR Slice",
        engine_stage="engineering-preview",
        source_path=source_path,
        gcode_path=output_path,
        layer_count=len(regions),
        line_count=emitted_lines,
        extrusion_length_mm=round(total_e, 3),
        estimated_mass_g=round(estimated_mass, 3),
        estimated_print_time_s=round(movement_time, 3),
        bounds_mm=tuple(round(float(value), 3) for value in mesh.extents),
        support_strategy=selected_support_strategy,
        support_extrusion_length_mm=round(support_e, 3),
        estimated_support_mass_g=round(support_mass, 3),
        estimated_support_time_s=round(support_time, 3),
        support_candidates=support_candidates,
        support_recommendation_reason=support_reason,
        audit=audit,
    )
