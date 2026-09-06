"""Shared admission control for Telegram and Mini App OpenRouter requests."""
import asyncio
import hashlib
import math
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import redis.asyncio as redis


class AIBusyError(Exception):
    """A bounded queue or provider cooldown could not finish in time."""


ADMIT = """
local t = redis.call('TIME')
local now = tonumber(t[1]) * 1000 + math.floor(tonumber(t[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now)
local wait = redis.call('PTTL', KEYS[2])
if wait > 0 then return wait end
if tonumber(ARGV[3]) > 0 then
  wait = redis.call('PTTL', KEYS[3])
  if wait > 0 then return wait end
end
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[2]) then return 100 end
redis.call('ZADD', KEYS[1], now + tonumber(ARGV[4]), ARGV[1])
redis.call('PEXPIRE', KEYS[1], ARGV[4])
if tonumber(ARGV[3]) > 0 then redis.call('SET', KEYS[3], '1', 'PX', ARGV[3]) end
return 0
"""
COOLDOWN = """
if redis.call('PTTL', KEYS[1]) < tonumber(ARGV[1]) then
  redis.call('SET', KEYS[1], '1', 'PX', ARGV[1])
end
return 1
"""


class RequestGate:
    def __init__(self, client=None, prefix='ai:load:', concurrency=None, free_interval_ms=None, queue_timeout=None):
        self.redis = client or redis.Redis(host=os.getenv('REDIS_HOST', 'localhost'),
                                          decode_responses=True, socket_connect_timeout=2, socket_timeout=2)
        self.prefix = prefix
        self.concurrency = concurrency or max(1, int(os.getenv('AI_MAX_CONCURRENCY', '4')))
        # 20 RPM is the free-pool ceiling; configuration may only slow it down.
        self.free_interval_ms = free_interval_ms if free_interval_ms is not None else max(3100, int(os.getenv('AI_FREE_INTERVAL_MS', '3100')))
        self.queue_timeout = queue_timeout if queue_timeout is not None else 30

    def keys(self, api_key, free=False):
        scope = self.prefix + hashlib.sha256(api_key.encode()).hexdigest()
        return scope + ':active', scope + (':free-cooldown' if free else ':paid-cooldown'), scope + ':free-pace'

    @asynccontextmanager
    async def slot(self, api_key, free):
        active, cooldown, pace = self.keys(api_key, free)
        ticket = uuid.uuid4().hex
        deadline = asyncio.get_running_loop().time() + self.queue_timeout
        try:
            while True:
                wait = int(await self.redis.eval(ADMIT, 3, active, cooldown, pace, ticket,
                                                self.concurrency, self.free_interval_ms if free else 0, 90000))
                if wait == 0:
                    break
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0 or wait / 1000 > remaining:
                    raise AIBusyError('Сейчас много обращений к ИИ. Повторите сообщение чуть позже.')
                await asyncio.sleep(min(wait / 1000, .5))
        except redis.RedisError as error:
            raise AIBusyError('Очередь ИИ временно недоступна. Попробуйте чуть позже.') from error
        try:
            yield
        finally:
            try:
                await asyncio.shield(self.redis.zrem(active, ticket))
            except redis.RedisError:
                pass  # The lease expires if a worker or Redis connection is lost.

    async def cooldown(self, api_key, seconds, free=False):
        try:
            await self.redis.eval(COOLDOWN, 1, self.keys(api_key, free)[1], max(1, int(seconds * 1000)))
        except redis.RedisError as error:
            raise AIBusyError('Очередь ИИ временно недоступна. Попробуйте чуть позже.') from error


_gate = None


def get_gate():
    global _gate
    if _gate is None:
        _gate = RequestGate()
    return _gate


def retry_delay(error, attempt):
    headers = getattr(getattr(error, 'response', None), 'headers', None) or getattr(error, 'headers', None) or {}
    value = headers.get('retry-after') or headers.get('Retry-After')
    if value:
        try:
            delay = float(value)
            if not math.isfinite(delay):
                raise ValueError('Invalid Retry-After')
            return max(0, delay)
        except (TypeError, ValueError):
            try:
                date = parsedate_to_datetime(value)
                if date.tzinfo is None:
                    date = date.replace(tzinfo=timezone.utc)
                return max(0, (date - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                pass
    return 2 ** (attempt + 1)


async def run_completion(client, api_key, free, **kwargs):
    gate = get_gate()
    try:
        async with asyncio.timeout(70):
            for attempt in range(3):
                try:
                    async with gate.slot(api_key, free):
                        return await client.chat.completions.create(**kwargs)
                except Exception as error:
                    status = getattr(error, 'status_code', None) or getattr(error, 'status', None)
                    if status != 429 and type(error).__name__ != 'RateLimitError':
                        raise
                    delay = retry_delay(error, attempt)
                    await gate.cooldown(api_key, delay, free)
                    if attempt == 2 or delay >= 65:
                        raise AIBusyError('OpenRouter временно ограничил запросы. Попробуйте позже или выберите недорогую модель.') from error
                    await asyncio.sleep(delay)
    except TimeoutError as error:
        raise AIBusyError('ИИ пока не успел ответить. Повторите сообщение чуть позже.') from error
