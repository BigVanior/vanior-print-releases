"""Reproducible analyze/repair/orient/slice pipeline."""

from __future__ import annotations

import json
import platform
import sys
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

from .analyzer import analyze_stl
from .builtin_profile import (
    BuiltinProfileError,
    GeneratedProfile,
    generate_builtin_profile,
)
from .config import SUPPORTED_NOZZLE_DIAMETERS_MM
from .functional_intent import (
    normalize_functional_intent,
    resolve_functional_intent,
    settings_for_functional_intent,
)
from .io_utils import atomic_write_new_bytes, atomic_write_new_json
from .material_protocol import MaterialProtocolError, settings_for_material
from .model_purpose import (
    ModelPurposeError,
    normalize_model_purpose,
    select_model_purpose,
    settings_for_model_purpose,
)
from .optimization_protocol import build_optimization_plan
from .orientation import OrientationExportResult, orient_stl
from .print_dna import (
    PrintDNAApplication,
    apply_print_dna,
    profile_from_dict,
)
from .print_priority import (
    PrintPriorityError,
    normalize_print_priority,
    settings_for_nozzle,
    settings_for_priority,
)
from .profile import (
    ProfileValidation,
    SourceProfileAssessment,
    assess_bambu_profile,
    validate_bambu_profile,
)
from .progress import ProgressCallback, check_cancelled, emit_progress
from .project3mf import (
    ExtractedProjectGeometry,
    ObjectPrintConfiguration,
    Project3MFError,
    extract_printable_stl,
    extract_stl_objects,
    rebuild_extracted_geometry,
)
from .repair import RepairResult, repair_stl
from .report import AnalysisReport, OrientationAnalysis, PrintSettings
from .schema import validate_release_document
from .simplification import (
    SimplificationError,
    SimplificationResult,
    simplify_stl,
)
from .slicer import (
    SliceRunResult,
    SupportComparison,
    _sha256,
    compare_stl_supports,
    discover_bambu_studio,
    optimize_multi_object_project,
)
from .surface_intelligence import settings_for_surfaces, with_surface_decisions
from .version import __version__


class PipelineError(RuntimeError):
    """Raised when the automatic pipeline cannot continue safely."""


@dataclass(frozen=True)
class ManifestVerification:
    manifest_path: Path
    valid: bool
    checked_files: int
    errors: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_path": str(self.manifest_path),
            "valid": self.valid,
            "checked_files": self.checked_files,
            "errors": list(self.errors),
        }


@dataclass(frozen=True)
class ObjectOptimizationResult:
    instance_index: int
    object_id: str
    name: str
    analysis: AnalysisReport
    support_mode: str
    orientation: OrientationAnalysis | None = None
    orientation_export: OrientationExportResult | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "instance_index": self.instance_index,
            "object_id": self.object_id,
            "name": self.name,
            "analysis": self.analysis.to_dict(),
            "support_mode": self.support_mode,
            "orientation": self.orientation.to_dict() if self.orientation else None,
            "orientation_export": (
                self.orientation_export.to_dict() if self.orientation_export else None
            ),
        }


@dataclass(frozen=True)
class PipelineResult:
    source_path: Path
    profile_template_path: Path
    output_dir: Path
    mode: str
    generated_profile: GeneratedProfile | None
    profile_validation: ProfileValidation
    source_profile_assessment: SourceProfileAssessment | None
    extracted_geometry: ExtractedProjectGeometry | None
    initial_analysis: AnalysisReport
    repair: RepairResult | None
    simplification: SimplificationResult | None
    simplification_note: str | None
    orientation: OrientationAnalysis
    orientation_export: OrientationExportResult
    final_analysis: AnalysisReport
    print_dna_application: PrintDNAApplication | None
    slice_result: SliceRunResult | None
    support_comparison: SupportComparison | None
    object_optimizations: tuple[ObjectOptimizationResult, ...]
    manifest_path: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "profile_template_path": str(self.profile_template_path),
            "output_dir": str(self.output_dir),
            "mode": self.mode,
            "generated_profile": (
                {
                    "path": str(self.generated_profile.path),
                    "printer_model": self.generated_profile.printer_model,
                    "printer_settings_id": self.generated_profile.printer_settings_id,
                    "process_settings_id": self.generated_profile.process_settings_id,
                    "filament_settings_id": self.generated_profile.filament_settings_id,
                    "material": self.generated_profile.material,
                    "nozzle_diameter_mm": self.generated_profile.nozzle_diameter_mm,
                    "bed_type": self.generated_profile.bed_type,
                    "setting_count": self.generated_profile.setting_count,
                    "source_files": [
                        str(path) for path in self.generated_profile.source_files
                    ],
                }
                if self.generated_profile
                else None
            ),
            "profile_validation": self.profile_validation.to_dict(),
            "source_profile_assessment": (
                self.source_profile_assessment.to_dict()
                if self.source_profile_assessment
                else None
            ),
            "extracted_geometry": (
                self.extracted_geometry.to_dict() if self.extracted_geometry else None
            ),
            "initial_analysis": self.initial_analysis.to_dict(),
            "repair": self.repair.to_dict() if self.repair else None,
            "simplification": (
                self.simplification.to_dict() if self.simplification else None
            ),
            "simplification_note": self.simplification_note,
            "orientation": self.orientation.to_dict(),
            "orientation_export": self.orientation_export.to_dict(),
            "final_analysis": self.final_analysis.to_dict(),
            "print_dna_application": (
                self.print_dna_application.to_dict()
                if self.print_dna_application
                else None
            ),
            "slice_result": self.slice_result.to_dict() if self.slice_result else None,
            "support_comparison": (
                self.support_comparison.to_dict() if self.support_comparison else None
            ),
            "object_optimizations": [
                item.to_dict() for item in self.object_optimizations
            ],
            "manifest_path": str(self.manifest_path),
        }


