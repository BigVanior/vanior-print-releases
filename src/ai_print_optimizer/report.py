"""Serializable data models returned by the STL analyzer."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .functional_intent import FunctionalIntent
    from .geometry_features import (
        GeometryFeatureAnalysis,
        LocalModifierPlan,
        SupportExitPlan,
    )
    from .surface_intelligence import SurfaceIntelligence


@dataclass(frozen=True)
class GeometryMetrics:
    dimensions_mm: tuple[float, float, float]
    volume_mm3: float | None
    surface_area_mm2: float
    body_count: int
    triangle_count: int
    vertex_count: int
    is_watertight: bool
    base_area_mm2: float
    base_area_ratio: float
    overhang_area_mm2: float
    overhang_area_ratio: float
    overhang_angle_deg: float


@dataclass(frozen=True)
class BodyMetrics:
    index: int
    triangle_count: int
    surface_area_mm2: float
    dimensions_mm: tuple[float, float, float]
    volume_mm3: float | None
    boundary_edge_count: int
    non_manifold_edge_count: int
    is_watertight: bool
    is_debris: bool


@dataclass(frozen=True)
class MeshHealth:
    status: str
    topology_status: str
    repairability: str
    mesh_density: str
    triangles_per_mm2: float
    total_body_count: int
    meaningful_body_count: int
    debris_body_count: int
    boundary_edge_count: int
    non_manifold_edge_count: int
    raw_triangle_count: int
    ignored_triangle_count: int
    body_details_truncated: int
    bodies: tuple[BodyMetrics, ...]


@dataclass(frozen=True)
class OrientationCandidate:
    rank: int
    kind: str
    rotation_deg: tuple[float, float, float]
    dimensions_mm: tuple[float, float, float]
    base_area_mm2: float
    base_area_ratio: float
    overhang_area_mm2: float
    overhang_area_ratio: float
    fits_build_volume: bool
    score: float


@dataclass(frozen=True)
class OrientationAnalysis:
    candidates_evaluated: int
    current_score: float
    best_score: float
    score_improvement: float
    confidence: str
    top_candidates: tuple[OrientationCandidate, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskAssessment:
    bed_adhesion: str
    overhang: str
    tall_object: str
    support_requirement: str


@dataclass(frozen=True)
class PurposeAssessment:
    classification: str
    confidence: str
    functional_score: float
    axis_aligned_surface_ratio: float
    curved_surface_ratio: float
    bounding_box_fill_ratio: float | None
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PrintSettings:
    layer_height_mm: float
    wall_loops: int
    top_layers: int
    bottom_layers: int
    supports: bool
    brim: bool
    nozzle_temperature_c: int
    bed_temperature_c: int
    fan_percent: int
    priority: str = "balanced"
    sparse_infill_percent: int = 15
    sparse_infill_pattern: str = "gyroid"
    outer_wall_speed_mm_s: int = 200
    inner_wall_speed_mm_s: int = 300
    sparse_infill_speed_mm_s: int = 270
    internal_solid_infill_speed_mm_s: int = 250
    top_surface_speed_mm_s: int = 200
    support_speed_mm_s: int = 150
    support_interface_speed_mm_s: int = 80
    bridge_speed_mm_s: int = 50
    initial_layer_speed_mm_s: int = 50
    travel_speed_mm_s: int = 500
    default_acceleration_mm_s2: int = 10_000
    outer_wall_acceleration_mm_s2: int = 5_000
    top_surface_acceleration_mm_s2: int = 2_000
    initial_layer_acceleration_mm_s2: int = 500
    travel_acceleration_mm_s2: int = 10_000
    model_purpose: str = "decorative"
    top_surface_pattern: str = "monotonicline"
    top_surface_line_width_mm: float = 0.42
    top_surface_density_percent: int = 100
    top_shell_thickness_mm: float = 1.0
    seam_placement_away_from_overhangs: bool = True
    wall_generator: str = "classic"
    support_top_z_distance_mm: float = 0.2
    support_bottom_z_distance_mm: float = 0.2
    support_object_xy_distance_mm: float = 0.4
    support_interface_top_layers: int = 3
    support_interface_bottom_layers: int = 2
    support_interface_spacing_mm: float = 0.4
    surface_detail_profile: str = "standard"
    seam_position: str = "aligned"
    scarf_seam_type: str = "none"
    override_filament_scarf_seam: bool = False
    small_perimeter_speed_percent: int = 50
    small_perimeter_threshold_mm: float = 0.0
    slow_down_layer_time_s: int = 8
    slow_down_min_speed_mm_s: int = 20
    initial_layer_height_mm: float = 0.2
    line_width_mm: float = 0.42
    ironing_enabled: bool = False
    detect_thin_wall: bool = True
    detect_floating_vertical_shell: bool = True
    bridge_no_support: bool = False
    infill_combination: bool = False
    reduce_crossing_wall: bool = False
    avoid_crossing_wall_includes_support: bool = False
    reduce_infill_retraction_mode: str = "Auto"
    functional_intent: str = "decorative"
    elephant_foot_compensation_mm: float = 0.15
    max_volumetric_speed_mm3_s: float = 21.0
    filament_flow_ratio: float = 1.0
    enable_pressure_advance: bool = False
    pressure_advance_k: float = 0.0
    retraction_length_mm: float = 0.8
    retraction_speed_mm_s: float = 30.0
    wipe_enabled: bool = True
    wipe_distance_mm: float = 2.0


@dataclass(frozen=True)
class AnalysisReport:
    model_path: Path
    printer: str
    material: str
    fits_build_volume: bool
    metrics: GeometryMetrics
    health: MeshHealth
    risks: RiskAssessment
    purpose: PurposeAssessment
    surface_intelligence: SurfaceIntelligence
    settings: PrintSettings
    warnings: tuple[str, ...]
    geometry_features: GeometryFeatureAnalysis | None = None
    support_exit_plan: SupportExitPlan | None = None
    local_modifier_plan: LocalModifierPlan | None = None
    functional_intent: FunctionalIntent | None = None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["model_path"] = str(self.model_path)
        return result
