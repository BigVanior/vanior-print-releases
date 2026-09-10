"""VANIOR PRINT desktop workspace.

The UI is intentionally project-oriented: a model is loaded once and then moves
through analysis, print settings, optimization, preview, and export without the
user having to re-select files on every screen.
"""

# GUI callbacks, worker entry points and self-tests deliberately catch arbitrary
# third-party/driver failures so they can be shown to the user instead of
# terminating the process.
# ruff: noqa: BLE001

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import subprocess
import sys
import time
import traceback
import zipfile
from array import array
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from threading import Event
from typing import Any

from PySide6.QtCore import (
    QEasingCurve,
    QParallelAnimationGroup,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRectF,
    QSettings,
    QSize,
    QStandardPaths,
    Qt,
    QThread,
    QTimer,
    QUrl,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QDragEnterEvent,
    QDropEvent,
    QFont,
    QIcon,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QPolygonF,
    QSurfaceFormat,
)
from PySide6.QtOpenGL import QOpenGLBuffer, QOpenGLShader, QOpenGLShaderProgram
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsOpacityEffect,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QTabWidget,
    QTextEdit,
    QToolTip,
    QVBoxLayout,
    QWidget,
)

from .analyzer import analyze_stl
from .gui import (
    SUPPORTED_MODEL_SUFFIXES,
    JobWorker,
    _format_duration,
    _health_text,
    build_preview_mesh,
    scan_model,
)
from .gui import (
    suggest_output_path as _suggest_output_path,
)
from .learning_sync import sync_community_learning
from .print_dna import (
    PrintDNAKey,
    PrintFeedback,
    load_combined_print_dna_profile,
    record_print_feedback,
)
from .printer_transport import (
    LocalP1SClient,
    PrintDispatchOptions,
    PrintDispatchResult,
    PrinterConnectionConfig,
    PrinterConnectionTest,
)
from .reliability import (
    build_diagnostic_bundle,
    create_application_backup,
    restore_application_data_backup,
)
from .vanior_3mf import (
    create_vanior_gcode_3mf,
    upgrade_legacy_vanior_gcode_3mf,
    verify_vanior_gcode_3mf,
)
from .vanior_slice import slice_stl_to_gcode
from .version import __version__
from .workspace import (
    BACKUPS_NAME,
    LEARNING_NAME,
    READY_NAME,
    UPLOADS_NAME,
    WorkspaceLayout,
    append_learning_event,
    ensure_workspace,
    import_model,
    next_project_output,
    next_ready_file,
)
from .workspace import (
    sha256_file as workspace_sha256_file,
)


def suggest_output_path(source_path: str | Path) -> Path:
    """Keep the established public GUI helper available to integrations."""
    return _suggest_output_path(source_path)


def _right_handed_view_coordinates(
    point: tuple[float, float, float], yaw_degrees: float, pitch_degrees: float
) -> tuple[float, float, float]:
    """Project model axes into a right-handed camera without mirroring them."""
    yaw = math.radians(yaw_degrees)
    pitch = math.radians(pitch_degrees)
    cy, sy = math.cos(yaw), math.sin(yaw)
    cp, sp = math.cos(pitch), math.sin(pitch)
    x, y, z = point
    horizontal = sy * y - cy * x
    depth = sy * x + cy * y
    return horizontal, cp * z - sp * depth, sp * z + cp * depth


APP_NAME = "VANIOR PRINT"
ORGANIZATION = "BigVanior"
SUPPORT_URL = "https://boosty.to/vaniorprint"
SUPPORT_PROMPT_SETTINGS_KEY = "support_prompt/boosty_v1_shown"
ASSET_DIR = Path(__file__).with_name("assets")
ICON_PATH = ASSET_DIR / "vanior_print_icon.png"
LOGO_PATH = ASSET_DIR / "vanior_print_logo.png"
ICO_PATH = ASSET_DIR / "vanior_print.ico"


CHECKMARK_PATH = ASSET_DIR / "checkmark.svg"
RADIO_DOT_PATH = ASSET_DIR / "radio-dot.svg"
NAVIGATION_ASSET_DIR = ASSET_DIR / "navigation"
NAVIGATION_ICON_FILES = {
    "overview": "nav_overview.png",
    "analysis": "nav_analysis.png",
    "print": "nav_print.png",
    "preview": "nav_preview.png",
    "optimization": "nav_optimization.png",
    "export": "nav_export.png",
    "history": "nav_history.png",
    "print_dna": "nav_print_dna.png",
    "printers": "nav_printers.png",
    "printer_head": "nav_printer_head.png",
    "material": "nav_material.png",
}

BUILD_PLATE_OPTIONS = (
    ("Текстурированная PEI", "Textured PEI Plate"),
    ("Гладкая PEI / высокотемпературная", "High Temp Plate"),
    ("Холодная пластина", "Cool Plate"),
    ("Инженерная пластина", "Engineering Plate"),
)


def _data_dir() -> Path:
    return ensure_workspace().system


def _migrate_legacy_data(layout: WorkspaceLayout) -> None:
    """Copy legacy application state into the version-independent S: workspace."""
    base = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation)
    legacy = Path(base) if base else Path.home() / ".vanior-print"
    mappings = (
        (legacy / "history.json", layout.history),
        (legacy / "print_dna.json", layout.print_dna),
    )
    for source, destination in mappings:
        try:
            if source.is_file() and not destination.exists() and source.resolve() != destination.resolve():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        except OSError:
            # Legacy migration is optional. A locked/permission-restricted old
            # AppData folder must never prevent the main window from opening.
            continue
    legacy_photos = legacy / "print_dna_photos"
    target_photos = layout.learning / "print_dna_photos"
    try:
        if legacy_photos.is_dir() and not target_photos.exists():
            shutil.copytree(legacy_photos, target_photos)
    except OSError:
        pass


def _read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return default


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def _format_number(value: float | None, suffix: str = "") -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        text = f"{value:,.1f}".replace(",", " ")
    else:
        text = f"{value:,}".replace(",", " ")
    return f"{text}{suffix}"


def _format_history_date(value: Any) -> str:
    try:
        stamp = datetime.fromisoformat(str(value))
        return stamp.strftime("%d.%m.%Y  %H:%M")
    except (TypeError, ValueError):
        return str(value or "—")


def _warning_text(value: Any) -> str:
    """Translate analyzer diagnostics into concise user-facing Russian."""
    text = str(value)
    if (
        text.startswith(("STL contains ", "Model contains "))
        and text.endswith(" meaningful disconnected bodies.")
    ):
        count = text.split(" contains ", 1)[1].removesuffix(" meaningful disconnected bodies.")
        return f"Модель содержит {count} отдельные геометрические части."
    translations = {
        "Mesh density is VERY_HIGH; simplification may improve processing speed.": (
            "Сетка очень плотная; предпросмотр оптимизирован без изменения исходной модели."
        ),
        "Mesh density is HIGH; simplification may improve processing speed.": (
            "Сетка плотная; предпросмотр оптимизирован без изменения исходной модели."
        ),
        "Bridge and local thin-wall detection are not implemented yet.": (
            "Мосты и локальные тонкие стенки следует проверить после нарезки."
        ),
    }
    return translations.get(text, text)


def _nav_icon(kind: str) -> QIcon:
    """Load the supplied navigation artwork, with vector icons as a fallback."""
    asset_name = NAVIGATION_ICON_FILES.get(kind)
    if asset_name is not None:
        supplied_pixmap = QPixmap(str(NAVIGATION_ASSET_DIR / asset_name))
        if not supplied_pixmap.isNull():
            return QIcon(supplied_pixmap)

    pixmap = QPixmap(48, 48)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.scale(1.5, 1.5)
    white = QColor("#fffefe")
    violet = QColor("#9f58ff")
    violet_bright = QColor("#caa2ff")
    dim = QColor("#8d83b5")
    pen = QPen(white, 1.60)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    accent_pen = QPen(violet_bright, 1.35)
    accent_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    accent_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    glow_pen = QPen(QColor(139, 68, 255, 40), 4.6)
    glow_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    glow_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    dim_pen = QPen(dim, 0.85)
    dim_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    dim_pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    dashed_pen = QPen(QColor("#716993"), 0.8)
    dashed_pen.setDashPattern([2.0, 2.2])
    dashed_pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)

    def glowing_path(path: QPainterPath, *, accent: bool = False) -> None:
        painter.setPen(glow_pen)
        painter.drawPath(path)
        painter.setPen(accent_pen if accent else pen)
        painter.drawPath(path)

    def glowing_polyline(points: list[QPointF], *, accent: bool = False) -> None:
        polygon = QPolygonF(points)
        painter.setPen(glow_pen)
        painter.drawPolyline(polygon)
        painter.setPen(accent_pen if accent else pen)
        painter.drawPolyline(polygon)

    def node(point: QPointF, *, accent: bool = False, radius: float = 1.15) -> None:
        painter.setPen(accent_pen if accent else pen)
        painter.setBrush(QColor("#0b0c1a"))
        painter.drawEllipse(point, radius, radius)
        painter.setBrush(Qt.BrushStyle.NoBrush)

    if kind == "overview":
        house = QPainterPath()
        house.moveTo(5, 13)
        house.lineTo(16, 4)
        house.lineTo(27, 13)
        house.moveTo(7.5, 14.5)
        house.lineTo(16, 7.7)
        house.lineTo(24.5, 14.5)
        house.lineTo(24.5, 24.5)
        house.moveTo(7.5, 14.5)
        house.lineTo(7.5, 24.5)
        glowing_path(house)
        painter.setPen(pen)
        painter.drawArc(QRectF(3.0, 21.8, 26.0, 7.0), 190 * 16, 160 * 16)
        painter.setPen(accent_pen)
        for x, top in ((11.0, 19.0), (15.3, 15.7), (19.6, 12.7)):
            painter.drawRoundedRect(QRectF(x, top, 2.1, 23.2 - top), 0.5, 0.5)
        painter.setPen(QPen(QColor(160, 85, 255, 105), 4.0))
        painter.drawLine(QPointF(11.0, 27.1), QPointF(21.0, 27.1))
    elif kind == "analysis":
        top = QPointF(14, 3.5)
        left_top = QPointF(5, 8)
        right_top = QPointF(23, 8)
        center = QPointF(14, 13.5)
        left_bottom = QPointF(5, 21)
        bottom = QPointF(14, 26)
        right_bottom = QPointF(23, 20.5)
        painter.setPen(dim_pen)
        for a, b in (
            (top, left_top), (top, right_top), (left_top, center), (right_top, center),
            (left_top, left_bottom), (left_bottom, bottom), (bottom, center),
            (right_top, right_bottom), (center, right_bottom),
        ):
            painter.drawLine(a, b)
        painter.setPen(dashed_pen)
        painter.drawLine(top, center)
        painter.drawLine(center, left_bottom)
        painter.drawLine(center, bottom)
        for point in (top, left_top, right_top, center, left_bottom, bottom, right_bottom):
            node(point)
        magnifier = QPainterPath()
        magnifier.addEllipse(QRectF(18.2, 16.0, 9.5, 9.5))
        glowing_path(magnifier)
        handle = QPainterPath()
        handle.moveTo(25.7, 23.7)
        handle.lineTo(30.0, 28.0)
        glowing_path(handle, accent=True)
    elif kind == "print":
        nozzle = QPainterPath()
        nozzle.moveTo(4, 4.5)
        nozzle.lineTo(27, 4.5)
        nozzle.moveTo(8, 4.5)
        nozzle.lineTo(8, 8)
        nozzle.lineTo(23, 8)
        nozzle.lineTo(21, 13)
        nozzle.lineTo(11, 13)
        nozzle.lineTo(8, 8)
        nozzle.moveTo(13, 13)
        nozzle.lineTo(13, 17)
        nozzle.lineTo(16, 21)
        nozzle.lineTo(19, 17)
        nozzle.lineTo(19, 13)
        glowing_path(nozzle)
        for y, x in ((22.5, 21.5), (27.0, 11.0), (31.0, 19.0)):
            painter.setPen(pen)
            painter.drawLine(QPointF(3.5, y), QPointF(28.5, y))
            node(QPointF(x, y), accent=True, radius=1.45)
    elif kind == "preview":
        eye = QPainterPath()
        eye.moveTo(2.0, 16)
        eye.cubicTo(8.0, 7.0, 23.5, 7.0, 30.0, 16)
        eye.cubicTo(23.5, 25.0, 8.0, 25.0, 2.0, 16)
        glowing_path(eye)
        painter.setPen(accent_pen)
        cube_top = QPolygonF([QPointF(12.3, 11.3), QPointF(16, 9.2), QPointF(19.7, 11.3), QPointF(16, 13.5)])
        painter.drawPolygon(cube_top)
        painter.drawLine(QPointF(12.3, 11.3), QPointF(12.3, 16.6))
        painter.drawLine(QPointF(19.7, 11.3), QPointF(19.7, 16.6))
        painter.drawLine(QPointF(16, 13.5), QPointF(16, 19.0))
        painter.drawLine(QPointF(12.3, 16.6), QPointF(16, 19.0))
        painter.drawLine(QPointF(19.7, 16.6), QPointF(16, 19.0))
        for offset, alpha in ((0.0, 230), (2.3, 175), (4.6, 115)):
            painter.setPen(QPen(QColor(170, 91, 255, alpha), 1.05))
            painter.drawPolyline(QPolygonF([
                QPointF(9.0, 19.2 + offset), QPointF(16, 23.2 + offset),
                QPointF(23.0, 19.2 + offset),
            ]))
    elif kind == "optimization":
        points = [QPointF(16, 3), QPointF(27, 9), QPointF(27, 21), QPointF(16, 28), QPointF(5, 21), QPointF(5, 9)]
        painter.setPen(dashed_pen)
        for index in range(6):
            painter.drawLine(points[index], points[(index + 2) % 6])
        for point in points:
            node(point)
        bolt = QPainterPath()
        bolt.moveTo(18, 4.8)
        bolt.lineTo(10.5, 17)
        bolt.lineTo(15.7, 17)
        bolt.lineTo(13.2, 27)
        bolt.lineTo(24.0, 12.5)
        bolt.lineTo(18.8, 12.5)
        bolt.closeSubpath()
        glowing_path(bolt)
    elif kind == "export":
        folder = QPainterPath()
        folder.moveTo(3.5, 9)
        folder.lineTo(11, 9)
        folder.lineTo(13.2, 12)
        folder.lineTo(28.5, 12)
        folder.lineTo(28.5, 26.5)
        folder.quadTo(28.5, 28.5, 26.0, 28.5)
        folder.lineTo(5.5, 28.5)
        folder.quadTo(3.5, 28.5, 3.5, 26.0)
        folder.closeSubpath()
        glowing_path(folder)
        arrow = QPainterPath()
        arrow.moveTo(9, 24)
        arrow.lineTo(22, 11)
        arrow.moveTo(22, 11)
        arrow.lineTo(22, 17)
        arrow.moveTo(22, 11)
        arrow.lineTo(16, 11)
        glowing_path(arrow)
    elif kind == "history":
        painter.setPen(glow_pen)
        painter.drawArc(QRectF(4.0, 4.0, 24.0, 24.0), 33 * 16, 255 * 16)
        painter.setPen(pen)
        painter.drawArc(QRectF(4.0, 4.0, 24.0, 24.0), 33 * 16, 255 * 16)
        painter.setPen(dashed_pen)
        painter.drawArc(QRectF(7.0, 7.0, 18.0, 18.0), 155 * 16, 105 * 16)
        painter.setPen(accent_pen)
        painter.drawPolyline(QPolygonF([QPointF(24.0, 6.2), QPointF(28.2, 6.0), QPointF(27.0, 10.2)]))
        painter.setPen(pen)
        painter.drawLine(QPointF(16, 9), QPointF(16, 17))
        painter.drawLine(QPointF(16, 17), QPointF(21, 20))
    elif kind == "print_dna":
        left = [QPointF(10.5 + math.sin(index * 0.82) * 4.2, 3.0 + index * 3.65) for index in range(8)]
        right = [QPointF(21.5 - math.sin(index * 0.82) * 4.2, 3.0 + index * 3.65) for index in range(8)]
        painter.setPen(glow_pen)
        painter.drawPolyline(QPolygonF(left))
        painter.drawPolyline(QPolygonF(right))
        painter.setPen(pen)
        painter.drawPolyline(QPolygonF(left))
        painter.drawPolyline(QPolygonF(right))
        painter.setPen(accent_pen)
        for index in range(1, 8, 2):
            painter.drawLine(left[index], right[index])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#9c56ef"))
        for x in (4.0, 28.0):
            for y in (8.0, 13.0, 18.0, 23.0):
                painter.drawEllipse(QPointF(x, y), 0.65, 0.65)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    elif kind == "material":
        painter.drawEllipse(QRectF(5, 5, 22, 8))
        painter.drawRoundedRect(QRectF(7, 9, 18, 14), 5, 5)
        painter.drawArc(QRectF(5, 18, 22, 9), 180 * 16, 180 * 16)
        painter.setPen(accent_pen)
        painter.drawEllipse(QRectF(11, 10, 10, 4))
        painter.drawArc(QRectF(11, 17, 10, 5), 180 * 16, 180 * 16)
        for y in (13, 16, 19):
            painter.drawLine(QPointF(9, y), QPointF(23, y))
    elif kind == "printers":
        painter.setPen(QPen(QColor(176, 150, 232, 155), 1.2))
        painter.drawRoundedRect(QRectF(8.0, 3.0, 20.0, 21.0), 2.2, 2.2)
        painter.drawRoundedRect(QRectF(5.0, 6.5, 20.0, 21.0), 2.2, 2.2)
        painter.setPen(pen)
        painter.drawRoundedRect(QRectF(2.0, 10.0, 20.0, 20.0), 2.2, 2.2)
        painter.setPen(accent_pen)
        painter.drawLine(QPointF(6, 16), QPointF(18, 16))
        painter.drawPolygon(QPolygonF([QPointF(8, 18), QPointF(16, 18), QPointF(15, 23), QPointF(9, 23)]))
        painter.drawPolyline(QPolygonF([QPointF(10, 23), QPointF(12, 26), QPointF(14, 23)]))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(violet)
        for x in (6.0, 9.0, 12.0):
            painter.drawEllipse(QPointF(x, 28.0), 0.65, 0.65)
        painter.setBrush(Qt.BrushStyle.NoBrush)
    else:
        gear = QPolygonF()
        tooth_count = 10
        for index in range(tooth_count * 4):
            angle = math.radians(-90 + index * (360 / (tooth_count * 4)))
            phase = index % 4
            radius = 13.0 if phase in {1, 2} else 10.7
            gear.append(QPointF(16 + math.cos(angle) * radius, 16 + math.sin(angle) * radius))
        gear.append(gear[0])
        glowing_polyline(list(gear))
        painter.setPen(accent_pen)
        painter.drawEllipse(QRectF(8.7, 8.7, 14.6, 14.6))
        painter.setPen(QPen(QColor(160, 83, 255, 120), 4.3))
        painter.drawArc(QRectF(10.0, 10.0, 12.0, 12.0), 20 * 16, 220 * 16)
    painter.end()
    return QIcon(pixmap)


class DropIllustration(QWidget):
    """Vector loading illustration matching the VANIOR visual language."""

    def __init__(self) -> None:
        super().__init__()
        self.setFixedSize(190, 150)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        center_x = self.width() / 2
        glow = QColor("#7c2bd1")
        faint = QColor(124, 43, 209, 65)
        painter.setPen(QPen(faint, 1))
        for x, height in ((36, 46), (58, 69), (78, 44), (112, 64), (134, 79), (154, 48)):
            painter.drawLine(QPointF(x, 24), QPointF(x, 24 + height))

        painter.setPen(QPen(QColor("#7130b9"), 2))
        painter.drawPolygon(QPolygonF([QPointF(34, 112), QPointF(center_x, 82), QPointF(156, 112), QPointF(center_x, 143)]))
        painter.setPen(QPen(QColor(124, 43, 209, 85), 1))
        painter.drawPolygon(QPolygonF([QPointF(24, 121), QPointF(center_x, 91), QPointF(166, 121), QPointF(center_x, 150)]))

        painter.setPen(QPen(glow, 1.5))
        painter.drawPolygon(QPolygonF([QPointF(55, 88), QPointF(55, 58), QPointF(center_x, 36), QPointF(135, 58), QPointF(135, 88), QPointF(center_x, 110)]))
        painter.drawLine(QPointF(55, 58), QPointF(center_x, 82))
        painter.drawLine(QPointF(135, 58), QPointF(center_x, 82))
        painter.drawLine(QPointF(center_x, 36), QPointF(center_x, 82))

        gradient = QLinearGradient(75, 58, 115, 100)
        gradient.setColorAt(0, QColor("#b45aff"))
        gradient.setColorAt(1, QColor("#54219b"))
        painter.setPen(QPen(QColor("#b061ff"), 1.2))
        painter.setBrush(gradient)
        painter.drawPolygon(QPolygonF([QPointF(76, 65), QPointF(center_x, 55), QPointF(114, 65), QPointF(center_x, 76)]))
        painter.drawPolygon(QPolygonF([QPointF(76, 65), QPointF(center_x, 76), QPointF(center_x, 101), QPointF(76, 88)]))
        painter.setBrush(QColor("#6024a8"))
        painter.drawPolygon(QPolygonF([QPointF(center_x, 76), QPointF(114, 65), QPointF(114, 88), QPointF(center_x, 101)]))
        painter.end()


class FeatureIllustration(QWidget):
    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.setFixedSize(94, 94)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor(136, 57, 222, 90), 1))
        painter.setBrush(QColor(61, 23, 104, 70))
        painter.drawEllipse(QRectF(7, 7, 80, 80))
        painter.setPen(QPen(QColor("#a85aff"), 2.2))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        if self.kind == "analysis":
            painter.drawEllipse(QRectF(23, 20, 43, 43))
            painter.drawLine(QPointF(58, 57), QPointF(76, 75))
        elif self.kind == "optimization":
            painter.drawRoundedRect(QRectF(19, 18, 57, 58), 8, 8)
            for y, x in ((32, 46), (47, 60), (62, 38)):
                painter.drawLine(QPointF(28, y), QPointF(68, y))
                painter.setBrush(QColor("#c893ff"))
                painter.drawEllipse(QPointF(x, y), 3.5, 3.5)
                painter.setBrush(Qt.BrushStyle.NoBrush)
        else:
            painter.drawRoundedRect(QRectF(20, 14, 55, 68), 11, 11)
            painter.drawLine(QPointF(47, 64), QPointF(47, 31))
            painter.drawLine(QPointF(35, 43), QPointF(47, 31))
            painter.drawLine(QPointF(59, 43), QPointF(47, 31))
            painter.drawLine(QPointF(34, 65), QPointF(60, 65))
        painter.end()


class DeviceIllustration(QWidget):
    """Compact printer/spool artwork for the persistent hardware cards."""

    def __init__(self, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.setFixedSize(52, 52)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)

    def paintEvent(self, event: Any) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        glow = QLinearGradient(4, 4, 48, 48)
        glow.setColorAt(0, QColor("#2c3042"))
        glow.setColorAt(1, QColor("#111522"))
        painter.setPen(QPen(QColor("#3a4058"), 1))
        painter.setBrush(glow)
        painter.drawRoundedRect(QRectF(1, 1, 50, 50), 9, 9)
        if self.kind == "printer":
            painter.setPen(QPen(QColor("#aeb5c6"), 1.3))
            painter.setBrush(QColor("#242938"))
            painter.drawRoundedRect(QRectF(12, 7, 29, 38), 3, 3)
            painter.setBrush(QColor("#0b0e16"))
            painter.drawRoundedRect(QRectF(16, 12, 21, 20), 2, 2)
            painter.setPen(QPen(QColor("#70519c"), 1))
            painter.drawLine(QPointF(17, 18), QPointF(36, 18))
            painter.drawLine(QPointF(27, 13), QPointF(27, 30))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor("#8e4ce5"))
            painter.drawRoundedRect(QRectF(23, 20, 8, 5), 1, 1)
            painter.setBrush(QColor("#50576a"))
            painter.drawRoundedRect(QRectF(17, 35, 19, 5), 1, 1)
        else:
            painter.setPen(QPen(QColor("#aeb5c6"), 1.1))
            painter.setBrush(QColor("#73798b"))
            painter.drawEllipse(QRectF(9, 9, 34, 12))
            painter.drawRoundedRect(QRectF(11, 15, 30, 22), 4, 4)
            painter.setBrush(QColor("#2a2e3b"))
            painter.drawEllipse(QRectF(9, 31, 34, 12))
            painter.setBrush(QColor("#121520"))
            painter.drawEllipse(QRectF(19, 12, 14, 7))
            painter.setPen(QPen(QColor("#9f58ec"), 1))
            for y in (20, 24, 28, 32):
                painter.drawLine(QPointF(13, y), QPointF(39, y))
        painter.end()


