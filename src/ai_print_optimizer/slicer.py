"""Safe Bambu Studio CLI integration for isolated 3MF slicing."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from threading import Event
from typing import Any

from .gcode_audit import GCodeAudit, audit_gcode
from .geometry_features import (
    LocalModifierPlan,
    SupportExitPlan,
    retune_local_modifier_plan,
)
from .input_safety import (
    UnsafeInputError,
    parse_xml_bytes,
    read_json_file,
    read_zip_json,
    read_zip_member,
    safe_output_child,
    validate_gcode_file,
    validate_stl_file,
    validate_zip_archive,
)
from .io_utils import atomic_write_new_bytes
from .optimization_protocol import (
    CandidateSliceMetrics,
    OptimizationPlan,
    QualityOptimizationResult,
    evaluate_optimization_plan,
)
from .progress import ProgressCallback, check_cancelled, emit_progress
from .project3mf import (
    ExtractedProjectGeometry,
    ObjectPrintConfiguration,
    PreparedMultiObjectProject,
    PreparedProject,
    Project3MFError,
    ReadyProjectVerification,
    create_bambu_project_from_3mf_objects,
    create_bambu_project_from_stl,
    create_bambu_project_from_stl_objects,
    verify_ready_project,
)
from .reliability import VerifiedSliceCache
from .report import PrintSettings


class SlicerError(RuntimeError):
    """Raised when the slicer cannot be discovered or its result is unsafe to use."""


@dataclass(frozen=True)
class FilamentSliceMetrics:
    slot: int
    filament_id: str
    total_used_g: float
    model_and_support_used_g: float
    overhead_used_g: float
    estimated_length_m: float | None
    estimated_cost: float | None


@dataclass(frozen=True)
class PlateSliceMetrics:
    plate_id: int
    print_time_s: float
    core_print_time_s: float
    slicing_time_ms: int
    support_generation_time_ms: int
    support_feature_time_s: float
    total_used_g: float
    model_and_support_used_g: float
    overhead_used_g: float
    estimated_length_m: float | None
    estimated_cost: float | None
    triangle_count: int
    warnings: tuple[str, ...]
    filaments: tuple[FilamentSliceMetrics, ...]


@dataclass(frozen=True)
class GcodeFeatureMetrics:
    feature: str
    deposited_length_m: float
    estimated_mass_g: float | None


@dataclass(frozen=True)
class GcodeRoleMetrics:
    gcode_path: Path
    deposited_length_m: float
    estimated_mass_g: float | None
    support_length_m: float
    support_mass_g: float | None
    features: tuple[GcodeFeatureMetrics, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["gcode_path"] = str(self.gcode_path)
        return result


@dataclass(frozen=True)
class SliceRunResult:
    source_path: Path
    output_dir: Path
    slicer_path: Path
    return_code: int
    error_string: str
    success: bool
    layer_height_mm: float
    wall_loops: int
    sparse_infill_percent: float
    total_print_time_s: float
    total_used_g: float
    total_estimated_length_m: float | None
    total_estimated_cost: float | None
    plates: tuple[PlateSliceMetrics, ...]
    gcode_files: tuple[Path, ...]
    gcode_roles: tuple[GcodeRoleMetrics, ...]
    support_estimated_length_m: float | None
    support_estimated_mass_g: float | None
    metric_limitations: tuple[str, ...]
    ready_project_path: Path | None = None
    ready_project_sha256: str | None = None
    ready_project_verification: ReadyProjectVerification | None = None
    gcode_audits: tuple[GCodeAudit, ...] = ()
    cache_hit: bool = False

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_path"] = str(self.source_path)
        result["output_dir"] = str(self.output_dir)
        result["slicer_path"] = str(self.slicer_path)
        result["gcode_files"] = [str(path) for path in self.gcode_files]
        result["gcode_roles"] = [item.to_dict() for item in self.gcode_roles]
        result["gcode_audits"] = [item.to_dict() for item in self.gcode_audits]
        result["ready_project_path"] = (
            str(self.ready_project_path) if self.ready_project_path else None
        )
        result["ready_project_verification"] = (
            self.ready_project_verification.to_dict()
            if self.ready_project_verification
            else None
        )
        return result


@dataclass(frozen=True)
class SupportComparison:
    source_path: Path
    output_dir: Path
    source_sha256: str
    profile_template_path: Path | None
    profile_template_sha256: str | None
    none: SliceRunResult
    normal: SliceRunResult
    tree: SliceRunResult
    recommended: str | None
    recommendation_reason: str
    ready_project_path: Path | None
    ready_gcode_path: Path | None
    quality_optimization: QualityOptimizationResult | None = None
    selected_run: SliceRunResult | None = None
    source_profile_baseline: SliceRunResult | None = None
    source_time_budget_ratio: float | None = None
    source_time_guard_applied: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_path": str(self.source_path),
            "output_dir": str(self.output_dir),
            "source_sha256": self.source_sha256,
            "profile_template_path": (
                str(self.profile_template_path) if self.profile_template_path else None
            ),
            "profile_template_sha256": self.profile_template_sha256,
            "none": self.none.to_dict(),
            "normal": self.normal.to_dict(),
            "tree": self.tree.to_dict(),
            "recommended": self.recommended,
            "recommendation_reason": self.recommendation_reason,
            "ready_project_path": (
                str(self.ready_project_path) if self.ready_project_path else None
            ),
            "ready_gcode_path": (
                str(self.ready_gcode_path) if self.ready_gcode_path else None
            ),
            "quality_optimization": (
                self.quality_optimization.to_dict()
                if self.quality_optimization
                else None
            ),
            "selected_run": (
                self.selected_run.to_dict() if self.selected_run else None
            ),
            "source_profile_baseline": (
                self.source_profile_baseline.to_dict()
                if self.source_profile_baseline
                else None
            ),
            "source_time_budget_ratio": self.source_time_budget_ratio,
            "source_time_guard_applied": self.source_time_guard_applied,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _run_publishability_errors(run: SliceRunResult) -> tuple[str, ...]:
    """Return the reasons why one slicer run must not become a user result.

    Every pipeline branch, including object-specific projects and restored
    cache entries, must pass exactly the same release gate.  A missing audit is
    deliberately a failure: it is not evidence of a perfect score.
    """
    errors: list[str] = []
    if not run.success:
        errors.append(run.error_string or "slicer did not report success")
    if run.ready_project_path is None or not run.ready_project_path.is_file():
        errors.append("verified ready 3MF is missing")
    if len(run.gcode_files) != 1 or not run.gcode_files[0].is_file():
        errors.append("exactly one generated G-code is required")
    if len(run.gcode_audits) != len(run.gcode_files) or not run.gcode_audits:
        errors.append("independent G-code audit is missing or incomplete")
    for plate in run.plates:
        errors.extend(f"slicer warning: {warning}" for warning in plate.warnings)
    for audit in run.gcode_audits:
        if audit.status == "BLOCKED" or audit.safety_score < 85.0:
            errors.append(
                f"G-code audit failed ({audit.status}, {audit.safety_score:.0f}/100)"
            )
        errors.extend(f"G-code audit: {warning}" for warning in audit.blocking_warnings)
    return tuple(dict.fromkeys(errors))


def _run_is_publishable(run: SliceRunResult) -> bool:
    return not _run_publishability_errors(run)


def ensure_run_publishable(run: SliceRunResult) -> None:
    errors = _run_publishability_errors(run)
    if errors:
        raise SlicerError("result failed the release gate: " + "; ".join(errors))


def embedded_slicer_candidates() -> tuple[Path, ...]:
    """Return portable slicing-engine locations shipped with frozen builds."""
    candidates: list[Path] = []
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        candidates.append(Path(str(bundle_root)) / "slicer" / "bambu-studio.exe")
    if getattr(sys, "frozen", False):
        executable_root = Path(sys.executable).resolve().parent
        candidates.extend(
            (
                executable_root / "_internal" / "slicer" / "bambu-studio.exe",
                executable_root / "slicer" / "bambu-studio.exe",
            )
        )
    return tuple(dict.fromkeys(path.resolve() for path in candidates))


def discover_slicer_engine(explicit: str | Path | None = None) -> Path:
    """Find the embedded engine first, with an installed engine as a dev fallback."""
    candidates: list[Path] = []
    if explicit is not None:
        candidates.append(Path(explicit).expanduser())
    candidates.extend(embedded_slicer_candidates())
    configured = os.environ.get("BAMBU_STUDIO_PATH")
    if configured:
        candidates.append(Path(configured).expanduser())
    found = shutil.which("bambu-studio") or shutil.which("bambu-studio.exe")
    if found:
        candidates.append(Path(found))
    candidates.extend(
        [
            Path("/mnt/c/Program Files/Bambu Studio/bambu-studio.exe"),
            Path("C:/Program Files/Bambu Studio/bambu-studio.exe"),
        ]
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise SlicerError(
        "VANIOR PRINT slicing engine was not found or is incomplete"
    )


def discover_bambu_studio(explicit: str | Path | None = None) -> Path:
    """Backward-compatible alias for the autonomous slicing-engine resolver."""
    return discover_slicer_engine(explicit)


def _is_wsl() -> bool:
    return os.name != "nt" and bool(os.environ.get("WSL_DISTRO_NAME"))


def _path_for_windows_executable(path: Path) -> str:
    if not _is_wsl():
        return str(path)
    completed = subprocess.run(
        ["wslpath", "-w", str(path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise SlicerError(f"cannot convert WSL path for Bambu Studio: {path}")
    return completed.stdout.strip()


def _project_settings(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            validate_zip_archive(archive)
            payload = read_zip_json(archive, "Metadata/project_settings.config")
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, UnsafeInputError) as exc:
        raise SlicerError(f"3MF has no readable Bambu project settings: {path}") from exc
    if not isinstance(payload, dict):
        raise SlicerError("Bambu project settings must be a JSON object")
    return payload


def _support_mode_from_settings(settings: dict[str, Any]) -> str:
    if str(settings.get("enable_support", "0")) != "1":
        return "none"
    return "tree" if str(settings.get("support_type", "")).startswith("tree") else "normal"


def _float_list(settings: dict[str, Any], key: str) -> list[float]:
    raw = settings.get(key, [])
    if not isinstance(raw, list):
        raw = [raw]
    result: list[float] = []
    for value in raw:
        try:
            result.append(float(value))
        except (TypeError, ValueError):
            result.append(0.0)
    return result


def _at(values: list[float], index: int) -> float | None:
    if not values:
        return None
    return values[index] if index < len(values) else values[0]


_GCODE_NUMBER = re.compile(r"([A-Za-z])([-+]?(?:\d+(?:\.\d*)?|\.\d+))")
_MATERIAL_CHANGE = re.compile(r"^M620\s+S(\d+)A(?:\s|$)", re.IGNORECASE)


def analyze_gcode_roles(
    gcode: str | Path,
    *,
    filament_densities_g_cm3: list[float] | tuple[float, ...],
    filament_diameters_mm: list[float] | tuple[float, ...],
) -> GcodeRoleMetrics:
    """Estimate deposited material by Bambu `; FEATURE:` role markers.

    Only positive extrusion moves that also contain X or Y motion are counted. This
    excludes retractions, unretractions and stationary purge moves from deposited
    feature mass. Both relative (M83) and absolute (M82) extrusion are supported.
    """
    path = Path(gcode).expanduser().resolve()
    if path.suffix.lower() != ".gcode" or not path.is_file():
        raise SlicerError(f"G-code file not found: {path}")
    densities = [float(value) for value in filament_densities_g_cm3]
    diameters = [float(value) for value in filament_diameters_mm]
    feature_lengths: dict[str, float] = {}
    feature_masses: dict[str, float] = {}
    feature_mass_complete: dict[str, bool] = {}
    current_feature: str | None = None
    current_tool = 0
    relative_extrusion = False
    absolute_e = 0.0

    try:
        stream = path.open("r", encoding="utf-8", errors="replace")
    except OSError as exc:
        raise SlicerError(f"cannot read G-code: {path}") from exc
    with stream:
        for raw_line in stream:
            stripped = raw_line.strip()
            if stripped.startswith("; FEATURE:"):
                current_feature = stripped.partition(":")[2].strip() or "Unknown"
                continue
            command = stripped.partition(";")[0].strip()
            if not command:
                continue
            upper = command.upper()
            if upper == "M82":
                relative_extrusion = False
                continue
            if upper == "M83":
                relative_extrusion = True
                continue
            material_change = _MATERIAL_CHANGE.match(command)
            if material_change:
                current_tool = int(material_change.group(1))
                continue
            if upper.startswith("T") and upper[1:].isdigit():
                tool = int(upper[1:])
                if tool < max(len(densities), len(diameters)):
                    current_tool = tool
                continue
            values = {key.upper(): float(value) for key, value in _GCODE_NUMBER.findall(command)}
            if any(not math.isfinite(value) for value in values.values()):
                continue
            opcode = command.split(maxsplit=1)[0].upper()
            if opcode == "G92" and "E" in values:
                absolute_e = values["E"]
                continue
            if opcode not in {"G0", "G1", "G2", "G3"} or "E" not in values:
                continue
            raw_e = values["E"]
            if relative_extrusion:
                extrusion_delta = raw_e
            else:
                extrusion_delta = raw_e - absolute_e
                absolute_e = raw_e
            if (
                current_feature is None
                or extrusion_delta <= 0.0
                or not ({"X", "Y"} & values.keys())
            ):
                continue
            feature_lengths[current_feature] = (
                feature_lengths.get(current_feature, 0.0) + extrusion_delta
            )
            density = _at(densities, current_tool)
            diameter = _at(diameters, current_tool)
            complete = bool(density and diameter)
            feature_mass_complete[current_feature] = (
                feature_mass_complete.get(current_feature, True) and complete
            )
            if complete:
                section_mm2 = 3.141592653589793 * (diameter / 2.0) ** 2
                mass_g = extrusion_delta * section_mm2 / 1000.0 * density
                feature_masses[current_feature] = (
                    feature_masses.get(current_feature, 0.0) + mass_g
                )

    features = tuple(
        GcodeFeatureMetrics(
            feature=feature,
            deposited_length_m=length_mm / 1000.0,
            estimated_mass_g=(
                feature_masses.get(feature, 0.0)
                if feature_mass_complete.get(feature, False)
                else None
            ),
        )
        for feature, length_mm in sorted(feature_lengths.items())
    )
    if not features:
        raise SlicerError(f"G-code has no deposited FEATURE extrusion: {path}")
    support_features = [
        item for item in features if item.feature.casefold().startswith("support")
    ]
    all_masses = [item.estimated_mass_g for item in features]
    support_masses = [item.estimated_mass_g for item in support_features]
    return GcodeRoleMetrics(
        gcode_path=path,
        deposited_length_m=sum(item.deposited_length_m for item in features),
        estimated_mass_g=(
            sum(value for value in all_masses if value is not None)
            if all_masses and all(value is not None for value in all_masses)
            else None
        ),
        support_length_m=sum(item.deposited_length_m for item in support_features),
        support_mass_g=(
            sum(value for value in support_masses if value is not None)
            if support_features and all(value is not None for value in support_masses)
            else (0.0 if not support_features else None)
        ),
        features=features,
    )


def _parse_result(
    result_path: Path,
    project_path: Path,
    output_dir: Path,
    slicer_path: Path,
    *,
    source_path: Path | None = None,
) -> SliceRunResult:
    try:
        payload = read_json_file(result_path)
    except (OSError, json.JSONDecodeError, UnsafeInputError) as exc:
        raise SlicerError(f"cannot parse slicer result: {result_path}") from exc
    if not isinstance(payload, dict):
        raise SlicerError("slicer result must be a JSON object")

    def finite_number(value: Any, field: str, *, minimum: float = 0.0) -> float:
        try:
            result = float(value)
        except (TypeError, ValueError) as exc:
            raise SlicerError(f"slicer result has invalid {field}") from exc
        if not math.isfinite(result) or result < minimum:
            raise SlicerError(f"slicer result has unsafe {field}: {value!r}")
        return result

    settings = _project_settings(project_path)
    costs = _float_list(settings, "filament_cost")
    densities = _float_list(settings, "filament_density")
    diameters = _float_list(settings, "filament_diameter")
    plates: list[PlateSliceMetrics] = []

    raw_plates = payload.get("sliced_plates", [])
    if not isinstance(raw_plates, list) or len(raw_plates) > 256:
        raise SlicerError("slicer result has an invalid plate collection")
    for raw_plate in raw_plates:
        if not isinstance(raw_plate, dict):
            raise SlicerError("slicer result contains a non-object plate record")
        filament_metrics: list[FilamentSliceMetrics] = []
        raw_filaments = raw_plate.get("filaments", [])
        if not isinstance(raw_filaments, list) or len(raw_filaments) > 64:
            raise SlicerError("slicer result has an invalid filament collection")
        for raw_filament in raw_filaments:
            if not isinstance(raw_filament, dict):
                raise SlicerError("slicer result contains a non-object filament record")
            slot = max(1, int(raw_filament.get("id", 1)))
            index = slot - 1
            total_g = finite_number(raw_filament.get("total_used_g", 0.0), "total_used_g")
            main_g = finite_number(raw_filament.get("main_used_g", 0.0), "main_used_g")
            density = _at(densities, index)
            diameter = _at(diameters, index)
            length_m: float | None = None
            if density and diameter:
                section_mm2 = 3.141592653589793 * (diameter / 2.0) ** 2
                length_m = (total_g / density * 1000.0 / section_mm2) / 1000.0
            cost_per_kg = _at(costs, index)
            cost = total_g * cost_per_kg / 1000.0 if cost_per_kg is not None else None
            filament_metrics.append(
                FilamentSliceMetrics(
                    slot=slot,
                    filament_id=str(raw_filament.get("filament_id", "unknown")),
                    total_used_g=total_g,
                    model_and_support_used_g=main_g,
                    overhead_used_g=max(0.0, total_g - main_g),
                    estimated_length_m=length_m,
                    estimated_cost=cost,
                )
            )

        feature_times = raw_plate.get("feature_type_times", {})
        if not isinstance(feature_times, dict) or len(feature_times) > 512:
            raise SlicerError("slicer result has invalid feature timing data")
        support_time = sum(
            finite_number(value, f"feature time {key}")
            for key, value in feature_times.items()
            if "support" in key.lower()
        )
        warnings = tuple(
            item.strip()
            for item in str(raw_plate.get("warning_message", "")).splitlines()
            if item.strip()
        )
        length_values = [item.estimated_length_m for item in filament_metrics]
        cost_values = [item.estimated_cost for item in filament_metrics]
        plates.append(
            PlateSliceMetrics(
                plate_id=int(raw_plate.get("id", 0)),
                print_time_s=finite_number(
                    raw_plate.get("total_predication", 0.0), "total_predication"
                ),
                core_print_time_s=finite_number(
                    raw_plate.get("main_predication", 0.0), "main_predication"
                ),
                slicing_time_ms=int(raw_plate.get("sliced_time", 0)),
                support_generation_time_ms=int(
                    raw_plate.get("generate_support_material_time", 0)
                ),
                support_feature_time_s=support_time,
                total_used_g=sum(item.total_used_g for item in filament_metrics),
                model_and_support_used_g=sum(
                    item.model_and_support_used_g for item in filament_metrics
                ),
                overhead_used_g=sum(item.overhead_used_g for item in filament_metrics),
                estimated_length_m=(
                    sum(value for value in length_values if value is not None)
                    if length_values and all(value is not None for value in length_values)
                    else None
                ),
                estimated_cost=(
                    sum(value for value in cost_values if value is not None)
                    if cost_values and all(value is not None for value in cost_values)
                    else None
                ),
                triangle_count=int(raw_plate.get("triangle_count", 0)),
                warnings=warnings,
                filaments=tuple(filament_metrics),
            )
        )

    total_lengths = [plate.estimated_length_m for plate in plates]
    total_costs = [plate.estimated_cost for plate in plates]
    gcode_files = tuple(
        safe_output_child(output_dir, item)
        for item in sorted(output_dir.glob("plate_*.gcode"))
        if item.is_file()
    )
    gcode_roles: list[GcodeRoleMetrics] = []
    gcode_audits: list[GCodeAudit] = []
    gcode_parse_errors: list[str] = []
    for gcode_path in gcode_files:
        try:
            validate_gcode_file(gcode_path)
        except (SlicerError, ValueError) as exc:
            gcode_parse_errors.append(str(exc))
            continue
        try:
            gcode_roles.append(
                analyze_gcode_roles(
                    gcode_path,
                    filament_densities_g_cm3=densities,
                    filament_diameters_mm=diameters,
                )
            )
        except (SlicerError, ValueError) as exc:
            gcode_parse_errors.append(str(exc))
        try:
            max_flow_values = _float_list(settings, "filament_max_volumetric_speed")
            gcode_audits.append(
                audit_gcode(
                    gcode_path,
                    filament_diameters_mm=diameters or (1.75,),
                    maximum_volumetric_speed_mm3_s=(
                        min(max_flow_values) if max_flow_values else None
                    ),
                )
            )
        except (SlicerError, ValueError) as exc:
            gcode_parse_errors.append(f"independent audit unavailable: {exc}")
    support_lengths = [item.support_length_m for item in gcode_roles]
    support_masses = [item.support_mass_g for item in gcode_roles]
    return_code = int(payload.get("return_code", -1))
    return SliceRunResult(
        source_path=source_path or project_path,
        output_dir=output_dir,
        slicer_path=slicer_path,
        return_code=return_code,
        error_string=str(payload.get("error_string", "")),
        success=return_code == 0 and bool(plates),
        layer_height_mm=finite_number(payload.get("layer_height", 0.0), "layer_height"),
        wall_loops=int(payload.get("wall_loops", 0)),
        sparse_infill_percent=finite_number(
            payload.get("sparse_infill_density", 0.0), "sparse_infill_density"
        ),
        total_print_time_s=sum(plate.print_time_s for plate in plates),
        total_used_g=sum(plate.total_used_g for plate in plates),
        total_estimated_length_m=(
            sum(value for value in total_lengths if value is not None)
            if total_lengths and all(value is not None for value in total_lengths)
            else None
        ),
        total_estimated_cost=(
            sum(value for value in total_costs if value is not None)
            if total_costs and all(value is not None for value in total_costs)
            else None
        ),
        plates=tuple(plates),
        gcode_files=gcode_files,
        gcode_roles=tuple(gcode_roles),
        support_estimated_length_m=(
            sum(support_lengths) if gcode_roles and not gcode_parse_errors else None
        ),
        support_estimated_mass_g=(
            sum(value for value in support_masses if value is not None)
            if gcode_roles
            and not gcode_parse_errors
            and all(value is not None for value in support_masses)
            else None
        ),
        metric_limitations=(
            ("Support mass is estimated from positive XY extrusion under Bambu "
            "; FEATURE: Support markers; purge, retraction and stationary extrusion are excluded."),
            "Cost and length are derived from project filament price, density and diameter.",
            *tuple(f"G-code role analysis unavailable: {item}" for item in gcode_parse_errors),
        ),
        gcode_audits=tuple(gcode_audits),
    )


def _run_slicer_inputs(
    inputs: list[Path],
    settings_project: Path,
    source_path: Path,
    output_dir: Path,
    *,
    slicer_path: Path,
    plate: int = 1,
    timeout_s: float = 300.0,
    arrange: bool = False,
    export_3mf_name: str | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> SliceRunResult:
    data_dir = output_dir / ".bambu-data"
    data_dir.mkdir()

    windows_executable = slicer_path.suffix.lower() == ".exe"
    convert = _path_for_windows_executable if windows_executable else lambda path: str(path)
    command: list[str] = [
        str(slicer_path),
        "--debug=2",
        f"--datadir={convert(data_dir)}",
        f"--outputdir={convert(output_dir)}",
    ]
    if arrange:
        command.extend(["--arrange=1", "--ensure-on-bed"])
    command.append(f"--slice={plate}")
    if export_3mf_name is not None:
        if Path(export_3mf_name).name != export_3mf_name or not export_3mf_name.endswith(
            ".3mf"
        ):
            raise SlicerError("exported 3MF name must be a plain .3mf filename")
        command.append(f"--export-3mf={export_3mf_name}")
    command.extend(convert(path) for path in inputs)
    check_cancelled(cancel_event)
    try:
        process = subprocess.Popen(
            command,
            cwd=output_dir,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=(
                getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
            ),
        )
    except OSError as exc:
        raise SlicerError(f"cannot start Bambu Studio: {exc}") from exc

    result_path = output_dir / "result.json"
    export_path = output_dir / export_3mf_name if export_3mf_name else None
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
            check_cancelled(cancel_event)
        if result_path.is_file() and (
            export_path is None or export_path.is_file() or process.poll() is not None
        ):
            break
        return_code = process.poll()
        if return_code is not None and not result_path.is_file():
            raise SlicerError(
                "Bambu Studio exited before producing result.json "
                f"(code {return_code})"
            )
        time.sleep(0.1)
    if not result_path.is_file():
        if process.poll() is None:
            process.terminate()
        raise SlicerError(f"Bambu Studio did not produce result.json within {timeout_s:g}s")
    if process.poll() is None:
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # result.json is the slicer's final authoritative record. Do not kill a
            # process after it has committed that result; it may still flush logs.
            pass
    result = _parse_result(
        result_path,
        settings_project,
        output_dir,
        slicer_path,
        source_path=source_path,
    )
    if export_path is not None and result.success and not export_path.is_file():
        raise SlicerError("Bambu Studio reported success but did not export the ready 3MF")
    return result


def slice_3mf(
    project: str | Path,
    output: str | Path,
    *,
    executable: str | Path | None = None,
    plate: int = 1,
    timeout_s: float = 300.0,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
) -> SliceRunResult:
    """Slice one plate into a new isolated directory and parse result.json."""
    emit_progress(progress_callback, "validate", "Проверка проекта 3MF", 10)
    check_cancelled(cancel_event)
    project_path = Path(project).expanduser().resolve()
    output_dir = Path(output).expanduser().resolve()
    if project_path.suffix.lower() != ".3mf" or not project_path.is_file():
        raise SlicerError("slicer integration requires an existing Bambu 3MF project")
    if plate < 1:
        raise SlicerError("plate must be a positive one-based index")
    if timeout_s <= 0:
        raise SlicerError("slice timeout must be positive")
    if output_dir.exists():
        raise SlicerError(f"slice output already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise SlicerError(f"slice output parent not found: {output_dir.parent}")
    _project_settings(project_path)
    slicer_path = discover_bambu_studio(executable)
    output_dir.mkdir()
    emit_progress(progress_callback, "slice", "Нарезка проекта в Bambu Studio", 35)
    result = _run_slicer_inputs(
        [project_path],
        project_path,
        project_path,
        output_dir,
        slicer_path=slicer_path,
        plate=plate,
        timeout_s=timeout_s,
        export_3mf_name="ready-to-print.3mf",
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    if not result.success:
        return result
    ready_path = output_dir / "ready-to-print.3mf"
    verification = verify_ready_project(
        ready_path,
        expected_support_mode=_support_mode_from_settings(_project_settings(project_path)),
        expected_printable_count=None,
        external_gcode=result.gcode_files[0] if len(result.gcode_files) == 1 else None,
    )
    if not verification.valid:
        raise SlicerError(
            "exported 3MF verification failed: " + "; ".join(verification.errors)
        )
    emit_progress(progress_callback, "complete", "Проект и G-code готовы", 100)
    return replace(
        result,
        ready_project_path=ready_path,
        ready_project_sha256=_sha256(ready_path),
        ready_project_verification=verification,
    )


def optimize_multi_object_project(
    source: str | Path,
    template_3mf: str | Path,
    extracted_geometry: ExtractedProjectGeometry,
    output: str | Path,
    *,
    object_configurations: tuple[ObjectPrintConfiguration, ...],
    executable: str | Path | None = None,
    plate: int = 1,
    timeout_s: float = 300.0,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
    use_extracted_geometry: bool = False,
) -> SupportComparison:
    """Slice a multi-model 3MF or multi-body STL with settings per object."""
    source_path = Path(source).expanduser().resolve()
    template_path = Path(template_3mf).expanduser().resolve()
    output_dir = Path(output).expanduser().resolve()
    if source_path.suffix.lower() not in {".3mf", ".stl"} or not source_path.is_file():
        raise SlicerError("object-specific optimization requires an STL or 3MF")
    if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
        raise SlicerError("object-specific optimization requires a Bambu profile 3MF")
    if output_dir.exists():
        raise SlicerError(f"object-specific output already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise SlicerError(f"object-specific output parent not found: {output_dir.parent}")
    if len(object_configurations) < 2:
        raise SlicerError("object-specific optimization requires at least two objects")
    source_hash = _sha256(source_path)
    template_hash = _sha256(template_path)
    output_dir.mkdir()
    emit_progress(
        progress_callback,
        "prepare-objects",
        f"Назначение индивидуальных настроек: {len(object_configurations)} моделей",
        58,
    )
    check_cancelled(cancel_event)
    try:
        if source_path.suffix.lower() == ".3mf" and not use_extracted_geometry:
            prepared: PreparedMultiObjectProject = create_bambu_project_from_3mf_objects(
                source_path,
                template_path,
                output_dir / "prepared-project.3mf",
                object_configurations=object_configurations,
                plate=plate,
            )
        else:
            prepared = create_bambu_project_from_stl_objects(
                extracted_geometry,
                template_path,
                output_dir / "prepared-project.3mf",
                object_configurations=object_configurations,
            )
    except Project3MFError as exc:
        raise SlicerError(f"cannot prepare object-specific Bambu project: {exc}") from exc
    slicer_path = discover_bambu_studio(executable)
    emit_progress(
        progress_callback,
        "slice-objects",
        "Совместная нарезка моделей с индивидуальными параметрами",
        76,
    )
    run = _run_slicer_inputs(
        [prepared.project_path],
        prepared.project_path,
        source_path,
        output_dir,
        slicer_path=slicer_path,
        plate=1,
        timeout_s=timeout_s,
        arrange=use_extracted_geometry,
        export_3mf_name="ready-to-print.3mf",
        progress_callback=progress_callback,
        cancel_event=cancel_event,
    )
    if not run.success:
        return SupportComparison(
            source_path=source_path,
            output_dir=output_dir,
            source_sha256=source_hash,
            profile_template_path=template_path,
            profile_template_sha256=template_hash,
            none=run,
            normal=run,
            tree=run,
            recommended=None,
            recommendation_reason=(
                "Совместная нарезка индивидуально настроенных моделей завершилась ошибкой."
            ),
            ready_project_path=None,
            ready_gcode_path=None,
            selected_run=run,
        )
    if len(run.gcode_files) != 1:
        raise SlicerError(
            f"object-specific project produced {len(run.gcode_files)} G-code files; expected one"
        )
    used_model_filaments = _used_model_filament_slots(run)
    if len(used_model_filaments) > 1:
        raise SlicerError(
            "object-specific project unexpectedly used multiple filaments: "
            + ", ".join(str(slot) for slot in used_model_filaments)
        )
    ready_path = output_dir / "ready-to-print.3mf"
    verification = verify_ready_project(
        ready_path,
        expected_triangle_count=extracted_geometry.triangle_count,
        expected_geometry_path=extracted_geometry.output_stl_path,
        expected_object_configurations=object_configurations,
        expected_printable_count=extracted_geometry.printable_object_count,
        expected_single_color=True,
        external_gcode=run.gcode_files[0],
    )
    if not verification.valid:
        raise SlicerError(
            "object-specific ready project verification failed: "
            + "; ".join(verification.errors)
        )
    if any(
        item.triangle_count != extracted_geometry.triangle_count for item in run.plates
    ):
        raise SlicerError(
            "object-specific slicing changed the combined source triangle count"
        )
    verified_run = replace(
        run,
        ready_project_path=ready_path,
        ready_project_sha256=_sha256(ready_path),
        ready_project_verification=verification,
    )
    release_errors = _run_publishability_errors(verified_run)
    if release_errors:
        raise SlicerError(
            "object-specific result failed the release gate: "
            + "; ".join(release_errors)
        )
    support_modes = ", ".join(
        f"{item.name}: {item.support_mode}" for item in object_configurations
    )
    return SupportComparison(
        source_path=source_path,
        output_dir=output_dir,
        source_sha256=source_hash,
        profile_template_path=template_path,
        profile_template_sha256=template_hash,
        none=verified_run,
        normal=verified_run,
        tree=verified_run,
        recommended="object-specific",
        recommendation_reason=(
            "Каждая модель проанализирована отдельно; Bambu Studio получил нативные "
            f"объектные параметры. Поддержки: {support_modes}."
        ),
        ready_project_path=ready_path,
        ready_gcode_path=run.gcode_files[0],
        selected_run=verified_run,
    )


def optimize_multi_object_3mf(
    source: str | Path,
    template_3mf: str | Path,
    extracted_geometry: ExtractedProjectGeometry,
    output: str | Path,
    **kwargs: Any,
) -> SupportComparison:
    """Backward-compatible name for the object-specific project optimizer."""
    return optimize_multi_object_project(
        source,
        template_3mf,
        extracted_geometry,
        output,
        **kwargs,
    )


def create_profile_carrier(template: str | Path, output: str | Path) -> Path:
    """Copy a Bambu 3MF profile while making every template object non-printable."""
    template_path = Path(template).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
        raise SlicerError("profile template must be an existing Bambu 3MF project")
    if output_path.suffix.lower() != ".3mf":
        raise SlicerError("profile carrier output must use the .3mf extension")
    if output_path == template_path or output_path.exists():
        raise SlicerError(f"profile carrier output already exists or is the source: {output_path}")
    if not output_path.parent.is_dir():
        raise SlicerError(f"profile carrier output parent not found: {output_path.parent}")
    _project_settings(template_path)
    original_hash = _sha256(template_path)
    model_found = False
    ET.register_namespace("", "http://schemas.microsoft.com/3dmanufacturing/core/2015/02")
    ET.register_namespace("p", "http://schemas.microsoft.com/3dmanufacturing/production/2015/06")
    ET.register_namespace("BambuStudio", "http://schemas.bambulab.com/package/2021")
    try:
        with template_path.open("rb") as source_stream, zipfile.ZipFile(source_stream) as source_zip:
            validate_zip_archive(source_zip)
            with output_path.open("xb") as output_stream, zipfile.ZipFile(
                output_stream, "w"
            ) as output_zip:
                for entry in source_zip.infolist():
                    content = read_zip_member(source_zip, entry.filename)
                    if entry.filename == "3D/3dmodel.model":
                        root = parse_xml_bytes(content, context=entry.filename)
                        namespace = {
                            "m": "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
                        }
                        items = root.findall("./m:build/m:item", namespace)
                        if not items:
                            raise SlicerError("profile template has no build items")
                        for item in items:
                            item.set("printable", "0")
                        content = ET.tostring(
                            root,
                            encoding="utf-8",
                            xml_declaration=True,
                        )
                        model_found = True
                    output_zip.writestr(entry, content)
    except SlicerError:
        output_path.unlink(missing_ok=True)
        raise
    except (OSError, zipfile.BadZipFile, ET.ParseError, UnsafeInputError) as exc:
        output_path.unlink(missing_ok=True)
        raise SlicerError(f"cannot create profile carrier: {exc}") from exc
    if not model_found:
        output_path.unlink(missing_ok=True)
        raise SlicerError("profile template has no 3D/3dmodel.model")
    if _sha256(template_path) != original_hash:
        output_path.unlink(missing_ok=True)
        raise SlicerError("profile template changed while creating the carrier")
    return output_path


def slice_stl_with_template(
    stl: str | Path,
    template_3mf: str | Path,
    output: str | Path,
    *,
    executable: str | Path | None = None,
    timeout_s: float = 300.0,
) -> SliceRunResult:
    """Slice an STL with settings copied from a 3MF, without printing template objects."""
    stl_path = Path(stl).expanduser().resolve()
    template_path = Path(template_3mf).expanduser().resolve()
    output_dir = Path(output).expanduser().resolve()
    if stl_path.suffix.lower() != ".stl" or not stl_path.is_file():
        raise SlicerError("STL slicing requires an existing .stl file")
    if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
        raise SlicerError("profile template must be an existing Bambu 3MF project")
    if output_dir.exists():
        raise SlicerError(f"slice output already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise SlicerError(f"slice output parent not found: {output_dir.parent}")
    try:
        validate_stl_file(stl_path)
    except UnsafeInputError as exc:
        raise SlicerError(f"unsafe or malformed STL: {exc}") from exc
    stl_hash = _sha256(stl_path)
    template_hash = _sha256(template_path)
    slicer_path = discover_bambu_studio(executable)
    support_mode = _support_mode_from_settings(_project_settings(template_path))
    result = _run_stl_strategy(
        stl_path,
        template_path,
        output_dir,
        support_mode,
        slicer_path=slicer_path,
        timeout_s=timeout_s,
    )
    if _sha256(stl_path) != stl_hash or _sha256(template_path) != template_hash:
        raise SlicerError("source STL or profile template changed during slicing")
    return result


def create_support_variant(
    source: str | Path,
    output: str | Path,
    support_type: str,
) -> Path:
    """Create a new 3MF with one support setting changed; never overwrite files."""
    if support_type not in {"none", "normal(auto)", "tree(auto)"}:
        raise SlicerError("support type must be none, normal(auto) or tree(auto)")
    source_path = Path(source).expanduser().resolve()
    output_path = Path(output).expanduser().resolve()
    if source_path.suffix.lower() != ".3mf" or not source_path.is_file():
        raise SlicerError("support comparison requires an existing Bambu 3MF project")
    if output_path.suffix.lower() != ".3mf":
        raise SlicerError("support variant output must use the .3mf extension")
    if output_path == source_path or output_path.exists():
        raise SlicerError(f"support variant output already exists or is the source: {output_path}")
    if not output_path.parent.is_dir():
        raise SlicerError(f"support variant output parent not found: {output_path.parent}")

    original_hash = _sha256(source_path)
    settings_found = False
    try:
        with source_path.open("rb") as source_stream, zipfile.ZipFile(source_stream) as source_zip:
            validate_zip_archive(source_zip)
            with output_path.open("xb") as output_stream, zipfile.ZipFile(
                output_stream, "w"
            ) as output_zip:
                for entry in source_zip.infolist():
                    content = read_zip_member(source_zip, entry.filename)
                    if entry.filename == "Metadata/project_settings.config":
                        settings = read_zip_json(
                            source_zip, "Metadata/project_settings.config"
                        )
                        settings["enable_support"] = "0" if support_type == "none" else "1"
                        if support_type != "none":
                            settings["support_type"] = support_type
                        different = settings.get("different_settings_to_system", [])
                        if not isinstance(different, list):
                            different = [str(different)]
                        if not different:
                            different.append("")
                        changed_keys = {
                            item for item in str(different[0]).split(";") if item
                        }
                        changed_keys.add("enable_support")
                        if support_type != "none":
                            changed_keys.add("support_type")
                        else:
                            changed_keys.discard("support_type")
                        different[0] = ";".join(sorted(changed_keys))
                        settings["different_settings_to_system"] = different
                        content = json.dumps(
                            settings,
                            ensure_ascii=False,
                            indent=4,
                        ).encode("utf-8")
                        settings_found = True
                    elif entry.filename == "Metadata/model_settings.config":
                        try:
                            root = parse_xml_bytes(content, context=entry.filename)
                        except (ET.ParseError, UnsafeInputError) as exc:
                            raise SlicerError(
                                "cannot parse Metadata/model_settings.config"
                            ) from exc
                        for metadata in root.findall("./object/metadata"):
                            key = metadata.get("key")
                            if key == "enable_support":
                                metadata.set(
                                    "value", "0" if support_type == "none" else "1"
                                )
                            elif key == "support_type" and support_type != "none":
                                metadata.set("value", support_type)
                        content = ET.tostring(
                            root,
                            encoding="utf-8",
                            xml_declaration=True,
                        )
                    output_zip.writestr(entry, content)
    except SlicerError:
        output_path.unlink(missing_ok=True)
        raise
    except (OSError, zipfile.BadZipFile, json.JSONDecodeError, UnsafeInputError) as exc:
        output_path.unlink(missing_ok=True)
        raise SlicerError(f"cannot create support variant: {exc}") from exc
    if not settings_found:
        output_path.unlink(missing_ok=True)
        raise SlicerError("3MF has no Metadata/project_settings.config")
    if _sha256(source_path) != original_hash:
        output_path.unlink(missing_ok=True)
        raise SlicerError("source 3MF changed while creating a support variant")
    return output_path


def _used_model_filament_slots(result: SliceRunResult) -> tuple[int, ...]:
    return tuple(
        sorted(
            {
                filament.slot
                for plate in result.plates
                for filament in plate.filaments
                if filament.model_and_support_used_g > 0.001
            }
        )
    )


def _run_stl_strategy(
    stl_path: Path,
    template_path: Path,
    strategy_dir: Path,
    strategy: str,
    *,
    slicer_path: Path,
    timeout_s: float,
    recommended_settings: PrintSettings | None = None,
    local_modifier_plan: LocalModifierPlan | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
    slice_cache_dir: Path | None = None,
) -> SliceRunResult:
    check_cancelled(cancel_event)
    strategy_dir.mkdir()
    effective_local_plan = retune_local_modifier_plan(
        local_modifier_plan, recommended_settings
    )
    try:
        prepared: PreparedProject = create_bambu_project_from_stl(
            stl_path,
            template_path,
            strategy_dir / "prepared-project.3mf",
            support_mode=strategy,
            recommended_settings=recommended_settings,
            local_modifier_plan=effective_local_plan,
        )
    except Project3MFError as exc:
        raise SlicerError(f"cannot prepare {strategy} Bambu project: {exc}") from exc
    cache = VerifiedSliceCache(slice_cache_dir) if slice_cache_dir is not None else None
    cache_key = cache.key(prepared.project_path, slicer_path) if cache else None
    restored = bool(cache and cache_key and cache.restore(cache_key, strategy_dir))
    if restored:
        result = replace(
            _parse_result(
                strategy_dir / "result.json",
                prepared.project_path,
                strategy_dir,
                slicer_path,
                source_path=stl_path,
            ),
            cache_hit=True,
        )
    else:
        result = _run_slicer_inputs(
            [prepared.project_path],
            prepared.project_path,
            stl_path,
            strategy_dir,
            slicer_path=slicer_path,
            timeout_s=timeout_s,
            arrange=True,
            export_3mf_name="ready-to-print.3mf",
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )
    if not result.success:
        return result
    if len(result.gcode_files) != 1:
        raise SlicerError(
            f"strategy {strategy} produced {len(result.gcode_files)} G-code files; expected one"
        )
    used_model_filaments = _used_model_filament_slots(result)
    if len(used_model_filaments) > 1:
        raise SlicerError(
            f"strategy {strategy} unexpectedly used multiple filaments for a single-color STL: "
            + ", ".join(str(slot) for slot in used_model_filaments)
        )
    ready_path = strategy_dir / "ready-to-print.3mf"
    verification = verify_ready_project(
        ready_path,
        expected_object_name=prepared.object_name,
        expected_triangle_count=prepared.triangle_count,
        expected_geometry_path=prepared.source_stl_path,
        expected_support_mode=strategy,
        expected_print_settings=recommended_settings,
        expected_local_modifier_plan=effective_local_plan,
        expected_printable_count=1,
        expected_single_color=True,
        external_gcode=result.gcode_files[0],
    )
    if not verification.valid:
        raise SlicerError(
            f"strategy {strategy} exported an invalid ready project: "
            + "; ".join(verification.errors)
        )
    if any(
        plate.triangle_count != prepared.triangle_count for plate in result.plates
    ):
        raise SlicerError(
            f"strategy {strategy} sliced a different triangle count than the prepared STL"
        )
    verified_result = replace(
        result,
        ready_project_path=ready_path,
        ready_project_sha256=_sha256(ready_path),
        ready_project_verification=verification,
    )
    if cache and cache_key and not restored:
        cache.store(cache_key, strategy_dir)
    return verified_result


def _strategy_recommendation(
    none: SliceRunResult,
    normal: SliceRunResult,
    tree: SliceRunResult,
    *,
    support_requirement: str | None = None,
    support_exit_plan: SupportExitPlan | None = None,
) -> tuple[str | None, str]:
    successful = {
        name: run
        for name, run in (("none", none), ("normal", normal), ("tree", tree))
        if _run_is_publishable(run)
    }
    if not successful:
        return None, "Все три стратегии завершились ошибкой; рекомендация невозможна."
    requirement = (support_requirement or "UNKNOWN").upper()
    none_clean = _run_is_publishable(none)
    if none_clean and requirement == "LOW":
        return (
            "none",
            ("Слайсинг без поддержек завершился без предупреждений, а геометрический "
            f"риск поддержек имеет уровень {requirement}; лишние supports не добавлены."),
        )
    if support_exit_plan is not None and support_exit_plan.overall_risk == "HIGH":
        tree_run = successful.get("tree")
        if tree_run is not None:
            return (
                "tree",
                ("Поддержки действительно требуются, а план извлечения обнаружил высокий "
                "риск запирания; выбран древовидный вариант с доступным выходом."),
            )
    supported = {
        name: run for name, run in successful.items() if name in {"normal", "tree"}
    }
    if not supported:
        return (
            "none" if _run_is_publishable(none) else None,
            "Поддерживаемые варианты не прошли слайсинг; выбран успешный вариант без supports."
            if _run_is_publishable(none)
            else "Ни одна пригодная стратегия не прошла слайсинг.",
        )
    if len(supported) == 1:
        return (
            next(iter(supported)),
            "Выбран единственный вариант с поддержками, который успешно прошёл слайсинг.",
        )
    minimum_time = min(run.total_print_time_s for run in supported.values()) or 1.0
    minimum_mass = min(run.total_used_g for run in supported.values()) or 1.0
    balanced_scores = {
        name: 0.6 * run.total_print_time_s / minimum_time
        + 0.4 * run.total_used_g / minimum_mass
        for name, run in supported.items()
    }
    recommended = min(balanced_scores, key=balanced_scores.get)  # type: ignore[arg-type]
    winner = supported[recommended]
    other_name = "tree" if recommended == "normal" else "normal"
    other = supported[other_name]
    time_delta = winner.total_print_time_s - other.total_print_time_s
    mass_delta = winner.total_used_g - other.total_used_g
    return recommended, (
        "Сбалансированный выбор (60% время, 40% материал): "
        f"разница {time_delta:+.1f} с и {mass_delta:+.2f} г относительно альтернативы."
    )


def _publish_recommended(
    output_dir: Path,
    recommended: str | None,
    runs: dict[str, SliceRunResult],
) -> tuple[Path | None, Path | None]:
    if recommended is None:
        return None, None
    winner = runs[recommended]
    if winner.ready_project_path is None or len(winner.gcode_files) != 1:
        raise SlicerError("recommended strategy has no verified ready 3MF and G-code")
    ready_project = output_dir / "ready-to-print.3mf"
    ready_gcode = output_dir / "ready-to-print.gcode"
    atomic_write_new_bytes(ready_project, winner.ready_project_path.read_bytes())
    atomic_write_new_bytes(ready_gcode, winner.gcode_files[0].read_bytes())
    if (
        _sha256(ready_project) != _sha256(winner.ready_project_path)
        or _sha256(ready_gcode) != _sha256(winner.gcode_files[0])
    ):
        raise SlicerError("published ready-to-print files failed SHA-256 verification")
    return ready_project, ready_gcode


def compare_supports(
    source: str | Path,
    output: str | Path,
    *,
    executable: str | Path | None = None,
    plate: int = 1,
    timeout_s: float = 300.0,
    support_requirement: str | None = None,
) -> SupportComparison:
    """Slice none, normal and tree variants and publish the recommended files."""
    source_path = Path(source).expanduser().resolve()
    output_dir = Path(output).expanduser().resolve()
    if source_path.suffix.lower() != ".3mf" or not source_path.is_file():
        raise SlicerError("support comparison requires an existing Bambu 3MF project")
    if output_dir.exists():
        raise SlicerError(f"comparison output already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise SlicerError(f"comparison output parent not found: {output_dir.parent}")
    source_hash = _sha256(source_path)
    output_dir.mkdir()
    none_project = create_support_variant(
        source_path, output_dir / "none.3mf", "none"
    )
    normal_project = create_support_variant(
        source_path, output_dir / "normal-auto.3mf", "normal(auto)"
    )
    tree_project = create_support_variant(
        source_path, output_dir / "tree-auto.3mf", "tree(auto)"
    )
    none = slice_3mf(
        none_project,
        output_dir / "none",
        executable=executable,
        plate=plate,
        timeout_s=timeout_s,
    )
    normal = slice_3mf(
        normal_project,
        output_dir / "normal",
        executable=executable,
        plate=plate,
        timeout_s=timeout_s,
    )
    tree = slice_3mf(
        tree_project,
        output_dir / "tree",
        executable=executable,
        plate=plate,
        timeout_s=timeout_s,
    )

    recommended, reason = _strategy_recommendation(
        none,
        normal,
        tree,
        support_requirement=support_requirement,
    )
    if _sha256(source_path) != source_hash:
        raise SlicerError("source 3MF changed during support comparison")
    ready_project, ready_gcode = _publish_recommended(
        output_dir,
        recommended,
        {"none": none, "normal": normal, "tree": tree},
    )
    return SupportComparison(
        source_path=source_path,
        output_dir=output_dir,
        source_sha256=source_hash,
        profile_template_path=None,
        profile_template_sha256=None,
        none=none,
        normal=normal,
        tree=tree,
        recommended=recommended,
        recommendation_reason=reason,
        ready_project_path=ready_project,
        ready_gcode_path=ready_gcode,
    )


def compare_stl_supports(
    stl: str | Path,
    template_3mf: str | Path,
    output: str | Path,
    *,
    executable: str | Path | None = None,
    timeout_s: float = 300.0,
    support_requirement: str | None = None,
    recommended_settings: PrintSettings | None = None,
    progress_callback: ProgressCallback | None = None,
    cancel_event: Event | None = None,
    preferred_strategy: str = "auto",
    optimization_plan: OptimizationPlan | None = None,
    local_modifier_plan: LocalModifierPlan | None = None,
    support_exit_plan: SupportExitPlan | None = None,
    slice_cache_dir: str | Path | None = None,
    source_time_budget_ratio: float | None = None,
    source_baseline_settings: PrintSettings | None = None,
) -> SupportComparison:
    """Compare none, normal and tree for an STL and export ready-to-print files."""
    stl_path = Path(stl).expanduser().resolve()
    template_path = Path(template_3mf).expanduser().resolve()
    output_dir = Path(output).expanduser().resolve()
    if stl_path.suffix.lower() != ".stl" or not stl_path.is_file():
        raise SlicerError("STL support comparison requires an existing .stl file")
    if template_path.suffix.lower() != ".3mf" or not template_path.is_file():
        raise SlicerError("profile template must be an existing Bambu 3MF project")
    if timeout_s <= 0:
        raise SlicerError("slice timeout must be positive")
    if preferred_strategy not in {"auto", "none", "normal", "tree"}:
        raise SlicerError("preferred strategy must be auto, none, normal or tree")
    if source_time_budget_ratio is not None and not 1.0 <= source_time_budget_ratio <= 2.0:
        raise SlicerError("source time budget ratio must be between 1.0 and 2.0")
    if output_dir.exists():
        raise SlicerError(f"comparison output already exists: {output_dir}")
    if not output_dir.parent.is_dir():
        raise SlicerError(f"comparison output parent not found: {output_dir.parent}")
    _project_settings(template_path)
    stl_hash = _sha256(stl_path)
    template_hash = _sha256(template_path)
    slicer_path = discover_bambu_studio(executable)
    output_dir.mkdir()
    emit_progress(progress_callback, "slice-none", "Слайсинг без поддержек", 60)
    none = _run_stl_strategy(
        stl_path,
        template_path,
        output_dir / "none",
        "none",
        slicer_path=slicer_path,
        timeout_s=timeout_s,
        recommended_settings=recommended_settings,
        local_modifier_plan=local_modifier_plan,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        slice_cache_dir=(Path(slice_cache_dir).expanduser().resolve() if slice_cache_dir else None),
    )
    emit_progress(progress_callback, "slice-normal", "Слайсинг с обычными поддержками", 72)
    normal = _run_stl_strategy(
        stl_path,
        template_path,
        output_dir / "normal",
        "normal",
        slicer_path=slicer_path,
        timeout_s=timeout_s,
        recommended_settings=recommended_settings,
        local_modifier_plan=local_modifier_plan,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        slice_cache_dir=(Path(slice_cache_dir).expanduser().resolve() if slice_cache_dir else None),
    )
    emit_progress(progress_callback, "slice-tree", "Слайсинг с древовидными поддержками", 84)
    tree = _run_stl_strategy(
        stl_path,
        template_path,
        output_dir / "tree",
        "tree",
        slicer_path=slicer_path,
        timeout_s=timeout_s,
        recommended_settings=recommended_settings,
        local_modifier_plan=local_modifier_plan,
        progress_callback=progress_callback,
        cancel_event=cancel_event,
        slice_cache_dir=(Path(slice_cache_dir).expanduser().resolve() if slice_cache_dir else None),
    )
    emit_progress(progress_callback, "select", "Выбор лучшего варианта", 91)
    check_cancelled(cancel_event)
    if _sha256(stl_path) != stl_hash or _sha256(template_path) != template_hash:
        raise SlicerError("source STL or profile template changed during support comparison")
    recommended, reason = _strategy_recommendation(
        none,
        normal,
        tree,
        support_requirement=support_requirement,
        support_exit_plan=support_exit_plan,
    )
    if preferred_strategy != "auto":
        requested = {"none": none, "normal": normal, "tree": tree}[preferred_strategy]
        if not _run_is_publishable(requested):
            raise SlicerError(
                f"requested support strategy {preferred_strategy} failed the release gate: "
                + "; ".join(_run_publishability_errors(requested))
            )
        recommended = preferred_strategy
        reason = (
            "Выбранная пользователем стратегия поддержек успешно прошла нарезку "
            "и проверку готового проекта."
        )
    if recommended is None:
        return SupportComparison(
            source_path=stl_path,
            output_dir=output_dir,
            source_sha256=stl_hash,
            profile_template_path=template_path,
            profile_template_sha256=template_hash,
            none=none,
            normal=normal,
            tree=tree,
            recommended=None,
            recommendation_reason=reason,
            ready_project_path=None,
            ready_gcode_path=None,
        )
    support_runs = {"none": none, "normal": normal, "tree": tree}
    selected_run = support_runs[recommended]
    quality_optimization: QualityOptimizationResult | None = None
    if optimization_plan is not None:
        baseline_identifier = optimization_plan.candidates[0].identifier
        candidate_runs: dict[str, SliceRunResult] = {baseline_identifier: selected_run}
        metrics: dict[str, CandidateSliceMetrics] = {
            baseline_identifier: CandidateSliceMetrics(
                selected_run.success,
                selected_run.total_print_time_s,
                selected_run.total_used_g,
                tuple(warning for plate in selected_run.plates for warning in plate.warnings),
                selected_run.error_string,
                min((item.safety_score for item in selected_run.gcode_audits), default=0.0),
                tuple(
                    warning
                    for audit in selected_run.gcode_audits
                    for warning in audit.blocking_warnings
                ),
            )
        }
        runnable = [
            candidate
            for candidate in optimization_plan.candidates[1:]
            if candidate.eligible
        ]
        if runnable:
            candidate_root = output_dir / "parameter-candidates"
            candidate_root.mkdir()
        for index, candidate in enumerate(runnable, start=1):
            emit_progress(
                progress_callback,
                "quality-search",
                f"Проверка скорости: {candidate.title}",
                min(97, 91 + round(index * 6 / max(1, len(runnable)))),
            )
            run = _run_stl_strategy(
                stl_path,
                template_path,
                candidate_root / candidate.identifier,
                recommended,
                slicer_path=slicer_path,
                timeout_s=timeout_s,
                recommended_settings=candidate.settings,
                local_modifier_plan=local_modifier_plan,
                progress_callback=progress_callback,
                cancel_event=cancel_event,
                slice_cache_dir=(Path(slice_cache_dir).expanduser().resolve() if slice_cache_dir else None),
            )
            candidate_runs[candidate.identifier] = run
            metrics[candidate.identifier] = CandidateSliceMetrics(
                run.success,
                run.total_print_time_s if run.success else None,
                run.total_used_g if run.success else None,
                tuple(warning for plate in run.plates for warning in plate.warnings),
                run.error_string,
                min((item.safety_score for item in run.gcode_audits), default=0.0),
                tuple(
                    warning
                    for audit in run.gcode_audits
                    for warning in audit.blocking_warnings
                ),
            )
        quality_optimization = evaluate_optimization_plan(optimization_plan, metrics)
        selected_run = candidate_runs[quality_optimization.selected_candidate]
        reason = reason + " " + quality_optimization.reason
    source_profile_baseline: SliceRunResult | None = None
    source_time_guard_applied = False
    if source_time_budget_ratio is not None:
        emit_progress(
            progress_callback,
            "source-time-budget",
            "Сравнение времени с исходным 3MF",
            98,
        )
        source_profile_baseline = _run_stl_strategy(
            stl_path,
            template_path,
            output_dir / "source-profile-baseline",
            recommended,
            slicer_path=slicer_path,
            timeout_s=timeout_s,
            recommended_settings=source_baseline_settings,
            local_modifier_plan=None,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
            slice_cache_dir=(Path(slice_cache_dir).expanduser().resolve() if slice_cache_dir else None),
        )
        tuple(
            warning for plate in source_profile_baseline.plates for warning in plate.warnings
        )
        tuple(
            warning
            for audit in source_profile_baseline.gcode_audits
            for warning in audit.blocking_warnings
        )
        min(
            (item.safety_score for item in source_profile_baseline.gcode_audits),
            default=0.0,
        )
        source_is_usable = (
            _run_is_publishable(source_profile_baseline)
            and source_profile_baseline.total_print_time_s > 0
        )
        if (
            source_is_usable
            and selected_run.total_print_time_s
            > source_profile_baseline.total_print_time_s * source_time_budget_ratio
        ):
            selected_run = source_profile_baseline
            source_time_guard_applied = True
            quality_optimization = None
            reason = (
                reason
                + " Ограничитель исходного 3MF отклонил более медленный вариант: "
                + f"допустимо не более +{(source_time_budget_ratio - 1.0) * 100:.0f}% времени."
            )
    release_errors = _run_publishability_errors(selected_run)
    if release_errors:
        raise SlicerError("selected result failed the release gate: " + "; ".join(release_errors))
    ready_project, ready_gcode = _publish_recommended(
        output_dir,
        "selected",
        {"selected": selected_run},
    )
    return SupportComparison(
        source_path=stl_path,
        output_dir=output_dir,
        source_sha256=stl_hash,
        profile_template_path=template_path,
        profile_template_sha256=template_hash,
        none=none,
        normal=normal,
        tree=tree,
        recommended=recommended,
        recommendation_reason=reason,
        ready_project_path=ready_project,
        ready_gcode_path=ready_gcode,
        quality_optimization=quality_optimization,
        selected_run=selected_run,
        source_profile_baseline=source_profile_baseline,
        source_time_budget_ratio=source_time_budget_ratio,
        source_time_guard_applied=source_time_guard_applied,
    )
