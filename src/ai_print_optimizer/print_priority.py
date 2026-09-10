"""Safe user-selectable quality/speed priorities for Bambu P1S printing."""

from __future__ import annotations

from dataclasses import replace

from .report import PrintSettings

PRINT_PRIORITIES = ("quality", "strength", "balanced", "fast")


class PrintPriorityError(ValueError):
    """Raised when an unknown print priority is requested."""


def normalize_print_priority(priority: str) -> str:
    requested = [part.strip().casefold() for part in priority.split("+") if part.strip()]
    unknown = [part for part in requested if part not in PRINT_PRIORITIES]
    if not requested or unknown:
        raise PrintPriorityError(
            f"unsupported print priority: {priority}; choose one or more of "
            "quality, strength, balanced and fast joined with '+'"
        )
    selected = [item for item in PRINT_PRIORITIES if item in requested]
    return "+".join(selected)


def priority_components(priority: str) -> tuple[str, ...]:
    """Return a stable, click-order-independent set of selected priorities."""
    return tuple(normalize_print_priority(priority).split("+"))


def _single_priority_settings(base: PrintSettings, selected: str) -> PrintSettings:
    """Apply one endpoint preset without recursively normalizing a mix."""
    if selected == "quality":
        return replace(
            base,
            priority=selected,
            layer_height_mm=0.12,
            wall_loops=min(5, max(4, base.wall_loops + 1)),
            top_layers=max(6, base.top_layers),
            bottom_layers=max(5, base.bottom_layers),
            sparse_infill_percent=20,
            sparse_infill_pattern="gyroid",
            outer_wall_speed_mm_s=80,
            inner_wall_speed_mm_s=150,
            sparse_infill_speed_mm_s=180,
            internal_solid_infill_speed_mm_s=160,
            top_surface_speed_mm_s=80,
            support_speed_mm_s=120,
            support_interface_speed_mm_s=60,
            bridge_speed_mm_s=40,
            initial_layer_speed_mm_s=30,
            travel_speed_mm_s=400,
            default_acceleration_mm_s2=6_000,
            outer_wall_acceleration_mm_s2=2_500,
            top_surface_acceleration_mm_s2=1_500,
            initial_layer_acceleration_mm_s2=500,
            travel_acceleration_mm_s2=8_000,
        )
    if selected == "strength":
        return replace(
            base,
            priority=selected,
            layer_height_mm=0.20,
            wall_loops=max(6, base.wall_loops),
            top_layers=max(6, base.top_layers),
            bottom_layers=max(6, base.bottom_layers),
            sparse_infill_percent=max(40, base.sparse_infill_percent),
            sparse_infill_pattern="gyroid",
            outer_wall_speed_mm_s=min(100, base.outer_wall_speed_mm_s),
            inner_wall_speed_mm_s=min(220, base.inner_wall_speed_mm_s),
            sparse_infill_speed_mm_s=min(200, base.sparse_infill_speed_mm_s),
            internal_solid_infill_speed_mm_s=min(180, base.internal_solid_infill_speed_mm_s),
            top_surface_speed_mm_s=min(90, base.top_surface_speed_mm_s),
            default_acceleration_mm_s2=min(8_000, base.default_acceleration_mm_s2),
            outer_wall_acceleration_mm_s2=min(2_500, base.outer_wall_acceleration_mm_s2),
        )
    if selected == "fast":
        return replace(
            base,
            priority=selected,
            layer_height_mm=0.24,
            wall_loops=max(2, base.wall_loops - 1),
            top_layers=4,
            bottom_layers=3,
            sparse_infill_percent=10,
            sparse_infill_pattern="grid",
            outer_wall_speed_mm_s=250,
            inner_wall_speed_mm_s=350,
            sparse_infill_speed_mm_s=330,
            internal_solid_infill_speed_mm_s=300,
            top_surface_speed_mm_s=250,
            support_speed_mm_s=180,
            support_interface_speed_mm_s=100,
            bridge_speed_mm_s=60,
            initial_layer_speed_mm_s=60,
            travel_speed_mm_s=500,
            default_acceleration_mm_s2=12_000,
            outer_wall_acceleration_mm_s2=6_000,
            top_surface_acceleration_mm_s2=3_000,
            initial_layer_acceleration_mm_s2=500,
            travel_acceleration_mm_s2=10_000,
        )
    # Balanced is a real three-way compromise: retain the geometry analysis,
    # but guarantee a useful structural shell instead of representing only the
    # midpoint between visual quality and speed.
    return replace(
        base,
        priority=selected,
        wall_loops=max(4, base.wall_loops),
        top_layers=max(5, base.top_layers),
        bottom_layers=max(5, base.bottom_layers),
        sparse_infill_percent=max(20, base.sparse_infill_percent),
        sparse_infill_pattern="gyroid",
    )


def settings_for_priority(
    base: PrintSettings,
    priority: str,
) -> PrintSettings:
    """Apply a priority while preserving geometry/material safety decisions."""
    selected = normalize_print_priority(priority)
    components = priority_components(selected)
    if len(components) == 1:
        return _single_priority_settings(base, components[0])

    candidates = [_single_priority_settings(base, item) for item in components]
    integer_fields = (
        "wall_loops", "top_layers", "bottom_layers", "sparse_infill_percent",
        "outer_wall_speed_mm_s", "inner_wall_speed_mm_s",
        "sparse_infill_speed_mm_s", "internal_solid_infill_speed_mm_s",
        "top_surface_speed_mm_s", "support_speed_mm_s",
        "support_interface_speed_mm_s", "bridge_speed_mm_s",
        "initial_layer_speed_mm_s", "travel_speed_mm_s",
        "default_acceleration_mm_s2", "outer_wall_acceleration_mm_s2",
        "top_surface_acceleration_mm_s2", "initial_layer_acceleration_mm_s2",
        "travel_acceleration_mm_s2",
    )
    blended: dict[str, object] = {
        name: round(sum(getattr(item, name) for item in candidates) / len(candidates))
        for name in integer_fields
    }
    blended["layer_height_mm"] = round(
        sum(item.layer_height_mm for item in candidates) / len(candidates), 3
    )
    # Pattern, material and safety fields stay with the analysed base model;
    # only the quality/speed axes are interpolated.
    return replace(base, priority=selected, **blended)


def settings_for_nozzle(base: PrintSettings, nozzle_diameter_mm: float) -> PrintSettings:
    """Scale geometry-dependent settings for the physically installed nozzle."""
    if abs(nozzle_diameter_mm - 0.4) <= 1e-6:
        return base
    components = priority_components(base.priority)
    factors = {"quality": 0.30, "strength": 0.50, "balanced": 0.50, "fast": 0.70}
    layer_factor = sum(factors[item] for item in components) / len(components)
    layer_height = round(nozzle_diameter_mm * layer_factor, 3)
    return replace(
        base,
        layer_height_mm=layer_height,
        initial_layer_height_mm=round(nozzle_diameter_mm * 0.5, 3),
        line_width_mm=round(nozzle_diameter_mm * 1.05, 3),
        top_surface_line_width_mm=round(nozzle_diameter_mm, 3),
        support_top_z_distance_mm=max(base.support_top_z_distance_mm, layer_height),
        support_bottom_z_distance_mm=max(base.support_bottom_z_distance_mm, layer_height),
        support_object_xy_distance_mm=max(
            base.support_object_xy_distance_mm, round(nozzle_diameter_mm, 3)
        ),
        support_interface_spacing_mm=max(
            base.support_interface_spacing_mm, round(nozzle_diameter_mm, 3)
        ),
    )
