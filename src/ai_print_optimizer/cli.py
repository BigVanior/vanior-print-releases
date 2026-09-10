"""Stable command-line interface for VANIOR PRINT."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from .analyzer import AnalysisError, analyze_stl
from .batch import BatchError, BatchRunResult, run_batch
from .diagnostics import run_diagnostics
from .orientation import (
    OrientationExportResult,
    optimize_orientation,
    orient_stl,
)
from .pipeline import PipelineError, PipelineResult, run_pipeline, verify_manifest
from .print_priority import normalize_print_priority
from .profile import ProfileError, validate_bambu_profile
from .project3mf import Project3MFError, extract_printable_stl
from .repair import RepairResult, repair_stl
from .report import AnalysisReport, OrientationAnalysis
from .simplification import SimplificationError, SimplificationResult, simplify_stl
from .slicer import (
    SlicerError,
    SliceRunResult,
    SupportComparison,
    compare_stl_supports,
    compare_supports,
    slice_3mf,
    slice_stl_with_template,
)
from .vanior_slice import VaniorSliceError, slice_stl_to_gcode
from .version import __version__


def _print_json(payload: object) -> None:
    """Print readable JSON even on legacy Windows console encodings."""
    rendered = json.dumps(payload, indent=2, ensure_ascii=False)
    try:
        print(rendered)
    except UnicodeEncodeError:
        print(json.dumps(payload, indent=2, ensure_ascii=True))


def _format_report(
    report: AnalysisReport,
    repair: RepairResult | None = None,
    orientation: OrientationAnalysis | None = None,
    orientation_export: OrientationExportResult | None = None,
    simplification: SimplificationResult | None = None,
) -> str:
    metrics = report.metrics
    health = report.health
    settings = report.settings
    risks = report.risks
    width, depth, height = metrics.dimensions_mm
    volume = (
        f"{metrics.volume_mm3 / 1000.0:.3f} cm^3"
        if metrics.volume_mm3 is not None
        else "недоступен (сетка не замкнута)"
    )
    lines = [
        f"VANIOR PRINT v{__version__}",
        "",
        f"Модель: {report.model_path}",
        f"Принтер: {report.printer}",
        f"Материал: {report.material}",
        f"Помещается в область печати: {'ДА' if report.fits_build_volume else 'НЕТ'}",
        "",
        "Геометрия",
        f"  Размеры: X {width:.3f} x Y {depth:.3f} x Z {height:.3f} мм",
        f"  Объём: {volume}",
        f"  Площадь поверхности: {metrics.surface_area_mm2 / 100.0:.3f} см²",
        f"  Тела: {metrics.body_count} значимых / {health.total_body_count} всего",
        f"  Треугольники: {metrics.triangle_count} рабочих / {health.raw_triangle_count} исходных",
        f"  Замкнута: {'ДА' if metrics.is_watertight else 'НЕТ'}",
        f"  Площадь основания: {metrics.base_area_mm2:.3f} мм² ({metrics.base_area_ratio:.1%} проекции)",
        f"  Нависания: {metrics.overhang_area_mm2:.3f} мм² ({metrics.overhang_area_ratio:.1%} поверхности)",
        "",
        "Состояние сетки",
        f"  Статус: {health.status}",
        f"  Топология: {health.topology_status}",
        f"  Автоматический ремонт: {health.repairability}",
        f"  Фрагменты мусора: {health.debris_body_count}",
        f"  Открытые граничные рёбра: {health.boundary_edge_count}",
        f"  Неманифолдные рёбра: {health.non_manifold_edge_count}",
        f"  Плотность сетки: {health.mesh_density} ({health.triangles_per_mm2:.2f} треугольников/мм²)",
        "",
        "Тела модели (сначала крупнейшие)",
    ]
    for body in health.bodies[:10]:
        body_type = "МУСОР" if body.is_debris else "МОДЕЛЬ"
        if body.non_manifold_edge_count and body.boundary_edge_count:
            body_state = "OPEN+NON_MANIFOLD"
        elif body.non_manifold_edge_count:
            body_state = "NON_MANIFOLD"
        elif body.boundary_edge_count:
            body_state = "OPEN"
        else:
            body_state = "WATERTIGHT"
        body_dimensions = " x ".join(f"{value:.3f}" for value in body.dimensions_mm)
        lines.append(
            f"  #{body.index} {body_type}: {body.triangle_count} треугольников, "
            f"{body_dimensions} мм, {body_state}, "
            f"открытых рёбер {body.boundary_edge_count}"
        )
    if len(health.bodies) > 10 or health.body_details_truncated:
        hidden = max(0, len(health.bodies) - 10) + health.body_details_truncated
        lines.append(f"  ... ещё {hidden} тел не показаны в текстовом отчёте")
    lines.extend(["", "Surface Intelligence"])
    for role in report.surface_intelligence.roles:
        if role.area_ratio >= 0.001:
            lines.append(
                f"  {role.role}: {role.area_ratio:.1%} ({role.area_mm2:.1f} мм²)"
            )
    lines.extend(
        f"  Решение: {item}"
        for item in report.surface_intelligence.recommendations
    )
    if orientation is not None:
        lines.extend(
            [
                "",
                "Оптимизация ориентации",
                f"  Проверено вариантов: {orientation.candidates_evaluated}",
                f"  Текущая / лучшая оценка: {orientation.current_score:.2f} / {orientation.best_score:.2f}",
                f"  Улучшение: +{orientation.score_improvement:.2f}",
                f"  Уверенность: {orientation.confidence}",
                "  Лучшие ориентации:",
            ]
        )
        for candidate in orientation.top_candidates:
            rotation = " / ".join(
                f"{axis} {value:.1f} deg"
                for axis, value in zip("XYZ", candidate.rotation_deg)
            )
            lines.append(
                f"    #{candidate.rank} [{candidate.kind}] {rotation}; "
                f"оценка {candidate.score:.2f}, основание {candidate.base_area_mm2:.1f} мм², "
                f"нависания {candidate.overhang_area_ratio:.1%}, "
                f"высота {candidate.dimensions_mm[2]:.1f} мм"
            )
    lines.extend([
        "",
        "Риски печати",
        f"  Адгезия к столу: {risks.bed_adhesion}",
        f"  Нависания: {risks.overhang}",
        f"  Высокая модель: {risks.tall_object}",
        f"  Необходимость поддержек: {risks.support_requirement}",
        "",
        "Рекомендуемые стартовые настройки",
        f"  Высота слоя: {settings.layer_height_mm:.2f} мм",
        f"  Контуры стенок: {settings.wall_loops}",
        f"  Верхние / нижние слои: {settings.top_layers} / {settings.bottom_layers}",
        f"  Поддержки: {'ВКЛ' if settings.supports else 'ВЫКЛ'}",
        f"  Кайма: {'ВКЛ' if settings.brim else 'ВЫКЛ'}",
        f"  Сопло / стол: {settings.nozzle_temperature_c} / {settings.bed_temperature_c} °C",
        f"  Вентилятор: {settings.fan_percent}%",
    ])
    if report.warnings:
        lines.extend(["", "Предупреждения"])
        lines.extend(f"  - {warning}" for warning in report.warnings)
    if repair is not None:
        repair_lines = [
            "СОЗДАНА ИСПРАВЛЕННАЯ КОПИЯ",
            f"  Источник: {repair.source_path}",
            f"  Результат: {repair.output_path}",
            f"  Удалённый мусор: {repair.removed_debris_bodies} тел / {repair.removed_triangles} треугольников",
            f"  Закрыто отверстий: {repair.filled_holes}",
            f"  Добавлено треугольников: {repair.added_triangles}",
            f"  Открытые рёбра: {repair.boundary_edges_before} -> {repair.boundary_edges_after}",
            f"  Неманифолдные рёбра: {repair.non_manifold_edges_before} -> {repair.non_manifold_edges_after}",
            f"  Итоговый статус: {repair.final_status}",
            "",
        ]
        lines = repair_lines + lines
    if orientation_export is not None:
        export_lines = [
            "СОЗДАНА ОРИЕНТИРОВАННАЯ КОПИЯ",
            f"  Источник: {orientation_export.source_path}",
            f"  Результат: {orientation_export.output_path}",
            f"  Поворот X/Y/Z: {orientation_export.rotation_deg}",
            f"  Оценка ориентации: {orientation_export.score:.2f}",
            "",
        ]
        lines = export_lines + lines
    if simplification is not None:
        simplification_lines = [
            "СОЗДАНА УПРОЩЁННАЯ КОПИЯ",
            f"  Источник: {simplification.source_path}",
            f"  Результат: {simplification.output_path}",
            f"  Треугольники: {simplification.triangles_before} -> {simplification.triangles_after}",
            f"  Сокращение: {simplification.reduction_percent:.1f}%",
            (f"  Ошибка размеров / объёма: {simplification.max_dimension_error_mm:.4f} мм / "
            f"{simplification.volume_error_percent:.4f}%"),
            "",
        ]
        lines = simplification_lines + lines
    return "\n".join(lines)


def _duration(seconds: float) -> str:
    rounded = max(0, round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d} ч {minutes:02d} мин {secs:02d} с"


def _format_slice_result(result: SliceRunResult) -> str:
    lines = [
        f"AI PRINT OPTIMIZER v{__version__} — РЕАЛЬНЫЙ СЛАЙСИНГ",
        "",
        f"Проект: {result.source_path}",
        f"Слайсер: {result.slicer_path}",
        f"Каталог результата: {result.output_dir}",
        f"Статус: {'УСПЕХ' if result.success else 'ОШИБКА'} ({result.return_code})",
    ]
    if result.error_string:
        lines.append(f"Сообщение: {result.error_string}")
    lines.extend(
        [
            "",
            f"Время печати: {_duration(result.total_print_time_s)}",
            f"Материал: {result.total_used_g:.2f} г",
            f"Поддержки по G-code: {result.support_estimated_mass_g:.2f} г"
            if result.support_estimated_mass_g is not None
            else "Поддержки по G-code: недоступно",
            f"Длина: {result.total_estimated_length_m:.2f} м"
            if result.total_estimated_length_m is not None
            else "Длина: недоступна",
            f"Оценочная стоимость: {result.total_estimated_cost:.2f}"
            if result.total_estimated_cost is not None
            else "Оценочная стоимость: недоступна",
            (f"Слой / стенки / заполнение: {result.layer_height_mm:.2f} мм / "
            f"{result.wall_loops} / {result.sparse_infill_percent:.1f}%"),
            "",
            "Ограничения метрик:",
        ]
    )
    lines.extend(f"  - {item}" for item in result.metric_limitations)
    if result.gcode_files:
        lines.extend(["", "Созданный G-code:"])
        lines.extend(f"  - {path}" for path in result.gcode_files)
    if result.ready_project_path:
        lines.extend(["", f"Готовый проект 3MF: {result.ready_project_path}"])
    return "\n".join(lines)


def _format_comparison(comparison: SupportComparison) -> str:
    none = comparison.none
    normal = comparison.normal
    tree = comparison.tree
    lines = [
        f"AI PRINT OPTIMIZER v{__version__} — СТРАТЕГИИ ПЕЧАТИ",
        "",
        f"Исходный проект: {comparison.source_path}",
        f"Результаты: {comparison.output_dir}",
        "",
        "Без поддержек:",
        f"  Статус: {'УСПЕХ' if none.success else 'ОШИБКА'}",
        f"  Время: {_duration(none.total_print_time_s)}",
        f"  Материал: {none.total_used_g:.2f} г",
        "",
        "Обычные автоматические поддержки:",
        f"  Статус: {'УСПЕХ' if normal.success else 'ОШИБКА'}",
        f"  Время: {_duration(normal.total_print_time_s)}",
        f"  Материал: {normal.total_used_g:.2f} г",
        f"  Поддержки по G-code: {normal.support_estimated_mass_g:.2f} г"
        if normal.support_estimated_mass_g is not None
        else "  Поддержки по G-code: недоступно",
        "",
        "Древовидные автоматические поддержки:",
        f"  Статус: {'УСПЕХ' if tree.success else 'ОШИБКА'}",
        f"  Время: {_duration(tree.total_print_time_s)}",
        f"  Материал: {tree.total_used_g:.2f} г",
        f"  Поддержки по G-code: {tree.support_estimated_mass_g:.2f} г"
        if tree.support_estimated_mass_g is not None
        else "  Поддержки по G-code: недоступно",
        "",
        f"Выбранная стратегия: {comparison.recommended or 'нет'}",
        f"Причина: {comparison.recommendation_reason}",
        f"Готовый 3MF: {comparison.ready_project_path or 'не создан'}",
        f"Готовый G-code: {comparison.ready_gcode_path or 'не создан'}",
        "",
        "Масса поддержек оценена по фактически уложенной экструзии в G-code.",
    ]
    return "\n".join(lines)


def _format_pipeline(result: PipelineResult) -> str:
    lines = [
        f"VANIOR PRINT v{__version__} — АВТОМАТИЧЕСКИЙ PIPELINE",
        "",
        f"Источник: {result.source_path}",
        f"Результаты: {result.output_dir}",
        f"Режим: {result.mode}",
        f"Приоритет печати: {result.final_analysis.settings.priority}",
        (f"Назначение модели: {result.final_analysis.settings.model_purpose} "
        f"(автоматически: {result.final_analysis.purpose.classification}, "
        f"уверенность {result.final_analysis.purpose.confidence})"),
        f"Итоговая модель: {result.orientation_export.output_path}",
        f"Манифест: {result.manifest_path}",
    ]
    if result.repair is not None:
        lines.append(f"Ремонт: {result.repair.final_status}")
    lines.append(
        f"Профиль: {result.profile_validation.printer_settings_id} / "
        f"{result.profile_validation.filament_types[0]}"
    )
    if result.simplification is not None:
        lines.append(
            f"Упрощение: {result.simplification.triangles_before} -> "
            f"{result.simplification.triangles_after} треугольников"
        )
    elif result.simplification_note:
        lines.append(f"Упрощение пропущено: {result.simplification_note}")
    lines.append(
        f"Surface Intelligence: решений {len(result.final_analysis.surface_intelligence.recommendations)}"
    )
    if result.print_dna_application is not None:
        lines.append(
            f"PrintDNA: {result.print_dna_application.sample_count} отпечатков, "
            f"поправок {len(result.print_dna_application.adjustments)}, "
            f"уверенность {result.print_dna_application.confidence}"
        )
    if result.support_comparison is not None:
        lines.extend(
            [
                f"Выбранные поддержки: {result.support_comparison.recommended}",
                f"Причина: {result.support_comparison.recommendation_reason}",
                f"Готовый 3MF: {result.support_comparison.ready_project_path}",
                f"Готовый G-code: {result.support_comparison.ready_gcode_path}",
            ]
        )
    elif result.slice_result is not None:
        lines.extend(
            [
                f"Время печати: {_duration(result.slice_result.total_print_time_s)}",
                f"Материал: {result.slice_result.total_used_g:.2f} г",
            ]
        )
    return "\n".join(lines)


def _format_batch(result: BatchRunResult) -> str:
    return "\n".join(
        [
            f"AI PRINT OPTIMIZER v{__version__} — ПАКЕТНАЯ ОБРАБОТКА",
            "",
            f"Каталог моделей: {result.input_dir}",
            f"Результаты: {result.output_dir}",
            f"Готово / пропущено / ошибок: {result.completed} / {result.skipped} / {result.failed}",
            f"JSON: {result.summary_path}",
            f"HTML: {result.html_report_path}",
        ]
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-print-optimizer",
        description="Analyze STL/3MF geometry and create verified ready Bambu print files.",
    )
    parser.add_argument("model", nargs="?", help="path to an STL, 3MF, manifest or directory")
    parser.add_argument(
        "--material",
        choices=("PLA", "PETG", "pla", "petg"),
        default="PLA",
        help="filament profile (default: PLA)",
    )
    parser.add_argument(
        "--overhang-angle",
        type=float,
        default=45.0,
        metavar="DEGREES",
        help="support threshold from downward vertical, 1..89 (default: 45)",
    )
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="run read-only release diagnostics; MODEL is not required",
    )
    parser.add_argument(
        "--repair-output",
        metavar="OUTPUT_STL",
        help="write a new cleaned STL and analyze that copy; refuses to overwrite files",
    )
    parser.add_argument(
        "--max-hole-diameter",
        type=float,
        default=5.0,
        metavar="MM",
        help="maximum diameter of a planar hole eligible for repair (default: 5 mm)",
    )
    parser.add_argument(
        "--orient",
        action="store_true",
        help="evaluate print orientations and show the top three",
    )
    parser.add_argument(
        "--orient-output",
        metavar="OUTPUT_STL",
        help="write the best orientation to a new STL; refuses to overwrite files",
    )
    parser.add_argument(
        "--simplify-output",
        metavar="OUTPUT_STL",
        help="write a safely decimated STL copy; requires --target-faces",
    )
    parser.add_argument(
        "--target-faces",
        type=int,
        metavar="COUNT",
        help="target triangle count for --simplify-output",
    )
    slicer_mode = parser.add_mutually_exclusive_group()
    slicer_mode.add_argument(
        "--vanior-slice-output",
        metavar="OUTPUT_GCODE",
        help=(
            "slice an STL or single-material 3MF with the independent VANIOR Slice "
            "engineering-preview engine"
        ),
    )
    slicer_mode.add_argument(
        "--slice-output",
        metavar="NEW_DIRECTORY",
        help="slice one plate from a Bambu 3MF into a new isolated directory",
    )
    slicer_mode.add_argument(
        "--compare-supports",
        metavar="NEW_DIRECTORY",
        help="compare none, normal(auto) and tree(auto) print strategies",
    )
    slicer_mode.add_argument(
        "--pipeline-output",
        metavar="NEW_DIRECTORY",
        help="process an STL/3MF into verified ready-to-print 3MF and G-code",
    )
    slicer_mode.add_argument(
        "--verify-manifest",
        action="store_true",
        help="treat MODEL as manifest.json and verify all recorded SHA-256 values",
    )
    slicer_mode.add_argument(
        "--validate-profile",
        action="store_true",
        help="treat MODEL as a Bambu 3MF and validate P1S/nozzle/material settings",
    )
    slicer_mode.add_argument(
        "--batch-output",
        metavar="DIRECTORY",
        help="treat MODEL as a directory and run the full pipeline for every STL/3MF",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        help="resume a batch by verifying and skipping completed model manifests",
    )
    parser.add_argument(
        "--no-recursive",
        action="store_true",
        help="do not search subdirectories in batch mode",
    )
    parser.add_argument(
        "--slicer-executable",
        metavar="PATH",
        help="explicit Bambu Studio executable (otherwise auto-detected)",
    )
    parser.add_argument(
        "--profile-template",
        metavar="BAMBU_PROJECT.3MF",
        help=(
            "optional legacy 3MF profile override; the full pipeline otherwise "
            "builds a clean P1S profile from installed Bambu Studio presets"
        ),
    )
    parser.add_argument(
        "--print-priority",
        type=normalize_print_priority,
        default="balanced",
        help=(
            "one or more priorities joined with '+': quality, strength, "
            "balanced (default), fast, or for example quality+strength"
        ),
    )
    parser.add_argument(
        "--model-purpose",
        choices=("auto", "decorative", "functional"),
        default="auto",
        help="model purpose: automatic classification (default), decorative, or functional",
    )
    parser.add_argument(
        "--functional-intent",
        choices=(
            "auto", "decorative", "enclosure", "fixture", "gear", "snap_fit",
            "vessel", "flexible", "structural", "general_functional",
        ),
        default="auto",
        help="detailed functional scenario for the full pipeline (default: auto)",
    )
    parser.add_argument(
        "--slice-cache",
        metavar="DIRECTORY",
        help="optional verified content-addressed cache for repeated pipeline slices",
    )
    parser.add_argument(
        "--no-quality-search",
        action="store_true",
        help="disable real multi-candidate time search and keep the quality baseline",
    )
    parser.add_argument(
        "--plate",
        type=int,
        default=1,
        help="one-based 3MF plate number for slicing (default: 1)",
    )
    parser.add_argument(
        "--slice-timeout",
        type=float,
        default=300.0,
        metavar="SECONDS",
        help="maximum wait for each slicer run (default: 300)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    repair_result: RepairResult | None = None
    orientation_analysis: OrientationAnalysis | None = None
    orientation_export: OrientationExportResult | None = None
    simplification_result: SimplificationResult | None = None
    model_to_analyze = args.model
    if args.self_check:
        report = run_diagnostics(args.slicer_executable)
        if args.json:
            _print_json(report.to_dict())
        else:
            print(f"AI PRINT OPTIMIZER v{__version__} — САМОДИАГНОСТИКА")
            for check in report.checks:
                print(f"  {'OK' if check.ok else 'FAIL'} {check.name}: {check.detail}")
        return 0 if report.ok else 7
    if args.model is None:
        print("error: MODEL is required unless --self-check is used", file=sys.stderr)
        return 1
    if args.batch_output:
        if not args.profile_template:
            print("error: batch mode requires --profile-template", file=sys.stderr)
            return 1
        try:
            batch = run_batch(
                args.model,
                args.profile_template,
                args.batch_output,
                material=args.material,
                recursive=not args.no_recursive,
                resume=args.resume,
                executable=args.slicer_executable,
                timeout_s=args.slice_timeout,
            )
        except (BatchError, ProfileError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print_json(batch.to_dict())
        else:
            print(_format_batch(batch))
        return 0 if batch.failed == 0 else 6
    if args.validate_profile:
        try:
            validation = validate_bambu_profile(
                args.model,
                expected_material=args.material,
            )
        except ProfileError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print_json(validation.to_dict())
        else:
            print(
                f"Профиль: {validation.profile_path}\n"
                f"Статус: {'СОВМЕСТИМ' if validation.valid else 'НЕСОВМЕСТИМ'}\n"
                f"Принтер: {validation.printer_model}\n"
                f"Сопло: {validation.nozzle_diameter_mm} мм\n"
                f"Материалы: {', '.join(validation.filament_types)}"
            )
            for error in validation.errors:
                print(f"  - {error}")
        return 0 if validation.valid else 5
    if args.verify_manifest:
        try:
            verification = verify_manifest(args.model)
        except PipelineError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        if args.json:
            _print_json(verification.to_dict())
        else:
            print(
                f"Манифест: {verification.manifest_path}\n"
                f"Статус: {'ЦЕЛ' if verification.valid else 'ПОВРЕЖДЁН'}\n"
                f"Проверено файлов: {verification.checked_files}"
            )
            for error in verification.errors:
                print(f"  - {error}")
        return 0 if verification.valid else 4
    if (
        args.vanior_slice_output
        or args.slice_output
        or args.compare_supports
        or args.pipeline_output
    ):
        if (
            args.repair_output
            or args.orient
            or args.orient_output
            or args.simplify_output
        ):
            print(
                "error: slicing and pipeline modes cannot be combined with separate repair/orientation flags",
                file=sys.stderr,
            )
            return 1
        try:
            if args.vanior_slice_output:
                source = Path(args.model).expanduser().resolve()
                temporary_geometry: tempfile.TemporaryDirectory[str] | None = None
                try:
                    if source.suffix.lower() == ".3mf":
                        temporary_geometry = tempfile.TemporaryDirectory(
                            prefix="vanior-slice-geometry-"
                        )
                        extracted_path = Path(temporary_geometry.name) / "printable-model.stl"
                        extract_printable_stl(source, extracted_path, plate=args.plate)
                        source = extracted_path
                    report = analyze_stl(
                        source,
                        material=args.material,
                        overhang_angle_deg=args.overhang_angle,
                        nozzle_diameter_mm=0.4,
                    )
                    sliced = slice_stl_to_gcode(
                        source,
                        args.vanior_slice_output,
                        report.settings,
                        material=args.material,
                        nozzle_diameter_mm=0.4,
                        overhang_angle_deg=args.overhang_angle,
                    )
                finally:
                    if temporary_geometry is not None:
                        temporary_geometry.cleanup()
                if args.json:
                    _print_json(sliced.to_dict())
                else:
                    print(
                        "VANIOR Slice — независимая инженерная проверка\n"
                        f"G-code: {sliced.gcode_path}\n"
                        f"Слоёв: {sliced.layer_count}\n"
                        f"Время: {_duration(sliced.estimated_print_time_s)}\n"
                        f"Материал: {sliced.estimated_mass_g:.2f} г\n"
                        f"Аудит: {sliced.audit.status}, "
                        f"безопасность {sliced.audit.safety_score:.0f}/100"
                    )
                return 0
            if args.pipeline_output:
                pipeline = run_pipeline(
                    args.model,
                    args.profile_template,
                    args.pipeline_output,
                    material=args.material,
                    print_priority=args.print_priority,
                    model_purpose=args.model_purpose,
                    functional_intent=args.functional_intent,
                    quality_search=not args.no_quality_search,
                    overhang_angle_deg=args.overhang_angle,
                    max_hole_diameter_mm=args.max_hole_diameter,
                    executable=args.slicer_executable,
                    timeout_s=args.slice_timeout,
                    plate=args.plate,
                    slice_cache_dir=args.slice_cache,
                )
                if args.json:
                    _print_json(pipeline.to_dict())
                else:
                    print(_format_pipeline(pipeline))
                return 0
            if args.slice_output:
                if str(args.model).lower().endswith(".stl"):
                    if not args.profile_template:
                        raise SlicerError(
                            "STL slicing requires --profile-template with a verified Bambu 3MF"
                        )
                    sliced = slice_stl_with_template(
                        args.model,
                        args.profile_template,
                        args.slice_output,
                        executable=args.slicer_executable,
                        timeout_s=args.slice_timeout,
                    )
                else:
                    sliced = slice_3mf(
                        args.model,
                        args.slice_output,
                        executable=args.slicer_executable,
                        plate=args.plate,
                        timeout_s=args.slice_timeout,
                    )
                if args.json:
                    _print_json(sliced.to_dict())
                else:
                    print(_format_slice_result(sliced))
                return 0 if sliced.success else 3
            if str(args.model).lower().endswith(".stl"):
                if not args.profile_template:
                    raise SlicerError(
                        "STL support comparison requires --profile-template with a verified Bambu 3MF"
                    )
                comparison = compare_stl_supports(
                    args.model,
                    args.profile_template,
                    args.compare_supports,
                    executable=args.slicer_executable,
                    timeout_s=args.slice_timeout,
                )
            else:
                comparison = compare_supports(
                    args.model,
                    args.compare_supports,
                    executable=args.slicer_executable,
                    plate=args.plate,
                    timeout_s=args.slice_timeout,
                )
            if args.json:
                _print_json(comparison.to_dict())
            else:
                print(_format_comparison(comparison))
            return 0 if comparison.recommended is not None else 3
        except (AnalysisError, PipelineError, Project3MFError, SlicerError, VaniorSliceError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    temporary_geometry: tempfile.TemporaryDirectory[str] | None = None
    try:
        if str(model_to_analyze).lower().endswith(".3mf"):
            temporary_geometry = tempfile.TemporaryDirectory()
            extracted_path = Path(temporary_geometry.name) / "printable-model.stl"
            extract_printable_stl(
                model_to_analyze,
                extracted_path,
                plate=args.plate,
            )
            model_to_analyze = extracted_path
        if args.repair_output:
            repair_result = repair_stl(
                model_to_analyze,
                args.repair_output,
                max_hole_diameter_mm=args.max_hole_diameter,
            )
            model_to_analyze = repair_result.output_path
        if args.simplify_output:
            if args.target_faces is None:
                raise SimplificationError("--simplify-output requires --target-faces")
            simplification_result = simplify_stl(
                model_to_analyze,
                args.simplify_output,
                target_faces=args.target_faces,
            )
            model_to_analyze = simplification_result.output_path
        if args.orient_output:
            orientation_analysis, orientation_export = orient_stl(
                model_to_analyze,
                args.orient_output,
                overhang_angle_deg=args.overhang_angle,
            )
            model_to_analyze = orientation_export.output_path
        elif args.orient:
            orientation_analysis = optimize_orientation(
                model_to_analyze,
                overhang_angle_deg=args.overhang_angle,
            )
        report = analyze_stl(
            model_to_analyze,
            material=args.material,
            overhang_angle_deg=args.overhang_angle,
        )
    except (AnalysisError, Project3MFError, SimplificationError) as exc:
        if temporary_geometry is not None:
            temporary_geometry.cleanup()
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        payload = report.to_dict()
        if (
            repair_result is not None
            or simplification_result is not None
            or orientation_analysis is not None
        ):
            wrapped: dict[str, object] = {"analysis": payload}
            if repair_result is not None:
                wrapped["repair"] = repair_result.to_dict()
            if simplification_result is not None:
                wrapped["simplification"] = simplification_result.to_dict()
            if orientation_analysis is not None:
                wrapped["orientation"] = orientation_analysis.to_dict()
            if orientation_export is not None:
                wrapped["orientation_export"] = orientation_export.to_dict()
            payload = wrapped
        _print_json(payload)
    else:
        print(
            _format_report(
                report,
                repair_result,
                orientation_analysis,
                orientation_export,
                simplification_result,
            )
        )
    exit_code = 0 if report.fits_build_volume else 2
    if temporary_geometry is not None:
        temporary_geometry.cleanup()
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
