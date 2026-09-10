"""Inspection and validation of Bambu 3MF printer/process/filament profiles."""

from __future__ import annotations

import json
import zipfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .input_safety import UnsafeInputError, read_zip_json, validate_zip_archive


class ProfileError(RuntimeError):
    """Raised when a Bambu profile cannot be inspected."""


@dataclass(frozen=True)
class ProfileValidation:
    profile_path: Path
    valid: bool
    printer_model: str
    printer_settings_id: str
    nozzle_diameter_mm: float | None
    filament_types: tuple[str, ...]
    bed_type: str
    printable_height_mm: float | None
    errors: tuple[str, ...]
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["profile_path"] = str(self.profile_path)
        return result


@dataclass(frozen=True)
class SourceProfileAssessment:
    """Quality-relevant settings recovered from an uploaded Bambu 3MF.

    Calibrated filament limits and the author's main process choices are kept as
    separate guardrails. A source 3MF is evidence from a real print, not a blank
    geometry container, so VANIOR PRINT must improve it without silently
    discarding the settings that determine its time budget.
    """

    setting_count: int
    quality_score: float
    strengths: tuple[str, ...]
    weaknesses: tuple[str, ...]
    guardrails: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _settings(path: Path) -> dict[str, Any]:
    try:
        with zipfile.ZipFile(path) as archive:
            validate_zip_archive(archive)
            payload = read_zip_json(archive, "Metadata/project_settings.config")
    except (OSError, KeyError, zipfile.BadZipFile, json.JSONDecodeError, UnsafeInputError) as exc:
        raise ProfileError(f"3MF has no readable Bambu project settings: {path}") from exc
    if not isinstance(payload, dict):
        raise ProfileError("Bambu project settings must be a JSON object")
    return payload


def _first_text(settings: dict[str, Any], key: str) -> str:
    value = settings.get(key, "")
    if isinstance(value, list):
        value = value[0] if value else ""
    return str(value).strip()


def _text_tuple(settings: dict[str, Any], key: str) -> tuple[str, ...]:
    value = settings.get(key, [])
    if not isinstance(value, list):
        value = [value]
    return tuple(str(item).strip().upper() for item in value if str(item).strip())


def _optional_float(settings: dict[str, Any], key: str) -> float | None:
    raw = _first_text(settings, key)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _optional_int(settings: dict[str, Any], key: str) -> int | None:
    value = _optional_float(settings, key)
    return round(value) if value is not None else None


def _optional_percent(settings: dict[str, Any], key: str) -> int | None:
    """Read a Bambu percentage field such as ``10%`` as a bounded integer."""
    raw = _first_text(settings, key).rstrip("% ")
    try:
        value = round(float(raw))
    except (TypeError, ValueError):
        return None
    return max(0, min(100, value))


