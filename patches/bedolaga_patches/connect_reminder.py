"""Напоминание с инструкцией по подключению.

Если через N минут (по умолчанию 10) после создания подписки человек так и не
подключился к VPN ни разу, ему уходит ссылка на инструкцию: в Telegram, если он
пришёл из бота, или на почту, если регистрировался через кабинет.

Как встраивается
----------------
Патч оборачивает `monitoring_service.start_monitoring` — единственную точку,
которую main.py дёргает и при старте, и при перезапуске цикла. Обёртка
поднимает свою фоновую задачу и передаёт управление оригиналу. Файлы бота при
этом не меняются.

Факт подключения берём из панели: `firstConnectedAt` у панельного пользователя.
Это единственный источник, который отмечает именно установленное VPN-соединение,
а не отложенную синхронизацию трафика.

Состояние (кому уже отправляли) живёт в собственных таблицах
`patch_connect_reminders` и `patch_connect_reminders_state`. Alembic бота о них
не знает и при миграциях не трогает, поэтому история переживает обновления.
"""

import asyncio
import html
from datetime import datetime, timedelta, timezone

from . import config


__all__ = ['install']

STATE_SENT = 'sent'
STATE_CONNECTED = 'connected'
STATE_FAILED = 'failed'
STATE_NO_CHANNEL = 'no_channel'
STATE_NO_PANEL_USER = 'no_panel_user'

# Статусы подписок, при которых человеку реально есть что подключать.
ELIGIBLE_SUBSCRIPTION_STATUSES = ('active', 'trial', 'limited')

DEFAULT_URL = 'https://telegra.ph/Instrukciya-po-podklyucheniyu-INCY-na-Android-05-29'

MESSAGES = {
    'ru': {
        'subject': 'Инструкция AhriVPN',
        'body': 'Чтобы всё заработало, установите приложение и добавьте подписку по инструкции.',
        'button': '📖 Открыть инструкцию',
        'footer': 'Если что-то не получается — напишите в поддержку, поможем.',
    },
    'en': {
        'subject': 'AhriVPN Guide',
        'body': 'Install the app and add your subscription following the guide.',
        'button': '📖 Open the guide',
        'footer': 'If anything goes wrong, message support — we will help.',
    },
}

_task: asyncio.Task | None = None
_tables_ready = False


def _logger():
    try:
        import structlog

        return structlog.get_logger('bedolaga_patches.connect_reminder')
    except Exception:  # structlog обязан быть, но патч не должен падать из-за логгера
        import logging

        return logging.getLogger('bedolaga_patches.connect_reminder')


def _log(message: str, **kwargs) -> None:
    try:
        _logger().info(message, **kwargs)
    except Exception:
        print(f'[bedolaga-patches] {message} {kwargs}', flush=True)


def _log_error(message: str, **kwargs) -> None:
    try:
        _logger().error(message, **kwargs)
    except Exception:
        print(f'[bedolaga-patches] ERROR {message} {kwargs}', flush=True)


# --------------------------------------------------------------------- настройки


def _enabled() -> bool:
    return config.get_bool('PATCH_CONNECT_REMINDER_ENABLED', True)


def _delay_minutes() -> int:
    return config.get_int('PATCH_CONNECT_REMINDER_DELAY_MINUTES', 10, minimum=1)


def _max_age_hours() -> int:
    return config.get_int('PATCH_CONNECT_REMINDER_MAX_AGE_HOURS', 24, minimum=1)


def _interval_seconds() -> int:
    return config.get_int('PATCH_CONNECT_REMINDER_INTERVAL_SECONDS', 60, minimum=15)


def _batch_size() -> int:
    return config.get_int('PATCH_CONNECT_REMINDER_BATCH', 50, minimum=1, maximum=500)


def _startup_delay_seconds() -> int:
    # Даём боту доиграть alembic-миграции, прежде чем лезть в БД со своими CREATE TABLE.
    return config.get_int('PATCH_CONNECT_REMINDER_STARTUP_DELAY_SECONDS', 90, minimum=0)


