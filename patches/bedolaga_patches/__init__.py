"""Реестр патчей бота Bedolaga.

Каждый патч — модуль в этом пакете с функцией `install()`. Патч включается и
выключается переменной `PATCH_<ИМЯ>_ENABLED` в `patches/patches.env`; по
умолчанию включены все. Падение одного патча не мешает остальным и не мешает
боту стартовать.

Как добавить новый патч:
  1. Положить `bedolaga_patches/<имя>.py` с функцией `install()`.
  2. Добавить строку в PATCH_MODULES ниже.
  3. Добавить оба файла в patches/manifest.txt репозитория апдейтера.
  4. Добавить `PATCH_<ИМЯ>_ENABLED=1` в patches.env.example.
"""

import importlib
import traceback

from . import config


__all__ = ['PATCH_MODULES', 'enabled_flag', 'install', 'is_enabled']

# (имя модуля, описание для вывода в скрипте обновления)
PATCH_MODULES = (
    ('connect_reminder', 'Инструкция по подключению, если за 10 минут не подключился'),
    ('contest_report', 'Отчёт по рефералам для конкурса в админ-панели'),
    ('shop_button', 'Кнопка магазина внизу главного меню'),
)


def _log(message: str) -> None:
    print(f'[bedolaga-patches] {message}', flush=True)


def enabled_flag(name: str) -> str:
    """Имя переменной-выключателя для патча."""
    return f'PATCH_{name.upper()}_ENABLED'


def is_enabled(name: str) -> bool:
    return config.get_bool(enabled_flag(name), True)


def install() -> None:
    """Подключить включённые патчи. Вызывается из bootstrap.py до запуска main.py."""
    installed = []
    skipped = []
    failed = []

    for name, _description in PATCH_MODULES:
        if not is_enabled(name):
            skipped.append(name)
            continue
        try:
            module = importlib.import_module(f'{__name__}.{name}')
            module.install()
        except Exception:
            _log(f'патч {name!r} не подключён из-за ошибки')
            traceback.print_exc()
            failed.append(name)
            continue
        installed.append(name)

    if installed:
        _log(f'подключены патчи: {", ".join(installed)}')
    else:
        _log('ни один патч не подключён')
    if skipped:
        _log(f'выключены настройкой: {", ".join(skipped)}')
    if failed:
        _log(f'упали при подключении: {", ".join(failed)}')
