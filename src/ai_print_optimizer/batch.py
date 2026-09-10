"""Batch orchestration and standalone HTML reporting."""

from __future__ import annotations

import hashlib
import html
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .io_utils import atomic_write_new_json, atomic_write_new_text
from .pipeline import PipelineResult, run_pipeline, verify_manifest
from .profile import validate_bambu_profile
from .schema import validate_release_document
from .version import __version__


class BatchError(RuntimeError):
    """Raised when a batch cannot start safely."""


@dataclass(frozen=True)
class BatchItemResult:
    source_path: Path
    source_sha256: str
    output_dir: Path
    status: str
    mode: str | None
    recommended_support: str | None
    total_print_time_s: float | None
    total_used_g: float | None
    manifest_path: Path | None
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["source_path"] = str(self.source_path)
        result["output_dir"] = str(self.output_dir)
        result["manifest_path"] = str(self.manifest_path) if self.manifest_path else None
        return result


@dataclass(frozen=True)
class BatchRunResult:
    input_dir: Path
    output_dir: Path
    profile_template_path: Path
    created_utc: str
    completed: int
    skipped: int
    failed: int
    items: tuple[BatchItemResult, ...]
    summary_path: Path
    html_report_path: Path
    error_log_path: Path | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "application": {"name": "ai-print-optimizer", "version": __version__},
            "input_dir": str(self.input_dir),
            "output_dir": str(self.output_dir),
            "profile_template_path": str(self.profile_template_path),
            "created_utc": self.created_utc,
            "completed": self.completed,
            "skipped": self.skipped,
            "failed": self.failed,
            "items": [item.to_dict() for item in self.items],
            "summary_path": str(self.summary_path),
            "html_report_path": str(self.html_report_path),
            "error_log_path": str(self.error_log_path) if self.error_log_path else None,
        }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _model_directory(source: Path) -> str:
    slug = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._-]+", "-", source.stem).strip("-._")
    return f"{slug or 'model'}-{_sha256(source)[:10]}"


def _selected_run_metrics(result: PipelineResult) -> tuple[str | None, float | None, float | None]:
    if result.slice_result is not None:
        return None, result.slice_result.total_print_time_s, result.slice_result.total_used_g
    comparison = result.support_comparison
    if comparison is None or comparison.recommended is None:
        return None, None, None
    optimized = getattr(comparison, "quality_optimization", None)
    if optimized is not None:
        return (
            comparison.recommended,
            optimized.selected_time_s,
            optimized.selected_material_g,
        )
    if comparison.selected_run is not None:
        return (
            comparison.recommended,
            comparison.selected_run.total_print_time_s,
            comparison.selected_run.total_used_g,
        )
    selected = {
        "none": comparison.none,
        "normal": comparison.normal,
        "tree": comparison.tree,
    }[comparison.recommended]
    return comparison.recommended, selected.total_print_time_s, selected.total_used_g


def _item_from_manifest(source: Path, output_dir: Path, manifest_path: Path) -> BatchItemResult:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    stages = payload.get("stages", {})
    mode = str(payload.get("parameters", {}).get("mode", "")) or None
    recommended: str | None = None
    time_s: float | None = None
    mass_g: float | None = None
    slice_result = stages.get("slice_result")
    comparison = stages.get("support_comparison")
    if isinstance(slice_result, dict):
        time_s = float(slice_result.get("total_print_time_s", 0.0))
        mass_g = float(slice_result.get("total_used_g", 0.0))
    elif isinstance(comparison, dict):
        recommended = str(comparison.get("recommended", "")) or None
        optimized = comparison.get("quality_optimization")
        if isinstance(optimized, dict):
            time_s = float(optimized.get("selected_time_s", 0.0))
            mass_g = float(optimized.get("selected_material_g", 0.0))
        else:
            selected = comparison.get(recommended, {}) if recommended else {}
            if isinstance(selected, dict):
                time_s = float(selected.get("total_print_time_s", 0.0))
                mass_g = float(selected.get("total_used_g", 0.0))
    return BatchItemResult(
        source_path=source,
        source_sha256=_sha256(source),
        output_dir=output_dir,
        status="skipped",
        mode=mode,
        recommended_support=recommended,
        total_print_time_s=time_s,
        total_used_g=mass_g,
        manifest_path=manifest_path,
        error=None,
    )


