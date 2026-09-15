#!/usr/bin/env python3
"""Точка входа контейнера бота, когда установлены патчи.

Штатный образ запускается как `python main.py`. docker-compose.override.yml,
который раскладывает `bedolaga_updater patches`, подменяет команду на этот файл.
Он подключает наши патчи и после этого запускает ровно тот же main.py — файлы
самого бота не меняются, поэтому `git checkout <тег>` при обновлении их не
трогает и не конфликтует.

Главное свойство: сломанный патч НЕ должен ронять бота. Любая ошибка на этапе
подключения логируется и проглатывается, main.py стартует в любом случае.
"""

import os
import runpy
import sys
import traceback


PATCH_DIR = os.path.dirname(os.path.abspath(__file__))
APP_DIR = os.environ.get('BEDOLAGA_APP_DIR', '/app')
MAIN_PY = os.path.join(APP_DIR, 'main.py')

# /app обязан идти первым: пакет `app` бота должен резолвиться из образа,
# а не из случайного одноимённого каталога внутри patches/.
for _path in (PATCH_DIR, APP_DIR):
    if _path in sys.path:
        sys.path.remove(_path)
sys.path.insert(0, PATCH_DIR)
sys.path.insert(0, APP_DIR)


def _log(message: str) -> None:
    print(f'[bedolaga-patches] {message}', flush=True)


def _truthy(value: str | None, default: bool = True) -> bool:
    if value is None or value == '':
        return default
    return value.strip().lower() not in ('0', 'false', 'no', 'off')


def _install_patches() -> None:
    try:
        import bedolaga_patches
    except Exception:
        _log('не удалось импортировать пакет патчей — бот стартует без них')
        traceback.print_exc()
        return

    try:
        bedolaga_patches.install()
    except Exception:
        _log('ошибка при подключении патчей — бот стартует без них')
        traceback.print_exc()


if not os.path.isfile(MAIN_PY):
    _log(f'не найден {MAIN_PY} — запускать нечего')
    sys.exit(1)

if _truthy(os.environ.get('BEDOLAGA_PATCHES')):
    _install_patches()
else:
    _log('отключены переменной BEDOLAGA_PATCHES=0')

# main.py читает sys.argv[0] и __file__ — отдаём ему то же окружение,
# что и при прямом `python main.py`.
sys.argv = [MAIN_PY, *sys.argv[1:]]
runpy.run_path(MAIN_PY, run_name='__main__')
