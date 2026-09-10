"""Automatic model-purpose selection and purpose-specific print settings."""

from __future__ import annotations

from dataclasses import replace

from .report import PrintSettings, PurposeAssessment

MODEL_PURPOSE_MODES = ("auto", "decorative", "functional")


class ModelPurposeError(ValueError):
    """Raised when a model-purpose mode is invalid."""


def normalize_model_purpose(mode: str) -> str:
    normalized = mode.strip().casefold()
    if normalized not in MODEL_PURPOSE_MODES:
        raise ModelPurposeError(
            f"unsupported model purpose: {mode}; choose auto, decorative or functional"
        )
    return normalized


def select_model_purpose(assessment: PurposeAssessment, mode: str) -> str:
    selected = normalize_model_purpose(mode)
    if selected != "auto":
        return selected
    if assessment.classification in {"decorative", "functional"}:
        return assessment.classification
    return "functional" if assessment.functional_score >= 0.5 else "decorative"


def settings_for_model_purpose(
    base: PrintSettings,
    purpose: str,
    *,
    max_dimension_mm: float | None = None,
    curved_surface_ratio: float | None = None,
) -> PrintSettings:
    """Keep visible surfaces beautiful, then tune strength for the model purpose."""
    selected = normalize_model_purpose(purpose)
    if selected == "auto":
        raise ModelPurposeError("settings require a resolved decorative or functional purpose")
    priorities = {item for item in base.priority.split("+") if item}
    # Keep a mixed quality+speed choice blended.  Treating any presence of
    # quality as the pure-quality endpoint made ordinary compromises print at
    # the slowest layer height.
    quality_selected = priorities == {"quality"}
    fast_only = priorities == {"fast"}
    small_curved_model = (
        max_dimension_mm is not None
        and curved_surface_ratio is not None
        and max_dimension_mm <= 50.0
        and curved_surface_ratio >= 0.35
    )
    layer_height = base.layer_height_mm
    outer_wall_speed = min(120, base.outer_wall_speed_mm_s)
    outer_wall_acceleration = min(2_500, base.outer_wall_acceleration_mm_s2)
    top_surface_speed = min(80, base.top_surface_speed_mm_s)
    top_surface_acceleration = min(1_500, base.top_surface_acceleration_mm_s2)
    nozzle_temperature = base.nozzle_temperature_c
    detail_overrides: dict[str, object] = {}
    decorative_strength_overrides: dict[str, object] = {}
    if small_curved_model:
        if quality_selected:
            layer_height = min(0.08, layer_height)
            outer_wall_speed = min(60, outer_wall_speed)
            outer_wall_acceleration = min(1_000, outer_wall_acceleration)
            detail_nozzle_temperature = 215
            small_perimeter_speed_percent = 50
            small_perimeter_threshold_mm = 5.0
            slow_down_layer_time_s = 10
            slow_down_min_speed_mm_s = 20
            decorative_strength_overrides = {
                "wall_loops": min(3, base.wall_loops),
                "sparse_infill_percent": min(15, base.sparse_infill_percent),
            }
        elif fast_only:
            layer_height = min(0.16, layer_height)
            outer_wall_speed = min(120, base.outer_wall_speed_mm_s)
            outer_wall_acceleration = min(4_500, base.outer_wall_acceleration_mm_s2)
            top_surface_speed = min(120, base.top_surface_speed_mm_s)
            top_surface_acceleration = min(2_500, base.top_surface_acceleration_mm_s2)
            detail_nozzle_temperature = 210
            small_perimeter_speed_percent = 100
            small_perimeter_threshold_mm = 0.0
            slow_down_layer_time_s = 3
            slow_down_min_speed_mm_s = 35
            decorative_strength_overrides = {
                "wall_loops": min(2, base.wall_loops),
                "sparse_infill_percent": min(10, base.sparse_infill_percent),
            }
        else:
            layer_height = min(0.12, layer_height)
            outer_wall_speed = min(100, base.outer_wall_speed_mm_s)
            outer_wall_acceleration = min(3_500, base.outer_wall_acceleration_mm_s2)
            top_surface_speed = min(100, base.top_surface_speed_mm_s)
            top_surface_acceleration = min(2_000, base.top_surface_acceleration_mm_s2)
            detail_nozzle_temperature = 210
            small_perimeter_speed_percent = 100
            small_perimeter_threshold_mm = 0.0
            slow_down_layer_time_s = 4
            slow_down_min_speed_mm_s = 30
            decorative_strength_overrides = {
                "wall_loops": min(2, base.wall_loops),
                "sparse_infill_percent": min(10, base.sparse_infill_percent),
            }
        if nozzle_temperature <= 230:
            nozzle_temperature = min(detail_nozzle_temperature, nozzle_temperature)
        detail_overrides = {
            "surface_detail_profile": "small-curved",
            "seam_position": "back",
            "scarf_seam_type": "external",
            "override_filament_scarf_seam": True,
            "small_perimeter_speed_percent": small_perimeter_speed_percent,
            "small_perimeter_threshold_mm": small_perimeter_threshold_mm,
            "slow_down_layer_time_s": slow_down_layer_time_s,
            "slow_down_min_speed_mm_s": slow_down_min_speed_mm_s,
        }
    support_gap = max(0.20, layer_height)
    common = dict(
        model_purpose=selected,
        layer_height_mm=layer_height,
        nozzle_temperature_c=nozzle_temperature,
        top_surface_pattern="monotonicline",
        top_surface_line_width_mm=0.40,
        top_surface_density_percent=100,
        top_layers=max(6, base.top_layers),
        top_shell_thickness_mm=max(1.2, base.top_shell_thickness_mm),
        top_surface_speed_mm_s=top_surface_speed,
        top_surface_acceleration_mm_s2=top_surface_acceleration,
        outer_wall_speed_mm_s=outer_wall_speed,
        outer_wall_acceleration_mm_s2=outer_wall_acceleration,
        seam_placement_away_from_overhangs=True,
        # Organic decorative skins are more predictable with the classic
        # perimeter generator. Arachne remains useful for functional thin-wall
        # parts and is selected there explicitly below.
        wall_generator="classic" if selected == "decorative" else "arachne",
        support_top_z_distance_mm=support_gap,
        support_bottom_z_distance_mm=support_gap,
        support_object_xy_distance_mm=0.40,
        support_interface_top_layers=4 if selected == "decorative" else 3,
        support_interface_bottom_layers=2,
        support_interface_spacing_mm=0.28 if selected == "decorative" else 0.40,
        support_interface_speed_mm_s=min(
            55 if selected == "decorative" else 70,
            base.support_interface_speed_mm_s,
        ),
        **detail_overrides,
    )
    if selected == "functional":
        return replace(
            base,
            **common,
            wall_loops=max(5, base.wall_loops),
            bottom_layers=max(5, base.bottom_layers),
            sparse_infill_percent=max(30, base.sparse_infill_percent),
            sparse_infill_pattern="gyroid",
        )
    return replace(
        base,
        **common,
        **decorative_strength_overrides,
    )