def assess_bambu_profile(profile: str | Path) -> SourceProfileAssessment:
    """Inspect an uploaded 3MF and retain its proven material guardrails."""
    path = Path(profile).expanduser().resolve()
    settings = _settings(path)
    source_material = (_first_text(settings, "filament_type") or "PLA").upper()
    guardrails: dict[str, Any] = {}
    mapping = {
        "layer_height": ("layer_height_mm", _optional_float),
        "line_width": ("line_width_mm", _optional_float),
        "top_surface_line_width": ("top_surface_line_width_mm", _optional_float),
        "top_surface_pattern": ("top_surface_pattern", _first_text),
        "wall_loops": ("wall_loops", _optional_int),
        "top_shell_layers": ("top_layers", _optional_int),
        "bottom_shell_layers": ("bottom_layers", _optional_int),
        "sparse_infill_density": ("sparse_infill_percent", _optional_percent),
        "wall_generator": ("wall_generator", _first_text),
        "filament_flow_ratio": ("filament_flow_ratio", _optional_float),
        "filament_max_volumetric_speed": ("max_volumetric_speed_mm3_s", _optional_float),
        "nozzle_temperature": ("nozzle_temperature_c", _optional_int),
        "nozzle_temperature_initial_layer": ("initial_nozzle_temperature_c", _optional_int),
        "fan_max_speed": ("fan_percent", _optional_int),
        "enable_pressure_advance": ("enable_pressure_advance", _first_text),
        "pressure_advance": ("pressure_advance_k", _optional_float),
        "outer_wall_speed": ("outer_wall_speed_mm_s", _optional_int),
        "inner_wall_speed": ("inner_wall_speed_mm_s", _optional_int),
        "internal_solid_infill_speed": ("internal_solid_infill_speed_mm_s", _optional_int),
        "bridge_speed": ("bridge_speed_mm_s", _optional_int),
        "default_acceleration": ("default_acceleration_mm_s2", _optional_int),
        "outer_wall_acceleration": ("outer_wall_acceleration_mm_s2", _optional_int),
        "top_surface_speed": ("top_surface_speed_mm_s", _optional_int),
        "top_surface_acceleration": ("top_surface_acceleration_mm_s2", _optional_int),
        "support_top_z_distance": ("support_top_z_distance_mm", _optional_float),
        "support_bottom_z_distance": ("support_bottom_z_distance_mm", _optional_float),
        "support_object_xy_distance": ("support_object_xy_distance_mm", _optional_float),
        "support_interface_top_layers": ("support_interface_top_layers", _optional_int),
        "support_interface_bottom_layers": ("support_interface_bottom_layers", _optional_int),
        "support_interface_spacing": ("support_interface_spacing_mm", _optional_float),
        "support_interface_speed": ("support_interface_speed_mm_s", _optional_int),
        "support_type": ("support_type", _first_text),
        "travel_speed": ("travel_speed_mm_s", _optional_int),
        "seam_position": ("seam_position", _first_text),
        "reduce_crossing_wall": ("reduce_crossing_wall", _first_text),
        "retraction_length": ("retraction_length_mm", _optional_float),
        "retraction_speed": ("retraction_speed_mm_s", _optional_float),
        "wipe": ("wipe_enabled", _first_text),
        "wipe_distance": ("wipe_distance_mm", _optional_float),
    }
    for source_key, (target_key, reader) in mapping.items():
        value = reader(settings, source_key)
        if value not in {None, ""}:
            guardrails[target_key] = value

    bed_type = _first_text(settings, "curr_bed_type").casefold()
    bed_key = (
        "textured_plate_temp"
        if "textured" in bed_type
        else "cool_plate_temp"
        if "cool" in bed_type
        else "eng_plate_temp"
        if "engineering" in bed_type
        else "hot_plate_temp"
    )
    bed_temperature = _optional_int(settings, bed_key)
    if bed_temperature is not None:
        guardrails["bed_temperature_c"] = bed_temperature

    strengths: list[str] = []
    weaknesses: list[str] = []
    score = 70.0
    if "filament_flow_ratio" in guardrails:
        strengths.append("Сохранена калибровка коэффициента подачи пластика.")
        score += 5.0
    else:
        weaknesses.append("В 3MF отсутствует коэффициент подачи пластика.")
        score -= 5.0
    if "max_volumetric_speed_mm3_s" in guardrails:
        strengths.append("Сохранён проверенный предел объёмного потока пластика.")
        score += 7.0
    else:
        weaknesses.append("Не задан проверенный предел объёмного потока.")
        score -= 8.0
    if "retraction_length_mm" in guardrails and "retraction_speed_mm_s" in guardrails:
        strengths.append("В 3MF присутствует калибровка ретракта.")
        score += 5.0
    else:
        weaknesses.append("Настройки ретракта неполные; контроль нитей требует проверки.")
        score -= 5.0
    if "support_interface_top_layers" in guardrails:
        strengths.append("Настройки интерфейса поддержек доступны для улучшения.")
        score += 3.0
    if float(guardrails.get("outer_wall_speed_mm_s", 0) or 0) > 160:
        weaknesses.append("Скорость внешней стенки высока для декоративной модели.")
        score -= 6.0
    material_flow_limit = 12.0 if source_material == "PETG" else 18.0
    if float(guardrails.get("max_volumetric_speed_mm3_s", 0) or 0) > material_flow_limit:
        weaknesses.append(
            f"Поток выше консервативного диапазона для качественной {source_material}-печати."
        )
        score -= 5.0
    return SourceProfileAssessment(
        setting_count=len(settings),
        quality_score=round(max(0.0, min(100.0, score)), 2),
        strengths=tuple(strengths),
        weaknesses=tuple(weaknesses),
        guardrails=guardrails,
    )


def validate_bambu_profile(
    profile: str | Path,
    *,
    expected_printer: str = "Bambu Lab P1S",
    expected_nozzle_mm: float = 0.4,
    expected_material: str = "PLA",
) -> ProfileValidation:
    """Validate the active printer, nozzle and first filament in a Bambu 3MF."""
    path = Path(profile).expanduser().resolve()
    if path.suffix.lower() != ".3mf" or not path.is_file():
        raise ProfileError("profile must be an existing Bambu 3MF project")
    settings = _settings(path)
    printer_model = _first_text(settings, "printer_model")
    printer_settings_id = _first_text(settings, "printer_settings_id")
    nozzle = _optional_float(settings, "nozzle_diameter")
    filament_types = _text_tuple(settings, "filament_type")
    bed_type = _first_text(settings, "curr_bed_type")
    printable_height = _optional_float(settings, "printable_height")
    technology = _first_text(settings, "printer_technology").upper()
    errors: list[str] = []
    warnings: list[str] = []

    if printer_model != expected_printer:
        errors.append(
            f"printer mismatch: expected {expected_printer}, profile has {printer_model or 'unknown'}"
        )
    if nozzle is None or abs(nozzle - expected_nozzle_mm) > 1e-6:
        errors.append(
            f"nozzle mismatch: expected {expected_nozzle_mm:g} mm, "
            f"profile has {nozzle if nozzle is not None else 'unknown'}"
        )
    expected_material = expected_material.strip().upper()
    if not filament_types:
        errors.append("profile has no filament_type")
    elif filament_types[0] != expected_material:
        errors.append(
            f"material mismatch: expected {expected_material}, active slot has {filament_types[0]}"
        )
    if technology and technology != "FFF":
        errors.append(f"unsupported printer technology: {technology}")
    if not bed_type:
        warnings.append("profile does not declare curr_bed_type")
    if printable_height is None:
        warnings.append("profile does not declare printable_height")
    if len(set(filament_types)) > 1:
        warnings.append(
            "profile contains multiple filament types; only the first active slot is "
            "validated against the requested material"
        )
    if not printer_settings_id:
        warnings.append("profile does not declare printer_settings_id")
    return ProfileValidation(
        profile_path=path,
        valid=not errors,
        printer_model=printer_model,
        printer_settings_id=printer_settings_id,
        nozzle_diameter_mm=nozzle,
        filament_types=filament_types,
        bed_type=bed_type,
        printable_height_mm=printable_height,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
