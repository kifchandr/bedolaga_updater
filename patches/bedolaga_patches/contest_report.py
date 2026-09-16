"""Отчёт по рефералам за период для конкурса (админ-панель).

Возможности:
  * Кнопка «🏆 Конкурс рефералов» в админ-панели — добавляется обёрткой вокруг
    клавиатуры, правки файлов бота не нужны.
  * Пресеты периода (7 / 30 дней, текущий / прошлый месяц) и свой период;
    последние введённые вручную периоды предлагаются кнопками.
  * Переключаемая сортировка топа: по оплатам (умолч.) / приглашённым / доходу.
  * Статистика по конкретному пользователю (@username или telegram_id).
  * CSV со сводкой и детализацией — по кнопке, а не автоматически.
  * Генерация списка билетов для розыгрыша: 1 билет = 1 реферал с покупкой либо
    1 приглашённый; участник в файле — @username, а если его нет, то id.
  * Команда: /contest_report [НАЧАЛО КОНЕЦ tz= min= top= scope=]

Что считается:
  * «Пригласил» — рефералы (users.referred_by_id = реферер), зарегистрированные
                  (users.created_at) в пределах периода.
  * «Оплатили»  — среди них те, у кого ПЕРВОЕ реальное пополнение баланса
                  (transactions.type='deposit', is_completed, реальный платёжный
                  метод, сумма >= min ₽) попадает в период (scope=period) или в
                  любое время (scope=all).
  * «Доход»     — сумма всех реальных завершённых пополнений этих рефералов
                  внутри периода.

Данные бота патч только ЧИТАЕТ (SELECT) — ничего в них не пишет и не меняет.
Пишет он лишь в собственную таблицу `patch_contest_report_periods` (история
введённых вручную периодов), о которой alembic бота не знает.

Агрегат за период кешируется на короткое время: смена сортировки, выгрузка CSV
и оба вида билетов иначе гоняли бы один и тот же тяжёлый запрос заново.

Как встраивается
----------------
1. `app.keyboards.admin.get_admin_main_keyboard` оборачивается ДО того, как
   хендлеры бота его импортируют — поэтому кнопка появляется на всех путях
   входа в админку, а не только на главном.
2. `app.bot.setup_bot` оборачивается так, чтобы после сборки Dispatcher'а
   зарегистрировать наши хендлеры. Регистрация идёт последней: в боте нет
   message-хендлеров без фильтров, поэтому наши FSM-состояния не перехватываются.
"""

import csv
import hashlib
import html
import io
import sys
from datetime import datetime, timedelta, timezone
from time import monotonic

import structlog
from aiogram import Dispatcher, F, types
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import bindparam, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import User
from app.utils.decorators import admin_required, error_handler

from . import config


__all__ = ['install']

logger = structlog.get_logger('bedolaga_patches.contest_report')

# ---- Настройки (переопределяются в patches/patches.env) ----------------------
DEFAULT_TZ_OFFSET = config.get_float('PATCH_CONTEST_REPORT_TZ_OFFSET', 3.0)
DEFAULT_MIN_DEPOSIT_RUB = config.get_float('PATCH_CONTEST_REPORT_MIN_DEPOSIT_RUB', 100)
DEFAULT_TOP = config.get_int('PATCH_CONTEST_REPORT_TOP', 20, minimum=1, maximum=200)
DEFAULT_SCOPE = config.get_str('PATCH_CONTEST_REPORT_SCOPE', 'period')
DEFAULT_SORT = 'paid'
# Платёжные методы, которые не считаются реальным пополнением (ручное начисление админом).
NON_REAL_METHODS = config.get_list('PATCH_CONTEST_REPORT_NON_REAL_METHODS', ['manual'])
MENU_BUTTON_TEXT = config.get_str('PATCH_CONTEST_REPORT_BUTTON_TEXT', '🏆 Конкурс рефералов')
# Кнопка админ-меню, под которую встаёт наша: «💰 Промокоды/Статистика».
ADMIN_ANCHOR_CALLBACK = config.get_str('PATCH_CONTEST_REPORT_ANCHOR', 'admin_submenu_promo')
# Сколько последних периодов, введённых вручную, предлагать кнопками.
RECENT_PERIODS = config.get_int('PATCH_CONTEST_REPORT_RECENT_PERIODS', 3, minimum=0, maximum=10)
# Сколько разных периодов держать в кеше одновременно.
CACHE_MAX_ENTRIES = 4
# ------------------------------------------------------------------------------

if DEFAULT_SCOPE not in ('period', 'all'):
    DEFAULT_SCOPE = 'period'

SORT_KEYS = {
    'paid': ('purchased', 'revenue_kopeks', 'invited'),
    'invited': ('invited', 'purchased', 'revenue_kopeks'),
    'revenue': ('revenue_kopeks', 'purchased', 'invited'),
}
SORT_LABEL = {'paid': 'по оплатам', 'invited': 'по приглашённым', 'revenue': 'по доходу'}

USAGE = (
    '📊 <b>Отчёт по рефералам за период</b>\n\n'
    '<code>/contest_report НАЧАЛО КОНЕЦ [tz=3] [min=100] [top=20] [scope=period|all]</code>\n\n'
    'Даты: <code>ГГГГ-ММ-ДД</code>. Пример:\n'
    '<code>/contest_report 2026-06-01 2026-06-30 tz=3 min=100</code>'
)


