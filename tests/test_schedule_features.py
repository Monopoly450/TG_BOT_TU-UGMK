import os
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse, unquote

os.environ.setdefault('LOGIN', 'offline-test')
os.environ.setdefault('PASSWORD', 'offline-test')
from schedule_config import GROUPS_DB, canonical_group, active_group, merged_groups, migrate_group_preferences
from scraper import ScheduleParser
import dashboard as api


class GroupCatalogTests(unittest.TestCase):
    def test_retired_cohorts_removed_and_it_subgroups_normalized(self):
        for name in ['Ит-22107', 'Ит-23107', 'А-23101', 'Гд-22104']:
            self.assertFalse(active_group(name))
        self.assertTrue(all(active_group(g) for g in GROUPS_DB))
        self.assertEqual(canonical_group('ИТ-24107 гр. 3'), 'Ит-24107')
        self.assertEqual([g for g in GROUPS_DB if '24107' in g], ['Ит-24107'])

    def test_discovered_ids_take_precedence_except_explicit_it_id(self):
        groups = merged_groups({'А-25101': 'new-live-id', 'Ит-24107 гр. 1': 'old', 'Ит-23107': 'retired'})
        self.assertEqual(groups['А-25101'], 'new-live-id')
        self.assertEqual(groups['Ит-24107'], GROUPS_DB['Ит-24107'])
        self.assertNotIn('Ит-23107', groups)

    def test_composite_object_id_is_encoded_exactly_once(self):
        parser = ScheduleParser()
        for g, oid in GROUPS_DB.items():
            query = parse_qs(urlparse(parser._build_url(1, 'group', g, oid)).query)
            self.assertEqual(query['objectId'], [unquote(oid)])
            self.assertEqual(query['another_group'], [g])

    def test_parser_rejects_foreign_group_for_both_layouts(self):
        row = '<table><tr><td>08:30</td><td>Алгебра</td><td>201</td><td>Ит-24107</td><td>Преподаватель</td></tr></table>'
        for html in [row, '<div class="day-container"><h3>Понедельник 07.09.2026</h3>'+row+'</div>']:
            parser = ScheduleParser()
            self.assertIn('_error', parser._parse(html, 'group', 'А-25101'))
            correct = parser._parse(html.replace('Ит-24107', 'А-25101'), 'group', 'А-25101')
            self.assertNotIn('_error', correct)
            self.assertEqual(correct['Понедельник'][0]['group'], 'А-25101')


class GroupApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_notification_preferences_accept_custom_time_and_off(self):
        request = SimpleNamespace(json=AsyncMock(return_value={'uid': 1, 'init_data': 'signed', 'morning_time': '08:17', 'evening_time': 'Отключено'}))
        with patch.object(api, 'verify_telegram_init_data', return_value={'id': 1}), patch.object(api.dao, 'hset', AsyncMock()) as save:
            await api.api_set_notifications(request)
            save.assert_any_await('user_morning_time', '1', '08:17')
            save.assert_any_await('user_evening_time', '1', 'Отключено')

    async def test_invalid_notification_time_does_not_partially_save(self):
        request = SimpleNamespace(json=AsyncMock(return_value={'uid': 1, 'init_data': 'signed', 'morning_time': '08:00', 'evening_time': '25:99'}))
        with patch.object(api, 'verify_telegram_init_data', return_value={'id': 1}), patch.object(api.dao, 'hset', AsyncMock()) as save:
            with self.assertRaises(api.HTTPException) as error:
                await api.api_set_notifications(request)
            self.assertEqual(error.exception.status_code, 400)
            save.assert_not_awaited()

    async def test_api_returns_requested_group_and_rejects_retired(self):
        with patch.object(api, 'verify_telegram_init_data', return_value={'id': 1}), patch.object(api.sm, 'fetch_schedule', AsyncMock(return_value={'_group':'Ит-24107'})) as fetch:
            result = await api.api_schedule(1,1,'signed',target_name='ИТ-24107 гр. 2')
            self.assertEqual(result['group_name'], 'Ит-24107')
            fetch.assert_awaited_once_with(1,'group','Ит-24107')
            with self.assertRaises(api.HTTPException):
                await api.api_schedule(1,1,'signed',target_name='Ит-23107')

    async def test_pending_schedule_is_not_reported_as_empty_and_jobs_deduplicated(self):
        redis = SimpleNamespace(exists=AsyncMock(return_value=False), set=AsyncMock(return_value=False), lpush=AsyncMock())
        with patch.object(api,'dao',redis), patch.object(api.asyncio,'sleep',AsyncMock()):
            result = await api.ScheduleManager().fetch_schedule(1,'group','А-25101')
        self.assertEqual(result, {'_pending':True})
        redis.lpush.assert_not_called()

    async def test_migration_preserves_current_users_and_normalizes_preferences(self):
        redis = MagicMock()
        redis.hgetall = AsyncMock(side_effect=lambda key: {'user_subs': {'1':'Ит-24107 гр. 2','2':'Ит-23107','3':'А-25101'}, 'starosta_group_saved':{'1':'Ит-24107 гр. 1'},'db_groups':{'Ит-23107':'old','Ит-24107 гр. 1':'old'}}.get(key,{}))
        for name in ['hset','hdel','srem','sadd']: setattr(redis,name,AsyncMock())
        redis.smembers = AsyncMock(return_value={'group:Ит-24107 гр. 2','group:Ит-23107','group:А-25101'})
        async def scan(**kwargs): yield 'favs:1'
        redis.scan_iter = scan
        conn = SimpleNamespace(fetch=AsyncMock(return_value=[{'telegram_id':1,'group_name':'Ит-24107 гр. 2'},{'telegram_id':2,'group_name':'Ит-23107'},{'telegram_id':3,'group_name':'А-25101'}]), execute=AsyncMock())
        db = MagicMock();db.pool.acquire.return_value.__aenter__=AsyncMock(return_value=conn)
        await migrate_group_preferences(redis,db)
        redis.hset.assert_any_await('user_subs','1','Ит-24107')
        redis.hdel.assert_any_await('user_subs','2')
        redis.sadd.assert_awaited_once_with('favs:1','group:Ит-24107')
        self.assertEqual(conn.execute.await_count,2)
        self.assertEqual(conn.execute.await_args_list[1].args[1:],(2,None))


class LauncherTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        with patch.dict(os.environ, {'BOT_TOKEN':'12345:offline-test-token','PROXY_URL':''}), patch('secure_store.SecureStore'):
            import bot
        self.module = bot

    async def test_restart_never_sends_service_messages(self):
        b = self.module
        for updating in ['1', None]:
            redis = SimpleNamespace(hgetall=AsyncMock(return_value={}), get=AsyncMock(return_value=updating), delete=AsyncMock())
            with patch.object(b, 'dao', redis), patch.object(b, 'broadcast', AsyncMock()) as broadcast, patch.object(b.bot, 'send_message', AsyncMock()) as send:
                await b.initialize_on_startup()
                broadcast.assert_not_awaited()
                send.assert_not_awaited()

    async def test_schedule_text_escapes_portal_and_homework_markup(self):
        b = self.module
        with patch.object(b.dao, 'hget', AsyncMock(return_value='x < y & z')):
            text = await b.format_lesson({'subject': 'C++ <основы>', 'teacher': 'А & Б', 'time': '08:30'}, 'Понедельник', 'Ит-24107')
        self.assertIn('C++ &lt;основы&gt;', text)
        self.assertIn('А &amp; Б', text)
        self.assertIn('x &lt; y &amp; z', text)

    async def test_old_group_buttons_use_visible_name_instead_of_shifted_index(self):
        b=self.module
        for data in ['fsel:group:0','fsel:group:А-25101']:
            button=SimpleNamespace(text='А-25101',callback_data=data)
            message=SimpleNamespace(delete=AsyncMock(),answer=AsyncMock(),reply_markup=SimpleNamespace(inline_keyboard=[[button]]))
            callback=SimpleNamespace(data=data,message=message,answer=AsyncMock(),from_user=SimpleNamespace(id=1,username='test'))
            with patch.object(b,'get_groups_db',AsyncMock(return_value=GROUPS_DB)),patch.object(b,'dao',SimpleNamespace(hset=AsyncMock())) as redis,patch.object(b.db_manager,'register_or_update_user',AsyncMock()),patch.object(b,'show_subscription_time_menu',AsyncMock()):
                await b.cb_sel(callback,MagicMock())
                redis.hset.assert_awaited_once_with('user_subs','1','А-25101')

    async def test_main_menu_message_creates_launcher(self):
        b=self.module
        result=b.Message(message_id=7,date=datetime.now(timezone.utc),chat={'id':1,'type':'private'})
        method=SimpleNamespace(reply_markup=b.get_main_menu())
        with patch.object(b,'track_message',AsyncMock()),patch.object(b,'send_app_launcher',AsyncMock()) as launcher:
            await b.OutgoingMessageTracker()(AsyncMock(return_value=result),MagicMock(),method)
            launcher.assert_awaited_once()

    async def test_launcher_replaces_previous_and_cleanup_keeps_current(self):
        b=self.module
        redis=SimpleNamespace(get=AsyncMock(return_value='8'),set=AsyncMock(),smembers=AsyncMock(return_value={'7','8','9'}),delete=AsyncMock(),sadd=AsyncMock())
        client=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),delete_message=AsyncMock(),delete_messages=AsyncMock())
        with patch.object(b,'dao',redis),patch.object(b,'bot',client),patch.object(b,'WEBAPP_URL','https://example.com'):
            await b.send_app_launcher(client,1)
            client.delete_message.assert_awaited_once_with(1,8)
            self.assertEqual(client.send_message.call_args.kwargs['reply_markup'].inline_keyboard[0][0].web_app.url,'https://example.com/webapp')
            await b.clear_chat_history(1)
            deleted=client.delete_messages.call_args.args[1]
            self.assertNotIn(8,deleted)
            self.assertEqual(set(deleted),{7,9})