class SidebarDeviceCard(QFrame):
    clicked = Signal()

    def __init__(self, title: str, subtitle: str, kind: str) -> None:
        super().__init__()
        self.kind = kind
        self.setObjectName("deviceFrame")
        if kind == "material":
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            self.setToolTip("Выбрать пластик и диаметр сопла")
            self.setAccessibleName("Выбор пластика и сопла")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(10, 9, 10, 9)
        layout.setSpacing(11)
        layout.addWidget(DeviceIllustration(kind))
        text_layout = QVBoxLayout()
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        self.title_label = QLabel()
        self.title_label.setObjectName("deviceTitle")
        self.title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.subtitle_label = QLabel()
        self.subtitle_label.setObjectName("deviceSubtitle")
        self.subtitle_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        text_layout.addWidget(self.title_label)
        text_layout.addWidget(self.subtitle_label)
        layout.addLayout(text_layout, 1)
        self.chevron = QLabel("⌄" if kind == "material" else "")
        self.chevron.setObjectName("deviceChevron")
        self.chevron.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        layout.addWidget(self.chevron)
        self.set_content(title, subtitle)

    def set_content(self, title: str, subtitle: str) -> None:
        ready = "готов" in subtitle.casefold()
        color = "#43df8c" if ready else "#aab1c1"
        self.title_label.setText(title)
        self.subtitle_label.setText(f"●  {subtitle}")
        self.subtitle_label.setStyleSheet(f"color: {color}; background: transparent;")

    def setText(self, text: str) -> None:
        cleaned = text.replace("◆", "").strip()
        self.set_content(cleaned, "Текущий профиль")

    def mouseReleaseEvent(self, event: Any) -> None:
        if self.kind == "material" and event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def keyPressEvent(self, event: Any) -> None:
        if self.kind == "material" and event.key() in {
            Qt.Key.Key_Return,
            Qt.Key.Key_Enter,
            Qt.Key.Key_Space,
        }:
            self.clicked.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class ImmediateToolTipButton(QPushButton):
    """Show rail labels immediately instead of relying on the OS tooltip delay."""

    def _show_tooltip(self) -> None:
        text = self.toolTip().strip()
        if not text:
            return
        anchor = self.mapToGlobal(QPoint(self.width() + 10, max(0, self.height() // 2 - 12)))
        QToolTip.showText(anchor, text, self)

    def enterEvent(self, event: Any) -> None:
        self._show_tooltip()
        super().enterEvent(event)

    def leaveEvent(self, event: Any) -> None:
        QToolTip.hideText()
        super().leaveEvent(event)

    def focusInEvent(self, event: Any) -> None:
        self._show_tooltip()
        super().focusInEvent(event)


class SidebarQuickAction(ImmediateToolTipButton):
    """Icon-only hardware action that keeps the compact navigation rail useful."""

    def __init__(self, kind: str, title: str, subtitle: str) -> None:
        super().__init__()
        self.kind = kind
        self.setObjectName("sidebarQuickAction")
        self.setFixedSize(48, 48)
        self.setIcon(_nav_icon(kind))
        self.setIconSize(QSize(36, 36))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.set_content(title, subtitle)

    def set_content(self, title: str, subtitle: str) -> None:
        action = (
            "Выбрать сопло, пластину и посмотреть принтер"
            if self.kind == "printer_head"
            else "Выбрать пластик"
        )
        self.setToolTip(f"{title}\n{subtitle}\n{action}")
        self.setAccessibleName(f"{title}. {subtitle}. {action}")


MATERIAL_PRODUCTS: tuple[tuple[str, str], ...] = (
    ("Generic PLA", "PLA"),
    ("Bambu Lab PLA Basic", "PLA"),
    ("Bambu Lab PLA Lite", "PLA"),
    ("Bambu Lab PLA Matte", "PLA"),
    ("Bambu Lab PLA Silk", "PLA"),
    ("eSUN PLA+", "PLA"),
    ("SUNLU PLA", "PLA"),
    ("Polymaker PolyLite PLA", "PLA"),
    ("Prusament PLA", "PLA"),
    ("Overture PLA", "PLA"),
    ("Generic PETG", "PETG"),
    ("Bambu Lab PETG Basic", "PETG"),
    ("Bambu Lab PETG HF", "PETG"),
    ("eSUN PETG", "PETG"),
    ("SUNLU PETG", "PETG"),
    ("Polymaker PolyLite PETG", "PETG"),
    ("Prusament PETG", "PETG"),
    ("Overture PETG", "PETG"),
)


class MaterialProfileDialog(QDialog):
    """Focused filament selector opened by the spool quick action."""

    def __init__(self, material_profile: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("profileDialog")
        self.setWindowTitle("Выбор пластика")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel("Пластик")
        title.setObjectName("dialogTitle")
        note = QLabel(
            "Выберите пластик, установленный в принтере. VANIOR PRINT "
            "применит подходящие температуры, обдув и параметры подачи."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(12)
        self.material = QComboBox()
        self.material.setObjectName("profileMaterial")
        for product, family in MATERIAL_PRODUCTS:
            self.material.addItem(product, family)
        material_index = self.material.findText(material_profile)
        if material_index < 0:
            material_index = self.material.findData(material_profile.upper())
        self.material.setCurrentIndex(max(0, material_index))

        form.addRow("Пластик", self.material)
        layout.addLayout(form)

        hint = QLabel("Выбор сохранится и будет применяться ко всем новым проектам.")
        hint.setObjectName("profileHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Отмена")
        save = QPushButton("Выбрать пластик")
        save.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    @property
    def selected_material(self) -> str:
        return self.material.currentText()

    @property
    def selected_family(self) -> str:
        return str(self.material.currentData())


class PrinterHardwareDialog(QDialog):
    """Printer identity, nozzle and build-plate selector."""

    def __init__(
        self,
        printer_model: str,
        nozzle_mm: float,
        bed_type: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("profileDialog")
        self.setWindowTitle("Принтер, сопло и пластина")
        self.setModal(True)
        self.setMinimumWidth(460)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 22)
        layout.setSpacing(14)

        title = QLabel("Печатающая головка")
        title.setObjectName("dialogTitle")
        note = QLabel(
            "Проверьте выбранный принтер, диаметр сопла и установленную пластину. "
            "От них зависят ширина линии, температура стола и безопасные параметры нарезки."
        )
        note.setObjectName("muted")
        note.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(note)

        form = QFormLayout()
        form.setSpacing(12)
        self.printer_name = QLabel(printer_model or "Bambu Lab P1S")
        self.printer_name.setObjectName("profilePrinterName")
        self.printer_name.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.nozzle = QComboBox()
        self.nozzle.setObjectName("profileNozzle")
        for diameter in (0.2, 0.4, 0.6, 0.8):
            self.nozzle.addItem(f"{diameter:.1f} мм".replace(".", ","), diameter)
        nozzle_index = self.nozzle.findData(float(nozzle_mm))
        self.nozzle.setCurrentIndex(max(0, nozzle_index))
        self.build_plate = QComboBox()
        self.build_plate.setObjectName("profileBuildPlate")
        for title_text, value in BUILD_PLATE_OPTIONS:
            self.build_plate.addItem(title_text, value)
        plate_index = self.build_plate.findData(str(bed_type))
        self.build_plate.setCurrentIndex(max(0, plate_index))
        form.addRow("Принтер", self.printer_name)
        form.addRow("Диаметр сопла", self.nozzle)
        form.addRow("Пластина", self.build_plate)
        layout.addLayout(form)

        hint = QLabel("Выбранные сопло и пластина будут применяться ко всем новым проектам.")
        hint.setObjectName("profileHint")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        actions = QHBoxLayout()
        actions.addStretch(1)
        cancel = QPushButton("Отмена")
        save = QPushButton("Применить")
        save.setObjectName("primary")
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept)
        actions.addWidget(cancel)
        actions.addWidget(save)
        layout.addLayout(actions)

    @property
    def selected_nozzle_mm(self) -> float:
        return float(self.nozzle.currentData())

    @property
    def selected_bed_type(self) -> str:
        return str(self.build_plate.currentData())


class RecentProjectRow(QWidget):
    def __init__(self, name: str, date: str, ready: bool) -> None:
        super().__init__()
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 6)
        icon = QLabel()
        icon.setObjectName("recentIcon")
        icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        icon.setFixedSize(38, 38)
        icon.setPixmap(_nav_icon("export").pixmap(23, 23))
        texts = QVBoxLayout()
        texts.setSpacing(1)
        title = QLabel(name)
        title.setObjectName("recentTitle")
        detail = QLabel(f"{date}   •   {'Готово' if ready else 'Файл недоступен'}")
        detail.setObjectName("recentMeta")
        texts.addWidget(title)
        texts.addWidget(detail)
        state = QLabel("✓" if ready else "!")
        state.setObjectName("recentReady" if ready else "recentMissing")
        state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        state.setFixedSize(26, 26)
        menu = QLabel("⋮")
        menu.setObjectName("recentMenu")
        layout.addWidget(icon)
        layout.addLayout(texts, 1)
        layout.addWidget(state)
        layout.addWidget(menu)


class DropLineEdit(QLineEdit):
    fileDropped = Signal(str)

    def __init__(self, suffixes: set[str], parent: QWidget | None = None) -> None:
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


class DropZone(QFrame):
    fileSelected = Signal(str)
    clicked = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("dropZone")
        self.setAcceptDrops(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(28, 20, 28, 28)
        layout.setSpacing(7)
        self.illustration = DropIllustration()
        title = QLabel("Перетащите 3D-модель сюда")
        title.setObjectName("dropTitle")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        subtitle = QLabel("или нажмите для выбора файла")
        subtitle.setObjectName("muted")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        badges = QLabel("   STL     3MF   ")
        badges.setObjectName("badges")
        badges.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addStretch(1)
        layout.addWidget(self.illustration, 0, Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(8)
        layout.addWidget(badges)
        layout.addStretch(1)
        self.setMinimumHeight(390)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and Path(urls[0].toLocalFile()).suffix.lower() in SUPPORTED_MODEL_SUFFIXES:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        path = event.mimeData().urls()[0].toLocalFile()
        self.fileSelected.emit(path)
        event.acceptProposedAction()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(event)


class SoftwareModelCanvas(QFrame):
    """Interactive solid-mesh viewport implemented with Qt's software painter."""

    def __init__(self) -> None:
        super().__init__()
        self.setObjectName("modelCanvas")
        self.setMinimumHeight(420)
        self._vertices: list[tuple[float, float, float]] = []
        self._faces: list[tuple[int, int, int]] = []
        self._interactive_vertices: list[tuple[float, float, float]] = []
        self._interactive_faces: list[tuple[int, int, int]] = []
        self._interaction_active = False
        self._zoom_snapshot: QPixmap | None = None
        self._zoom_snapshot_zoom = 1.0
        self._capturing_zoom_snapshot = False
        self._zoom_timer = QTimer(self)
        self._zoom_timer.setSingleShot(True)
        self._zoom_timer.timeout.connect(self._finish_smooth_zoom)
        self._caption = "Загрузите STL или 3MF"
        self._accent = QColor("#9b4dff")
        self._yaw = 145.0
        self._pitch = 22.0
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self._last_pointer: QPointF | None = None
        self._layer_fraction = 1.0
        self._layer_index = 1
        self._layer_count = 1
        self._layer_z_mm: float | None = None
        self._source_center_z_mm = 0.0
        self._source_scale_mm: float | None = None
        self._layer_z_values_mm: list[float] = []
        self._layer_paths: list[list[tuple[float, float, float, float, str]]] = []
        self._visible_path_roles = {
            "model", "outer_wall", "inner_wall", "top_surface", "bottom_surface",
            "infill", "solid_infill", "bridge", "skirt_brim",
            "support", "support_interface",
        }
        self.setMouseTracking(True)

    def set_model(self, path: Path | None, caption: str = "") -> None:
        """Clear the viewport; mesh parsing is deliberately worker-only."""
        self._vertices = []
        self._faces = []
        self._interactive_vertices = []
        self._interactive_faces = []
        self._layer_fraction = 1.0
        self._layer_paths = []
        self._layer_z_values_mm = []
        self._source_center_z_mm = 0.0
        self._source_scale_mm = None
        self._finish_smooth_zoom(update=False)
        self._caption = caption or (path.name if path else "Загрузите STL или 3MF")
        self.update()

    def set_mesh(self, mesh: dict[str, Any] | None, caption: str) -> None:
        """Apply a worker-produced compact mesh without touching the source file."""
        mesh = mesh or {}
        prepared = mesh.get("_vanior_prepared_mesh")
        if not isinstance(prepared, tuple) or len(prepared) != 4:
            def prepare(
                raw_vertices: Any, raw_faces: Any
            ) -> tuple[tuple[tuple[float, float, float], ...], tuple[tuple[int, int, int], ...]]:
                vertices = tuple(
                    (float(item[0]), float(item[1]), float(item[2]))
                    for item in raw_vertices
                    if isinstance(item, (list, tuple)) and len(item) == 3
                )
                vertex_count = len(vertices)
                faces = tuple(
                    (int(item[0]), int(item[1]), int(item[2]))
                    for item in raw_faces
                    if (
                        isinstance(item, (list, tuple))
                        and len(item) == 3
                        and all(0 <= int(index) < vertex_count for index in item)
                    )
                )
                return vertices, faces

            detail_vertices, detail_faces = prepare(
                mesh.get("vertices", []), mesh.get("faces", [])
            )
            interactive_vertices, interactive_faces = prepare(
                mesh.get("interactive_vertices", mesh.get("vertices", [])),
                mesh.get("interactive_faces", mesh.get("faces", [])),
            )
            prepared = (
                detail_vertices, detail_faces, interactive_vertices, interactive_faces
            )
            mesh["_vanior_prepared_mesh"] = prepared
        self._vertices, self._faces, self._interactive_vertices, self._interactive_faces = prepared
        self._caption = caption
        self._finish_smooth_zoom(update=False)
        self._layer_fraction = 1.0
        self._layer_index = 1
        self._layer_count = 1
        self._layer_z_mm = None
        self._layer_paths = []
        self._layer_z_values_mm = []
        source_center = mesh.get("source_center_mm")
        source_scale = mesh.get("source_scale_mm")
        self._source_center_z_mm = (
            float(source_center[2])
            if isinstance(source_center, (list, tuple)) and len(source_center) == 3
            else 0.0
        )
        self._source_scale_mm = (
            float(source_scale)
            if isinstance(source_scale, (int, float)) and float(source_scale) > 1e-9
            else None
        )
        self.reset_view()

    def clear_mesh(self, caption: str) -> None:
        self._vertices = []
        self._faces = []
        self._interactive_vertices = []
        self._interactive_faces = []
        self._layer_fraction = 1.0
        self._layer_paths = []
        self._layer_z_values_mm = []
        self._source_center_z_mm = 0.0
        self._source_scale_mm = None
        self._caption = caption
        self._finish_smooth_zoom(update=False)
        self.update()

    def set_layer_paths(
        self,
        layers: list[Any] | None,
        z_mm: list[float] | None = None,
    ) -> None:
        """Attach worker-parsed extrusion paths for exact per-layer preview."""
        self._layer_paths = []
        self._layer_z_values_mm = [float(value) for value in z_mm or []]
        for raw_layer in layers or []:
            layer: list[tuple[float, float, float, float, str]] = []
            if isinstance(raw_layer, (list, tuple)):
                for item in raw_layer:
                    if isinstance(item, (list, tuple)) and len(item) in {4, 5}:
                        layer.append(
                            (
                                *(float(value) for value in item[:4]),
                                str(item[4]) if len(item) == 5 else "model",
                            )
                        )
            self._layer_paths.append(layer)
        self.update()

    def _layer_scene_z(self, index: int) -> float:
        if (
            0 <= index < len(self._layer_z_values_mm)
            and self._source_scale_mm is not None
        ):
            return (
                self._layer_z_values_mm[index] - self._source_center_z_mm
            ) / self._source_scale_mm
        vertices = self._interactive_vertices or self._vertices
        if not vertices:
            return 0.0
        minimum_z = min(vertex[2] for vertex in vertices)
        maximum_z = max(vertex[2] for vertex in vertices)
        return minimum_z + (maximum_z - minimum_z) * (index + 1) / max(
            1, len(self._layer_paths)
        )

    def set_layer(
        self,
        layer: int,
        total: int,
        *,
        fraction: float | None = None,
        z_mm: float | None = None,
    ) -> None:
        """Reveal the model up to the selected print layer."""
        total = max(1, int(total))
        layer = max(1, min(total, int(layer)))
        self._layer_index = layer
        self._layer_count = total
        self._layer_fraction = max(
            0.0,
            min(1.0, float(fraction) if fraction is not None else layer / total),
        )
        self._layer_z_mm = float(z_mm) if z_mm is not None else None
        self.update()

    def _selected_layer_scene_z(self, minimum_z: float, maximum_z: float) -> float:
        """Map the slicer's physical Z value into the normalized preview frame."""
        if self._layer_index >= self._layer_count:
            return maximum_z
        if self._layer_z_mm is not None and self._source_scale_mm is not None:
            mapped = (self._layer_z_mm - self._source_center_z_mm) / self._source_scale_mm
            return max(minimum_z, min(maximum_z, mapped))
        return minimum_z + (maximum_z - minimum_z) * self._layer_fraction

    def reset_view(self) -> None:
        self._finish_smooth_zoom(update=False)
        self._yaw = 145.0
        self._pitch = 22.0
        self._zoom = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def begin_interaction(self) -> None:
        self._finish_smooth_zoom(update=False)
        if self._interactive_faces:
            self._interaction_active = True
            self.update()

    def end_interaction(self) -> None:
        if self._interaction_active:
            self._interaction_active = False
            self.update()

    def rotate_view(self, horizontal_delta: float, vertical_delta: float) -> None:
        """Rotate as if the user grabbed and moved the physical model."""
        self._yaw = (self._yaw + horizontal_delta * 0.65) % 360.0
        self._pitch = max(-88.0, min(88.0, self._pitch + vertical_delta * 0.55))
        self.update()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            if event.button() == Qt.MouseButton.LeftButton:
                self.begin_interaction()
            else:
                self._finish_smooth_zoom(update=False)
                self.end_interaction()
            self._last_pointer = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._last_pointer is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position() - self._last_pointer
        self._last_pointer = event.position()
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.rotate_view(delta.x(), delta.y())
        elif event.buttons() & Qt.MouseButton.RightButton:
            self._pan += QPointF(delta.x(), delta.y())
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._last_pointer = None
            self.unsetCursor()
            self.end_interaction()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def wheelEvent(self, event: Any) -> None:
        self._begin_smooth_zoom()
        self.zoom_view(event.angleDelta().y() / 120.0)
        self._zoom_timer.start(160)
        event.accept()

    def zoom_view(self, steps: float) -> None:
        """Zoom without swapping the detailed mesh for the interaction LOD."""
        # Zoom keeps the detailed mesh.  The wheel changes only projection
        # scale, so degrading geometry here is visually distracting and does
        # not justify the temporary loss of shape.
        self.end_interaction()
        self._zoom = max(0.25, min(5.0, self._zoom * (1.12 ** steps)))
        self.update()

    def _viewport_rect(self) -> Any:
        return self.rect().adjusted(22, 22, -22, -42)

    def _begin_smooth_zoom(self) -> None:
        if self._zoom_snapshot is not None or not self.isVisible():
            return
        viewport = self._viewport_rect()
        if viewport.width() <= 0 or viewport.height() <= 0:
            return
        self._capturing_zoom_snapshot = True
        try:
            self._zoom_snapshot = self.grab(viewport)
            self._zoom_snapshot_zoom = self._zoom
        finally:
            self._capturing_zoom_snapshot = False

    def _finish_smooth_zoom(self, *, update: bool = True) -> None:
        self._zoom_timer.stop()
        self._zoom_snapshot = None
        if update:
            self.update()

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    def _transformed_vertices(
        self, vertices: list[tuple[float, float, float]] | tuple[tuple[float, float, float], ...]
    ) -> list[tuple[float, float, float]]:
        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        transformed: list[tuple[float, float, float]] = []
        for x, y, z in vertices:
            horizontal = sy * y - cy * x
            depth = sy * x + cy * y
            vertical = cp * z - sp * depth
            camera_depth = sp * z + cp * depth
            transformed.append((horizontal, vertical, camera_depth))
        return transformed

    @staticmethod
    def _clip_polygon_to_z(
        polygon: list[tuple[float, float, float]], cutoff: float
    ) -> list[tuple[float, float, float]]:
        """Clip a convex polygon against the horizontal layer plane."""
        if not polygon:
            return []
        result: list[tuple[float, float, float]] = []
        previous = polygon[-1]
        previous_inside = previous[2] <= cutoff + 1e-8
        for current in polygon:
            current_inside = current[2] <= cutoff + 1e-8
            if current_inside != previous_inside:
                denominator = current[2] - previous[2]
                amount = 0.0 if abs(denominator) <= 1e-12 else (cutoff - previous[2]) / denominator
                result.append(
                    (
                        previous[0] + (current[0] - previous[0]) * amount,
                        previous[1] + (current[1] - previous[1]) * amount,
                        cutoff,
                    )
                )
            if current_inside:
                result.append(current)
            previous = current
            previous_inside = current_inside
        return result

    def paintEvent(self, event: Any) -> None:
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._viewport_rect()
        painter.setPen(QPen(QColor(58, 35, 91, 115), 1))
        horizon = rect.bottom() - rect.height() * 0.20
        for index in range(12):
            y = horizon - index * rect.height() / 18
            painter.drawLine(rect.left(), int(y), rect.right(), int(y))
        center = rect.center().x()
        for index in range(-10, 11):
            x = center + index * rect.width() / 16
            painter.drawLine(int(center), int(rect.top() + rect.height() * 0.28), int(x), rect.bottom())

        if self._zoom_snapshot is not None and not self._capturing_zoom_snapshot:
            ratio = self._zoom / max(self._zoom_snapshot_zoom, 1e-9)
            anchor = QPointF(rect.center()) + self._pan
            source_anchor_x = rect.width() / 2.0 + self._pan.x()
            source_anchor_y = rect.height() / 2.0 + self._pan.y()
            target = QRectF(
                anchor.x() - source_anchor_x * ratio,
                anchor.y() - source_anchor_y * ratio,
                rect.width() * ratio,
                rect.height() * ratio,
            )
            painter.save()
            painter.setClipRect(rect)
            painter.drawPixmap(target, self._zoom_snapshot, QRectF(self._zoom_snapshot.rect()))
            painter.restore()
            painter.setPen(QColor("#b9aec9"))
            painter.drawText(
                self.rect().adjusted(14, 10, -14, -10),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                "ЛКМ — вращение   •   колесо — масштаб   •   ПКМ — перемещение   •   двойной щелчок — сброс   •   плавный масштаб",
            )
            painter.drawText(
                self.rect().adjusted(12, 0, -12, -10),
                Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
                self._caption,
            )
            return

        model_rect = QRectF(rect)
        vertices = (
            self._interactive_vertices
            if self._interaction_active and self._interactive_vertices
            else self._vertices
        )
        faces = (
            self._interactive_faces
            if self._interaction_active and self._interactive_faces
            else self._faces
        )
        if vertices and faces:
            transformed = self._transformed_vertices(vertices)
            yaw = math.radians(self._yaw)
            pitch = math.radians(self._pitch)
            cy, sy = math.cos(yaw), math.sin(yaw)
            cp, sp = math.cos(pitch), math.sin(pitch)

            def transform(point: tuple[float, float, float]) -> tuple[float, float, float]:
                x, y, z = point
                horizontal = sy * y - cy * x
                depth = sy * x + cy * y
                return (
                    horizontal,
                    cp * z - sp * depth,
                    sp * z + cp * depth,
                )

            camera_distance = 4.5
            scale = min(model_rect.width(), model_rect.height()) * 0.39 * self._zoom
            centre = model_rect.center() + self._pan

            def project(point: tuple[float, float, float]) -> QPointF:
                x, y, depth = point
                perspective = camera_distance / max(1.0, camera_distance - depth)
                return QPointF(
                    centre.x() + x * scale * perspective,
                    centre.y() - y * scale * perspective,
                )

            minimum_z = min(vertex[2] for vertex in vertices)
            maximum_z = max(vertex[2] for vertex in vertices)
            cutoff = self._selected_layer_scene_z(minimum_z, maximum_z)
            cutting = self._layer_index < self._layer_count
            triangles: list[
                tuple[
                    float,
                    tuple[float, float, float],
                    tuple[float, float, float],
                    tuple[float, float, float],
                ]
            ] = []
            cut_edges: list[tuple[tuple[float, float, float], tuple[float, float, float]]] = []
            for face in faces:
                raw = [vertices[face[0]], vertices[face[1]], vertices[face[2]]]
                if cutting:
                    clipped = self._clip_polygon_to_z(raw, cutoff)
                    if len(clipped) < 3:
                        continue
                    boundary = [point for point in clipped if abs(point[2] - cutoff) <= 1e-7]
                    if len(boundary) >= 2:
                        cut_edges.append((transform(boundary[0]), transform(boundary[1])))
                    transformed_polygon = [transform(point) for point in clipped]
                else:
                    transformed_polygon = [
                        transformed[face[0]], transformed[face[1]], transformed[face[2]]
                    ]
                for index in range(1, len(transformed_polygon) - 1):
                    a = transformed_polygon[0]
                    b = transformed_polygon[index]
                    c = transformed_polygon[index + 1]
                    triangles.append(((a[2] + b[2] + c[2]) / 3.0, a, b, c))

            triangles.sort(key=lambda item: item[0])
            painter.setPen(QPen(Qt.PenStyle.NoPen))
            light = (-0.35, 0.45, 0.82)
            for _, a, b, c in triangles:
                ax, ay, az = a
                bx, by, bz = b
                cx, cy, cz = c
                ux, uy, uz = bx - ax, by - ay, bz - az
                vx, vy, vz = cx - ax, cy - ay, cz - az
                nx = uy * vz - uz * vy
                ny = uz * vx - ux * vz
                nz = ux * vy - uy * vx
                length = math.sqrt(nx * nx + ny * ny + nz * nz)
                if length <= 1e-10:
                    continue
                brightness = 0.34 + 0.66 * abs(
                    (nx * light[0] + ny * light[1] + nz * light[2]) / length
                )
                painter.setBrush(
                    QColor(
                        int(78 + 72 * brightness),
                        int(22 + 29 * brightness),
                        int(128 + 105 * brightness),
                        255,
                    )
                )
                painter.drawPolygon(QPolygonF([project(a), project(b), project(c)]))

            if cutting and cut_edges:
                painter.setPen(QPen(QColor("#ffb347"), 1.35))
                for left, right in cut_edges:
                    painter.drawLine(project(left), project(right))

            path_index = self._layer_index - 1
            if 0 <= path_index < len(self._layer_paths):
                # Build a cumulative 3D view of supports below the selected
                # layer. Drawing only the current cross-section made tree
                # supports look like disconnected green contours.
                support_segments = [
                    (layer_index, segment)
                    for layer_index, layer in enumerate(self._layer_paths[: path_index + 1])
                    for segment in layer
                    if segment[4] in {"support", "support_interface"}
                    and segment[4] in self._visible_path_roles
                ]
                support_step = max(1, math.ceil(len(support_segments) / 35_000))
                for layer_index, (x1, y1, x2, y2, role) in support_segments[::support_step]:
                    support_z = self._layer_scene_z(layer_index) + 0.001
                    painter.setPen(
                        QPen(
                            QColor("#47e889" if role == "support" else "#ffd75a"),
                            2.1 if role == "support" else 2.5,
                        )
                    )
                    painter.drawLine(
                        project(transform((x1, y1, support_z))),
                        project(transform((x2, y2, support_z))),
                    )
                painter.setPen(QPen(QColor("#55ddff"), 1.15))
                path_z = min(maximum_z, cutoff + (maximum_z - minimum_z) * 0.0015)
                for x1, y1, x2, y2, role in self._layer_paths[path_index]:
                    painter.setPen(
                        QPen(
                            QColor(
                                "#5ff28a"
                                if role == "support"
                                else "#ffd166"
                                if role == "support_interface"
                                else "#55ddff"
                            ),
                            1.25,
                        )
                    )
                    painter.drawLine(
                        project(transform((x1, y1, path_z))),
                        project(transform((x2, y2, path_z))),
                    )

            if not self._capturing_zoom_snapshot:
                painter.setPen(QColor("#b9aec9"))
                interaction_hint = "   •   быстрый режим" if self._interaction_active else ""
                painter.drawText(
                    self.rect().adjusted(14, 10, -14, -10),
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                    "ЛКМ — вращение   •   колесо — масштаб   •   ПКМ — перемещение   •   двойной щелчок — сброс" + interaction_hint,
                )
            if self._layer_count > 1:
                layer_text = f"Слой {self._layer_index} / {self._layer_count}"
                if self._layer_z_mm is not None:
                    layer_text += f"   •   Z {self._layer_z_mm:.2f} мм"
                painter.setPen(QColor("#ffca72") if cutting else QColor("#b9aec9"))
                painter.drawText(
                    self.rect().adjusted(14, 10, -14, -10),
                    Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                    layer_text,
                )
        else:
            model_rect = QRectF(
                rect.left() + rect.width() * 0.12,
                rect.top() + rect.height() * 0.05,
                rect.width() * 0.76,
                rect.height() * 0.82,
            )
            cx, cy = model_rect.center().x(), model_rect.center().y()
            size = min(model_rect.width(), model_rect.height()) * 0.24
            top = QPointF(cx, cy - size)
            left = QPointF(cx - size * 0.86, cy - size * 0.48)
            right = QPointF(cx + size * 0.86, cy - size * 0.48)
            bottom = QPointF(cx, cy)
            painter.setPen(QPen(self._accent, 2.2))
            painter.drawPolygon(QPolygonF([top, right, bottom, left]))
            painter.drawLine(bottom, QPointF(cx, cy + size))
            painter.drawLine(left, QPointF(cx, cy + size * 0.52))
            painter.drawLine(right, QPointF(cx, cy + size * 0.52))
            painter.drawLine(QPointF(cx, cy + size), QPointF(cx - size * 0.86, cy + size * 0.52))
            painter.drawLine(QPointF(cx, cy + size), QPointF(cx + size * 0.86, cy + size * 0.52))

        if not self._capturing_zoom_snapshot:
            painter.setPen(QColor("#a99fba"))
            painter.drawText(self.rect().adjusted(12, 0, -12, -10), Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter, self._caption)


class ModelCanvas(QOpenGLWidget):
    """Hardware-accelerated 3D viewport.

    The old preview submitted every triangle to QPainter on every frame.  This
    widget uploads a compact triangle stream once and leaves camera transforms,
    clipping, depth testing and lighting to the GPU.  Wheel and drag events now
    only update a few shader uniforms, so dense organic models remain smooth.
    """

    _VERTEX_SHADER = """
        attribute vec3 vertexPosition;
        attribute vec3 vertexNormal;
        uniform float yawAngle;
        uniform float pitchAngle;
        uniform float zoomFactor;
        uniform float cameraDistance;
        uniform vec2 viewportScale;
        uniform vec2 panOffset;
        varying vec3 normalView;
        varying float modelZ;

        void main() {
            float cy = cos(yawAngle);
            float sy = sin(yawAngle);
            float cp = cos(pitchAngle);
            float sp = sin(pitchAngle);
            // The camera basis must be right-handed.  The previous sign order
            // reflected asymmetric models horizontally in every preview.
            float horizontal = sy * vertexPosition.y - cy * vertexPosition.x;
            float depth = sy * vertexPosition.x + cy * vertexPosition.y;
            float vertical = cp * vertexPosition.z - sp * depth;
            float cameraDepth = sp * vertexPosition.z + cp * depth;
            float perspective = cameraDistance / max(1.0, cameraDistance - cameraDepth);

            float normalHorizontal = sy * vertexNormal.y - cy * vertexNormal.x;
            float normalDepth = sy * vertexNormal.x + cy * vertexNormal.y;
            normalView = vec3(
                normalHorizontal,
                cp * vertexNormal.z - sp * normalDepth,
                sp * vertexNormal.z + cp * normalDepth
            );
            modelZ = vertexPosition.z;
            gl_Position = vec4(
                horizontal * viewportScale.x * zoomFactor * perspective + panOffset.x,
                vertical * viewportScale.y * zoomFactor * perspective + panOffset.y,
                -cameraDepth / 4.5,
                1.0
            );
        }
    """

    _FRAGMENT_SHADER = """
        uniform float layerCutoff;
        uniform float unlitMode;
        uniform vec4 baseColor;
        varying vec3 normalView;
        varying float modelZ;

        void main() {
            // Toolpaths are intentionally placed just above the selected
            // physical layer. Clipping them with the solid mesh used to make
            // every parsed wall/infill/support path invisible.
            if ((unlitMode < 0.5 || unlitMode > 1.5) && modelZ > layerCutoff + 0.00001)
                discard;
            if (unlitMode > 0.5) {
                gl_FragColor = baseColor;
                return;
            }
            vec3 lightDirection = normalize(vec3(-0.35, 0.45, 0.82));
            float brightness = 0.34 + 0.66 * abs(dot(normalize(normalView), lightDirection));
            vec3 darkPurple = vec3(0.31, 0.086, 0.50);
            vec3 lightPurple = vec3(0.59, 0.20, 0.91);
            gl_FragColor = vec4(mix(darkPurple, lightPurple, brightness), 1.0);
        }
    """

    def __init__(self) -> None:
        surface_format = QSurfaceFormat()
        surface_format.setDepthBufferSize(24)
        surface_format.setStencilBufferSize(8)
        surface_format.setSamples(4)
        super().__init__()
        self.setFormat(surface_format)
        self.setObjectName("modelCanvas")
        self.setMinimumHeight(420)
        self.setMouseTracking(True)
        self.setUpdateBehavior(QOpenGLWidget.UpdateBehavior.NoPartialUpdate)

        self._vertices: tuple[tuple[float, float, float], ...] = ()
        self._faces: tuple[tuple[int, int, int], ...] = ()
        self._interactive_vertices: tuple[tuple[float, float, float], ...] = ()
        self._interactive_faces: tuple[tuple[int, int, int], ...] = ()
        self._gpu_payload: tuple[bytes, int, bytes, int] = (b"", 0, b"", 0)
        self._gpu_dirty = True
        self._layer_gpu_dirty = True
        self._support_gpu_dirty = True
        self._layer_blob = b""
        self._layer_vertex_count = 0
        self._support_blob = b""
        self._support_vertex_count = 0
        self._support_ranges: list[tuple[str, int, int]] = []
        self._gl_ready = False
        self._gl_error = ""
        self._program: QOpenGLShaderProgram | None = None
        self._detail_vbo: QOpenGLBuffer | None = None
        self._interactive_vbo: QOpenGLBuffer | None = None
        self._layer_vbo: QOpenGLBuffer | None = None
        self._support_vbo: QOpenGLBuffer | None = None
        self._plate_vbo: QOpenGLBuffer | None = None
        self._plate_blob = b""
        self._plate_surface_count = 0
        self._plate_grid_first = 0
        self._plate_grid_count = 0
        self._plate_border_first = 0
        self._plate_border_count = 0
        self._plate_gpu_dirty = True
        self._plate_bounds = (-1.6, 1.6, -1.6, 1.6, -1.02)
        self._plate_center_xy_mm = (128.0, 128.0)
        self._build_volume_mm = (256.0, 256.0, 256.0)
        self._camera_distance = 4.5

        self._caption = "Загрузите STL или 3MF"
        self._yaw = 145.0
        self._pitch = 22.0
        self._zoom = 1.0
        self._zoom_target = 1.0
        self._zoom_animation = QTimer(self)
        self._zoom_animation.setInterval(8)
        self._zoom_animation.timeout.connect(self._animate_zoom)
        self._pan = QPointF(0.0, 0.0)
        self._last_pointer: QPointF | None = None
        self._interaction_active = False
        self._layer_fraction = 1.0
        self._layer_index = 1
        self._layer_count = 1
        self._layer_z_mm: float | None = None
        self._source_center_z_mm = 0.0
        self._source_scale_mm: float | None = None
        self._source_center_xy_mm = (0.0, 0.0)
        self._layer_z_values_mm: list[float] = []
        self._layer_paths: list[list[tuple[float, float, float, float, str]]] = []
        self._layer_ranges: list[tuple[str, int, int]] = []
        self._visible_path_roles = {
            "model", "outer_wall", "inner_wall", "top_surface", "bottom_surface",
            "infill", "solid_infill", "bridge", "skirt_brim",
            "support", "support_interface",
        }
        self._mesh_bounds = (-1.0, 1.0, -1.0, 1.0, -1.0, 1.0)
        self._path_bounds: tuple[float, float, float, float] | None = None
        self._view_scale = 0.78

    @staticmethod
    def _prepare_mesh(
        raw_vertices: Any, raw_faces: Any
    ) -> tuple[tuple[tuple[float, float, float], ...], tuple[tuple[int, int, int], ...]]:
        vertices = tuple(
            (float(item[0]), float(item[1]), float(item[2]))
            for item in raw_vertices
            if isinstance(item, (list, tuple)) and len(item) == 3
        )
        vertex_count = len(vertices)
        faces = tuple(
            (int(item[0]), int(item[1]), int(item[2]))
            for item in raw_faces
            if (
                isinstance(item, (list, tuple))
                and len(item) == 3
                and all(0 <= int(index) < vertex_count for index in item)
            )
        )
        return vertices, faces

    @staticmethod
    def _triangle_stream(
        vertices: tuple[tuple[float, float, float], ...],
        faces: tuple[tuple[int, int, int], ...],
    ) -> tuple[bytes, int]:
        stream = array("f")
        for first, second, third in faces:
            a, b, c = vertices[first], vertices[second], vertices[third]
            ux, uy, uz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
            vx, vy, vz = c[0] - a[0], c[1] - a[1], c[2] - a[2]
            nx = uy * vz - uz * vy
            ny = uz * vx - ux * vz
            nz = ux * vy - uy * vx
            length = math.sqrt(nx * nx + ny * ny + nz * nz)
            if length > 1e-12:
                nx, ny, nz = nx / length, ny / length, nz / length
            else:
                nx, ny, nz = 0.0, 0.0, 1.0
            for vertex in (a, b, c):
                stream.extend((vertex[0], vertex[1], vertex[2], nx, ny, nz))
        return stream.tobytes(), len(faces) * 3

    def set_model(self, path: Path | None, caption: str = "") -> None:
        self.clear_mesh(caption or (path.name if path else "Загрузите STL или 3MF"))

    def set_mesh(self, mesh: dict[str, Any] | None, caption: str) -> None:
        mesh = mesh or {}
        prepared = mesh.get("_vanior_prepared_mesh")
        if not isinstance(prepared, tuple) or len(prepared) != 4:
            detail_vertices, detail_faces = self._prepare_mesh(
                mesh.get("vertices", []), mesh.get("faces", [])
            )
            interactive_vertices, interactive_faces = self._prepare_mesh(
                mesh.get("interactive_vertices", mesh.get("vertices", [])),
                mesh.get("interactive_faces", mesh.get("faces", [])),
            )
            prepared = (
                detail_vertices,
                detail_faces,
                interactive_vertices,
                interactive_faces,
            )
            mesh["_vanior_prepared_mesh"] = prepared
        self._vertices, self._faces, self._interactive_vertices, self._interactive_faces = prepared

        gpu_payload = mesh.get("_vanior_gpu_payload")
        if not isinstance(gpu_payload, tuple) or len(gpu_payload) != 4:
            detail_blob, detail_count = self._triangle_stream(self._vertices, self._faces)
            interactive_blob, interactive_count = self._triangle_stream(
                self._interactive_vertices, self._interactive_faces
            )
            gpu_payload = (detail_blob, detail_count, interactive_blob, interactive_count)
            mesh["_vanior_gpu_payload"] = gpu_payload
        self._gpu_payload = gpu_payload
        self._gpu_dirty = True
        self._caption = caption
        self._layer_fraction = 1.0
        self._layer_index = 1
        self._layer_count = 1
        self._layer_z_mm = None
        self._layer_paths = []
        self._layer_z_values_mm = []
        self._layer_ranges = []
        self._support_blob = b""
        self._support_vertex_count = 0
        self._support_ranges = []
        self._support_gpu_dirty = True
        self._path_bounds = None
        source_center = mesh.get("source_center_mm")
        source_scale = mesh.get("source_scale_mm")
        self._source_center_z_mm = (
            float(source_center[2])
            if isinstance(source_center, (list, tuple)) and len(source_center) == 3
            else 0.0
        )
        self._source_scale_mm = (
            float(source_scale)
            if isinstance(source_scale, (int, float)) and float(source_scale) > 1e-9
            else None
        )
        self._source_center_xy_mm = (
            (float(source_center[0]), float(source_center[1]))
            if isinstance(source_center, (list, tuple)) and len(source_center) == 3
            else (0.0, 0.0)
        )
        self._layer_gpu_dirty = True
        if self._vertices:
            xs = [vertex[0] for vertex in self._vertices]
            ys = [vertex[1] for vertex in self._vertices]
            zs = [vertex[2] for vertex in self._vertices]
            self._mesh_bounds = (min(xs), max(xs), min(ys), max(ys), min(zs), max(zs))
            self._rebuild_plate_blob(
                preserve_plate_coordinates=(
                    str(mesh.get("coordinate_source", "")) == "ready_3mf_build"
                )
            )
            self._view_scale = self._calculate_initial_view_scale()
        self.reset_view()

    def _rebuild_plate_blob(self, *, preserve_plate_coordinates: bool) -> None:
        """Build a real 256 × 256 mm P1S plate in the model coordinate frame."""
        scale = self._source_scale_mm or 128.0
        width_mm, depth_mm, _ = self._build_volume_mm
        if preserve_plate_coordinates:
            center_x, center_y = self._source_center_xy_mm
        else:
            # Raw STL has no plate placement. VANIOR PRINT centers it before
            # slicing, so the preview shows that deterministic target position.
            center_x, center_y = width_mm / 2.0, depth_mm / 2.0
        self._plate_center_xy_mm = (center_x, center_y)
        left = (0.0 - center_x) / scale
        right = (width_mm - center_x) / scale
        back = (0.0 - center_y) / scale
        front = (depth_mm - center_y) / scale
        plate_z = self._mesh_bounds[4] - max(0.006, 0.35 / scale)
        self._plate_bounds = (left, right, back, front, plate_z)
        stream = array("f")

        def vertex(x: float, y: float, z: float) -> None:
            stream.extend((x, y, z, 0.0, 0.0, 1.0))

        for x, y in (
            (left, back), (right, back), (right, front),
            (left, back), (right, front), (left, front),
        ):
            vertex(x, y, plate_z)
        self._plate_surface_count = len(stream) // 6
        self._plate_grid_first = self._plate_surface_count
        for value in range(20, int(width_mm), 20):
            x = (float(value) - center_x) / scale
            vertex(x, back, plate_z + 0.001)
            vertex(x, front, plate_z + 0.001)
        for value in range(20, int(depth_mm), 20):
            y = (float(value) - center_y) / scale
            vertex(left, y, plate_z + 0.001)
            vertex(right, y, plate_z + 0.001)
        self._plate_grid_count = len(stream) // 6 - self._plate_grid_first
        self._plate_border_first = len(stream) // 6
        for start, end in (
            ((left, back), (right, back)),
            ((right, back), (right, front)),
            ((right, front), (left, front)),
            ((left, front), (left, back)),
        ):
            vertex(start[0], start[1], plate_z + 0.002)
            vertex(end[0], end[1], plate_z + 0.002)
        self._plate_border_count = len(stream) // 6 - self._plate_border_first
        self._plate_blob = stream.tobytes()
        self._plate_gpu_dirty = True
        scene_extent = max(right - left, front - back, 2.0)
        self._camera_distance = max(4.5, scene_extent * 2.8)

    def _calculate_initial_view_scale(self) -> float:
        """Fit disconnected 3MF plate objects without perspective clipping."""
        if not self._vertices:
            return 0.78
        yaw = math.radians(145.0)
        pitch = math.radians(22.0)
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)
        projected_x: list[float] = []
        projected_y: list[float] = []
        step = max(1, len(self._vertices) // 25_000)
        sample_vertices = list(self._vertices[::step])
        left, right, back, front, plate_z = self._plate_bounds
        sample_vertices.extend(
            ((left, back, plate_z), (right, back, plate_z),
             (right, front, plate_z), (left, front, plate_z))
        )
        for x, y, z in sample_vertices:
            horizontal = sy * y - cy * x
            depth = sy * x + cy * y
            vertical = cp * z - sp * depth
            camera_depth = sp * z + cp * depth
            perspective = self._camera_distance / max(
                1.0, self._camera_distance - camera_depth
            )
            projected_x.append(horizontal * perspective)
            projected_y.append(vertical * perspective)
        if not projected_x or not projected_y:
            return 0.78
        extent = max(
            max(projected_x) - min(projected_x),
            max(projected_y) - min(projected_y),
            1e-9,
        )
        return max(0.18, min(0.82, 1.24 / extent))

    def clear_mesh(self, caption: str) -> None:
        self._vertices = ()
        self._faces = ()
        self._interactive_vertices = ()
        self._interactive_faces = ()
        self._gpu_payload = (b"", 0, b"", 0)
        self._gpu_dirty = True
        self._layer_paths = []
        self._layer_z_values_mm = []
        self._layer_blob = b""
        self._layer_vertex_count = 0
        self._support_blob = b""
        self._support_vertex_count = 0
        self._support_ranges = []
        self._layer_gpu_dirty = True
        self._support_gpu_dirty = True
        self._source_center_z_mm = 0.0
        self._source_scale_mm = None
        self._source_center_xy_mm = (0.0, 0.0)
        self._layer_z_mm = None
        self._caption = caption
        self.update()

    def set_layer_paths(
        self,
        layers: list[Any] | None,
        z_mm: list[float] | None = None,
    ) -> None:
        self._layer_paths = []
        self._layer_z_values_mm = [float(value) for value in z_mm or []]
        all_x: list[float] = []
        all_y: list[float] = []
        for raw_layer in layers or []:
            layer: list[tuple[float, float, float, float, str]] = []
            if isinstance(raw_layer, (list, tuple)):
                for item in raw_layer:
                    if isinstance(item, (list, tuple)) and len(item) in {4, 5}:
                        segment = (
                            *(float(value) for value in item[:4]),
                            str(item[4]) if len(item) == 5 else "model",
                        )
                        layer.append(segment)
                        all_x.extend((segment[0], segment[2]))
                        all_y.extend((segment[1], segment[3]))
            self._layer_paths.append(layer)
        self._path_bounds = (
            (min(all_x), max(all_x), min(all_y), max(all_y)) if all_x and all_y else None
        )
        self._rebuild_support_blob()
        self._layer_gpu_dirty = True
        self._support_gpu_dirty = True
        self.update()

    def _layer_scene_z(self, index: int) -> float:
        minimum_z, maximum_z = self._mesh_bounds[4], self._mesh_bounds[5]
        if (
            0 <= index < len(self._layer_z_values_mm)
            and self._source_scale_mm is not None
        ):
            mapped = (
                self._layer_z_values_mm[index] - self._source_center_z_mm
            ) / self._source_scale_mm
            return max(minimum_z, min(maximum_z, mapped))
        return minimum_z + (maximum_z - minimum_z) * (index + 1) / max(
            1, len(self._layer_paths)
        )

    @staticmethod
    def _append_support_ribbon(
        stream: array,
        segment: tuple[float, float, float, float, str],
        z: float,
        half_width: float,
    ) -> None:
        x1, y1, x2, y2, _ = segment
        dx, dy = x2 - x1, y2 - y1
        length = math.hypot(dx, dy)
        if length <= 1e-10:
            return
        nx, ny = -dy / length * half_width, dx / length * half_width
        corners = (
            (x1 + nx, y1 + ny, z),
            (x1 - nx, y1 - ny, z),
            (x2 + nx, y2 + ny, z),
            (x2 - nx, y2 - ny, z),
        )
        for index in (0, 1, 2, 2, 1, 3):
            x, y, vertex_z = corners[index]
            stream.extend((x, y, vertex_z, 0.0, 0.0, 1.0))

    def _rebuild_support_blob(self) -> None:
        """Create printable-width ribbons for every support extrusion layer."""
        stream = array("f")
        self._support_ranges = []
        normalized_width = (
            0.23 / self._source_scale_mm
            if self._source_scale_mm is not None
            else 0.005
        )
        for role in ("support", "support_interface"):
            first = len(stream) // 6
            for layer_index, layer in enumerate(self._layer_paths):
                z = self._layer_scene_z(layer_index) + 0.001
                width = normalized_width * (1.18 if role == "support_interface" else 1.0)
                for segment in layer:
                    if segment[4] == role:
                        self._append_support_ribbon(stream, segment, z, width)
            count = len(stream) // 6 - first
            if count:
                self._support_ranges.append((role, first, count))
        self._support_blob = stream.tobytes()
        self._support_vertex_count = len(stream) // 6

    def set_path_role_visible(self, role: str, visible: bool) -> None:
        """Show or hide one toolpath family without reparsing the G-code."""
        if visible:
            self._visible_path_roles.add(role)
        else:
            self._visible_path_roles.discard(role)
        self.update()

    def set_layer(
        self,
        layer: int,
        total: int,
        *,
        fraction: float | None = None,
        z_mm: float | None = None,
    ) -> None:
        total = max(1, int(total))
        layer = max(1, min(total, int(layer)))
        self._layer_index = layer
        self._layer_count = total
        self._layer_fraction = max(
            0.0, min(1.0, float(fraction) if fraction is not None else layer / total)
        )
        self._layer_z_mm = float(z_mm) if z_mm is not None else None
        self._layer_gpu_dirty = True
        self.update()

    def _selected_layer_scene_z(self) -> float:
        minimum_z, maximum_z = self._mesh_bounds[4], self._mesh_bounds[5]
        if self._layer_index >= self._layer_count:
            return maximum_z
        if self._layer_z_mm is not None and self._source_scale_mm is not None:
            mapped = (self._layer_z_mm - self._source_center_z_mm) / self._source_scale_mm
            return max(minimum_z, min(maximum_z, mapped))
        return minimum_z + (maximum_z - minimum_z) * self._layer_fraction

    def reset_view(self) -> None:
        self._zoom_animation.stop()
        self._yaw = 145.0
        self._pitch = 22.0
        self._zoom = 1.0
        self._zoom_target = 1.0
        self._pan = QPointF(0.0, 0.0)
        self.update()

    def begin_interaction(self) -> None:
        self._interaction_active = True

    def end_interaction(self) -> None:
        self._interaction_active = False

    def rotate_view(self, horizontal_delta: float, vertical_delta: float) -> None:
        self._yaw = (self._yaw + horizontal_delta * 0.65) % 360.0
        self._pitch = max(-88.0, min(88.0, self._pitch + vertical_delta * 0.55))
        self.update()

    def zoom_view(self, steps: float) -> None:
        self.end_interaction()
        self._zoom = max(0.25, min(5.0, self._zoom * (1.12 ** float(steps))))
        self._zoom_target = self._zoom
        self.update()

    def queue_zoom(self, steps: float) -> None:
        """Accumulate wheel input and ease the camera to the requested scale."""
        self.end_interaction()
        self._zoom_target = max(
            0.25, min(5.0, self._zoom_target * (1.12 ** float(steps)))
        )
        if not self._zoom_animation.isActive():
            self._zoom_animation.start()

    def _animate_zoom(self) -> None:
        difference = self._zoom_target - self._zoom
        if abs(difference) <= max(0.0005, self._zoom_target * 0.0005):
            self._zoom = self._zoom_target
            self._zoom_animation.stop()
        else:
            self._zoom += difference * 0.34
        self.update()

    def wheelEvent(self, event: Any) -> None:
        delta = event.pixelDelta().y()
        steps = delta / 120.0 if delta else event.angleDelta().y() / 120.0
        self.queue_zoom(steps)
        event.accept()

    def mousePressEvent(self, event: Any) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._zoom_animation.stop()
            self._zoom_target = self._zoom
            if event.button() == Qt.MouseButton.LeftButton:
                self.begin_interaction()
            self._last_pointer = event.position()
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: Any) -> None:
        if self._last_pointer is None:
            super().mouseMoveEvent(event)
            return
        delta = event.position() - self._last_pointer
        self._last_pointer = event.position()
        if event.buttons() & Qt.MouseButton.LeftButton:
            self.rotate_view(delta.x(), delta.y())
        elif event.buttons() & Qt.MouseButton.RightButton:
            self._pan += QPointF(delta.x(), delta.y())
            self.update()
        event.accept()

    def mouseReleaseEvent(self, event: Any) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._last_pointer = None
            self.unsetCursor()
            self.end_interaction()
            self.update()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: Any) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self.reset_view()
            event.accept()
            return
        super().mouseDoubleClickEvent(event)

    @staticmethod
    def _clip_polygon_to_z(
        polygon: list[tuple[float, float, float]], cutoff: float
    ) -> list[tuple[float, float, float]]:
        if not polygon:
            return []
        result: list[tuple[float, float, float]] = []
        previous = polygon[-1]
        previous_inside = previous[2] <= cutoff + 1e-8
        for current in polygon:
            current_inside = current[2] <= cutoff + 1e-8
            if current_inside != previous_inside:
                denominator = current[2] - previous[2]
                amount = 0.0 if abs(denominator) <= 1e-12 else (cutoff - previous[2]) / denominator
                result.append(
                    (
                        previous[0] + (current[0] - previous[0]) * amount,
                        previous[1] + (current[1] - previous[1]) * amount,
                        cutoff,
                    )
                )
            if current_inside:
                result.append(current)
            previous = current
            previous_inside = current_inside
        return result

    def initializeGL(self) -> None:
        try:
            self._program = QOpenGLShaderProgram(self)
            if not self._program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Vertex, self._VERTEX_SHADER
            ):
                raise RuntimeError(self._program.log())
            if not self._program.addShaderFromSourceCode(
                QOpenGLShader.ShaderTypeBit.Fragment, self._FRAGMENT_SHADER
            ):
                raise RuntimeError(self._program.log())
            if not self._program.link():
                raise RuntimeError(self._program.log())
            self._detail_vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self._interactive_vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self._layer_vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self._support_vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            self._plate_vbo = QOpenGLBuffer(QOpenGLBuffer.Type.VertexBuffer)
            for buffer in (
                self._detail_vbo,
                self._interactive_vbo,
                self._layer_vbo,
                self._support_vbo,
                self._plate_vbo,
            ):
                if not buffer.create():
                    raise RuntimeError("Не удалось создать буфер OpenGL")
            self.context().aboutToBeDestroyed.connect(self._cleanup_gl)
            self._gl_ready = True
            self._gpu_dirty = True
            self._layer_gpu_dirty = True
            self._support_gpu_dirty = True
            self._plate_gpu_dirty = True
        except Exception as error:
            self._gl_ready = False
            self._gl_error = str(error)

    def _cleanup_gl(self) -> None:
        """Release video memory while the owning OpenGL context is current."""
        try:
            self.makeCurrent()
            for buffer in (
                self._detail_vbo,
                self._interactive_vbo,
                self._layer_vbo,
                self._support_vbo,
                self._plate_vbo,
            ):
                if buffer is not None and buffer.isCreated():
                    buffer.destroy()
            if self._program is not None:
                self._program.removeAllShaders()
            self.doneCurrent()
        except RuntimeError:
            pass
        self._detail_vbo = None
        self._interactive_vbo = None
        self._layer_vbo = None
        self._support_vbo = None
        self._plate_vbo = None
        self._program = None
        self._gl_ready = False
        self._gpu_dirty = True
        self._layer_gpu_dirty = True
        self._support_gpu_dirty = True
        self._plate_gpu_dirty = True

    @staticmethod
    def _upload(buffer: QOpenGLBuffer, payload: bytes) -> None:
        if not buffer.bind():
            raise RuntimeError("Не удалось привязать буфер OpenGL")
        buffer.allocate(payload, len(payload))
        if buffer.size() != len(payload):
            raise RuntimeError(
                f"Буфер OpenGL загружен не полностью: {buffer.size()} из {len(payload)} байт"
            )
        buffer.release()

    def _upload_pending_buffers(self) -> None:
        if not self._gl_ready:
            return
        if self._gpu_dirty:
            detail_blob, _, interactive_blob, _ = self._gpu_payload
            assert self._detail_vbo is not None and self._interactive_vbo is not None
            self._upload(self._detail_vbo, detail_blob)
            self._upload(self._interactive_vbo, interactive_blob)
            self._gpu_dirty = False
        if self._layer_gpu_dirty:
            self._rebuild_layer_blob()
            assert self._layer_vbo is not None
            self._upload(self._layer_vbo, self._layer_blob)
            self._layer_gpu_dirty = False
        if self._support_gpu_dirty:
            assert self._support_vbo is not None
            self._upload(self._support_vbo, self._support_blob)
            self._support_gpu_dirty = False
        if self._plate_gpu_dirty:
            assert self._plate_vbo is not None
            self._upload(self._plate_vbo, self._plate_blob)
            self._plate_gpu_dirty = False

    def _rebuild_layer_blob(self) -> None:
        stream = array("f")
        self._layer_ranges = []
        path_index = self._layer_index - 1
        if (
            0 <= path_index < len(self._layer_paths)
            and self._vertices
        ):
            z = self._selected_layer_scene_z() + 0.002
            grouped: dict[str, list[tuple[float, float, float, float, str]]] = {}
            for segment in self._layer_paths[path_index]:
                grouped.setdefault(segment[4], []).append(segment)
            for role in (
                "skirt_brim", "infill", "solid_infill", "model", "inner_wall",
                "outer_wall", "bottom_surface", "top_surface", "bridge",
                "support", "support_interface",
            ):
                role_segments = grouped.get(role, [])
                first = len(stream) // 6
                for x1, y1, x2, y2, _ in role_segments:
                    for x, y in ((x1, y1), (x2, y2)):
                        stream.extend(
                            (
                                x,
                                y,
                                z,
                                0.0,
                                0.0,
                                1.0,
                            )
                        )
                count = len(stream) // 6 - first
                if count:
                    self._layer_ranges.append((role, first, count))
        self._layer_blob = stream.tobytes()
        self._layer_vertex_count = len(stream) // 6

    def _set_common_uniforms(self) -> None:
        assert self._program is not None
        functions = self.context().functions()
        width = max(1, self.width())
        height = max(1, self.height())
        shortest = float(min(width, height))
        cutoff = self._selected_layer_scene_z()
        functions.glUniform1f(self._program.uniformLocation("yawAngle"), math.radians(self._yaw))
        functions.glUniform1f(
            self._program.uniformLocation("pitchAngle"), math.radians(self._pitch)
        )
        functions.glUniform1f(self._program.uniformLocation("zoomFactor"), float(self._zoom))
        functions.glUniform1f(
            self._program.uniformLocation("cameraDistance"), float(self._camera_distance)
        )
        functions.glUniform2f(
            self._program.uniformLocation("viewportScale"),
            self._view_scale * shortest / width,
            self._view_scale * shortest / height,
        )
        functions.glUniform2f(
            self._program.uniformLocation("panOffset"),
            2.0 * self._pan.x() / width,
            -2.0 * self._pan.y() / height,
        )
        functions.glUniform1f(self._program.uniformLocation("layerCutoff"), float(cutoff))

    def _draw_buffer(
        self,
        buffer: QOpenGLBuffer,
        vertex_count: int,
        mode: int,
        *,
        first_vertex: int = 0,
    ) -> None:
        if not vertex_count or self._program is None:
            return
        functions = self.context().functions()
        position = self._program.attributeLocation(b"vertexPosition")
        normal = self._program.attributeLocation(b"vertexNormal")
        buffer.bind()
        self._program.enableAttributeArray(position)
        self._program.enableAttributeArray(normal)
        self._program.setAttributeBuffer(position, 0x1406, 0, 3, 24)
        self._program.setAttributeBuffer(normal, 0x1406, 12, 3, 24)
        functions.glDrawArrays(mode, first_vertex, vertex_count)
        self._program.disableAttributeArray(position)
        self._program.disableAttributeArray(normal)
        buffer.release()

    def _paint_software_fallback(self, painter: QPainter) -> None:
        """Keep the preview usable on machines without a working GL driver."""
        vertices = self._interactive_vertices or self._vertices
        faces = self._interactive_faces or self._faces
        if not vertices or not faces:
            return
        yaw = math.radians(self._yaw)
        pitch = math.radians(self._pitch)
        cy, sy = math.cos(yaw), math.sin(yaw)
        cp, sp = math.cos(pitch), math.sin(pitch)

        def transform(point: tuple[float, float, float]) -> tuple[float, float, float]:
            x, y, z = point
            horizontal = sy * y - cy * x
            depth = sy * x + cy * y
            return horizontal, cp * z - sp * depth, sp * z + cp * depth

        transformed = []
        for vertex in vertices:
            transformed.append(transform(vertex))
        width, height = max(1, self.width()), max(1, self.height())
        scale = min(width, height) * 0.39 * self._zoom
        center = QPointF(width / 2.0, height / 2.0) + self._pan

        def project(point: tuple[float, float, float]) -> QPointF:
            perspective = self._camera_distance / max(
                1.0, self._camera_distance - point[2]
            )
            return QPointF(
                center.x() + point[0] * scale * perspective,
                center.y() - point[1] * scale * perspective,
            )

        left, right, back, front, plate_z = self._plate_bounds
        plate_points = [
            project(transform((left, back, plate_z))),
            project(transform((right, back, plate_z))),
            project(transform((right, front, plate_z))),
            project(transform((left, front, plate_z))),
        ]
        painter.setPen(QPen(QColor("#61338e"), 1.4))
        painter.setBrush(QColor(18, 20, 37, 235))
        painter.drawPolygon(QPolygonF(plate_points))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor(78, 61, 105, 115), 0.8))
        scale_mm = self._source_scale_mm or 128.0
        center_x, center_y = self._plate_center_xy_mm
        for value in range(20, int(self._build_volume_mm[0]), 20):
            x = (value - center_x) / scale_mm
            painter.drawLine(
                project(transform((x, back, plate_z + 0.001))),
                project(transform((x, front, plate_z + 0.001))),
            )
        for value in range(20, int(self._build_volume_mm[1]), 20):
            y = (value - center_y) / scale_mm
            painter.drawLine(
                project(transform((left, y, plate_z + 0.001))),
                project(transform((right, y, plate_z + 0.001))),
            )

        cutoff = self._selected_layer_scene_z()
        visible = []
        step = max(1, math.ceil(len(faces) / 4_000))
        for face in faces[::step]:
            if all(vertices[index][2] <= cutoff + 1e-8 for index in face):
                points = [transformed[index] for index in face]
                visible.append((sum(point[2] for point in points) / 3.0, points))
        visible.sort(key=lambda item: item[0])
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#8534d9"))
        for _, points in visible:
            painter.drawPolygon(QPolygonF([project(point) for point in points]))

        path_index = self._layer_index - 1
        if path_index >= 0 and self._layer_paths:
            # The fallback renderer must preserve the same slicer semantics as
            # the GPU path: supports form a cumulative structure from the bed
            # through the selected layer, instead of flashing only one contour.
            support_budget = 35_000
            support_segments = sum(
                1
                for layer in self._layer_paths[: path_index + 1]
                for segment in layer
                if segment[4] in {"support", "support_interface"}
            )
            support_step = max(1, math.ceil(support_segments / support_budget))
            support_seen = 0
            support_colors = {
                "support": QColor("#35db69"),
                "support_interface": QColor("#ffe66e"),
            }
            for layer_index, layer in enumerate(self._layer_paths[: path_index + 1]):
                path_z = self._layer_scene_z(layer_index) + 0.001
                for x1, y1, x2, y2, role in layer:
                    if role not in support_colors or role not in self._visible_path_roles:
                        continue
                    if support_seen % support_step == 0:
                        painter.setPen(QPen(support_colors[role], 2.0))
                        painter.drawLine(
                            project(transform((x1, y1, path_z))),
                            project(transform((x2, y2, path_z))),
                        )
                    support_seen += 1

        if (
            0 <= path_index < len(self._layer_paths)
        ):
            colors = {
                "model": "#66b8ff", "outer_wall": "#fa576f",
                "inner_wall": "#ff9e47", "top_surface": "#ffb840",
                "bottom_surface": "#59d1ff", "infill": "#a66beb",
                "solid_infill": "#cc8aff", "bridge": "#ff66b8",
                "skirt_brim": "#8c94a8", "support": "#4deb85",
                "support_interface": "#ffd14d",
            }
            path_z = self._selected_layer_scene_z() + 0.002
            for x1, y1, x2, y2, role in self._layer_paths[path_index]:
                if role not in self._visible_path_roles:
                    continue
                left = (x1, y1, path_z)
                right = (x2, y2, path_z)
                painter.setPen(QPen(QColor(colors.get(role, colors["model"])), 1.35))
                painter.drawLine(project(transform(left)), project(transform(right)))

    def paintGL(self) -> None:
        functions = self.context().functions()
        functions.glViewport(0, 0, self.width(), self.height())
        functions.glClearColor(0.025, 0.018, 0.07, 1.0)
        functions.glClear(0x00004000 | 0x00000100)

        if self._gl_ready and self._program is not None:
            self._upload_pending_buffers()
            functions.glEnable(0x0B71)
            functions.glEnable(0x0BE2)
            functions.glBlendFunc(0x0302, 0x0303)
            self._program.bind()
            self._set_common_uniforms()
            if self._plate_surface_count and self._plate_vbo is not None:
                # The printer bed is real scene geometry, so camera movement,
                # perspective and clipping match the printed model exactly.
                functions.glUniform1f(self._program.uniformLocation("unlitMode"), 1.0)
                functions.glUniform4f(
                    self._program.uniformLocation("baseColor"), 0.055, 0.063, 0.11, 1.0
                )
                self._draw_buffer(
                    self._plate_vbo, self._plate_surface_count, 0x0004
                )
                functions.glUniform4f(
                    self._program.uniformLocation("baseColor"), 0.24, 0.18, 0.33, 0.72
                )
                functions.glLineWidth(1.0)
                self._draw_buffer(
                    self._plate_vbo,
                    self._plate_grid_count,
                    0x0001,
                    first_vertex=self._plate_grid_first,
                )
                functions.glUniform4f(
                    self._program.uniformLocation("baseColor"), 0.49, 0.23, 0.78, 1.0
                )
                functions.glLineWidth(2.0)
                self._draw_buffer(
                    self._plate_vbo,
                    self._plate_border_count,
                    0x0001,
                    first_vertex=self._plate_border_first,
                )
            functions.glUniform1f(self._program.uniformLocation("unlitMode"), 0.0)
            functions.glUniform4f(
                self._program.uniformLocation("baseColor"), 0.61, 0.30, 1.0, 1.0
            )
            detail_blob, detail_count, interactive_blob, interactive_count = self._gpu_payload
            # Even 100k triangles are inexpensive once resident on the GPU.  Keep
            # the detailed silhouette throughout interaction instead of visibly
            # switching levels of detail.
            if detail_blob and self._detail_vbo is not None:
                self._draw_buffer(self._detail_vbo, detail_count, 0x0004)
            elif interactive_blob and self._interactive_vbo is not None:
                self._draw_buffer(self._interactive_vbo, interactive_count, 0x0004)
            if self._support_vertex_count and self._support_vbo is not None:
                # Reconstruct the complete support structure from every
                # extrusion below the selected layer. Printable-width ribbons
                # make tree trunks and interfaces visible as real 3D geometry.
                functions.glUniform1f(self._program.uniformLocation("unlitMode"), 2.0)
                support_colors = {
                    # A translucent structure keeps the protected model
                    # readable while still exposing every accumulated support
                    # extrusion, as in a production slicer's layer preview.
                    "support": (0.22, 0.92, 0.52, 0.30),
                    "support_interface": (1.0, 0.82, 0.28, 0.46),
                }
                for role, first, count in self._support_ranges:
                    if role not in self._visible_path_roles:
                        continue
                    red, green, blue, alpha = support_colors[role]
                    functions.glUniform4f(
                        self._program.uniformLocation("baseColor"),
                        red,
                        green,
                        blue,
                        alpha,
                    )
                    self._draw_buffer(
                        self._support_vbo,
                        count,
                        0x0004,
                        first_vertex=first,
                    )
            if self._layer_vertex_count and self._layer_vbo is not None:
                # The selected layer is the primary slicer view. Draw it over
                # the clipped reference mesh so internal infill and supports
                # remain readable instead of disappearing in the depth buffer.
                functions.glDisable(0x0B71)
                functions.glUniform1f(self._program.uniformLocation("unlitMode"), 1.0)
                colors = {
                    "model": (0.40, 0.72, 1.0, 1.0),
                    "outer_wall": (0.98, 0.34, 0.44, 1.0),
                    "inner_wall": (1.0, 0.62, 0.28, 1.0),
                    "top_surface": (1.0, 0.72, 0.25, 1.0),
                    "bottom_surface": (0.35, 0.82, 1.0, 1.0),
                    "infill": (0.65, 0.42, 0.92, 1.0),
                    "solid_infill": (0.80, 0.54, 1.0, 1.0),
                    "bridge": (1.0, 0.40, 0.72, 1.0),
                    "skirt_brim": (0.55, 0.58, 0.66, 1.0),
                    "support": (0.30, 0.92, 0.52, 1.0),
                    "support_interface": (1.0, 0.82, 0.30, 1.0),
                }
                functions.glLineWidth(1.7)
                for role, first, count in self._layer_ranges:
                    if role not in self._visible_path_roles:
                        continue
                    red, green, blue, alpha = colors.get(role, colors["model"])
                    functions.glUniform4f(
                        self._program.uniformLocation("baseColor"),
                        red, green, blue, alpha,
                    )
                    self._draw_buffer(
                        self._layer_vbo,
                        count,
                        0x0001,
                        first_vertex=first,
                    )
                functions.glEnable(0x0B71)
            self._program.release()

        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self._gl_error:
            self._paint_software_fallback(painter)
        painter.setPen(QPen(QColor(86, 48, 128, 115), 1))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -2, -2), 11, 11)
        painter.setPen(QColor("#b9aec9"))
        painter.drawText(
            self.rect().adjusted(14, 10, -14, -10),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
            "ЛКМ — вращение   •   колесо — масштаб   •   ПКМ — перемещение   •   двойной щелчок — сброс   •   GPU",
        )
        if self._vertices:
            painter.setPen(QColor("#8e86a2"))
            painter.drawText(
                self.rect().adjusted(14, 34, -14, -10),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft,
                "Стол Bambu Lab P1S  •  256 × 256 мм  •  шаг сетки 20 мм",
            )
        if self._layer_count > 1:
            layer_text = f"Слой {self._layer_index} / {self._layer_count}"
            if self._layer_z_mm is not None:
                layer_text += f"   •   Z {self._layer_z_mm:.2f} мм"
            painter.drawText(
                self.rect().adjusted(14, 10, -14, -10),
                Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignRight,
                layer_text,
            )
        if self._gl_error:
            painter.setPen(QColor("#ffca72"))
            painter.drawText(
                self.rect().adjusted(14, 34, -14, -10),
                Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
                "Безопасный режим 3D: обновите драйвер видеокарты для максимальной плавности.",
            )
        elif not self._vertices:
            painter.setPen(QColor("#9b4dff"))
            painter.setFont(QFont("Segoe UI", 42))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "◇")
        painter.setFont(QFont("Segoe UI", 9))
        painter.setPen(QColor("#a99fba"))
        painter.drawText(
            self.rect().adjusted(12, 0, -12, -10),
            Qt.AlignmentFlag.AlignBottom | Qt.AlignmentFlag.AlignHCenter,
            self._caption,
        )
        painter.end()


