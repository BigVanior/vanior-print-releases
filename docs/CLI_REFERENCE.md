# Архив: CLI внешнего контура v3.0

> Описанные ниже команды Bambu-пайплайна оставлены как исторический интерфейс
> разработчика и не входят в пользовательский релизный путь 0.6.6. Актуальная
> самостоятельная команда: `--vanior-slice-output`; диагностика: `--self-check`.

Точка входа: `ai-print-optimizer` или `python -m ai_print_optimizer`.

## Основные режимы

| Режим | Обязательные аргументы | Результат |
|---|---|---|
| Анализ | `MODEL.stl` или `MODEL.3mf` | Текстовый или JSON-отчёт |
| Ремонт | `MODEL.stl --repair-output NEW.stl` | Новая консервативно исправленная копия |
| Ориентация | `MODEL.stl --orient` | Три лучшие ориентации |
| Экспорт ориентации | `MODEL.stl --orient-output NEW.stl` | Новая ориентированная копия |
| Упрощение | `MODEL.stl --simplify-output NEW.stl --target-faces N` | Новая копия после проверки инвариантов |
| Слайсинг | `MODEL --slice-output NEW_DIR` | Проверенный 3MF, G-code и `result.json` |
| Сравнение стратегий | `MODEL --compare-supports NEW_DIR` | None/normal/tree и готовый победитель |
| Pipeline STL | `MODEL.stl --pipeline-output NEW_DIR` | Готовые 3MF/G-code и manifest; профиль создаётся автоматически |
| Pipeline 3MF | `MODEL.3mf --pipeline-output NEW_DIR` | Извлечённая/проверенная модель и готовые 3MF/G-code |
| Batch | `DIR --profile-template PROFILE.3mf --batch-output NEW_DIR` | Результаты моделей и сводный отчёт |
| Проверка manifest | `manifest.json --verify-manifest` | Проверка SHA-256 всех записей |
| Проверка профиля | `PROFILE.3mf --validate-profile` | Совместимость принтера и материала |
| Самодиагностика | `--self-check` | Проверка установленного окружения |

Для полного pipeline STL/3MF профиль создаётся из системных пресетов установленного
Bambu Studio. Низкоуровневым режимам `--slice-output` и `--compare-supports` для
STL по-прежнему требуется явный `--profile-template`. Готовый Bambu 3MF уже
содержит настройки.

Приоритет полного pipeline задаётся `--print-priority quality`,
`--print-priority strength`, `--print-priority balanced` или
`--print-priority fast`.
Назначение модели задаётся `--model-purpose auto`, `decorative` или `functional`.
Подробный сценарий задаётся `--functional-intent`: `auto`, `decorative`,
`enclosure`, `fixture`, `gear`, `snap_fit`, `vessel`, `flexible`, `structural`
или `general_functional`. Для повторных запусков можно указать проверяемый кэш
`--slice-cache DIRECTORY`; восстановленный результат всегда проверяется заново.
Pipeline применяет рекомендации анализатора и сохраняет
`ready-to-print.3mf` и `ready-to-print.gcode` в корне результата. Все выходные пути должны быть новыми; приложение не
перезаписывает результаты. `--json` делает вывод пригодным для автоматизации.

## Общие параметры

- `--material PLA|PETG` — ожидаемый материал, по умолчанию PLA.
- `--overhang-angle 1..89` — порог нависаний, по умолчанию 45°.
- `--plate N` — номер пластины 3MF, начиная с 1.
- `--slice-timeout SECONDS` — ожидание каждого запуска слайсера.
- `--slicer-executable PATH` — явный путь к Bambu Studio.
- `--resume` — проверить и пропустить готовые элементы batch.
- `--no-recursive` — не обходить вложенные каталоги batch.

## Коды завершения

| Код | Значение |
|---:|---|
| 0 | Успех |
| 1 | Ошибка аргументов, чтения или запуска |
| 2 | Модель не помещается в рабочую область |
| 3 | Ошибка слайсинга |
| 4 | Манифест повреждён или не прошёл SHA-256 |
| 5 | Профиль несовместим |
| 6 | Batch завершён с отдельными ошибками |
| 7 | Самодиагностика не пройдена |

Полный перечень флагов всегда доступен через `ai-print-optimizer --help`.
