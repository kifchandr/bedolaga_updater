"""Чтение настроек патчей.

Настройки берутся из `patches/patches.env` рядом с пакетом. Переменная
окружения с тем же именем имеет приоритет — так можно временно переопределить
значение через docker-compose, не редактируя файл.

Файл лежит вне git-дерева бота и переживает обновления: `bedolaga_updater`
создаёт его один раз и больше не перезаписывает.
"""

import os
import threading


_PATCH_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_ENV_FILE = os.path.join(_PATCH_ROOT, 'patches.env')

_lock = threading.Lock()
_file_values: dict[str, str] | None = None


def _parse_env_file(path: str) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        with open(path, encoding='utf-8') as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith('#') or '=' not in line:
                    continue
                key, _, value = line.partition('=')
                key = key.strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                if key:
                    values[key] = value
    except FileNotFoundError:
        pass
    except OSError as error:
        print(f'[bedolaga-patches] не удалось прочитать {path}: {error}', flush=True)
    return values


def _values() -> dict[str, str]:
    global _file_values
    if _file_values is None:
        with _lock:
            if _file_values is None:
                _file_values = _parse_env_file(_ENV_FILE)
    return _file_values


def reload() -> None:
    """Сбросить кеш файла настроек (используется тестами и ручной отладкой)."""
    global _file_values
    with _lock:
        _file_values = None


def get(key: str, default: str | None = None) -> str | None:
    env_value = os.environ.get(key)
    if env_value is not None and env_value != '':
        return env_value
    return _values().get(key, default)


def get_bool(key: str, default: bool) -> bool:
    raw = get(key)
    if raw is None or raw == '':
        return default
    return raw.strip().lower() not in ('0', 'false', 'no', 'off')


def get_int(key: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = get(key)
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if minimum is not None and value < minimum:
        return minimum
    if maximum is not None and value > maximum:
        return maximum
    return value


def get_float(key: str, default: float) -> float:
    raw = get(key)
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return default


def get_list(key: str, default: list[str]) -> list[str]:
    """Список через запятую. Пустое значение = пустой список, а не default."""
    raw = get(key)
    if raw is None:
        return list(default)
    return [item.strip() for item in raw.split(',') if item.strip()]


def get_str(key: str, default: str) -> str:
    raw = get(key)
    if raw is None:
        return default
    raw = raw.strip()
    return raw or default