def _artifact_records(output_dir: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted(output_dir.rglob("*")):
        if not path.is_file() or ".bambu-data" in path.parts or path.name == "manifest.json":
            continue
        records.append(
            {
                "path": path.relative_to(output_dir).as_posix(),
                "size_bytes": path.stat().st_size,
                "sha256": _sha256(path),
            }
        )
    return records


def _write_decision_ledger(
    output_dir: Path,
    analysis: AnalysisReport,
    comparison: SupportComparison,
    object_optimizations: tuple[ObjectOptimizationResult, ...] = (),
) -> Path:
    """Persist the complete explainable chain behind the selected print file."""
    payload = {
        "schema_version": 1,
        "created_utc": datetime.now(UTC).isoformat(),
        "purpose": analysis.purpose.to_dict() if hasattr(analysis.purpose, "to_dict") else asdict(analysis.purpose),
        "functional_intent": (
            analysis.functional_intent.to_dict() if analysis.functional_intent else None
        ),
        "surface_intelligence": analysis.surface_intelligence.to_dict(),
        "geometry_features": (
            analysis.geometry_features.to_dict() if analysis.geometry_features else None
        ),
        "support_exit_plan": (
            analysis.support_exit_plan.to_dict() if analysis.support_exit_plan else None
        ),
        "local_modifier_plan": (
            analysis.local_modifier_plan.to_dict() if analysis.local_modifier_plan else None
        ),
        "selected_support_strategy": comparison.recommended,
        "support_reason": comparison.recommendation_reason,
        "quality_optimization": (
            comparison.quality_optimization.to_dict()
            if comparison.quality_optimization
            else None
        ),
        "final_settings": asdict(analysis.settings),
        "object_optimizations": [
            item.to_dict() for item in object_optimizations
        ],
    }
    path = output_dir / "decision-ledger.json"
    atomic_write_new_json(path, payload)
    return path


def _write_manifest(
    output_dir: Path,
    *,
    source_path: Path,
    source_sha256: str,
    template_path: Path,
    template_sha256: str,
    mode: str,
    generated_profile: GeneratedProfile | None,
    profile_validation: ProfileValidation,
    source_profile_assessment: SourceProfileAssessment | None,
    extracted_geometry: ExtractedProjectGeometry | None,
    initial_analysis: AnalysisReport,
    repair: RepairResult | None,
    simplification: SimplificationResult | None,
    simplification_note: str | None,
    orientation: OrientationAnalysis,
    orientation_export: OrientationExportResult,
    final_analysis: AnalysisReport,
    print_dna_application: PrintDNAApplication | None,
    slice_result: SliceRunResult | None,
    support_comparison: SupportComparison | None,
    object_optimizations: tuple[ObjectOptimizationResult, ...] = (),
) -> Path:
    slicer_path = (
        slice_result.slicer_path
        if slice_result is not None
        else support_comparison.normal.slicer_path  # type: ignore[union-attr]
    )
    payload = {
        "schema_version": 1,
        "application": {"name": "ai-print-optimizer", "version": __version__},
        "created_utc": datetime.now(UTC).isoformat(),
        "runtime": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "slicer_path": str(slicer_path),
            "slicer_sha256": _sha256(slicer_path),
        },
        "inputs": [
            {
                "role": f"source_{source_path.suffix.lower().lstrip('.')}",
                "path": str(source_path),
                "sha256": source_sha256,
            },
            {
                "role": "profile_template_3mf",
                "path": str(template_path),
                "sha256": template_sha256,
            },
        ],
        "parameters": {
            "mode": mode,
            "material": final_analysis.material,
            "print_priority": final_analysis.settings.priority,
            "model_purpose": final_analysis.settings.model_purpose,
            "automatic_model_classification": final_analysis.purpose.classification,
            "overhang_angle_deg": final_analysis.metrics.overhang_angle_deg,
        },
        "stages": {
            "generated_profile": (
                {
                    "path": str(generated_profile.path),
                    "printer_model": generated_profile.printer_model,
                    "printer_settings_id": generated_profile.printer_settings_id,
                    "process_settings_id": generated_profile.process_settings_id,
                    "filament_settings_id": generated_profile.filament_settings_id,
                    "material": generated_profile.material,
                    "nozzle_diameter_mm": generated_profile.nozzle_diameter_mm,
                    "bed_type": generated_profile.bed_type,
                    "setting_count": generated_profile.setting_count,
                    "source_files": [str(path) for path in generated_profile.source_files],
                }
                if generated_profile
                else None
            ),
            "profile_validation": profile_validation.to_dict(),
            "source_profile_assessment": (
                source_profile_assessment.to_dict()
                if source_profile_assessment
                else None
            ),
            "extracted_geometry": (
                extracted_geometry.to_dict() if extracted_geometry else None
            ),
            "initial_analysis": initial_analysis.to_dict(),
            "repair": repair.to_dict() if repair else None,
            "simplification": simplification.to_dict() if simplification else None,
            "simplification_note": simplification_note,
            "orientation": orientation.to_dict(),
            "orientation_export": orientation_export.to_dict(),
            "final_analysis": final_analysis.to_dict(),
            "print_dna_application": (
                print_dna_application.to_dict() if print_dna_application else None
            ),
            "slice_result": slice_result.to_dict() if slice_result else None,
            "support_comparison": (
                support_comparison.to_dict() if support_comparison else None
            ),
            "object_optimizations": [
                item.to_dict() for item in object_optimizations
            ],
        },
        "artifacts": _artifact_records(output_dir),
    }
    manifest_path = output_dir / "manifest.json"
    schema_errors = validate_release_document(payload, "pipeline-manifest")
    if schema_errors:
        raise PipelineError("generated manifest violates release schema: " + "; ".join(schema_errors))
    try:
        atomic_write_new_json(manifest_path, payload)
    except OSError as exc:
        raise PipelineError(f"cannot write pipeline manifest: {manifest_path}") from exc
    return manifest_path


