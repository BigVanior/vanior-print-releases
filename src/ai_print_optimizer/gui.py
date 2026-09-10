"""Legacy Russian-language Qt desktop interface for VANIOR PRINT."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import traceback
from dataclasses import replace
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import QObject, QSettings, Qt, QThread, QTimer, Signal, Slot
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QFont
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QStackedWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from .analyzer import analyze_stl
from .delivery import publish_single_3mf
from .gcode_audit import audit_gcode
from .input_safety import (
    MAX_TEXT_LINE_BYTES,
    UnsafeInputError,
    validate_gcode_file,
    validate_stl_file,
)
from .model_purpose import select_model_purpose
from .optimization_protocol import (
    build_optimization_plan,
    select_independent_optimization_candidate,
)
from .orientation import orient_stl
from .pipeline import PipelineResult, _configure_analysis_for_print, run_pipeline
from .profile import validate_bambu_profile
from .progress import (
    OperationCancelled,
    ProgressCallback,
    ProgressEvent,
    check_cancelled,
    emit_progress,
)
from .project3mf import extract_printable_stl
from .reliability import JobJournal
from .repair import repair_stl
from .slicer import (
    SliceRunResult,
    discover_bambu_studio,
    ensure_run_publishable,
    slice_3mf,
)
from .vanior_3mf import create_vanior_gcode_3mf
from .vanior_slice import slice_stl_to_gcode
from .version import __version__

APP_NAME = "VANIOR PRINT"
SUPPORTED_MODEL_SUFFIXES = {".stl", ".3mf"}
_GCODE_Z_HEIGHT = re.compile(r"^;\s*Z_HEIGHT:\s*([-+0-9.eE]+)\s*$")
_GCODE_ALT_Z_HEIGHT = re.compile(r"^;\s*(?:Z|HEIGHT):\s*([-+0-9.eE]+)\s*$", re.IGNORECASE)
_GCODE_FEATURE = re.compile(r"^;\s*(?:FEATURE|TYPE):\s*(.+?)\s*$", re.IGNORECASE)
_GCODE_PARAMETER = re.compile(r"([XYZE])([-+0-9.eE]+)")


def _preview_role(feature: str) -> str:
    normalized = feature.strip().casefold().replace("_", " ")
    if "support" in normalized and "interface" in normalized:
        return "support_interface"
    if "support" in normalized:
        return "support"
    if "outer" in normalized or "external perimeter" in normalized:
        return "outer_wall"
    if "inner" in normalized or "internal perimeter" in normalized:
        return "inner_wall"
    if "top" in normalized or "ironing" in normalized:
        return "top_surface"
    if "bottom" in normalized:
        return "bottom_surface"
    if "bridge" in normalized:
        return "bridge"
    if "internal solid" in normalized or "solid infill" in normalized:
        return "solid_infill"
    if "infill" in normalized:
        return "infill"
    if "skirt" in normalized or "brim" in normalized:
        return "skirt_brim"
    return "model"


def suggest_output_path(source: Path) -> Path:
    """Return a new sibling result directory without overwriting prior work."""
    base = source.with_name(f"{source.stem}-result")
    if not base.exists():
        return base
    index = 2
    while True:
        candidate = source.with_name(f"{source.stem}-result-{index}")
        if not candidate.exists():
            return candidate
        index += 1


def _format_duration(seconds: float) -> str:
    total = max(0, round(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {seconds} с"
    return f"{seconds} с"


def _health_text(status: str) -> str:
    return {
        "READY": "Готова",
        "REPAIR_RECOMMENDED": "Нужно исправление",
        "INVALID": "Некорректна",
    }.get(status, status)


def build_preview_mesh(
    model_path: Path,
    *,
    max_faces: int = 100_000,
    interactive_faces: int = 20_000,
) -> dict[str, Any]:
    """Build detailed and interactive levels of detail for the GUI preview.

    Loading, vertex merging and simplification intentionally happen in the job
    worker.  A detailed STL must never be parsed from the Qt event thread.
    The detailed mesh is uploaded to the GPU and remains visible throughout
    camera interaction.  A compact second mesh is retained only for the
    software-rendering fallback used with broken or outdated graphics drivers.
    """
    import trimesh

    try:
        validate_stl_file(model_path)
    except UnsafeInputError:
        return {"vertices": [], "faces": []}
    loaded = trimesh.load(model_path, force="mesh", process=True)
    if not isinstance(loaded, trimesh.Trimesh):
        return {"vertices": [], "faces": []}
    mesh = loaded

    def decimate(source: Any, target: int) -> Any:
        if len(source.faces) <= target:
            return source.copy()
        try:
            simplified = source.simplify_quadric_decimation(face_count=target)
            if len(simplified.vertices) and len(simplified.faces):
                return simplified
        except Exception:  # noqa: BLE001,S110 -- optional UI preview fallback
            pass
        # Preserve the surface when the optional quadric decimator cannot
        # process a damaged/non-manifold mesh.  Sampling disconnected faces
        # produced the former "hairy point cloud" preview.  Vertex clustering
        # keeps all neighbouring faces connected while collapsing only nearby
        # vertices.
        import numpy as np

        vertices = np.asarray(source.vertices, dtype=np.float64)
        faces = np.asarray(source.faces, dtype=np.int64)
        lower = vertices.min(axis=0)
        extent = np.maximum(vertices.max(axis=0) - lower, 1e-9)
        longest = float(extent.max())
        base_bins = max(4, int(math.sqrt(max(8, target // 2))))
        clustered = None
        for attempt in range(5):
            factor = 0.78**attempt
            bins = np.maximum(2, np.rint(base_bins * factor * extent / longest).astype(np.int64))
            coordinates = np.floor((vertices - lower) / extent * bins).astype(np.int64)
            coordinates = np.minimum(coordinates, bins - 1)
            _, inverse = np.unique(coordinates, axis=0, return_inverse=True)
            counts = np.bincount(inverse)
            compact_vertices = np.zeros((len(counts), 3), dtype=np.float64)
            np.add.at(compact_vertices, inverse, vertices)
            compact_vertices /= counts[:, None]
            compact_faces = inverse[faces]
            keep = (
                (compact_faces[:, 0] != compact_faces[:, 1])
                & (compact_faces[:, 1] != compact_faces[:, 2])
                & (compact_faces[:, 0] != compact_faces[:, 2])
            )
            compact_faces = compact_faces[keep]
            if len(compact_faces):
                _, unique_index = np.unique(
                    np.sort(compact_faces, axis=1), axis=0, return_index=True
                )
                compact_faces = compact_faces[np.sort(unique_index)]
            if not len(compact_faces):
                continue
            clustered = trimesh.Trimesh(
                vertices=compact_vertices,
                faces=compact_faces,
                process=False,
            )
            if len(compact_faces) <= target * 1.25:
                break
        return clustered if clustered is not None else source

    detailed = decimate(mesh, max(1_000, int(max_faces)))
    # Build the interaction LOD directly from the source.  Re-decimating an
    # already simplified organic mesh can accumulate errors and create the
    # exaggerated low-poly silhouette seen on dense figurines.
    interactive = decimate(mesh, min(max(500, int(interactive_faces)), len(mesh.faces)))

    vertices = detailed.vertices
    faces = detailed.faces
    if not len(vertices) or not len(faces):
        return {"vertices": [], "faces": []}
    # Both preview LODs and the G-code overlay must use one immutable coordinate
    # frame. A decimator is allowed to drop an extreme vertex, so deriving the
    # bounds from the simplified mesh can visibly shift toolpaths.
    source_min = mesh.vertices.min(axis=0)
    source_max = mesh.vertices.max(axis=0)
    center = (source_min + source_max) / 2.0
    extent = source_max - source_min
    scale = max(float(extent.max()), 1e-9) / 2.0
    normalized = (vertices - center) / scale
    interactive_normalized = (interactive.vertices - center) / scale
    return {
        "vertices": normalized.astype("float32").tolist(),
        "faces": faces.astype("int32").tolist(),
        "interactive_vertices": interactive_normalized.astype("float32").tolist(),
        "interactive_faces": interactive.faces.astype("int32").tolist(),
        "source_face_count": len(mesh.faces),
        "detail_face_count": len(faces),
        "interactive_face_count": len(interactive.faces),
        "source_bounds_mm": [
            source_min.astype(float).tolist(),
            source_max.astype(float).tolist(),
        ],
        "source_center_mm": center.astype(float).tolist(),
        "source_dimensions_mm": extent.astype(float).tolist(),
        "source_scale_mm": scale,
    }


def build_printed_preview_mesh(
    ready_project_path: Path | None,
    *,
    fallback_model_path: Path | None = None,
) -> dict[str, Any]:
    """Build a preview in the exact coordinate frame used by the final G-code."""
    if ready_project_path is not None and ready_project_path.is_file():
        try:
            with tempfile.TemporaryDirectory(prefix="vanior-final-preview-") as temporary:
                temporary_dir = Path(temporary)
                extracted = extract_printable_stl(
                    ready_project_path,
                    temporary_dir / "printed-plate.stl",
                    object_output_dir=temporary_dir / "objects",
                )
                preview = build_preview_mesh(extracted.output_stl_path)
                preview["coordinate_source"] = "ready_3mf_build"
                return preview
        except Exception:  # noqa: BLE001,S110 -- optional UI preview fallback
            # Preview generation is a non-critical UI boundary. The ready file
            # has already passed pipeline verification, so retain a usable solid
            # preview if an unusual vendor extension prevents re-extraction.
            pass
    if fallback_model_path is not None and fallback_model_path.is_file():
        preview = build_preview_mesh(fallback_model_path)
        preview["coordinate_source"] = "fallback_model"
        return preview
    return {"vertices": [], "faces": [], "coordinate_source": "unavailable"}


def read_gcode_layer_heights(gcode_path: Path | None) -> list[float]:
    """Read exact cumulative layer heights emitted by Bambu Studio."""
    if gcode_path is None or not gcode_path.is_file():
        return []
    try:
        validate_gcode_file(gcode_path)
    except UnsafeInputError:
        return []
    heights: list[float] = []
    with gcode_path.open("r", encoding="utf-8", errors="replace") as stream:
        for line in stream:
            if len(line) > MAX_TEXT_LINE_BYTES:
                continue
            match = _GCODE_Z_HEIGHT.match(line.strip())
            if not match:
                continue
            height = float(match.group(1))
            if height > 0 and (not heights or height > heights[-1]):
                heights.append(height)
    return heights


def read_gcode_layer_preview(
    gcode_path: Path | None,
    *,
    max_segments_per_layer: int = 5000,
    coordinate_mesh: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Extract every layer and role-coloured extrusion paths from slicer G-code."""
    if gcode_path is None or not gcode_path.is_file():
        return {"z_mm": [], "paths": []}
    try:
        validate_gcode_file(gcode_path)
    except UnsafeInputError:
        return {"z_mm": [], "paths": []}
    heights: list[float] = []
    paths: list[list[tuple[float, float, float, float, str]]] = []
    x = y = extrusion = 0.0
    axes_absolute = True
    extrusion_relative = True
    layer_stride = 1
    layer_seen = 0
    feature_role = "model"
    waiting_for_altitude = False
    with gcode_path.open("r", encoding="utf-8", errors="replace") as stream:
        for raw_line in stream:
            if len(raw_line) > MAX_TEXT_LINE_BYTES:
                continue
            stripped = raw_line.strip()
            feature_match = _GCODE_FEATURE.match(stripped)
            if feature_match:
                feature_role = _preview_role(feature_match.group(1))
                continue
            if stripped.casefold() in {"; change_layer", "; layer_change"} or stripped.casefold().startswith(";layer:"):
                waiting_for_altitude = True
                continue
            height_match = _GCODE_Z_HEIGHT.match(stripped)
            if height_match is None and waiting_for_altitude:
                height_match = _GCODE_ALT_Z_HEIGHT.match(stripped)
            if height_match:
                height = float(height_match.group(1))
                if height > 0 and (not heights or height > heights[-1]):
                    heights.append(height)
                    paths.append([])
                    layer_stride = 1
                    layer_seen = 0
                waiting_for_altitude = False
                continue
            command = stripped.split(";", 1)[0].strip()
            if not command:
                continue
            code = command.split(maxsplit=1)[0].upper()
            if code == "G90":
                axes_absolute = True
                continue
            if code == "G91":
                axes_absolute = False
                continue
            if code == "M82":
                extrusion_relative = False
                continue
            if code == "M83":
                extrusion_relative = True
                continue
            parameters = {key: float(value) for key, value in _GCODE_PARAMETER.findall(command)}
            if code == "G92":
                if "X" in parameters:
                    x = parameters["X"]
                if "Y" in parameters:
                    y = parameters["Y"]
                if "E" in parameters:
                    extrusion = parameters["E"]
                continue
            if code not in {"G0", "G1", "G2", "G3"}:
                continue
            new_x = parameters.get("X", 0.0) + (0.0 if axes_absolute else x) if "X" in parameters else x
            new_y = parameters.get("Y", 0.0) + (0.0 if axes_absolute else y) if "Y" in parameters else y
            deposited = 0.0
            if "E" in parameters:
                deposited = parameters["E"] if extrusion_relative else parameters["E"] - extrusion
                if not extrusion_relative:
                    extrusion = parameters["E"]
            if paths and deposited > 1e-8 and (abs(new_x - x) > 1e-8 or abs(new_y - y) > 1e-8):
                layer_seen += 1
                if layer_seen % layer_stride == 0:
                    paths[-1].append((x, y, new_x, new_y, feature_role))
                if len(paths[-1]) >= max_segments_per_layer * 2:
                    paths[-1] = paths[-1][::2]
                    layer_stride *= 2
            x, y = new_x, new_y

    all_segments = [segment for layer in paths for segment in layer]
    if not all_segments:
        return {"z_mm": heights, "paths": [[] for _ in heights]}
    # Normalize against printed model paths, not against supports or brim.
    # Otherwise a wide tree base would shrink every path into the model's own
    # footprint in the preview and visually move the supports to wrong places.
    reference_segments = [
        segment for segment in all_segments
        if segment[4] not in {"support", "support_interface", "skirt_brim"}
    ] or all_segments
    xs = [
        coordinate for segment in reference_segments
        for coordinate in (segment[0], segment[2])
    ]
    ys = [
        coordinate for segment in reference_segments
        for coordinate in (segment[1], segment[3])
    ]
    mesh_center = (coordinate_mesh or {}).get("source_center_mm")
    mesh_scale = (coordinate_mesh or {}).get("source_scale_mm")
    if (
        isinstance(mesh_center, (list, tuple))
        and len(mesh_center) == 3
        and isinstance(mesh_scale, (int, float))
        and float(mesh_scale) > 1e-9
    ):
        center_x = float(mesh_center[0])
        center_y = float(mesh_center[1])
        scale = float(mesh_scale)
    else:
        center_x = (min(xs) + max(xs)) / 2.0
        center_y = (min(ys) + max(ys)) / 2.0
        scale = max(
            max(xs) - min(xs),
            max(ys) - min(ys),
            heights[-1] if heights else 0.0,
            1e-9,
        ) / 2.0
    normalized_layers: list[list[list[float | str]]] = []
    for layer in paths:
        step = max(1, math.ceil(len(layer) / max_segments_per_layer))
        normalized_layers.append(
            [
                [
                    (left_x - center_x) / scale,
                    (left_y - center_y) / scale,
                    (right_x - center_x) / scale,
                    (right_y - center_y) / scale,
                    role,
                ]
                for left_x, left_y, right_x, right_y, role in layer[::step]
            ]
        )
    role_counts: dict[str, int] = {}
    for layer in normalized_layers:
        for segment in layer:
            role = str(segment[4])
            role_counts[role] = role_counts.get(role, 0) + 1
    return {"z_mm": heights, "paths": normalized_layers, "role_counts": role_counts}


