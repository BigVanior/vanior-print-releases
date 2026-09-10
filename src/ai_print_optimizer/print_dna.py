"""Local learning profile for a printer, nozzle and filament combination."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .report import PrintSettings

KNOWN_DEFECTS = {
    "rough_top",
    "top_underfill",
    "rough_outer_wall",
    "bed_contact_roughness",
    "support_scars",
    "support_underside_roughness",
    "stringing",
    "visible_seam",
    "weak_part",
    "dimensional_error",
    "warping",
}
KNOWN_REGIONS = {
    "whole",
    "top",
    "outer_wall",
    "support_interface",
    "supported_underside",
    "seam",
    "base",
    "bed_contact",
    "dimensional_feature",
}


class PrintDNAError(RuntimeError):
    """Raised when local PrintDNA data is malformed or cannot be stored."""


@dataclass(frozen=True)
class PrintDNAKey:
    printer_model: str
    nozzle_diameter_mm: float
    material: str
    spool: str = "default"

    @property
    def identifier(self) -> str:
        canonical = (
            f"{self.printer_model.strip().casefold()}|{self.nozzle_diameter_mm:.3f}|"
            f"{self.material.strip().upper()}|{self.spool.strip().casefold()}"
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:20]

    def to_dict(self) -> dict[str, Any]:
        return {
            "printer_model": self.printer_model,
            "nozzle_diameter_mm": self.nozzle_diameter_mm,
            "material": self.material.upper(),
            "spool": self.spool,
        }


@dataclass(frozen=True)
class PrintFeedback:
    project_id: str
    source_name: str
    quality_rating: int
    support_removal_rating: int | None = None
    dimensional_rating: int | None = None
    defects: tuple[str, ...] = ()
    notes: str = ""
    created_utc: str = ""
    photo_path: str = ""
    photo_sha256: str = ""
    defect_layer_index: int | None = None
    defect_region: str = "whole"
    parameter_snapshot: dict[str, Any] | None = None

    def normalized(self) -> PrintFeedback:
        if not 1 <= int(self.quality_rating) <= 10:
            raise PrintDNAError("quality rating must be between 1 and 10")
        for value, label in (
            (self.support_removal_rating, "support removal rating"),
            (self.dimensional_rating, "dimensional rating"),
        ):
            if value is not None and not 1 <= int(value) <= 5:
                raise PrintDNAError(f"{label} must be between 1 and 5")
        defects = tuple(sorted({item for item in self.defects if item in KNOWN_DEFECTS}))
        layer = self.defect_layer_index
        if layer is not None and int(layer) < 1:
            raise PrintDNAError("defect layer index must be positive")
        region = self.defect_region if self.defect_region in KNOWN_REGIONS else "whole"
        snapshot: dict[str, Any] = {}
        for key, value in dict(self.parameter_snapshot or {}).items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                snapshot[str(key)[:100]] = value
        return replace(
            self,
            quality_rating=int(self.quality_rating),
            support_removal_rating=(
                int(self.support_removal_rating)
                if self.support_removal_rating is not None
                else None
            ),
            dimensional_rating=(
                int(self.dimensional_rating)
                if self.dimensional_rating is not None
                else None
            ),
            defects=defects,
            notes=self.notes.strip()[:2000],
            created_utc=self.created_utc or datetime.now(UTC).isoformat(),
            defect_layer_index=(int(layer) if layer is not None else None),
            defect_region=region,
            parameter_snapshot=snapshot,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "source_name": self.source_name,
            "quality_rating": self.quality_rating,
            "support_removal_rating": self.support_removal_rating,
            "dimensional_rating": self.dimensional_rating,
            "defects": list(self.defects),
            "notes": self.notes,
            "created_utc": self.created_utc,
            "photo_path": self.photo_path,
            "photo_sha256": self.photo_sha256,
            "defect_layer_index": self.defect_layer_index,
            "defect_region": self.defect_region,
            "parameter_snapshot": dict(self.parameter_snapshot or {}),
        }


@dataclass(frozen=True)
class PrintDNAProfile:
    key: PrintDNAKey
    sample_count: int
    confidence: str
    defect_rates: dict[str, float]
    adjustments: dict[str, Any]
    updated_utc: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key.to_dict(),
            "sample_count": self.sample_count,
            "confidence": self.confidence,
            "defect_rates": dict(self.defect_rates),
            "adjustments": dict(self.adjustments),
            "updated_utc": self.updated_utc,
        }


@dataclass(frozen=True)
class PrintDNAApplication:
    settings: PrintSettings
    profile_id: str
    sample_count: int
    confidence: str
    adjustments: dict[str, Any]
    decisions: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "sample_count": self.sample_count,
            "confidence": self.confidence,
            "adjustments": dict(self.adjustments),
            "decisions": list(self.decisions),
        }


def _empty_store() -> dict[str, Any]:
    return {"schema_version": 3, "profiles": {}}


def _migrate_quality_scale(profiles: dict[str, Any]) -> dict[str, Any]:
    """Preserve the meaning of legacy 1–5 ratings on the new 1–10 scale."""
    for profile in profiles.values():
        if not isinstance(profile, dict):
            continue
        feedback = profile.get("feedback")
        if not isinstance(feedback, list):
            continue
        for item in feedback:
            if not isinstance(item, dict):
                continue
            rating = item.get("quality_rating")
            if isinstance(rating, int) and 1 <= rating <= 5:
                item["quality_rating"] = rating * 2
    return profiles


def _read_store(path: Path, *, strict: bool) -> dict[str, Any]:
    if not path.exists():
        return _empty_store()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        if strict:
            raise PrintDNAError(f"cannot read PrintDNA store: {path}") from exc
        return _empty_store()
    if not isinstance(value, dict) or not isinstance(value.get("profiles"), dict):
        if strict:
            raise PrintDNAError("unsupported or malformed PrintDNA store")
        return _empty_store()
    version = value.get("schema_version")
    if version in (1, 2):
        value = {
            "schema_version": 3,
            "profiles": _migrate_quality_scale(value["profiles"]),
        }
    elif version != 3:
        if strict:
            raise PrintDNAError("unsupported or malformed PrintDNA store")
        return _empty_store()
    return value


def _write_store(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise PrintDNAError(f"cannot write PrintDNA store: {path}") from exc


def _key_from_dict(value: dict[str, Any]) -> PrintDNAKey:
    return PrintDNAKey(
        printer_model=str(value.get("printer_model", "Bambu Lab P1S")),
        nozzle_diameter_mm=float(value.get("nozzle_diameter_mm", 0.4)),
        material=str(value.get("material", "PLA")),
        spool=str(value.get("spool", "default")),
    )


def _profile_from_feedback(
    key: PrintDNAKey,
    records: Iterable[dict[str, Any]],
) -> PrintDNAProfile:
    items = list(records)
    count = len(items)
    defect_counts = {name: 0 for name in KNOWN_DEFECTS}
    support_low = 0
    dimensional_low = 0
    stringing_projects: set[str] = set()
    latest: str | None = None
    for item in items:
        for defect in item.get("defects", []):
            if defect in defect_counts:
                defect_counts[defect] += 1
            if defect == "stringing":
                project_id = str(item.get("project_id", "")).strip()
                if project_id:
                    stringing_projects.add(project_id)
        rating = item.get("support_removal_rating")
        if isinstance(rating, int) and rating <= 2:
            support_low += 1
        rating = item.get("dimensional_rating")
        if isinstance(rating, int) and rating <= 2:
            dimensional_low += 1
        stamp = str(item.get("created_utc", ""))
        if stamp and (latest is None or stamp > latest):
            latest = stamp

    rates = {
        name: (value / count if count else 0.0)
        for name, value in sorted(defect_counts.items())
    }
    adjustments: dict[str, Any] = {}
    if count:
        if rates["rough_top"] >= 0.25:
            adjustments.update(top_layers_delta=1, top_surface_speed_multiplier=0.85)
        if rates["top_underfill"] >= 0.20:
            # Visible gaps need wider, overlapping top tracks. Slowing an
            # already hot PETG roof further increases dwell and stringing.
            adjustments.update(
                top_layers_delta=1,
                top_surface_line_width_delta_mm=0.04,
            )
        if support_low / count >= 0.25:
            adjustments.update(support_z_delta_mm=0.04, support_spacing_delta_mm=0.05)
        elif rates["support_underside_roughness"] >= 0.20:
            # Supports detach cleanly, but the first model layers still sag
            # between interface lines. Bring the top contact one controlled
            # step closer and make the sacrificial roof denser and slower.
            # The application guard never lets the gap fall below one layer.
            adjustments.update(
                support_gap_delta_mm=-0.04,
                support_interface_layers_delta=2,
                support_interface_spacing_delta_mm=-0.08,
                support_interface_speed_multiplier=0.70,
            )
        elif rates["support_scars"] >= 0.20:
            # The supports already came away easily: increasing the gap would
            # make the underside worse. Improve the sacrificial interface
            # instead, while preserving the proven separation distance.
            adjustments.update(
                support_interface_layers_delta=1,
                support_interface_spacing_delta_mm=-0.05,
                support_interface_speed_multiplier=0.80,
            )
        if rates["rough_outer_wall"] >= 0.20:
            adjustments.update(
                outer_wall_speed_multiplier=0.75,
                outer_wall_acceleration_multiplier=0.65,
                visible_layer_height_cap_mm=0.12,
                wall_generator="classic",
            )
        if rates["bed_contact_roughness"] >= 0.20:
            adjustments["protect_visible_surfaces_from_bed"] = True
        if rates["stringing"] >= 0.25:
            persistent = len(stringing_projects) >= 2
            adjustments.update(
                nozzle_temperature_delta_c=-10 if persistent else -5,
                stringing_motion_guard=True,
                retraction_length_delta_mm=0.20 if persistent else 0.10,
                retraction_speed_delta_mm_s=10.0 if persistent else 5.0,
                wipe_distance_delta_mm=1.0 if persistent else 0.5,
            )
        if rates["visible_seam"] >= 0.20:
            adjustments["seam_position"] = "back"
        if rates["weak_part"] >= 0.20:
            adjustments.update(wall_loops_delta=1, infill_delta_percent=5)
        if rates["dimensional_error"] >= 0.20 or dimensional_low / count >= 0.25:
            adjustments.update(outer_wall_speed_multiplier=0.85, outer_wall_acceleration_multiplier=0.80)
        if rates["warping"] >= 0.20:
            adjustments.update(brim=True, bed_temperature_delta_c=5)
    confidence = "NONE" if count == 0 else "LOW" if count < 3 else "MEDIUM" if count < 8 else "HIGH"
    return PrintDNAProfile(key, count, confidence, rates, adjustments, latest)


def load_print_dna_profile(path: str | Path, key: PrintDNAKey) -> PrintDNAProfile:
    store_path = Path(path).expanduser().resolve()
    store = _read_store(store_path, strict=False)
    record = store["profiles"].get(key.identifier, {})
    feedback = record.get("feedback", []) if isinstance(record, dict) else []
    return _profile_from_feedback(key, feedback if isinstance(feedback, list) else [])


def load_combined_print_dna_profile(
    local_path: str | Path,
    global_path: str | Path,
    key: PrintDNAKey,
) -> PrintDNAProfile:
    """Combine anonymous community experience with the user's local feedback.

    Local observations receive triple weight so a user's own printer and spool
    quickly override population-wide defaults without discarding the useful
    cold-start knowledge collected from other installations.
    """
    local_store = _read_store(Path(local_path).expanduser().resolve(), strict=False)
    global_store = _read_store(Path(global_path).expanduser().resolve(), strict=False)

    def feedback(store: dict[str, Any]) -> list[dict[str, Any]]:
        record = store.get("profiles", {}).get(key.identifier, {})
        values = record.get("feedback", []) if isinstance(record, dict) else []
        return [item for item in values if isinstance(item, dict)] if isinstance(values, list) else []

    local = feedback(local_store)
    community = feedback(global_store)
    # Cap the global sample so a very large fleet cannot drown out local
    # calibration; local samples are intentionally repeated three times.
    bounded_community = community[-300:]
    bounded_local = local[-100:]
    combined = bounded_community + bounded_local * 3
    weighted = _profile_from_feedback(key, combined)

    # Weighting changes how strongly local observations influence the tuning,
    # but it must not pretend that one physical print is three independent
    # samples.  Confidence is based only on unique observations.
    unique_samples: set[tuple[str, str]] = set()
    for index, item in enumerate(bounded_community + bounded_local):
        project_id = str(item.get("project_id", "")).strip()
        photo_sha = str(item.get("photo_sha256", "")).strip()
        if project_id or photo_sha:
            identity = (project_id, photo_sha)
        else:
            identity = ("anonymous", json.dumps(item, sort_keys=True, ensure_ascii=False))
        unique_samples.add(identity)
    count = len(unique_samples)
    confidence = (
        "NONE" if count == 0 else "LOW" if count < 3 else "MEDIUM" if count < 8 else "HIGH"
    )
    return replace(weighted, sample_count=count, confidence=confidence)


def record_print_feedback(
    path: str | Path,
    key: PrintDNAKey,
    feedback: PrintFeedback,
) -> PrintDNAProfile:
    store_path = Path(path).expanduser().resolve()
    normalized = feedback.normalized()
    store = _read_store(store_path, strict=True)
    if normalized.photo_path:
        source_photo = Path(normalized.photo_path).expanduser().resolve()
        if not source_photo.is_file() or source_photo.suffix.casefold() not in {
            ".jpg", ".jpeg", ".png", ".webp"
        }:
            raise PrintDNAError("feedback photo must be an existing JPG, PNG or WEBP file")
        digest = hashlib.sha256(source_photo.read_bytes()).hexdigest()
        photo_dir = store_path.parent / "print_dna_photos" / key.identifier
        photo_dir.mkdir(parents=True, exist_ok=True)
        destination = photo_dir / f"{digest[:24]}{source_photo.suffix.casefold()}"
        if not destination.exists():
            temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
            try:
                shutil.copyfile(source_photo, temporary)
                if hashlib.sha256(temporary.read_bytes()).hexdigest() != digest:
                    raise PrintDNAError("feedback photo copy failed SHA-256 verification")
                os.replace(temporary, destination)
            finally:
                temporary.unlink(missing_ok=True)
        normalized = replace(
            normalized,
            photo_path=str(destination),
            photo_sha256=digest,
        )
    profiles = store["profiles"]
    record = profiles.setdefault(key.identifier, {"key": key.to_dict(), "feedback": []})
    if not isinstance(record, dict) or not isinstance(record.get("feedback"), list):
        raise PrintDNAError("malformed PrintDNA profile")
    record["key"] = key.to_dict()
    # Importing the same physical print twice must not increase confidence.
    duplicate = next(
        (
            item
            for item in record["feedback"]
            if isinstance(item, dict)
            and str(item.get("project_id", "")) == normalized.project_id
            and str(item.get("photo_sha256", "")) == normalized.photo_sha256
        ),
        None,
    )
    if duplicate is None:
        record["feedback"].append(normalized.to_dict())
    record["feedback"] = record["feedback"][-100:]
    _write_store(store_path, store)
    return _profile_from_feedback(key, record["feedback"])


def profile_from_dict(value: dict[str, Any] | None) -> PrintDNAProfile | None:
    if not value or not isinstance(value.get("key"), dict):
        return None
    key = _key_from_dict(value["key"])
    return PrintDNAProfile(
        key=key,
        sample_count=max(0, int(value.get("sample_count", 0))),
        confidence=str(value.get("confidence", "NONE")),
        defect_rates={str(k): float(v) for k, v in dict(value.get("defect_rates", {})).items()},
        adjustments=dict(value.get("adjustments", {})),
        updated_utc=str(value["updated_utc"]) if value.get("updated_utc") else None,
    )


def apply_print_dna(
    base: PrintSettings,
    profile: PrintDNAProfile,
) -> PrintDNAApplication:
    adjustments = profile.adjustments
    changes: dict[str, Any] = {}
    decisions: list[str] = []

    if "top_layers_delta" in adjustments:
        changes["top_layers"] = min(10, base.top_layers + int(adjustments["top_layers_delta"]))
    if "top_surface_speed_multiplier" in adjustments:
        changes["top_surface_speed_mm_s"] = max(
            30,
            round(base.top_surface_speed_mm_s * float(adjustments["top_surface_speed_multiplier"])),
        )
        decisions.append("PrintDNA снизил скорость верхней поверхности по предыдущим отпечаткам.")
    if "top_surface_line_width_delta_mm" in adjustments:
        changes["top_surface_line_width_mm"] = min(
            0.60,
            base.top_surface_line_width_mm
            + float(adjustments["top_surface_line_width_delta_mm"]),
        )
        decisions.append("PrintDNA увеличил перекрытие линий на неплотной верхней поверхности.")
    if "visible_layer_height_cap_mm" in adjustments:
        changes["layer_height_mm"] = min(
            base.layer_height_mm,
            float(adjustments["visible_layer_height_cap_mm"]),
        )
    if adjustments.get("wall_generator"):
        changes["wall_generator"] = str(adjustments["wall_generator"])
        decisions.append("PrintDNA защитил органические внешние поверхности классическими периметрами.")
    if "support_z_delta_mm" in adjustments:
        delta = float(adjustments["support_z_delta_mm"])
        changes["support_top_z_distance_mm"] = min(0.36, base.support_top_z_distance_mm + delta)
        changes["support_bottom_z_distance_mm"] = min(0.36, base.support_bottom_z_distance_mm + delta)
        changes["support_interface_spacing_mm"] = min(
            0.60,
            base.support_interface_spacing_mm + float(adjustments.get("support_spacing_delta_mm", 0.0)),
        )
        decisions.append("PrintDNA увеличил отделяемость поддержек после оценки пользователя.")
    if "support_gap_delta_mm" in adjustments:
        changes["support_top_z_distance_mm"] = max(
            base.layer_height_mm,
            min(
                0.36,
                base.support_top_z_distance_mm
                + float(adjustments["support_gap_delta_mm"]),
            ),
        )
        decisions.append(
            "PrintDNA откалибровал верхний зазор поддержек для более чистой нижней поверхности."
        )
    if "support_interface_layers_delta" in adjustments:
        changes["support_interface_top_layers"] = min(
            8,
            base.support_interface_top_layers
            + int(adjustments["support_interface_layers_delta"]),
        )
        changes["support_interface_spacing_mm"] = max(
            0.18,
            base.support_interface_spacing_mm
            + float(adjustments.get("support_interface_spacing_delta_mm", 0.0)),
        )
        changes["support_interface_speed_mm_s"] = max(
            30,
            round(
                base.support_interface_speed_mm_s
                * float(adjustments.get("support_interface_speed_multiplier", 1.0))
            ),
        )
        decisions.append("PrintDNA уплотнил и замедлил интерфейс поддержек без увеличения зазора.")
    if "nozzle_temperature_delta_c" in adjustments:
        changes["nozzle_temperature_c"] = max(
            185,
            min(280, base.nozzle_temperature_c + int(adjustments["nozzle_temperature_delta_c"])),
        )
        decisions.append("PrintDNA скорректировал температуру против нитей пластика.")
    if adjustments.get("stringing_motion_guard"):
        changes.update(
            reduce_crossing_wall=True,
            avoid_crossing_wall_includes_support=True,
            reduce_infill_retraction_mode="Auto",
            travel_speed_mm_s=max(500, base.travel_speed_mm_s),
            retraction_length_mm=min(
                1.2,
                base.retraction_length_mm
                + float(adjustments.get("retraction_length_delta_mm", 0.0)),
            ),
            retraction_speed_mm_s=min(
                45.0,
                base.retraction_speed_mm_s
                + float(adjustments.get("retraction_speed_delta_mm_s", 0.0)),
            ),
            wipe_enabled=True,
            wipe_distance_mm=min(
                4.0,
                base.wipe_distance_mm
                + float(adjustments.get("wipe_distance_delta_mm", 0.0)),
            ),
        )
        decisions.append("PrintDNA сократил открытые перемещения сопла против волосистости.")
    if adjustments.get("seam_position"):
        changes.update(
            seam_position=str(adjustments["seam_position"]),
            scarf_seam_type="external",
            override_filament_scarf_seam=True,
        )
        decisions.append("PrintDNA перенёс и сгладил заметный шов.")
    if "wall_loops_delta" in adjustments:
        changes["wall_loops"] = min(8, base.wall_loops + int(adjustments["wall_loops_delta"]))
        changes["sparse_infill_percent"] = min(
            60,
            base.sparse_infill_percent + int(adjustments.get("infill_delta_percent", 0)),
        )
        decisions.append("PrintDNA усилил деталь по результату проверки прочности.")
    if "outer_wall_speed_multiplier" in adjustments:
        changes["outer_wall_speed_mm_s"] = max(
            30,
            round(base.outer_wall_speed_mm_s * float(adjustments["outer_wall_speed_multiplier"])),
        )
        changes["outer_wall_acceleration_mm_s2"] = max(
            500,
            round(base.outer_wall_acceleration_mm_s2 * float(adjustments.get("outer_wall_acceleration_multiplier", 1.0))),
        )
        decisions.append("PrintDNA ограничил динамику внешней стенки для чистоты поверхности.")
    if adjustments.get("protect_visible_surfaces_from_bed"):
        decisions.append("PrintDNA запретил класть крупную видимую поверхность прямо на стол.")
    if adjustments.get("brim"):
        changes["brim"] = True
        changes["bed_temperature_c"] = min(
            90,
            base.bed_temperature_c + int(adjustments.get("bed_temperature_delta_c", 0)),
        )
        decisions.append("PrintDNA усилил адгезию после зафиксированной деформации.")

    return PrintDNAApplication(
        settings=replace(base, **changes),
        profile_id=profile.key.identifier,
        sample_count=profile.sample_count,
        confidence=profile.confidence,
        adjustments=dict(adjustments),
        decisions=tuple(decisions),
    )
