"""Material-specific final safety and quality guardrails.

Geometry, purpose, priority and PrintDNA are allowed to propose settings first.
This module is deliberately applied last so a fast preset or learned adjustment
cannot push a material outside a stable process window.
"""

from __future__ import annotations

from dataclasses import replace

from .report import PrintSettings


class MaterialProtocolError(ValueError):
    """Raised when no verified material protocol exists."""


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


# Conservative cross-printer limits used only to narrow the family protocol.
# They are intentionally not advertised as replacements for a user's measured
# spool calibration or a manufacturer's newest official slicer preset.
MATERIAL_PRODUCT_PROTOCOLS: dict[str, tuple[str, float, int, int]] = {
    "GENERIC PLA": ("PLA", 15.0, 190, 230),
    "BAMBU LAB PLA BASIC": ("PLA", 21.0, 190, 230),
    "BAMBU LAB PLA LITE": ("PLA", 16.0, 190, 230),
    "BAMBU LAB PLA MATTE": ("PLA", 18.0, 190, 230),
    "BAMBU LAB PLA SILK": ("PLA", 12.0, 200, 235),
    "ESUN PLA+": ("PLA", 16.0, 195, 230),
    "SUNLU PLA": ("PLA", 15.0, 190, 230),
    "POLYMAKER POLYLITE PLA": ("PLA", 15.0, 190, 230),
    "PRUSAMENT PLA": ("PLA", 15.0, 195, 230),
    "OVERTURE PLA": ("PLA", 15.0, 190, 230),
    "GENERIC PETG": ("PETG", 10.0, 235, 260),
    "BAMBU LAB PETG BASIC": ("PETG", 12.0, 235, 265),
    "BAMBU LAB PETG HF": ("PETG", 12.0, 230, 260),
    "ESUN PETG": ("PETG", 10.0, 235, 260),
    "SUNLU PETG": ("PETG", 10.0, 235, 260),
    "POLYMAKER POLYLITE PETG": ("PETG", 10.0, 235, 260),
    "PRUSAMENT PETG": ("PETG", 10.0, 240, 265),
    "OVERTURE PETG": ("PETG", 10.0, 235, 260),
}