def scan_model(
    source: Path,
    profile: Path | None,
    *,
    material: str,
    plate: int,
    nozzle_diameter_mm: float = 0.4,
    progress_callback: ProgressCallback | None,
    cancel_event: Event,
) -> dict[str, Any]:
    emit_progress(progress_callback, "validate", "Проверка выбранного файла", 5)
    check_cancelled(cancel_event)
    if source.suffix.lower() not in SUPPORTED_MODEL_SUFFIXES or not source.is_file():
        raise ValueError("Выберите существующую модель STL или 3MF.")

    profile_path = profile or (source if source.suffix.lower() == ".3mf" else None)
    profile_result = None
    if profile_path is not None:
        emit_progress(progress_callback, "profile", "Проверка настроек принтера", 15)
        profile_result = validate_bambu_profile(profile_path, expected_material=material)

    with tempfile.TemporaryDirectory(prefix="ai-print-optimizer-scan-") as temporary:
        analysis_path = source
        extracted = None
        if source.suffix.lower() == ".3mf":
            emit_progress(progress_callback, "extract", "Чтение печатных объектов 3MF", 35)
            extracted = extract_printable_stl(
                source, Path(temporary) / "printable-model.stl", plate=plate
            )
            analysis_path = extracted.output_stl_path
        check_cancelled(cancel_event)
        emit_progress(progress_callback, "analyze", "Анализ геометрии", 60)
        report = analyze_stl(
            analysis_path,
            material=material,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )
        emit_progress(progress_callback, "preview", "Подготовка облегчённого предпросмотра", 86)
        preview_mesh = build_preview_mesh(analysis_path)
        check_cancelled(cancel_event)
        payload = {
            "kind": "scan",
            "source": str(source),
            "report": report.to_dict(),
            "profile": profile_result.to_dict() if profile_result else None,
            "extracted": extracted.to_dict() if extracted else None,
            "preview_mesh": preview_mesh,
        }
    emit_progress(progress_callback, "complete", "Проверка завершена", 100)
    return payload


