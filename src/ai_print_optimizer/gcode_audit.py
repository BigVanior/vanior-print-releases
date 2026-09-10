"""Independent post-slice safety audit based on the emitted G-code."""

from __future__ import annotations

import math
import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .input_safety import MAX_TEXT_LINE_BYTES, UnsafeInputError, validate_gcode_file

_NUMBER = re.compile(r"([A-Za-z])(-?(?:\d+(?:\.\d*)?|\.\d+))")
_NON_FINITE_AXIS = re.compile(r"(?:^|\s)[XYZEF](?:[-+]?NAN|[-+]?INF(?:INITY)?)(?:\s|$)", re.IGNORECASE)
_LAYER = re.compile(r"^;\s*(?:CHANGE_LAYER|LAYER_CHANGE|LAYER:\s*\d+)", re.IGNORECASE)


@dataclass(frozen=True)
class GCodeAudit:
    version: int
    gcode_path: Path
    status: str
    safety_score: float
    layer_count: int
    extrusion_moves: int
    travel_moves: int
    retraction_count: int
    maximum_volumetric_flow_mm3_s: float
    p95_volumetric_flow_mm3_s: float
    shortest_estimated_layer_time_s: float | None
    maximum_feedrate_mm_s: float
    blocking_warnings: tuple[str, ...]
    advisory_warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["gcode_path"] = str(self.gcode_path)
        return value


