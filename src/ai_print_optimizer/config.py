"""Supported printer and material profiles."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PrinterProfile:
    name: str
    build_volume_mm: tuple[float, float, float]


@dataclass(frozen=True)
class MaterialProfile:
    name: str
    nozzle_temperature_c: int
    bed_temperature_c: int
    default_fan_percent: int


P1S = PrinterProfile(
    name="Bambu Lab P1S",
    build_volume_mm=(256.0, 256.0, 256.0),
)

SUPPORTED_NOZZLE_DIAMETERS_MM = (0.2, 0.4, 0.6, 0.8)

MATERIALS = {
    "PLA": MaterialProfile(
        name="PLA",
        nozzle_temperature_c=220,
        bed_temperature_c=55,
        default_fan_percent=100,
    ),
    "PETG": MaterialProfile(
        name="PETG",
        nozzle_temperature_c=255,
        bed_temperature_c=70,
        default_fan_percent=50,
    ),
}