class ContestReportStates(StatesGroup):
    waiting_period = State()
    waiting_user = State()


# ---------- Вспомогательные --------------------------------------------------


def _tz():
    return timezone(timedelta(hours=DEFAULT_TZ_OFFSET))


def _bind_dt(dt, sqlite):
    # Колонки дат в Postgres — timestamptz (миграция 0007 сконвертировала все
    # naive-колонки), поэтому туда уходит aware-datetime. SQLite хранит naive.
    dt = dt.astimezone(timezone.utc)
    return dt.replace(tzinfo=None) if sqlite else dt


def _parse_boundary(value, tz_offset, is_end):
    only_date = False
    dt = None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(value, fmt)
            only_date = fmt == '%Y-%m-%d'
            break
        except ValueError:
            continue
    if dt is None:
        raise ValueError(f'не смог разобрать дату: {value}')
    dt = dt.replace(tzinfo=timezone(timedelta(hours=tz_offset)))
    if is_end and only_date:
        # Голая дата конца означает «включительно», поэтому граница — её полночь+1.
        dt = dt + timedelta(days=1)
    return dt.astimezone(timezone.utc)


def _to_aware(dt):
    """Привести значение даты из БД к aware-datetime в UTC.

    Postgres через asyncpg отдаёт datetime, а SQLite на сыром SQL — строку:
    text()-запрос не проходит через TypeDecorator модели, и типизации у колонки
    не остаётся. Поэтому строку разбираем здесь, иначе весь отчёт падает на
    SQLite-режиме, который бот поддерживает.
    """
    if dt is None:
        return None
    if isinstance(dt, str):
        value = dt.strip()
        if value.endswith('Z'):
            value = value[:-1] + '+00:00'
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(dt, datetime):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _display_name(username, first_name, last_name, tg_id):
    if username:
        return '@' + username
    name = ' '.join(p for p in (first_name, last_name) if p)
    return name or (f'id{tg_id}' if tg_id else '—')


def _preset_window(preset):
    now_local = datetime.now(_tz())
    midnight = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    now_utc = now_local.astimezone(timezone.utc)
    if preset == '7':
        return (now_local - timedelta(days=7)).astimezone(timezone.utc), now_utc, 'последние 7 дней'
    if preset == '30':
        return (now_local - timedelta(days=30)).astimezone(timezone.utc), now_utc, 'последние 30 дней'
    if preset == 'month':
        return midnight.replace(day=1).astimezone(timezone.utc), now_utc, 'текущий месяц'
    if preset == 'prevmonth':
        first_this = midnight.replace(day=1)
        start = (first_this - timedelta(days=1)).replace(day=1)
        return start.astimezone(timezone.utc), first_this.astimezone(timezone.utc), 'прошлый месяц'
    raise ValueError('неизвестный пресет')


# ---------- Клавиатуры -------------------------------------------------------


def _menu_kb():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text='📅 7 дней', callback_data='contest_report_run:7'),
                InlineKeyboardButton(text='📅 30 дней', callback_data='contest_report_run:30'),
            ],
            [
                InlineKeyboardButton(text='🗓 Текущий месяц', callback_data='contest_report_run:month'),
                InlineKeyboardButton(text='🗓 Прошлый месяц', callback_data='contest_report_run:prevmonth'),
            ],
            [InlineKeyboardButton(text='✏️ Свой период', callback_data='contest_report_custom')],
            [InlineKeyboardButton(text='📇 Статистика пользователя', callback_data='contest_report_userstats')],
            [InlineKeyboardButton(text='⬅️ Назад', callback_data='admin_panel')],
        ]
    )


def _cancel_kb(recent=None):
    """Экранам ввода нужен выход: без него из них выбирались только текстом
    или /start, а брошенное состояние съедало следующее сообщение админа.

    На экране «свой период» сверху идут кнопки с последними введёнными
    периодами, чтобы не набирать одно и то же заново.
    """
    rows = []
    for key, raw in recent or []:
        rows.append([InlineKeyboardButton(text=f'🔁 {raw}'[:64], callback_data=f'crperiod:{key}')])
    rows.append([InlineKeyboardButton(text='⬅️ Отмена', callback_data='contest_report_menu')])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _report_kb(active_sort):
    def mark(key, label):
        return ('✅ ' if key == active_sort else '') + label

    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=mark('paid', '🥇 Оплаты'), callback_data='crsort:paid'),
                InlineKeyboardButton(text=mark('invited', '👥 Приглаш.'), callback_data='crsort:invited'),
                InlineKeyboardButton(text=mark('revenue', '💰 Доход'), callback_data='crsort:revenue'),
            ],
            [InlineKeyboardButton(text='📄 Выгрузить CSV', callback_data='crcsv')],
            [
                InlineKeyboardButton(text='🎟 Билеты: оплаты', callback_data='crraffle:paid'),
                InlineKeyboardButton(text='🎟 Билеты: приглашённые', callback_data='crraffle:invited'),
            ],
            [InlineKeyboardButton(text='⬅️ Меню', callback_data='contest_report_menu')],
        ]
    )