def audit_gcode(
    gcode: str | Path,
    *,
    filament_diameters_mm: Sequence[float] = (1.75,),
    maximum_volumetric_speed_mm3_s: float | None = None,
    maximum_coordinate_abs_mm: float = 1_000.0,
    maximum_nozzle_temperature_c: float = 320.0,
    maximum_bed_temperature_c: float = 150.0,
) -> GCodeAudit:
    """Audit motion/extrusion commands without trusting slicer summary metadata."""
    path = Path(gcode).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() != ".gcode":
        raise ValueError(f"G-code audit requires an existing .gcode file: {path}")
    try:
        validate_gcode_file(path)
    except UnsafeInputError as exc:
        raise ValueError(f"unsafe G-code: {exc}") from exc
    diameters = tuple(float(value) for value in filament_diameters_mm) or (1.75,)
    if any(not math.isfinite(value) or value <= 0 for value in diameters):
        raise ValueError("filament diameters must be finite positive values")

    absolute_xyz = True
    relative_e = False
    x = y = z = e = 0.0
    feed_mm_min = 0.0
    tool = 0
    # A deterministic bounded sample keeps multi-gigabyte production G-code
    # auditable without retaining one Python float per extrusion move.
    flows: list[float] = []
    flow_stride = 1
    flow_seen = 0
    layer_times: list[float] = []
    current_layer_time = 0.0
    current_layer_has_extrusion = False
    layer_count = 0
    extrusion_moves = travel_moves = retractions = 0
    maximum_feed = 0.0
    blocking: list[str] = []
    advisory: list[str] = []

    with path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw in stream:
            if len(raw) > MAX_TEXT_LINE_BYTES:
                blocking.append("G-code содержит строку ненормального размера.")
                continue
            stripped = raw.strip()
            if _LAYER.match(stripped):
                if layer_count and current_layer_has_extrusion:
                    layer_times.append(current_layer_time)
                layer_count += 1
                current_layer_time = 0.0
                current_layer_has_extrusion = False
                continue
            command = stripped.partition(";")[0].strip()
            if not command:
                continue
            if _NON_FINITE_AXIS.search(command):
                blocking.append("G-code содержит нечисловую или бесконечную координату.")
                continue
            upper = command.upper()
            if upper == "G90":
                absolute_xyz = True
                continue
            if upper == "G91":
                absolute_xyz = False
                continue
            if upper == "M82":
                relative_e = False
                continue
            if upper == "M83":
                relative_e = True
                continue
            if upper.startswith("T") and upper[1:].isdigit():
                tool = max(0, int(upper[1:]))
                continue
            values = {key.upper(): float(value) for key, value in _NUMBER.findall(command)}
            if any(not math.isfinite(value) for value in values.values()):
                blocking.append("G-code содержит нечисловую или бесконечную координату.")
                continue
            opcode = upper.split(maxsplit=1)[0]
            if opcode in {"M104", "M109"} and values.get("S", 0.0) > maximum_nozzle_temperature_c:
                blocking.append("Температура сопла выходит за аппаратный предел проверки.")
                continue
            if opcode in {"M140", "M190"} and values.get("S", 0.0) > maximum_bed_temperature_c:
                blocking.append("Температура стола выходит за аппаратный предел проверки.")
                continue
            if opcode == "G4":
                dwell_s = values.get("P", 0.0) / 1000.0 + values.get("S", 0.0)
                if dwell_s < 0 or dwell_s > 300.0:
                    blocking.append("Команда ожидания G4 выходит за безопасный предел.")
                else:
                    current_layer_time += dwell_s
                continue
            if opcode == "G92":
                x = values.get("X", x)
                y = values.get("Y", y)
                z = values.get("Z", z)
                e = values.get("E", e)
                continue
            if opcode not in {"G0", "G1", "G2", "G3"}:
                continue
            if "F" in values:
                feed_mm_min = max(0.0, values["F"])
                maximum_feed = max(maximum_feed, feed_mm_min / 60.0)
            nx = values.get("X", x if absolute_xyz else 0.0)
            ny = values.get("Y", y if absolute_xyz else 0.0)
            nz = values.get("Z", z if absolute_xyz else 0.0)
            if not absolute_xyz:
                nx += x
                ny += y
                nz += z
            ne = values.get("E", e if not relative_e else 0.0)
            delta_e = (ne - e) if ("E" in values and not relative_e) else (ne if "E" in values else 0.0)
            if relative_e and "E" in values:
                ne = e + delta_e
            dx, dy, dz = nx - x, ny - y, nz - z
            if max(abs(nx), abs(ny), abs(nz)) > maximum_coordinate_abs_mm:
                blocking.append("Координата движения выходит за предел независимой проверки.")
                x, y, z, e = nx, ny, nz, ne
                continue
            distance = math.sqrt(dx * dx + dy * dy + dz * dz)
            speed = feed_mm_min / 60.0
            if distance > 1e-8 and speed > 1e-8:
                current_layer_time += distance / speed
            if delta_e > 1e-7 and distance > 1e-7:
                extrusion_moves += 1
                current_layer_has_extrusion = True
                diameter = diameters[min(tool, len(diameters) - 1)]
                volume = delta_e * math.pi * (diameter * 0.5) ** 2
                duration = distance / speed if speed > 1e-8 else 0.0
                if duration > 0:
                    flow = volume / duration
                    if math.isfinite(flow) and flow >= 0:
                        flow_seen += 1
                        if flow_seen % flow_stride == 0:
                            flows.append(flow)
                        if len(flows) >= 1_000_000:
                            flows = flows[::2]
                            flow_stride *= 2
            elif abs(delta_e) > 1e-7 and distance <= 1e-7:
                if speed > 1e-8:
                    current_layer_time += abs(delta_e) / speed
                if delta_e < -1e-5:
                    retractions += 1
            elif distance > 1e-7:
                travel_moves += 1
            x, y, z, e = nx, ny, nz, ne
    if layer_count and current_layer_has_extrusion:
        layer_times.append(current_layer_time)
    if extrusion_moves == 0:
        blocking.append("G-code не содержит движений печати с положительной экструзией.")
    maximum_flow = max(flows, default=0.0)
    ordered = sorted(flows)
    p95 = ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))] if ordered else 0.0
    if maximum_volumetric_speed_mm3_s and p95 > maximum_volumetric_speed_mm3_s * 1.08:
        blocking.append(
            "Устойчивый объёмный поток превышает лимит филамента: "
            f"P95 {p95:.1f} > {maximum_volumetric_speed_mm3_s:.1f} мм³/с."
        )
    elif maximum_volumetric_speed_mm3_s and p95 > maximum_volumetric_speed_mm3_s:
        advisory.append(
            f"95-й перцентиль потока близок к пределу: {p95:.1f} мм³/с."
        )
    if maximum_volumetric_speed_mm3_s and maximum_flow > maximum_volumetric_speed_mm3_s * 1.5:
        advisory.append(
            "Обнаружен единичный пик экструзии; он рассматривается как восстановление "
            "ретракта и не блокирует печать без устойчивой перегрузки."
        )
    shortest = min(layer_times) if layer_times else None
    short_layer_count = sum(value < 2.0 for value in layer_times)
    if shortest is not None and short_layer_count >= 3:
        advisory.append(
            f"Есть {short_layer_count} очень быстрых слоя (минимум {shortest:.2f} с); "
            "охлаждение должно быть проверено."
        )
    score = max(0.0, 100.0 - 35.0 * len(blocking) - 3.0 * len(advisory))
    status = "BLOCKED" if blocking else "WARNING" if advisory else "PASS"
    return GCodeAudit(
        version=2,
        gcode_path=path,
        status=status,
        safety_score=round(score, 2),
        layer_count=max(layer_count, len(layer_times)),
        extrusion_moves=extrusion_moves,
        travel_moves=travel_moves,
        retraction_count=retractions,
        maximum_volumetric_flow_mm3_s=round(maximum_flow, 3),
        p95_volumetric_flow_mm3_s=round(p95, 3),
        shortest_estimated_layer_time_s=(round(shortest, 3) if shortest is not None else None),
        maximum_feedrate_mm_s=round(maximum_feed, 3),
        blocking_warnings=tuple(blocking),
        advisory_warnings=tuple(advisory),
    )