def verify_manifest(manifest: str | Path) -> ManifestVerification:
    """Verify every input and artifact hash recorded by a pipeline manifest."""
    manifest_path = Path(manifest).expanduser().resolve()
    errors: list[str] = []
    checked = 0
    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PipelineError(f"cannot read pipeline manifest: {manifest_path}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise PipelineError("unsupported or malformed pipeline manifest")
    errors.extend(validate_release_document(payload, "pipeline-manifest"))

    candidates: list[tuple[str, Path, str]] = []
    for item in payload.get("inputs", []):
        if not isinstance(item, dict):
            errors.append("malformed input record")
            continue
        candidates.append(
            (str(item.get("role", "input")), Path(str(item.get("path", ""))), str(item.get("sha256", "")))
        )
    root = manifest_path.parent
    for item in payload.get("artifacts", []):
        if not isinstance(item, dict):
            errors.append("malformed artifact record")
            continue
        relative = Path(str(item.get("path", "")))
        artifact_path = (root / relative).resolve()
        if artifact_path != root and root not in artifact_path.parents:
            errors.append(f"artifact path escapes manifest directory: {relative}")
            continue
        candidates.append(
            (f"artifact:{relative.as_posix()}", artifact_path, str(item.get("sha256", "")))
        )

    for role, path, expected_hash in candidates:
        if not path.is_file():
            errors.append(f"missing {role}: {path}")
            continue
        checked += 1
        actual_hash = _sha256(path)
        if not expected_hash or actual_hash.casefold() != expected_hash.casefold():
            errors.append(f"SHA-256 mismatch for {role}: {path}")
    return ManifestVerification(
        manifest_path=manifest_path,
        valid=not errors,
        checked_files=checked,
        errors=tuple(errors),
    )


def _configure_analysis_for_print(
    report: AnalysisReport,
    *,
    print_priority: str,
    model_purpose: str,
    functional_intent: str,
    print_setting_overrides: dict[str, Any] | None,
    print_dna_profile: dict[str, Any] | None,
    material_profile: str | None = None,
    source_profile_assessment: SourceProfileAssessment | None = None,
    nozzle_diameter_mm: float = 0.4,
) -> tuple[AnalysisReport, PrintDNAApplication | None]:
    """Apply the full decision stack to one geometry report."""
    selected_purpose = select_model_purpose(report.purpose, model_purpose)
    priority_settings = settings_for_nozzle(
        settings_for_priority(report.settings, print_priority),
        nozzle_diameter_mm,
    )
    configured = replace(
        report,
        settings=settings_for_model_purpose(
            priority_settings,
            selected_purpose,
            max_dimension_mm=max(report.metrics.dimensions_mm),
            curved_surface_ratio=report.purpose.curved_surface_ratio,
        ),
    )
    if source_profile_assessment is not None:
        guardrails = source_profile_assessment.guardrails
        calibrated: dict[str, Any] = {}
        # A supplied 3MF is a measured print baseline. Improve it within one
        # small process step instead of replacing a proven 0.20–0.25 mm layer
        # and established extrusion widths with a generic quality endpoint.
        mixed_priority = configured.settings.priority != "quality"
        source_layer = guardrails.get("layer_height_mm")
        if mixed_priority and isinstance(source_layer, (int, float)) and 0.08 <= float(source_layer) <= nozzle_diameter_mm * 0.8:
            calibrated["layer_height_mm"] = max(
                configured.settings.layer_height_mm,
                round(max(0.10, float(source_layer) - 0.02), 3),
            )
        source_walls = guardrails.get("wall_loops")
        if mixed_priority and isinstance(source_walls, int) and 1 <= source_walls <= 8:
            # A third wall increases a small PETG benchmark's path count and
            # hot-nozzle dwell without improving the visible surface. Preserve
            # the author's proven shell unless the user selects pure quality.
            calibrated["wall_loops"] = source_walls
        source_top = guardrails.get("top_layers")
        if mixed_priority and isinstance(source_top, int) and 1 <= source_top <= 20:
            calibrated["top_layers"] = max(configured.settings.top_layers, source_top)
        source_bottom = guardrails.get("bottom_layers")
        if mixed_priority and isinstance(source_bottom, int) and 1 <= source_bottom <= 20:
            calibrated["bottom_layers"] = max(configured.settings.bottom_layers, source_bottom)
        source_infill = guardrails.get("sparse_infill_percent")
        if mixed_priority and isinstance(source_infill, int) and 0 <= source_infill <= 100:
            calibrated["sparse_infill_percent"] = max(8, min(configured.settings.sparse_infill_percent, source_infill))
        for name, minimum, maximum in (
            ("line_width_mm", nozzle_diameter_mm * 0.90, nozzle_diameter_mm * 1.50),
            ("top_surface_line_width_mm", nozzle_diameter_mm * 0.90, nozzle_diameter_mm * 1.50),
        ):
            value = guardrails.get(name)
            if mixed_priority and isinstance(value, (int, float)) and minimum <= float(value) <= maximum:
                calibrated[name] = float(value)
        source_top_pattern = str(guardrails.get("top_surface_pattern", "")).strip()
        if mixed_priority and source_top_pattern:
            calibrated["top_surface_pattern"] = source_top_pattern
        source_flow = guardrails.get("filament_flow_ratio")
        if isinstance(source_flow, (int, float)) and 0.85 <= float(source_flow) <= 1.15:
            calibrated["filament_flow_ratio"] = float(source_flow)
        source_mvs = guardrails.get("max_volumetric_speed_mm3_s")
        if isinstance(source_mvs, (int, float)) and 2.0 <= float(source_mvs) <= 40.0:
            calibrated["max_volumetric_speed_mm3_s"] = min(
                configured.settings.max_volumetric_speed_mm3_s,
                float(source_mvs),
            )
        source_temp = guardrails.get("nozzle_temperature_c")
        if isinstance(source_temp, int) and 185 <= source_temp <= 260:
            # Temperature is a material/spool calibration. Preserve it here;
            # PrintDNA may then make a small evidence-backed correction.
            calibrated["nozzle_temperature_c"] = source_temp
        source_bed = guardrails.get("bed_temperature_c")
        if isinstance(source_bed, int) and 35 <= source_bed <= 100:
            calibrated["bed_temperature_c"] = source_bed
        source_fan = guardrails.get("fan_percent")
        if isinstance(source_fan, int) and 0 <= source_fan <= 100:
            calibrated["fan_percent"] = source_fan
        for name, minimum, maximum in (
            ("retraction_length_mm", 0.2, 2.0),
            ("retraction_speed_mm_s", 10.0, 60.0),
            ("wipe_distance_mm", 0.0, 10.0),
        ):
            value = guardrails.get(name)
            if isinstance(value, (int, float)) and minimum <= float(value) <= maximum:
                calibrated[name] = float(value)
        wipe = str(guardrails.get("wipe_enabled", "")).strip().casefold()
        if wipe in {"1", "true", "yes", "on"}:
            calibrated["wipe_enabled"] = True
        elif wipe in {"0", "false", "no", "off"}:
            calibrated["wipe_enabled"] = False
        pressure_advance = str(
            guardrails.get("enable_pressure_advance", "")
        ).strip().casefold()
        pressure_advance_k = guardrails.get("pressure_advance_k")
        if pressure_advance in {"1", "true", "yes", "on"} and isinstance(
            pressure_advance_k, (int, float)
        ) and 0.0 <= float(pressure_advance_k) <= 0.2:
            calibrated["enable_pressure_advance"] = True
            calibrated["pressure_advance_k"] = float(pressure_advance_k)
        if configured.settings.model_purpose == "decorative":
            quality_only = configured.settings.priority == "quality"
            source_outer_speed = guardrails.get("outer_wall_speed_mm_s")
            source_inner_speed = guardrails.get("inner_wall_speed_mm_s")
            source_solid_speed = guardrails.get("internal_solid_infill_speed_mm_s")
            source_top_speed = guardrails.get("top_surface_speed_mm_s")
            source_bridge_speed = guardrails.get("bridge_speed_mm_s")
            source_default_acceleration = guardrails.get("default_acceleration_mm_s2")
            if not quality_only and isinstance(source_outer_speed, int) and 40 <= source_outer_speed <= 400:
                calibrated["outer_wall_speed_mm_s"] = max(
                    configured.settings.outer_wall_speed_mm_s,
                    min(source_outer_speed, 140),
                )
            if not quality_only and isinstance(source_inner_speed, int) and 40 <= source_inner_speed <= 500:
                calibrated["inner_wall_speed_mm_s"] = max(
                    configured.settings.inner_wall_speed_mm_s,
                    min(source_inner_speed, 220),
                )
            if not quality_only and isinstance(source_solid_speed, int) and 30 <= source_solid_speed <= 400:
                calibrated["internal_solid_infill_speed_mm_s"] = max(
                    configured.settings.internal_solid_infill_speed_mm_s,
                    min(source_solid_speed, 170),
                )
            if not quality_only and isinstance(source_top_speed, int) and 40 <= source_top_speed <= 400:
                calibrated["top_surface_speed_mm_s"] = max(
                    configured.settings.top_surface_speed_mm_s,
                    min(source_top_speed, 100),
                )
            if not quality_only and isinstance(source_bridge_speed, int) and 20 <= source_bridge_speed <= 200:
                calibrated["bridge_speed_mm_s"] = max(
                    configured.settings.bridge_speed_mm_s,
                    min(source_bridge_speed, 45),
                )
            if not quality_only and isinstance(source_default_acceleration, int) and 500 <= source_default_acceleration <= 20_000:
                calibrated["default_acceleration_mm_s2"] = max(
                    configured.settings.default_acceleration_mm_s2,
                    min(source_default_acceleration, 9_000),
                )
            calibrated.update(
                wall_generator="classic",
                outer_wall_speed_mm_s=calibrated.get("outer_wall_speed_mm_s", min(configured.settings.outer_wall_speed_mm_s, 80)),
                outer_wall_acceleration_mm_s2=min(
                    configured.settings.outer_wall_acceleration_mm_s2,
                    int(guardrails.get("outer_wall_acceleration_mm_s2", 1_500) or 1_500),
                    2_500 if not quality_only else 1_500,
                ),
            )
        configured = replace(configured, settings=replace(configured.settings, **calibrated))
    if source_profile_assessment is not None:
        configured = replace(
            configured,
            surface_intelligence=with_surface_decisions(
                configured.surface_intelligence,
                configured.surface_intelligence.recommendations
                + (
                    "Исходный 3MF оценён: сохранены допустимые калибровки материала; требования выбранного назначения имеют приоритет.",
                ),
            ),
        )
    resolved_intent = resolve_functional_intent(
        configured.functional_intent, functional_intent
    )
    intent_settings, intent_decisions = settings_for_functional_intent(
        configured.settings, resolved_intent
    )
    configured = replace(
        configured,
        settings=intent_settings,
        functional_intent=replace(
            resolved_intent,
            reasons=resolved_intent.reasons + intent_decisions,
        ),
    )
    surface_settings, surface_decisions = settings_for_surfaces(
        configured.settings, configured.surface_intelligence
    )
    configured = replace(
        configured,
        settings=surface_settings,
        surface_intelligence=with_surface_decisions(
            configured.surface_intelligence,
            surface_decisions or configured.surface_intelligence.recommendations,
        ),
    )
    if source_profile_assessment is not None and configured.settings.priority != "quality":
        # Surface intelligence and PETG safety run after purpose selection and
        # may otherwise lower the blended layer again. Reapply the single-step
        # source guardrail at this final point, before material-specific gaps
        # and support speeds are calculated.
        guardrails = source_profile_assessment.guardrails
        source_layer = guardrails.get("layer_height_mm")
        process: dict[str, Any] = {}
        if isinstance(source_layer, (int, float)) and 0.08 <= float(source_layer) <= nozzle_diameter_mm * 0.8:
            process["layer_height_mm"] = max(
                configured.settings.layer_height_mm,
                round(max(0.10, float(source_layer) - 0.02), 3),
            )
        source_walls = guardrails.get("wall_loops")
        if isinstance(source_walls, int) and 1 <= source_walls <= 8:
            process["wall_loops"] = max(configured.settings.wall_loops, source_walls)
        source_infill = guardrails.get("sparse_infill_percent")
        if isinstance(source_infill, int) and 0 <= source_infill <= 100:
            process["sparse_infill_percent"] = max(
                configured.settings.sparse_infill_percent, source_infill
            )
        for name, minimum, maximum in (
            ("line_width_mm", nozzle_diameter_mm * 0.90, nozzle_diameter_mm * 1.50),
            ("top_surface_line_width_mm", nozzle_diameter_mm * 0.90, nozzle_diameter_mm * 1.50),
        ):
            value = guardrails.get(name)
            if isinstance(value, (int, float)) and minimum <= float(value) <= maximum:
                process[name] = float(value)
        source_top_pattern = str(guardrails.get("top_surface_pattern", "")).strip()
        if source_top_pattern:
            process["top_surface_pattern"] = source_top_pattern
        if process:
            configured = replace(
                configured,
                settings=replace(configured.settings, **process),
            )
    dna_application: PrintDNAApplication | None = None
    resolved_print_dna = profile_from_dict(print_dna_profile)
    if resolved_print_dna is not None and resolved_print_dna.sample_count > 0:
        dna_application = apply_print_dna(configured.settings, resolved_print_dna)
        configured = replace(configured, settings=dna_application.settings)
    if print_setting_overrides:
        allowed = {item.name for item in fields(configured.settings)} - {
            "priority",
            "model_purpose",
        }
        unknown = sorted(set(print_setting_overrides) - allowed)
        if unknown:
            raise PipelineError(
                "unsupported print setting overrides: " + ", ".join(unknown)
            )
        configured = replace(
            configured,
            settings=replace(configured.settings, **print_setting_overrides),
        )
    try:
        material_settings, material_decisions = settings_for_material(
            configured.settings,
            material_profile or configured.material,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )
    except MaterialProtocolError as exc:
        raise PipelineError(str(exc)) from exc
    configured = replace(
        configured,
        settings=material_settings,
        surface_intelligence=with_surface_decisions(
            configured.surface_intelligence,
            configured.surface_intelligence.recommendations + material_decisions,
        ),
    )
    return configured, dna_application


def _source_time_budget_ratio(priority: str) -> float:
    """Maximum real-slice time growth allowed over a tuned source 3MF."""
    selected = {item for item in priority.split("+") if item}
    if selected == {"quality"}:
        return 1.50
    if selected == {"strength"}:
        return 1.50
    if selected == {"fast"}:
        return 1.05
    if selected == {"balanced"}:
        return 1.15
    if "quality" in selected and "balanced" in selected:
        return 1.25
    if "quality" in selected and "fast" in selected:
        return 1.20
    return 1.15


def _source_profile_baseline_settings(
    base: PrintSettings,
    assessment: SourceProfileAssessment,
) -> PrintSettings:
    """Recreate the author's process on the selected safe printer profile."""
    guardrails = assessment.guardrails
    changes: dict[str, Any] = {}
    numeric_ranges: dict[str, tuple[float, float]] = {
        "layer_height_mm": (0.08, 0.32),
        "line_width_mm": (0.35, 0.60),
        "top_surface_line_width_mm": (0.35, 0.60),
        "wall_loops": (1, 8),
        "top_layers": (1, 20),
        "bottom_layers": (1, 20),
        "sparse_infill_percent": (0, 100),
        "nozzle_temperature_c": (190, 265),
        "bed_temperature_c": (45, 90),
        "fan_percent": (0, 100),
        "outer_wall_speed_mm_s": (20, 400),
        "inner_wall_speed_mm_s": (20, 500),
        "internal_solid_infill_speed_mm_s": (20, 400),
        "top_surface_speed_mm_s": (20, 400),
        "bridge_speed_mm_s": (10, 200),
        "default_acceleration_mm_s2": (300, 20_000),
        "outer_wall_acceleration_mm_s2": (300, 20_000),
        "top_surface_acceleration_mm_s2": (300, 20_000),
        "support_top_z_distance_mm": (0.08, 0.50),
        "support_bottom_z_distance_mm": (0.08, 0.50),
        "support_object_xy_distance_mm": (0.20, 1.00),
        "support_interface_top_layers": (0, 10),
        "support_interface_bottom_layers": (0, 10),
        "support_interface_spacing_mm": (0.10, 1.00),
        "support_interface_speed_mm_s": (10, 150),
        "travel_speed_mm_s": (100, 800),
        "max_volumetric_speed_mm3_s": (2.0, 30.0),
        "filament_flow_ratio": (0.85, 1.15),
        "retraction_length_mm": (0.2, 2.0),
        "retraction_speed_mm_s": (10.0, 60.0),
        "wipe_distance_mm": (0.0, 10.0),
    }
    integer_fields = {
        "wall_loops",
        "top_layers",
        "bottom_layers",
        "sparse_infill_percent",
        "nozzle_temperature_c",
        "bed_temperature_c",
        "fan_percent",
        "outer_wall_speed_mm_s",
        "inner_wall_speed_mm_s",
        "internal_solid_infill_speed_mm_s",
        "top_surface_speed_mm_s",
        "bridge_speed_mm_s",
        "default_acceleration_mm_s2",
        "outer_wall_acceleration_mm_s2",
        "top_surface_acceleration_mm_s2",
        "support_interface_top_layers",
        "support_interface_bottom_layers",
        "support_interface_speed_mm_s",
        "travel_speed_mm_s",
    }
    for name, (minimum, maximum) in numeric_ranges.items():
        value = guardrails.get(name)
        if isinstance(value, (int, float)) and minimum <= float(value) <= maximum:
            changes[name] = round(value) if name in integer_fields else float(value)
    top_pattern = str(guardrails.get("top_surface_pattern", "")).strip()
    if top_pattern:
        changes["top_surface_pattern"] = top_pattern
    wall_generator = str(guardrails.get("wall_generator", "")).strip()
    if wall_generator in {"classic", "arachne"}:
        changes["wall_generator"] = wall_generator
    seam = str(guardrails.get("seam_position", "")).strip()
    if seam:
        changes["seam_position"] = seam
    for name in ("reduce_crossing_wall", "wipe_enabled"):
        raw = str(guardrails.get(name, "")).strip().casefold()
        if raw in {"1", "true", "yes", "on"}:
            changes[name] = True
        elif raw in {"0", "false", "no", "off"}:
            changes[name] = False
    return replace(base, **changes)


def _object_support_mode(report: AnalysisReport, requested: str) -> str:
    if requested != "auto":
        return requested
    requirement = report.risks.support_requirement.upper()
    if requirement in {"HIGH", "REQUIRED"}:
        if (
            report.support_exit_plan is not None
            and report.support_exit_plan.overall_risk == "HIGH"
        ):
            return "tree"
        return "tree"
    if requirement in {"MEDIUM", "RECOMMENDED"} or report.settings.supports:
        return "normal"
    return "none"


def _safe_for_slicer_recovery(report: AnalysisReport) -> bool:
    """Allow the slicer to recover only small, bounded topology defects.

    A non-watertight STL is not automatically unusable.  Exporters commonly
    leave a handful of microscopic seams or T-junctions which the slicer can
    resolve without changing the printable shape.  The old pipeline rejected
    every ``POSSIBLE`` repair before the slicer was even tried.  Keep the hard
    stop for empty, ambiguous or broadly damaged meshes, but permit a verified
    slicing attempt when defects are both absolutely small and sparse.

    The ready-project geometry and generated G-code are still validated by the
    normal slicing pipeline, so passing this gate is permission to *attempt*
    recovery, never permission to publish an unchecked result.
    """
    health = report.health
    metrics = report.metrics
    if health.status == "READY":
        return True
    if health.status == "INVALID" or health.repairability not in {
        "LIKELY_AUTOMATIC",
        "POSSIBLE",
    }:
        return False
    if health.meaningful_body_count < 1 or metrics.triangle_count < 4:
        return False
    if any(float(value) <= 1e-9 for value in metrics.dimensions_mm):
        return False

    boundary_edges = int(health.boundary_edge_count)
    non_manifold_edges = int(health.non_manifold_edge_count)
    defect_edges = boundary_edges + non_manifold_edges
    if boundary_edges > 64 or non_manifold_edges > 16:
        return False
    return defect_edges / max(1, int(health.raw_triangle_count)) <= 0.005


def run_pipeline(
    source: str | Path,
    profile_template: str | Path | None,
    output: str | Path,
    *,
    material: str = "PLA",
    material_profile: str | None = None,
    printer_model: str = "Bambu Lab P1S",
    nozzle_diameter_mm: float = 0.4,
    bed_type: str = "Textured PEI Plate",
    print_priority: str = "balanced",
    model_purpose: str = "auto",
    functional_intent: str = "auto",
    print_setting_overrides: dict[str, Any] | None = None,
    print_dna_profile: dict[str, Any] | None = None,
    quality_search: bool = True,
    support_strategy: str = "auto",
    overhang_angle_deg: float = 45.0,
    max_hole_diameter_mm: float = 5.0,
    executable: str | Path | None = None,
    timeout_s: float = 300.0,
    plate: int = 1,
    simplify_threshold_faces: int = 500_000,
    simplify_ratio: float = 0.6,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
    slice_cache_dir: str | Path | None = None,
) -> PipelineResult:
    """Run the safe automatic STL/3MF workflow and write ready print files."""
    emit_progress(progress_callback, "validate", "Проверка исходных файлов", 2)
    check_cancelled(cancel_event)
    source_path = Path(source).expanduser().resolve()
    suffix = source_path.suffix.lower()
    if suffix not in {".stl", ".3mf"} or not source_path.is_file():
        raise PipelineError("pipeline source must be an existing STL or 3MF file")
    output_dir = Path(output).expanduser().resolve()
    if output_dir.exists():
        raise PipelineError(f"pipeline output already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise PipelineError(f"pipeline output parent not found: {output_dir.parent}")
    if timeout_s <= 0:
        raise PipelineError("slice timeout must be positive")
    if plate < 1:
        raise PipelineError("plate must be a positive one-based index")
    if simplify_threshold_faces < 100 or not 0.1 <= simplify_ratio < 1.0:
        raise PipelineError("invalid automatic simplification threshold or ratio")
    try:
        print_priority = normalize_print_priority(print_priority)
    except PrintPriorityError as exc:
        raise PipelineError(str(exc)) from exc
    try:
        model_purpose = normalize_model_purpose(model_purpose)
    except ModelPurposeError as exc:
        raise PipelineError(str(exc)) from exc
    try:
        functional_intent = normalize_functional_intent(functional_intent)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    if printer_model != "Bambu Lab P1S":
        raise PipelineError("built-in profile generation currently supports Bambu Lab P1S")
    selected_nozzle = next(
        (
            supported
            for supported in SUPPORTED_NOZZLE_DIAMETERS_MM
            if abs(nozzle_diameter_mm - supported) <= 1e-6
        ),
        None,
    )
    if selected_nozzle is None:
        raise PipelineError("supported nozzle diameters are 0.2, 0.4, 0.6 and 0.8 mm")
    if support_strategy not in {"auto", "none", "normal", "tree"}:
        raise PipelineError("support strategy must be auto, none, normal or tree")

    source_sha256 = _sha256(source_path)
    output_created = False
    artifacts_dir = output_dir / "artifacts"
    generated_profile: GeneratedProfile | None = None
    source_profile_assessment: SourceProfileAssessment | None = None
    source_profile_validation: ProfileValidation | None = None
    if profile_template is None and suffix == ".3mf":
        # A 3MF supplied by the author is the most relevant baseline. Keep its
        # printer/filament calibration and improve the process settings over it
        # instead of replacing everything with generic factory defaults.
        try:
            source_profile_assessment = assess_bambu_profile(source_path)
            source_profile_validation = validate_bambu_profile(
                source_path,
                expected_printer=printer_model,
                expected_nozzle_mm=nozzle_diameter_mm,
                expected_material=material,
            )
            if (
                source_profile_validation.valid
                and source_profile_validation.bed_type.casefold() == bed_type.casefold()
            ):
                template_path = source_path
                executable = discover_bambu_studio(executable)
            else:
                source_profile_validation = None
        except Exception:  # noqa: BLE001 -- optional source metadata must not abort repair
            source_profile_validation = None

    if profile_template is None and source_profile_validation is None:
        emit_progress(
            progress_callback,
            "profile",
            "Создание профиля P1S из настроек Bambu Studio",
            5,
        )
        check_cancelled(cancel_event)
        try:
            slicer_path = discover_bambu_studio(executable)
            output_dir.mkdir()
            output_created = True
            artifacts_dir.mkdir()
            process_by_nozzle = {
                0.2: "0.10mm Standard @BBL X1C 0.2 nozzle",
                0.4: "0.20mm Standard @BBL X1C",
                0.6: "0.30mm Standard @BBL X1C 0.6 nozzle",
                0.8: "0.40mm Standard @BBL X1C 0.8 nozzle",
            }
            generated_profile = generate_builtin_profile(
                artifacts_dir / "generated-profile.3mf",
                bambu_studio=slicer_path,
                material=material,
                bed_type=bed_type,
                machine_name=f"Bambu Lab P1S {selected_nozzle:.1f} nozzle",
                process_name=process_by_nozzle[selected_nozzle],
            )
            template_path = generated_profile.path
            executable = slicer_path
        except (BuiltinProfileError, OSError) as exc:
            raise PipelineError(f"cannot generate built-in Bambu profile: {exc}") from exc
    elif profile_template is not None:
        template_path = Path(profile_template).expanduser().resolve()
        if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
            raise PipelineError("pipeline requires an existing Bambu 3MF profile template")

    template_sha256 = _sha256(template_path)
    emit_progress(progress_callback, "profile", "Проверка профиля печати", 6)
    check_cancelled(cancel_event)
    try:
        profile_validation = source_profile_validation or validate_bambu_profile(
            template_path,
            expected_printer=printer_model,
            expected_nozzle_mm=nozzle_diameter_mm,
            expected_material=material,
        )
    except Exception as exc:
        raise PipelineError(f"cannot validate profile template: {exc}") from exc
    if not profile_validation.valid:
        raise PipelineError(
            "profile validation failed: " + "; ".join(profile_validation.errors)
        )
    extracted_geometry: ExtractedProjectGeometry | None = None
    analysis_source = source_path
    if suffix == ".3mf":
        emit_progress(progress_callback, "extract", "Извлечение геометрии из 3MF", 12)
        check_cancelled(cancel_event)
        if not output_created:
            output_dir.mkdir()
            output_created = True
            artifacts_dir.mkdir()
        try:
            extracted_geometry = extract_printable_stl(
                source_path,
                artifacts_dir / "model.extracted.stl",
                plate=plate,
            )
        except Project3MFError as exc:
            raise PipelineError(f"cannot extract printable 3MF geometry: {exc}") from exc
        analysis_source = extracted_geometry.output_stl_path
    else:
        emit_progress(
            progress_callback,
            "extract",
            "Проверка STL на несколько самостоятельных моделей",
            12,
        )
        check_cancelled(cancel_event)
        if not output_created:
            output_dir.mkdir()
            output_created = True
            artifacts_dir.mkdir()
        try:
            extracted_geometry = extract_stl_objects(
                source_path,
                artifacts_dir / "model.extracted.stl",
                object_output_dir=artifacts_dir / "model.objects",
            )
        except Project3MFError as exc:
            raise PipelineError(f"cannot inspect STL bodies: {exc}") from exc
        analysis_source = extracted_geometry.output_stl_path
    emit_progress(progress_callback, "analyze", "Анализ геометрии модели", 20)
    check_cancelled(cancel_event)
    initial = analyze_stl(
        analysis_source,
        material=material,
        overhang_angle_deg=overhang_angle_deg,
        nozzle_diameter_mm=nozzle_diameter_mm,
    )
    if initial.health.status == "INVALID":
        raise PipelineError("source mesh is invalid and cannot enter the automatic pipeline")
    if not _safe_for_slicer_recovery(initial):
        raise PipelineError(
            "source mesh requires manual repair; automatic pipeline stopped before slicing"
        )

    if not output_created:
        output_dir.mkdir()
        artifacts_dir = output_dir / "artifacts"
        artifacts_dir.mkdir()
    working_path = analysis_source
    repair_result: RepairResult | None = None
    simplification_result: SimplificationResult | None = None
    simplification_note: str | None = None
    working_analysis = initial
    if initial.health.status != "READY":
        emit_progress(progress_callback, "repair", "Безопасное исправление геометрии", 30)
        check_cancelled(cancel_event)
        repair_result = repair_stl(
            working_path,
            artifacts_dir / "model.repaired.stl",
            max_hole_diameter_mm=max_hole_diameter_mm,
        )
        working_path = repair_result.output_path
        working_analysis = analyze_stl(
            working_path,
            material=material,
            overhang_angle_deg=overhang_angle_deg,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )
        if not _safe_for_slicer_recovery(working_analysis):
            raise PipelineError(
                "automatic repair could not reduce the mesh defects to a safe slicing "
                "threshold; partial artifacts were retained"
            )
        if working_analysis.health.status != "READY":
            emit_progress(
                progress_callback,
                "repair",
                "Незначительные дефекты проверит встроенный слайсер",
                34,
            )

    if working_analysis.metrics.triangle_count > simplify_threshold_faces:
        emit_progress(progress_callback, "simplify", "Оптимизация сложной сетки", 38)
        check_cancelled(cancel_event)
        target_faces = max(100, int(working_analysis.metrics.triangle_count * simplify_ratio))
        try:
            simplification_result = simplify_stl(
                working_path,
                artifacts_dir / "model.simplified.stl",
                target_faces=target_faces,
            )
            working_path = simplification_result.output_path
        except SimplificationError as exc:
            simplification_note = (
                "automatic simplification was skipped after safety validation: " + str(exc)
            )

    emit_progress(progress_callback, "orient", "Поиск оптимальной ориентации", 46)
    check_cancelled(cancel_event)
    orientation, orientation_export = orient_stl(
        working_path,
        artifacts_dir / "model.oriented.stl",
        overhang_angle_deg=overhang_angle_deg,
        protect_visible_surfaces=(
            select_model_purpose(initial.purpose, model_purpose) == "decorative"
            and initial.purpose.curved_surface_ratio >= 0.35
        ),
        prefer_layer_strength="strength" in print_priority.split("+"),
    )
    final = analyze_stl(
        orientation_export.output_path,
        material=material,
        overhang_angle_deg=overhang_angle_deg,
        nozzle_diameter_mm=nozzle_diameter_mm,
    )
    final, print_dna_application = _configure_analysis_for_print(
        final,
        print_priority=print_priority,
        model_purpose=model_purpose,
        functional_intent=functional_intent,
        print_setting_overrides=print_setting_overrides,
        print_dna_profile=print_dna_profile,
        material_profile=material_profile,
        source_profile_assessment=source_profile_assessment,
        nozzle_diameter_mm=nozzle_diameter_mm,
    )
    if not final.fits_build_volume:
        raise PipelineError("oriented model does not fit the configured build volume")

    object_optimizations: tuple[ObjectOptimizationResult, ...] = ()
    object_configurations: tuple[ObjectPrintConfiguration, ...] = ()
    rebuilt_multi_geometry = False
    is_multi_object = bool(
        extracted_geometry is not None
        and extracted_geometry.printable_object_count > 1
        and extracted_geometry.objects
    )
    if is_multi_object:
        emit_progress(
            progress_callback,
            "analyze-objects",
            f"Раздельный анализ {len(extracted_geometry.objects)} моделей",
            52,
        )
        per_instance: list[ObjectOptimizationResult] = []
        unique_configurations: dict[str, ObjectPrintConfiguration] = {}
        oriented_objects: dict[int, Path] = {}
        repaired_objects_dir = artifacts_dir / "model.objects.repaired"
        oriented_objects_dir = artifacts_dir / "model.objects.oriented"
        for item in extracted_geometry.objects:
            check_cancelled(cancel_event)
            object_path = item.output_stl_path
            object_report = analyze_stl(
                object_path,
                material=material,
                overhang_angle_deg=overhang_angle_deg,
                nozzle_diameter_mm=nozzle_diameter_mm,
            )
            if not _safe_for_slicer_recovery(object_report):
                raise PipelineError(
                    f"model {item.name!r} requires manual mesh repair before "
                    "object-specific slicing"
                )
            if object_report.health.status != "READY":
                repaired_objects_dir.mkdir(exist_ok=True)
                object_path = repaired_objects_dir / f"object-{item.instance_index}.stl"
                repair_stl(
                    item.output_stl_path,
                    object_path,
                    max_hole_diameter_mm=max_hole_diameter_mm,
                )
                object_report = analyze_stl(
                    object_path,
                    material=material,
                    overhang_angle_deg=overhang_angle_deg,
                    nozzle_diameter_mm=nozzle_diameter_mm,
                )
                if not _safe_for_slicer_recovery(object_report):
                    raise PipelineError(
                        f"automatic repair of model {item.name!r} could not produce "
                        "safe printable geometry"
                    )
            oriented_objects_dir.mkdir(exist_ok=True)
            object_orientation, object_orientation_export = orient_stl(
                object_path,
                oriented_objects_dir / f"object-{item.instance_index}.stl",
                overhang_angle_deg=overhang_angle_deg,
                protect_visible_surfaces=(
                    select_model_purpose(object_report.purpose, model_purpose)
                    == "decorative"
                    and object_report.purpose.curved_surface_ratio >= 0.35
                ),
                prefer_layer_strength="strength" in print_priority.split("+"),
            )
            object_path = object_orientation_export.output_path
            oriented_objects[item.instance_index] = object_path
            object_report = analyze_stl(
                object_path,
                material=material,
                overhang_angle_deg=overhang_angle_deg,
                nozzle_diameter_mm=nozzle_diameter_mm,
            )
            object_report, _ = _configure_analysis_for_print(
                object_report,
                print_priority=print_priority,
                model_purpose=model_purpose,
                functional_intent=functional_intent,
                print_setting_overrides=print_setting_overrides,
                print_dna_profile=print_dna_profile,
                material_profile=material_profile,
                source_profile_assessment=source_profile_assessment,
                nozzle_diameter_mm=nozzle_diameter_mm,
            )
            if not object_report.fits_build_volume:
                raise PipelineError(
                    f"model {item.name!r} does not fit the configured build volume"
                )
            support_mode = _object_support_mode(object_report, support_strategy)
            result_item = ObjectOptimizationResult(
                instance_index=item.instance_index,
                object_id=item.object_id,
                name=item.name,
                analysis=object_report,
                support_mode=support_mode,
                orientation=object_orientation,
                orientation_export=object_orientation_export,
            )
            per_instance.append(result_item)
            configuration = ObjectPrintConfiguration(
                object_id=item.object_id,
                name=item.name,
                settings=object_report.settings,
                support_mode=support_mode,
                local_modifier_plan=object_report.local_modifier_plan,
            )
            previous = unique_configurations.get(item.object_id)
            if previous is not None and previous != configuration:
                raise PipelineError(
                    f"repeated 3MF object {item.name!r} produced conflicting settings"
                )
            unique_configurations[item.object_id] = configuration
        if oriented_objects:
            try:
                extracted_geometry = rebuild_extracted_geometry(
                    extracted_geometry,
                    oriented_objects,
                    artifacts_dir / "model.objects.oriented.stl",
                )
            except Project3MFError as exc:
                raise PipelineError(
                    f"cannot rebuild oriented multi-model project: {exc}"
                ) from exc
            rebuilt_multi_geometry = True
        object_optimizations = tuple(per_instance)
        object_configurations = tuple(unique_configurations.values())

    optimization_plan = build_optimization_plan(
        final,
        allow_setting_changes=quality_search,
        nozzle_diameter_mm=nozzle_diameter_mm,
        locked_fields=frozenset(print_setting_overrides or ()),
        material_profile=material_profile,
    )

    slice_result: SliceRunResult | None = None
    mode = (
        "object-specific-project-optimization"
        if is_multi_object
        else "print-strategy-comparison"
    )
    emit_progress(progress_callback, "slice", "Подготовка вариантов печати", 55)
    check_cancelled(cancel_event)
    if is_multi_object:
        support_comparison = optimize_multi_object_project(
            source_path,
            template_path,
            extracted_geometry,
            output_dir / "print-strategies",
            object_configurations=object_configurations,
            executable=executable,
            plate=plate,
            timeout_s=timeout_s,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            use_extracted_geometry=rebuilt_multi_geometry,
        )
    else:
        support_comparison = compare_stl_supports(
            orientation_export.output_path,
            template_path,
            output_dir / "print-strategies",
            executable=executable,
            timeout_s=timeout_s,
            support_requirement=final.risks.support_requirement,
            recommended_settings=final.settings,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            preferred_strategy=support_strategy,
            optimization_plan=optimization_plan,
            local_modifier_plan=final.local_modifier_plan,
            support_exit_plan=final.support_exit_plan,
            slice_cache_dir=slice_cache_dir,
            source_time_budget_ratio=(
                _source_time_budget_ratio(final.settings.priority)
                if source_profile_assessment is not None
                else None
            ),
            source_baseline_settings=(
                _source_profile_baseline_settings(
                    final.settings,
                    source_profile_assessment,
                )
                if source_profile_assessment is not None
                else None
            ),
        )
    if support_comparison.recommended is None:
        raise PipelineError("all print strategies failed; partial artifacts were retained")
    if (
        support_comparison.ready_project_path is None
        or support_comparison.ready_gcode_path is None
    ):
        raise PipelineError("recommended strategy did not publish ready print files")

    # Keep the report and decision ledger aligned with the settings that were
    # actually selected by the measured slicer search.  Previously the report
    # could describe the pre-search baseline while a faster candidate (or the
    # source-time safety fallback) was the file delivered to the user.
    effective_settings = final.settings
    if support_comparison.source_time_guard_applied and source_profile_assessment is not None:
        effective_settings = _source_profile_baseline_settings(
            final.settings,
            source_profile_assessment,
        )
    elif support_comparison.quality_optimization is not None:
        selected_identifier = support_comparison.quality_optimization.selected_candidate
        selected_candidate = next(
            (
                candidate
                for candidate in optimization_plan.candidates
                if candidate.identifier == selected_identifier
            ),
            None,
        )
        if selected_candidate is not None:
            effective_settings = selected_candidate.settings
    effective_settings = replace(
        effective_settings,
        supports=support_comparison.recommended != "none",
    )
    final = replace(final, settings=effective_settings)

    emit_progress(progress_callback, "publish", "Проверка и сохранение результата", 94)
    check_cancelled(cancel_event)
    ready_project = output_dir / "ready-to-print.3mf"
    ready_gcode = output_dir / "ready-to-print.gcode"
    atomic_write_new_bytes(
        ready_project, support_comparison.ready_project_path.read_bytes()
    )
    atomic_write_new_bytes(ready_gcode, support_comparison.ready_gcode_path.read_bytes())
    if (
        _sha256(ready_project) != _sha256(support_comparison.ready_project_path)
        or _sha256(ready_gcode) != _sha256(support_comparison.ready_gcode_path)
    ):
        raise PipelineError("top-level ready print files failed SHA-256 verification")
    support_comparison = replace(
        support_comparison,
        ready_project_path=ready_project,
        ready_gcode_path=ready_gcode,
    )
    _write_decision_ledger(
        output_dir,
        final,
        support_comparison,
        object_optimizations,
    )

    if _sha256(source_path) != source_sha256 or _sha256(template_path) != template_sha256:
        raise PipelineError("source model or profile template changed during pipeline execution")
    check_cancelled(cancel_event)
    manifest_path = _write_manifest(
        output_dir,
        source_path=source_path,
        source_sha256=source_sha256,
        template_path=template_path,
        template_sha256=template_sha256,
        mode=mode,
        generated_profile=generated_profile,
        profile_validation=profile_validation,
        source_profile_assessment=source_profile_assessment,
        extracted_geometry=extracted_geometry,
        initial_analysis=initial,
        repair=repair_result,
        simplification=simplification_result,
        simplification_note=simplification_note,
        orientation=orientation,
        orientation_export=orientation_export,
        final_analysis=final,
        print_dna_application=print_dna_application,
        slice_result=slice_result,
        support_comparison=support_comparison,
        object_optimizations=object_optimizations,
    )
    emit_progress(progress_callback, "complete", "Готово к печати", 100)
    return PipelineResult(
        source_path=source_path,
        profile_template_path=template_path,
        output_dir=output_dir,
        mode=mode,
        generated_profile=generated_profile,
        profile_validation=profile_validation,
        source_profile_assessment=source_profile_assessment,
        extracted_geometry=extracted_geometry,
        initial_analysis=initial,
        repair=repair_result,
        simplification=simplification_result,
        simplification_note=simplification_note,
        orientation=orientation,
        orientation_export=orientation_export,
        final_analysis=final,
        print_dna_application=print_dna_application,
        slice_result=slice_result,
        support_comparison=support_comparison,
        object_optimizations=object_optimizations,
        manifest_path=manifest_path,
    )
