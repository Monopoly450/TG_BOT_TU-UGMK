import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import HTTPException, Request
import dashboard
import miniapp_launch


class MainAppLaunchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        miniapp_launch._cached_until = 0
        miniapp_launch._cached_link = None

    def telegram_response(self, result):
        response = MagicMock()
        response.json = AsyncMock(return_value={'ok': True, 'result': result})
        request = MagicMock()
        request.__aenter__ = AsyncMock(return_value=response)
        session = MagicMock()
        session.get.return_value = request
        session.__aenter__ = AsyncMock(return_value=session)
        return session

    async def test_link_uses_verified_bot_identity_and_caches(self):
        session = self.telegram_response({'username': 'campus_test_bot', 'has_main_web_app': True})
        with patch.dict(os.environ, {'BOT_TOKEN': 'offline-token'}), patch.object(miniapp_launch, 'telegram_connector'), patch.object(miniapp_launch.aiohttp, 'ClientSession', return_value=session):
            self.assertEqual(await miniapp_launch.get_main_app_link(), 'https://t.me/campus_test_bot?startapp=keyboard')
            self.assertEqual(await miniapp_launch.get_main_app_link(), 'https://t.me/campus_test_bot?startapp=keyboard')
            session.get.assert_called_once()

    async def test_disabled_main_app_does_not_redirect_to_chat(self):
        session = self.telegram_response({'username': 'campus_test_bot', 'has_main_web_app': False})
        with patch.dict(os.environ, {'BOT_TOKEN': 'offline-token'}), patch.object(miniapp_launch, 'telegram_connector'), patch.object(miniapp_launch.aiohttp, 'ClientSession', return_value=session):
            self.assertIsNone(await miniapp_launch.get_main_app_link())

    async def test_keyboard_landing_never_fabricates_authentication(self):
        request = Request({'type': 'http', 'method': 'GET', 'path': '/webapp', 'query_string': b'launch=keyboard&uid=999&username=other_bot', 'headers': []})
        with patch.object(dashboard, 'get_main_app_link', AsyncMock(return_value='https://t.me/campus_test_bot?startapp=keyboard')):
            response = await dashboard.webapp(request)
        self.assertEqual(response.context['launch_url'], 'https://t.me/campus_test_bot?startapp=keyboard')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertNotIn('999', response.body.decode())
        with self.assertRaises(HTTPException) as error:
            await dashboard.api_verify(SimpleNamespace(json=AsyncMock(return_value={'init_data': ''})))
        self.assertEqual(error.exception.status_code, 400)

    async def test_unconfigured_or_unavailable_launch_shows_retry(self):
        request = Request({'type': 'http', 'method': 'GET', 'path': '/webapp', 'query_string': b'launch=keyboard', 'headers': []})
        for result in [None, RuntimeError('unavailable')]:
            lookup = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
            with patch.object(dashboard, 'get_main_app_link', lookup):
                response = await dashboard.webapp(request)
            self.assertIsNone(response.context['launch_url'])
            self.assertIn('Попробовать снова', response.body.decode())
            self.assertNotIn('Ошибка авторизации', response.body.decode())