def _instruction_url() -> str:
    return config.get_str('PATCH_CONNECT_REMINDER_URL', DEFAULT_URL)


def _backfill_enabled() -> bool:
    # По умолчанию выключено: при первой установке патча не рассылаем всем,
    # кто подписался за последние сутки.
    return config.get_bool('PATCH_CONNECT_REMINDER_BACKFILL', False)


def _once_per_subscription() -> bool:
    return config.get_str('PATCH_CONNECT_REMINDER_ONCE_PER', 'user').lower() == 'subscription'


# ------------------------------------------------------------------- подключение


def install() -> None:
    """Подцепиться к службе мониторинга.

    `monitoring_service` — единственная точка, которую main.py дёргает и при
    старте, и при перезапуске цикла после сбоя, поэтому наша задача живёт
    ровно столько же, сколько штатный мониторинг.
    """
    from app.services.monitoring_service import monitoring_service

    if getattr(monitoring_service, '_bedolaga_connect_reminder_installed', False):
        return

    original_start = monitoring_service.start_monitoring

    async def start_monitoring_with_reminder():
        _ensure_task()
        return await original_start()

    monitoring_service.start_monitoring = start_monitoring_with_reminder

    # Остановка — приятное дополнение, а не обязательное условие: без неё задача
    # просто доживёт до конца процесса. Поэтому если апстрим переименует метод,
    # патч всё равно должен установиться.
    original_stop = getattr(monitoring_service, 'stop_monitoring', None)
    if callable(original_stop):

        def stop_monitoring_with_reminder():
            _cancel_task()
            return original_stop()

        monitoring_service.stop_monitoring = stop_monitoring_with_reminder

    monitoring_service._bedolaga_connect_reminder_installed = True


def _cancel_task() -> None:
    global _task
    if _task is not None and not _task.done():
        _task.cancel()
    _task = None


def _ensure_task() -> None:
    global _task

    if not _enabled():
        _log('Патч connect_reminder выключен настройкой PATCH_CONNECT_REMINDER_ENABLED')
        return
    if _task is not None and not _task.done():
        return

    _task = asyncio.create_task(_loop())
    _log(
        'Патч connect_reminder активирован',
        delay_minutes=_delay_minutes(),
        interval_seconds=_interval_seconds(),
        url=_instruction_url(),
    )


async def _loop() -> None:
    await asyncio.sleep(_startup_delay_seconds())

    try:
        await _ensure_tables()
        await _ensure_floor()
    except Exception as error:
        _log_error('Не удалось подготовить таблицы патча connect_reminder', error=str(error))
        return

    while True:
        try:
            await _tick()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            _log_error('Ошибка в цикле connect_reminder', error=str(error))
        await asyncio.sleep(_interval_seconds())


# ------------------------------------------------------------------ своё хранилище


async def _ensure_tables() -> None:
    global _tables_ready
    if _tables_ready:
        return

    from sqlalchemy import text

    from app.database.database import AsyncSessionLocal

    async with AsyncSessionLocal() as db:
        await db.execute(
            text("""
                CREATE TABLE IF NOT EXISTS patch_connect_reminders (
                    scope VARCHAR(16) NOT NULL,
                    key_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    subscription_id INTEGER,
                    state VARCHAR(16) NOT NULL,
                    channel VARCHAR(16),
                    updated_at TIMESTAMP NOT NULL,
                    PRIMARY KEY (scope, key_id)
                )
            """)
        )
        await db.execute(
            text("""
                CREATE TABLE IF NOT EXISTS patch_connect_reminders_state (
                    key VARCHAR(64) NOT NULL PRIMARY KEY,
                    value VARCHAR(255) NOT NULL
                )
            """)
        )
        await db.commit()

    _tables_ready = True


