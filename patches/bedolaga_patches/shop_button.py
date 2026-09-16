"""Кнопка магазина в главном меню бота.

Добавляет в главное меню кнопку-ссылку (по умолчанию «💰 Наш магазин» →
https://ahrishop.com/) — выше служебных строк «Админ-панель» и «Модерация»,
а у обычного пользователя последней.

Почему патч, а не штатная возможность
-------------------------------------
В боте есть свои кастомные кнопки главного меню (таблица `main_menu_buttons`,
управляются через кабинет). Но в клавиатуре они вставляются в середину — перед
промокодом, рефералами, конкурсами и поддержкой, — и нужного места через них не
добиться. Патч вставляет строку уже в готовую клавиатуру.

Как встраивается
----------------
Оборачивается `app.keyboards.inline.get_main_menu_keyboard_async` — единственная
точка, через которую главное меню рисуют и menu.py, и start.py. Внутри неё две
ветки (конструктор меню и обычная сборка), но обе возвращают готовый
InlineKeyboardMarkup, поэтому обёртка снаружи покрывает обе.

Обёртка ставится ДО импорта хендлеров, иначе их `from ... import` унесёт
оригинал; уже импортированным модулям имя перепривязывается вручную.

Текстовый режим меню (`TEXT_MAIN_MENU_MODE`) патч не трогает: там reply-клавиатура,
а она не умеет кнопки-ссылки.
"""

import sys

import structlog
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import config


__all__ = ['install']

logger = structlog.get_logger('bedolaga_patches.shop_button')

DEFAULT_TEXT = '💰 Наш магазин'
DEFAULT_URL = 'https://ahrishop.com/'

# Служебные строки меню: «Админ-панель» и «Модерация». Обе ветки сборки
# клавиатуры вешают на них эти callback_data и ставят их в самый низ.
STAFF_CALLBACKS = ('admin_panel', 'moderator_panel')


def _text() -> str:
    return config.get_str('PATCH_SHOP_BUTTON_TEXT', DEFAULT_TEXT)


def _url() -> str:
    return config.get_str('PATCH_SHOP_BUTTON_URL', DEFAULT_URL)


def _valid_url(url: str) -> bool:
    """Ссылку проверяем до того, как она попадёт в клавиатуру.

    Кнопка добавляется в КАЖДУЮ отрисовку главного меню. Битый URL Telegram
    отвергает вместе со всем сообщением — пользователь остался бы без меню
    вообще. Поэтому при сомнительной ссылке кнопку просто не рисуем.
    """
    return url.startswith(('http://', 'https://')) and len(url) > len('https://')


def _insert_at(rows) -> int:
    """Номер строки, перед которой встаёт кнопка.

    Выше служебных строк: «Админ-панель» и «Модерация» всегда идут последними,
    и магазин под ними читался бы как часть админки. У обычного пользователя
    таких строк нет — тогда кнопка просто последняя.
    """
    for index, row in enumerate(rows):
        for button in row:
            if getattr(button, 'callback_data', None) in STAFF_CALLBACKS:
                return index
    return len(rows)


def _with_shop_button(markup):
    if markup is None:
        return markup

    url = _url()
    if not _valid_url(url):
        logger.warning('shop_button: ссылка не похожа на http(s)-адрес, кнопку не добавляю', url=url)
        return markup

    # Под try и разбор, и сборка: клавиатура — pydantic-модель, и ругнуться она
    # может на любом из шагов. Функция обязана быть безотказной сама по себе,
    # а не за счёт того, что вызывающий её обернул.
    try:
        rows = list(markup.inline_keyboard)

        # Кнопка с этой же ссылкой уже есть (например, заведена в кабинете как
        # штатная кастомная кнопка) — второй такой же не нужно.
        if any(getattr(button, 'url', None) == url for row in rows for button in row):
            return markup

        rows.insert(_insert_at(rows), [InlineKeyboardButton(text=_text(), url=url)])
        return InlineKeyboardMarkup(inline_keyboard=rows)
    except Exception as error:  # noqa: BLE001
        logger.warning('shop_button: клавиатура не поддалась, оставляю как есть', error=str(error))
        return markup


def install() -> None:
    import app.keyboards.inline as inline_module

    original = inline_module.get_main_menu_keyboard_async
    if getattr(original, '_bedolaga_shop_button_wrapped', False):
        return

    async def get_main_menu_keyboard_async_with_shop(*args, **kwargs):
        markup = await original(*args, **kwargs)
        try:
            return _with_shop_button(markup)
        except Exception as error:  # noqa: BLE001
            # Меню важнее кнопки: любая неожиданность — отдаём исходную клавиатуру.
            logger.error('shop_button: не удалось дорисовать кнопку', error=str(error))
            return markup

    get_main_menu_keyboard_async_with_shop._bedolaga_shop_button_wrapped = True
    inline_module.get_main_menu_keyboard_async = get_main_menu_keyboard_async_with_shop

    # Хендлеры делают `from app.keyboards.inline import get_main_menu_keyboard_async`.
    # Те, кто успел импортировать до нас, держат ссылку на оригинал — перепривязываем.
    rebound = 0
    for module in list(sys.modules.values()):
        if module is None or module is inline_module:
            continue
        if getattr(module, 'get_main_menu_keyboard_async', None) is original:
            module.get_main_menu_keyboard_async = get_main_menu_keyboard_async_with_shop
            rebound += 1

    logger.info(
        'shop_button: кнопка добавлена в главное меню',
        text=_text(),
        url=_url(),
        rebound_modules=rebound,
    )
