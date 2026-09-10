# AI Print Optimizer — контрольная точка v0.5

Дата: 23 августа 2026 года. Статус: рабочая интеграция реального слайсинга.

## Реализовано после v0.4

- обнаружение Bambu Studio в Windows и WSL;
- безопасный запуск CLI 2.8.2.61 в отдельном `datadir`/`outputdir`;
- ожидание асинхронного `result.json` с таймаутом;
- слайсинг готового Bambu 3MF;
- слайсинг отдельного/repaired STL через непечатаемый carrier проверенного 3MF;
- разбор времени, массы, длины, стоимости, supports time и предупреждений;
- создание и A/B-слайсинг `normal(auto)` против `tree(auto)`;
- рекомендация по сбалансированной оценке времени и материала;
- сохранение source/template SHA-256;
- классификация субмиллиметрового мусора с большим числом граней;
- отдельный `topology_status`: `WATERTIGHT`, `OPEN`, `NON_MANIFOLD`, `OPEN_AND_NON_MANIFOLD`;
- 22 автоматических теста.

## Реальные проверки

`Porta_Filaccini_v1.3mf` успешно слайсится из WSL: 2595,32 с, 16,48 г.

Сравнение поддержек той же модели:

```text
normal(auto): 2741,15 с / 23,63 г
tree(auto):   2724,82 с / 21,40 г
выбор: tree (-16,3 с / -2,23 г)
```

`mini_scorpione.repaired.stl` успешно передан через carrier профиля
`mini_scorpione.3mf`: 1554,34 с, 5,62 г, 877106 треугольников. Оба исходника
остались неизменными по SHA-256.

## Честное ограничение API Bambu

`result.json` 2.8.2.61 не публикует отдельную массу support volumes: поле
`main_used_g` содержит модель вместе с поддержками. v0.5 показывает support feature
time и сравнивает реальные итоговые значения вариантов, но не выдаёт фиктивную
абсолютную массу поддержек.

## Команды

```bash
python -m unittest discover -s tests -v

ai-print-optimizer project.3mf --slice-output NEW_DIR

ai-print-optimizer repaired.stl \
  --profile-template verified-profile.3mf \
  --slice-output NEW_DIR

ai-print-optimizer project.3mf --compare-supports NEW_DIR
```

## Следующий этап v0.6

1. связать `analyze -> repair -> orient -> carrier -> slice` одной pipeline-командой;
2. создавать support variants и для входного STL, а не только готового 3MF;
3. разбирать role-specific extrusion из G-code для независимой массы supports;
4. добавить manifest запуска с версиями и SHA-256 всех входов/артефактов;
5. проверить Hulk normal/tree с одинаковым профилем;
6. локализовать оставшиеся англоязычные разделы геометрического отчёта;
7. после программной проверки запросить физический A/B-отпечаток выбранных моделей.