async def _ensure_floor() -> None:
    """Запомнить момент первого запуска патча.

    Без этого при установке патча на живого бота рассылка ушла бы всем, кто
    подписался за последние MAX_AGE часов и не подключился. Порог отсекает
    прошлое; чтобы разослать и им, поставьте PATCH_CONNECT_REMINDER_BACKFILL=1.
    """
    from sqlalchemy import text

    from app.database.database import AsyncSessionLocal

    now = _utcnow()
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            text('SELECT value FROM patch_connect_reminders_state WHERE key = :key'),
            {'key': 'installed_at'},
        )
        if result.scalar_one_or_none() is not None:
            return
        await db.execute(
            text('INSERT INTO patch_connect_reminders_state (key, value) VALUES (:key, :value)'),
            {'key': 'installed_at', 'value': now.isoformat()},
        )
        await db.commit()

    _log('connect_reminder: зафиксирован момент установки', installed_at=now.isoformat())


async def _get_floor(db) -> datetime | None:
    if _backfill_enabled():
        return None

    from sqlalchemy import text

    result = await db.execute(
        text('SELECT value FROM patch_connect_reminders_state WHERE key = :key'),
        {'key': 'installed_at'},
    )
    raw = result.scalar_one_or_none()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _scope() -> str:
    """Единица дедупликации: 'user' — один раз на человека, 'subscription' — на подписку.

    Область хранится в самой строке, поэтому переключение настройки не путает
    старые записи с новыми: у них разный scope.
    """
    return 'subscription' if _once_per_subscription() else 'user'


async def _mark(db, user_id: int, subscription_id: int | None, state: str, channel: str | None) -> None:
    """Записать исход, чтобы больше не беспокоить этого человека (или эту подписку)."""
    from sqlalchemy import text

    scope = _scope()
    key_id = subscription_id if scope == 'subscription' else user_id
    if key_id is None:
        return

    params = {
        'scope': scope,
        'key_id': key_id,
        'user_id': user_id,
        'subscription_id': subscription_id,
        'state': state,
        'channel': channel,
        'updated_at': _utcnow().replace(tzinfo=None),
    }
    # ON CONFLICT есть и в PostgreSQL 9.5+, и в SQLite 3.24+ — оба поддерживаемых бэкенда.
    await db.execute(
        text("""
            INSERT INTO patch_connect_reminders
                (scope, key_id, user_id, subscription_id, state, channel, updated_at)
            VALUES (:scope, :key_id, :user_id, :subscription_id, :state, :channel, :updated_at)
            ON CONFLICT (scope, key_id) DO UPDATE
                SET user_id = EXCLUDED.user_id,
                    subscription_id = EXCLUDED.subscription_id,
                    state = EXCLUDED.state,
                    channel = EXCLUDED.channel,
                    updated_at = EXCLUDED.updated_at
        """),
        params,
    )
    await db.commit()


# ------------------------------------------------------------------- основной цикл


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _tick() -> None:
    candidates = await _collect_candidates()
    if not candidates:
        return

    _log('connect_reminder: кандидаты на напоминание', count=len(candidates))

    panel_ids = {row['remnawave_id'] for row in candidates if row['remnawave_id']}
    states = await _panel_states(panel_ids)
    if states is None:
        # Панель недоступна — молчим и пробуем на следующем тике. Отправить
        # инструкцию тому, кто уже подключился, хуже, чем опоздать на минуту.
        _log('connect_reminder: панель недоступна, пропускаю тик')
        return

    from app.database.database import AsyncSessionLocal

    bot = _get_bot()
    sent = 0

    async with AsyncSessionLocal() as db:
        for row in candidates:
            state = states.get(row['remnawave_id'])
            if state is None:
                # Панель не ответила именно по этому пользователю — перепроверим позже.
                continue
            if state == 'connected':
                await _mark(db, row['user_id'], row['subscription_id'], STATE_CONNECTED, None)
                continue
            if state == 'absent':
                await _mark(db, row['user_id'], row['subscription_id'], STATE_NO_PANEL_USER, None)
                continue

            state, channel = await _deliver(bot, row)
            if state is None:
                # Временный сбой: строку не пишем, повторим на следующем тике,
                # пока подписка не выпадет из окна MAX_AGE.
                continue

            await _mark(db, row['user_id'], row['subscription_id'], state, channel)
            if state == STATE_SENT:
                sent += 1
                await asyncio.sleep(0.1)  # мягкий троттлинг Telegram

    if sent:
        _log('connect_reminder: инструкция отправлена', sent=sent)