def _next_report_paths(output_dir: Path, resume: bool) -> tuple[Path, Path, Path]:
    if not resume:
        return (
            output_dir / "batch-summary.json",
            output_dir / "batch-report.html",
            output_dir / "batch-errors.jsonl",
        )
    for index in range(1, 10_000):
        suffix = f"resume-{index:03d}"
        summary = output_dir / f"batch-summary.{suffix}.json"
        report = output_dir / f"batch-report.{suffix}.html"
        errors = output_dir / f"batch-errors.{suffix}.jsonl"
        if not summary.exists() and not report.exists() and not errors.exists():
            return summary, report, errors
    raise BatchError("cannot allocate a unique resume report name")


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    rounded = round(max(0.0, seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:d}:{minutes:02d}:{secs:02d}"


def _write_html(result: BatchRunResult) -> None:
    rows: list[str] = []
    for item in result.items:
        manifest_link = "—"
        if item.manifest_path is not None:
            relative = item.manifest_path.relative_to(result.output_dir).as_posix()
            manifest_link = f'<a href="{html.escape(relative)}">manifest</a>'
        mass_cell = (
            f"<td>{item.total_used_g:.2f}</td>"
            if item.total_used_g is not None
            else "<td>—</td>"
        )
        rows.append(
            "<tr>"
            f"<td>{html.escape(item.source_path.name)}</td>"
            f'<td><span class="status {html.escape(item.status)}">{html.escape(item.status)}</span></td>'
            f"<td>{html.escape(item.mode or '—')}</td>"
            f"<td>{html.escape(item.recommended_support or '—')}</td>"
            f"<td>{_duration(item.total_print_time_s)}</td>"
            f"{mass_cell}"
        )
        rows[-1] += (
            f"<td>{manifest_link}</td>"
            f"<td>{html.escape(item.error or '')}</td>"
            "</tr>"
        )
    document = f"""<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>VANIOR PRINT batch report</title>
<style>
body{{font:15px system-ui,sans-serif;margin:32px;color:#1f2937;background:#f8fafc}}
h1{{margin-bottom:6px}} .summary{{display:flex;gap:16px;margin:20px 0}}
.card{{background:white;padding:14px 18px;border:1px solid #dbe2ea;border-radius:10px}}
table{{width:100%;border-collapse:collapse;background:white}}th,td{{padding:10px;border:1px solid #dbe2ea;text-align:left}}
th{{background:#eef2f7}}.status{{font-weight:700}}.completed{{color:#067647}}.skipped{{color:#175cd3}}.failed{{color:#b42318}}
.table-wrap{{overflow-x:auto}}
</style></head><body>
<h1>VANIOR PRINT v{html.escape(__version__)}</h1>
<div>Пакетный отчёт · {html.escape(result.created_utc)}</div>
<div class="summary"><div class="card">Готово: {result.completed}</div><div class="card">Пропущено: {result.skipped}</div><div class="card">Ошибок: {result.failed}</div></div>
<div class="table-wrap"><table><thead><tr><th>Модель</th><th>Статус</th><th>Режим</th><th>Стратегия</th><th>Время</th><th>Масса, г</th><th>Данные</th><th>Ошибка</th></tr></thead>
<tbody>{''.join(rows)}</tbody></table></div>
</body></html>"""
    atomic_write_new_text(result.html_report_path, document)


def run_batch(
    input_dir: str | Path,
    profile_template: str | Path,
    output: str | Path,
    *,
    material: str = "PLA",
    recursive: bool = True,
    resume: bool = False,
    executable: str | Path | None = None,
    timeout_s: float = 300.0,
) -> BatchRunResult:
    """Run the full pipeline for every STL/3MF and produce JSON plus HTML."""
    source_dir = Path(input_dir).expanduser().resolve()
    profile_path = Path(profile_template).expanduser().resolve()
    output_dir = Path(output).expanduser().resolve()
    if not source_dir.is_dir():
        raise BatchError(f"batch input directory not found: {source_dir}")
    validation = validate_bambu_profile(profile_path, expected_material=material)
    if not validation.valid:
        raise BatchError("profile validation failed: " + "; ".join(validation.errors))
    if output_dir.exists() and not resume:
        raise BatchError(f"batch output already exists: {output_dir}")
    if output_dir.exists() and not output_dir.is_dir():
        raise BatchError(f"batch output is not a directory: {output_dir}")
    if not output_dir.exists() and not output_dir.parent.is_dir():
        raise BatchError(f"batch output parent not found: {output_dir.parent}")

    iterator = source_dir.rglob("*") if recursive else source_dir.glob("*")
    models = sorted(
        path.resolve()
        for path in iterator
        if path.is_file()
        and path.suffix.lower() in {".stl", ".3mf"}
        and path.resolve() != profile_path
        and not (output_dir == path or (output_dir.exists() and output_dir in path.parents))
    )
    if not models:
        raise BatchError("batch input contains no STL or 3MF files")
    output_dir.mkdir(exist_ok=resume)
    items: list[BatchItemResult] = []
    for source in models:
        source_hash = _sha256(source)
        model_output = output_dir / _model_directory(source)
        manifest_path = model_output / "manifest.json"
        if resume and manifest_path.is_file():
            verification = verify_manifest(manifest_path)
            if verification.valid:
                items.append(_item_from_manifest(source, model_output, manifest_path))
                continue
        if model_output.exists():
            items.append(
                BatchItemResult(
                    source_path=source,
                    source_sha256=source_hash,
                    output_dir=model_output,
                    status="failed",
                    mode=None,
                    recommended_support=None,
                    total_print_time_s=None,
                    total_used_g=None,
                    manifest_path=manifest_path if manifest_path.is_file() else None,
                    error="existing incomplete or invalid output was not overwritten",
                )
            )
            continue
        try:
            pipeline = run_pipeline(
                source,
                profile_path,
                model_output,
                material=material,
                executable=executable,
                timeout_s=timeout_s,
            )
            recommended, time_s, mass_g = _selected_run_metrics(pipeline)
            items.append(
                BatchItemResult(
                    source_path=source,
                    source_sha256=source_hash,
                    output_dir=model_output,
                    status="completed",
                    mode=pipeline.mode,
                    recommended_support=recommended,
                    total_print_time_s=time_s,
                    total_used_g=mass_g,
                    manifest_path=pipeline.manifest_path,
                    error=None,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- isolate failures between batch items
            items.append(
                BatchItemResult(
                    source_path=source,
                    source_sha256=source_hash,
                    output_dir=model_output,
                    status="failed",
                    mode=None,
                    recommended_support=None,
                    total_print_time_s=None,
                    total_used_g=None,
                    manifest_path=manifest_path if manifest_path.is_file() else None,
                    error=str(exc),
                )
            )

    summary_path, html_path, error_path = _next_report_paths(output_dir, resume)
    created_utc = datetime.now(UTC).isoformat()
    result = BatchRunResult(
        input_dir=source_dir,
        output_dir=output_dir,
        profile_template_path=profile_path,
        created_utc=created_utc,
        completed=sum(item.status == "completed" for item in items),
        skipped=sum(item.status == "skipped" for item in items),
        failed=sum(item.status == "failed" for item in items),
        items=tuple(items),
        summary_path=summary_path,
        html_report_path=html_path,
        error_log_path=error_path if any(item.status == "failed" for item in items) else None,
    )
    payload = result.to_dict()
    schema_errors = validate_release_document(payload, "batch-summary")
    if schema_errors:
        raise BatchError("generated batch summary violates release schema: " + "; ".join(schema_errors))
    atomic_write_new_json(summary_path, payload)
    if result.error_log_path is not None:
        error_lines = "".join(
            json.dumps(item.to_dict(), ensure_ascii=False) + "\n"
            for item in items
            if item.status == "failed"
        )
        atomic_write_new_text(result.error_log_path, error_lines)
    _write_html(result)
    return result
