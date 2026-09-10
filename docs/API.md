# Публичный Python API v1.1

Все поддерживаемые имена экспортируются из `ai_print_optimizer.__all__`.
Основные функции:

- `analyze_stl(...)` — анализ геометрии и печатных рисков;
- `repair_stl(...)`, `orient_stl(...)`, `simplify_stl(...)` — создание новых STL-копий;
- `extract_printable_stl(...)` — извлечение печатной геометрии plate из 3MF;
- `create_bambu_project_from_stl(...)` — замена геометрии шаблона целевой моделью;
- `verify_ready_project(...)` — проверка модели, настроек и встроенного G-code;
- `validate_bambu_profile(...)` — проверка совместимости 3MF;
- `slice_3mf(...)`, `slice_stl_with_template(...)` — изолированный слайсинг;
- `compare_supports(...)`, `compare_stl_supports(...)` — none/normal/tree;
- `run_pipeline(...)` — полный pipeline одной модели;
- `run_batch(...)` — пакетная обработка и resume;
- `verify_manifest(...)` — проверка результата по SHA-256;
- `run_diagnostics(...)` — диагностика установленного окружения.

Результаты возвращаются неизменяемыми dataclass-объектами. Ошибки предметных
областей представлены классами `AnalysisError`, `ProfileError`, `SlicerError`,
`PipelineError`, `BatchError` и `SimplificationError`.

Пример:

```python
from ai_print_optimizer import analyze_stl, run_pipeline

report = analyze_stl("model.stl", material="PLA")
print(report.mesh_health.status, report.triangle_count)

result = run_pipeline(
    "model.stl",
    "verified-profile.3mf",
    "new-result-directory",
)
print(result.manifest_path)
```

Совместимость v1 означает сохранение экспортируемых имён, основных сигнатур,
кодов завершения CLI и `schema_version = 1` в пределах ветки 1.x. Новые
необязательные поля могут добавляться без изменения версии схемы.
