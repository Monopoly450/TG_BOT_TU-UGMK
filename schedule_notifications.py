"""Retryable daily Telegram delivery, independent of the server's local timezone."""
import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

logger = logging.getLogger(__name__)
LOCAL_TZ = timezone(timedelta(hours=5))
DAYS = ['Понедельник', 'Вторник', 'Среда', 'Четверг', 'Пятница', 'Суббота', 'Воскресенье']
CATCHUP = timedelta(minutes=30)
RELEASE = "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) end return 0"


def notification_due(value, now):
    if not isinstance(value, str) or not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', value):
        return False
    hour, minute = map(int, value.split(':'))
    scheduled = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return timedelta(0) <= now - scheduled <= CATCHUP


def message_parts(text):
    if len(text.encode('utf-16-le')) // 2 <= 4000:
        return [(text, 'HTML')]
    # Keep unusually long days deliverable without breaking HTML tags/entities.
    class PlainText(HTMLParser):
        def __init__(self):
            super().__init__()
            self.parts = []

        def handle_data(self, data):
            self.parts.append(data)

    parser = PlainText()
    parser.feed(text)
    plain = ''.join(parser.parts)
    return [(plain[i:i + 1800], None) for i in range(0, len(plain), 1800)]


class ScheduleNotifications:
    def __init__(self, redis, schedule_manager, bot, formatter):
        self.redis = redis
        self.schedule_manager = schedule_manager
        self.bot = bot
        self.formatter = formatter
        self.fetch_slots = asyncio.Semaphore(4)
        self.send_lock = asyncio.Lock()

    async def run(self, now=None):
        now = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
        subs, mornings, evenings = await asyncio.gather(
            self.redis.hgetall('user_subs'),
            self.redis.hgetall('user_morning_time'),
            self.redis.hgetall('user_evening_time'),
        )
        groups = {}
        for uid, group in subs.items():
            for kind, value in [('morning', mornings.get(uid, '08:00')), ('evening', evenings.get(uid))]:
                if notification_due(value, now):
                    key = f'schedule:delivery:{now.date()}:{kind}:{uid}'
                    if not await self.redis.exists(key):
                        groups.setdefault((group, kind), []).append((uid, key))
        results = await asyncio.gather(
            *(self.deliver_group(group, kind, recipients, now)
              for (group, kind), recipients in groups.items()),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, Exception):
                logger.error('Schedule delivery failed; retry on next tick: %s', result)

    async def deliver_group(self, group, kind, recipients, now):
        target = now.date() + timedelta(days=kind == 'evening')
        monday = now.date() - timedelta(days=now.weekday())
        week = (target - monday).days // 7
        async with self.fetch_slots:
            schedule = await asyncio.wait_for(self.schedule_manager.fetch_schedule(week, 'group', group), 75)
        if not schedule or '_error' in schedule or '_pending' in schedule:
            logger.warning('Schedule unavailable for %s (%s); delivery will retry', group, kind)
            return
        if schedule.get('_group', group) != group:
            logger.error('Refusing notification for mismatched group %s', group)
            return
        title = 'на завтра' if kind == 'evening' else 'на сегодня'
        text = f'🔔 <b>Расписание {title}, {target:%d.%m}:</b>\n\n'
        text += await self.formatter(target, schedule.get(DAYS[target.weekday()], []), group)
        parts = message_parts(text)
        for uid, key in recipients:
            try:
                await self.deliver_user(uid, key, parts)
            except Exception as exc:
                logger.warning('Schedule notification to %s failed; will retry: %s', uid, exc)

    async def deliver_user(self, uid, key, parts):
        # The lease also protects against a second scheduler during a restart.
        token = uuid.uuid4().hex
        async with self.send_lock:
            if await self.redis.exists(key):
                return
            if not await self.redis.set(key + ':lock', token, ex=120, nx=True):
                return
            try:
                async with asyncio.timeout(90):
                    for index, (text, mode) in enumerate(parts):
                        part_key = f'{key}:part:{index}'
                        if await self.redis.exists(part_key):
                            continue
                        try:
                            await self.bot.send_message(int(uid), text, parse_mode=mode, disable_notification=False)
                        except TelegramRetryAfter as exc:
                            await asyncio.sleep(exc.retry_after)
                            await self.bot.send_message(int(uid), text, parse_mode=mode, disable_notification=False)
                        await self.redis.set(part_key, 'sent', ex=172800)
                        await asyncio.sleep(.05)
                    await self.redis.set(key, 'sent', ex=172800)
            except TelegramForbiddenError:
                # A blocked bot cannot deliver; do not hammer Telegram every tick.
                await self.redis.set(key, 'blocked', ex=172800)
                logger.info('Schedule notification unavailable: bot blocked by %s', uid)
            finally:
                await self.redis.eval(RELEASE, 1, key + ':lock', token)
