"""Geometry-driven surface roles and safe print-setting decisions."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

import numpy as np
import trimesh

from .report import PrintSettings


@dataclass(frozen=True)
class SurfaceRole:
    role: str
    face_count: int
    area_mm2: float
    area_ratio: float


@dataclass(frozen=True)
class SurfaceIntelligence:
    version: int
    confidence: str
    dominant_role: str
    roles: tuple[SurfaceRole, ...]
    recommendations: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "confidence": self.confidence,
            "dominant_role": self.dominant_role,
            "roles": [
                {
                    "role": item.role,
                    "face_count": item.face_count,
                    "area_mm2": item.area_mm2,
                    "area_ratio": item.area_ratio,
                }
                for item in self.roles
            ],
            "recommendations": list(self.recommendations),
        }

    def ratio(self, role: str) -> float:
        return next((item.area_ratio for item in self.roles if item.role == role), 0.0)


ROLE_ORDER = (
    "bed_contact",
    "top_visible",
    "support_contact",
    "curved_visible",
    "precision_candidate",
    "visible_wall",
)


def analyze_surfaces(
    mesh: trimesh.Trimesh,
    *,
    overhang_angle_deg: float = 45.0,
) -> SurfaceIntelligence:
    """Assign every triangle one conservative geometric surface role.

    The classification intentionally uses geometry only.  It never claims to
    know semantic intent with certainty; a later UI may let the user override
    individual regions.
    """
    areas = np.asarray(mesh.area_faces, dtype=np.float64)
    normals = np.asarray(mesh.face_normals, dtype=np.float64)
    triangles = np.asarray(mesh.triangles, dtype=np.float64)
    if len(areas) == 0 or normals.shape != (len(areas), 3):
        return SurfaceIntelligence(1, "LOW", "visible_wall", (), ())

    total_area = max(float(areas.sum()), 1e-9)
    height = max(float(mesh.extents[2]), 1e-9)
    bed_tolerance = max(0.02, min(0.20, height * 0.001))
    minimum_z = float(mesh.bounds[0, 2])
    face_max_z = triangles[:, :, 2].max(axis=1)
    nz = normals[:, 2]
    axis_alignment = np.abs(normals).max(axis=1)

    bed = (face_max_z <= minimum_z + bed_tolerance) & (np.abs(nz) >= 0.90)
    support_limit = -np.cos(np.deg2rad(overhang_angle_deg))
    above_bed = triangles.mean(axis=1)[:, 2] > minimum_z + max(0.05, height * 0.002)
    support = (~bed) & above_bed & (nz < support_limit)
    top = (~bed) & (~support) & (nz >= 0.82)
    curved = (~bed) & (~support) & (~top) & (axis_alignment < 0.94)
    precision = (
        (~bed)
        & (~support)
        & (~top)
        & (~curved)
        & (np.abs(nz) <= 0.22)
    )
    wall = ~(bed | support | top | curved | precision)
    masks = {
        "bed_contact": bed,
        "top_visible": top,
        "support_contact": support,
        "curved_visible": curved,
        "precision_candidate": precision,
        "visible_wall": wall,
    }
    roles = tuple(
        SurfaceRole(
            role=role,
            face_count=int(np.count_nonzero(masks[role])),
            area_mm2=float(areas[masks[role]].sum()),
            area_ratio=float(areas[masks[role]].sum() / total_area),
        )
        for role in ROLE_ORDER
    )
    visible_roles = [item for item in roles if item.role != "bed_contact"]
    dominant = max(visible_roles, key=lambda item: item.area_mm2).role
    confidence = "HIGH" if len(mesh.faces) >= 100 and mesh.is_watertight else "MEDIUM"
    recommendations = _surface_recommendations(roles)
    return SurfaceIntelligence(
        version=1,
        confidence=confidence,
        dominant_role=dominant,
        roles=roles,
        recommendations=recommendations,
    )


def _surface_recommendations(roles: tuple[SurfaceRole, ...]) -> tuple[str, ...]:
    values = {item.role: item.area_ratio for item in roles}
    result: list[str] = []
    if values.get("top_visible", 0.0) >= 0.04:
        result.append("Защитить верхние поверхности: плотная крышка и сниженная скорость.")
    if values.get("curved_visible", 0.0) >= 0.25:
        result.append("Сохранить криволинейные поверхности уменьшенным слоем и скоростью стенки.")
    if values.get("precision_candidate", 0.0) >= 0.12:
        result.append("Печатать плоские посадочные кандидаты с ограниченным ускорением стенки.")
    if values.get("support_contact", 0.0) >= 0.03:
        result.append("Настроить отделяемый интерфейс поддержек для нижних видимых граней.")
    if values.get("bed_contact", 0.0) < 0.01:
        result.append("Компенсировать малую площадь контакта со столом каймой.")
    return tuple(result)


def settings_for_surfaces(
    base: PrintSettings,
    intelligence: SurfaceIntelligence,
) -> tuple[PrintSettings, tuple[str, ...]]:
    """Apply bounded global settings derived from the surface-role map."""
    changes: dict[str, object] = {}
    decisions: list[str] = []
    top = intelligence.ratio("top_visible")
    curved = intelligence.ratio("curved_visible")
    precision = intelligence.ratio("precision_candidate")
    support = intelligence.ratio("support_contact")
    bed = intelligence.ratio("bed_contact")

    priorities = {item for item in base.priority.split("+") if item}
    # A mixed selection is an interpolation, not the quality endpoint.  The
    # priority combiner already averaged the numeric settings; do not silently
    # reset the layer height to the pure-quality value here.
    quality_selected = priorities == {"quality"}
    balanced_selected = "balanced" in priorities

    if top >= 0.04:
        changes.update(
            top_layers=max(6, base.top_layers),
            top_shell_thickness_mm=max(1.2, base.top_shell_thickness_mm),
            top_surface_pattern="monotonicline",
            top_surface_density_percent=100,
            top_surface_speed_mm_s=min(80, base.top_surface_speed_mm_s),
            top_surface_acceleration_mm_s2=min(1_500, base.top_surface_acceleration_mm_s2),
        )
        if top >= 0.18 and quality_selected:
            changes["ironing_enabled"] = True
        decisions.append(f"Верхние поверхности: {top * 100:.1f}% площади; усилена чистовая крышка.")

    if curved >= 0.25:
        if quality_selected:
            target_layer = 0.12
        elif balanced_selected:
            target_layer = 0.16
        else:
            target_layer = 0.20
        changes.update(
            layer_height_mm=min(target_layer, base.layer_height_mm),
            outer_wall_speed_mm_s=min(80 if quality_selected else 100, base.outer_wall_speed_mm_s),
            outer_wall_acceleration_mm_s2=min(1_500 if quality_selected else 2_000, base.outer_wall_acceleration_mm_s2),
            wall_generator="classic" if base.model_purpose == "decorative" else base.wall_generator,
        )
        decisions.append(f"Криволинейные видимые поверхности: {curved * 100:.1f}%; сохранена детализация.")

    if precision >= 0.12:
        changes.update(
            wall_loops=max(3, base.wall_loops),
            outer_wall_speed_mm_s=min(100, int(changes.get("outer_wall_speed_mm_s", base.outer_wall_speed_mm_s))),
            outer_wall_acceleration_mm_s2=min(2_000, int(changes.get("outer_wall_acceleration_mm_s2", base.outer_wall_acceleration_mm_s2))),
        )
        decisions.append(f"Размерно-критичные кандидаты: {precision * 100:.1f}%; ограничены скорость и ускорение стенки.")

    if support >= 0.03:
        layer = float(changes.get("layer_height_mm", base.layer_height_mm))
        changes.update(
            supports=True,
            support_top_z_distance_mm=max(0.20, layer),
            support_bottom_z_distance_mm=max(0.20, layer),
            support_object_xy_distance_mm=max(0.40, base.support_object_xy_distance_mm),
            support_interface_top_layers=max(4, base.support_interface_top_layers),
            support_interface_spacing_mm=min(0.30, base.support_interface_spacing_mm),
            support_interface_speed_mm_s=min(55, base.support_interface_speed_mm_s),
        )
        decisions.append(f"Контакт с поддержками: {support * 100:.1f}%; выбран отделяемый интерфейс.")

    if bed < 0.01:
        changes["brim"] = True
        decisions.append("Площадь контакта со столом мала; включена кайма.")

    return replace(base, **changes), tuple(decisions)


def with_surface_decisions(
    intelligence: SurfaceIntelligence,
    decisions: tuple[str, ...],
) -> SurfaceIntelligence:
    return replace(intelligence, recommendations=decisions)
