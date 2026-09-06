"""Shared Redis integration checks. Uses isolated keys; no OpenRouter calls."""
import asyncio
import json
import os
import uuid
from datetime import datetime, timezone

import redis.asyncio as redis

from ai_load import RequestGate
from schedule_refresh import enqueue_daily_refresh
from schedule_config import GROUPS_DB


async def main():
    client = redis.Redis(host=os.getenv('REDIS_HOST', 'localhost'), decode_responses=True)
    prefix = 'test:load:' + uuid.uuid4().hex + ':'
    try:
        gates = [RequestGate(client, prefix=prefix, concurrency=4) for _ in range(2)]
        active = peak = 0
        async def request(index):
            nonlocal active, peak
            async with gates[index % 2].slot('fixture-key', False):
                active += 1
                peak = max(peak, active)
                await asyncio.sleep(.05)
                active -= 1
        await asyncio.gather(*(request(i) for i in range(32)))
        assert peak == 4, peak
        assert await client.zcard(gates[0].keys('fixture-key')[0]) == 0
        print('32 parallel requests, two application instances: peak concurrency = 4, all completed')

        starts = []
        paced = [RequestGate(client, prefix=prefix, free_interval_ms=80) for _ in range(2)]
        async def free_request(index):
            async with paced[index % 2].slot('fixture-free', True):
                starts.append(asyncio.get_running_loop().time())
        await asyncio.gather(*(free_request(i) for i in range(6)))
        assert all(b-a >= .07 for a,b in zip(starts,starts[1:])), starts
        print('Free-pool pacing is shared across both instances')
        await gates[0].cooldown('fixture-pool',60,True)
        async with gates[1].slot('fixture-pool',False):
            pass  # Exhausting the free pool must still allow an explicit paid selection.

        entered = asyncio.Event()
        async def cancelled_request():
            async with gates[0].slot('fixture-cancel', False):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(cancelled_request())
        await entered.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        assert await client.zcard(gates[0].keys('fixture-cancel')[0]) == 0

        # Rewrite every test scheduler key, including queued job flags.
        class SchedulerRedis:
            async def exists(self, key): return await client.exists(prefix+key)
            async def hgetall(self, key): return await client.hgetall(prefix+key)
            async def eval(self, script, count, *args):
                args = [prefix+arg if i < count or arg.startswith('queued:') else arg for i,arg in enumerate(args)]
                return await client.eval(script,count,*args)
        clock = datetime(2026,9,7,3,0,tzinfo=timezone.utc)
        results = await asyncio.gather(*(enqueue_daily_refresh(SchedulerRedis(),clock) for _ in range(5)))
        assert sum(results) == 2 * len(GROUPS_DB), results
        jobs = await client.lrange(prefix+'schedule_jobs',0,-1)
        assert len(jobs) == len(set(jobs)) == 2*len(GROUPS_DB)
        assert {json.loads(job)['week_offset'] for job in jobs} == {0,1}
        print('Five schedulers at 08:00: exactly one daily batch, both weeks, no duplicate jobs')
    finally:
        keys = [key async for key in client.scan_iter(match=prefix+'*')]
        if keys:
            await client.delete(*keys)
        await client.aclose()


asyncio.run(main())