def _menu_text():
    sign = '+' if DEFAULT_TZ_OFFSET >= 0 else ''
    return (
        '🏆 <b>Отчёт по рефералам для конкурса</b>\n\n'
        'По каждому рефереру: сколько пригласил за период и сколько из них сделали '
        f'первую реальную покупку (депозит от {DEFAULT_MIN_DEPOSIT_RUB:g} ₽), плюс доход.\n\n'
        f'⏱ Часовой пояс расчёта: UTC{sign}{DEFAULT_TZ_OFFSET:g}\n\n'
        'Выберите период или задайте свой:'
    )


# ---------- Ядро отчёта ------------------------------------------------------


def _excluded_query(inner_sql):
    return text(inner_sql).bindparams(bindparam('excluded', expanding=True))


async def _run_report(db, start_utc, end_utc, min_kopeks, scope, sort):
    """Агрегат за период, отсортированный по `sort`.

    Сортировка данных не меняет, поэтому кешируем сам агрегат: смена сортировки,
    выгрузка CSV и оба вида билетов гоняли один и тот же запрос заново — на
    большой базе это заметно.
    """
    key = (start_utc.isoformat(), end_utc.isoformat(), min_kopeks, scope)
    cached = _cache_get(key)
    if cached is None:
        cached = await _collect(db, start_utc, end_utc, min_kopeks, scope)
        _cache_put(key, cached)
    referrers, detail = cached

    keys = SORT_KEYS.get(sort, SORT_KEYS[DEFAULT_SORT])
    ranking = sorted(referrers, key=lambda x: tuple(x[k] for k in keys), reverse=True)
    return ranking, detail


# Кеш агрегатов: ключ → (момент, (referrers, detail)). Записей мало и живут
# недолго — отчёт открывают сериями по одному периоду, а между сериями данные
# должны быть свежими.
_CACHE: dict = {}


def _cache_ttl() -> int:
    return config.get_int('PATCH_CONTEST_REPORT_CACHE_SECONDS', 120, minimum=0, maximum=3600)


def _cache_get(key):
    ttl = _cache_ttl()
    if ttl <= 0:
        return None
    item = _CACHE.get(key)
    if not item:
        return None
    born, value = item
    if monotonic() - born > ttl:
        _CACHE.pop(key, None)
        return None
    return value


def _cache_put(key, value) -> None:
    if _cache_ttl() <= 0:
        return
    now = monotonic()
    # Подчищаем протухшее и держим размер в узде: в кеше лежат полные списки
    # рефералов, и на годовом периоде это уже мегабайты.
    for stale in [k for k, (born, _) in _CACHE.items() if now - born > _cache_ttl()]:
        _CACHE.pop(stale, None)
    while len(_CACHE) >= CACHE_MAX_ENTRIES:
        _CACHE.pop(next(iter(_CACHE)))
    _CACHE[key] = (now, value)


def cache_clear() -> None:
    """Сбросить кеш. Нужен тестам и ручной отладке."""
    _CACHE.clear()


async def _collect(db, start_utc, end_utc, min_kopeks, scope):
    sqlite = settings.is_sqlite()
    params = {
        'start': _bind_dt(start_utc, sqlite),
        'end': _bind_dt(end_utc, sqlite),
        'completed': True,
        'min_kopeks': min_kopeks,
        'excluded': NON_REAL_METHODS or ['__none__'],
    }
    query = _excluded_query(
        """
        SELECT
            r.id AS referrer_id, r.telegram_id AS referrer_tg, r.username AS referrer_username,
            r.first_name AS referrer_first_name, r.last_name AS referrer_last_name,
            u.id AS referral_id, u.telegram_id AS referral_tg, u.username AS referral_username,
            u.created_at AS referral_created_at, fp.first_at AS first_purchase_at,
            COALESCE(rev.revenue, 0) AS revenue_kopeks
        FROM users u
        JOIN users r ON r.id = u.referred_by_id
        LEFT JOIN (
            SELECT t.user_id AS uid, MIN(t.created_at) AS first_at
            FROM transactions t
            WHERE t.is_completed = :completed AND t.type = 'deposit'
              AND t.amount_kopeks >= :min_kopeks
              AND t.payment_method IS NOT NULL AND t.payment_method NOT IN :excluded
            GROUP BY t.user_id
        ) fp ON fp.uid = u.id
        LEFT JOIN (
            SELECT t.user_id AS uid, SUM(t.amount_kopeks) AS revenue
            FROM transactions t
            WHERE t.is_completed = :completed AND t.type = 'deposit'
              AND t.payment_method IS NOT NULL AND t.payment_method NOT IN :excluded
              AND t.created_at >= :start AND t.created_at < :end
            GROUP BY t.user_id
        ) rev ON rev.uid = u.id
        WHERE u.referred_by_id IS NOT NULL
          AND u.created_at >= :start AND u.created_at < :end
        ORDER BY r.id, u.created_at
        """
    )
    rows = (await db.execute(query, params)).mappings().all()

    referrers = {}
    detail = []
    for row in rows:
        first_at = _to_aware(row['first_purchase_at'])
        if scope == 'period':
            converted = first_at is not None and start_utc <= first_at < end_utc
        else:
            converted = first_at is not None
        detail.append((row, converted, first_at))
        rid = row['referrer_id']
        agg = referrers.get(rid)
        if agg is None:
            agg = {
                'referrer_id': rid,
                'referrer_tg': row['referrer_tg'],
                'username': row['referrer_username'],
                'name': _display_name(
                    row['referrer_username'], row['referrer_first_name'], row['referrer_last_name'], row['referrer_tg']
                ),
                'invited': 0,
                'purchased': 0,
                'revenue_kopeks': 0,
            }
            referrers[rid] = agg
        agg['invited'] += 1
        if converted:
            agg['purchased'] += 1
        agg['revenue_kopeks'] += int(row['revenue_kopeks'] or 0)

    return list(referrers.values()), detail


