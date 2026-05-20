import asyncio
from bot import sm, dao
async def test():
    # Let's see what is stored in user_subs to pick a real group
    subs = await dao.hgetall("user_subs")
    if subs:
        for uid, gid in subs.items():
            print(f"UID: {uid}, GID: {gid}")
            res = await sm.fetch_schedule(0, "group", gid)
            print("Result week 0:", res)
            res1 = await sm.fetch_schedule(1, "group", gid)
            print("Result week 1:", res1)
            break
asyncio.run(test())
