import asyncio
import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from schedule_notifications import LOCAL_TZ, ScheduleNotifications, message_parts, notification_due


class MemoryRedis:
    def __init__(self):
        self.values = {}
        self.hashes = {'user_subs': {'1': 'Ит-24107'}, 'user_morning_time': {}, 'user_evening_time': {}}

    async def hgetall(self, key):
        return self.hashes[key]

    async def exists(self, key):
        return key in self.values

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def eval(self, script, count, key, token):
        if self.values.get(key) == token:
            del self.values[key]


class NotificationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.redis = MemoryRedis()
        self.bot = SimpleNamespace(send_message=AsyncMock())
        self.sm = SimpleNamespace(fetch_schedule=AsyncMock(return_value={'Понедельник': [{'subject': 'Алгебра'}]}))
        self.fmt = AsyncMock(return_value='<b>Алгебра</b>')
        self.service = ScheduleNotifications(self.redis, self.sm, self.bot, self.fmt)
        self.now = datetime(2026, 9, 7, 8, 2, tzinfo=LOCAL_TZ)

    async def test_skipped_minute_and_restart_are_delivered_once(self):
        await self.service.run(self.now)
        restarted = ScheduleNotifications(self.redis, self.sm, self.bot, self.fmt)
        await restarted.run(self.now + timedelta(minutes=1))
        self.bot.send_message.assert_awaited_once()
        self.assertFalse(self.bot.send_message.call_args.kwargs['disable_notification'])

    async def test_failed_fetch_and_failed_send_retry(self):
        self.sm.fetch_schedule.return_value = {'_error': 'Portal unavailable'}
        await self.service.run(self.now)
        self.bot.send_message.assert_not_awaited()
        self.sm.fetch_schedule.return_value = {'Понедельник': []}
        self.bot.send_message.side_effect = [RuntimeError('offline'), None]
        await self.service.run(self.now)
        await self.service.run(self.now + timedelta(minutes=1))
        await self.service.run(self.now + timedelta(minutes=2))
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_sunday_evening_uses_next_week_and_reports_empty_day(self):
        self.redis.hashes['user_evening_time'] = {'1': '20:00'}
        self.sm.fetch_schedule.return_value = {'Понедельник': []}
        now = datetime(2026, 9, 6, 20, 0, tzinfo=LOCAL_TZ)
        await self.service.run(now)
        self.sm.fetch_schedule.assert_awaited_once_with(1, 'group', 'Ит-24107')
        self.assertEqual(self.fmt.call_args.args[:2], ((now + timedelta(days=1)).date(), []))
        self.bot.send_message.assert_awaited_once()

    async def test_disabled_and_outside_catchup_do_not_send(self):
        self.redis.hashes['user_morning_time'] = {'1': 'Отключено'}
        await self.service.run(self.now)
        self.redis.hashes['user_morning_time'] = {'1': '07:00'}
        await self.service.run(self.now)
        self.bot.send_message.assert_not_awaited()
        for value in ['25:00', '8:00', '', None]:
            self.assertFalse(notification_due(value, self.now))
        self.assertFalse(notification_due('08:03', self.now))

    async def test_two_schedulers_do_not_duplicate_delivery(self):
        other = ScheduleNotifications(self.redis, self.sm, self.bot, self.fmt)
        await asyncio.gather(self.service.run(self.now), other.run(self.now))
        self.bot.send_message.assert_awaited_once()

    async def test_group_fetched_once_for_multiple_subscribers(self):
        self.redis.hashes['user_subs']['2'] = 'Ит-24107'
        await self.service.run(self.now)
        self.sm.fetch_schedule.assert_awaited_once()
        self.assertEqual(self.bot.send_message.await_count, 2)

    async def test_long_day_retry_does_not_repeat_successful_parts(self):
        self.fmt.return_value = '<b>' + 'Пара 📚 &amp; ' * 600 + '</b>'
        self.bot.send_message.side_effect = [None, RuntimeError('network')]
        await self.service.run(self.now)
        first_text = self.bot.send_message.await_args_list[0].args[1]
        self.bot.send_message.reset_mock(side_effect=True)
        await self.service.run(self.now)
        self.assertGreater(self.bot.send_message.await_count, 1)
        self.assertNotEqual(self.bot.send_message.await_args_list[0].args[1], first_text)
        for call in self.bot.send_message.await_args_list:
            self.assertLessEqual(len(call.args[1].encode('utf-16-le')) // 2, 4096)
            self.assertIsNone(call.kwargs['parse_mode'])

    def test_short_html_preserved_long_html_converted_without_entities(self):
        self.assertEqual(message_parts('<b>Пара</b>'), [('<b>Пара</b>', 'HTML')])
        parts = message_parts('<b>' + '&lt; 📚 ' * 1000 + '</b>')
        self.assertEqual(''.join(text for text, _ in parts), '< 📚 ' * 1000)
