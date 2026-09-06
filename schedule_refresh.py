"""Daily refresh owned by workers, independent of Telegram notifications."""
import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone

from schedule_config import CACHE_VERSION, merged_groups

TZ = timezone(timedelta(hours=5))
logger = logging.getLogger('schedule_refresh')
ENQUEUE = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local count = 0
for i = 1, #ARGV, 2 do
  if redis.call('SET', ARGV[i], '1', 'NX', 'EX', 600) then
    redis.call('RPUSH', KEYS[2], ARGV[i+1])
    count = count + 1
  end
end
redis.call('SET', KEYS[1], '1', 'EX', 259200)
return count
"""


async def enqueue_daily_refresh(client, now=None):
    now = (now or datetime.now(TZ)).astimezone(TZ)
    if now.hour < 8:
        return 0
    marker = f'schedule:daily:v{CACHE_VERSION}:{now.date().isoformat()}'
    if await client.exists(marker):
        return 0
    groups = merged_groups(await client.hgetall('db_groups'))
    args = []
    for offset in (0, 1):
        monday = now.date() - timedelta(days=now.weekday()) + timedelta(weeks=offset)
        for group in groups:
            key = f'data:v{CACHE_VERSION}:{monday:%d.%m.%Y}:group:{group}'
            args.extend((f'queued:{key}', json.dumps({'week_offset': offset, 'target_type': 'group',
                                                     'target_value': group, 'refresh': True}, ensure_ascii=False)))
    return int(await client.eval(ENQUEUE, 2, marker, 'schedule_jobs', *args))


async def daily_refresh_loop(client):
    while True:
        try:
            count = await enqueue_daily_refresh(client)
            if count:
                logger.info('Daily 08:00 refresh queued: %s schedules', count)
        except Exception:
            logger.exception('Daily schedule refresh will retry')
        await asyncio.sleep(15)


async def store_schedule_result(dao, key, result, lifetime):
    if not result or '_error' in result:
        previous = await dao.get(key)
        if previous and '_error' not in previous:
            # Keep a usable cache while the portal is unavailable; retry later.
            await dao.set(key, previous, ex=lifetime)
            return False
        await dao.set(key, result or {'_error': 'Портал временно недоступен'}, ex=60)
        return False
    await dao.set(key, result, ex=lifetime)
    return True
