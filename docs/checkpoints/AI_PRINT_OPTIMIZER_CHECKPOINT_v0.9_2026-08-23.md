# AI Print Optimizer — контрольная точка v0.9

Дата: 23 августа 2026 года. Статус: release candidate interfaces.

## Реализовано

- JSON Schema Draft 2020-12 для pipeline manifest и batch summary;
- dependency-free структурная проверка документов по bundled schema;
- атомарная публикация JSON/HTML через fsync и hard-link без перезаписи;
- отдельный JSONL-журнал неуспешных batch items;
- `--self-check` без обязательного MODEL;
- диагностика Python, metadata version, зависимостей, схем, atomic write и Bambu;
- тест переключения активного материала через `M620 SxA` в G-code;
- package data для схем в wheel/editable install;
- 37 автоматических тестов.

## Реальные проверки

После обновления editable install:

```text
source version:    0.9.0
installed version: 0.9.0
numpy:             2.5.2
trimesh:           4.12.2
fast-simplification: 0.1.13
Bambu Studio:      найден
schemas:           2/2
atomic write:      OK
self-check:        OK
```

Resume реального batch v0.8 прошёл через новые schema checks: два результата
проверены и пропущены, создан новый атомарный summary/report, ошибок нет.

На ПК не найден готовый многоматериальный G-code с переключением `M620 S1A`;
парсер проверен синтетическим контрольным G-code с двумя плотностями и диаметрами.

## Условия перехода к v1.0

1. зафиксировать публичный CLI/API и версию schema;
2. добавить release metadata, лицензию и changelog;
3. собрать wheel и source distribution;
4. установить wheel в чистое venv и прогнать тесты/diagnostics;
5. повторно проверить реальные manifest и batch report;
6. создать финальный Git tag и переносимый релиз.
