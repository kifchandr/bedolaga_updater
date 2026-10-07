"""Порядок способов оплаты: сначала СБП, потом карты, крипта последней.

В боте список способов собран длинной цепочкой `if` в
`app.keyboards.inline.get_payment_methods_keyboard` — порядок задан жёстко
очерёдностью этих `if` и никак не настраивается. Править саму цепочку (под
семьсот строк, меняется от версии к версии) патч не стал бы: он переставляет
уже готовые строки клавиатуры.

Патч трогает два места, и оба нужны: текст сообщения со списком методов
собирается отдельно от кнопок.

  • `app.keyboards.inline.get_payment_methods_keyboard`   — кнопки
  • `app.utils.payment_utils.get_available_payment_methods` — список в тексте

Перестановка «на месте»
-----------------------
Переставляются только строки, которые действительно являются способом оплаты.
Всё остальное — «Через поддержку», «Способы оплаты временно недоступны»,
«Назад», кнопки корзины — остаётся ровно на своих позициях: собираются номера
подходящих строк, их содержимое сортируется, и отсортированное раскладывается
обратно по тем же номерам. Так патч не зависит от того, что ещё бот положил в
эту клавиатуру.
"""

import re
import sys

import structlog

from . import config


__all__ = ['install']

logger = structlog.get_logger('bedolaga_patches.payment_order')

SBP = 'sbp'
CARD = 'card'
CRYPTO = 'crypto'
OTHER = 'other'

DEFAULT_ORDER = (SBP, CARD, OTHER, CRYPTO)
GROUPS = frozenset(DEFAULT_ORDER)

# `topup_heleket`  или  `topup_amount|heleket|50000`
_CALLBACK_RE = re.compile(r'^topup_(?:amount\|)?([a-z0-9_]+?)(?:\|\d+)?$')

# Служебные пункты: не способ оплаты, позицию им менять нельзя.
PINNED_IDS = frozenset({'support'})

# Провайдеры, у которых по названию не догадаться, что это криптовалюта.
CRYPTO_IDS = frozenset({'cryptobot', 'heleket', 'cryptomus', 'coingate', 'nowpayments'})

# Ключевые слова в подписи. Порядок проверки — как в нужном порядке групп:
# «Карты + СБП» попадёт к СБП, а не к картам.
_KEYWORDS = (
    (CRYPTO, ('крипт', 'crypto', 'usdt', 'tether', 'bitcoin', 'btc')),
    (SBP, ('сбп', 'sbp', ' qr', 'qr-')),
    (CARD, ('карт', 'card')),
)

_ICONS = (
    ('📱', SBP),
    ('🏦', SBP),
    ('💳', CARD),
    ('🪙', CRYPTO),
    ('₿', CRYPTO),
    ('🌕', CRYPTO),
    ('💎', CRYPTO),
)


def _order() -> tuple:
    """Порядок групп. Неизвестное отбрасываем, забытое дописываем в конец.

    Опечатка в настройке не должна приводить к тому, что часть способов оплаты
    молча уедет в неожиданное место или пропадёт из сортировки вовсе.
    """
    raw = [item.lower() for item in config.get_list('PATCH_PAYMENT_ORDER', list(DEFAULT_ORDER))]
    order = []
    for group in raw:
        if group in GROUPS:
            if group not in order:
                order.append(group)
        else:
            logger.warning('payment_order: в PATCH_PAYMENT_ORDER неизвестная группа, пропускаю', group=group)
    for group in DEFAULT_ORDER:
        if group not in order:
            order.append(group)
    return tuple(order)


def _overrides() -> dict:
    """Явная привязка id метода к группе — последнее слово за администратором."""
    result = {}
    for group in DEFAULT_ORDER:
        for method_id in config.get_list(f'PATCH_PAYMENT_ORDER_{group.upper()}', []):
            result[method_id.strip().lower()] = group
    return result


def _classify(method_id: str, label: str, overrides: dict) -> str:
    method_id = (method_id or '').lower()
    override = overrides.get(method_id)
    if override:
        return override

    # Суффикс id — самый надёжный признак: его ставит сам бот, когда провайдер
    # разведён на отдельные кнопки (aurapay_sbp, freekassa_card, ...).
    if method_id.endswith('_sbp'):
        return SBP
    if method_id.endswith(('_card', '_sberpay')):
        return CARD
    if method_id in CRYPTO_IDS:
        return CRYPTO

    # Дальше — по тому, что видит пользователь: названия провайдеров задаются в
    # кабинете, и id о них уже ничего не говорит.
    label = label or ''
    lowered = label.lower()
    for group, words in _KEYWORDS:
        if any(word in lowered for word in words):
            return group

    for icon, group in _ICONS:
        if icon in label:
            return group

    return OTHER


