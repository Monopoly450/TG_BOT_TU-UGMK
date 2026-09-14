import os
import json
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch, mock_open
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


class SchedulePanelTests(unittest.IsolatedAsyncioTestCase):
    notice = '<div class="alert alert-warning">Для заданных параметров данные не могут быть предоставлены.</div>'

    def test_portal_notice_is_terminal_without_claiming_no_classes(self):
        pages = [self.notice, self.page(self.notice),
                 self.page('').replace('<option value="selected">Эк-25109</option>', '') + self.notice,
                 self.page('').replace('id="selected"', 'id="missing"') + self.notice]
        for html in pages:
            with self.subTest(html=html):
                self.assertEqual(ScheduleParser()._parse(html, 'group', 'Эк-25109'), {'_unavailable': True})

    def test_hidden_foreign_notice_does_not_override_selected_schedule(self):
        html = self.page(self.day('Эк-25109')).replace(self.day('Ит-24107', 'Вторник'), self.notice)
        result = ScheduleParser()._parse(html, 'group', 'Эк-25109')
        self.assertEqual(result['Понедельник'][0]['group'], 'Эк-25109')
        # A notice in the account's hidden block cannot excuse a missing group.
        self.assertIn('_error', ScheduleParser()._parse(html, 'group', 'Ит-26107'))

    @staticmethod
    def day(group, name='Понедельник'):
        return f'<div class="day-container"><h3>{name} 21.09.2026</h3><table><tr><td>08:30</td><td>Алгебра</td><td>201</td><td>{group}</td><td>Преподаватель</td></tr></table></div>'

    def page(self, selected, *, own_last=False):
        selector = '<select id="group-select"><option value="own">Ит-24107</option><option value="selected">Эк-25109</option></select>'
        own = f'<div id="own" class="schedule-container">{self.day("Ит-24107", "Вторник")}</div>'
        target = f'<div id="selected" class="schedule-container active">{selected}</div>'
        return selector + (target + own if own_last else own + target)

    def test_empty_selected_week_does_not_use_hidden_own_schedule(self):
        for empty in ['', '<p>Расписание не найдено</p>']:
            self.assertEqual(ScheduleParser()._parse(self.page(empty), 'group', 'Эк-25109'), {'_empty': True})

    def test_partial_selected_week_ignores_other_days_and_dom_order(self):
        for own_last in [False, True]:
            result = ScheduleParser()._parse(self.page(self.day('Эк-25109'), own_last=own_last), 'group', 'Эк-25109')
            self.assertNotIn('_error', result)
            self.assertNotIn('Вторник', result)
            self.assertEqual(result['Понедельник'][0]['group'], 'Эк-25109')

    def test_foreign_selected_rows_still_rejected(self):
        result = ScheduleParser()._parse(self.page(self.day('Ит-24107')), 'group', 'Эк-25109')
        self.assertIn('_error', result)

    def test_missing_group_or_panel_is_not_an_empty_week(self):
        for html in [self.page(''), self.page('').replace('id="selected"', 'id="missing"')]:
            group = 'А-26101' if 'id="selected"' in html else 'Эк-25109'
            self.assertIn('_error', ScheduleParser()._parse(html, 'group', group))

    def test_legacy_tables_stay_inside_selected_panel(self):
        table = '<table><tr><td>08:30</td><td>Алгебра</td><td>201</td><td>Эк-25109</td><td>Преподаватель</td></tr></table>'
        result = ScheduleParser()._parse(self.page(table), 'group', 'Эк-25109')
        self.assertNotIn('_error', result)
        self.assertEqual(result['Понедельник'][0]['group'], 'Эк-25109')

    def test_active_panel_without_selector_and_unknown_html(self):
        html = self.page('').split('</select>', 1)[1]
        self.assertEqual(ScheduleParser()._parse(html, 'group', 'Эк-25109'), {'_empty': True})
        result = ScheduleParser()._parse(self.page('<p>Сервис временно недоступен</p>'), 'group', 'Эк-25109')
        self.assertNotIn('_empty', result)

    async def test_fetch_preserves_explicit_empty_week_and_rejects_http_error(self):
        parser = ScheduleParser()
        parser.get_entity_id = AsyncMock(return_value='test-id')
        parser.discover_entities = AsyncMock()
        parser.page = SimpleNamespace(url='https://portal.test/student/schedule', goto=AsyncMock(return_value=SimpleNamespace(status=200)),
            wait_for_selector=AsyncMock(), title=AsyncMock(return_value='Портал'), content=AsyncMock(return_value=self.page('')))
        with patch('builtins.open', mock_open()), patch('scraper.asyncio.sleep', AsyncMock()):
            self.assertEqual(await parser.fetch(1, 'group', 'Эк-25109'), {'_empty': True, '_group': 'Эк-25109'})
            parser.page.content.return_value = self.notice
            self.assertEqual(await parser.fetch(1, 'group', 'Эк-25109'), {'_unavailable': True, '_group': 'Эк-25109'})
            parser.page.goto.return_value.status = 403
            parser.page.content.reset_mock()
            result = await parser.fetch(1, 'group', 'Эк-25109')
            self.assertIn('HTTP 403', result['_error'])
            parser.page.content.assert_not_awaited()


class GroupApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_cached_unavailable_is_returned_without_polling_or_queueing(self):
        result = {'_unavailable': True, '_group': 'Ит-26107'}
        redis = SimpleNamespace(exists=AsyncMock(return_value=True), get=AsyncMock(return_value=json.dumps(result)),
                                set=AsyncMock(), lpush=AsyncMock())
        with patch.object(api, 'dao', redis):
            for _ in range(2):
                self.assertEqual(await api.ScheduleManager().fetch_schedule(1, 'group', 'Ит-26107'), result)
        redis.set.assert_not_awaited()
        redis.lpush.assert_not_awaited()

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
    async def test_week_formatter_distinguishes_unavailable_from_empty(self):
        text = await self.module.fmt_week({'_unavailable': True}, 'Ит-26107')
        self.assertIn('Портал пока не предоставляет', text)
        self.assertNotIn('занятий нет', text)

    async def test_author_draft_slash_cannot_be_parsed_as_username_path_on_mac(self):
        b = self.module
        message = SimpleNamespace(answer=AsyncMock())
        await b.show_author(message)
        url = message.answer.call_args.kwargs['reply_markup'].inline_keyboard[0][0].url
        self.assertEqual(urlparse(url).path, '/mopoly_rio')
        self.assertNotIn('/', urlparse(url).query)
        self.assertIn('%2F', url)
        self.assertEqual(parse_qs(urlparse(url).query)['text'], [b.AUTHOR_DRAFT])

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

    async def test_main_menu_message_does_not_send_extra_launcher(self):
        b=self.module
        result=b.Message(message_id=7,date=datetime.now(timezone.utc),chat={'id':1,'type':'private'})
        method=SimpleNamespace(reply_markup=b.get_main_menu())
        client = SimpleNamespace(send_message=AsyncMock())
        send = AsyncMock(return_value=result)
        with patch.object(b,'track_message',AsyncMock()) as track:
            self.assertIs(await b.OutgoingMessageTracker()(send,client,method), result)
            send.assert_awaited_once_with(client, method)
            track.assert_awaited_once_with(1, 7)
            client.send_message.assert_not_awaited()

    def test_reply_menus_launch_via_main_app_bridge_without_text(self):
        b = self.module
        for menu in [b.get_main_menu(), b.get_main_menu('group'), b.get_submenu_keyboard()]:
            self.assertFalse(menu.is_persistent)
            self.assertEqual(len(menu.keyboard[0]), 1)
            button = menu.keyboard[0][0]
            self.assertEqual(button.text, '🎓 ТУ УГМК · Кампус')
            self.assertEqual(button.model_dump()['style'], 'success')
            self.assertTrue(button.web_app.url.endswith('/webapp?launch=keyboard'))

    async def test_native_menu_opens_app_directly_without_message(self):
        b = self.module
        client = SimpleNamespace(set_chat_menu_button=AsyncMock(), send_message=AsyncMock())
        with patch.object(b, 'bot', client), patch.object(b, 'WEBAPP_URL', 'https://example.com/'):
            await b.configure_mini_app_menu_button()
            button = client.set_chat_menu_button.call_args.kwargs['menu_button']
            self.assertEqual(button.type, 'web_app')
            self.assertEqual(button.text, '🎓 ТУ УГМК · Кампус')
            self.assertEqual(button.web_app.url, 'https://example.com/webapp')
            client.send_message.assert_not_awaited()

    async def test_cleanup_removes_old_launcher_and_keeps_explicit_exclusions(self):
        b=self.module
        redis=SimpleNamespace(get=AsyncMock(return_value='8'),set=AsyncMock(),smembers=AsyncMock(return_value={'7','8','9'}),delete=AsyncMock(),sadd=AsyncMock())
        client=SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=10)),delete_message=AsyncMock(),delete_messages=AsyncMock())
        with patch.object(b,'dao',redis),patch.object(b,'bot',client):
            await b.clear_chat_history(1, exclude_ids=[9])
            deleted=client.delete_messages.call_args.args[1]
            self.assertEqual(set(deleted),{7,8})
            redis.delete.assert_any_await('miniapp_launcher:1')
            redis.sadd.assert_awaited_once_with('msg_history:1', 9)