class JobWorker(QObject):
    progress = Signal(str, str, int)
    completed = Signal(object)
    failed = Signal(str, str)
    cancelled = Signal()
    finished = Signal()

    def __init__(self, operation: str, options: dict[str, Any], cancel_event: Event):
        super().__init__()
        self.operation = operation
        self.options = options
        self.cancel_event = cancel_event

    def _progress(self, event: ProgressEvent) -> None:
        if getattr(self, "journal", None) is not None:
            self.journal.checkpoint(event.stage, event.message, event.percent)
        self.progress.emit(event.stage, event.message, event.percent)

    @Slot()
    def run(self) -> None:
        self.journal: JobJournal | None = None
        try:
            source = Path(self.options["source"])
            journal_path = self.options.get("journal_path")
            if journal_path:
                self.journal = JobJournal(
                    journal_path,
                    str(self.options.get("job_id", Path(journal_path).stem)),
                    {
                        "operation": self.operation,
                        "source_name": source.name,
                        "output_name": Path(self.options.get("output", "")).name,
                    },
                )
            if self.operation == "scan":
                result: object = scan_model(
                    source,
                    None,
                    material=self.options["material"],
                    plate=self.options["plate"],
                    nozzle_diameter_mm=float(self.options.get("nozzle", 0.4)),
                    progress_callback=self._progress,
                    cancel_event=self.cancel_event,
                )
            elif self.operation == "optimize":
                output = Path(self.options["output"])
                with tempfile.TemporaryDirectory(
                    prefix=".ai-print-optimizer-work-",
                    dir=output.parent,
                    ignore_cleanup_errors=True,
                ) as temporary:
                    pipeline = run_pipeline(
                        source,
                        None,
                        Path(temporary) / "pipeline",
                        material=self.options["material"],
                        material_profile=self.options.get("material_profile"),
                        printer_model=self.options["printer"],
                        nozzle_diameter_mm=self.options["nozzle"],
                        bed_type=self.options["bed_type"],
                        print_priority=self.options["print_priority"],
                        model_purpose=self.options["model_purpose"],
                        functional_intent=self.options.get("functional_intent", "auto"),
                        print_setting_overrides=self.options.get("print_setting_overrides"),
                        print_dna_profile=self.options.get("print_dna_profile"),
                        quality_search=bool(self.options.get("quality_search", True)),
                        support_strategy=self.options.get("support_strategy", "auto"),
                        overhang_angle_deg=float(self.options.get("overhang_angle_deg", 45.0)),
                        executable=self.options.get("slicer") or None,
                        plate=self.options["plate"],
                        timeout_s=self.options["timeout"],
                        progress_callback=self._progress,
                        cancel_event=self.cancel_event,
                        slice_cache_dir=self.options.get("slice_cache_dir"),
                    )
                    result = self._pipeline_payload(pipeline)
                    check_cancelled(self.cancel_event)
                    final_3mf = publish_single_3mf(
                        pipeline.support_comparison.ready_project_path,
                        output,
                        source,
                    )
                    result.update(
                        output=str(output),
                        ready_3mf=str(final_3mf),
                        ready_gcode="",
                        manifest="",
                    )
                    emit_progress(
                        self._progress,
                        "publish",
                        "Опубликован один итоговый 3MF",
                        100,
                    )
            elif self.operation == "slice":
                output = Path(self.options["output"])
                with tempfile.TemporaryDirectory(
                    prefix=".ai-print-optimizer-work-",
                    dir=output.parent,
                    ignore_cleanup_errors=True,
                ) as temporary:
                    sliced = slice_3mf(
                        source,
                        Path(temporary) / "slice",
                        executable=self.options.get("slicer") or None,
                        plate=self.options["plate"],
                        timeout_s=self.options["timeout"],
                        progress_callback=self._progress,
                        cancel_event=self.cancel_event,
                    )
                    ensure_run_publishable(sliced)
                    result = self._slice_payload(sliced)
                    check_cancelled(self.cancel_event)
                    final_3mf = publish_single_3mf(
                        sliced.ready_project_path,
                        output,
                        source,
                    )
                    result.update(
                        output=str(output),
                        ready_3mf=str(final_3mf),
                        ready_gcode="",
                    )
                    emit_progress(
                        self._progress,
                        "publish",
                        "Опубликован один итоговый 3MF",
                        100,
                    )
            elif self.operation == "vanior":
                output = Path(self.options["output"])
                output.mkdir(parents=True, exist_ok=False)
                with tempfile.TemporaryDirectory(
                    prefix=".vanior-slice-geometry-",
                    dir=output.parent,
                    ignore_cleanup_errors=True,
                ) as temporary:
                    analysis_path = source
                    if source.suffix.lower() == ".3mf":
                        analysis_path = Path(temporary) / "printable-model.stl"
                        extract_printable_stl(
                            source,
                            analysis_path,
                            plate=self.options["plate"],
                        )
                    report = analyze_stl(
                        analysis_path,
                        material=self.options["material"],
                        overhang_angle_deg=float(
                            self.options.get("overhang_angle_deg", 45.0)
                        ),
                        nozzle_diameter_mm=float(self.options.get("nozzle", 0.4)),
                    )
                    repair_result = None
                    if report.health.status == "INVALID":
                        raise ValueError(
                            "Геометрия повреждена сильнее безопасного порога автоматического восстановления."
                        )
                    if report.health.status != "READY":
                        emit_progress(
                            self._progress,
                            "vanior-repair",
                            "Безопасное исправление геометрии",
                            18,
                        )
                        repaired_path = Path(temporary) / "model.repaired.stl"
                        repair_result = repair_stl(analysis_path, repaired_path)
                        analysis_path = repaired_path
                        report = analyze_stl(
                            analysis_path,
                            material=self.options["material"],
                            overhang_angle_deg=float(
                                self.options.get("overhang_angle_deg", 45.0)
                            ),
                            nozzle_diameter_mm=float(
                                self.options.get("nozzle", 0.4)
                            ),
                        )
                        if report.health.status == "INVALID":
                            raise ValueError(
                                "Автоматическое исправление не сделало геометрию безопасной."
                            )
                    check_cancelled(self.cancel_event)
                    emit_progress(
                        self._progress,
                        "vanior-orient",
                        "Сравнение безопасных положений модели",
                        25,
                    )
                    selected_purpose = select_model_purpose(
                        report.purpose,
                        str(self.options.get("model_purpose", "auto")),
                    )
                    orientation, orientation_export = orient_stl(
                        analysis_path,
                        Path(temporary) / "model.oriented.stl",
                        overhang_angle_deg=float(
                            self.options.get("overhang_angle_deg", 45.0)
                        ),
                        protect_visible_surfaces=(
                            selected_purpose == "decorative"
                            and report.purpose.curved_surface_ratio >= 0.35
                        ),
                        prefer_layer_strength=(
                            "strength"
                            in str(self.options.get("print_priority", "balanced")).split("+")
                        ),
                    )
                    analysis_path = orientation_export.output_path
                    report = analyze_stl(
                        analysis_path,
                        material=self.options["material"],
                        overhang_angle_deg=float(
                            self.options.get("overhang_angle_deg", 45.0)
                        ),
                        nozzle_diameter_mm=float(self.options.get("nozzle", 0.4)),
                    )
                    report, print_dna_application = _configure_analysis_for_print(
                        report,
                        print_priority=str(
                            self.options.get("print_priority", "balanced")
                        ),
                        model_purpose=str(
                            self.options.get("model_purpose", "auto")
                        ),
                        functional_intent=str(
                            self.options.get("functional_intent", "auto")
                        ),
                        print_setting_overrides=(
                            self.options.get("print_setting_overrides") or None
                        ),
                        print_dna_profile=self.options.get("print_dna_profile"),
                        material_profile=self.options.get("material_profile"),
                        nozzle_diameter_mm=float(self.options.get("nozzle", 0.4)),
                    )
                    settings = report.settings
                    quality_optimization = None
                    if bool(self.options.get("quality_search", True)):
                        overrides = self.options.get("print_setting_overrides") or {}
                        plan = build_optimization_plan(
                            report,
                            allow_setting_changes=True,
                            nozzle_diameter_mm=float(
                                self.options.get("nozzle", 0.4)
                            ),
                            locked_fields=frozenset(str(key) for key in overrides),
                            material_profile=self.options.get("material_profile"),
                        )
                        candidate, quality_optimization = (
                            select_independent_optimization_candidate(plan)
                        )
                        settings = candidate.settings
                        report = replace(report, settings=settings)
                    check_cancelled(self.cancel_event)
                    gcode_path = output / f"{source.stem}-vanior.gcode"
                    sliced = slice_stl_to_gcode(
                        analysis_path,
                        gcode_path,
                        settings,
                        material=self.options["material"],
                        nozzle_diameter_mm=float(self.options.get("nozzle", 0.4)),
                        overhang_angle_deg=float(
                            self.options.get("overhang_angle_deg", 45.0)
                        ),
                        support_strategy=str(
                            self.options.get("support_strategy", "auto")
                        ),
                        support_exit_risk=(
                            report.support_exit_plan.overall_risk
                            if report.support_exit_plan is not None
                            else "UNKNOWN"
                        ),
                        support_accessibility_score=(
                            report.support_exit_plan.accessibility_score
                            if report.support_exit_plan is not None
                            else 100.0
                        ),
                        progress_callback=self._progress,
                        cancel_event=self.cancel_event,
                    )
                    effective_settings = replace(
                        settings, supports=sliced.support_strategy != "none"
                    )
                    print_package = create_vanior_gcode_3mf(
                        analysis_path,
                        sliced.gcode_path,
                        output / f"{source.stem}-vanior.gcode.3mf",
                        effective_settings,
                        sliced,
                        material=self.options["material"],
                    )
                    preview_mesh = build_preview_mesh(analysis_path)
                    layer_preview = read_gcode_layer_preview(
                        sliced.gcode_path,
                        coordinate_mesh=preview_mesh,
                    )
                result = {
                    "kind": "vanior-slice",
                    "engine": sliced.engine,
                    "engine_stage": sliced.engine_stage,
                    "source": str(source),
                    "output": str(output),
                    "status": report.health.status,
                    "dimensions": report.metrics.dimensions_mm,
                    "preview_mesh": preview_mesh,
                    "preview_caption": gcode_path.name,
                    "strategy": sliced.support_strategy,
                    "reason": sliced.support_recommendation_reason,
                    "support_comparison": list(sliced.support_candidates),
                    "support_mass": sliced.estimated_support_mass_g,
                    "support_time": sliced.estimated_support_time_s,
                    "print_time": sliced.estimated_print_time_s,
                    "mass": sliced.estimated_mass_g,
                    "ready_3mf": str(print_package),
                    "ready_gcode": str(sliced.gcode_path),
                    "manifest": "",
                    "printer": "Bambu Lab P1S (профиль VANIOR)",
                    "repair": repair_result.to_dict() if repair_result else None,
                    "orientation": orientation.to_dict(),
                    "orientation_export": orientation_export.to_dict(),
                    "print_priority": settings.priority,
                    "model_purpose": settings.model_purpose,
                    "functional_intent": report.functional_intent.to_dict(),
                    "surface_intelligence": report.surface_intelligence.to_dict(),
                    "geometry_features": report.geometry_features.to_dict(),
                    "support_exit_plan": report.support_exit_plan.to_dict(),
                    "local_modifier_plan": report.local_modifier_plan.to_dict(),
                    "object_optimizations": [],
                    "gcode_audit": sliced.audit.to_dict(),
                    "print_dna_application": (
                        print_dna_application.to_dict()
                        if print_dna_application is not None
                        else None
                    ),
                    "quality_optimization": (
                        {
                            **quality_optimization,
                            "selected_time_s": sliced.estimated_print_time_s,
                            "selected_material_g": sliced.estimated_mass_g,
                        }
                        if quality_optimization is not None
                        else None
                    ),
                    "layer_z_mm": layer_preview["z_mm"],
                    "layer_paths": layer_preview["paths"],
                    "layer_role_counts": layer_preview.get("role_counts", {}),
                }
            else:
                raise ValueError(f"Неизвестный режим: {self.operation}")
            if isinstance(result, dict) and self.options.get("request_fingerprint"):
                result["request_fingerprint"] = str(self.options["request_fingerprint"])
            if self.journal is not None:
                self.journal.finish(manifest=str(result.get("manifest", "")) if isinstance(result, dict) else None)
            self.completed.emit(result)
        except OperationCancelled:
            if self.journal is not None:
                self.journal.finish(error="Операция отменена пользователем")
            self.cancelled.emit()
        except Exception as exc:  # noqa: BLE001 -- GUI boundary preserves full diagnostics
            if self.journal is not None:
                self.journal.finish(error=str(exc))
            self.failed.emit(str(exc), traceback.format_exc())
        finally:
            self.finished.emit()

    @staticmethod
    def _pipeline_payload(result: PipelineResult) -> dict[str, Any]:
        comparison = result.support_comparison
        selected = comparison.selected_run
        if selected is None and comparison.recommended in {"none", "normal", "tree"}:
            selected = getattr(comparison, comparison.recommended)
        optimization = comparison.quality_optimization
        preview_mesh = build_printed_preview_mesh(
            comparison.ready_project_path,
            fallback_model_path=result.orientation_export.output_path,
        )
        layer_preview = read_gcode_layer_preview(
            comparison.ready_gcode_path,
            coordinate_mesh=preview_mesh,
        )
        final_audit = (
            audit_gcode(
                comparison.ready_gcode_path,
                maximum_volumetric_speed_mm3_s=(
                    result.final_analysis.settings.max_volumetric_speed_mm3_s
                ),
            ).to_dict()
            if comparison.ready_gcode_path
            else None
        )
        return {
            "kind": "pipeline",
            "source": str(result.source_path),
            "output": str(result.output_dir),
            "status": result.final_analysis.health.status,
            "dimensions": result.final_analysis.metrics.dimensions_mm,
            "preview_mesh": preview_mesh,
            "preview_caption": (
                comparison.ready_project_path.name
                if comparison.ready_project_path
                else result.orientation_export.output_path.name
            ),
            "orientation": result.orientation.to_dict(),
            "orientation_export": result.orientation_export.to_dict(),
            "strategy": comparison.recommended,
            "reason": comparison.recommendation_reason,
            "print_time": (
                optimization.selected_time_s
                if optimization
                else selected.total_print_time_s if selected else None
            ),
            "mass": (
                optimization.selected_material_g
                if optimization
                else selected.total_used_g if selected else None
            ),
            "ready_3mf": str(comparison.ready_project_path or ""),
            "ready_gcode": str(comparison.ready_gcode_path or ""),
            "manifest": str(result.manifest_path),
            "printer": result.profile_validation.printer_model,
            "profile_setting_count": (
                result.generated_profile.setting_count
                if result.generated_profile is not None
                else None
            ),
            "source_profile_assessment": (
                result.source_profile_assessment.to_dict()
                if result.source_profile_assessment
                else None
            ),
            "print_priority": result.final_analysis.settings.priority,
            "model_purpose": result.final_analysis.settings.model_purpose,
            "automatic_model_classification": result.final_analysis.purpose.classification,
            "purpose_confidence": result.final_analysis.purpose.confidence,
            "surface_detail_profile": (
                result.final_analysis.settings.surface_detail_profile
            ),
            "surface_intelligence": result.final_analysis.surface_intelligence.to_dict(),
            "functional_intent": (
                result.final_analysis.functional_intent.to_dict()
                if result.final_analysis.functional_intent
                else None
            ),
            "geometry_features": (
                result.final_analysis.geometry_features.to_dict()
                if result.final_analysis.geometry_features
                else None
            ),
            "support_exit_plan": (
                result.final_analysis.support_exit_plan.to_dict()
                if result.final_analysis.support_exit_plan
                else None
            ),
            "local_modifier_plan": (
                result.final_analysis.local_modifier_plan.to_dict()
                if result.final_analysis.local_modifier_plan
                else None
            ),
            "object_optimizations": [
                item.to_dict() for item in result.object_optimizations
            ],
            "gcode_audit": final_audit,
            "print_dna_application": (
                result.print_dna_application.to_dict()
                if result.print_dna_application
                else None
            ),
            "quality_optimization": (
                optimization.to_dict() if optimization else None
            ),
            "layer_z_mm": layer_preview["z_mm"],
            "layer_paths": layer_preview["paths"],
            "layer_role_counts": layer_preview.get("role_counts", {}),
        }

    @staticmethod
    def _slice_payload(result: SliceRunResult) -> dict[str, Any]:
        gcode_path = result.gcode_files[0] if len(result.gcode_files) == 1 else None
        fallback = result.source_path if result.source_path.suffix.lower() == ".stl" else None
        preview_mesh = build_printed_preview_mesh(
            result.ready_project_path,
            fallback_model_path=fallback,
        )
        layer_preview = read_gcode_layer_preview(
            gcode_path,
            coordinate_mesh=preview_mesh,
        )
        return {
            "kind": "slice",
            "source": str(result.source_path),
            "output": str(result.output_dir),
            "print_time": result.total_print_time_s,
            "mass": result.total_used_g,
            "ready_3mf": str(result.ready_project_path or ""),
            "ready_gcode": str(gcode_path or ""),
            "preview_mesh": preview_mesh,
            "preview_caption": (
                result.ready_project_path.name
                if result.ready_project_path
                else result.source_path.name
            ),
            "layer_z_mm": layer_preview["z_mm"],
            "layer_paths": layer_preview["paths"],
            "layer_role_counts": layer_preview.get("role_counts", {}),
        }