class MetricCard(QFrame):
    def __init__(self, title: str, value: str = "—") -> None:
        super().__init__()
        self.setObjectName("metricCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        label = QLabel(title)
        label.setObjectName("muted")
        self.value = QLabel(value)
        self.value.setObjectName("metricValue")
        layout.addWidget(label)
        layout.addWidget(self.value)


class LearningSyncWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(self, local_store: Path, global_store: Path, endpoint: str) -> None:
        super().__init__()
        self.local_store = local_store
        self.global_store = global_store
        self.endpoint = endpoint

    def run(self) -> None:
        try:
            self.succeeded.emit(
                sync_community_learning(self.local_store, self.global_store, self.endpoint)
            )
        except Exception as exc:
            self.failed.emit(str(exc))


class PrinterActionWorker(QThread):
    """Keep direct printer I/O away from the GUI event loop."""

    progress = Signal(str, int)
    succeeded = Signal(object)
    failed = Signal(str, str)

    def __init__(
        self,
        action: str,
        config: PrinterConnectionConfig,
        *,
        package: Path | None = None,
        options: PrintDispatchOptions | None = None,
    ) -> None:
        super().__init__()
        self.action = action
        self.config = config
        self.package = package
        self.options = options or PrintDispatchOptions()

    def run(self) -> None:
        try:
            client = LocalP1SClient(
                self.config,
                progress_callback=lambda message, percent: self.progress.emit(
                    message, percent
                ),
            )
            if self.action == "test":
                result: object = client.test_connection()
            elif self.action == "print" and self.package is not None:
                result = client.send_print(self.package, self.options)
            else:
                raise ValueError("неизвестная операция с принтером")
            self.succeeded.emit(result)
        except Exception as exc:
            self.failed.emit(str(exc), traceback.format_exc())


def _job_fingerprint(options: dict[str, Any]) -> str:
    """Bind a ready result to the exact model and user-visible print choices."""
    source = Path(str(options.get("source", ""))).expanduser().resolve()
    payload = {
        key: options.get(key)
        for key in (
            "material", "material_profile", "printer", "nozzle", "bed_type", "print_priority",
            "model_purpose", "functional_intent", "plate", "overhang_angle_deg",
            "support_strategy", "print_setting_overrides", "print_dna_profile",
            "quality_search", "slicer_backend",
        )
    }
    payload["source_sha256"] = workspace_sha256_file(source) if source.is_file() else ""
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    ).hexdigest()


