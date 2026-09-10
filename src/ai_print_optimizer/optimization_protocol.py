"""Quality-constrained search for the fastest safe print settings."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from typing import Any

from .material_protocol import settings_for_material
from .report import AnalysisReport, PrintSettings


@dataclass(frozen=True)
class OptimizationCandidate:
    identifier: str
    title: str
    settings: PrintSettings
    quality_score: float
    reliability_score: float
    eligible: bool
    constraints: tuple[str, ...]
    changes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["settings"] = asdict(self.settings)
        return value


@dataclass(frozen=True)
class OptimizationPlan:
    protocol_version: int
    quality_floor: float
    reliability_floor: float
    candidates: tuple[OptimizationCandidate, ...]
    geometry_facts: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "protocol_version": self.protocol_version,
            "quality_floor": self.quality_floor,
            "reliability_floor": self.reliability_floor,
            "candidates": [item.to_dict() for item in self.candidates],
            "geometry_facts": list(self.geometry_facts),
        }


@dataclass(frozen=True)
class CandidateSliceMetrics:
    success: bool
    print_time_s: float | None
    material_g: float | None
    warnings: tuple[str, ...] = ()
    error: str = ""
    audit_score: float = 100.0
    blocking_warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class CandidateEvaluation:
    identifier: str
    title: str
    quality_score: float
    reliability_score: float
    eligible: bool
    success: bool
    accepted: bool
    print_time_s: float | None
    material_g: float | None
    rejection_reasons: tuple[str, ...]
    changes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class QualityOptimizationResult:
    protocol_version: int
    quality_floor: float
    reliability_floor: float
    selected_candidate: str
    selected_title: str
    selected_quality_score: float
    selected_reliability_score: float
    baseline_time_s: float
    selected_time_s: float
    time_saved_s: float
    time_saved_percent: float
    baseline_material_g: float
    selected_material_g: float
    material_saved_g: float
    evaluations: tuple[CandidateEvaluation, ...]
    reason: str

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evaluations"] = [item.to_dict() for item in self.evaluations]
        return value


def _thresholds(priority: str, purpose: str) -> tuple[float, float]:
    floors = {"quality": 96.0, "strength": 92.0, "balanced": 92.0, "fast": 88.0}
    parts = [part for part in priority.split("+") if part in floors]
    quality = sum(floors[part] for part in parts) / len(parts) if parts else 92.0
    # Selecting quality together with another objective means "find the best
    # compromise without sacrificing quality", not randomly weaken the quality
    # gate by averaging it away.
    if "quality" in parts:
        quality = max(96.0, quality)
    reliability = 97.0 if "strength" in parts else 95.0 if purpose == "functional" else 92.0
    return quality, reliability


def _candidate_scores(
    report: AnalysisReport,
    candidate: PrintSettings,
    baseline: PrintSettings,
) -> tuple[float, float, tuple[str, ...]]:
    surface = report.surface_intelligence
    visible_complexity = min(
        1.0,
        surface.ratio("top_visible")
        + surface.ratio("curved_visible")
        + 0.7 * surface.ratio("precision_candidate"),
    )
    quality_penalty = 0.0
    reliability_penalty = 0.0
    constraints: list[str] = []

    if candidate.layer_height_mm > baseline.layer_height_mm:
        steps = (candidate.layer_height_mm - baseline.layer_height_mm) / 0.04
        quality_penalty += steps * (2.5 + 4.5 * visible_complexity)
    if candidate.wall_loops < baseline.wall_loops:
        delta = baseline.wall_loops - candidate.wall_loops
        quality_penalty += 2.0 * delta
        reliability_penalty += 7.0 * delta
    if candidate.top_layers < baseline.top_layers:
        quality_penalty += 3.0 * (baseline.top_layers - candidate.top_layers)
    if candidate.bottom_layers < baseline.bottom_layers:
        reliability_penalty += 5.0 * (baseline.bottom_layers - candidate.bottom_layers)
    if candidate.sparse_infill_percent < baseline.sparse_infill_percent:
        delta = baseline.sparse_infill_percent - candidate.sparse_infill_percent
        reliability_penalty += delta * (0.8 if candidate.model_purpose == "functional" else 0.15)
    if candidate.outer_wall_speed_mm_s > baseline.outer_wall_speed_mm_s:
        quality_penalty += (candidate.outer_wall_speed_mm_s / baseline.outer_wall_speed_mm_s - 1.0) * 12.0
    if candidate.top_surface_speed_mm_s > baseline.top_surface_speed_mm_s:
        quality_penalty += (candidate.top_surface_speed_mm_s / baseline.top_surface_speed_mm_s - 1.0) * 14.0
    if candidate.outer_wall_acceleration_mm_s2 > baseline.outer_wall_acceleration_mm_s2:
        quality_penalty += (
            candidate.outer_wall_acceleration_mm_s2
            / max(baseline.outer_wall_acceleration_mm_s2, 1)
            - 1.0
        ) * (12.0 + 18.0 * visible_complexity)
    if candidate.wall_generator != baseline.wall_generator:
        quality_penalty += 12.0 * visible_complexity
    if baseline.ironing_enabled and not candidate.ironing_enabled:
        quality_penalty += 4.0
    if candidate.infill_combination:
        reliability_penalty += 1.0 if candidate.model_purpose == "decorative" else 5.0
    if report.risks.tall_object != "LOW" and candidate.default_acceleration_mm_s2 > baseline.default_acceleration_mm_s2:
        reliability_penalty += 3.0
    if report.risks.bed_adhesion == "HIGH" and not candidate.brim:
        reliability_penalty += 20.0
    if report.risks.overhang == "HIGH" and candidate.bridge_speed_mm_s > baseline.bridge_speed_mm_s:
        quality_penalty += 4.0
    if candidate.max_volumetric_speed_mm3_s > baseline.max_volumetric_speed_mm3_s:
        quality_penalty += min(
            18.0,
            (candidate.max_volumetric_speed_mm3_s / max(baseline.max_volumetric_speed_mm3_s, 1.0) - 1.0)
            * (12.0 + 18.0 * visible_complexity),
        )
        reliability_penalty += min(
            15.0,
            (candidate.max_volumetric_speed_mm3_s / max(baseline.max_volumetric_speed_mm3_s, 1.0) - 1.0)
            * 30.0,
        )
    if candidate.filament_flow_ratio != baseline.filament_flow_ratio:
        quality_penalty += abs(candidate.filament_flow_ratio - baseline.filament_flow_ratio) * 180.0
        reliability_penalty += abs(candidate.filament_flow_ratio - baseline.filament_flow_ratio) * 100.0
    if candidate.nozzle_temperature_c != baseline.nozzle_temperature_c:
        reliability_penalty += 30.0
        constraints.append("температура материала сохраняется")
    if candidate.fan_percent != baseline.fan_percent:
        reliability_penalty += 20.0
        constraints.append("охлаждение материала сохраняется")

    minimum_walls = min(
        baseline.wall_loops,
        4 if candidate.model_purpose == "functional" else 2,
    )
    minimum_infill = min(
        baseline.sparse_infill_percent,
        20 if candidate.model_purpose == "functional" else 8,
    )
    minimum_top = min(
        baseline.top_layers,
        max(5, round(0.8 / max(candidate.layer_height_mm, 0.04))),
    )
    minimum_top_thickness = min(
        baseline.top_shell_thickness_mm,
        baseline.top_layers * baseline.layer_height_mm,
    )
    minimum_bottom_thickness = baseline.bottom_layers * baseline.layer_height_mm
    if candidate.wall_loops < minimum_walls:
        reliability_penalty += 25.0
        constraints.append(f"не менее {minimum_walls} стенок")
    if candidate.sparse_infill_percent < minimum_infill:
        reliability_penalty += 20.0
        constraints.append(f"заполнение не менее {minimum_infill}%")
    if candidate.top_layers < minimum_top:
        quality_penalty += 20.0
        constraints.append(f"толщина верхней оболочки не менее {minimum_top} слоёв")
    if candidate.top_layers * candidate.layer_height_mm + 1e-9 < minimum_top_thickness:
        quality_penalty += 25.0
        constraints.append(
            f"верхняя оболочка не тоньше {minimum_top_thickness:.2f} мм"
        )
    if candidate.bottom_layers * candidate.layer_height_mm + 1e-9 < minimum_bottom_thickness:
        reliability_penalty += 25.0
        constraints.append(
            f"нижняя оболочка не тоньше {minimum_bottom_thickness:.2f} мм"
        )
    if candidate.initial_layer_speed_mm_s > baseline.initial_layer_speed_mm_s:
        reliability_penalty += 15.0
        constraints.append("первый слой не ускоряется")
    if candidate.support_interface_speed_mm_s > baseline.support_interface_speed_mm_s:
        quality_penalty += 15.0
        constraints.append("интерфейс supports не ускоряется")
    if candidate.detect_thin_wall:
        constraints.append("сохранение тонких стенок")
    if candidate.detect_floating_vertical_shell:
        constraints.append("контроль плавающих вертикальных оболочек")
    constraints.append("внешние и верхние поверхности не ускоряются")
    return (
        round(max(0.0, 100.0 - quality_penalty), 2),
        round(max(0.0, 100.0 - reliability_penalty), 2),
        tuple(dict.fromkeys(constraints)),
    )


def _changes(baseline: PrintSettings, candidate: PrintSettings) -> tuple[str, ...]:
    labels = {
        "layer_height_mm": "адаптивный предел высоты слоя",
        "inner_wall_speed_mm_s": "ускорены внутренние стенки",
        "sparse_infill_speed_mm_s": "ускорено заполнение",
        "internal_solid_infill_speed_mm_s": "ускорено сплошное заполнение",
        "default_acceleration_mm_s2": "оптимизировано ускорение",
        "sparse_infill_percent": "убрано избыточное заполнение",
        "infill_combination": "объединены слои заполнения",
        "reduce_crossing_wall": "снижены пересечения видимых стенок",
        "detect_thin_wall": "включено сохранение тонких стенок",
        "bridge_no_support": "включена печать безопасных мостов без supports",
    }
    before = asdict(baseline)
    after = asdict(candidate)
    return tuple(label for key, label in labels.items() if before.get(key) != after.get(key))


def build_optimization_plan(
    report: AnalysisReport,
    *,
    allow_setting_changes: bool = True,
    nozzle_diameter_mm: float = 0.4,
    locked_fields: frozenset[str] = frozenset(),
    material_profile: str | None = None,
) -> OptimizationPlan:
    """Create a small deterministic Pareto search bounded by geometry quality."""
    baseline = report.settings
    quality_floor, reliability_floor = _thresholds(baseline.priority, baseline.model_purpose)
    surface = report.surface_intelligence
    curved = surface.ratio("curved_visible")
    precision = surface.ratio("precision_candidate")
    top = surface.ratio("top_visible")
    maximum_dimension = max(report.metrics.dimensions_mm)
    priorities = {item for item in baseline.priority.split("+") if item}
    visible_complexity = min(1.0, top + curved + 0.7 * precision)
    protects_quality = "quality" in priorities or visible_complexity >= 0.50
    if protects_quality:
        layer_cap = baseline.layer_height_mm
    elif curved >= 0.40 or (maximum_dimension <= 50.0 and curved >= 0.25):
        layer_cap = min(0.16, baseline.layer_height_mm + 0.04)
    elif curved + precision >= 0.35:
        layer_cap = min(0.20, baseline.layer_height_mm + 0.04)
    else:
        layer_cap = min(0.24, baseline.layer_height_mm + 0.08)
    if "quality" in priorities:
        layer_cap = min(layer_cap, baseline.layer_height_mm + 0.04)

    variants: list[tuple[str, str, PrintSettings]] = [
        ("quality-baseline", "Эталон качества", baseline)
    ]
    if allow_setting_changes:
        efficient = replace(
            baseline,
            layer_height_mm=round(max(baseline.layer_height_mm, layer_cap), 2),
            inner_wall_speed_mm_s=min(340, round(baseline.inner_wall_speed_mm_s * 1.12)),
            sparse_infill_speed_mm_s=min(330, round(baseline.sparse_infill_speed_mm_s * 1.15)),
            internal_solid_infill_speed_mm_s=min(290, round(baseline.internal_solid_infill_speed_mm_s * 1.10)),
            default_acceleration_mm_s2=min(11_000, round(baseline.default_acceleration_mm_s2 * 1.10)),
            detect_thin_wall=True,
            detect_floating_vertical_shell=True,
            reduce_crossing_wall=True,
            bridge_no_support=report.risks.overhang != "HIGH",
        )
        variants.append(("surface-efficient", "Быстро с сохранением поверхностей", efficient))
        motion_efficient = replace(
            baseline,
            travel_speed_mm_s=min(600, round(baseline.travel_speed_mm_s * 1.12)),
            travel_acceleration_mm_s2=min(
                15_000, round(baseline.travel_acceleration_mm_s2 * 1.20)
            ),
            inner_wall_speed_mm_s=min(
                340, round(baseline.inner_wall_speed_mm_s * 1.10)
            ),
            reduce_crossing_wall=True,
            avoid_crossing_wall_includes_support=True,
            reduce_infill_retraction_mode="Auto",
        )
        variants.append(
            ("motion-efficient", "Меньше холостых перемещений", motion_efficient)
        )
        flow_balanced = replace(
            efficient,
            max_volumetric_speed_mm3_s=min(
                baseline.max_volumetric_speed_mm3_s, 21.0
            ),
            filament_flow_ratio=baseline.filament_flow_ratio,
            outer_wall_speed_mm_s=baseline.outer_wall_speed_mm_s,
            top_surface_speed_mm_s=baseline.top_surface_speed_mm_s,
        )
        variants.append(("flow-balanced", "Поток без потери поверхности", flow_balanced))
        if baseline.model_purpose == "decorative" and not protects_quality:
            material_efficient = replace(
                efficient,
                sparse_infill_percent=max(8, baseline.sparse_infill_percent - 4),
                sparse_infill_pattern="crosshatch",
                infill_combination=maximum_dimension >= 45.0 and top + curved < 0.55,
            )
            variants.append(
                ("material-efficient", "Меньше материала без просветов", material_efficient)
            )
        if not protects_quality:
            minimum_time = replace(
                efficient,
                inner_wall_speed_mm_s=min(380, round(baseline.inner_wall_speed_mm_s * 1.25)),
                sparse_infill_speed_mm_s=min(360, round(baseline.sparse_infill_speed_mm_s * 1.30)),
                internal_solid_infill_speed_mm_s=min(320, round(baseline.internal_solid_infill_speed_mm_s * 1.22)),
                default_acceleration_mm_s2=min(12_000, round(baseline.default_acceleration_mm_s2 * 1.20)),
                sparse_infill_percent=(
                    max(8, baseline.sparse_infill_percent - 3)
                    if baseline.model_purpose == "decorative"
                    else baseline.sparse_infill_percent
                ),
                infill_combination=(
                    baseline.model_purpose == "decorative"
                    and maximum_dimension >= 45.0
                    and top + curved < 0.55
                ),
            )
            variants.append(("minimum-safe-time", "Минимальное безопасное время", minimum_time))

    candidates: list[OptimizationCandidate] = []
    fingerprints: set[tuple[tuple[str, Any], ...]] = set()
    for identifier, title, settings in variants:
        if locked_fields:
            settings = replace(
                settings,
                **{
                    name: getattr(baseline, name)
                    for name in locked_fields
                    if hasattr(baseline, name)
                },
            )
        settings, _ = settings_for_material(
            settings,
            material_profile or report.material,
            nozzle_diameter_mm=nozzle_diameter_mm,
        )
        fingerprint = tuple(sorted(asdict(settings).items()))
        if fingerprint in fingerprints:
            continue
        fingerprints.add(fingerprint)
        quality, reliability, constraints = _candidate_scores(report, settings, baseline)
        candidates.append(
            OptimizationCandidate(
                identifier=identifier,
                title=title,
                settings=settings,
                quality_score=quality,
                reliability_score=reliability,
                eligible=quality >= quality_floor and reliability >= reliability_floor,
                constraints=constraints,
                changes=_changes(baseline, settings),
            )
        )
    facts = (
        f"видимые сложные поверхности: {(top + curved + precision) * 100:.1f}%",
        f"риск нависаний: {report.risks.overhang}",
        f"риск адгезии: {report.risks.bed_adhesion}",
        f"назначение: {baseline.model_purpose}",
        f"функциональный сценарий: {baseline.functional_intent}",
        f"локальных диапазонов: {len(report.local_modifier_plan.ranges) if report.local_modifier_plan else 0}",
        f"риск извлечения supports: {report.support_exit_plan.overall_risk if report.support_exit_plan else 'UNKNOWN'}",
        f"предельный слой без выхода из бюджета качества: {layer_cap:.2f} мм",
    )
    return OptimizationPlan(5, quality_floor, reliability_floor, tuple(candidates), facts)


def select_independent_optimization_candidate(
    plan: OptimizationPlan,
) -> tuple[OptimizationCandidate, dict[str, Any]]:
    """Choose an eligible settings plan without repeating mesh sectioning.

    VANIOR Slice owns the final flow limiter and G-code audit.  This bounded
    cost model is used before that expensive pass: every candidate is checked
    against the same quality/reliability gates as the external protocol, then
    estimated from layers, walls, infill, solid work and travel.  The selected
    candidate still has to pass the real G-code audit before publication.
    """
    if not plan.candidates:
        raise ValueError("optimization plan has no candidates")
    baseline = plan.candidates[0]

    def cost(settings: PrintSettings) -> float:
        layer_factor = 1.0 / max(0.04, float(settings.layer_height_mm))
        wall_work = (
            1.0 / max(1.0, float(settings.outer_wall_speed_mm_s))
            + max(0, settings.wall_loops - 1)
            / max(1.0, float(settings.inner_wall_speed_mm_s))
        )
        shell_work = (
            settings.top_layers + settings.bottom_layers
        ) / max(1.0, float(settings.internal_solid_infill_speed_mm_s))
        infill_work = (
            max(1, settings.sparse_infill_percent)
            / 100.0
            / max(1.0, float(settings.sparse_infill_speed_mm_s))
        )
        travel_work = 0.15 / max(1.0, float(settings.travel_speed_mm_s))
        return layer_factor * (wall_work + 0.35 * shell_work + 2.0 * infill_work + travel_work)

    baseline_cost = max(cost(baseline.settings), 1e-12)
    eligible = [candidate for candidate in plan.candidates if candidate.eligible]
    pool = eligible or [baseline]
    selected = min(pool, key=lambda candidate: cost(candidate.settings))
    evaluations = []
    for candidate in plan.candidates:
        ratio = cost(candidate.settings) / baseline_cost
        evaluations.append(
            {
                "identifier": candidate.identifier,
                "title": candidate.title,
                "quality_score": candidate.quality_score,
                "reliability_score": candidate.reliability_score,
                "eligible": candidate.eligible,
                "accepted": candidate.eligible,
                "projected_time_ratio": round(ratio, 4),
                "projected_time_saved_percent": round((1.0 - ratio) * 100.0, 2),
                "selected": candidate.identifier == selected.identifier,
                "changes": list(candidate.changes),
            }
        )
    payload: dict[str, Any] = {
        "protocol_version": plan.protocol_version,
        "method": "geometry-cost-model+real-final-audit",
        "quality_floor": plan.quality_floor,
        "reliability_floor": plan.reliability_floor,
        "selected_candidate": selected.identifier,
        "selected_title": selected.title,
        "selected_quality_score": selected.quality_score,
        "selected_reliability_score": selected.reliability_score,
        "projected_time_saved_percent": round(
            (1.0 - cost(selected.settings) / baseline_cost) * 100.0, 2
        ),
        "evaluations": evaluations,
        "geometry_facts": list(plan.geometry_facts),
        "reason": (
            "Выбран самый быстрый допустимый план по геометрической модели стоимости; "
            "итоговая траектория дополнительно проходит реальный аудит G-code."
        ),
    }
    return selected, payload


def evaluate_optimization_plan(
    plan: OptimizationPlan,
    metrics: Mapping[str, CandidateSliceMetrics],
) -> QualityOptimizationResult:
    """Select the fastest real slice that passes every quality constraint."""
    evaluations: list[CandidateEvaluation] = []
    accepted: list[tuple[OptimizationCandidate, CandidateSliceMetrics]] = []
    for candidate in plan.candidates:
        result = metrics.get(candidate.identifier, CandidateSliceMetrics(False, None, None, error="не нарезан"))
        reasons: list[str] = []
        if not candidate.eligible:
            if candidate.quality_score < plan.quality_floor:
                reasons.append("ниже порога качества")
            if candidate.reliability_score < plan.reliability_floor:
                reasons.append("ниже порога надёжности")
        if not result.success:
            reasons.append(result.error or "ошибка Bambu Studio")
        if result.warnings:
            reasons.append("предупреждения слайсера")
        if result.blocking_warnings:
            reasons.append("аудит G-code обнаружил небезопасные команды")
        if result.audit_score < 85.0:
            reasons.append("аудит G-code ниже безопасного порога 85")
        is_accepted = (
            candidate.eligible
            and result.success
            and not result.warnings
            and not result.blocking_warnings
            and result.audit_score >= 85.0
        )
        if is_accepted:
            accepted.append((candidate, result))
        evaluations.append(
            CandidateEvaluation(
                candidate.identifier,
                candidate.title,
                candidate.quality_score,
                candidate.reliability_score,
                candidate.eligible,
                result.success,
                is_accepted,
                result.print_time_s,
                result.material_g,
                tuple(reasons),
                candidate.changes,
            )
        )
    baseline_candidate = plan.candidates[0]
    baseline_metrics = metrics.get(baseline_candidate.identifier)
    if (
        baseline_metrics is None
        or not baseline_metrics.success
        or baseline_metrics.print_time_s is None
        or baseline_metrics.warnings
        or baseline_metrics.blocking_warnings
        or baseline_metrics.audit_score < 85.0
    ):
        raise ValueError("quality baseline did not produce a valid slice")
    if not accepted:
        raise ValueError("no optimization candidate passed the quality and safety gates")
    selected_candidate, selected_metrics = min(
        accepted,
        key=lambda item: (
            float(item[1].print_time_s or float("inf")),
            -item[0].quality_score,
            float(item[1].material_g or float("inf")),
        ),
    )
    baseline_time = float(baseline_metrics.print_time_s)
    selected_time = float(selected_metrics.print_time_s or baseline_time)
    # Avoid changing a proven baseline for a negligible slicer-estimate gain.
    if selected_candidate.identifier != baseline_candidate.identifier and selected_time > baseline_time * 0.99:
        selected_candidate, selected_metrics = baseline_candidate, baseline_metrics
        selected_time = baseline_time
    baseline_mass = float(baseline_metrics.material_g or 0.0)
    selected_mass = float(selected_metrics.material_g or baseline_mass)
    saved = max(0.0, baseline_time - selected_time)
    percent = 100.0 * saved / max(baseline_time, 1e-9)
    reason = (
        f"Выбран «{selected_candidate.title}»: фактическое время Bambu Studio "
        f"{selected_time / 60:.1f} мин, экономия {percent:.1f}%; "
        f"качество {selected_candidate.quality_score:.1f}/{plan.quality_floor:.1f}, "
        f"надёжность {selected_candidate.reliability_score:.1f}/{plan.reliability_floor:.1f}."
    )
    return QualityOptimizationResult(
        plan.protocol_version,
        plan.quality_floor,
        plan.reliability_floor,
        selected_candidate.identifier,
        selected_candidate.title,
        selected_candidate.quality_score,
        selected_candidate.reliability_score,
        baseline_time,
        selected_time,
        saved,
        round(percent, 2),
        baseline_mass,
        selected_mass,
        baseline_mass - selected_mass,
        tuple(evaluations),
        reason,
    )
