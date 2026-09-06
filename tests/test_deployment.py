import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode
import dashboard
from network_config import configure_direct_network, telegram_connector
from web_runtime import miniapp_app, admin_app


async def request_status(app, path, method='GET', params=None):
    messages = []
    scope = {'type':'http','asgi':{'version':'3.0'},'http_version':'1.1','method':method,
             'scheme':'http','path':path,'raw_path':path.encode(),'query_string':urlencode(params or {}).encode(),
             'root_path':'','headers':[(b'host',b'test')],'server':('test',80),'client':('127.0.0.1',12345)}
    async def receive(): return {'type':'http.request','body':b'','more_body':False}
    async def send(message): messages.append(message)
    await app(scope,receive,send)
    return next(m['status'] for m in messages if m['type']=='http.response.start')


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_public_container_serves_miniapp_without_admin_website(self):
        self.assertEqual(await request_status(miniapp_app,'/webapp'),200)
        self.assertEqual(await request_status(miniapp_app,'/login','POST'),404)
        self.assertEqual(await request_status(miniapp_app,'/settings/openrouter_keys','POST'),404)
        with patch.object(dashboard,'dao',SimpleNamespace(hgetall=AsyncMock(return_value={}))):
            self.assertEqual(await request_status(miniapp_app,'/api/groups'),200)
        self.assertEqual(await request_status(miniapp_app,'/api/models',params={'uid':1,'init_data':'invalid'}),401)

    async def test_admin_container_does_not_serve_miniapp_api(self):
        self.assertEqual(await request_status(admin_app,'/webapp'),404)
        self.assertEqual(await request_status(admin_app,'/api/groups'),404)
        self.assertEqual(await request_status(admin_app,'/'),200)

    async def test_telegram_proxy_is_explicit(self):
        with patch.dict(os.environ,{'PROXY_URL':'socks5://example.com:1080'}),patch('network_config.ProxyConnector.from_url') as connector:
            telegram_connector()
            connector.assert_called_once_with('socks5://example.com:1080')
        with patch.dict(os.environ,{'PROXY_URL':''}):
            self.assertIsNone(telegram_connector())


class DirectNetworkTests(unittest.TestCase):
    def test_scraper_removes_all_inherited_proxy_variants(self):
        keys=['HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy','PROXY_URL']
        with patch.dict(os.environ,{key:'http://unreachable:1' for key in keys}):
            configure_direct_network()
            self.assertTrue(all(key not in os.environ for key in keys))
            self.assertEqual(os.environ['NO_PROXY'],'*')
            self.assertEqual(os.environ['no_proxy'],'*')