class MainWindow(QMainWindow):
    PAGE_TRANSITION_MS = 210
    PAGE_TRANSITION_OFFSET = 18

    PAGE_NAMES = (
        ("overview", "⌂", "Обзор"),
        ("analysis", "◇", "Анализ модели"),
        ("print", "▦", "Настройки печати"),
        ("preview", "▣", "Предпросмотр"),
        ("optimization", "↗", "Оптимизация"),
        ("export", "⇧", "Экспорт проекта"),
        ("history", "◴", "История"),
        ("print_dna", "✦", "PrintDNA"),
        ("printers", "▤", "Профили принтеров"),
        ("settings", "⚙", "Настройки"),
    )

    def __init__(self) -> None:
        super().__init__()
        self.settings = QSettings(ORGANIZATION, APP_NAME)
        self.workspace = ensure_workspace()
        _migrate_legacy_data(self.workspace)
        self.thread: QThread | None = None
        self.worker: JobWorker | None = None
        self.learning_sync_worker: LearningSyncWorker | None = None
        self.printer_worker: PrinterActionWorker | None = None
        self._printer_mqtt_fingerprint = ""
        self._printer_ftps_fingerprint = ""
        self._printer_trusted_identity = ""
        self._printer_retest_after_trust = False
        self.cancel_event: Event | None = None
        self.last_result: dict[str, Any] | None = None
        self.scan_result: dict[str, Any] | None = None
        self.close_when_finished = False
        self.pending_export: str | None = None
        self.manual_settings_enabled = False
        self._layer_heights_mm: list[float] = []
        self.history_path = self.workspace.history
        self.print_dna_path = self.workspace.print_dna
        self.history: list[dict[str, Any]] = _read_json(self.history_path, [])
        if not isinstance(self.history, list):
            self.history = []
        self.setWindowTitle(f"{APP_NAME} {__version__}")
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        self.resize(1536, 960)
        self.setMinimumSize(1180, 900)
        self.setAcceptDrops(True)
        self._nav_buttons: dict[str, QPushButton] = {}
        self._page_indexes: dict[str, int] = {}
        self._page_transition: QParallelAnimationGroup | None = None
        self._page_transition_widget: QWidget | None = None
        self._page_transition_effect: QGraphicsOpacityEffect | None = None
        self._page_transition_origin: QPoint | None = None
        self._canvases: list[ModelCanvas] = []
        self._build_ui()
        self._apply_style()
        self._restore_settings()
        self._refresh_history()
        self._refresh_overview()
        self._update_ui_state()
        self._show_page("overview")
        if self.community_learning_check.isChecked() and self.community_endpoint_edit.text().strip():
            QTimer.singleShot(1800, lambda: self._start_community_sync(quiet=True))

    def _show_support_prompt_once(self) -> None:
        """Offer the optional Boosty link once, without blocking future starts."""
        if self.settings.value(SUPPORT_PROMPT_SETTINGS_KEY, False, bool):
            return
        self.settings.setValue(SUPPORT_PROMPT_SETTINGS_KEY, True)
        self.settings.sync()

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Поддержать VANIOR PRINT")
        box.setText("Нравится VANIOR PRINT?")
        box.setInformativeText(
            "Вы можете поддержать развитие проекта на Boosty. "
            "Поддержка добровольная и не влияет на работу приложения.\n\n"
            "Это сообщение показывается только один раз."
        )
        support_button = box.addButton(
            "Поддержать на Boosty", QMessageBox.ButtonRole.ActionRole
        )
        box.addButton("Продолжить", QMessageBox.ButtonRole.AcceptRole)
        box.exec()
        if box.clickedButton() is support_button:
            QDesktopServices.openUrl(QUrl(SUPPORT_URL))

    # ---------- layout helpers ----------
    def _card(self, title: str | None = None) -> tuple[QFrame, QVBoxLayout]:
        frame = QFrame()
        frame.setObjectName("card")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(16, 14, 16, 14)
        layout.setSpacing(10)
        if title:
            label = QLabel(title)
            label.setObjectName("sectionTitle")
            layout.addWidget(label)
        return frame, layout

    def _page(self, title: str, subtitle: str, action: QWidget | None = None) -> tuple[QScrollArea, QVBoxLayout]:
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        content = QWidget()
        content.setObjectName("pageContent")
        content.setMinimumWidth(760)
        layout = QVBoxLayout(content)
        layout.setContentsMargins(34, 26, 34, 28)
        layout.setSpacing(16)
        header_widget = QWidget()
        header_widget.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        header = QHBoxLayout(header_widget)
        header.setContentsMargins(0, 0, 0, 0)
        texts = QVBoxLayout()
        heading = QLabel(title)
        heading.setObjectName("pageTitle")
        if APP_NAME in title:
            heading.setText(
                title.replace(APP_NAME, f'<span style="color:#a95aff">{APP_NAME}</span>')
            )
            heading.setTextFormat(Qt.TextFormat.RichText)
        sub = QLabel(subtitle)
        sub.setObjectName("subtitle")
        sub.setWordWrap(True)
        texts.addWidget(heading)
        texts.addWidget(sub)
        header.addLayout(texts, 1)
        if action:
            header.addWidget(action, 0, Qt.AlignmentFlag.AlignTop)
        layout.addWidget(header_widget)
        scroll.setWidget(content)
        return scroll, layout

    def _primary_button(self, text: str, callback: Callable[[], None]) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("primary")
        button.setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Fixed)
        button.setMinimumWidth(max(150, button.fontMetrics().horizontalAdvance(text) + 42))
        button.clicked.connect(callback)
        return button

    def _path_row(self, edit: QLineEdit, callback: Callable[[], None]) -> QWidget:
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)
        browse = QPushButton("Обзор…")
        browse.setMinimumWidth(92)
        browse.clicked.connect(callback)
        layout.addWidget(edit, 1)
        layout.addWidget(browse)
        return row

    def _build_ui(self) -> None:
        central = QWidget()
        central.setObjectName("appRoot")
        outer = QHBoxLayout(central)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_sidebar())
        self.pages = QStackedWidget()
        outer.addWidget(self.pages, 1)
        self.setCentralWidget(central)

        builders = {
            "overview": self._build_overview,
            "analysis": self._build_analysis,
            "print": self._build_print_settings,
            "preview": self._build_preview,
            "optimization": self._build_optimization,
            "export": self._build_export,
            "history": self._build_history,
            "print_dna": self._build_print_dna,
            "printers": self._build_printers,
            "settings": self._build_settings,
        }
        for key, _, _ in self.PAGE_NAMES:
            page = builders[key]()
            self._page_indexes[key] = self.pages.addWidget(page)

    def _build_sidebar(self) -> QWidget:
        sidebar = QFrame()
        sidebar.setObjectName("sidebar")
        sidebar.setAccessibleName("Боковое меню")
        sidebar.setFixedWidth(76)
        layout = QVBoxLayout(sidebar)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(4)

        brand_icon = QLabel()
        brand_icon.setObjectName("sidebarBrand")
        icon_pixmap = QPixmap(str(ICON_PATH))
        if not icon_pixmap.isNull():
            brand_icon.setPixmap(
                icon_pixmap.scaled(
                    34,
                    34,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )
        brand_icon.setFixedSize(48, 48)
        brand_icon.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_icon.setToolTip(f"{APP_NAME} v{__version__}")
        brand_icon.setAccessibleName(f"{APP_NAME}, версия {__version__}")
        layout.addWidget(brand_icon)
        layout.addSpacing(7)

        for key, _, label in self.PAGE_NAMES:
            button = ImmediateToolTipButton()
            button.setObjectName("navRailButton")
            button.setCheckable(True)
            button.setProperty("pageKey", key)
            button.setIcon(_nav_icon(key))
            button.setIconSize(QSize(36, 36))
            button.setFixedSize(48, 48)
            button.setCursor(Qt.CursorShape.PointingHandCursor)
            button.setToolTip(label)
            button.setStatusTip(label)
            button.setAccessibleName(label)
            button.clicked.connect(lambda checked=False, selected=key: self._show_page(selected))
            self._nav_buttons[key] = button
            layout.addWidget(button)
        layout.addStretch(1)

        divider = QFrame()
        divider.setObjectName("sidebarDivider")
        divider.setFixedHeight(1)
        layout.addWidget(divider)
        layout.addSpacing(4)

        self.sidebar_printer = SidebarQuickAction("printer_head", "Bambu Lab P1S", "Готов к работе")
        self.sidebar_printer.clicked.connect(self._choose_printer_hardware)
        layout.addWidget(self.sidebar_printer)
        self.sidebar_material = SidebarQuickAction("material", "PLA  •  сопло 0,4 мм", "Текущий профиль")
        self.sidebar_material.clicked.connect(self._choose_material_profile)
        layout.addWidget(self.sidebar_material)
        return sidebar

    # ---------- pages ----------
    def _build_overview(self) -> QWidget:
        new_project = self._primary_button("＋  Новый проект", self._browse_model)
        scroll, layout = self._page(
            f"Добро пожаловать в {APP_NAME}",
            "Умный помощник для анализа, оптимизации и подготовки 3D-моделей к печати",
            new_project,
        )
        self.scroll_area = scroll  # compatibility and screenshot automation
        self.project_state = QLabel("○  Новый проект  •  выберите STL или 3MF, чтобы начать")
        self.project_state.setObjectName("projectState")
        layout.addWidget(self.project_state)
        top = QGridLayout()
        top.setHorizontalSpacing(16)
        top.setVerticalSpacing(14)
        self.drop_zone = DropZone()
        self.drop_zone.clicked.connect(self._browse_model)
        self.drop_zone.fileSelected.connect(self._model_selected)
        top.addWidget(self.drop_zone, 0, 0)

        recent_card = QFrame()
        recent_card.setObjectName("card")
        recent_layout = QVBoxLayout(recent_card)
        recent_layout.setContentsMargins(18, 16, 18, 16)
        recent_layout.setSpacing(10)
        recent_header = QHBoxLayout()
        recent_title = QLabel("Последние проекты")
        recent_title.setObjectName("sectionTitle")
        all_projects = QPushButton("Все проекты  →")
        all_projects.setObjectName("linkButton")
        all_projects.clicked.connect(lambda: self._show_page("history"))
        recent_header.addWidget(recent_title)
        recent_header.addStretch(1)
        recent_header.addWidget(all_projects)
        recent_layout.addLayout(recent_header)
        self.recent_list = QListWidget()
        self.recent_list.setObjectName("recentList")
        self.recent_list.itemDoubleClicked.connect(self._open_history_item)
        recent_layout.addWidget(self.recent_list)
        top.addWidget(recent_card, 0, 1)

        top.setColumnStretch(0, 5)
        top.setColumnStretch(1, 4)
        layout.addLayout(top)

        # The large drop zone above is the single visible model picker.  Keep
        # these controls as hidden state holders because the processing flow
        # and saved-project restoration use their values.
        self.model_edit = DropLineEdit(SUPPORTED_MODEL_SUFFIXES, scroll)
        self.model_edit.setPlaceholderText("Перетащите сюда STL или 3MF")
        self.model_edit.setObjectName("modelPath")
        self.model_edit.setMinimumHeight(32)
        self.model_edit.setVisible(False)
        self.model_edit.fileDropped.connect(self._model_selected)
        self.mode_combo = QComboBox(scroll)
        self.mode_combo.addItem("Полная оптимизация и один готовый 3MF", "optimize")
        self.mode_combo.addItem("Только проверить модель", "scan")
        self.mode_combo.addItem("Нарезать исходный 3MF без исправления", "slice")
        self.mode_combo.setVisible(False)

        features = QGridLayout()
        features.setHorizontalSpacing(16)
        for column, (badge, title, text, detail, page, icon_kind) in enumerate((
            ("01  •  ГЕОМЕТРИЯ", "Умный анализ", "Находим проблемы геометрии\nи риски печати", "Сетка  •  поверхности  •  положение", "analysis", "analysis"),
            ("02  •  PRINTDNA", "Оптимизация настроек", "Подбираем качество, скорость\nи поддержки", "Материал  •  назначение  •  опыт печати", "optimization", "optimization"),
            ("03  •  ГОТОВО", "Один готовый файл", "Экспортируем 3MF или G-code\nбез лишних вариантов", "Проверка  •  нарезка  •  безопасный экспорт", "export", "export"),
        )):
            card, card_layout = self._card()
            card.setObjectName("featureCard")
            card.setMinimumHeight(226)
            badge_label = QLabel(badge)
            badge_label.setObjectName("featureBadge")
            card_layout.addWidget(badge_label, 0, Qt.AlignmentFlag.AlignLeft)
            feature_header = QHBoxLayout()
            feature_header.addWidget(FeatureIllustration(icon_kind))
            feature_text = QVBoxLayout()
            title_label = QLabel(title)
            title_label.setObjectName("featureTitle")
            description = QLabel(text)
            description.setObjectName("muted")
            description.setWordWrap(True)
            feature_text.addWidget(title_label)
            feature_text.addWidget(description)
            feature_text.addStretch(1)
            feature_header.addLayout(feature_text, 1)
            card_layout.addLayout(feature_header)
            detail_label = QLabel(detail)
            detail_label.setObjectName("featureDetail")
            detail_label.setWordWrap(True)
            card_layout.addWidget(detail_label)
            open_button = QPushButton("Перейти  →")
            open_button.setObjectName("featureButton")
            open_button.clicked.connect(lambda checked=False, selected=page: self._show_page(selected))
            card_layout.addWidget(open_button)
            features.addWidget(card, 0, column)
            features.setColumnStretch(column, 1)
        layout.addLayout(features)
        layout.addStretch(1)
        return scroll

    def _build_analysis(self) -> QWidget:
        repeat = self._primary_button("↻  Повторный анализ", self._start_scan)
        self.repeat_analysis_button = repeat
        scroll, layout = self._page("Анализ модели", "Подробная проверка геометрии, назначения детали и потенциальных проблем печати", repeat)
        body = QHBoxLayout()
        self.analysis_canvas = ModelCanvas()
        self._canvases.append(self.analysis_canvas)
        body.addWidget(self.analysis_canvas, 3)
        right = QVBoxLayout()
        issues_card, issues_layout = self._card("Обнаруженные проблемы")
        self.issues_list = QListWidget()
        issues_layout.addWidget(self.issues_list)
        right.addWidget(issues_card, 3)
        stats_card, stats_layout = self._card("Статистика модели")
        grid = QGridLayout()
        self.metric_dimensions = MetricCard("Габариты")
        self.metric_volume = MetricCard("Объём")
        self.metric_area = MetricCard("Площадь")
        self.metric_triangles = MetricCard("Треугольников")
        self.metric_overhang = MetricCard("Нависающие поверхности")
        self.metric_bodies = MetricCard("Объектов")
        for index, card in enumerate((self.metric_dimensions, self.metric_volume, self.metric_area, self.metric_triangles, self.metric_overhang, self.metric_bodies)):
            grid.addWidget(card, index // 2, index % 2)
        stats_layout.addLayout(grid)
        right.addWidget(stats_card, 2)
        body.addLayout(right, 2)
        layout.addLayout(body)
        surface_card, surface_layout = self._card("Surface Intelligence — карта назначения поверхностей")
        self.surface_summary = QLabel(
            "После анализа здесь появятся роли поверхностей и решения, которые повлияют на печать."
        )
        self.surface_summary.setWordWrap(True)
        self.surface_summary.setObjectName("muted")
        surface_layout.addWidget(self.surface_summary)
        layout.addWidget(surface_card)
        recommendation, rec_layout = self._card("Рекомендации")
        self.analysis_recommendation = QLabel("Загрузите модель — приложение выполнит проверку автоматически.")
        self.analysis_recommendation.setWordWrap(True)
        self.analysis_recommendation.setObjectName("muted")
        rec_layout.addWidget(self.analysis_recommendation)
        self.analysis_optimize_button = self._primary_button("Перейти к оптимизации  →", lambda: self._show_page("optimization"))
        rec_layout.addWidget(self.analysis_optimize_button, 0, Qt.AlignmentFlag.AlignRight)
        layout.addWidget(recommendation)
        return scroll

    def _spin(self, value: float, minimum: float, maximum: float, suffix: str, decimals: int = 2) -> QDoubleSpinBox:
        control = QDoubleSpinBox()
        control.setRange(minimum, maximum)
        control.setDecimals(decimals)
        control.setValue(value)
        control.setSuffix(suffix)
        return control

    def _build_print_settings(self) -> QWidget:
        apply_button = self._primary_button("Применить настройки", self._apply_print_settings)
        self.apply_settings_button = apply_button
        scroll, layout = self._page("Настройки печати", "Ключевые параметры проекта; полный профиль формирует встроенный движок VANIOR Slice", apply_button)
        self.print_tabs = QTabWidget()
        self.print_tabs.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.print_tabs.setMinimumHeight(540)
        layout.addWidget(self.print_tabs)

        basic = QWidget()
        basic_grid = QHBoxLayout(basic)
        quality, quality_layout = self._card("Качество и заполнение")
        form = QFormLayout()
        self.layer_height = self._spin(0.20, 0.04, 0.40, " мм")
        self.first_layer_height = self._spin(0.20, 0.08, 0.40, " мм")
        self.line_width = self._spin(0.42, 0.20, 1.20, " мм")
        self.wall_loops = QSpinBox(); self.wall_loops.setRange(1, 12); self.wall_loops.setValue(3)
        self.infill_percent = QSpinBox(); self.infill_percent.setRange(0, 100); self.infill_percent.setValue(15); self.infill_percent.setSuffix(" %")
        self.infill_pattern = QComboBox(); self.infill_pattern.addItems(["Гироид", "Сетка", "Кубическое", "Линии", "Соты"])
        self.top_layers = QSpinBox(); self.top_layers.setRange(1, 20); self.top_layers.setValue(5)
        self.bottom_layers = QSpinBox(); self.bottom_layers.setRange(1, 20); self.bottom_layers.setValue(4)
        for label, control in (
            ("Высота слоя", self.layer_height), ("Первый слой", self.first_layer_height), ("Ширина линии", self.line_width),
            ("Контуры стенок", self.wall_loops), ("Плотность заполнения", self.infill_percent), ("Шаблон заполнения", self.infill_pattern),
            ("Верхние слои", self.top_layers), ("Нижние слои", self.bottom_layers),
        ):
            form.addRow(label, control)
        quality_layout.addLayout(form)
        form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        quality_layout.addStretch(1)
        basic_grid.addWidget(quality)

        surface, surface_layout = self._card("Поверхности и охлаждение")
        form2 = QFormLayout()
        self.top_pattern = QComboBox(); self.top_pattern.addItems(["Монотонные линии", "Концентрический", "Линии"])
        self.ironing = QCheckBox("Разглаживание верхних поверхностей")
        self.fan_percent = QSpinBox(); self.fan_percent.setRange(0, 100); self.fan_percent.setValue(100); self.fan_percent.setSuffix(" %")
        self.brim_enable = QCheckBox("Добавлять кайму при необходимости")
        self.seam_position = QComboBox(); self.seam_position.addItems(["Выровненный", "Сзади", "Ближайший", "Случайный"])
        form2.addRow("Верхний рисунок", self.top_pattern)
        form2.addRow("", self.ironing)
        form2.addRow("Максимальный обдув", self.fan_percent)
        form2.addRow("", self.brim_enable)
        form2.addRow("Положение шва", self.seam_position)
        surface_layout.addLayout(form2)
        form2.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        surface_layout.addStretch(1)
        basic_grid.addWidget(surface)
        self.print_tabs.addTab(basic, "Основные")

        speed = QWidget(); speed_layout = QHBoxLayout(speed)
        speed_card, speed_form_layout = self._card("Скорость")
        speed_form = QFormLayout()
        self.outer_speed = QSpinBox(); self.outer_speed.setRange(10, 500); self.outer_speed.setValue(200); self.outer_speed.setSuffix(" мм/с")
        self.inner_speed = QSpinBox(); self.inner_speed.setRange(10, 500); self.inner_speed.setValue(300); self.inner_speed.setSuffix(" мм/с")
        self.infill_speed = QSpinBox(); self.infill_speed.setRange(10, 500); self.infill_speed.setValue(270); self.infill_speed.setSuffix(" мм/с")
        self.top_speed = QSpinBox(); self.top_speed.setRange(10, 300); self.top_speed.setValue(200); self.top_speed.setSuffix(" мм/с")
        self.travel_speed = QSpinBox(); self.travel_speed.setRange(50, 700); self.travel_speed.setValue(500); self.travel_speed.setSuffix(" мм/с")
        self.bridge_speed = QSpinBox(); self.bridge_speed.setRange(10, 200); self.bridge_speed.setValue(50); self.bridge_speed.setSuffix(" мм/с")
        for label, control in (("Внешняя стенка", self.outer_speed), ("Внутренняя стенка", self.inner_speed), ("Заполнение", self.infill_speed), ("Верхняя поверхность", self.top_speed), ("Перемещения", self.travel_speed), ("Мосты", self.bridge_speed)):
            speed_form.addRow(label, control)
        speed_form_layout.addLayout(speed_form)
        speed_form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        speed_form_layout.addStretch(1)
        speed_layout.addWidget(speed_card)
        accel_card, accel_layout = self._card("Ускорения")
        accel_form = QFormLayout()
        self.default_accel = QSpinBox(); self.default_accel.setRange(500, 20000); self.default_accel.setValue(10000); self.default_accel.setSuffix(" мм/с²")
        self.outer_accel = QSpinBox(); self.outer_accel.setRange(500, 20000); self.outer_accel.setValue(5000); self.outer_accel.setSuffix(" мм/с²")
        self.top_accel = QSpinBox(); self.top_accel.setRange(500, 20000); self.top_accel.setValue(2000); self.top_accel.setSuffix(" мм/с²")
        accel_form.addRow("Обычное", self.default_accel); accel_form.addRow("Внешняя стенка", self.outer_accel); accel_form.addRow("Верхняя поверхность", self.top_accel)
        accel_layout.addLayout(accel_form)
        accel_form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        accel_layout.addStretch(1)
        speed_layout.addWidget(accel_card)
        self.print_tabs.addTab(speed, "Скорость")

        support = QWidget(); support_layout = QHBoxLayout(support)
        support_card, support_card_layout = self._card("Поддержки")
        support_form = QFormLayout()
        self.support_enable = QCheckBox("Включить поддержки")
        self.support_type = QComboBox(); self.support_type.addItems(["Автоматически", "Обычные", "Древовидные"])
        self.support_angle = self._spin(45, 10, 80, "°", 0)
        self.support_top_distance = self._spin(0.20, 0, 1.0, " мм")
        self.support_xy_distance = self._spin(0.40, 0, 2.0, " мм")
        self.support_interface_layers = QSpinBox(); self.support_interface_layers.setRange(0, 10); self.support_interface_layers.setValue(3)
        support_form.addRow("", self.support_enable); support_form.addRow("Тип", self.support_type); support_form.addRow("Угол нависания", self.support_angle); support_form.addRow("Зазор сверху", self.support_top_distance); support_form.addRow("Зазор по XY", self.support_xy_distance); support_form.addRow("Слои интерфейса", self.support_interface_layers)
        support_card_layout.addLayout(support_form)
        support_form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        support_card_layout.addStretch(1)
        support_layout.addWidget(support_card)
        note_card, note_layout = self._card("Принцип выбора")
        note = QLabel("VANIOR PRINT сравнивает печать без поддержек, с обычными и древовидными поддержками. В итоговый проект попадает только безопасный лучший вариант. Зазоры настраиваются так, чтобы поддержки снимались легче и меньше повреждали поверхность.")
        note.setWordWrap(True); note.setObjectName("muted"); note_layout.addWidget(note); note_layout.addStretch(1)
        support_layout.addWidget(note_card)
        self.print_tabs.addTab(support, "Поддержки")

        printer_tab = QWidget(); printer_layout = QVBoxLayout(printer_tab)
        printer_card, printer_card_layout = self._card("Принтер и материал")
        printer_form = QFormLayout()
        self.printer_combo = QComboBox(); self.printer_combo.addItem("Bambu Lab P1S", "Bambu Lab P1S")
        self.nozzle_combo = QComboBox()
        for diameter in (0.2, 0.4, 0.6, 0.8):
            self.nozzle_combo.addItem(f"{diameter:.1f} мм".replace(".", ","), diameter)
        self.nozzle_combo.setCurrentIndex(self.nozzle_combo.findData(0.4))
        self.bed_combo = QComboBox()
        for title_text, value in BUILD_PLATE_OPTIONS:
            self.bed_combo.addItem(title_text, value)
        self.material_combo = QComboBox()
        for product, family in MATERIAL_PRODUCTS:
            self.material_combo.addItem(product, family)
        self.printer_combo.currentIndexChanged.connect(self._sync_sidebar_profile)
        self.nozzle_combo.currentIndexChanged.connect(self._sync_sidebar_profile)
        self.bed_combo.currentIndexChanged.connect(self._sync_sidebar_profile)
        self.material_combo.currentIndexChanged.connect(self._sync_sidebar_profile)
        self.plate_spin = QSpinBox(); self.plate_spin.setRange(1, 99); self.plate_spin.setValue(1)
        printer_form.addRow("Принтер", self.printer_combo); printer_form.addRow("Сопло", self.nozzle_combo); printer_form.addRow("Стол", self.bed_combo); printer_form.addRow("Материал", self.material_combo); printer_form.addRow("Пластина 3MF", self.plate_spin)
        printer_card_layout.addLayout(printer_form)
        printer_form.setFormAlignment(Qt.AlignmentFlag.AlignTop)
        printer_layout.addWidget(printer_card); printer_layout.addStretch(1)
        self.print_tabs.addTab(printer_tab, "Принтер")

        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        advanced_card, advanced_card_layout = self._card("Полный профиль VANIOR Slice")
        self.advanced_profile_text = QTextEdit()
        self.advanced_profile_text.setReadOnly(True)
        self.advanced_profile_text.setPlaceholderText("После анализа здесь появятся все рассчитанные параметры VANIOR PRINT. Встроенный движок объединит их с полным профилем принтера и материала.")
        advanced_card_layout.addWidget(self.advanced_profile_text)
        advanced_layout.addWidget(advanced_card)
        self.print_tabs.addTab(advanced, "Расширенные")
        return scroll

    def _build_preview(self) -> QWidget:
        open_button = self._primary_button(
            "Открыть готовый файл", self._open_ready_result
        )
        self.preview_header_button = open_button
        scroll, layout = self._page("Предпросмотр", "Просмотр подготовленной модели, итоговых параметров и результата нарезки", open_button)
        body = QGridLayout()
        self.preview_canvas = ModelCanvas(); self.preview_canvas.setMinimumHeight(480); self._canvases.append(self.preview_canvas)
        body.addWidget(self.preview_canvas, 0, 0)
        right_card, right_layout = self._card("Информация о печати")
        self.preview_info = QLabel("Сначала выполните оптимизацию и нарезку.")
        self.preview_info.setWordWrap(True)
        self.preview_info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        right_layout.addWidget(self.preview_info)
        right_layout.addStretch(1)
        self.open_3mf_button = self._primary_button(
            "Открыть готовый файл", self._open_ready_result
        )
        self.send_print_button = self._primary_button(
            "Отправить на принтер", self._send_ready_to_printer
        )
        self.send_print_button.setToolTip(
            "Передача проверенного .gcode.3mf напрямую на P1S по локальной сети"
        )
        self.open_gcode_button = QPushButton("Показать G-code"); self.open_gcode_button.clicked.connect(self._show_ready_gcode)
        self.open_folder_button = QPushButton("Открыть папку"); self.open_folder_button.clicked.connect(self._open_result_folder)
        right_layout.addWidget(self.send_print_button); right_layout.addWidget(self.open_3mf_button); right_layout.addWidget(self.open_gcode_button); right_layout.addWidget(self.open_folder_button)
        body.addWidget(right_card, 0, 1)
        body.setColumnStretch(0, 3)
        body.setColumnStretch(1, 1)
        layout.addLayout(body)
        layer_card, layer_layout = self._card("Просмотр слоёв")
        layer_row = QHBoxLayout()
        self.layer_slider = QSlider(Qt.Orientation.Horizontal); self.layer_slider.setRange(1, 1)
        self.layer_label = QLabel("1 / 1")
        self.layer_label.setMinimumWidth(180)
        self.layer_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.layer_slider.valueChanged.connect(self._on_layer_changed)
        self.layer_slider.sliderPressed.connect(self.preview_canvas.begin_interaction)
        self.layer_slider.sliderReleased.connect(self.preview_canvas.end_interaction)
        layer_row.addWidget(QLabel("Слой")); layer_row.addWidget(self.layer_slider, 1); layer_row.addWidget(self.layer_label)
        layer_layout.addLayout(layer_row)
        role_row = QHBoxLayout()
        self.show_model_paths = QCheckBox("Модель")
        self.show_model_paths.setChecked(True)
        self.show_support_paths = QCheckBox("Поддержки и интерфейс")
        self.show_support_paths.setChecked(True)
        self.show_support_paths.setEnabled(False)
        self.layer_legend = QLabel(
            '<span style="color:#ff644d">■ наружная стенка</span>  '
            '<span style="color:#ff9e47">■ внутренняя стенка</span>  '
            '<span style="color:#ffd84d">■ верх</span>  '
            '<span style="color:#59d1ff">■ низ</span>  '
            '<span style="color:#a66beb">■ заполнение</span>  '
            '<span style="color:#cc8aff">■ сплошное заполнение</span>  '
            '<span style="color:#ff66b8">■ мосты</span>  '
            '<span style="color:#35db69">■ поддержки</span>  '
            '<span style="color:#ffe66e">■ интерфейс</span>  '
            '<span style="color:#8c94a8">■ кайма</span>'
        )
        self.layer_legend.setWordWrap(True)
        self.show_model_paths.toggled.connect(self._toggle_model_paths)
        self.show_support_paths.toggled.connect(self._toggle_support_paths)
        role_row.addWidget(self.show_model_paths)
        role_row.addWidget(self.show_support_paths)
        role_row.addSpacing(10)
        role_row.addWidget(self.layer_legend, 1)
        layer_layout.addLayout(role_row)
        layout.addWidget(layer_card)

        self.result_stack = QStackedWidget(); self.result_stack.setMinimumHeight(210)
        self.result_stack.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.MinimumExpanding)
        empty = QLabel("Здесь появится итог оптимизации."); empty.setAlignment(Qt.AlignmentFlag.AlignCenter); empty.setObjectName("empty")
        self.result_stack.addWidget(empty)
        result_page = QFrame(); result_page.setObjectName("card"); result_layout = QVBoxLayout(result_page)
        self.result_title = QLabel("Готово"); self.result_title.setObjectName("resultTitle")
        self.result_text = QLabel(); self.result_text.setWordWrap(True); self.result_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        result_layout.addWidget(self.result_title); result_layout.addWidget(self.result_text)
        self.result_stack.addWidget(result_page)
        layout.addWidget(self.result_stack)
        return scroll

    def _build_optimization(self) -> QWidget:
        scroll, layout = self._page("Оптимизация", "Автоматический выбор параметров печати на основе геометрии и назначения модели")
        modes, modes_layout = self._card("Режим оптимизации")
        mode_row = QHBoxLayout()
        self.priority_combo = QComboBox()
        self.priority_combo.addItem("Баланс качества и скорости", "balanced")
        self.priority_combo.addItem("Максимальное качество", "quality")
        self.priority_combo.addItem("Максимальная прочность", "strength")
        self.priority_combo.addItem("Быстрая печать", "fast")
        self.priority_radios: dict[str, QCheckBox] = {}
        for key, title, hint in (("fast", "Быстрая печать", "Минимальное время"), ("balanced", "Сбалансированная", "Оптимальный баланс"), ("quality", "Высокое качество", "Максимум детализации"), ("strength", "Максимальная прочность", "Усиленная оболочка")):
            card, card_layout = self._card(title)
            selector = QCheckBox(hint); selector.setProperty("priority", key)
            selector.toggled.connect(
                lambda checked, selected=key: self._set_priority(selected, checked)
            )
            self.priority_radios[key] = selector
            card_layout.addWidget(selector)
            mode_row.addWidget(card)
        modes_layout.addLayout(mode_row)
        self.priority_mix_label = QLabel(
            "Можно выбрать несколько режимов — значения будут усреднены, а не выбраны случайно."
        )
        self.priority_mix_label.setObjectName("muted")
        self.priority_mix_label.setWordWrap(True)
        modes_layout.addWidget(self.priority_mix_label)
        self.priority_radios["balanced"].setChecked(True)
        layout.addWidget(modes)

        body = QHBoxLayout()
        controls, controls_layout = self._card("Параметры оптимизации")
        self.optimize_layers = QCheckBox("Адаптивная высота слоя"); self.optimize_layers.setChecked(True)
        self.optimize_walls = QCheckBox("Оптимизация стенок"); self.optimize_walls.setChecked(True)
        self.optimize_infill = QCheckBox("Адаптивная плотность заполнения"); self.optimize_infill.setChecked(True)
        self.optimize_supports = QCheckBox("Легко удаляемые поддержки"); self.optimize_supports.setChecked(True)
        self.optimize_travel = QCheckBox("Оптимизация перемещений"); self.optimize_travel.setChecked(True)
        for checkbox in (self.optimize_layers, self.optimize_walls, self.optimize_infill, self.optimize_supports, self.optimize_travel):
            controls_layout.addWidget(checkbox)
        self.quality_search = QCheckBox(
            "Точный поиск минимального времени по реальным нарезкам"
        )
        self.quality_search.setChecked(True)
        self.quality_search.setToolTip(
            "Встроенный движок проверит несколько безопасных наборов параметров; "
            "будет выбран самый быстрый вариант, прошедший пороги качества и надёжности."
        )
        controls_layout.addWidget(self.quality_search)
        self.purpose_combo = QComboBox(); self.purpose_combo.addItem("Определить назначение автоматически", "auto"); self.purpose_combo.addItem("Декоративная модель", "decorative"); self.purpose_combo.addItem("Нагруженная деталь", "functional")
        controls_layout.addWidget(QLabel("Назначение детали")); controls_layout.addWidget(self.purpose_combo)
        self.functional_intent_combo = QComboBox()
        for title, value in (
            ("Определить сценарий автоматически", "auto"),
            ("Декоративная модель", "decorative"),
            ("Корпус устройства", "enclosure"),
            ("Крепёж / оснастка", "fixture"),
            ("Шестерня / механизм", "gear"),
            ("Защёлка", "snap_fit"),
            ("Сосуд / ёмкость", "vessel"),
            ("Гибкая деталь", "flexible"),
            ("Несущая деталь", "structural"),
            ("Функциональная деталь", "general_functional"),
        ):
            self.functional_intent_combo.addItem(title, value)
        controls_layout.addWidget(QLabel("Функциональный сценарий"))
        controls_layout.addWidget(self.functional_intent_combo)
        self.priority_combo.currentIndexChanged.connect(self._invalidate_current_result)
        self.purpose_combo.currentIndexChanged.connect(self._invalidate_current_result)
        self.functional_intent_combo.currentIndexChanged.connect(self._invalidate_current_result)
        self.quality_search.toggled.connect(self._invalidate_current_result)
        self.run_button = self._primary_button("Запустить оптимизацию", self._start_optimize)
        self.cancel_button = QPushButton("Отменить"); self.cancel_button.setEnabled(False); self.cancel_button.clicked.connect(self._cancel_job)
        controls_layout.addWidget(self.run_button); controls_layout.addWidget(self.cancel_button)
        body.addWidget(controls, 1)
        self.optimize_canvas = ModelCanvas(); self._canvases.append(self.optimize_canvas); body.addWidget(self.optimize_canvas, 2)
        result, result_layout = self._card("Результаты оптимизации")
        self.optimization_summary = QLabel("Готов к анализу модели."); self.optimization_summary.setWordWrap(True)
        result_layout.addWidget(self.optimization_summary); result_layout.addStretch(1)
        body.addWidget(result, 1)
        layout.addLayout(body)
        progress_card, progress_layout = self._card("Ход работы")
        self.status_label = QLabel("Выберите модель для начала"); self.status_label.setObjectName("status")
        self.progress_bar = QProgressBar(); self.progress_bar.setRange(0, 100); self.progress_bar.setValue(0)
        self.log_edit = QTextEdit(); self.log_edit.setReadOnly(True); self.log_edit.setMaximumHeight(130)
        progress_layout.addWidget(self.status_label); progress_layout.addWidget(self.progress_bar); progress_layout.addWidget(self.log_edit)
        layout.addWidget(progress_card)
        return scroll

    def _build_export(self) -> QWidget:
        scroll, layout = self._page("Экспорт проекта", "Сохраните один итоговый файл с готовыми настройками печати")
        body = QHBoxLayout()
        formats, formats_layout = self._card("Формат экспорта")
        self.export_3mf = QRadioButton("3MF — готовый проект с настройками и нарезкой"); self.export_3mf.setChecked(True)
        self.export_gcode = QRadioButton("G-code — готовая программа печати")
        formats_layout.addWidget(self.export_3mf); formats_layout.addWidget(self.export_gcode)
        formats_layout.addSpacing(12)
        self.export_name = QLineEdit(); self.export_name.setPlaceholderText("Имя итогового файла")
        self.output_edit = QLineEdit(str(self.workspace.ready)); self.output_edit.setReadOnly(True)
        self.output_edit.setMinimumHeight(32)
        formats_layout.addWidget(QLabel("Имя файла")); formats_layout.addWidget(self.export_name)
        formats_layout.addWidget(QLabel("Папка готовых файлов (фиксированная)")); formats_layout.addWidget(self._path_row(self.output_edit, self._browse_output))
        self.export_button = self._primary_button("Экспортировать проект", self._export_project)
        formats_layout.addWidget(self.export_button)
        body.addWidget(formats, 2)
        preview, preview_layout = self._card("Сводка")
        self.export_summary = QLabel("Загрузите и оптимизируйте модель."); self.export_summary.setWordWrap(True)
        preview_layout.addWidget(self.export_summary); preview_layout.addStretch(1)
        body.addWidget(preview, 1)
        layout.addLayout(body)
        self.export_canvas = ModelCanvas(); self.export_canvas.setMinimumHeight(400); self._canvases.append(self.export_canvas)
        layout.addWidget(self.export_canvas)
        return scroll

    def _build_history(self) -> QWidget:
        scroll, layout = self._page("История", "Проекты и экспортированные результаты VANIOR PRINT")
        body = QHBoxLayout()
        card, card_layout = self._card("Проекты")
        self.history_list = QListWidget(); self.history_list.currentItemChanged.connect(self._history_selected); self.history_list.itemDoubleClicked.connect(self._open_history_item)
        card_layout.addWidget(self.history_list)
        body.addWidget(card, 3)
        details, details_layout = self._card("Сведения о проекте")
        self.history_details = QLabel("Выберите проект в списке."); self.history_details.setWordWrap(True); self.history_details.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        details_layout.addWidget(self.history_details); details_layout.addStretch(1)
        self.history_open_button = self._primary_button("Открыть проект", self._open_selected_history)
        self.history_folder_button = QPushButton("Открыть папку"); self.history_folder_button.clicked.connect(self._open_selected_history_folder)
        self.history_delete_button = QPushButton("Удалить из истории"); self.history_delete_button.setObjectName("danger"); self.history_delete_button.clicked.connect(self._delete_selected_history)
        self.history_open_button.setEnabled(False); self.history_folder_button.setEnabled(False); self.history_delete_button.setEnabled(False)
        details_layout.addWidget(self.history_open_button); details_layout.addWidget(self.history_folder_button); details_layout.addWidget(self.history_delete_button)
        body.addWidget(details, 2)
        layout.addLayout(body)
        return scroll

    def _build_print_dna(self) -> QWidget:
        scroll, layout = self._page(
            "PrintDNA",
            "Локальное обучение по реальным отпечаткам конкретного принтера, сопла и материала",
        )
        overview, overview_layout = self._card("Текущий обучаемый профиль")
        self.dna_profile_summary = QLabel("Профиль ещё не обучен.")
        self.dna_profile_summary.setWordWrap(True)
        overview_layout.addWidget(self.dna_profile_summary)
        privacy = QLabel(
            "По умолчанию оценки хранятся только на этом компьютере. Общее обучение "
            "включается отдельно в настройках и передаёт только обезличенные параметры; "
            "модели, имена файлов, фотографии, заметки и история не отправляются."
        )
        privacy.setObjectName("muted")
        privacy.setWordWrap(True)
        overview_layout.addWidget(privacy)
        layout.addWidget(overview)

        body = QHBoxLayout()
        feedback, feedback_layout = self._card("Результат физической печати")
        self.dna_project_combo = QComboBox()
        self.dna_project_combo.currentIndexChanged.connect(self._refresh_print_dna_profile)
        feedback_layout.addWidget(QLabel("Проект из истории"))
        feedback_layout.addWidget(self.dna_project_combo)

        self.dna_quality = QComboBox()
        for title, value in (
            ("1 — очень плохо", 1),
            ("2 — плохо", 2),
            ("3 — значительно ниже ожидаемого", 3),
            ("4 — ниже ожидаемого", 4),
            ("5 — удовлетворительно", 5),
            ("6 — нормально", 6),
            ("7 — хорошо", 7),
            ("8 — очень хорошо", 8),
            ("9 — отлично", 9),
            ("10 — превосходно", 10),
        ):
            self.dna_quality.addItem(title, value)
        self.dna_quality.setCurrentIndex(7)
        feedback_layout.addWidget(QLabel("Общее качество"))
        feedback_layout.addWidget(self.dna_quality)

        self.dna_support_rating = QComboBox()
        self.dna_dimension_rating = QComboBox()
        for combo in (self.dna_support_rating, self.dna_dimension_rating):
            combo.addItem("Не оценивалось", None)
            combo.addItem("1 — плохо", 1)
            combo.addItem("2 — ниже ожидаемого", 2)
            combo.addItem("3 — нормально", 3)
            combo.addItem("4 — хорошо", 4)
            combo.addItem("5 — отлично", 5)
        feedback_layout.addWidget(QLabel("Удаление поддержек"))
        feedback_layout.addWidget(self.dna_support_rating)
        feedback_layout.addWidget(QLabel("Точность размеров"))
        feedback_layout.addWidget(self.dna_dimension_rating)

        self.dna_region = QComboBox()
        for title, value in (
            ("Вся модель", "whole"),
            ("Верхняя поверхность", "top"),
            ("Внешняя стенка", "outer_wall"),
            ("Контакт с поддержкой", "support_interface"),
            ("Нижняя поверхность над поддержками", "supported_underside"),
            ("Шов", "seam"),
            ("Основание", "base"),
            ("Контакт со столом", "bed_contact"),
            ("Точный размер / посадка", "dimensional_feature"),
        ):
            self.dna_region.addItem(title, value)
        self.dna_layer = QSpinBox(); self.dna_layer.setRange(0, 100000)
        self.dna_layer.setSpecialValueText("Не указан")
        feedback_layout.addWidget(QLabel("Область дефекта")); feedback_layout.addWidget(self.dna_region)
        feedback_layout.addWidget(QLabel("Слой дефекта")); feedback_layout.addWidget(self.dna_layer)
        self.dna_photo_edit = QLineEdit(); self.dna_photo_edit.setReadOnly(True)
        self.dna_photo_edit.setPlaceholderText("Необязательно: фотография результата")
        feedback_layout.addWidget(QLabel("Фотография"))
        feedback_layout.addWidget(self._path_row(self.dna_photo_edit, self._browse_dna_photo))

        self.dna_notes = QTextEdit()
        self.dna_notes.setPlaceholderText("Необязательно: что получилось хорошо или плохо")
        self.dna_notes.setMaximumHeight(90)
        feedback_layout.addWidget(QLabel("Комментарий"))
        feedback_layout.addWidget(self.dna_notes)
        self.dna_save_button = self._primary_button(
            "Сохранить результат и обучить PrintDNA",
            self._save_print_dna_feedback,
        )
        feedback_layout.addWidget(self.dna_save_button)
        body.addWidget(feedback, 1)

        defects, defects_layout = self._card("Замеченные дефекты")
        defect_labels = (
            ("rough_top", "Неровная верхняя поверхность"),
            ("rough_outer_wall", "Ухудшение внешней поверхности"),
            ("bed_contact_roughness", "Отпечаток текстуры стола на видимой поверхности"),
            ("support_scars", "Следы поддержек"),
            ("support_underside_roughness", "Шероховатость над поддержками"),
            ("stringing", "Нити пластика"),
            ("visible_seam", "Заметный шов"),
            ("weak_part", "Недостаточная прочность"),
            ("dimensional_error", "Неточность размеров или посадки"),
            ("warping", "Отрыв от стола или деформация"),
        )
        self.dna_defects: dict[str, QCheckBox] = {}
        for key, label in defect_labels:
            checkbox = QCheckBox(label)
            self.dna_defects[key] = checkbox
            defects_layout.addWidget(checkbox)
        defects_layout.addStretch(1)
        body.addWidget(defects, 1)
        layout.addLayout(body)

        learned, learned_layout = self._card("Как PrintDNA повлияет на следующие проекты")
        self.dna_adjustments = QLabel(
            "После первой оценки здесь появятся только безопасные ограниченные поправки."
        )
        self.dna_adjustments.setWordWrap(True)
        learned_layout.addWidget(self.dna_adjustments)
        layout.addWidget(learned)
        layout.addStretch(1)
        return scroll

    def _build_printers(self) -> QWidget:
        save = self._primary_button("Сохранить профиль", self._save_printer_profile)
        scroll, layout = self._page(
            "Принтер и локальная печать",
            "Профиль оборудования и прямая отправка задания на P1S без Bambu Studio",
            save,
        )
        body = QHBoxLayout()
        profile, profile_layout = self._card("Bambu Lab P1S")
        status = QLabel("●  Профиль доступен во встроенном движке VANIOR Slice")
        status.setObjectName("success")
        description = QLabel(
            "VANIOR PRINT самостоятельно анализирует, ремонтирует и нарезает модель. "
            "Подключение к принтеру работает только внутри локальной сети."
        )
        description.setObjectName("muted"); description.setWordWrap(True)
        profile_layout.addWidget(status); profile_layout.addWidget(description); profile_layout.addStretch(1)
        body.addWidget(profile)
        materials, materials_layout = self._card("Материалы")
        self.material_profiles = QListWidget()
        self.material_profiles.addItems([product for product, _family in MATERIAL_PRODUCTS])
        materials_layout.addWidget(self.material_profiles)
        body.addWidget(materials)
        layout.addLayout(body)

        connection, connection_layout = self._card("Подключение к P1S по локальной сети")
        connection_form = QFormLayout()
        self.printer_ip_edit = QLineEdit()
        self.printer_ip_edit.setPlaceholderText("192.168.1.100")
        self.printer_serial_edit = QLineEdit()
        self.printer_serial_edit.setPlaceholderText("Серийный номер с экрана принтера")
        self.printer_access_code_edit = QLineEdit()
        self.printer_access_code_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.printer_access_code_edit.setPlaceholderText("LAN access code — не сохраняется")
        self.printer_ip_edit.editingFinished.connect(self._printer_identity_changed)
        self.printer_serial_edit.editingFinished.connect(self._printer_identity_changed)
        self.filament_source_combo = QComboBox()
        self.filament_source_combo.addItem("Внешняя катушка", -1)
        for slot in range(1, 5):
            self.filament_source_combo.addItem(f"AMS A{slot}", slot - 1)
        connection_form.addRow("IP-адрес", self.printer_ip_edit)
        connection_form.addRow("Серийный номер", self.printer_serial_edit)
        connection_form.addRow("Код доступа", self.printer_access_code_edit)
        connection_form.addRow("Источник пластика", self.filament_source_combo)
        connection_layout.addLayout(connection_form)
        safety_row = QHBoxLayout()
        self.direct_bed_leveling = QCheckBox("Калибровка стола")
        self.direct_bed_leveling.setChecked(True)
        self.direct_vibration = QCheckBox("Проверка вибраций")
        self.direct_vibration.setChecked(True)
        self.direct_flow = QCheckBox("Калибровка потока")
        self.direct_timelapse = QCheckBox("Таймлапс")
        safety_row.addWidget(self.direct_bed_leveling)
        safety_row.addWidget(self.direct_vibration)
        safety_row.addWidget(self.direct_flow)
        safety_row.addWidget(self.direct_timelapse)
        safety_row.addStretch(1)
        connection_layout.addLayout(safety_row)
        self.printer_connection_status = QLabel(
            "Не подключено. Включите LAN Only/Developer Mode на принтере, затем "
            "укажите IP, серийный номер и код доступа."
        )
        self.printer_connection_status.setObjectName("muted")
        self.printer_connection_status.setWordWrap(True)
        connection_layout.addWidget(self.printer_connection_status)
        self.test_printer_button = QPushButton("Проверить и доверять принтеру")
        self.test_printer_button.clicked.connect(self._test_printer_connection)
        connection_layout.addWidget(self.test_printer_button)
        layout.addWidget(connection)

        defaults, defaults_layout = self._card("Текущие значения")
        self.printer_defaults = QLabel("Bambu Lab P1S  •  сопло 0,4 мм  •  Textured PEI  •  PLA")
        defaults_layout.addWidget(self.printer_defaults)
        layout.addWidget(defaults)
        layout.addStretch(1)
        return scroll

    def _build_settings(self) -> QWidget:
        scroll, layout = self._page("Настройки", "Параметры приложения, рабочие папки и встроенный движок нарезки")
        tabs = QTabWidget(); layout.addWidget(tabs)
        general = QWidget(); general_layout = QHBoxLayout(general)
        common, common_layout = self._card("Общие настройки")
        self.language_combo = QComboBox(); self.language_combo.addItem("Русский")
        self.theme_combo = QComboBox(); self.theme_combo.addItem("Тёмная фиолетовая")
        self.autosave_check = QCheckBox("Запоминать последнюю выбранную модель"); self.autosave_check.setChecked(True)
        common_layout.addWidget(QLabel("Язык интерфейса")); common_layout.addWidget(self.language_combo)
        common_layout.addWidget(QLabel("Тема")); common_layout.addWidget(self.theme_combo); common_layout.addWidget(self.autosave_check); common_layout.addStretch(1)
        general_layout.addWidget(common)
        behavior, behavior_layout = self._card("Поведение приложения")
        self.tips_check = QCheckBox("Показывать полезные советы"); self.tips_check.setChecked(True)
        behavior_layout.addWidget(self.tips_check)
        behavior_note = QLabel(
            "Анализ и нарезка выполняются локально. Готовое задание передаётся "
            "только напрямую на выбранный принтер в локальной сети и только после подтверждения."
        )
        behavior_note.setObjectName("muted"); behavior_note.setWordWrap(True)
        behavior_layout.addWidget(behavior_note); behavior_layout.addStretch(1)
        general_layout.addWidget(behavior)
        tabs.addTab(general, "Общие")

        integration = QWidget(); integration_layout = QVBoxLayout(integration)
        bambu, bambu_layout = self._card("VANIOR Slice — встроенная нарезка")
        self.engine_combo = QComboBox()
        self.engine_combo.addItem(
            "VANIOR Slice — самостоятельное ядро", "vanior"
        )
        self.engine_combo.setEnabled(False)
        self.engine_combo.currentIndexChanged.connect(self._on_engine_changed)
        bambu_layout.addWidget(QLabel("Движок нарезки"))
        bambu_layout.addWidget(self.engine_combo)
        self.slicer_edit = QLineEdit(); self.slicer_edit.setPlaceholderText("Встроенный движок определяется автоматически")
        self.slicer_edit.setReadOnly(True)
        bambu_layout.addWidget(self.slicer_edit)
        self.engine_note = QLabel(
            "VANIOR PRINT не ищет, не запускает и не включает Bambu Studio или другой "
            "внешний слайсер. Текущий проверяемый контур: одноцветные STL/3MF, P1S, "
            "сопло 0,4 мм, PLA/PETG. Неподдержанные задания блокируются до создания G-code."
        )
        self.engine_note.setWordWrap(True); self.engine_note.setObjectName("muted")
        bambu_layout.addWidget(self.engine_note)
        self.detect_engine_button = QPushButton("Проверить встроенный движок")
        self.detect_engine_button.clicked.connect(self._detect_slicer)
        bambu_layout.addWidget(self.detect_engine_button)
        integration_layout.addWidget(bambu)
        folders, folders_layout = self._card("Рабочие папки")
        self.default_output_edit = QLineEdit(str(self.workspace.ready)); self.default_output_edit.setReadOnly(True)
        folders_layout.addWidget(QLabel("Единая папка готовых файлов")); folders_layout.addWidget(self._path_row(self.default_output_edit, self._browse_default_output))
        if self.workspace.personal_mode:
            workspace_text = (
                "Личный режим: исходные модели безопасно переносятся в назначенную папку.\n"
                f"Загрузка: {self.workspace.uploads}\n"
                f"Готово к печати: {self.workspace.ready}\n"
                f"Бэкапы: {self.workspace.backups}\n"
                f"Обучение: {self.workspace.learning}"
            )
        else:
            workspace_text = (
                "Обычная установка: исходные файлы всегда остаются на месте.\n"
                f"Готовые файлы: {self.workspace.ready}\n"
                f"Локальные данные приложения: {self.workspace.root}"
            )
        workspace_note = QLabel(workspace_text)
        workspace_note.setWordWrap(True); workspace_note.setObjectName("muted")
        folders_layout.addWidget(workspace_note)
        integration_layout.addWidget(folders)
        timeout_card, timeout_layout = self._card("Ограничения")
        self.timeout_spin = QSpinBox(); self.timeout_spin.setRange(30, 3600); self.timeout_spin.setValue(600); self.timeout_spin.setSuffix(" сек")
        timeout_layout.addWidget(QLabel("Максимальное время одной операции")); timeout_layout.addWidget(self.timeout_spin)
        integration_layout.addWidget(timeout_card)
        learning_card, learning_layout = self._card("Общее обучение PrintDNA")
        learning_note = QLabel(
            "После явного согласия приложение отправляет только обезличенные оценки печати и параметры. "
            "Имена файлов, пути, модели, фотографии и заметки никогда не передаются. "
            "Для работы нужен HTTPS-сервис VANIOR Learning Hub."
        )
        learning_note.setWordWrap(True); learning_note.setObjectName("muted")
        self.community_learning_check = QCheckBox("Разрешить обезличенное общее обучение")
        self.community_endpoint_edit = QLineEdit()
        self.community_endpoint_edit.setPlaceholderText("https://learning.vanior-print.example/api/v1")
        self.community_sync_button = QPushButton("Синхронизировать сейчас")
        self.community_sync_button.clicked.connect(self._start_community_sync)
        learning_layout.addWidget(learning_note)
        learning_layout.addWidget(self.community_learning_check)
        learning_layout.addWidget(QLabel("Адрес сервиса"))
        learning_layout.addWidget(self.community_endpoint_edit)
        learning_layout.addWidget(self.community_sync_button)
        integration_layout.addWidget(learning_card)
        integration_layout.addStretch(1)
        tabs.addTab(integration, "Система")

        recovery = QWidget(); recovery_layout = QVBoxLayout(recovery)
        recovery_card, recovery_card_layout = self._card("Надёжность и восстановление")
        recovery_text = QLabel(
            "Резервная копия сохраняет полный комплект приложения для отката, историю, PrintDNA и фотографии обучения. "
            "Диагностический ZIP не включает модели, G-code и фотографии."
        )
        recovery_text.setWordWrap(True); recovery_text.setObjectName("muted")
        backup_button = QPushButton("Создать резервную копию")
        backup_button.clicked.connect(self._create_data_backup)
        restore_button = QPushButton("Восстановить данные из копии")
        restore_button.clicked.connect(self._restore_data_backup)
        diagnostics_button = QPushButton("Собрать диагностический ZIP")
        diagnostics_button.clicked.connect(self._create_diagnostic_bundle)
        recovery_card_layout.addWidget(recovery_text)
        recovery_card_layout.addWidget(backup_button)
        recovery_card_layout.addWidget(restore_button)
        recovery_card_layout.addWidget(diagnostics_button)
        recovery_layout.addWidget(recovery_card); recovery_layout.addStretch(1)
        tabs.addTab(recovery, "Восстановление")

        about = QWidget(); about_layout = QVBoxLayout(about)
        about_card, about_card_layout = self._card(APP_NAME)
        about_label = QLabel(
            f"Версия {__version__}\nРазработчик: Ivan Valevich (BigVanior)\n\n"
            "Самостоятельный локальный цикл: анализ STL/3MF, ремонт, ориентация, "
            "поддержки, нарезка, аудит, упаковка и отправка на принтер."
        )
        about_label.setWordWrap(True); about_card_layout.addWidget(about_label)
        legal_button = QPushButton("Открыть условия и лицензии")
        legal_button.clicked.connect(self._open_legal_notices)
        about_card_layout.addWidget(legal_button)
        about_layout.addWidget(about_card); about_layout.addStretch(1)
        tabs.addTab(about, "О программе")
        save = self._primary_button("Сохранить настройки", self._save_settings); layout.addWidget(save, 0, Qt.AlignmentFlag.AlignRight)
        return scroll

    # ---------- navigation and state ----------
    def _finish_page_transition(
        self, transition: QParallelAnimationGroup | None = None
    ) -> None:
        if transition is not None and transition is not self._page_transition:
            transition.deleteLater()
            return

        active = self._page_transition
        widget = self._page_transition_widget
        effect = self._page_transition_effect
        origin = self._page_transition_origin
        self._page_transition = None
        self._page_transition_widget = None
        self._page_transition_effect = None
        self._page_transition_origin = None

        if active is not None:
            active.stop()
        if widget is not None:
            if origin is not None:
                widget.move(origin)
            if effect is not None and widget.graphicsEffect() is effect:
                effect.setOpacity(1.0)
                widget.setGraphicsEffect(None)
        if active is not None:
            active.deleteLater()

    def _show_page(self, key: str) -> None:
        if key not in self._page_indexes:
            return
        target_index = self._page_indexes[key]
        for button_key, button in self._nav_buttons.items():
            button.setChecked(button_key == key)

        current_index = self.pages.currentIndex()
        if current_index == target_index:
            return

        self._finish_page_transition()
        direction = 1 if target_index > current_index else -1
        self.pages.setCurrentIndex(target_index)
        target = self.pages.currentWidget()
        if target is None:
            return

        origin = target.pos()
        target.move(origin + QPoint(direction * self.PAGE_TRANSITION_OFFSET, 0))
        effect = QGraphicsOpacityEffect(target)
        effect.setOpacity(0.18)
        target.setGraphicsEffect(effect)

        transition = QParallelAnimationGroup(self)
        position = QPropertyAnimation(target, b"pos", transition)
        position.setDuration(self.PAGE_TRANSITION_MS)
        position.setStartValue(target.pos())
        position.setEndValue(origin)
        position.setEasingCurve(QEasingCurve.Type.OutCubic)
        opacity = QPropertyAnimation(effect, b"opacity", transition)
        opacity.setDuration(self.PAGE_TRANSITION_MS)
        opacity.setStartValue(0.18)
        opacity.setEndValue(1.0)
        opacity.setEasingCurve(QEasingCurve.Type.OutCubic)
        transition.addAnimation(position)
        transition.addAnimation(opacity)

        self._page_transition = transition
        self._page_transition_widget = target
        self._page_transition_effect = effect
        self._page_transition_origin = origin
        transition.finished.connect(
            lambda finished=transition: self._finish_page_transition(finished)
        )
        transition.start()

    def _update_ui_state(self, message: str | None = None) -> None:
        has_model = self._current_source() is not None
        ready_3mf = bool(
            self.last_result
            and Path(str(self.last_result.get("ready_3mf", ""))).is_file()
        )
        ready_gcode = bool(
            self.last_result
            and Path(str(self.last_result.get("ready_gcode", ""))).is_file()
        )
        busy = self.thread is not None
        printer_busy = self.printer_worker is not None
        for name in (
            "repeat_analysis_button", "analysis_optimize_button",
            "apply_settings_button", "run_button",
        ):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled(has_model and not busy)
        for name in ("preview_header_button", "open_3mf_button"):
            widget = getattr(self, name, None)
            if widget is not None:
                widget.setEnabled((ready_3mf or ready_gcode) and not busy)
                widget.setText(
                    "Открыть готовый 3MF"
                    if ready_3mf
                    else "Показать готовый G-code"
                    if ready_gcode
                    else "Открыть готовый файл"
                )
        if hasattr(self, "open_gcode_button"):
            self.open_gcode_button.setEnabled(ready_gcode and not busy)
        if hasattr(self, "open_folder_button"):
            self.open_folder_button.setEnabled(bool(self.last_result) and not busy)
        if hasattr(self, "send_print_button"):
            trusted = bool(
                self._printer_mqtt_fingerprint
                and self._printer_ftps_fingerprint
                and self._printer_trusted_identity == self._printer_identity()
            )
            self.send_print_button.setEnabled(
                ready_3mf and trusted and not busy and not printer_busy
            )
            self.send_print_button.setText(
                "Отправка на принтер…" if printer_busy else "Отправить на принтер"
            )
        if hasattr(self, "export_button"):
            self.export_button.setEnabled(has_model and not busy)
            self.export_button.setText(
                "Экспортировать готовый файл"
                if ready_3mf or ready_gcode
                else "Оптимизировать и экспортировать"
            )
        if hasattr(self, "project_state"):
            if message:
                state = message
            elif busy:
                state = "Обработка модели…"
            elif ready_3mf:
                state = "Готово к печати  •  итоговый 3MF проверен"
            elif ready_gcode:
                state = "Инженерный G-code создан  •  требуется проверка перед печатью"
            elif has_model and self.scan_result:
                state = "Анализ завершён  •  можно запускать оптимизацию"
            elif has_model:
                state = "Модель выбрана  •  требуется анализ"
            else:
                state = "Новый проект  •  выберите STL или 3MF, чтобы начать"
            marker = "✓" if ready_3mf else "◆" if ready_gcode else "●"
            self.project_state.setText(f"{marker}  {state}")
            self.project_state.setProperty("ready", ready_3mf or ready_gcode)
            self.project_state.style().unpolish(self.project_state)
            self.project_state.style().polish(self.project_state)

    def _set_priority(self, priority: str, checked: bool | None = None) -> None:
        selected = [
            key for key in ("quality", "strength", "balanced", "fast")
            if self.priority_radios[key].isChecked()
        ]
        if not selected:
            selector = self.priority_radios.get(priority) or self.priority_radios["balanced"]
            selector.blockSignals(True)
            selector.setChecked(True)
            selector.blockSignals(False)
            selected = [str(selector.property("priority"))]
        value = "+".join(selected)
        index = self.priority_combo.findData(value)
        if index < 0:
            self.priority_combo.addItem(value, value)
            index = self.priority_combo.findData(value)
        if index >= 0:
            self.priority_combo.setCurrentIndex(index)
        names = {
            "quality": "высокого качества",
            "strength": "максимальной прочности",
            "balanced": "сбалансированного режима",
            "fast": "быстрой печати",
        }
        if len(selected) == 1:
            summary = names[selected[0]].capitalize()
        else:
            summary = "Среднее между " + ", ".join(names[item] for item in selected)
        self.priority_mix_label.setText(
            f"Итог: {summary}. Расчёт детерминирован и не зависит от порядка нажатий."
        )

    def _current_source(self) -> Path | None:
        text = self.model_edit.text().strip()
        path = Path(text).expanduser() if text else None
        return path if path and path.is_file() else None

    def _invalidate_current_result(self, _value: object = None) -> None:
        if self.last_result is None:
            return
        self.last_result = None
        self._update_ui_state("Параметры изменены  •  требуется новая оптимизация")

    def _browse_model(self) -> None:
        start = self.settings.value("model_dir", str(self.workspace.uploads), str)
        path, _ = QFileDialog.getOpenFileName(self, "Выберите модель", start, "3D-модели (*.stl *.3mf)")
        if path:
            self._model_selected(path)

    def _model_selected(self, path: str) -> None:
        original = Path(path).expanduser().resolve()
        if not original.is_file() or original.suffix.lower() not in SUPPORTED_MODEL_SUFFIXES:
            self._show_error("Выберите существующий файл STL или 3MF.")
            return
        original_parent = original.parent
        try:
            source = import_model(original, self.workspace)
        except Exception as exc:
            action = "перенести" if self.workspace.move_imports else "открыть"
            self._show_error(f"Не удалось безопасно {action} модель: {exc}")
            return
        self.model_edit.setText(str(source))
        self.last_result = None
        self.scan_result = None
        self.settings.setValue("model_dir", str(original_parent if original_parent.is_dir() else self.workspace.uploads))
        if self.autosave_check.isChecked():
            self.settings.setValue("last_model", str(source))
        self.output_edit.setText(str(self.workspace.ready))
        self.export_name.setText(f"{source.stem}-optimized")
        try:
            append_learning_event(
                self.workspace,
                "model_imported",
                {
                    "source_name": source.name,
                    "format": source.suffix.casefold(),
                    "size_bytes": source.stat().st_size,
                    "sha256": workspace_sha256_file(source),
                },
                application_version=__version__,
            )
        except OSError:
            pass
        self._layer_heights_mm = []
        self.layer_slider.setRange(1, 1)
        self.layer_slider.setValue(1)
        for canvas in self._canvases:
            canvas.clear_mesh(f"{source.name}  •  анализируется…")
        self._refresh_overview()
        self._update_ui_state("Анализируем модель…")
        self._start_scan()

    def _browse_output(self) -> None:
        os.startfile(self.workspace.ready)  # type: ignore[attr-defined]

    def _browse_default_output(self) -> None:
        os.startfile(self.workspace.ready)  # type: ignore[attr-defined]

    def _browse_dna_photo(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Фотография отпечатка",
            self.settings.value("dna_photo_dir", str(Path.home()), str),
            "Изображения (*.jpg *.jpeg *.png *.webp)",
        )
        if path:
            self.dna_photo_edit.setText(path)
            self.settings.setValue("dna_photo_dir", str(Path(path).parent))

    def _on_engine_changed(self, _index: int = -1) -> None:
        self.export_3mf.setEnabled(True)
        self.export_3mf.setChecked(True)
        self.slicer_edit.setText("VANIOR Slice — встроенное самостоятельное ядро")
        self.run_button.setText("Создать печатный 3MF")
        self.last_result = None
        self._update_ui_state()

    def _detect_slicer(self) -> None:
        self.slicer_edit.setText("VANIOR Slice — встроенное самостоятельное ядро")
        QMessageBox.information(
            self,
            APP_NAME,
            "VANIOR Slice установлен. Анализ, ремонт, поддержки, нарезка, аудит "
            "и упаковка выполняются внутри VANIOR PRINT.",
        )

    def _start_community_sync(self, *, quiet: bool = False) -> None:
        if not self.community_learning_check.isChecked():
            if not quiet:
                self._show_error("Сначала подтвердите согласие на обезличенное общее обучение.")
            return
        endpoint = self.community_endpoint_edit.text().strip()
        if not endpoint:
            if not quiet:
                self._show_error("Укажите HTTPS-адрес VANIOR Learning Hub.")
            return
        if self.learning_sync_worker and self.learning_sync_worker.isRunning():
            return
        worker = LearningSyncWorker(
            self.print_dna_path, self.workspace.global_print_dna, endpoint
        )
        self.learning_sync_worker = worker
        self.community_sync_button.setEnabled(False)
        self.community_sync_button.setText("Синхронизация…")

        def succeeded(result: dict[str, Any]) -> None:
            self.statusBar().showMessage(
                f"PrintDNA синхронизирован: отправлено {result.get('uploaded', 0)}, "
                f"получено профилей {result.get('profiles', 0)}",
                7000,
            )

        def failed(message: str) -> None:
            if not quiet:
                self._show_error(f"Не удалось синхронизировать PrintDNA: {message}")
            else:
                self.statusBar().showMessage(f"PrintDNA: {message}", 5000)

        def cleanup() -> None:
            self.community_sync_button.setEnabled(True)
            self.community_sync_button.setText("Синхронизировать сейчас")
            self.learning_sync_worker = None

        worker.succeeded.connect(succeeded)
        worker.failed.connect(failed)
        worker.finished.connect(cleanup)
        worker.start()

    def _create_data_backup(self) -> None:
        target = self._unused_file(
            self.workspace.backups
            / f"VANIOR PRINT v{__version__} backup {datetime.now(UTC):%Y%m%d-%H%M%S}.zip"
        )
        settings_snapshot = self.workspace.system / "settings-snapshot.json"
        _write_json(
            settings_snapshot,
            {key: str(self.settings.value(key)) for key in self.settings.allKeys()},
        )
        application_dir = (
            Path(sys.executable).resolve().parent
            if getattr(sys, "frozen", False)
            else Path(__file__).resolve().parents[2]
        )
        try:
            result = create_application_backup(
                application_dir,
                target,
                data_files=(self.history_path, self.print_dna_path, settings_snapshot),
                data_roots=(self.print_dna_path.parent / "print_dna_photos",),
                excluded_names=(UPLOADS_NAME, READY_NAME, BACKUPS_NAME, LEARNING_NAME),
            )
            self.statusBar().showMessage(f"Резервная копия создана: {result}", 5000)
        except Exception as exc:
            self._show_error(f"Не удалось создать резервную копию: {exc}")

    def _restore_data_backup(self) -> None:
        source, _ = QFileDialog.getOpenFileName(
            self,
            "Выберите резервную копию VANIOR PRINT",
            str(self.workspace.backups),
            "Резервная копия (*.zip)",
        )
        if not source:
            return
        answer = QMessageBox.question(
            self,
            "Восстановить данные",
            "Текущая история, настройки и PrintDNA будут заменены данными из выбранной копии. Продолжить?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        settings_snapshot = self.workspace.system / "settings-snapshot.json"
        try:
            restored = restore_application_data_backup(
                source,
                data_files={
                    self.history_path.name: self.history_path,
                    self.print_dna_path.name: self.print_dna_path,
                    settings_snapshot.name: settings_snapshot,
                },
                data_roots={
                    "print_dna_photos": self.print_dna_path.parent / "print_dna_photos"
                },
            )
            snapshot = _read_json(settings_snapshot, {})
            if isinstance(snapshot, dict):
                for key, value in snapshot.items():
                    self.settings.setValue(str(key), value)
                self.settings.sync()
            history = _read_json(self.history_path, [])
            self.history = history if isinstance(history, list) else []
            self._refresh_history()
            self._refresh_overview()
            self.statusBar().showMessage(
                f"Данные восстановлены ({len(restored)} файлов). Перезапустите приложение для применения всех настроек.",
                9000,
            )
        except Exception as exc:
            self._show_error(f"Не удалось восстановить резервную копию: {exc}")

    def _create_diagnostic_bundle(self) -> None:
        diagnostics_dir = self.workspace.backups / "Диагностика"
        diagnostics_dir.mkdir(parents=True, exist_ok=True)
        target = self._unused_file(
            diagnostics_dir / f"vanior-diagnostics-{datetime.now(UTC):%Y%m%d-%H%M%S}.zip"
        )
        job_documents = sorted((self.workspace.system / "jobs").glob("*.json"))[-5:]
        try:
            result = build_diagnostic_bundle(
                target,
                documents=job_documents,
                environment={
                    "app": APP_NAME,
                    "version": __version__,
                    "python": sys.version.split()[0],
                    "platform": sys.platform,
                    "slicer_engine": "VANIOR Slice",
                    "direct_printer_transport": "local FTPS + MQTT/TLS",
                },
            )
            self.statusBar().showMessage(f"Диагностический ZIP создан: {result}", 5000)
        except Exception as exc:
            self._show_error(f"Не удалось собрать диагностику: {exc}")

    def _restore_settings(self) -> None:
        self.engine_combo.setCurrentIndex(max(0, self.engine_combo.findData("vanior")))
        self.engine_combo.setEnabled(False)
        self.slicer_edit.setText("VANIOR Slice — встроенное самостоятельное ядро")
        self.detect_engine_button.setText("Проверить VANIOR Slice")
        self.default_output_edit.setText(str(self.workspace.ready))
        self.output_edit.setText(str(self.workspace.ready))
        saved_material = self.settings.value("material_profile", "", str)
        if not saved_material:
            legacy_family = self.settings.value("material", "PLA", str)
            saved_material = "Generic PETG" if legacy_family == "PETG" else "Generic PLA"
        saved_material_index = self.material_combo.findText(saved_material)
        self.material_combo.setCurrentIndex(max(0, saved_material_index))
        saved_nozzle = self.settings.value("nozzle_diameter_mm", 0.4, float)
        nozzle_index = self.nozzle_combo.findData(float(saved_nozzle))
        self.nozzle_combo.setCurrentIndex(
            nozzle_index if nozzle_index >= 0 else self.nozzle_combo.findData(0.4)
        )
        saved_bed_type = self.settings.value(
            "bed_type", "Textured PEI Plate", str
        )
        bed_index = self.bed_combo.findData(saved_bed_type)
        self.bed_combo.setCurrentIndex(max(0, bed_index))
        priority = self.settings.value("print_priority", "balanced", str)
        selected_priorities = {
            part for part in priority.split("+") if part in self.priority_radios
        } or {"balanced"}
        for key, selector in self.priority_radios.items():
            selector.blockSignals(True)
            selector.setChecked(key in selected_priorities)
            selector.blockSignals(False)
        self._set_priority(next(iter(selected_priorities)))
        purpose = self.settings.value("model_purpose", "auto", str)
        purpose_index = self.purpose_combo.findData(purpose)
        self.purpose_combo.setCurrentIndex(max(0, purpose_index))
        intent = self.settings.value("functional_intent", "auto", str)
        intent_index = self.functional_intent_combo.findData(intent)
        self.functional_intent_combo.setCurrentIndex(max(0, intent_index))
        self.timeout_spin.setValue(self.settings.value("timeout", 600, int))
        self.autosave_check.setChecked(self.settings.value("remember_model", True, bool))
        self.tips_check.setChecked(self.settings.value("show_tips", True, bool))
        self.community_learning_check.setChecked(
            self.settings.value("community_learning_enabled", False, bool)
        )
        self.community_endpoint_edit.setText(
            self.settings.value("community_learning_endpoint", "", str)
        )
        self.printer_ip_edit.setText(
            self.settings.value("printer_lan_ip", "", str)
        )
        self.printer_serial_edit.setText(
            self.settings.value("printer_serial", "", str)
        )
        self._printer_mqtt_fingerprint = self.settings.value(
            "printer_mqtt_certificate_sha256", "", str
        )
        self._printer_ftps_fingerprint = self.settings.value(
            "printer_ftps_certificate_sha256", "", str
        )
        self._printer_trusted_identity = self.settings.value(
            "printer_trusted_identity", "", str
        )
        source_index = self.filament_source_combo.findData(
            self.settings.value("printer_filament_source", -1, int)
        )
        self.filament_source_combo.setCurrentIndex(max(0, source_index))
        self.direct_bed_leveling.setChecked(
            self.settings.value("printer_bed_leveling", True, bool)
        )
        self.direct_vibration.setChecked(
            self.settings.value("printer_vibration_calibration", True, bool)
        )
        self.direct_flow.setChecked(
            self.settings.value("printer_flow_calibration", False, bool)
        )
        self.direct_timelapse.setChecked(
            self.settings.value("printer_timelapse", False, bool)
        )
        if (
            self._printer_mqtt_fingerprint
            and self._printer_ftps_fingerprint
            and self._printer_trusted_identity == self._printer_identity()
        ):
            self.printer_connection_status.setText(
                "Сертификаты этого принтера закреплены. Введите код доступа для проверки состояния или отправки."
            )
        elif self._printer_mqtt_fingerprint or self._printer_ftps_fingerprint:
            self._printer_mqtt_fingerprint = ""
            self._printer_ftps_fingerprint = ""
            self._printer_trusted_identity = ""
        if self.autosave_check.isChecked():
            last_model = Path(self.settings.value("last_model", "", str))
            if (
                last_model.is_file()
                and last_model.suffix.lower() in SUPPORTED_MODEL_SUFFIXES
                and last_model.parent == self.workspace.uploads
            ):
                self.model_edit.setText(str(last_model))
                self.output_edit.setText(str(self.workspace.ready))
                self.export_name.setText(f"{last_model.stem}-optimized")
                for canvas in self._canvases:
                    canvas.clear_mesh(f"{last_model.name}  •  нажмите «Повторный анализ»")
        geometry = self.settings.value("geometry")
        if geometry:
            self.restoreGeometry(geometry)
        self._sync_sidebar_profile()

    def _save_settings(self) -> None:
        self.settings.setValue("default_output", str(self.workspace.ready))
        self.settings.setValue("material", str(self.material_combo.currentData()))
        self.settings.setValue("material_profile", self.material_combo.currentText())
        self.settings.setValue("nozzle_diameter_mm", float(self.nozzle_combo.currentData()))
        self.settings.setValue("bed_type", str(self.bed_combo.currentData()))
        self.settings.setValue("print_priority", self.priority_combo.currentData())
        self.settings.setValue("model_purpose", self.purpose_combo.currentData())
        self.settings.setValue("functional_intent", self.functional_intent_combo.currentData())
        self.settings.setValue("slicer_backend", self.engine_combo.currentData())
        self.settings.setValue("timeout", self.timeout_spin.value())
        self.settings.setValue("remember_model", self.autosave_check.isChecked())
        self.settings.setValue("show_tips", self.tips_check.isChecked())
        self.settings.setValue(
            "community_learning_enabled", self.community_learning_check.isChecked()
        )
        self.settings.setValue(
            "community_learning_endpoint", self.community_endpoint_edit.text().strip()
        )
        self.settings.setValue("printer_lan_ip", self.printer_ip_edit.text().strip())
        self.settings.setValue(
            "printer_serial", self.printer_serial_edit.text().strip().upper()
        )
        self.settings.setValue(
            "printer_mqtt_certificate_sha256", self._printer_mqtt_fingerprint
        )
        self.settings.setValue(
            "printer_ftps_certificate_sha256", self._printer_ftps_fingerprint
        )
        self.settings.setValue(
            "printer_trusted_identity", self._printer_trusted_identity
        )
        self.settings.setValue(
            "printer_filament_source", int(self.filament_source_combo.currentData())
        )
        self.settings.setValue(
            "printer_bed_leveling", self.direct_bed_leveling.isChecked()
        )
        self.settings.setValue(
            "printer_vibration_calibration", self.direct_vibration.isChecked()
        )
        self.settings.setValue(
            "printer_flow_calibration", self.direct_flow.isChecked()
        )
        self.settings.setValue("printer_timelapse", self.direct_timelapse.isChecked())
        source = self._current_source()
        if self.autosave_check.isChecked() and source:
            self.settings.setValue("last_model", str(source))
        elif not self.autosave_check.isChecked():
            self.settings.remove("last_model")
        self.settings.setValue("geometry", self.saveGeometry())
        self.statusBar().showMessage("Настройки сохранены", 3000)

    def _save_printer_profile(self) -> None:
        self._save_settings()
        self.printer_defaults.setText(f"{self.printer_combo.currentText()}  •  сопло {self.nozzle_combo.currentText()}  •  {self.bed_combo.currentText()}  •  {self.material_combo.currentText()}")
        self._sync_sidebar_profile()
        self.statusBar().showMessage("Профиль принтера сохранён", 3000)

    def _sync_sidebar_profile(self, _index: int = -1) -> None:
        if not hasattr(self, "sidebar_material") or not hasattr(self, "material_combo"):
            return
        material = self.material_combo.currentText() or "PLA"
        nozzle = self.nozzle_combo.currentText() or "0,4 мм"
        self.sidebar_material.set_content(
            f"{material}  •  сопло {nozzle}",
            "Текущий профиль",
        )
        if hasattr(self, "sidebar_printer") and hasattr(self, "printer_combo"):
            self.sidebar_printer.set_content(
                self.printer_combo.currentText() or "Принтер",
                "Готов к работе",
            )
        if hasattr(self, "printer_defaults"):
            self.printer_defaults.setText(
                f"{self.printer_combo.currentText()}  •  сопло {nozzle}  •  "
                f"{self.bed_combo.currentText()}  •  {material}"
            )
        if _index >= 0 and self.last_result is not None:
            self.last_result = None
            self._update_ui_state("Параметры изменены  •  требуется новая оптимизация")

    def _choose_material_profile(self) -> None:
        dialog = MaterialProfileDialog(
            self.material_combo.currentText(),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        material_index = self.material_combo.findText(dialog.selected_material)
        if material_index >= 0:
            self.material_combo.setCurrentIndex(material_index)
        self._sync_sidebar_profile()
        self._save_settings()
        self.statusBar().showMessage(
            f"Выбран пластик: {dialog.selected_material}",
            4000,
        )

    def _choose_printer_hardware(self) -> None:
        dialog = PrinterHardwareDialog(
            self.printer_combo.currentText(),
            float(self.nozzle_combo.currentData()),
            str(self.bed_combo.currentData()),
            self,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        nozzle_index = self.nozzle_combo.findData(dialog.selected_nozzle_mm)
        if nozzle_index >= 0:
            self.nozzle_combo.setCurrentIndex(nozzle_index)
        bed_index = self.bed_combo.findData(dialog.selected_bed_type)
        if bed_index >= 0:
            self.bed_combo.setCurrentIndex(bed_index)
        self._sync_sidebar_profile()
        self._save_settings()
        self.statusBar().showMessage(
            (
                f"{self.printer_combo.currentText()}: сопло "
                f"{dialog.selected_nozzle_mm:.1f} мм, "
                f"{self.bed_combo.currentText()}"
            ).replace(".", ","),
            4000,
        )

    def _printer_identity(self) -> str:
        return (
            f"{self.printer_ip_edit.text().strip()}|"
            f"{self.printer_serial_edit.text().strip().upper()}"
        )

    def _printer_identity_changed(self) -> None:
        identity = self._printer_identity()
        if self._printer_trusted_identity and identity != self._printer_trusted_identity:
            self._printer_mqtt_fingerprint = ""
            self._printer_ftps_fingerprint = ""
            self._printer_trusted_identity = ""
            self.printer_connection_status.setText(
                "Адрес или серийный номер изменён. Проверьте принтер повторно."
            )
            self._save_settings()
        self._update_ui_state()

    def _direct_printer_config(self) -> PrinterConnectionConfig:
        return PrinterConnectionConfig(
            host=self.printer_ip_edit.text().strip(),
            serial=self.printer_serial_edit.text().strip(),
            access_code=self.printer_access_code_edit.text(),
            mqtt_certificate_sha256=self._printer_mqtt_fingerprint,
            ftps_certificate_sha256=self._printer_ftps_fingerprint,
            timeout_s=min(30.0, max(5.0, float(self.timeout_spin.value()))),
        )

    def _direct_print_options(self) -> PrintDispatchOptions:
        source = int(self.filament_source_combo.currentData())
        return PrintDispatchOptions(
            use_ams=source >= 0,
            ams_slot=source + 1 if source >= 0 else 1,
            bed_leveling=self.direct_bed_leveling.isChecked(),
            vibration_calibration=self.direct_vibration.isChecked(),
            flow_calibration=self.direct_flow.isChecked(),
            timelapse=self.direct_timelapse.isChecked(),
            expected_material=self.material_combo.currentText(),
        )

    def _start_printer_action(
        self,
        action: str,
        *,
        package: Path | None = None,
    ) -> None:
        if self.printer_worker is not None:
            self._show_error("Дождитесь завершения текущей операции с принтером.")
            return
        try:
            config = self._direct_printer_config().validated(
                require_trust=action == "print"
            )
        except Exception as exc:
            self._show_error(str(exc))
            self._show_page("printers")
            return
        worker = PrinterActionWorker(
            action,
            config,
            package=package,
            options=self._direct_print_options(),
        )
        self.printer_worker = worker
        worker.progress.connect(self._on_printer_progress)
        worker.succeeded.connect(self._on_printer_action_succeeded)
        worker.failed.connect(self._on_printer_action_failed)
        worker.finished.connect(self._on_printer_action_finished)
        self.test_printer_button.setEnabled(False)
        self.printer_connection_status.setText("Подключение к принтеру…")
        self._update_ui_state()
        worker.start()

    def _test_printer_connection(self) -> None:
        self._start_printer_action("test")

    def _send_ready_to_printer(self) -> None:
        if not self.last_result:
            self._show_error("Сначала выполните оптимизацию и нарезку.")
            return
        package = Path(str(self.last_result.get("ready_3mf", "")))
        if not package.is_file() or not package.name.casefold().endswith(".gcode.3mf"):
            self._show_error(
                "Для прямой печати нужен проверенный файл VANIOR с расширением .gcode.3mf."
            )
            return
        source = self.filament_source_combo.currentText()
        answer = QMessageBox.question(
            self,
            "Подтверждение печати",
            (
                f"Отправить задание на {self.printer_ip_edit.text().strip()} и запустить печать?\n\n"
                f"Файл: {package.name}\n"
                f"Принтер: Bambu Lab P1S\n"
                f"Материал: {self.material_combo.currentText()}\n"
                f"Источник: {source}\n\n"
                "Перед подтверждением проверьте пластину, сопло, катушку и свободное пространство принтера."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._start_printer_action("print", package=package)

    @Slot(str, int)
    def _on_printer_progress(self, message: str, percent: int) -> None:
        self.printer_connection_status.setText(f"{percent}%  {message}")
        self.statusBar().showMessage(message)

    @Slot(object)
    def _on_printer_action_succeeded(self, result: object) -> None:
        if isinstance(result, PrinterConnectionTest):
            if result.status.state != "UNVERIFIED":
                self.printer_connection_status.setText(
                    f"Принтер проверен и закреплён • состояние {result.status.state}"
                )
                return
            short_mqtt = result.mqtt_certificate_sha256[:16]
            short_ftps = result.ftps_certificate_sha256[:16]
            answer = QMessageBox.question(
                self,
                "Доверие локальному принтеру",
                (
                    f"P1S ответил со статусом {result.status.state}.\n\n"
                    f"IP: {result.host}\nСерийный номер: {result.serial}\n"
                    f"MQTT SHA-256: {short_mqtt}…\nFTPS SHA-256: {short_ftps}…\n\n"
                    "Закрепить эти сертификаты за данным принтером?"
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.Yes,
            )
            if answer == QMessageBox.StandardButton.Yes:
                self._printer_mqtt_fingerprint = result.mqtt_certificate_sha256
                self._printer_ftps_fingerprint = result.ftps_certificate_sha256
                self._printer_trusted_identity = self._printer_identity()
                self._save_settings()
                self.printer_connection_status.setText(
                    "Сертификаты закреплены • проверяю код доступа и состояние…"
                )
                self._printer_retest_after_trust = True
            else:
                self.printer_connection_status.setText(
                    "Сертификаты не закреплены; код доступа не отправлялся, печать заблокирована."
                )
        elif isinstance(result, PrintDispatchResult):
            try:
                append_learning_event(
                    self.workspace,
                    "print_dispatched",
                    {
                        "package_sha256": result.package_sha256,
                        "remote_file": result.remote_file,
                        "uploaded_bytes": result.uploaded_bytes,
                        "confirmed": result.confirmed,
                        "printer_state": result.printer_state,
                        "printer_serial_sha256": hashlib.sha256(
                            self.printer_serial_edit.text().strip().upper().encode("ascii")
                        ).hexdigest().upper(),
                    },
                    application_version=__version__,
                )
            except (OSError, UnicodeEncodeError):
                pass
            self.printer_connection_status.setText(result.message)
            if result.confirmed:
                QMessageBox.information(self, APP_NAME, result.message)
            else:
                QMessageBox.warning(self, APP_NAME, result.message)

    @Slot(str, str)
    def _on_printer_action_failed(self, message: str, details: str) -> None:
        self.printer_connection_status.setText("Операция с принтером не выполнена.")
        self._show_error(message, details)

    @Slot()
    def _on_printer_action_finished(self) -> None:
        worker = self.printer_worker
        self.printer_worker = None
        if worker is not None:
            worker.deleteLater()
        self.test_printer_button.setEnabled(True)
        self._update_ui_state()
        if self._printer_retest_after_trust:
            self._printer_retest_after_trust = False
            QTimer.singleShot(0, self._test_printer_connection)

    # ---------- jobs ----------
    def _job_options(self, operation: str) -> dict[str, Any] | None:
        source = self._current_source()
        if source is None:
            self._show_error("Сначала выберите модель STL или 3MF.")
            return None
        if operation == "slice" and source.suffix.lower() != ".3mf":
            self._show_error("Прямая нарезка доступна только для 3MF.")
            return None
        backend = "vanior"
        slicer_path = ""
        output = next_project_output(source, self.workspace)
        dna_key = PrintDNAKey(
            printer_model=str(self.printer_combo.currentData()),
            nozzle_diameter_mm=float(self.nozzle_combo.currentData()),
            material=self.material_combo.currentText(),
        )
        dna_profile = load_combined_print_dna_profile(
            self.print_dna_path, self.workspace.global_print_dna, dna_key
        )
        support_strategy = self._support_strategy()
        job_id = f"{int(time.time() * 1000)}-{source.stem[:40]}"
        return {
            "source": str(source),
            "output": str(output),
            "material": str(self.material_combo.currentData()),
            "material_profile": self.material_combo.currentText(),
            "printer": str(self.printer_combo.currentData()),
            "nozzle": float(self.nozzle_combo.currentData()),
            "bed_type": str(self.bed_combo.currentData()),
            "print_priority": str(self.priority_combo.currentData()),
            "model_purpose": str(self.purpose_combo.currentData()),
            "functional_intent": str(self.functional_intent_combo.currentData()),
            "plate": self.plate_spin.value(),
            "slicer": slicer_path,
            "slicer_backend": backend,
            "timeout": float(self.timeout_spin.value()),
            "overhang_angle_deg": float(self.support_angle.value()),
            "support_strategy": support_strategy,
            "print_setting_overrides": self._effective_print_setting_overrides(),
            "print_dna_profile": dna_profile.to_dict(),
            "quality_search": bool(self.quality_search.isChecked()),
            "slice_cache_dir": str(self.workspace.system / "slice-cache"),
            "job_id": job_id,
            "journal_path": str(self.workspace.system / "jobs" / f"{job_id}.json"),
        }

    def _support_strategy(self) -> str:
        if not self.manual_settings_enabled and self.optimize_supports.isChecked():
            return "auto"
        if not self.support_enable.isChecked():
            return "none"
        return ("auto", "normal", "tree")[self.support_type.currentIndex()]

    def _effective_print_setting_overrides(self) -> dict[str, Any] | None:
        all_values = self._print_setting_overrides()
        if self.manual_settings_enabled:
            return all_values
        keys: set[str] = set()
        if not self.optimize_layers.isChecked():
            keys.update({"layer_height_mm", "initial_layer_height_mm"})
        if not self.optimize_walls.isChecked():
            keys.update({
                "wall_loops", "top_layers", "bottom_layers", "line_width_mm",
                "top_surface_line_width_mm", "top_surface_pattern", "ironing_enabled",
                "seam_position",
            })
        if not self.optimize_infill.isChecked():
            keys.update({"sparse_infill_percent", "sparse_infill_pattern"})
        if not self.optimize_supports.isChecked():
            keys.update({
                "supports", "support_top_z_distance_mm", "support_bottom_z_distance_mm",
                "support_object_xy_distance_mm", "support_interface_top_layers",
            })
        if not self.optimize_travel.isChecked():
            keys.update({"travel_speed_mm_s", "default_acceleration_mm_s2"})
        return {key: all_values[key] for key in keys} or None

    def _print_setting_overrides(self) -> dict[str, Any]:
        infill_patterns = ("gyroid", "grid", "cubic", "line", "honeycomb")
        top_patterns = ("monotonicline", "concentric", "rectilinear")
        seam_positions = ("aligned", "rear", "nearest", "random")
        return {
            "layer_height_mm": float(self.layer_height.value()),
            "initial_layer_height_mm": float(self.first_layer_height.value()),
            "line_width_mm": float(self.line_width.value()),
            "wall_loops": int(self.wall_loops.value()),
            "top_layers": int(self.top_layers.value()),
            "bottom_layers": int(self.bottom_layers.value()),
            "supports": bool(self.support_enable.isChecked()),
            "brim": bool(self.brim_enable.isChecked()),
            "fan_percent": int(self.fan_percent.value()),
            "sparse_infill_percent": int(self.infill_percent.value()),
            "sparse_infill_pattern": infill_patterns[self.infill_pattern.currentIndex()],
            "top_surface_pattern": top_patterns[self.top_pattern.currentIndex()],
            "top_surface_line_width_mm": float(self.line_width.value()),
            "ironing_enabled": bool(self.ironing.isChecked()),
            "seam_position": seam_positions[self.seam_position.currentIndex()],
            "support_top_z_distance_mm": float(self.support_top_distance.value()),
            "support_bottom_z_distance_mm": float(self.support_top_distance.value()),
            "support_object_xy_distance_mm": float(self.support_xy_distance.value()),
            "support_interface_top_layers": int(self.support_interface_layers.value()),
            "outer_wall_speed_mm_s": int(self.outer_speed.value()),
            "inner_wall_speed_mm_s": int(self.inner_speed.value()),
            "sparse_infill_speed_mm_s": int(self.infill_speed.value()),
            "top_surface_speed_mm_s": int(self.top_speed.value()),
            "travel_speed_mm_s": int(self.travel_speed.value()),
            "bridge_speed_mm_s": int(self.bridge_speed.value()),
            "default_acceleration_mm_s2": int(self.default_accel.value()),
            "outer_wall_acceleration_mm_s2": int(self.outer_accel.value()),
            "top_surface_acceleration_mm_s2": int(self.top_accel.value()),
        }

    def _start_scan(self) -> None:
        self._launch_job("scan")

    def _start_optimize(self) -> None:
        self._launch_job("vanior")

    def _start_job(self) -> None:  # compatibility with 1.x automation
        operation = str(self.mode_combo.currentData())
        if operation in {"optimize", "slice"}:
            operation = "vanior"
        self._launch_job(operation)

    def _launch_job(self, operation: str) -> None:
        if self.thread is not None:
            return
        options = self._job_options(operation)
        if options is None:
            return
        options["request_fingerprint"] = _job_fingerprint(options)
        if operation == "optimize":
            self.last_result = None
        self._save_settings()
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
        self.progress_bar.setValue(0)
        self.log_edit.clear()
        self.status_label.setText("Запуск…")
        self._update_ui_state("Обработка модели…")
        self._show_page("analysis" if operation == "scan" else "optimization")
        self.thread.start()

    @Slot(str, str, int)
    def _on_progress(self, stage: str, message: str, percent: int) -> None:
        self.progress_bar.setValue(percent)
        self.status_label.setText(message)
        self.log_edit.append(f"{percent:3d}%  {message}")
        if stage in {"analyze", "repair", "orient", "slice", "compare"}:
            self.optimization_summary.setText(f"<b>{message}</b><br><br>Этап: {stage}<br>Выполнено: {percent}%")

    @Slot(object)
    def _on_completed(self, payload: object) -> None:
        result = dict(payload)  # type: ignore[arg-type]
        self.progress_bar.setValue(100)
        self.status_label.setText("Операция успешно завершена")
        if result.get("kind") == "scan":
            self.scan_result = result
            self._render_scan(result)
            self._show_page("analysis")
        else:
            try:
                result = self._publish_ready_result(result)
            except Exception as exc:
                self._show_error(f"Не удалось сохранить итоговый файл в назначенную папку: {exc}")
                self.status_label.setText("Результат нарезан, но не опубликован")
                self._update_ui_state("Не удалось сохранить готовый файл")
                return
            self.last_result = result
            self._render_result(result)
            self._append_history(result)
            self._show_page("preview")
            if self.pending_export:
                pending = self.pending_export
                self.pending_export = None
                QTimer.singleShot(0, lambda: self._perform_export(pending))
        self._update_ui_state()

    def _publish_ready_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Publish one verified 3MF, falling back to engineering G-code."""
        project_file = Path(str(result.get("ready_3mf", ""))).resolve()
        gcode_file = Path(str(result.get("ready_gcode", ""))).resolve()
        if not project_file.is_file() and not gcode_file.is_file():
            raise FileNotFoundError("проверенный итоговый файл не найден")
        source = self._current_source()
        name = self.export_name.text().strip() or (
            f"{source.stem}-optimized" if source else "vanior-print-result"
        )
        published_source = project_file if project_file.is_file() else gcode_file
        is_vanior_package = (
            project_file.is_file()
            and project_file.name.casefold().endswith(".gcode.3mf")
        )
        if is_vanior_package:
            verification = verify_vanior_gcode_3mf(project_file)
            if not verification.valid:
                raise ValueError(
                    "самостоятельный 3MF не прошёл проверку: "
                    + "; ".join(verification.errors)
                )
        suffix = (
            ".gcode.3mf"
            if is_vanior_package
            else ".3mf"
            if project_file.is_file()
            else ".gcode"
        )
        destination = next_ready_file(name, suffix, self.workspace)
        shutil.copy2(published_source, destination)
        if workspace_sha256_file(published_source) != workspace_sha256_file(destination):
            destination.unlink(missing_ok=True)
            raise OSError("SHA-256 итогового файла не совпал после сохранения")
        if project_file.is_file():
            result["project_ready_3mf"] = str(project_file)
            result["ready_3mf"] = str(destination)
        else:
            result["project_ready_gcode"] = str(gcode_file)
            result["ready_gcode"] = str(destination)
        result["ready_folder"] = str(self.workspace.ready)

        if source and source.is_file():
            optimization = result.get("quality_optimization") or {}
            audit = result.get("gcode_audit") or {}
            try:
                append_learning_event(
                    self.workspace,
                    "optimization_completed",
                    {
                        "model": {
                            "name": source.name,
                            "format": source.suffix.casefold(),
                            "sha256": workspace_sha256_file(source),
                            "size_bytes": source.stat().st_size,
                        },
                        "printer": result.get("printer", self.printer_combo.currentData()),
                        "nozzle_mm": float(self.nozzle_combo.currentData()),
                        "material": self.material_combo.currentText(),
                        "priority": result.get("print_priority"),
                        "purpose": result.get("model_purpose"),
                        "functional_intent": result.get("functional_intent"),
                        "support_strategy": result.get("strategy"),
                        "print_time_s": result.get("print_time"),
                        "material_g": result.get("mass"),
                        "quality_search": {
                            "protocol_version": optimization.get("protocol_version"),
                            "selected_candidate": optimization.get("selected_candidate"),
                            "quality_score": optimization.get("selected_quality_score"),
                            "reliability_score": optimization.get("selected_reliability_score"),
                            "time_saved_percent": optimization.get("time_saved_percent"),
                        },
                        "gcode_audit": {
                            "status": audit.get("status"),
                            "safety_score": audit.get("safety_score"),
                            "p95_volumetric_flow_mm3_s": audit.get("p95_volumetric_flow_mm3_s"),
                        },
                        "ready_sha256": workspace_sha256_file(destination),
                    },
                    application_version=__version__,
                )
            except OSError as exc:
                self.statusBar().showMessage(
                    f"Итоговый файл сохранён, но журнал обучения недоступен: {exc}",
                    5000,
                )
        return result

    @Slot(str, str)
    def _on_failed(self, message: str, details: str) -> None:
        self.status_label.setText("Операция остановлена из-за ошибки")
        self.log_edit.append(details)
        self._show_error(self._friendly_error(message), details)
        self._update_ui_state("Обработка завершилась ошибкой")

    @Slot()
    def _on_cancelled(self) -> None:
        self.status_label.setText("Операция отменена безопасно")
        self.log_edit.append("Отменено пользователем")
        self._update_ui_state("Обработка отменена")

    @Slot()
    def _job_finished(self) -> None:
        self.thread = None
        self.worker = None
        self.cancel_event = None
        self.run_button.setEnabled(True)
        self.cancel_button.setEnabled(False)
        self._update_ui_state()
        if self.close_when_finished:
            self.close_when_finished = False
            QTimer.singleShot(0, self.close)

    def _cancel_job(self) -> None:
        if self.cancel_event is not None:
            self.cancel_event.set()
            self.cancel_button.setEnabled(False)
            self.status_label.setText("Останавливаю безопасно…")

    # ---------- results ----------
    def _render_scan(self, payload: dict[str, Any]) -> None:
        report = payload["report"]
        metrics = report["metrics"]
        health = report["health"]
        risks = report["risks"]
        purpose = report.get("purpose", {})
        surface = report.get("surface_intelligence", {})
        features = report.get("geometry_features") or {}
        support_exit = report.get("support_exit_plan") or {}
        intent = report.get("functional_intent") or {}
        dimensions = " × ".join(f"{value:.1f}" for value in metrics["dimensions_mm"])
        self.metric_dimensions.value.setText(dimensions + " мм")
        self.metric_volume.value.setText(_format_number(metrics.get("volume_mm3"), " мм³"))
        self.metric_area.value.setText(_format_number(metrics.get("surface_area_mm2"), " мм²"))
        self.metric_triangles.value.setText(_format_number(metrics.get("triangle_count")))
        self.metric_overhang.value.setText(f"{metrics.get('overhang_area_ratio', 0) * 100:.1f}%")
        self.metric_bodies.value.setText(str(metrics.get("body_count", 1)))
        self.issues_list.clear()
        warnings = list(report.get("warnings", []))
        extracted = payload.get("extracted") or {}
        printable_count = int(extracted.get("printable_object_count", 0) or 0)
        object_names = [str(name) for name in extracted.get("object_names", []) if str(name)]
        if printable_count > 1:
            names_text = "; ".join(object_names[:4])
            self.issues_list.addItem(
                f"●  В 3MF находятся {printable_count} печатаемых объекта"
                + (f": {names_text}" if names_text else ".")
            )
        risk_labels = {
            "bed_adhesion": "Адгезия к столу",
            "overhang": "Нависающие элементы",
            "tall_object": "Высокая модель",
            "support_requirement": "Потребность в поддержках",
        }
        risk_values = {
            "LOW": "НИЗКИЙ",
            "MEDIUM": "СРЕДНИЙ",
            "HIGH": "ВЫСОКИЙ",
            "RECOMMENDED": "РЕКОМЕНДУЕТСЯ",
            "REQUIRED": "ОБЯЗАТЕЛЬНО",
        }
        for key, label in risk_labels.items():
            value = str(risks.get(key, "LOW"))
            marker = "●" if value in {"HIGH", "REQUIRED"} else "◆" if value in {"MEDIUM", "RECOMMENDED"} else "✓"
            self.issues_list.addItem(f"{marker}  {label}: {risk_values.get(value, value)}")
        for warning in warnings[:8]:
            if printable_count > 1 and str(warning).startswith(("STL contains ", "Model contains ")):
                continue
            self.issues_list.addItem(f"!  {_warning_text(warning)}")
        if features:
            self.issues_list.addItem(
                f"◇  Локальные мосты: {len(features.get('bridges', []))}; "
                f"тонкие стенки: {len(features.get('thin_walls', []))}"
            )
        if support_exit:
            self.issues_list.addItem(
                "◇  Извлечение supports: "
                f"{float(support_exit.get('accessibility_score', 100)):.0f}/100, "
                f"риск {support_exit.get('overall_risk', 'LOW')}"
            )
        classification = {"decorative": "декоративная", "functional": "нагруженная", "ambiguous": "неоднозначная"}.get(purpose.get("classification"), "не определена")
        object_summary = (
            f"В проекте <b>{printable_count} печатаемых объекта</b>; будут обработаны оба. "
            if printable_count > 1
            else ""
        )
        self.analysis_recommendation.setText(
            object_summary
            + f"Состояние сетки: <b>{_health_text(health['status'])}</b>. "
            f"Назначение: <b>{classification}</b>, уверенность {str(purpose.get('confidence', 'LOW')).lower()}. "
            f"Сценарий: <b>{intent.get('category', 'авто')}</b>. "
            f"Рекомендуемый слой: <b>{report['settings']['layer_height_mm']:.2f} мм</b>; "
            f"стенок: <b>{report['settings']['wall_loops']}</b>; заполнение: <b>{report['settings'].get('sparse_infill_percent', 15)}%</b>."
        )
        role_names = {
            "bed_contact": "контакт со столом",
            "top_visible": "верхние видимые",
            "support_contact": "контакт с поддержками",
            "curved_visible": "криволинейные видимые",
            "precision_candidate": "кандидаты точной посадки",
            "visible_wall": "видимые стенки",
        }
        role_lines = [
            f"<b>{role_names.get(str(item.get('role')), str(item.get('role')))}</b>: "
            f"{float(item.get('area_ratio', 0)) * 100:.1f}%"
            for item in surface.get("roles", [])
            if float(item.get("area_ratio", 0)) >= 0.001
        ]
        decisions = [str(item) for item in surface.get("recommendations", [])]
        self.surface_summary.setText(
            "Распознано по геометрии: "
            + ("  •  ".join(role_lines) if role_lines else "данных недостаточно")
            + ("<br><br>" + "<br>".join(f"✓ {item}" for item in decisions) if decisions else "")
        )
        self._load_settings_from_report(report["settings"])
        source = self._current_source()
        if source:
            preview_mesh = payload.get("preview_mesh", {})
            for canvas in self._canvases:
                canvas.set_mesh(preview_mesh, f"{source.name}  •  {dimensions} мм")
            height_mm = float(metrics["dimensions_mm"][2])
            layer_height_mm = max(float(report["settings"].get("layer_height_mm", 0.2)), 0.04)
            estimated_layers = max(1, round(height_mm / layer_height_mm))
            self._layer_heights_mm = []
            self.layer_slider.setRange(1, estimated_layers)
            self.layer_slider.setValue(estimated_layers)

    def _load_settings_from_report(self, settings: dict[str, Any]) -> None:
        self.layer_height.setValue(float(settings.get("layer_height_mm", 0.2)))
        self.first_layer_height.setValue(float(settings.get("initial_layer_height_mm", 0.2)))
        self.line_width.setValue(float(settings.get("line_width_mm", settings.get("top_surface_line_width_mm", 0.42))))
        self.wall_loops.setValue(int(settings.get("wall_loops", 3)))
        self.infill_percent.setValue(int(settings.get("sparse_infill_percent", 15)))
        self.top_layers.setValue(int(settings.get("top_layers", 5)))
        self.bottom_layers.setValue(int(settings.get("bottom_layers", 4)))
        self.fan_percent.setValue(int(settings.get("fan_percent", 100)))
        self.outer_speed.setValue(int(settings.get("outer_wall_speed_mm_s", 200)))
        self.inner_speed.setValue(int(settings.get("inner_wall_speed_mm_s", 300)))
        self.infill_speed.setValue(int(settings.get("sparse_infill_speed_mm_s", 270)))
        self.top_speed.setValue(int(settings.get("top_surface_speed_mm_s", 200)))
        self.travel_speed.setValue(int(settings.get("travel_speed_mm_s", 500)))
        self.bridge_speed.setValue(int(settings.get("bridge_speed_mm_s", 50)))
        self.default_accel.setValue(int(settings.get("default_acceleration_mm_s2", 10000)))
        self.outer_accel.setValue(int(settings.get("outer_wall_acceleration_mm_s2", 5000)))
        self.top_accel.setValue(int(settings.get("top_surface_acceleration_mm_s2", 2000)))
        self.support_enable.setChecked(bool(settings.get("supports", False)))
        self.brim_enable.setChecked(bool(settings.get("brim", False)))
        self.ironing.setChecked(bool(settings.get("ironing_enabled", False)))
        self.advanced_profile_text.setPlainText(json.dumps(settings, ensure_ascii=False, indent=2))

    def _apply_print_settings(self) -> None:
        self.manual_settings_enabled = True
        self.last_result = None
        self.statusBar().showMessage("Настройки проекта сохранены; автоматическая оптимизация учтёт выбранный режим и назначение модели", 5000)
        self._show_page("optimization")

    @Slot(int)
    def _on_layer_changed(self, value: int) -> None:
        total = max(1, self.layer_slider.maximum())
        z_mm: float | None = None
        if self._layer_heights_mm:
            index = min(max(0, value - 1), len(self._layer_heights_mm) - 1)
            z_mm = self._layer_heights_mm[index]
            maximum_z = max(self._layer_heights_mm[-1], 1e-9)
            fraction = z_mm / maximum_z
        else:
            fraction = value / total
        self.layer_label.setText(
            f"{value} / {total}" + (f"  •  Z {z_mm:.2f} мм" if z_mm is not None else "")
        )
        self.preview_canvas.set_layer(value, total, fraction=fraction, z_mm=z_mm)

    @Slot(bool)
    def _toggle_model_paths(self, visible: bool) -> None:
        for role in (
            "model", "outer_wall", "inner_wall", "top_surface", "bottom_surface",
            "infill", "solid_infill", "bridge", "skirt_brim",
        ):
            self.preview_canvas.set_path_role_visible(role, visible)

    @Slot(bool)
    def _toggle_support_paths(self, visible: bool) -> None:
        for role in ("support", "support_interface"):
            self.preview_canvas.set_path_role_visible(role, visible)

    def _render_result(self, payload: dict[str, Any]) -> None:
        strategy_names = {"none": "без поддержек", "normal": "обычные поддержки", "tree": "древовидные поддержки", "object-specific": "индивидуально для каждой модели"}
        priority_names = {"quality": "максимальное качество", "strength": "максимальная прочность", "balanced": "баланс качества, скорости и прочности", "fast": "быстрая печать"}
        priority_value = str(payload.get("print_priority", ""))
        priority_label = " + ".join(
            priority_names.get(item, item) for item in priority_value.split("+") if item
        ) or "—"
        if payload.get("kind") == "vanior-slice":
            lines = [
                "<b>Печатный 3MF VANIOR Slice создан</b>",
                ("До физической валидации профиля обязательно проверьте все слои; "
                "результат не помечен как серийно готовый."),
            ]
        else:
            lines = ["<b>Готовый проект создан</b>"]
        if payload.get("strategy"):
            lines.append(f"Поддержки: {strategy_names.get(payload['strategy'], payload['strategy'])}")
        support_comparison = payload.get("support_comparison") or []
        if support_comparison:
            lines.append("Сравнение поддержек:")
            for candidate in support_comparison:
                name = strategy_names.get(
                    str(candidate.get("strategy")), str(candidate.get("strategy", "—"))
                )
                eligibility = "допущен" if candidate.get("eligible") else "отклонён по геометрии"
                selected = " • выбран" if candidate.get("selected") else ""
                lines.append(
                    f"• {name}: {eligibility}, "
                    f"{float(candidate.get('estimated_support_mass_g', 0)):.2f} г, "
                    f"{_format_duration(float(candidate.get('estimated_support_time_s', 0)))}"
                    f"{selected}"
                )
        if payload.get("support_mass") is not None and float(payload["support_mass"]) > 0:
            lines.append(
                f"Из них поддержки: {float(payload['support_mass']):.2f} г / "
                f"{_format_duration(float(payload.get('support_time', 0)))}"
            )
        if payload.get("print_time") is not None:
            lines.append(f"Время печати: {_format_duration(float(payload['print_time']))}")
        if payload.get("mass") is not None:
            lines.append(f"Материал: {float(payload['mass']):.1f} г")
        lines.append(f"Режим: {priority_label}")
        if payload.get("profile_setting_count"):
            lines.append(f"Параметров профиля: {payload['profile_setting_count']}")
        orientation_export = payload.get("orientation_export") or {}
        if orientation_export:
            rotation = orientation_export.get("rotation_deg") or (0, 0, 0)
            lines.append(
                "Положение на столе: "
                f"поворот X/Y/Z {float(rotation[0]):.0f}° / "
                f"{float(rotation[1]):.0f}° / {float(rotation[2]):.0f}°; "
                f"оценка {float(orientation_export.get('score', 0)):.1f}/100"
            )
        object_optimizations = payload.get("object_optimizations") or []
        if object_optimizations:
            lines.append(
                f"Индивидуально настроено моделей: <b>{len(object_optimizations)}</b>"
            )
            for item in object_optimizations:
                analysis = item.get("analysis") or {}
                settings = analysis.get("settings") or {}
                purpose = analysis.get("purpose") or {}
                support_name = strategy_names.get(
                    str(item.get("support_mode")), str(item.get("support_mode", "—"))
                )
                lines.append(
                    f"• {item.get('name', 'Модель')}: "
                    f"{purpose.get('classification', 'auto')}, "
                    f"слой {float(settings.get('layer_height_mm', 0.2)):.2f} мм, "
                    f"стенок {settings.get('wall_loops', '—')}, "
                    f"заполнение {settings.get('sparse_infill_percent', '—')}%, "
                    f"{support_name}"
                )
                object_orientation = item.get("orientation_export") or {}
                if object_orientation:
                    rotation = object_orientation.get("rotation_deg") or (0, 0, 0)
                    lines.append(
                        "&nbsp;&nbsp;положение: "
                        f"X/Y/Z {float(rotation[0]):.0f}° / "
                        f"{float(rotation[1]):.0f}° / {float(rotation[2]):.0f}°"
                    )
        surface = payload.get("surface_intelligence") or {}
        if surface.get("recommendations"):
            lines.append(
                f"Surface Intelligence: применено решений {len(surface['recommendations'])}"
            )
        source_assessment = payload.get("source_profile_assessment") or {}
        if source_assessment:
            lines.append(
                "Исходный 3MF оценён: "
                f"{float(source_assessment.get('quality_score', 0)):.0f}/100; "
                "калибровки материала сохранены и улучшены."
            )
        intent = payload.get("functional_intent") or {}
        if intent:
            lines.append(
                f"Functional Intent: {intent.get('category', 'auto')} "
                f"({str(intent.get('confidence', 'LOW')).lower()})"
            )
        local = payload.get("local_modifier_plan") or {}
        if local:
            lines.append(
                f"Локальная оптимизация: {len(local.get('ranges', []))} диапазонов по высоте"
            )
        support_exit = payload.get("support_exit_plan") or {}
        if support_exit:
            lines.append(
                f"Доступность supports: {float(support_exit.get('accessibility_score', 100)):.0f}/100"
            )
        audit = payload.get("gcode_audit") or {}
        if audit:
            lines.append(
                f"Аудит G-code: {audit.get('status', '—')}, "
                f"безопасность {float(audit.get('safety_score', 0)):.0f}/100, "
                f"P95 потока {float(audit.get('p95_volumetric_flow_mm3_s', 0)):.1f} мм³/с"
            )
        dna = payload.get("print_dna_application") or {}
        if dna.get("sample_count"):
            lines.append(
                f"PrintDNA: учтено отпечатков {dna['sample_count']}, "
                f"поправок {len(dna.get('adjustments', {}))}"
            )
        protocol = payload.get("quality_optimization") or {}
        if protocol:
            evaluations = protocol.get("evaluations") or []
            accepted_count = sum(1 for item in evaluations if item.get("accepted"))
            if str(protocol.get("method", "")).startswith("geometry-cost-model"):
                lines.append(
                    f"Оптимизация v{protocol.get('protocol_version', 3)}: "
                    f"{protocol.get('selected_title', 'выбран лучший вариант')}; "
                    "финальный G-code проверен"
                )
                lines.append(
                    f"Оценено планов настроек: {len(evaluations)}; "
                    f"прошли ограничения: {accepted_count}; "
                    f"расчётная экономия {float(protocol.get('projected_time_saved_percent', 0)):.1f}%"
                )
            else:
                lines.append(
                    f"Оптимизация v{protocol.get('protocol_version', 3)}: "
                    f"{protocol.get('selected_title', 'выбран лучший вариант')}; "
                    f"экономия времени {float(protocol.get('time_saved_percent', 0)):.1f}%"
                )
                lines.append(
                    f"Проверено реальных нарезок: {len(evaluations)}; "
                    f"прошли ограничения: {accepted_count}"
                )
                lines.append(
                    f"Сравнение времени: {_format_duration(float(protocol.get('baseline_time_s', 0)))} "
                    f"→ {_format_duration(float(protocol.get('selected_time_s', 0)))}"
                )
            lines.append(
                f"Контроль: качество {float(protocol.get('selected_quality_score', 0)):.1f} "
                f"(порог {float(protocol.get('quality_floor', 0)):.1f}), "
                f"надёжность {float(protocol.get('selected_reliability_score', 0)):.1f} "
                f"(порог {float(protocol.get('reliability_floor', 0)):.1f})"
            )
        layer_z_mm = [float(value) for value in payload.get("layer_z_mm", []) if float(value) > 0]
        if layer_z_mm:
            lines.append(f"Слоёв: {len(layer_z_mm)}")
        role_counts = {
            str(key): int(value)
            for key, value in (payload.get("layer_role_counts") or {}).items()
        }
        support_segments = role_counts.get("support", 0) + role_counts.get("support_interface", 0)
        self.show_support_paths.setEnabled(support_segments > 0)
        self.show_support_paths.setChecked(support_segments > 0)
        if support_segments:
            lines.append(
                "Предпросмотр поддержек: "
                f"{role_counts.get('support', 0)} траекторий, "
                f"интерфейс {role_counts.get('support_interface', 0)}"
            )
        if payload.get("reason"):
            lines.append(str(payload["reason"]))
        html = "<br>".join(lines)
        self.preview_info.setText(html)
        self.export_summary.setText(html)
        self.optimization_summary.setText(html)
        self.result_title.setText(
            "Инженерный G-code создан"
            if payload.get("ready_gcode") and not payload.get("ready_3mf")
            else "Итоговый 3MF готов к печати"
        )
        self.result_text.setText(html)
        self.result_stack.setCurrentIndex(1)
        ready_3mf = Path(str(payload.get("ready_3mf", ""))).is_file()
        ready_gcode = Path(str(payload.get("ready_gcode", ""))).is_file()
        self.open_3mf_button.setEnabled(ready_3mf or ready_gcode)
        self.open_3mf_button.setText(
            "Открыть готовый 3MF" if ready_3mf else "Показать готовый G-code"
        )
        self.open_gcode_button.setEnabled(ready_gcode)
        preview_mesh = payload.get("preview_mesh")
        if preview_mesh:
            dimensions = " × ".join(
                f"{float(value):.1f}" for value in payload.get("dimensions", [])
            )
            caption = str(payload.get("preview_caption") or "Оптимизированная модель")
            self.preview_canvas.set_mesh(preview_mesh, f"{caption}  •  {dimensions} мм")
            self.optimize_canvas.set_mesh(preview_mesh, f"{caption}  •  {dimensions} мм")
        self.preview_canvas.set_layer_paths(
            payload.get("layer_paths", []),
            layer_z_mm,
        )
        self._layer_heights_mm = layer_z_mm
        if layer_z_mm:
            layer_count = len(layer_z_mm)
        else:
            layer_count = max(1, round((payload.get("dimensions") or [0, 0, 1])[2] / max(self.layer_height.value(), 0.04)))
        self.layer_slider.setMaximum(layer_count)
        self.layer_slider.setValue(self.layer_slider.maximum())
        self._on_layer_changed(self.layer_slider.value())

    # ---------- export/open ----------
    def _export_project(self) -> None:
        export_format = "gcode" if self.export_gcode.isChecked() else "3mf"
        operation = "vanior"
        current_options = self._job_options(operation)
        result_is_current = bool(
            self.last_result
            and current_options
            and self.last_result.get("request_fingerprint")
            == _job_fingerprint(current_options)
        )
        if not result_is_current:
            self.pending_export = export_format
            self._start_optimize()
            return
        self._perform_export(export_format)

    def _perform_export(self, export_format: str) -> None:
        if not self.last_result:
            return
        folder = self.workspace.ready
        folder.mkdir(parents=True, exist_ok=True)
        name = self.export_name.text().strip() or (self._current_source().stem + "-optimized" if self._current_source() else "vanior-print-result")
        try:
            if export_format == "3mf":
                source_file = Path(self.last_result.get("ready_3mf", ""))
                if not source_file.is_file():
                    raise FileNotFoundError("готовый 3MF не найден")
                if source_file.parent.resolve() == folder.resolve():
                    destination = source_file
                else:
                    suffix = (
                        ".gcode.3mf"
                        if source_file.name.casefold().endswith(".gcode.3mf")
                        else ".3mf"
                    )
                    destination = next_ready_file(name, suffix, self.workspace)
                    shutil.copy2(source_file, destination)
            else:
                existing_gcode = Path(self.last_result.get("ready_gcode", ""))
                if existing_gcode.is_file():
                    if existing_gcode.parent.resolve() == folder.resolve():
                        destination = existing_gcode
                    else:
                        destination = next_ready_file(name, ".gcode", self.workspace)
                        shutil.copy2(existing_gcode, destination)
                else:
                    project = Path(self.last_result.get("ready_3mf", ""))
                    destination = next_ready_file(name, ".gcode", self.workspace)
                    self._extract_embedded_gcode(project, destination)
            self.settings.setValue("export_dir", str(self.workspace.ready))
            QMessageBox.information(self, APP_NAME, f"Готов один итоговый файл:\n{destination}")
            subprocess.Popen(["explorer.exe", "/select,", str(destination)])
        except Exception as exc:
            self._show_error(f"Не удалось экспортировать файл: {exc}")

    @staticmethod
    def _unused_file(path: Path) -> Path:
        if not path.exists():
            return path
        for index in range(2, 1000):
            candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
            if not candidate.exists():
                return candidate
        raise RuntimeError("не удалось подобрать свободное имя файла")

    @staticmethod
    def _extract_embedded_gcode(project: Path, destination: Path) -> None:
        if not project.is_file():
            raise FileNotFoundError("готовый 3MF не найден")
        with zipfile.ZipFile(project) as archive:
            names = [name for name in archive.namelist() if name.lower().endswith(".gcode")]
            if not names:
                raise RuntimeError("в итоговом 3MF нет встроенного G-code")
            preferred = next((name for name in names if "plate_1" in name.lower()), names[0])
            with archive.open(preferred) as source, destination.open("xb") as target:
                shutil.copyfileobj(source, target)

    def _open_ready_3mf(self) -> None:
        if not self.last_result:
            self._show_error("Сначала выполните оптимизацию.")
            return
        path = Path(self.last_result.get("ready_3mf", ""))
        if not path.is_file():
            self._show_error("Готовый 3MF не найден.")
            return
        try:
            if (
                self.last_result.get("kind") == "vanior-slice"
                or path.name.casefold().endswith(".gcode.3mf")
            ):
                verification = verify_vanior_gcode_3mf(path)
                if not verification.valid:
                    path = upgrade_legacy_vanior_gcode_3mf(path)
                    self.last_result["ready_3mf"] = str(path)
                    verification = verify_vanior_gcode_3mf(path)
                if not verification.valid:
                    raise ValueError("; ".join(verification.errors))
                os.startfile(path)  # type: ignore[attr-defined]
                self.statusBar().showMessage(
                    "3MF проверен, при необходимости обновлён и открыт.",
                    8000,
                )
            else:
                os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:
            self._show_error(f"Не удалось открыть готовый 3MF: {exc}")

    def _open_ready_result(self) -> None:
        if not self.last_result:
            self._show_error("Сначала выполните оптимизацию.")
            return
        project = Path(str(self.last_result.get("ready_3mf", "")))
        if project.is_file():
            self._open_ready_3mf()
            return
        gcode = Path(str(self.last_result.get("ready_gcode", "")))
        if gcode.is_file():
            self._show_ready_gcode()
            return
        self._show_error("Готовый файл был перемещён или удалён.")

    def _show_ready_gcode(self) -> None:
        if not self.last_result:
            return
        path = Path(self.last_result.get("ready_gcode", ""))
        if path.is_file():
            subprocess.Popen(["explorer.exe", "/select,", str(path)])
        else:
            self._show_page("export")
            self.export_gcode.setChecked(True)

    def _open_result_folder(self) -> None:
        if self.last_result:
            output = Path(self.last_result.get("output", ""))
            if output.is_dir():
                os.startfile(output)  # type: ignore[attr-defined]
                return
        self._show_error("Папка результата больше не существует.")

    def _open_legal_notices(self) -> None:
        if getattr(sys, "frozen", False):
            legal = Path(sys.executable).resolve().parent / "_internal" / "legal"
        else:
            legal = Path(__file__).resolve().parents[2] / "docs"
        if not legal.is_dir():
            self._show_error("Каталог условий и лицензий не найден.")
            return
        try:
            os.startfile(legal)  # type: ignore[attr-defined]
        except OSError as exc:
            self._show_error(f"Не удалось открыть каталог условий и лицензий: {exc}")

    # ---------- history ----------
    def _append_history(self, payload: dict[str, Any]) -> None:
        source = self._current_source()
        entry = {
            "id": datetime.now(UTC).strftime("%Y%m%d%H%M%S%f"),
            "created": datetime.now(UTC).isoformat(timespec="seconds"),
            "name": source.stem if source else "Проект",
            "source": str(source or ""),
            "output": str(payload.get("output", "")),
            "ready_3mf": str(payload.get("ready_3mf", "")),
            "ready_gcode": str(payload.get("ready_gcode", "")),
            "print_time": payload.get("print_time"),
            "mass": payload.get("mass"),
            "priority": payload.get("print_priority"),
            "strategy": payload.get("strategy"),
            "printer": payload.get("printer", self.printer_combo.currentData()),
            "nozzle": float(self.nozzle_combo.currentData()),
            "material_profile": self.material_combo.currentText(),
            "print_dna_application": payload.get("print_dna_application"),
        }
        self.history.insert(0, entry)
        self.history = self.history[:100]
        _write_json(self.history_path, self.history)
        self._refresh_history()
        self._refresh_overview()

    def _refresh_history(self) -> None:
        if not hasattr(self, "history_list"):
            return
        self.history_list.clear()
        for entry in self.history:
            ready = Path(str(entry.get("ready_3mf", ""))).is_file() or Path(
                str(entry.get("ready_gcode", ""))
            ).is_file()
            availability = "Готов к открытию" if ready else "Файл недоступен"
            item = QListWidgetItem(
                f"{entry.get('name', 'Проект')}\n"
                f"{_format_history_date(entry.get('created'))}   •   "
                f"{_format_duration(float(entry.get('print_time') or 0))}   •   {availability}"
            )
            item.setData(Qt.ItemDataRole.UserRole, entry.get("id"))
            self.history_list.addItem(item)
        self._refresh_print_dna_projects()

    def _refresh_print_dna_projects(self) -> None:
        if not hasattr(self, "dna_project_combo"):
            return
        selected = self.dna_project_combo.currentData()
        self.dna_project_combo.blockSignals(True)
        self.dna_project_combo.clear()
        for entry in self.history:
            self.dna_project_combo.addItem(
                f"{entry.get('name', 'Проект')}  •  {_format_history_date(entry.get('created'))}",
                entry.get("id"),
            )
        if selected:
            index = self.dna_project_combo.findData(selected)
            if index >= 0:
                self.dna_project_combo.setCurrentIndex(index)
        self.dna_project_combo.blockSignals(False)
        self.dna_save_button.setEnabled(bool(self.history))
        self._refresh_print_dna_profile()

    def _print_dna_history_entry(self) -> dict[str, Any] | None:
        if not hasattr(self, "dna_project_combo"):
            return None
        identifier = self.dna_project_combo.currentData()
        return next((item for item in self.history if item.get("id") == identifier), None)

    def _print_dna_key_for_entry(self, entry: dict[str, Any] | None) -> PrintDNAKey:
        return PrintDNAKey(
            printer_model=str(
                (entry or {}).get("printer") or self.printer_combo.currentData()
            ),
            nozzle_diameter_mm=float(
                (entry or {}).get("nozzle") or self.nozzle_combo.currentData()
            ),
            material=str(
                (entry or {}).get("material_profile") or self.material_combo.currentText()
            ),
        )

    def _refresh_print_dna_profile(self, *_: Any) -> None:
        if not hasattr(self, "dna_profile_summary"):
            return
        entry = self._print_dna_history_entry()
        key = self._print_dna_key_for_entry(entry)
        profile = load_combined_print_dna_profile(
            self.print_dna_path, self.workspace.global_print_dna, key
        )
        confidence_names = {
            "NONE": "нет данных",
            "LOW": "низкая",
            "MEDIUM": "средняя",
            "HIGH": "высокая",
        }
        self.dna_profile_summary.setText(
            f"<b>{key.printer_model}</b>  •  сопло {key.nozzle_diameter_mm:.1f} мм  •  "
            f"{key.material.upper()}<br>Учтено физических отпечатков: "
            f"<b>{profile.sample_count}</b>  •  уверенность: "
            f"<b>{confidence_names.get(profile.confidence, profile.confidence)}</b>"
        )
        labels = {
            "top_layers_delta": "добавить слой верхней крышки",
            "top_surface_speed_multiplier": "снизить скорость верхней поверхности",
            "support_z_delta_mm": "увеличить зазор поддержек",
            "support_spacing_delta_mm": "увеличить шаг интерфейса поддержек",
            "nozzle_temperature_delta_c": "скорректировать температуру сопла",
            "seam_position": "перенести и сгладить шов",
            "wall_loops_delta": "добавить контур стенки",
            "infill_delta_percent": "увеличить заполнение",
            "outer_wall_speed_multiplier": "снизить скорость внешней стенки",
            "outer_wall_acceleration_multiplier": "снизить ускорение внешней стенки",
            "visible_layer_height_cap_mm": "уменьшить слой на видимых поверхностях",
            "wall_generator": "защитить органические поверхности классическими стенками",
            "support_interface_layers_delta": "уплотнить интерфейс поддержек",
            "support_gap_delta_mm": "откалибровать верхний зазор поддержек",
            "support_interface_spacing_delta_mm": "уменьшить шаг интерфейса поддержек",
            "support_interface_speed_multiplier": "замедлить интерфейс поддержек",
            "stringing_motion_guard": "сократить открытые перемещения против нитей",
            "retraction_length_delta_mm": "точнее настроить длину ретракта",
            "retraction_speed_delta_mm_s": "точнее настроить скорость ретракта",
            "wipe_distance_delta_mm": "увеличить очистку сопла перед перемещением",
            "protect_visible_surfaces_from_bed": "защитить видимые поверхности от стола",
            "brim": "включить кайму",
            "bed_temperature_delta_c": "повысить температуру стола",
        }
        if profile.adjustments:
            lines = [f"✓ {labels.get(name, name)}" for name in profile.adjustments]
            self.dna_adjustments.setText(
                "Поправки будут автоматически применены к следующей оптимизации:<br>"
                + "<br>".join(lines)
            )
        else:
            self.dna_adjustments.setText(
                "Активных поправок пока нет. PrintDNA применяет изменения только "
                "после зафиксированного отклонения качества."
            )

    def _save_print_dna_feedback(self) -> None:
        entry = self._print_dna_history_entry()
        if entry is None:
            self._show_error("Сначала выберите завершённый проект из истории.")
            return
        feedback = PrintFeedback(
            project_id=str(entry.get("id", "")),
            source_name=str(entry.get("name", "Проект")),
            quality_rating=int(self.dna_quality.currentData()),
            support_removal_rating=self.dna_support_rating.currentData(),
            dimensional_rating=self.dna_dimension_rating.currentData(),
            defects=tuple(
                key for key, checkbox in self.dna_defects.items() if checkbox.isChecked()
            ),
            notes=self.dna_notes.toPlainText(),
            photo_path=self.dna_photo_edit.text().strip(),
            defect_layer_index=(self.dna_layer.value() or None),
            defect_region=str(self.dna_region.currentData()),
            parameter_snapshot={
                "printer": entry.get("printer"),
                "nozzle": entry.get("nozzle"),
                "material": entry.get("material_profile"),
                "priority": entry.get("priority"),
                "purpose": entry.get("purpose"),
                "ready_3mf": Path(str(entry.get("ready_3mf", ""))).name,
            },
        )
        try:
            profile = record_print_feedback(
                self.print_dna_path,
                self._print_dna_key_for_entry(entry),
                feedback,
            )
        except Exception as exc:
            self._show_error(f"Не удалось сохранить PrintDNA: {exc}")
            return
        entry["print_dna_feedback_count"] = int(
            entry.get("print_dna_feedback_count", 0)
        ) + 1
        _write_json(self.history_path, self.history)
        try:
            append_learning_event(
                self.workspace,
                "print_feedback",
                {
                    "project_id": feedback.project_id,
                    "source_name": feedback.source_name,
                    "printer": entry.get("printer"),
                    "nozzle_mm": entry.get("nozzle"),
                    "material": entry.get("material_profile"),
                    "quality_rating": feedback.quality_rating,
                    "support_removal_rating": feedback.support_removal_rating,
                    "dimensional_rating": feedback.dimensional_rating,
                    "defects": list(feedback.defects),
                    "defect_layer_index": feedback.defect_layer_index,
                    "defect_region": feedback.defect_region,
                    "profile_sample_count": profile.sample_count,
                },
                application_version=__version__,
            )
        except OSError:
            pass
        self.dna_notes.clear()
        self.dna_photo_edit.clear()
        self.dna_layer.setValue(0)
        self.dna_region.setCurrentIndex(0)
        for checkbox in self.dna_defects.values():
            checkbox.setChecked(False)
        self._refresh_print_dna_profile()
        self.statusBar().showMessage(
            f"PrintDNA обновлён: учтено отпечатков {profile.sample_count}",
            5000,
        )
        if self.community_learning_check.isChecked():
            self._start_community_sync(quiet=True)
        QMessageBox.information(
            self,
            APP_NAME,
            "Результат сохранён. Безопасные поправки PrintDNA будут учтены "
            "при следующей оптимизации этой связки принтера и материала.",
        )

    def _refresh_overview(self) -> None:
        if not hasattr(self, "recent_list"):
            return
        self.recent_list.clear()
        for entry in self.history[:5]:
            ready = Path(str(entry.get("ready_3mf", ""))).is_file() or Path(
                str(entry.get("ready_gcode", ""))
            ).is_file()
            item = QListWidgetItem()
            item.setData(Qt.ItemDataRole.UserRole, entry.get("id"))
            item.setSizeHint(QSize(100, 62))
            self.recent_list.addItem(item)
            self.recent_list.setItemWidget(
                item,
                RecentProjectRow(
                    str(entry.get("name", "Проект")),
                    _format_history_date(entry.get("created")),
                    ready,
                ),
            )
        if not self.history:
            item = QListWidgetItem("История пока пуста\nЗагрузите первую модель")
            item.setFlags(Qt.ItemFlag.NoItemFlags)
            item.setSizeHint(QSize(100, 76))
            self.recent_list.addItem(item)

    def _history_entry(self, item: QListWidgetItem | None) -> dict[str, Any] | None:
        if item is None:
            return None
        identifier = item.data(Qt.ItemDataRole.UserRole)
        return next((entry for entry in self.history if entry.get("id") == identifier), None)

    def _history_selected(self, current: QListWidgetItem | None, previous: QListWidgetItem | None = None) -> None:
        entry = self._history_entry(current)
        if not entry:
            self.history_details.setText("Выберите проект в списке.")
            self.history_open_button.setEnabled(False)
            self.history_folder_button.setEnabled(False)
            self.history_delete_button.setEnabled(False)
            return
        ready_3mf = Path(str(entry.get("ready_3mf", ""))).is_file()
        ready_gcode = Path(str(entry.get("ready_gcode", ""))).is_file()
        ready = ready_3mf or ready_gcode
        folder = Path(str(entry.get("output", ""))).is_dir()
        self.history_open_button.setEnabled(ready)
        self.history_folder_button.setEnabled(folder)
        self.history_delete_button.setEnabled(True)
        self.history_details.setText(
            f"<b>{entry.get('name', 'Проект')}</b><br><br>Создан: {_format_history_date(entry.get('created'))}<br>"
            f"Время печати: {_format_duration(float(entry.get('print_time') or 0))}<br>"
            f"Материал: {_format_number(entry.get('mass'), ' г')}<br>Режим: {entry.get('priority', '—')}<br>"
            f"Поддержки: {entry.get('strategy', '—')}<br>"
            f"Состояние: {'готов к открытию' if ready else 'итоговый файл перемещён или удалён'}"
            f"<br><br>{entry.get('ready_3mf') or entry.get('ready_gcode', '')}"
        )

    def _open_history_item(self, item: QListWidgetItem) -> None:
        entry = self._history_entry(item)
        if not entry:
            return
        project = Path(entry.get("ready_3mf", ""))
        gcode = Path(entry.get("ready_gcode", ""))
        if project.is_file():
            self.last_result = dict(entry)
            self.last_result["kind"] = "history"
            self._open_ready_3mf()
        elif gcode.is_file():
            subprocess.Popen(["explorer.exe", "/select,", str(gcode)])
        else:
            self._show_error("Итоговый файл из этой записи был перемещён или удалён.")

    def _open_selected_history(self) -> None:
        self._open_history_item(self.history_list.currentItem()) if self.history_list.currentItem() else None

    def _open_selected_history_folder(self) -> None:
        entry = self._history_entry(self.history_list.currentItem())
        if entry:
            folder = Path(entry.get("output", ""))
            if folder.is_dir():
                os.startfile(folder)  # type: ignore[attr-defined]
            else:
                self._show_error("Папка этого проекта больше не существует.")

    def _delete_selected_history(self) -> None:
        entry = self._history_entry(self.history_list.currentItem())
        if not entry:
            return
        self.history = [item for item in self.history if item.get("id") != entry.get("id")]
        _write_json(self.history_path, self.history)
        self._refresh_history(); self._refresh_overview()
        self._history_selected(None)

    # ---------- errors/events/style ----------
    @staticmethod
    def _friendly_error(message: str) -> str:
        translations = (
            ("requires manual repair", "Модель требует ручного исправления геометрии."),
            ("slicing engine was not found", "Встроенный движок нарезки отсутствует или повреждён. Переустановите VANIOR PRINT."),
            ("does not fit", "Модель не помещается в рабочую область принтера."),
            ("output already exists", "Папка результата уже существует."),
        )
        for needle, translated in translations:
            if needle.lower() in message.lower():
                return translated + f"\n\nТехническая причина: {message}"
        return message or "Неизвестная ошибка обработки."

    def _show_error(self, message: str, details: str = "") -> None:
        box = QMessageBox(self); box.setIcon(QMessageBox.Icon.Critical); box.setWindowTitle(APP_NAME); box.setText(message)
        if details:
            box.setDetailedText(details)
        box.exec()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        urls = event.mimeData().urls()
        if len(urls) == 1 and Path(urls[0].toLocalFile()).suffix.lower() in SUPPORTED_MODEL_SUFFIXES:
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        self._model_selected(event.mimeData().urls()[0].toLocalFile()); event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.printer_worker is not None and self.printer_worker.isRunning():
            QMessageBox.information(
                self,
                APP_NAME,
                "Дождитесь завершения защищённой передачи задания на принтер.",
            )
            event.ignore()
            return
        if self.learning_sync_worker is not None and self.learning_sync_worker.isRunning():
            QMessageBox.information(
                self,
                APP_NAME,
                "Дождитесь завершения безопасной синхронизации PrintDNA.",
            )
            event.ignore()
            return
        if self.thread is not None:
            answer = QMessageBox.question(self, "Идёт обработка", "Отменить обработку и закрыть приложение?", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore(); return
            if self.cancel_event is not None:
                self.cancel_event.set()
            self.close_when_finished = True; event.ignore(); return
        self._save_settings(); event.accept()

    def _apply_style(self) -> None:
        style = """
            QMainWindow, QWidget#appRoot, QWidget#pageContent, QScrollArea, QStackedWidget { background: qradialgradient(cx:0.55,cy:0.08,radius:1.1, stop:0 #111326, stop:0.42 #080b18, stop:1 #060811); color: #f5f4fa; }
            QFrame#sidebar { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #0c0d1c, stop:0.48 #090a16, stop:1 #070811); border-right: 1px solid #26233a; }
            QLabel#sidebarBrand { background: transparent; border: 1px solid transparent; border-radius: 13px; }
            QLabel#sidebarBrand:hover { background: #141226; border-color: #312449; }
            QPushButton#navRailButton, QPushButton#sidebarQuickAction { background: transparent; border: 1px solid transparent; border-radius: 13px; padding: 0; min-width: 48px; max-width: 48px; min-height: 48px; max-height: 48px; }
            QPushButton#navRailButton:hover, QPushButton#sidebarQuickAction:hover { background: #171328; border-color: #473062; }
            QPushButton#navRailButton:checked { background: qradialgradient(cx:0.5, cy:0.36, radius:0.82, stop:0 #5e2ca2, stop:0.62 #2c1c50, stop:1 #18142c); border-color: #8b55d5; }
            QPushButton#navRailButton:focus, QPushButton#sidebarQuickAction:focus { border-color: #b66dff; }
            QFrame#sidebarDivider { background: #2a2540; border: none; max-height: 1px; }
            QToolTip { color: #f7f4ff; background: #151226; border: 1px solid #68469e; border-radius: 8px; padding: 7px 10px; font-size: 12px; }
            QFrame#deviceFrame { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #121627, stop:1 #0c101d); border: 1px solid #2e3449; border-radius: 12px; min-height: 64px; }
            QFrame#deviceFrame:hover { background: #17162c; border-color: #7950a6; }
            QFrame#deviceFrame:focus { border: 2px solid #9550e6; }
            QLabel#deviceTitle { background: transparent; color: #f4f3f8; font-size: 12px; font-weight: 650; }
            QLabel#deviceSubtitle { background: transparent; color: #aab1c1; font-size: 11px; }
            QLabel#deviceChevron { background: transparent; color: #c7bad8; font-size: 18px; }
            QDialog#profileDialog { background: #0b0d19; color: #f5f4fa; }
            QLabel#dialogTitle { color: #ffffff; font-size: 22px; font-weight: 700; }
            QLabel#profileHint { color: #c9b1e5; background: #171126; border: 1px solid #47276b; border-radius: 8px; padding: 10px; }
            QLabel#pageTitle { font-size: 28px; font-weight: 700; color: #faf9fd; }
            QLabel#subtitle, QLabel#muted { color: #9ca4b8; }
            QLabel#sectionTitle { font-size: 16px; font-weight: 650; color: #faf7ff; }
            QLabel#featureTitle { font-size: 16px; font-weight: 650; color: #faf7ff; }
            QLabel#metricValue { font-size: 16px; font-weight: 650; color: #ffffff; }
            QLabel#dropTitle { color: white; font-size: 21px; font-weight: 600; }
            QLabel#badges { color: #ede8f6; background: #0f1120; border: 1px solid #3b2855; border-radius: 9px; padding: 12px; font-size: 14px; }
            QLabel#success { color: #36d46a; }
            QLabel#status { color: #cdb4f7; font-weight: 600; }
            QLabel#resultTitle { color: #65f6ae; font-size: 18px; font-weight: 700; }
            QLabel#projectState { background: #0d101e; border: 1px solid #2c3045; border-radius: 10px; color: #d9d5e4; padding: 13px 16px; font-weight: 600; font-size: 14px; }
            QLabel#projectState[ready="true"] { background: #0b211a; border-color: #1c684c; color: #6de5ad; }
            QFrame#card, QFrame#metricCard { background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 #111426, stop:1 #0c0f1c); border: 1px solid #2a2f45; border-radius: 12px; }
            QFrame#featureCard { background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #15182d, stop:0.55 #101326, stop:1 #0b0e1a); border: 1px solid #30364d; border-radius: 14px; }
            QFrame#featureCard:hover { border-color: #5d347f; background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #191a34, stop:1 #101022); }
            QLabel#featureBadge { color: #b875f1; background: #1b1230; border: 1px solid #4d276e; border-radius: 8px; padding: 4px 9px; font-size: 10px; font-weight: 700; }
            QLabel#featureDetail { color: #b9b0c7; background: #0c101d; border: 1px solid #272d41; border-radius: 8px; padding: 7px 9px; font-size: 11px; }
            QFrame#metricCard { background: #121725; }
            QFrame#dropZone { background: qradialgradient(cx:0.5,cy:0.44,radius:0.76, stop:0 #17102c, stop:0.6 #0d1020, stop:1 #0a0d19); border: 1px dashed #55317b; border-radius: 14px; }
            QFrame#dropZone:hover { border-color: #a85cf3; background: #171227; }
            QFrame#modelCanvas { background: qradialgradient(cx:0.5,cy:0.42,radius:0.8, stop:0 #151226, stop:1 #090c16); border: 1px solid #2d3347; border-radius: 12px; }
            QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTextEdit, QListWidget {
                background: #0b0f1b; border: 1px solid #2d3448; border-radius: 8px; color: #f0f2f7; padding: 7px 9px; min-height: 20px; selection-background-color: #6530a8;
            }
            QLineEdit#modelPath { min-height: 32px; padding-left: 8px; }
            QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QTextEdit:focus, QListWidget:focus { border-color: #8b48d8; }
            QComboBox QAbstractItemView { background: #101421; color: #f8f9fc; border: 1px solid #3b435a; selection-background-color: #60309d; selection-color: #ffffff; outline: 0; }
            QListWidget#recentList { background: #0a0d19; border: 1px solid #252b3d; border-radius: 10px; padding: 0; }
            QListWidget#recentList::item { padding: 0; border-bottom: 1px solid #252a3b; }
            QListWidget#recentList::item:selected { background: #19132c; color: #ffffff; }
            QListWidget::item { padding: 10px 8px; border-bottom: 1px solid #22283a; }
            QListWidget::item:selected { background: #38205d; color: #ffffff; border-radius: 6px; }
            QLabel#recentIcon { background: #17132a; border: 1px solid #3d2362; border-radius: 7px; color: #9e52ec; font-size: 20px; }
            QLabel#recentTitle { color: #f4f1f8; font-weight: 600; }
            QLabel#recentMeta { color: #969daf; font-size: 11px; }
            QLabel#recentReady { color: #3ee58c; border: 2px solid #29c977; border-radius: 12px; font-weight: 700; }
            QLabel#recentMissing { color: #ff535b; border: 2px solid #ed3c46; border-radius: 12px; font-weight: 700; }
            QLabel#recentMenu { color: #b2aabb; font-size: 20px; padding-left: 4px; }
            QPushButton { background: #151a2a; border: 1px solid #333b50; border-radius: 8px; color: #edf0f7; padding: 8px 14px; min-height: 19px; }
            QPushButton:hover { background: #20263a; border-color: #675083; }
            QPushButton:disabled { color: #62697a; background: #0d111c; border-color: #202637; }
            QPushButton#primary { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #6d28c7, stop:1 #792fce); border-color: #9550e6; color: #ffffff; font-weight: 600; padding: 10px 19px; }
            QPushButton#primary:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #7d35d8, stop:1 #8b42dc); }
            QPushButton#primary:disabled { color: #646b7a; background: #0d111c; border-color: #22283a; }
            QPushButton#featureButton { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #51208a, stop:1 #3b1769); border: 1px solid #7132b4; color: #f5effc; font-size: 14px; padding: 10px 18px; }
            QPushButton#featureButton:hover { background: qlineargradient(x1:0,y1:0,x2:1,y2:0, stop:0 #682aac, stop:1 #4c1e83); border-color: #9550de; }
            QPushButton#linkButton { background: transparent; border: 0; color: #ad5bef; padding: 4px 7px; }
            QPushButton#linkButton:hover { color: #d198ff; background: transparent; }
            QPushButton#danger { color: #ff5c67; }
            QProgressBar { background: #101522; border: 1px solid #293044; border-radius: 6px; text-align: center; height: 17px; }
            QProgressBar::chunk { background: #7935ce; border-radius: 5px; }
            QTabWidget::pane { border: 1px solid #2b3144; border-radius: 10px; background: #0b0f1a; top: -1px; }
            QTabBar::tab { background: transparent; color: #a4abbb; padding: 10px 18px; border-bottom: 2px solid transparent; }
            QTabBar::tab:selected { color: #ffffff; border-bottom-color: #9d55ec; }
            QCheckBox, QRadioButton { color: #f2edf7; spacing: 9px; min-height: 24px; }
            QCheckBox:checked, QRadioButton:checked { color: #ffffff; }
            QCheckBox::indicator { width: 18px; height: 18px; border: 2px solid #586077; border-radius: 5px; background: #0b0f19; }
            QCheckBox::indicator:hover { border-color: #a967eb; }
            QCheckBox::indicator:checked { image: url("__CHECKMARK__"); background: #7030be; border-color: #a967eb; }
            QRadioButton::indicator { width: 18px; height: 18px; border: 2px solid #586077; border-radius: 10px; background: #0b0f19; }
            QRadioButton::indicator:hover { border-color: #a967eb; }
            QRadioButton::indicator:checked { image: url("__RADIO_DOT__"); background: #7030be; border-color: #a967eb; }
            QSlider::groove:horizontal { background: #252b3e; height: 5px; border-radius: 2px; }
            QSlider::sub-page:horizontal { background: #7133bc; border-radius: 2px; }
            QSlider::handle:horizontal { background: #a75aef; width: 15px; margin: -5px 0; border-radius: 7px; }
            QScrollBar:vertical { background: #0a0d17; width: 11px; }
            QScrollBar::handle:vertical { background: #343b51; border-radius: 5px; min-height: 30px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            """
        self.setStyleSheet(
            style.replace("__CHECKMARK__", CHECKMARK_PATH.as_posix()).replace(
                "__RADIO_DOT__", RADIO_DOT_PATH.as_posix()
            )
        )


def build_parser() -> Any:
    import argparse

    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--smoke-test", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--screenshot", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--page", choices=[key for key, _, _ in MainWindow.PAGE_NAMES], help=argparse.SUPPRESS)
    parser.add_argument("--self-test-model", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--self-test-result", type=Path, help=argparse.SUPPRESS)
    parser.add_argument(
        "--independent-self-test-output", type=Path, help=argparse.SUPPRESS
    )
    parser.add_argument(
        "--independent-self-test-package", type=Path, help=argparse.SUPPRESS
    )
    parser.add_argument("--gpu-test-model", type=Path, help=argparse.SUPPRESS)
    parser.add_argument("--gpu-test-result", type=Path, help=argparse.SUPPRESS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.self_test_model:
        if args.self_test_result is None:
            return 2
        try:
            model = args.self_test_model.expanduser().resolve()
            if args.independent_self_test_output is not None:
                report = analyze_stl(model, material="PLA", nozzle_diameter_mm=0.4)
                sliced = slice_stl_to_gcode(
                    model,
                    args.independent_self_test_output.expanduser().resolve(),
                    report.settings,
                    material="PLA",
                    nozzle_diameter_mm=0.4,
                    progress_callback=lambda _event: None,
                    cancel_event=Event(),
                )
                result = sliced.to_dict()
                if args.independent_self_test_package is not None:
                    package = create_vanior_gcode_3mf(
                        model,
                        sliced.gcode_path,
                        args.independent_self_test_package.expanduser().resolve(),
                        report.settings,
                        sliced,
                        material="PLA",
                    )
                    verification = verify_vanior_gcode_3mf(package)
                    if not verification.valid:
                        raise RuntimeError("; ".join(verification.errors))
                    result["ready_3mf"] = str(package)
                    result["package_entries"] = list(verification.entries)
            else:
                result = scan_model(
                    model,
                    None,
                    material="PLA",
                    plate=1,
                    progress_callback=None,
                    cancel_event=Event(),
                )
            args.self_test_result.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
        except Exception:
            args.self_test_result.write_text(traceback.format_exc(), encoding="utf-8")
            return 1
    app = QApplication.instance() or QApplication(sys.argv[:1])
    app.setOrganizationName(ORGANIZATION)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setApplicationVersion(__version__)
    app.setWindowIcon(QIcon(str(ICON_PATH)))
    app.setStyle("Fusion")
    app.setFont(QFont("Segoe UI", 10))
    if args.gpu_test_model:
        if args.gpu_test_result is None:
            return 2
        try:
            mesh = build_preview_mesh(args.gpu_test_model.expanduser().resolve())
            canvas = ModelCanvas()
            canvas.resize(900, 650)
            canvas.set_mesh(mesh, args.gpu_test_model.name)
            canvas.show()

            def finish_gpu_test() -> None:
                samples: list[float] = []
                for step in ([1.0, -1.0] * 12):
                    started = time.perf_counter()
                    canvas.zoom_view(step)
                    canvas.repaint()
                    app.processEvents()
                    samples.append((time.perf_counter() - started) * 1000.0)
                image_path = args.gpu_test_result.with_suffix(".png")
                image_path.parent.mkdir(parents=True, exist_ok=True)
                image = canvas.grabFramebuffer()
                saved = image.save(str(image_path))
                ordered = sorted(samples)
                report = {
                    "ok": bool(canvas._gl_ready and not canvas._gl_error and saved),
                    "gl_ready": canvas._gl_ready,
                    "gl_error": canvas._gl_error,
                    "gl_version": (
                        f"{canvas.context().format().majorVersion()}."
                        f"{canvas.context().format().minorVersion()}"
                    ),
                    "detail_triangles": len(canvas._faces),
                    "median_frame_ms": ordered[len(ordered) // 2],
                    "p95_frame_ms": ordered[max(0, int(len(ordered) * 0.95) - 1)],
                    "image": str(image_path),
                }
                args.gpu_test_result.write_text(
                    json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                canvas.close()
                app.exit(0 if report["ok"] else 1)

            QTimer.singleShot(350, finish_gpu_test)
            return app.exec()
        except Exception:
            args.gpu_test_result.parent.mkdir(parents=True, exist_ok=True)
            args.gpu_test_result.write_text(traceback.format_exc(), encoding="utf-8")
            return 1
    window = MainWindow()
    if args.page:
        window._show_page(args.page)
    window.show()
    if args.screenshot:
        destination = args.screenshot.expanduser().resolve()
        def save_screenshot() -> None:
            destination.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(destination)); app.quit()
        QTimer.singleShot(900, save_screenshot)
    elif args.smoke_test:
        QTimer.singleShot(350, app.quit)
    else:
        QTimer.singleShot(700, window._show_support_prompt_once)
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
