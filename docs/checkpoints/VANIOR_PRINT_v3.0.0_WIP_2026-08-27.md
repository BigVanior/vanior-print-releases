# VANIOR PRINT v3.0.0 — промежуточная точка

Сохранено: 2026-08-27 перед перезапуском ПК.

## Запрос

Реализовать весь согласованный набор улучшений: локальные настройки поверхностей,
мосты и тонкие стенки, Quality-Constrained Search v3, Support Exit Planning,
расширенное назначение детали, PrintDNA 2, аудит G-code и механизмы надёжности.

## Уже сделано в текущем WIP

- Добавлен `geometry_features.py`:
  - геометрическое обнаружение локальных мостов;
  - оценка пар поверхностей тонких стенок;
  - Support Exit Planning с оценкой доступности удаления;
  - генерация плана локальных диапазонов настроек по высоте.
- Добавлен `functional_intent.py`:
  - категории decorative, enclosure, fixture, gear, snap-fit, vessel, flexible,
    structural и general functional;
  - автоматическая геометрическая классификация;
  - безопасные настройки для каждого назначения;
  - пользовательский выбор имеет приоритет.
- `AnalysisReport` расширен полями geometry features, support exit plan,
  local modifier plan и functional intent.
- `PrintSettings` расширен параметрами functional intent, elephant-foot
  compensation, максимального объёмного потока, flow ratio и pressure advance.
- Анализатор теперь действительно вычисляет мосты и тонкие стенки; старое
  сообщение «not implemented» удалено.
- Pipeline начал принимать и применять расширенный functional intent.
- Старые тесты анализатора, назначения и Surface Intelligence прошли: 29/29.

## Точное место остановки

Исследован официальный формат Bambu Studio. Следующий шаг — записывать
`LocalModifierPlan` в `Metadata/layer_config_ranges.xml` и/или Bambu
modifier parts, передавать план через `create_bambu_project_from_stl` и
`_run_stl_strategy`, а затем проверить сохранение и применение через настоящий
Bambu Studio CLI.

После этого:

1. Quality-Constrained Search v3 с динамическими кандидатами.
2. G-code audit и блокировка опасных кандидатов.
3. PrintDNA 2 с фотографиями и привязкой дефекта к слою/области.
4. Кэширование, resume, резервные копии, диагностический пакет и журнал решений.
5. Интерфейс, документация, полный тестовый набор, реальная нарезка и v3.0.0.

## Ограничения точки

Это WIP, приложение v3 ещё не собрано и не установлено. Рабочий релиз пользователя
остаётся VANIOR PRINT v2.4.0.