def _build_csv(ranking, detail):
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(['=== СВОДКА ПО РЕФЕРЕРАМ ==='])
    w.writerow(['referrer_id', 'referrer_tg', 'username', 'name', 'invited', 'purchased', 'revenue_rub'])
    for r in ranking:
        w.writerow(
            [
                r['referrer_id'],
                r['referrer_tg'],
                r['username'] or '',
                r['name'],
                r['invited'],
                r['purchased'],
                round(r['revenue_kopeks'] / 100, 2),
            ]
        )
    w.writerow([])
    w.writerow(['=== ДЕТАЛИЗАЦИЯ (каждый реферал) ==='])
    w.writerow(
        [
            'referrer_id',
            'referrer_tg',
            'referral_id',
            'referral_tg',
            'referral_username',
            'referral_created_at_utc',
            'first_purchase_at_utc',
            'converted',
            'revenue_rub',
        ]
    )
    for row, converted, first_at in detail:
        created = _to_aware(row['referral_created_at'])
        w.writerow(
            [
                row['referrer_id'],
                row['referrer_tg'],
                row['referral_id'],
                row['referral_tg'],
                row['referral_username'],
                created.isoformat() if created else '',
                first_at.isoformat() if first_at else '',
                int(converted),
                round(int(row['revenue_kopeks'] or 0) / 100, 2),
            ]
        )
    return buf.getvalue().encode('utf-8-sig')


# ---------- История введённых вручную периодов -------------------------------
# Единственное, что патч пишет в базу: своя таблица с префиксом patch_, о
# которой alembic бота не знает. Нужна, чтобы не набирать один и тот же период
# заново — кнопки с ним появляются на экране «Свой период».

_periods_table_ready = False


