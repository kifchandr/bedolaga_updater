"""Реестр патчей бота Bedolaga.

Каждый патч — модуль в этом пакете с функцией `install()`. Модули перечислены
в `PATCH_MODULES`; порядок значения не имеет. Падение одного патча не мешает
остальным и не мешает боту стартовать.

Как добавить новый патч:
  1. Положить `bedolaga_patches/<имя>.py` с функцией `install()`.
  2. Добавить `<имя>` в PATCH_MODULES ниже.
  3. Добавить оба файла в patches/manifest.txt репозитория апдейтера.
"""

import importlib
import traceback


__all__ = ['PATCH_MODULES', 'install']

PATCH_MODULES = (
    'connect_reminder',
)


def _log(message: str) -> None:
    print(f'[bedolaga-patches] {message}', flush=True)


def install() -> None:
    """Подключить все патчи. Вызывается из bootstrap.py до запуска main.py."""
    installed = []
    for name in PATCH_MODULES:
        try:
            module = importlib.import_module(f'{__name__}.{name}')
            module.install()
        except Exception:
            _log(f'патч {name!r} не подключён из-за ошибки')
            traceback.print_exc()
            continue
        installed.append(name)

    if installed:
        _log(f'подключены патчи: {", ".join(installed)}')
    else:
        _log('ни один патч не подключён')
