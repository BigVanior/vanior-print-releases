"""Translate geometric intent into bounded strength and accuracy requirements."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from typing import Any

import numpy as np
import trimesh

from .report import PrintSettings, PurposeAssessment

FUNCTIONAL_INTENTS = (
    "auto",
    "decorative",
    "enclosure",
    "fixture",
    "gear",
    "snap_fit",
    "vessel",
    "flexible",
    "structural",
    "general_functional",
)


@dataclass(frozen=True)
class FunctionalIntent:
    version: int
    category: str
    confidence: str
    load_axis: str
    dimensional_priority: str
    watertight_required: bool
    flexibility_required: bool
    impact_resistance_required: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def normalize_functional_intent(value: str) -> str:
    normalized = value.strip().casefold().replace("-", "_").replace(" ", "_")
    if normalized not in FUNCTIONAL_INTENTS:
        raise ValueError(
            "unsupported functional intent: " + value + "; choose " + ", ".join(FUNCTIONAL_INTENTS)
        )
    return normalized


def infer_functional_intent(
    mesh: trimesh.Trimesh,
    purpose: PurposeAssessment,
    *,
    requested: str = "auto",
) -> FunctionalIntent:
    requested = normalize_functional_intent(requested)
    dimensions = np.asarray(mesh.extents, dtype=np.float64)
    order = np.argsort(dimensions)
    axis_names = np.asarray(["X", "Y", "Z"])
    longest_axis = str(axis_names[int(order[-1])])
    shortest_axis = str(axis_names[int(order[0])])
    largest = max(float(dimensions.max()), 1e-9)
    smallest = float(dimensions.min())
    flatness = smallest / largest
    fill = purpose.bounding_box_fill_ratio
    axis = purpose.axis_aligned_surface_ratio
    curved = purpose.curved_surface_ratio
    reasons: list[str] = []

    if requested != "auto":
        category = requested
        confidence = "USER"
        reasons.append("Назначение задано пользователем и имеет приоритет над геометрической оценкой.")
    elif purpose.classification == "decorative":
        category = "decorative"
        confidence = purpose.confidence
        reasons.append("Органические криволинейные поверхности указывают на декоративную модель.")
    elif flatness <= 0.18 and curved >= 0.28 and abs(dimensions[0] - dimensions[1]) / largest < 0.20:
        category = "gear"
        confidence = "MEDIUM"
        reasons.append("Плоская радиально-подобная форма рассматривается как передающий элемент.")
    elif fill is not None and fill <= 0.32 and axis >= 0.45 and largest >= 35:
        category = "enclosure"
        confidence = "MEDIUM"
        reasons.append("Низкое заполнение габаритного объёма и плоские стенки похожи на корпус.")
    elif purpose.functional_score >= 0.72 and flatness <= 0.35:
        category = "structural"
        confidence = "MEDIUM"
        reasons.append("Выраженная функциональная геометрия и вытянутая форма предполагают нагрузку.")
    elif purpose.functional_score >= 0.58 and largest <= 60:
        category = "snap_fit"
        confidence = "LOW"
        reasons.append("Малая функциональная деталь может содержать защёлки или упругие посадки.")
    else:
        category = "fixture" if purpose.functional_score >= 0.5 else "decorative"
        confidence = "LOW"
        reasons.append("Геометрия неоднозначна; выбран безопасный общий функциональный класс.")

    if category in {"gear", "enclosure", "snap_fit", "fixture"}:
        dimensional = "HIGH"
    elif category in {"structural", "general_functional"}:
        dimensional = "MEDIUM"
    else:
        dimensional = "NORMAL"
    load_axis = shortest_axis if category in {"gear", "snap_fit"} else longest_axis
    watertight = category == "vessel"
    flexibility = category in {"flexible", "snap_fit"}
    impact = category in {"structural", "fixture", "snap_fit"}
    return FunctionalIntent(
        version=1,
        category=category,
        confidence=confidence,
        load_axis=load_axis,
        dimensional_priority=dimensional,
        watertight_required=watertight,
        flexibility_required=flexibility,
        impact_resistance_required=impact,
        reasons=tuple(reasons),
    )


def resolve_functional_intent(
    automatic: FunctionalIntent | None,
    requested: str,
) -> FunctionalIntent:
    """Resolve a user category without requiring geometry to be loaded again."""
    selected = normalize_functional_intent(requested)
    if automatic is None:
        automatic = FunctionalIntent(
            version=1,
            category="decorative",
            confidence="LOW",
            load_axis="Z",
            dimensional_priority="NORMAL",
            watertight_required=False,
            flexibility_required=False,
            impact_resistance_required=False,
            reasons=("Назначение восстановлено из совместимого отчёта предыдущей версии.",),
        )
    if selected == "auto":
        return automatic
    return replace(
        automatic,
        category=selected,
        confidence="USER",
        dimensional_priority=(
            "HIGH" if selected in {"enclosure", "fixture", "gear", "snap_fit", "vessel"} else "MEDIUM"
        ),
        watertight_required=selected == "vessel",
        flexibility_required=selected in {"flexible", "snap_fit"},
        impact_resistance_required=selected in {"structural", "fixture", "snap_fit"},
        reasons=("Назначение задано пользователем и имеет приоритет над геометрической оценкой.",),
    )


def settings_for_functional_intent(
    base: PrintSettings,
    intent: FunctionalIntent,
) -> tuple[PrintSettings, tuple[str, ...]]:
    """Apply surface-safe settings for the resolved functional category."""
    category = intent.category
    changes: dict[str, object] = {"functional_intent": category}
    decisions: list[str] = []
    if category == "decorative":
        changes.update(sparse_infill_percent=min(base.sparse_infill_percent, 15))
        decisions.append("Декоративный объект: материал экономится внутри, поверхности защищены.")
    elif category == "enclosure":
        changes.update(
            wall_loops=max(4, base.wall_loops),
            sparse_infill_percent=max(12, min(base.sparse_infill_percent, 20)),
            sparse_infill_pattern="gyroid",
            elephant_foot_compensation_mm=0.15,
        )
        decisions.append("Корпус: усилены стенки и компенсирован первый слой для посадочных размеров.")
    elif category == "gear":
        changes.update(
            wall_loops=max(6, base.wall_loops),
            sparse_infill_percent=max(40, base.sparse_infill_percent),
            sparse_infill_pattern="gyroid",
            outer_wall_speed_mm_s=min(80, base.outer_wall_speed_mm_s),
            outer_wall_acceleration_mm_s2=min(1800, base.outer_wall_acceleration_mm_s2),
        )
        decisions.append("Передающий элемент: увеличены оболочка, заполнение и размерная точность зубьев.")
    elif category == "snap_fit":
        changes.update(
            wall_loops=max(5, base.wall_loops),
            sparse_infill_percent=max(25, base.sparse_infill_percent),
            sparse_infill_pattern="gyroid",
            outer_wall_speed_mm_s=min(90, base.outer_wall_speed_mm_s),
        )
        decisions.append("Защёлка: приоритет непрерывным стенкам и сопротивлению циклическому изгибу.")
    elif category == "vessel":
        changes.update(
            wall_loops=max(6, base.wall_loops),
            bottom_layers=max(6, base.bottom_layers),
            top_layers=max(6, base.top_layers),
            sparse_infill_percent=max(20, base.sparse_infill_percent),
            line_width_mm=max(0.44, base.line_width_mm),
        )
        decisions.append("Герметичная деталь: увеличена непрерывная оболочка и перекрытие линий.")
    elif category == "flexible":
        changes.update(
            wall_loops=max(3, base.wall_loops),
            sparse_infill_percent=min(18, base.sparse_infill_percent),
            sparse_infill_pattern="gyroid",
            default_acceleration_mm_s2=min(5000, base.default_acceleration_mm_s2),
        )
        decisions.append("Гибкая деталь: снижены заполнение и динамика, сохранена непрерывность стенок.")
    elif category in {"fixture", "structural", "general_functional"}:
        walls = 6 if category == "structural" else 5
        infill = 35 if category == "structural" else 30
        changes.update(
            wall_loops=max(walls, base.wall_loops),
            bottom_layers=max(5, base.bottom_layers),
            sparse_infill_percent=max(infill, base.sparse_infill_percent),
            sparse_infill_pattern="gyroid",
        )
        decisions.append("Нагруженная деталь: усилена оболочка и внутренний силовой объём.")
    return replace(base, **changes), tuple(decisions)