async def _ensure_periods_table() -> None:
    global _periods_table_ready
    if _periods_table_ready:
        return

    from app.database.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                CREATE TABLE IF NOT EXISTS patch_contest_report_periods (
                    admin_id BIGINT NOT NULL,
                    key VARCHAR(16) NOT NULL,
                    raw VARCHAR(255) NOT NULL,
                    used_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (admin_id, key)
                )
            """)
        )
        await db.commit()

    _periods_table_ready = True


def _period_key(raw: str) -> str:
    return hashlib.sha1(raw.encode('utf-8')).hexdigest()[:16]


async def _remember_period(admin_id: int, raw: str) -> None:
    """Запомнить удачно разобранный ввод. Сбой записи молча игнорируем:
    история — удобство, из-за неё отчёт падать не должен."""
    if not admin_id or RECENT_PERIODS <= 0:
        return
    raw = raw.strip()[:255]
    if not raw:
        return

    try:
        await _ensure_periods_table()

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            await db.execute(
                text("""
                    INSERT INTO patch_contest_report_periods (admin_id, key, raw, used_at)
                    VALUES (:admin_id, :key, :raw, :used_at)
                    ON CONFLICT (admin_id, key) DO UPDATE
                        SET raw = EXCLUDED.raw, used_at = EXCLUDED.used_at
                """),
                {
                    'admin_id': admin_id,
                    'key': _period_key(raw),
                    'raw': raw,
                    'used_at': datetime.now(timezone.utc).replace(tzinfo=None),
                },
            )
            await db.commit()
    except Exception as error:  # noqa: BLE001
        logger.warning('contest_report: не удалось запомнить период', error=str(error))


async def _recent_periods(admin_id: int) -> list[tuple[str, str]]:
    """Последние введённые периоды как список (key, raw)."""
    if not admin_id or RECENT_PERIODS <= 0:
        return []
    try:
        await _ensure_periods_table()

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text("""
                    SELECT key, raw FROM patch_contest_report_periods
                    WHERE admin_id = :admin_id
                    ORDER BY used_at DESC
                    LIMIT :limit
                """),
                {'admin_id': admin_id, 'limit': RECENT_PERIODS},
            )
            return [(row[0], row[1]) for row in result.all()]
    except Exception as error:  # noqa: BLE001
        logger.warning('contest_report: не удалось прочитать историю периодов', error=str(error))
        return []


async def _period_by_key(admin_id: int, key: str) -> str | None:
    try:
        await _ensure_periods_table()

        from app.database.database import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                text('SELECT raw FROM patch_contest_report_periods WHERE admin_id = :a AND key = :k'),
                {'a': admin_id, 'k': key},
            )
            return result.scalar_one_or_none()
    except Exception as error:  # noqa: BLE001
        logger.warning('contest_report: не удалось найти период', error=str(error))
        return None


def _raffle_name(r) -> str:
    """Как участник записан в файле билетов.

    Раньше тех, у кого нет @username, просто выбрасывали — человек выигрывал
    конкурс, но в розыгрыше не участвовал. Теперь они идут по Telegram ID:
    победителя по нему находит и админка, и поиск в боте.
    """
    if r['username']:
        return '@' + r['username']
    if r['referrer_tg']:
        return f'id{r["referrer_tg"]}'
    return f'user{r["referrer_id"]}'


def _build_raffle(ranking, mode):
    """mode: paid|invited. Возвращает (текст_файла, билетов, участников, без_username)."""
    lines = []
    participants = 0
    without_username = 0
    for r in ranking:
        tickets = r['purchased'] if mode == 'paid' else r['invited']
        if tickets <= 0:
            continue
        if not r['username']:
            without_username += 1
        participants += 1
        lines.extend([_raffle_name(r)] * tickets)
    return '\n'.join(lines) + ('\n' if lines else ''), len(lines), participants, without_username


def _format_report_text(ranking, start_utc, end_utc, min_rub, scope, top, sort, label=None):
    total_invited = sum(r['invited'] for r in ranking)
    total_purchased = sum(r['purchased'] for r in ranking)
    total_revenue = sum(r['revenue_kopeks'] for r in ranking)
    head = f' ({label})' if label else ''
    lines = [
        f'📊 <b>Отчёт по рефералам{head}</b>',
        f'🗓 UTC: <code>{start_utc.strftime("%Y-%m-%d %H:%M")} → {end_utc.strftime("%Y-%m-%d %H:%M")}</code>',
        (
            f'⚙️ Мин. депозит: {min_rub:.0f} ₽ · конверсия: '
            f'{"внутри периода" if scope == "period" else "за всё время"} · '
            f'сортировка: {SORT_LABEL.get(sort, "")}'
        ),
        '',
        (
            f'👥 Рефереров: <b>{len(ranking)}</b> · Приглашено: <b>{total_invited}</b> · '
            f'Оплатили: <b>{total_purchased}</b> · Доход: <b>{settings.format_price(total_revenue)}</b>'
        ),
        '',
        f'<b>Топ-{min(top, len(ranking))}:</b>' if ranking else '— за период приглашённых нет.',
    ]
    for i, r in enumerate(ranking[:top], 1):
        lines.append(
            f'{i}. {html.escape(r["name"])} — '
            f'{r["invited"]} пригл. / {r["purchased"]} опл. / {settings.format_price(r["revenue_kopeks"])}'
        )
    tail = '\n…(полный список в CSV)'
    out = '\n'.join(lines)
    if len(out) <= 4096:
        return out
    # Режем по границе строк, а не по символам: обрыв посреди HTML-сущности
    # («&amp;» из экранированного имени) Telegram считает ошибкой разметки и
    # отказывается публиковать сообщение целиком.
    limit = 4096 - len(tail)
    kept = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > limit:
            break
        kept.append(line)
        used += len(line) + 1
    return '\n'.join(kept) + tail


async def _generate_and_send(
    target, db, admin_tg, state, start_utc, end_utc, min_kopeks, min_rub, scope, top, sort, label=None
):
    ranking, detail = await _run_report(db, start_utc, end_utc, min_kopeks, scope, sort)
    await state.update_data(
        cr={
            'start': start_utc.isoformat(),
            'end': end_utc.isoformat(),
            'min_kopeks': min_kopeks,
            'min_rub': min_rub,
            'scope': scope,
            'top': top,
            'sort': sort,
            'label': label or '',
        }
    )
    await target.answer(
        _format_report_text(ranking, start_utc, end_utc, min_rub, scope, top, sort, label),
        reply_markup=_report_kb(sort),
        parse_mode='HTML',
    )
    # CSV не отправляем сам: при переборе периодов и сортировок файлы сыпались
    # в чат пачками. Теперь он выгружается кнопкой «📄 Выгрузить CSV».
    logger.info(
        'contest_report generated',
        admin=admin_tg,
        referrers=len(ranking),
        invited=sum(r['invited'] for r in ranking),
        purchased=sum(r['purchased'] for r in ranking),
    )


def _load_cr(data):
    cr = data.get('cr')
    if not cr:
        return None
    cr = dict(cr)
    cr['start_utc'] = datetime.fromisoformat(cr['start'])
    cr['end_utc'] = datetime.fromisoformat(cr['end'])
    return cr


# ---------- Хендлеры: меню и отчёт -------------------------------------------


@admin_required
@error_handler
async def open_menu_callback(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    # Состояние сбрасываем обязательно. Экраны «свой период» и «статистика
    # пользователя» ставят FSM-состояние и ждут текст; уход отсюда кнопкой
    # оставлял его висеть, и следующее сообщение админа в боте — любое —
    # съедалось нашим обработчиком.
    await state.set_state(None)
    await callback.message.edit_text(_menu_text(), reply_markup=_menu_kb(), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def run_preset_callback(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    start_utc, end_utc, label = _preset_window(callback.data.split(':', 1)[1])
    await callback.answer('Считаю…')
    await _generate_and_send(
        callback.message,
        db,
        db_user.telegram_id,
        state,
        start_utc,
        end_utc,
        int(round(DEFAULT_MIN_DEPOSIT_RUB * 100)),
        DEFAULT_MIN_DEPOSIT_RUB,
        DEFAULT_SCOPE,
        DEFAULT_TOP,
        DEFAULT_SORT,
        label,
    )


@admin_required
@error_handler
async def ask_custom_callback(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(ContestReportStates.waiting_period)
    recent = await _recent_periods(db_user.telegram_id)
    hint = (
        '✏️ Пришлите период одним сообщением:\n\n'
        '<code>ГГГГ-ММ-ДД ГГГГ-ММ-ДД</code>\nнапример: <code>2026-06-01 2026-06-30</code>\n\n'
        'Доп. параметры: <code>min=100 tz=3 top=20 scope=period</code>'
    )
    if recent:
        hint += '\n\nИли повторите один из прошлых — кнопками ниже.'
    await callback.message.edit_text(hint, reply_markup=_cancel_kb(recent), parse_mode='HTML')
    await callback.answer()


@admin_required
@error_handler
async def recent_period_callback(
    callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext
):
    raw = await _period_by_key(db_user.telegram_id, callback.data.split(':', 1)[1])
    if not raw:
        await callback.answer('Этот период уже не сохранён, введите заново', show_alert=True)
        return

    ok, payload = _parse_command_args(raw)
    if not ok:
        await callback.answer(f'Не смог разобрать сохранённый период: {payload}', show_alert=True)
        return

    await state.set_state(None)
    await callback.answer('Считаю…')
    start_utc, end_utc, min_kopeks, min_rub, scope, top = payload
    await _remember_period(db_user.telegram_id, raw)
    await _generate_and_send(
        callback.message, db, db_user.telegram_id, state,
        start_utc, end_utc, min_kopeks, min_rub, scope, top, DEFAULT_SORT,
    )


@admin_required
@error_handler
async def process_custom_period(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(None)
    ok, payload = _parse_command_args(message.text or '')
    if not ok:
        await message.answer(f'⚠️ {payload}\n\n{USAGE}', parse_mode='HTML')
        return
    start_utc, end_utc, min_kopeks, min_rub, scope, top = payload
    await message.answer('⏳ Считаю…')
    await _remember_period(db_user.telegram_id, message.text or '')
    await _generate_and_send(
        message, db, db_user.telegram_id, state, start_utc, end_utc, min_kopeks, min_rub, scope, top, DEFAULT_SORT
    )


@admin_required
@error_handler
async def resort_callback(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    cr = _load_cr(await state.get_data())
    if not cr:
        await callback.answer('Отчёт устарел, откройте заново', show_alert=True)
        return
    sort = callback.data.split(':', 1)[1]
    ranking, _ = await _run_report(db, cr['start_utc'], cr['end_utc'], cr['min_kopeks'], cr['scope'], sort)
    await state.update_data(
        cr={
            'start': cr['start'],
            'end': cr['end'],
            'min_kopeks': cr['min_kopeks'],
            'min_rub': cr['min_rub'],
            'scope': cr['scope'],
            'top': cr['top'],
            'sort': sort,
            'label': cr.get('label', ''),
        }
    )
    try:
        await callback.message.edit_text(
            _format_report_text(
                ranking, cr['start_utc'], cr['end_utc'], cr['min_rub'], cr['scope'], cr['top'], sort, cr.get('label') or None
            ),
            reply_markup=_report_kb(sort),
            parse_mode='HTML',
        )
    except Exception as error:  # noqa: BLE001
        # Чаще всего это «message is not modified» при повторном нажатии той же
        # сортировки. Остальное не должно ронять хендлер, но и молчать не стоит.
        logger.debug('contest_report: не удалось обновить сообщение отчёта', error=str(error))
    await callback.answer(f'Сортировка: {SORT_LABEL.get(sort, "")}')


@admin_required
@error_handler
async def raffle_callback(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    cr = _load_cr(await state.get_data())
    if not cr:
        await callback.answer('Отчёт устарел, откройте заново', show_alert=True)
        return
    mode = callback.data.split(':', 1)[1]  # paid | invited
    await callback.answer('Готовлю билеты…')
    ranking, _ = await _run_report(db, cr['start_utc'], cr['end_utc'], cr['min_kopeks'], cr['scope'], 'paid')
    content, tickets, participants, without_username = _build_raffle(ranking, mode)
    if tickets == 0:
        await callback.message.answer('За этот период участников для розыгрыша нет.')
        return
    basis = 'с покупкой' if mode == 'paid' else 'приглашённых'
    fname = f'raffle_{mode}_{cr["start_utc"].strftime("%Y%m%d")}_{cr["end_utc"].strftime("%Y%m%d")}.txt'
    caption = (
        f'🎟 Билеты для розыгрыша (1 билет = 1 реферал {basis}).\n'
        f'Участников: {participants} · Билетов (строк): {tickets}'
    )
    if without_username:
        caption += f'\nБез @username — записаны по id: {without_username}'
    await callback.message.answer_document(
        types.BufferedInputFile(content.encode('utf-8'), filename=fname), caption=caption
    )


@admin_required
@error_handler
async def csv_callback(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    cr = _load_cr(await state.get_data())
    if not cr:
        await callback.answer('Отчёт устарел, откройте заново', show_alert=True)
        return
    await callback.answer('Готовлю файл…')
    ranking, detail = await _run_report(
        db, cr['start_utc'], cr['end_utc'], cr['min_kopeks'], cr['scope'], cr.get('sort', DEFAULT_SORT)
    )
    if not detail:
        await callback.message.answer('За этот период приглашённых нет — выгружать нечего.')
        return
    fname = f'contest_{cr["start_utc"].strftime("%Y%m%d")}_{cr["end_utc"].strftime("%Y%m%d")}.csv'
    await callback.message.answer_document(
        types.BufferedInputFile(_build_csv(ranking, detail), filename=fname),
        caption=(
            'Полный отчёт: сводка по реферерам + детализация по каждому рефералу.\n'
            f'Рефереров: {len(ranking)} · строк детализации: {len(detail)}'
        ),
    )


# ---------- Хендлеры: статистика пользователя --------------------------------


@admin_required
@error_handler
async def ask_userstats_callback(callback: types.CallbackQuery, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(ContestReportStates.waiting_user)
    await callback.message.edit_text(
        '📇 Пришлите пользователя одним сообщением:\n\n'
        '• <code>@username</code>\n• Telegram ID (число)\n\n'
        'Покажу его реферальную статистику за всё время.',
        reply_markup=_cancel_kb(),
        parse_mode='HTML',
    )
    await callback.answer()


@admin_required
@error_handler
async def process_userstats(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    await state.set_state(None)
    ident = (message.text or '').strip()

    if ident.isdigit():
        where = 'telegram_id = :val'
        val = int(ident)
    else:
        where = 'LOWER(username) = LOWER(:val)'
        val = ident.lstrip('@')

    user_row = (
        await db.execute(
            text(f"""SELECT id, telegram_id, username, first_name, last_name, created_at,
                            referred_by_id, has_made_first_topup
                     FROM users WHERE {where} LIMIT 1"""),
            {'val': val},
        )
    ).mappings().first()

    if not user_row:
        await message.answer('Пользователь не найден. Проверьте @username или Telegram ID.')
        return

    params = {
        'uid': user_row['id'],
        'completed': True,
        'min_kopeks': int(round(DEFAULT_MIN_DEPOSIT_RUB * 100)),
        'excluded': NON_REAL_METHODS or ['__none__'],
    }
    stats = (
        await db.execute(
            _excluded_query(
                """
        SELECT
            COUNT(*) AS invited,
            COALESCE(SUM(CASE WHEN fp.first_at IS NOT NULL THEN 1 ELSE 0 END), 0) AS purchased,
            COALESCE(SUM(COALESCE(rev.revenue, 0)), 0) AS revenue
        FROM users u
        LEFT JOIN (
            SELECT t.user_id AS uid, MIN(t.created_at) AS first_at FROM transactions t
            WHERE t.is_completed = :completed AND t.type = 'deposit'
              AND t.amount_kopeks >= :min_kopeks
              AND t.payment_method IS NOT NULL AND t.payment_method NOT IN :excluded
            GROUP BY t.user_id
        ) fp ON fp.uid = u.id
        LEFT JOIN (
            SELECT t.user_id AS uid, SUM(t.amount_kopeks) AS revenue FROM transactions t
            WHERE t.is_completed = :completed AND t.type = 'deposit'
              AND t.payment_method IS NOT NULL AND t.payment_method NOT IN :excluded
            GROUP BY t.user_id
        ) rev ON rev.uid = u.id
        WHERE u.referred_by_id = :uid
        """
            ),
            params,
        )
    ).mappings().first()

    referrer_line = '—'
    if user_row['referred_by_id']:
        rr = (
            await db.execute(
                text('SELECT username, telegram_id FROM users WHERE id = :id'),
                {'id': user_row['referred_by_id']},
            )
        ).mappings().first()
        if rr:
            referrer_line = ('@' + rr['username']) if rr['username'] else f'id{rr["telegram_id"]}'

    created = _to_aware(user_row['created_at'])
    name = _display_name(user_row['username'], user_row['first_name'], user_row['last_name'], user_row['telegram_id'])
    txt = (
        f'📇 <b>{html.escape(name)}</b>\n'
        f'Telegram ID: <code>{user_row["telegram_id"]}</code>\n'
        f'Регистрация: {created.strftime("%Y-%m-%d") if created else "—"}\n'
        f'Пригласил его: {html.escape(referrer_line)}\n'
        f'Сделал первое пополнение: {"да" if user_row["has_made_first_topup"] else "нет"}\n\n'
        f'<b>Как реферер (за всё время):</b>\n'
        f'• Приглашено: <b>{stats["invited"]}</b>\n'
        f'• Оплатили (депозит от {DEFAULT_MIN_DEPOSIT_RUB:g} ₽): <b>{stats["purchased"]}</b>\n'
        f'• Доход от рефералов: <b>{settings.format_price(int(stats["revenue"] or 0))}</b>'
    )
    await message.answer(txt, parse_mode='HTML')


# ---------- Команда ----------------------------------------------------------


@admin_required
@error_handler
async def cmd_contest_report(message: types.Message, db_user: User, db: AsyncSession, state: FSMContext):
    raw = (message.text or '').partition(' ')[2].strip()
    if not raw:
        await message.answer(_menu_text(), reply_markup=_menu_kb(), parse_mode='HTML')
        return
    ok, payload = _parse_command_args(raw)
    if not ok:
        await message.answer(f'⚠️ {payload}\n\n{USAGE}', parse_mode='HTML')
        return
    start_utc, end_utc, min_kopeks, min_rub, scope, top = payload
    await message.answer('⏳ Считаю…')
    await _remember_period(db_user.telegram_id, raw)
    await _generate_and_send(
        message, db, db_user.telegram_id, state, start_utc, end_utc, min_kopeks, min_rub, scope, top, DEFAULT_SORT
    )


def _parse_command_args(raw):
    # Границы периода разделяются пробелом, поэтому дата со временем
    # («2026-06-01 10:00») не поддерживается — только ГГГГ-ММ-ДД.
    tokens = raw.split()
    positional = [t for t in tokens if '=' not in t]
    opts = {}
    for t in tokens:
        if '=' in t:
            k, _, v = t.partition('=')
            opts[k.strip().lower()] = v.strip()
    if len(positional) < 2:
        return False, 'нужно указать начало и конец периода'
    try:
        tz_offset = float(opts.get('tz', DEFAULT_TZ_OFFSET))
        min_rub = float(opts.get('min', DEFAULT_MIN_DEPOSIT_RUB))
        top = int(opts.get('top', DEFAULT_TOP))
        scope = opts.get('scope', DEFAULT_SCOPE).lower()
        if scope not in ('period', 'all'):
            scope = DEFAULT_SCOPE
        start_utc = _parse_boundary(positional[0], tz_offset, is_end=False)
        end_utc = _parse_boundary(positional[1], tz_offset, is_end=True)
        if end_utc <= start_utc:
            return False, 'конец периода должен быть позже начала'
    except ValueError as exc:
        return False, f'ошибка в параметрах: {html.escape(str(exc))}'
    return True, (start_utc, end_utc, int(round(min_rub * 100)), min_rub, scope, top)


# ---------- Встраивание в бота -----------------------------------------------


def _admin_insert_at(rows) -> int:
    """Номер строки, на место которой встаёт кнопка конкурса.

    Сразу под «Промокоды/Статистика» — отчёт по рефералам туда и просится по
    смыслу. Ориентируемся на callback_data этой кнопки, а не на номер строки:
    состав админ-меню между версиями бота меняется. Не нашли — дописываем
    в конец, как раньше.
    """
    for index, row in enumerate(rows):
        for button in row:
            if getattr(button, 'callback_data', None) == ADMIN_ANCHOR_CALLBACK:
                return index + 1
    return len(rows)


def _patch_admin_keyboard() -> None:
    """Добавить кнопку в клавиатуру админ-панели.

    Патчим сам `app.keyboards.admin`, а не модуль-потребитель: админку рисуют
    два разных хендлера (главный и выход из режима техработ), каждый со своим
    `from ... import get_admin_main_keyboard`. Если патч успевает до их импорта
    — обёртку получают оба. Для уже импортированных модулей перепривязываем имя
    вручную, чтобы порядок загрузки патчей ничего не решал.
    """
    import app.keyboards.admin as kb_module

    original = kb_module.get_admin_main_keyboard
    if getattr(original, '_contest_report_wrapped', False):
        return

    def wrapped(*args, **kwargs):
        kb = original(*args, **kwargs)
        try:
            rows = list(kb.inline_keyboard)
            already = any(
                getattr(b, 'callback_data', None) == 'contest_report_menu' for row in rows for b in row
            )
            if not already:
                rows.insert(
                    _admin_insert_at(rows),
                    [InlineKeyboardButton(text=MENU_BUTTON_TEXT, callback_data='contest_report_menu')],
                )
            return InlineKeyboardMarkup(inline_keyboard=rows)
        except Exception as error:  # noqa: BLE001
            logger.warning('contest_report: не удалось дорисовать кнопку', error=str(error))
            return kb

    wrapped._contest_report_wrapped = True
    kb_module.get_admin_main_keyboard = wrapped

    rebound = 0
    for module in list(sys.modules.values()):
        if module is None or module is kb_module:
            continue
        if getattr(module, 'get_admin_main_keyboard', None) is original:
            module.get_admin_main_keyboard = wrapped
            rebound += 1

    logger.info('contest_report: кнопка добавлена в админ-панель', rebound_modules=rebound)


def _register_handlers(dp: Dispatcher) -> None:
    dp.callback_query.register(open_menu_callback, F.data == 'contest_report_menu')
    dp.callback_query.register(run_preset_callback, F.data.startswith('contest_report_run:'))
    dp.callback_query.register(ask_custom_callback, F.data == 'contest_report_custom')
    dp.callback_query.register(ask_userstats_callback, F.data == 'contest_report_userstats')
    dp.callback_query.register(resort_callback, F.data.startswith('crsort:'))
    dp.callback_query.register(raffle_callback, F.data.startswith('crraffle:'))
    dp.callback_query.register(csv_callback, F.data == 'crcsv')
    dp.callback_query.register(recent_period_callback, F.data.startswith('crperiod:'))
    dp.message.register(process_custom_period, ContestReportStates.waiting_period)
    dp.message.register(process_userstats, ContestReportStates.waiting_user)
    dp.message.register(cmd_contest_report, Command('contest_report'))
    logger.info('contest_report: хендлеры зарегистрированы')


def install() -> None:
    # Порядок важен: клавиатуру патчим ДО импорта app.bot, который тянет за
    # собой все хендлеры вместе с их `from app.keyboards.admin import ...`.
    _patch_admin_keyboard()

    import app.bot as bot_module

    if getattr(bot_module, '_contest_report_installed', False):
        return

    original_setup = bot_module.setup_bot

    async def setup_bot_with_contest_report():
        bot, dp = await original_setup()
        try:
            _register_handlers(dp)
        except Exception as error:  # noqa: BLE001
            logger.error('contest_report: не удалось зарегистрировать хендлеры', error=str(error))
        return bot, dp

    bot_module.setup_bot = setup_bot_with_contest_report
    bot_module._contest_report_installed = True