def settings_for_material(
    base: PrintSettings,
    material: str,
    *,
    nozzle_diameter_mm: float = 0.4,
) -> tuple[PrintSettings, tuple[str, ...]]:
    """Apply conservative P1S process windows without erasing calibration.

    Existing source-3MF temperature, flow and retraction values are retained
    whenever they are already inside the verified range.  PETG receives its
    own thermal, cooling, flow, motion, stringing and support-separation
    limits; it is never treated as "PLA with a hotter nozzle".
    """

    requested = material.strip().upper()
    product = MATERIAL_PRODUCT_PROTOCOLS.get(requested)
    selected = product[0] if product else requested
    if selected not in {"PLA", "PETG"}:
        raise MaterialProtocolError(
            f"unsupported material protocol: {material}; choose PLA or PETG"
        )

    if selected == "PLA":
        safe = replace(
            base,
            nozzle_temperature_c=round(_clamp(base.nozzle_temperature_c, 190, 235)),
            bed_temperature_c=round(_clamp(base.bed_temperature_c, 45, 65)),
            fan_percent=round(_clamp(base.fan_percent, 70, 100)),
            max_volumetric_speed_mm3_s=_clamp(
                base.max_volumetric_speed_mm3_s, 2.0, 21.0
            ),
            filament_flow_ratio=_clamp(base.filament_flow_ratio, 0.90, 1.08),
            retraction_length_mm=_clamp(base.retraction_length_mm, 0.4, 1.2),
            retraction_speed_mm_s=_clamp(base.retraction_speed_mm_s, 20.0, 45.0),
            wipe_distance_mm=_clamp(base.wipe_distance_mm, 1.0, 4.0),
        )
        decisions = [
            "Материал PLA: проверены безопасные пределы температуры, охлаждения, потока и ретракта."
        ]
        if product:
            safe = replace(
                safe,
                nozzle_temperature_c=round(
                    _clamp(safe.nozzle_temperature_c, product[2], product[3])
                ),
                max_volumetric_speed_mm3_s=min(
                    safe.max_volumetric_speed_mm3_s, product[1]
                ),
            )
            decisions.append(
                f"Продукт {material.strip()}: применён отдельный консервативный предел потока."
            )
        return safe, tuple(decisions)

    layer = float(base.layer_height_mm)
    support_gap = max(layer, 0.22 if nozzle_diameter_mm <= 0.4 else nozzle_diameter_mm * 0.55)
    safe = replace(
        base,
        nozzle_temperature_c=round(_clamp(base.nozzle_temperature_c, 235, 265)),
        bed_temperature_c=round(_clamp(base.bed_temperature_c, 65, 85)),
        fan_percent=round(_clamp(base.fan_percent, 30, 60)),
        # Generic PETG and PETG Basic remain stable below this flow on P1S.
        # A proven slower source profile is kept; an optimistic one is capped.
        max_volumetric_speed_mm3_s=_clamp(
            base.max_volumetric_speed_mm3_s, 2.0, 12.0
        ),
        filament_flow_ratio=_clamp(base.filament_flow_ratio, 0.90, 1.05),
        # A calibrated volumetric-flow limit remains the primary PETG speed
        # guard. Overly low nominal speeds keep a hot nozzle above the part
        # longer and can increase ooze, as confirmed by the physical Benchy A/B.
        outer_wall_speed_mm_s=min(base.outer_wall_speed_mm_s, 120),
        inner_wall_speed_mm_s=min(base.inner_wall_speed_mm_s, 200),
        sparse_infill_speed_mm_s=min(base.sparse_infill_speed_mm_s, 220),
        internal_solid_infill_speed_mm_s=min(
            base.internal_solid_infill_speed_mm_s, 160
        ),
        top_surface_speed_mm_s=min(base.top_surface_speed_mm_s, 90),
        support_speed_mm_s=min(base.support_speed_mm_s, 100),
        support_interface_speed_mm_s=min(base.support_interface_speed_mm_s, 50),
        bridge_speed_mm_s=min(base.bridge_speed_mm_s, 40),
        initial_layer_speed_mm_s=min(base.initial_layer_speed_mm_s, 35),
        default_acceleration_mm_s2=min(base.default_acceleration_mm_s2, 9_000),
        outer_wall_acceleration_mm_s2=min(base.outer_wall_acceleration_mm_s2, 3_500),
        top_surface_acceleration_mm_s2=min(base.top_surface_acceleration_mm_s2, 2_000),
        support_top_z_distance_mm=max(base.support_top_z_distance_mm, support_gap),
        support_bottom_z_distance_mm=max(base.support_bottom_z_distance_mm, support_gap),
        support_object_xy_distance_mm=max(
            base.support_object_xy_distance_mm,
            max(0.45, nozzle_diameter_mm),
        ),
        support_interface_top_layers=max(base.support_interface_top_layers, 4),
        support_interface_spacing_mm=max(base.support_interface_spacing_mm, 0.32),
        slow_down_layer_time_s=max(base.slow_down_layer_time_s, 10),
        slow_down_min_speed_mm_s=min(base.slow_down_min_speed_mm_s, 20),
        ironing_enabled=False,
        reduce_crossing_wall=True,
        avoid_crossing_wall_includes_support=True,
        reduce_infill_retraction_mode="Auto",
        retraction_length_mm=_clamp(base.retraction_length_mm, 0.6, 1.0),
        retraction_speed_mm_s=_clamp(base.retraction_speed_mm_s, 25.0, 35.0),
        wipe_enabled=True,
        wipe_distance_mm=_clamp(base.wipe_distance_mm, 2.0, 3.5),
    )
    decisions = [
        "Материал PETG: применено отдельное тепловое окно и ограничен объёмный поток.",
        "PETG: замедлены видимые поверхности, мосты и интерфейс поддержек.",
        "PETG: включена защита от волосистости и увеличены отделяемые зазоры поддержек.",
    ]
    if product:
        safe = replace(
            safe,
            nozzle_temperature_c=round(
                _clamp(safe.nozzle_temperature_c, product[2], product[3])
            ),
            max_volumetric_speed_mm3_s=min(
                safe.max_volumetric_speed_mm3_s, product[1]
            ),
        )
        decisions.append(
            f"Продукт {material.strip()}: применён отдельный консервативный предел потока."
        )
    return safe, tuple(decisions)