def _reorder(items, key_of, label_of):
    """Переставить только «сортируемые» элементы, остальные оставить на местах.

    `key_of` возвращает id метода или None, если элемент сортировать нельзя.
    Сортировка устойчивая: внутри группы порядок остаётся ботовым.
    """
    order = _order()
    rank = {group: index for index, group in enumerate(order)}
    overrides = _overrides()

    slots = []
    sortable = []
    for index, item in enumerate(items):
        method_id = key_of(item)
        if method_id is None or method_id in PINNED_IDS:
            continue
        slots.append(index)
        sortable.append(item)

    if len(sortable) < 2:
        return items

    sortable.sort(key=lambda item: rank[_classify(key_of(item), label_of(item), overrides)])

    result = list(items)
    for index, item in zip(slots, sortable):
        result[index] = item
    return result


def _row_method_id(row):
    """id способа оплаты для строки клавиатуры, иначе None."""
    if len(row) != 1:
        return None
    button = row[0]
    if getattr(button, 'url', None):
        return None
    match = _CALLBACK_RE.match(getattr(button, 'callback_data', None) or '')
    return match.group(1) if match else None


def _reorder_keyboard(markup):
    if markup is None:
        return markup
    try:
        rows = list(markup.inline_keyboard)
        reordered = _reorder(rows, _row_method_id, lambda row: row[0].text)
        if reordered is rows or reordered == rows:
            return markup
        return type(markup)(inline_keyboard=reordered)
    except Exception as error:  # noqa: BLE001
        # Пополнение баланса важнее порядка кнопок в нём.
        logger.warning('payment_order: клавиатуру переставить не вышло, оставляю как есть', error=str(error))
        return markup


def _reorder_methods(methods):
    try:
        return _reorder(
            list(methods),
            lambda method: str(method.get('id') or '').lower() or None,
            lambda method: '{} {}'.format(method.get('icon', ''), method.get('name', '')),
        )
    except Exception as error:  # noqa: BLE001
        logger.warning('payment_order: список методов переставить не вышло', error=str(error))
        return methods


def _rebind(name: str, original, replacement) -> int:
    """Перепривязать имя у модулей, успевших сделать `from ... import`."""
    rebound = 0
    for module in list(sys.modules.values()):
        if module is None:
            continue
        if getattr(module, name, None) is original:
            setattr(module, name, replacement)
            rebound += 1
    return rebound


def _patch_keyboard() -> int:
    import app.keyboards.inline as inline_module

    original = inline_module.get_payment_methods_keyboard
    if getattr(original, '_bedolaga_payment_order_wrapped', False):
        return 0

    def get_payment_methods_keyboard(*args, **kwargs):
        return _reorder_keyboard(original(*args, **kwargs))

    get_payment_methods_keyboard._bedolaga_payment_order_wrapped = True
    inline_module.get_payment_methods_keyboard = get_payment_methods_keyboard
    # `get_payment_methods_keyboard_with_cart` зовёт эту функцию через глобаль
    # своего модуля, поэтому корзина подхватывает подмену сама.
    return _rebind('get_payment_methods_keyboard', original, get_payment_methods_keyboard)


def _patch_methods_list() -> int:
    import app.utils.payment_utils as payment_utils

    original = payment_utils.get_available_payment_methods
    if getattr(original, '_bedolaga_payment_order_wrapped', False):
        return 0

    def get_available_payment_methods(*args, **kwargs):
        return _reorder_methods(original(*args, **kwargs))

    get_available_payment_methods._bedolaga_payment_order_wrapped = True
    payment_utils.get_available_payment_methods = get_available_payment_methods
    # `get_payment_methods_text` в том же модуле зовёт её по имени — подмены
    # глобали достаточно.
    return _rebind('get_available_payment_methods', original, get_available_payment_methods)


def install() -> None:
    rebound = _patch_keyboard() + _patch_methods_list()
    logger.info('payment_order: порядок способов оплаты изменён', order=_order(), rebound_modules=rebound)
