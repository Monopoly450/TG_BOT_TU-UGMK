"""Explicit Telegram proxy; schedule workers always use direct connections."""
import os
from aiohttp_socks import ProxyConnector


def telegram_connector():
    proxy = os.getenv('PROXY_URL', '').strip()
    return ProxyConnector.from_url(proxy) if proxy else None


def configure_direct_network():
    for key in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy', 'PROXY_URL'):
        os.environ.pop(key, None)
    os.environ['NO_PROXY'] = '*'
    os.environ['no_proxy'] = '*'