async def _collect_candidates() -> list[dict]:
    from sqlalchemy import column, select, table

    from app.database.database import AsyncSessionLocal
    from app.database.models import Subscription, User

    now = _utcnow()
    not_before = now - timedelta(hours=_max_age_hours())
    not_after = now - timedelta(minutes=_delay_minutes())

    reminders = table('patch_connect_reminders', column('scope'), column('key_id'))
    scope = _scope()
    key_column = Subscription.id if scope == 'subscription' else Subscription.user_id

    async with AsyncSessionLocal() as db:
        floor = await _get_floor(db)
        if floor is not None and floor > not_before:
            not_before = floor

        if not_before >= not_after:
            return []

        stmt = (
            select(
                Subscription.id,
                Subscription.user_id,
                Subscription.remnawave_id,
                User.telegram_id,
                User.email,
                User.email_verified,
                User.language,
            )
            .join(User, User.id == Subscription.user_id)
            .where(
                Subscription.created_at <= not_after,
                Subscription.created_at >= not_before,
                Subscription.remnawave_id.isnot(None),
                Subscription.status.in_(ELIGIBLE_SUBSCRIPTION_STATUSES),
                User.status == 'active',
                ~key_column.in_(select(reminders.c.key_id).where(reminders.c.scope == scope)),
            )
            .order_by(Subscription.created_at)
            .limit(_batch_size())
        )

        result = await db.execute(stmt)
        return [
            {
                'subscription_id': subscription_id,
                'user_id': user_id,
                'remnawave_id': remnawave_id,
                'telegram_id': telegram_id,
                'email': email,
                'email_verified': bool(email_verified),
                'language': language or 'ru',
            }
            for subscription_id, user_id, remnawave_id, telegram_id, email, email_verified, language in result.all()
        ]


async def _panel_states(panel_ids: set[int]) -> dict[int, str] | None:
    """Состояние панельных пользователей: 'connected' | 'absent' | 'fresh'.

    Ключевое: id, по которому запрос не удался, в словарь НЕ попадает. Такой
    кандидат пропускается без записи в таблицу и будет перепроверен на
    следующем тике — сетевой сбой не должен ни поднимать ложную рассылку, ни
    навсегда лишать человека напоминания.

    Возвращает None, если недоступна вся панель.
    """
    if not panel_ids:
        return {}

    from app.services.remnawave_service import RemnaWaveService

    service = RemnaWaveService()
    if not service.is_configured:
        _log_error('connect_reminder: панель не настроена', error=service.configuration_error)
        return None

    states: dict[int, str] = {}
    try:
        async with service.get_api_client() as api:
            for panel_id in panel_ids:
                try:
                    panel_user = await api.get_user_by_id(int(panel_id))
                except Exception as error:
                    _log_error(
                        'connect_reminder: не удалось получить пользователя панели',
                        panel_id=panel_id,
                        error=str(error),
                    )
                    continue

                if panel_user is None:
                    # Записи в панели нет — подключаться не к чему.
                    states[panel_id] = 'absent'
                elif panel_user.first_connected_at is not None:
                    states[panel_id] = 'connected'
                else:
                    states[panel_id] = 'fresh'
    except Exception as error:
        _log_error('connect_reminder: панель недоступна', error=str(error))
        return None

    return states


def _get_bot():
    try:
        from app.services.monitoring_service import monitoring_service

        return getattr(monitoring_service, 'bot', None)
    except Exception:
        return None


# ---------------------------------------------------------------------- доставка


