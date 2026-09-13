"""Resolve the bot's official Main Mini App link, without issuing login tokens."""
import asyncio
import os
import re
import time

import aiohttp

from network_config import telegram_connector

_lock = asyncio.Lock()
_cached_link = None
_cached_until = 0.0


async def get_main_app_link():
    global _cached_link, _cached_until
    async with _lock:
        if time.monotonic() < _cached_until:
            return _cached_link
        token = os.environ.get('BOT_TOKEN')
        if not token:
            raise RuntimeError('Bot is not configured')
        async with aiohttp.ClientSession(connector=telegram_connector()) as session:
            async with session.get(
                f'https://api.telegram.org/bot{token}/getMe',
                timeout=aiohttp.ClientTimeout(total=5),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        result = data.get('result', {})
        username = result.get('username', '')
        if not data.get('ok') or not re.fullmatch(r'[A-Za-z0-9_]{5,32}', username):
            raise RuntimeError('Bot identity is unavailable')
        # No fallback username: a link for another bot would fail authentication.
        _cached_link = f'https://t.me/{username}?startapp=keyboard' if result.get('has_main_web_app') else None
        _cached_until = time.monotonic() + (60 if _cached_link else 5)
        return _cached_link
