"""Stable backend boundary used while replacing the embedded AGPL slicer."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import Protocol

from .progress import ProgressCallback
from .report import PrintSettings
from .vanior_slice import VaniorSliceResult, slice_stl_to_gcode


class SlicerBackend(Protocol):
    name: str
    independent: bool

    def slice_stl(
        self,
        source: str | Path,
        output: str | Path,
        settings: PrintSettings,
        *,
        material: str,
        nozzle_diameter_mm: float,
        overhang_angle_deg: float = 45.0,
        support_strategy: str = "auto",
        progress_callback: ProgressCallback | None = None,
        cancel_event: Event | None = None,
    ) -> VaniorSliceResult: ...


@dataclass(frozen=True)
class VaniorSlicerBackend:
    name: str = "VANIOR Slice"
    independent: bool = True

    def slice_stl(
        self,
        source: str | Path,
        output: str | Path,
        settings: PrintSettings,
        *,
        material: str,
        nozzle_diameter_mm: float,
        overhang_angle_deg: float = 45.0,
        support_strategy: str = "auto",
        progress_callback: ProgressCallback | None = None,
        cancel_event: Event | None = None,
    ) -> VaniorSliceResult:
        return slice_stl_to_gcode(
            source,
            output,
            settings,
            material=material,
            nozzle_diameter_mm=nozzle_diameter_mm,
            overhang_angle_deg=overhang_angle_deg,
            support_strategy=support_strategy,
            progress_callback=progress_callback,
            cancel_event=cancel_event,
        )


INDEPENDENT_BACKEND = VaniorSlicerBackend()