class DropLineEdit(QLineEdit):
    fileDropped = Signal(str)

    def __init__(self, suffixes: set[str], parent: QWidget | None = None):
        super().__init__(parent)
        self.suffixes = suffixes
        self.setAcceptDrops(True)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and Path(urls[0].toLocalFile()).suffix.lower() in self.suffixes:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        path = event.mimeData().urls()[0].toLocalFile()
        self.setText(path)
        self.fileDropped.emit(path)
        event.acceptProposedAction()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings("BigVanior", APP_NAME)
        self.thread: QThread | None = None
        self.worker: JobWorker | None = None
        self.cancel_event: Event | None = None
        self.last_result: dict[str, Any] | None = None
        self.close_when_finished = False
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.resize(980, 900)
        self.setMinimumSize(820, 850)
        self.setAcceptDrops(True)
        self._build_ui()
        self._apply_style()
        self._restore_settings()
        self._mode_changed()

    def _build_ui(self) -> None:
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        root = QWidget()
        root.setObjectName("contentRoot")
        self.scroll_area.setWidget(root)
        self.setCentralWidget(self.scroll_area)
        layout = QVBoxLayout(root)
        layout.setSizeConstraint(QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setSpacing(16)

        title = QLabel(APP_NAME)
        title.setObjectName("title")
        subtitle = QLabel("Проверка модели, подбор настроек и готовый файл для Bambu Studio")
        subtitle.setObjectName("subtitle")
        layout.addWidget(title)
        layout.addWidget(subtitle)

        card = QFrame()
        card.setObjectName("card")
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(22, 20, 22, 20)
        card_layout.setSpacing(13)

        form = QFormLayout()
        form.setHorizontalSpacing(18)
        form.setVerticalSpacing(12)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)

        self.model_edit = DropLineEdit(SUPPORTED_MODEL_SUFFIXES)
        self.model_edit.setPlaceholderText("Перетащите сюда STL или 3MF")
        self.model_edit.fileDropped.connect(self._model_selected)
        form.addRow("Модель", self._path_row(self.model_edit, self._browse_model))

        self.mode_combo = QComboBox()
        self.mode_combo.addItem("Полная оптимизация и один готовый 3MF", "optimize")
        self.mode_combo.addItem("Только проверить модель", "scan")
        self.mode_combo.addItem("Нарезать исходный 3MF без исправления", "slice")
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Действие", self.mode_combo)

        self.priority_combo = QComboBox()
        self.priority_combo.addItem("Баланс качества и скорости", "balanced")
        self.priority_combo.addItem("Максимальное качество", "quality")
        self.priority_combo.addItem("Максимальная прочность", "strength")
        self.priority_combo.addItem("Быстрая печать", "fast")
        self.priority_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Приоритет", self.priority_combo)

        self.purpose_combo = QComboBox()
        self.purpose_combo.addItem("Определить автоматически", "auto")
        self.purpose_combo.addItem("Декоративная модель", "decorative")
        self.purpose_combo.addItem("Нагруженная деталь", "functional")
        self.purpose_combo.currentIndexChanged.connect(self._mode_changed)
        form.addRow("Назначение", self.purpose_combo)

        printer_row = QWidget()
        printer_layout = QHBoxLayout(printer_row)
        printer_layout.setContentsMargins(0, 0, 0, 0)
        self.printer_combo = QComboBox()
        self.printer_combo.addItem("Bambu Lab P1S", "Bambu Lab P1S")
        self.nozzle_combo = QComboBox()
        self.nozzle_combo.addItem("Сопло 0,4 мм", 0.4)
        self.bed_combo = QComboBox()
        self.bed_combo.addItem("Текстурированная PEI", "Textured PEI Plate")
        printer_layout.addWidget(self.printer_combo, 2)
        printer_layout.addWidget(self.nozzle_combo, 1)
        printer_layout.addWidget(self.bed_combo, 2)
        form.addRow("Принтер", printer_row)

        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("Новая папка с одним итоговым 3MF")
        form.addRow("Результат", self._path_row(self.output_edit, self._browse_output))

        choice_row = QWidget()
        choice_layout = QHBoxLayout(choice_row)
        choice_layout.setContentsMargins(0, 0, 0, 0)
        self.material_combo = QComboBox()
        self.material_combo.addItems(["PLA", "PETG"])
        self.plate_spin = QSpinBox()
        self.plate_spin.setRange(1, 99)
        self.plate_spin.setValue(1)
        self.plate_spin.setPrefix("Пластина ")
        self.plate_spin.setMinimumWidth(150)
        choice_layout.addWidget(self.material_combo, 1)
        choice_layout.addWidget(self.plate_spin, 1)
        form.addRow("Печать", choice_row)

        self.slicer_edit = QLineEdit()
        self.slicer_edit.setPlaceholderText("Определить автоматически")
        form.addRow("Bambu Studio", self._path_row(self.slicer_edit, self._browse_slicer))

        timeout_row = QWidget()
        timeout_layout = QHBoxLayout(timeout_row)
        timeout_layout.setContentsMargins(0, 0, 0, 0)
        self.timeout_spin = QSpinBox()
        self.timeout_spin.setRange(30, 3600)
        self.timeout_spin.setValue(600)
        self.timeout_spin.setSuffix(" сек")
        self.timeout_spin.setMinimumWidth(120)
        timeout_layout.addWidget(self.timeout_spin)
        timeout_layout.addStretch(1)
        form.addRow("Ожидание", timeout_row)

        card_layout.addLayout(form)
        self.mode_hint = QLabel()
        self.mode_hint.setWordWrap(True)
        self.mode_hint.setObjectName("hint")
        card_layout.addWidget(self.mode_hint)

        button_row = QHBoxLayout()
        self.run_button = QPushButton("Начать обработку")
        self.run_button.setObjectName("primary")
        self.run_button.clicked.connect(self._start_job)
        self.cancel_button = QPushButton("Отменить")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self._cancel_job)
        button_row.addWidget(self.run_button)
        button_row.addWidget(self.cancel_button)
        button_row.addStretch(1)
        card_layout.addLayout(button_row)
        layout.addWidget(card)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.status_label = QLabel("Выберите модель для начала")
        self.status_label.setObjectName("status")
        layout.addWidget(self.status_label)
        layout.addWidget(self.progress_bar)

        self.result_stack = QStackedWidget()
        self.empty_result = QLabel("Здесь появится результат проверки или обработки.")
        self.empty_result.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_result.setObjectName("empty")
        self.result_stack.addWidget(self.empty_result)

        result_page = QFrame()
        result_page.setObjectName("resultCard")
        result_layout = QVBoxLayout(result_page)
        result_layout.setContentsMargins(20, 16, 20, 16)
        self.result_title = QLabel("Готово")
        self.result_title.setObjectName("resultTitle")
        self.result_text = QLabel()
        self.result_text.setWordWrap(True)
        self.result_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        result_layout.addWidget(self.result_title)
        result_layout.addWidget(self.result_text)
        result_buttons = QHBoxLayout()
        self.open_3mf_button = QPushButton("Открыть 3MF в Bambu Studio")
        self.open_3mf_button.clicked.connect(self._open_ready_3mf)
        self.open_gcode_button = QPushButton("Показать G-code")
        self.open_gcode_button.clicked.connect(self._show_ready_gcode)
        self.open_folder_button = QPushButton("Открыть папку")
        self.open_folder_button.clicked.connect(self._open_result_folder)
        result_buttons.addWidget(self.open_3mf_button)
        result_buttons.addWidget(self.open_gcode_button)
        result_buttons.addWidget(self.open_folder_button)
        result_buttons.addStretch(1)
        result_layout.addLayout(result_buttons)
        self.result_stack.addWidget(result_page)
        self.result_stack.setMinimumHeight(190)
        layout.addWidget(self.result_stack)

        self.log_edit = QTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMinimumHeight(100)
        self.log_edit.setMaximumHeight(130)
        self.log_edit.setPlaceholderText("Ход работы")
        layout.addWidget(self.log_edit)

        for control in (
            self.model_edit,
            self.mode_combo,
            self.priority_combo,
            self.purpose_combo,
            self.printer_combo,
            self.nozzle_combo,
            self.bed_combo,
            self.output_edit,
            self.material_combo,
            self.plate_spin,
            self.slicer_edit,
            self.timeout_spin,
        ):
            control.setMinimumHeight(30)

    def _path_row(self, edit: QLineEdit, callback: Any) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        browse = QPushButton("Обзор…")
        browse.setMinimumWidth(88)
        browse.clicked.connect(callback)
        layout.addWidget(edit, 1)
        layout.addWidget(browse)
        return row

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget, QScrollArea { background: #111827; color: #e5e7eb; }
            QLabel#title { font-size: 27px; font-weight: 700; color: #f9fafb; }
            QLabel#subtitle { color: #9ca3af; font-size: 13px; }
            QFrame#card, QFrame#resultCard { background: #1f2937; border: 1px solid #374151; border-radius: 12px; }
            QLineEdit, QComboBox, QSpinBox, QTextEdit {
                background: #111827; border: 1px solid #4b5563; border-radius: 7px;
                color: #e5e7eb; padding: 3px 8px;
                selection-background-color: #2563eb;
            }
            QLineEdit, QComboBox, QSpinBox { min-height: 24px; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus { border: 1px solid #60a5fa; }
            QPushButton { color: #e5e7eb; background: #374151; border: 1px solid #4b5563; border-radius: 7px; padding: 5px 13px; }
            QPushButton:hover { background: #4b5563; }
            QPushButton:disabled { color: #6b7280; background: #1f2937; }
            QPushButton#primary { background: #2563eb; border-color: #3b82f6; font-weight: 600; padding: 10px 18px; }
            QPushButton#primary:hover { background: #1d4ed8; }
            QLabel#hint { color: #9ca3af; padding: 4px; }
            QLabel#status { color: #bfdbfe; font-weight: 600; }
            QLabel#empty { color: #6b7280; border: 1px dashed #374151; border-radius: 10px; }
            QLabel#resultTitle { color: #86efac; font-size: 18px; font-weight: 700; }
            QProgressBar { background: #1f2937; border: 1px solid #374151; border-radius: 6px; text-align: center; height: 18px; }
            QProgressBar::chunk { background: #3b82f6; border-radius: 5px; }
            QScrollBar:vertical { background: #111827; width: 12px; margin: 0; }
            QScrollBar::handle:vertical { background: #4b5563; border-radius: 6px; min-height: 32px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            """
        )

    def _restore_settings(self) -> None:
        self.slicer_edit.setText(self.settings.value("slicer", "", str))
        self.material_combo.setCurrentText(self.settings.value("material", "PLA", str))
        saved_priority = self.settings.value("print_priority", "balanced", str)
        priority_index = self.priority_combo.findData(saved_priority)
        self.priority_combo.setCurrentIndex(max(0, priority_index))
        saved_purpose = self.settings.value("model_purpose", "auto", str)
        purpose_index = self.purpose_combo.findData(saved_purpose)
        self.purpose_combo.setCurrentIndex(max(0, purpose_index))
        self.timeout_spin.setValue(self.settings.value("timeout", 600, int))
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)

    def _save_settings(self) -> None:
        self.settings.setValue("slicer", self.slicer_edit.text().strip())
        self.settings.setValue("material", self.material_combo.currentText())
        self.settings.setValue("print_priority", self.priority_combo.currentData())
        self.settings.setValue("model_purpose", self.purpose_combo.currentData())
        self.settings.setValue("timeout", self.timeout_spin.value())
        self.settings.setValue("geometry", self.saveGeometry())

    def _browse_model(self) -> None:
        start = self.settings.value("model_dir", str(Path.home()), str)
        path, _ = QFileDialog.getOpenFileName(self, "Выберите модель", start, "3D-модели (*.stl *.3mf)")
        if path:
            self.model_edit.setText(path)
            self._model_selected(path)

    def _model_selected(self, path: str) -> None:
        source = Path(path)
        self.settings.setValue("model_dir", str(source.parent))
        self.output_edit.setText(str(suggest_output_path(source)))
        self._mode_changed()

    def _browse_output(self) -> None:
        model = Path(self.model_edit.text().strip()) if self.model_edit.text().strip() else Path.home()
        parent = QFileDialog.getExistingDirectory(self, "Выберите родительскую папку результата", str(model.parent))
        if parent:
            name = f"{model.stem}-result" if model.name else "ai-print-result"
            candidate = Path(parent) / name
            if candidate.exists():
                candidate = suggest_output_path(Path(parent) / f"{model.stem}.stl")
            self.output_edit.setText(str(candidate))

    def _browse_slicer(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, "Выберите Bambu Studio", "C:/Program Files/Bambu Studio", "Bambu Studio (bambu-studio.exe)"
        )
        if path:
            self.slicer_edit.setText(path)

    def _mode_changed(self) -> None:
        operation = self.mode_combo.currentData()
        is_scan = operation == "scan"
        self.output_edit.setEnabled(not is_scan)
        self.priority_combo.setEnabled(operation == "optimize")
        self.purpose_combo.setEnabled(operation == "optimize")
        self.run_button.setText(
            {"scan": "Проверить модель", "optimize": "Оптимизировать и нарезать", "slice": "Создать один готовый 3MF"}[operation]
        )
        if operation == "scan":
            hint = "Проверяет геометрию без изменения исходного файла. Папка результата не создаётся."
        elif operation == "slice":
            hint = "Только для 3MF: сохраняет исходную геометрию и использует уже записанные в проекте настройки."
        else:
            priority_hints = {
                "quality": "слой 0,12 мм, усиленные оболочки и сниженные скорости",
                "balanced": "слой по геометрии модели и стандартная динамика P1S",
                "fast": "слой 0,24 мм и ускоренная внутренняя печать",
            }
            priority_hint = priority_hints.get(
                str(self.priority_combo.currentData()), "сбалансированные настройки"
            )
            purpose_hints = {
                "auto": "назначение будет определено по форме модели",
                "decorative": "красивые поверхности и экономичные силовые параметры",
                "functional": "те же красивые поверхности плюс усиленные стенки и заполнение",
            }
            purpose_hint = purpose_hints.get(
                str(self.purpose_combo.currentData()), "автоматическое назначение"
            )
            hint = (
                "Профиль добавлять не нужно: приложение создаст чистые настройки "
                "из официальных пресетов установленного Bambu Studio. "
                f"Выбранный режим: {priority_hint}; {purpose_hint}. "
                "В папке результата останется один готовый 3MF со встроенным G-code."
            )
        self.mode_hint.setText(hint)

    def _validate_inputs(self) -> dict[str, Any] | None:
        operation = self.mode_combo.currentData()
        source = Path(self.model_edit.text().strip()).expanduser()
        if not source.is_file() or source.suffix.lower() not in SUPPORTED_MODEL_SUFFIXES:
            self._show_error("Выберите существующий файл STL или 3MF.")
            return None
        if operation == "slice" and source.suffix.lower() != ".3mf":
            self._show_error("Прямая нарезка доступна только для проекта 3MF.")
            return None
        output_text = self.output_edit.text().strip()
        if operation != "scan":
            output = Path(output_text).expanduser()
            if not output_text:
                self._show_error("Укажите новую папку для результата.")
                return None
            if output.exists():
                self._show_error("Папка результата уже существует. Выберите новое имя — приложение не перезаписывает результаты.")
                return None
            if not output.parent.is_dir():
                self._show_error("Родительская папка результата не существует.")
                return None
        slicer_text = self.slicer_edit.text().strip()
        if slicer_text and not Path(slicer_text).is_file():
            self._show_error("Указанный файл Bambu Studio не найден.")
            return None
        return {
            "source": str(source.resolve()),
            "output": str(Path(output_text).resolve()) if output_text else "",
            "material": self.material_combo.currentText(),
            "printer": str(self.printer_combo.currentData()),
            "nozzle": float(self.nozzle_combo.currentData()),
            "bed_type": str(self.bed_combo.currentData()),
            "print_priority": str(self.priority_combo.currentData()),
            "model_purpose": str(self.purpose_combo.currentData()),
            "plate": self.plate_spin.value(),
            "slicer": str(Path(slicer_text).resolve()) if slicer_text else "",
            "timeout": float(self.timeout_spin.value()),
        }

    def _start_job(self) -> None:
        if self.thread is not None:
            return
        options = self._validate_inputs()
        if options is None:
            return
        self._save_settings()
        operation = self.mode_combo.currentData()
        self.cancel_event = Event()
        self.thread = QThread(self)
        self.worker = JobWorker(operation, options, self.cancel_event)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._on_progress)
        self.worker.completed.connect(self._on_completed)
        self.worker.failed.connect(self._on_failed)
        self.worker.cancelled.connect(self._on_cancelled)
        self.worker.finished.connect(self.thread.quit)
        self.worker.finished.connect(self.worker.deleteLater)
        self.thread.finished.connect(self._job_finished)
        self.thread.finished.connect(self.thread.deleteLater)
        self.run_button.setEnabled(False)
        self.cancel_button.setEnabled(True)
        self.mode_combo.setEnabled(False)
        self.progress_bar.setValue(0)
        self.log_edit.clear()
        self.result_stack.setCurrentIndex(0)
        self.status_label.setText("Запуск…")
        self.scroll_area.verticalScrollBar().setValue(0)
        self.thread.start()

    @Slot(str, str, int)
    def _on_progress(self, stage: str, message: str, percent: int) -> None:
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)
        self.log_edit.append(f"{percent:3d}%  {message}")

    @Slot(object)
    def _on_completed(self, payload: object) -> None:
        self.last_result = dict(payload)  # type: ignore[arg-type]
        self.progress_bar.setValue(100)
        self.status_label.setText("Операция успешно завершена")
        self._render_result(self.last_result)
        QTimer.singleShot(
            0,
            lambda: self.scroll_area.ensureWidgetVisible(self.result_stack, 20, 20),
        )

    @Slot(str, str)
    def _on_failed(self, message: str, details: str) -> None:
        self.status_label.setText("Операция остановлена из-за ошибки")
        self.log_edit.append(details)
        friendly = self._friendly_error(message)
        self._show_error(friendly, details)

    @Slot()
    def _on_cancelled(self) -> None:
        self.status_label.setText("Операция отменена. Частичные файлы не являются готовым результатом.")
        self.log_edit.append("Отменено пользователем")

    @Slot()
    def _job_finished(self) -> None:
        self.thread = None
        self.worker = None
        self.cancel_event = None
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self.mode_combo.setEnabled(True)
        if self.close_when_finished:
            self.close_when_finished = False
            QTimer.singleShot(0, self.close)

    def _cancel_job(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Останавливаю безопасно…")

    def _render_result(self, payload: dict[str, Any]) -> None:
        kind = payload["kind"]
        if kind == "scan":
            report = payload["report"]
            health = report["health"]
            metrics = report["metrics"]
            dimensions = " × ".join(f"{value:.1f}" for value in metrics["dimensions_mm"])
            profile = payload.get("profile")
            profile_line = ""
            if profile:
                profile_line = f"<br>Профиль: {'подходит' if profile['valid'] else 'не подходит'} — {profile['printer_model']}"
            purpose = report.get("purpose", {})
            purpose_names = {
                "decorative": "декоративная",
                "functional": "нагруженная",
                "ambiguous": "неоднозначная",
            }
            confidence_names = {"HIGH": "высокая", "MEDIUM": "средняя", "LOW": "низкая"}
            purpose_line = (
                f"<br>Тип модели: {purpose_names.get(purpose.get('classification'), purpose.get('classification', 'не определён'))}"
                f" ({confidence_names.get(purpose.get('confidence'), 'низкая')} уверенность)"
            )
            warnings = report.get("warnings", [])
            warning_line = f"<br>Предупреждения: {len(warnings)}" if warnings else "<br>Предупреждений нет"
            self.result_title.setText("Проверка завершена")
            self.result_text.setText(
                f"Состояние: <b>{_health_text(health['status'])}</b><br>"
                f"Размер: {dimensions} мм<br>Треугольников: {metrics['triangle_count']:,}<br>"
                f"Помещается в принтер: {'да' if report['fits_build_volume'] else 'нет'}"
                f"{purpose_line}{profile_line}{warning_line}"
            )
            self.open_3mf_button.setVisible(False)
            self.open_gcode_button.setVisible(False)
            self.open_folder_button.setVisible(False)
        else:
            strategy_names = {"none": "без поддержек", "normal": "обычные поддержки", "tree": "древовидные поддержки"}
            strategy = payload.get("strategy")
            lines = []
            if strategy:
                lines.append(f"Выбранный вариант: <b>{strategy_names.get(strategy, strategy)}</b>")
            comparison = payload.get("support_comparison") or []
            if comparison:
                lines.append("Проверены варианты: без поддержек, обычные и древовидные")
            if payload.get("print_time") is not None:
                lines.append(f"Расчётное время: {_format_duration(float(payload['print_time']))}")
            if payload.get("mass") is not None:
                lines.append(f"Материал: {float(payload['mass']):.1f} г")
            if payload.get("profile_setting_count") is not None:
                lines.append(
                    f"Профиль: {payload.get('printer', 'Bambu Lab P1S')}, "
                    f"параметров {int(payload['profile_setting_count'])}"
                )
            priority_names = {
                "quality": "максимальное качество",
                "balanced": "баланс качества и скорости",
                "fast": "быстрая печать",
            }
            if payload.get("print_priority"):
                lines.append(
                    "Режим: "
                    + priority_names.get(
                        str(payload["print_priority"]),
                        str(payload["print_priority"]),
                    )
                )
            purpose_names = {
                "decorative": "декоративная модель",
                "functional": "нагруженная деталь",
            }
            if payload.get("model_purpose"):
                lines.append(
                    "Назначение: "
                    + purpose_names.get(
                        str(payload["model_purpose"]),
                        str(payload["model_purpose"]),
                    )
                )
            if payload.get("surface_detail_profile") == "small-curved":
                lines.append("Поверхность: высокая детализация малой криволинейной модели")
            if payload.get("reason"):
                lines.append(str(payload["reason"]))
            self.result_title.setText("Итоговый 3MF готов к печати")
            self.result_text.setText("<br>".join(lines))
            self.open_3mf_button.setVisible(bool(payload.get("ready_3mf")))
            self.open_gcode_button.setVisible(bool(payload.get("ready_gcode")))
            self.open_folder_button.setVisible(True)
        self.result_stack.setCurrentIndex(1)

    def _open_ready_3mf(self) -> None:
        if not self.last_result:
            return
        path = Path(self.last_result.get("ready_3mf", ""))
        if not path.is_file():
            self._show_error("Готовый 3MF не найден.")
            return
        try:
            slicer = discover_bambu_studio(self.slicer_edit.text().strip() or None)
            subprocess.Popen([str(slicer), str(path)], close_fds=True)
        except Exception as exc:  # noqa: BLE001 -- legacy desktop shell boundary
            self._show_error(f"Не удалось открыть Bambu Studio: {exc}")

    def _show_ready_gcode(self) -> None:
        if self.last_result:
            self._select_in_explorer(Path(self.last_result.get("ready_gcode", "")))

    def _open_result_folder(self) -> None:
        if not self.last_result:
            return
        output = Path(self.last_result.get("output", ""))
        if output.is_dir():
            os.startfile(output)  # type: ignore[attr-defined]

    @staticmethod
    def _select_in_explorer(path: Path) -> None:
        if path.is_file():
            subprocess.Popen(["explorer.exe", "/select,", str(path)])

    @staticmethod
    def _friendly_error(message: str) -> str:
        translations = (
            ("requires manual repair", "Модель требует ручного исправления геометрии. Безопасная оптимизация остановлена до нарезки. Можно исправить модель либо явно выбрать режим «Нарезать исходный 3MF»."),
            ("profile validation failed", "Профиль принтера не подходит выбранной модели или материалу."),
            ("Bambu Studio executable not found", "Bambu Studio не найден. Установите его или укажите bambu-studio.exe в поле приложения."),
            ("does not fit", "Модель не помещается в рабочую область принтера."),
            ("output already exists", "Папка результата уже существует. Выберите новое имя."),
        )
        lower = message.lower()
        for needle, translated in translations:
            if needle.lower() in lower:
                return translated + f"\n\nТехническая причина: {message}"
        return message or "Неизвестная ошибка обработки."

    def _show_error(self, message: str, details: str = "") -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(APP_NAME)
        box.setText(message)
        if details:
            box.setDetailedText(details)
        box.exec()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and Path(urls[0].toLocalFile()).suffix.lower() in SUPPORTED_MODEL_SUFFIXES:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        path = event.mimeData().urls()[0].toLocalFile()
        self.model_edit.setText(path)
        self._model_selected(path)
        event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.thread is not None:
            answer = QMessageBox.question(
                self,
                "Идёт обработка",
                "Отменить текущую обработку и закрыть приложение?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            if self.cancel_event is not None:
                self.cancel_event.set()
            self.close_when_finished = True
            self.status_label.setText("Останавливаю безопасно перед закрытием…")
            event.ignore()
            return
        self._save_settings()
        event.accept()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--self-test-model", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--self-test-result", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test_model:
        if args.self_test_result is None:
            return 2
        try:
            result = scan_model(
                args.self_test_model.expanduser().resolve(),
                None,
                material="PLA",
                plate=1,
                progress_callback=None,
                cancel_event=Event(),
            )
            args.self_test_result.write_text(
                json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return 0
        except Exception:  # noqa: BLE001 -- top-level self-test crash capture
            args.self_test_result.write_text(traceback.format_exc(), encoding="utf-8")
            return 1
    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    window = MainWindow()
    window.show()
    if args.screenshot:
        destination = args.screenshot.expanduser().resolve()

        def save_screenshot() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(destination))
            app.quit()

        QTimer.singleShot(400, save_screenshot)
    elif args.smoke_test:
        QTimer.singleShot(250, app.quit)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
