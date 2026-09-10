# Архив: разработка старого контура в WSL2 Ubuntu

> Установленное приложение VANIOR PRINT 0.6.6 не требует WSL2. Инструкция ниже
> сохранена только для воспроизведения исторической среды разработки.

Команды ниже рассчитаны на текущую локальную папку проекта. Выполняйте их по блокам и переходите дальше только после проверки.

## 1. Открыть Ubuntu

В PowerShell Windows:

```powershell
wsl -d Ubuntu
```

Проверка в открывшемся терминале Ubuntu:

```bash
uname -a
```

В строке результата должно присутствовать `Linux` и обычно `WSL2`.

## 2. Перейти в проект

```bash
cd /mnt/c/Users/vanov/.codex/.chatgpt-projects/g-p-6a8a9ea824a88191867891ef7c3b51ae/ai-print-optimizer
pwd
ls
```

`pwd` должен завершаться на `/ai-print-optimizer`, а `ls` должен показать `pyproject.toml`, `src`, `tests` и `README.md`.

## 3. Проверить инструменты

```bash
python3 --version
git --version
```

Поддерживается Python 3.11 или новее.

Если команда создания окружения далее сообщит, что `ensurepip` недоступен, один раз установите системный модуль:

```bash
sudo apt update
sudo apt install -y python3-venv git
```

## 4. Создать и активировать окружение

```bash
python3 -m venv .venv
source .venv/bin/activate
python --version
which python
```

Путь от `which python` должен завершаться на `ai-print-optimizer/.venv/bin/python`.

Окружение уже создано в текущей рабочей копии. При повторном входе в WSL достаточно выполнить только:

```bash
source .venv/bin/activate
```

## 5. Установить проект и минимальные зависимости

```bash
python -m pip install --upgrade pip
python -m pip install -e .
python -m pip check
```

Последняя команда должна вывести `No broken requirements found.`. Основные
зависимости проекта — `numpy`, `trimesh` и `fast-simplification`.

## 6. Проверить Git

```bash
git branch --show-current
git status --short
```

Текущая ветка должна называться `main`. До первого коммита Git покажет новые файлы проекта; `.venv` не должна появляться в списке.

## 7. Запустить тесты

```bash
python -m unittest discover -s tests -v
```

Ожидаемый итог v1.0: не менее `Ran 39 tests`, все тесты имеют статус `ok`,
последняя строка — `OK`.

## 8. Проанализировать STL

```bash
ai-print-optimizer /полный/путь/к/model.stl --material PLA
```

Для PETG:

```bash
ai-print-optimizer /полный/путь/к/model.stl --material PETG
```

Для передачи результата другой программе:

```bash
ai-print-optimizer /полный/путь/к/model.stl --material PLA --json
```

Для создания отдельной очищенной копии:

```bash
ai-print-optimizer /полный/путь/к/model.stl \
  --material PLA \
  --repair-output /полный/путь/к/model.repaired.stl
```

Исходный файл не изменяется. Если выходной файл уже существует, команда остановится без перезаписи.

Для сравнения ориентаций:

```bash
ai-print-optimizer /полный/путь/к/model.repaired.stl --material PLA --orient
```

Для сохранения лучшей ориентации в отдельный файл:

```bash
ai-print-optimizer /полный/путь/к/model.repaired.stl \
  --material PLA \
  --orient-output /полный/путь/к/model.oriented.stl
```

Если команда `ai-print-optimizer` не найдена, окружение не активно. Выполните `source .venv/bin/activate` и повторите запуск.

## 9. Запустить Bambu Studio CLI из WSL

Готовый 3MF:

```bash
ai-print-optimizer /полный/путь/project.3mf \
  --slice-output /полный/новый/каталог
```

Отдельный STL использует настройки из заранее проверенного 3MF:

```bash
ai-print-optimizer /полный/путь/model.repaired.stl \
  --profile-template /полный/путь/verified-profile.3mf \
  --slice-output /полный/новый/каталог
```

Выходной каталог обязан быть новым. Внутри появятся `result.json`,
`plate_1.gcode`, `ready-to-print.3mf` и изолированный `.bambu-data`. Для STL
создаётся `prepared-project.3mf`, в котором геометрия шаблона заменена целевой.
Глобальные настройки Bambu Studio не изменяются.

## 10. Проверить установленный пакет

Запустите встроенную диагностику:

```bash
ai-print-optimizer --self-check
```

Каждая строка должна начинаться с `OK`. Проверяются версия пакета, зависимости,
схемы результатов, атомарная запись и обнаружение Bambu Studio.

## 11. Запустить полный pipeline

```bash
ai-print-optimizer /полный/путь/model.stl \
  --profile-template /полный/путь/verified-profile.3mf \
  --pipeline-output /полный/новый/pipeline-каталог
```

Или для 3MF без отдельного шаблона:

```bash
ai-print-optimizer /полный/путь/model.3mf \
  --pipeline-output /полный/новый/pipeline-каталог
```

Открывать в Bambu Studio нужно файл `pipeline-каталог/ready-to-print.3mf`.
Рядом находится проверенный `ready-to-print.gcode` с теми же настройками.

Проверить целостность результата:

```bash
ai-print-optimizer /полный/новый/pipeline-каталог/manifest.json --verify-manifest
```