async def _deliver(bot, row: dict) -> tuple[str | None, str | None]:
    """Отправить инструкцию. Возвращает (state, channel); state=None — повторить позже."""
    texts = MESSAGES.get(row['language'], MESSAGES['ru'])

    if row['telegram_id']:
        return await _send_telegram(bot, row['telegram_id'], texts)

    if row['email'] and row['email_verified']:
        return await _send_email(row['email'], row['language'], texts)

    return STATE_NO_CHANNEL, None


async def _send_telegram(bot, telegram_id: int, texts: dict) -> tuple[str | None, str | None]:
    if bot is None:
        _log_error('connect_reminder: экземпляр бота недоступен')
        return None, None

    url = _instruction_url()
    message = (
        f'{html.escape(texts["body"])}\n\n'
        f'<a href="{html.escape(url, quote=True)}">{html.escape(texts["button"])}</a>\n\n'
        f'<i>{html.escape(texts["footer"])}</i>'
    )

    markup = None
    try:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=texts['button'], url=url)]])
    except Exception:
        pass

    try:
        await bot.send_message(
            chat_id=telegram_id,
            text=message,
            parse_mode='HTML',
            reply_markup=markup,
            disable_web_page_preview=False,
        )
    except Exception as error:
        if _is_permanent_telegram_error(error):
            _log('connect_reminder: Telegram недоступен для пользователя', telegram_id=telegram_id, error=str(error))
            return STATE_FAILED, 'telegram'
        _log_error('connect_reminder: временная ошибка Telegram', telegram_id=telegram_id, error=str(error))
        return None, None

    return STATE_SENT, 'telegram'


def _is_permanent_telegram_error(error: Exception) -> bool:
    """Бот заблокирован / чата нет — повторять бессмысленно."""
    name = type(error).__name__
    if name in ('TelegramForbiddenError', 'TelegramNotFound', 'TelegramUnauthorizedError'):
        return True
    text = str(error).lower()
    return any(
        marker in text
        for marker in ('bot was blocked', 'user is deactivated', 'chat not found', 'bot can\'t initiate conversation')
    )


async def _send_email(email: str, language: str, texts: dict) -> tuple[str | None, str | None]:
    url = _instruction_url()
    body_html = _render_email_html(language, texts, url)

    def _send() -> bool:
        from app.cabinet.services.email_service import email_service

        return email_service.send_email(
            to_email=email,
            subject=texts['subject'],
            body_html=body_html,
        )

    try:
        delivered = await asyncio.to_thread(_send)
    except Exception as error:
        _log_error('connect_reminder: ошибка отправки письма', error=str(error))
        return None, None

    if not delivered:
        # send_email сам кладёт письмо в очередь ретраев при сбое SMTP; повторять
        # своими силами не нужно, иначе получится дубль.
        return STATE_FAILED, 'email'
    return STATE_SENT, 'email'


def _render_email_html(language: str, texts: dict, url: str) -> str:
    safe_url = html.escape(url, quote=True)
    content = (
        f'<p style="margin:0 0 16px; line-height:1.6;">{html.escape(texts["body"]).replace(chr(10), "<br>")}</p>'
        f'<p style="margin:0 0 24px;">'
        f'<a href="{safe_url}" style="display:inline-block; padding:12px 20px; border-radius:8px; '
        f'background:#2b7fff; color:#ffffff; text-decoration:none; font-weight:600;">'
        f'{html.escape(texts["button"])}</a></p>'
        f'<p style="margin:0; color:#6b7280; font-size:14px;">{html.escape(texts["footer"])}</p>'
    )

    # Пробуем завернуть в фирменную обёртку кабинета; если её API изменился —
    # уходит тот же текст без обёртки, письмо всё равно доставляется.
    try:
        from app.cabinet.services.email_layout import render_email_layout, resolve_email_layout

        layout = resolve_email_layout(language)
        return render_email_layout(layout, language, {'content': content, 'subject': texts['subject']})
    except Exception:
        return f'<html><body style="font-family:Arial,Helvetica,sans-serif;">{content}</body></html>'
