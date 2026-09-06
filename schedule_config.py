"""Shared group catalog for the bot, API and schedule workers."""
import re

CACHE_VERSION = 41
GROUPS_DB = {'Ит-24107': 'c158228d-7b93-11f1-b455-00155d7f2942%3Afee16686-4374-11ef-b448-00155d7f1420',
 'А-24101': 'bb2b2840-3d0f-11ef-b448-00155d7f1420%3A715cc0fc-3eb1-11ef-b448-00155d7f1420',
 'М-24102': '9a3f0516-42b2-11ef-b448-00155d7f1420%3A372960bb-4374-11ef-b448-00155d7f1420',
 'Т-24105': '16190a05-42b5-11ef-b448-00155d7f1420%3A5873fb74-4373-11ef-b448-00155d7f1420',
 'Эн-24103': '1da4f5f9-3d19-11ef-b448-00155d7f1420%3A19692d41-3ead-11ef-b448-00155d7f1420',
 'ГД-24104': '1ef98392-4335-11ef-b448-00155d7f1420%3A148d5959-4376-11ef-b448-00155d7f1420',
 'Гэм-24106': 'd5332769-4338-11ef-b448-00155d7f1420%3A629425ac-4375-11ef-b448-00155d7f1420',
 'Эк-25109': 'cc6b10c1-1542-11f0-b44a-00155d7f1420%3A06321270-5d88-11f0-b44a-00155d7f1420',
 'А-25101': '64345542-d3ec-11ef-b449-00155d7f1420%3A87999d48-5d7f-11f0-b44a-00155d7f1420',
 'Ит-25107': '7ac8400c-7b93-11f1-b455-00155d7f2942%3A9a9bd9dc-5d84-11f0-b44a-00155d7f1420',
 'М-25102': 'efdd4a32-d3fb-11ef-b449-00155d7f1420%3Aa7f635af-5d85-11f0-b44a-00155d7f1420',
 'Т-25105': '8dd0b9e3-d400-11ef-b449-00155d7f1420%3A690b7f2d-5d87-11f0-b44a-00155d7f1420',
 'Эн-25103': '43c9ca99-d402-11ef-b449-00155d7f1420%3A5dfec504-5d88-11f0-b44a-00155d7f1420',
 'Гд-25104': '8e4c5c1c-d40a-11ef-b449-00155d7f1420%3A11b10f9e-5d82-11f0-b44a-00155d7f1420',
 'Гэм-25106': 'ef6847a9-d40c-11ef-b449-00155d7f1420%3A14e87d8c-5d84-11f0-b44a-00155d7f1420',
 'А-26101': '3bb57b9d-2db9-11f1-b450-00155d7f6c95%3A4dc3b0ab-6574-11f1-b453-00155d7f6c95',
 'Ит-26107': '893bd83b-2dbc-11f1-b450-00155d7f6c95%3Ab0f550ee-64ea-11f1-b453-00155d7f6c95',
 'М-26102': '4cba4e42-2dc0-11f1-b450-00155d7f6c95%3Af86ae356-6572-11f1-b453-00155d7f6c95',
 'Т-26105': '30534609-2dcd-11f1-b450-00155d7f6c95%3A68db3eec-64e4-11f1-b453-00155d7f6c95',
 'Эн-26103': '3ec916bb-3000-11f1-b450-00155d7f6c95%3A71e842ad-64e1-11f1-b453-00155d7f6c95',
 'Эк-26109': 'a39e3fe5-3006-11f1-b450-00155d7f6c95%3Ac54119ba-64e2-11f1-b453-00155d7f6c95',
 'Гд-26104': '4d251450-31ca-11f1-b450-00155d7f6c95%3Aef812cc0-6573-11f1-b453-00155d7f6c95',
 'Гэм-26106': 'a597a698-31cb-11f1-b450-00155d7f6c95%3A9dc13d23-6573-11f1-b453-00155d7f6c95'}


def canonical_group(name):
    if not isinstance(name, str):
        return None
    name = name.strip()
    if re.fullmatch(r"ит-24107(?:\s+гр\.?\s*\d+)?", name, re.I):
        return "Ит-24107"
    return next((key for key in GROUPS_DB if key.casefold() == name.casefold()), name)


def active_group(name):
    name = canonical_group(name)
    return bool(name and re.fullmatch(r"[А-Яа-яЁёA-Za-z]+-(?:2[4-9]|[3-9]\d)\d{3}(?:\s+гр\.?\s*\d+)?", name))


def merged_groups(discovered=None):
    result = dict(GROUPS_DB)
    result.update({canonical_group(k): v for k, v in (discovered or {}).items() if active_group(k)})
    result["Ит-24107"] = GROUPS_DB["Ит-24107"]  # Explicit user-provided ID.
    return result


def lesson_matches_group(value, target):
    names = re.findall(r"[А-Яа-яЁёA-Za-z]+-\d{5}", str(value or ""))
    if not names:
        return True
    return canonical_group(target).casefold() in {canonical_group(n).casefold() for n in names}


async def migrate_group_preferences(redis, db):
    """Normalize subgroup subscriptions; remove retired cohorts from selectors."""
    for field in ("user_subs", "starosta_group_saved"):
        for uid, group in (await redis.hgetall(field)).items():
            if not active_group(group):
                await redis.hdel(field, uid)
            elif canonical_group(group) != group:
                await redis.hset(field, uid, canonical_group(group))
    for group in (await redis.hgetall("db_groups")):
        if not active_group(group) or canonical_group(group) != group:
            await redis.hdel("db_groups", group)
    await redis.hset("db_groups", mapping=merged_groups(await redis.hgetall("db_groups")))
    async for key in redis.scan_iter(match="favs:*"):
        for favorite in await redis.smembers(key):
            if favorite.startswith("group:"):
                group = favorite[6:]
                if not active_group(group) or canonical_group(group) != group:
                    await redis.srem(key, favorite)
                    if active_group(group):
                        await redis.sadd(key, "group:" + canonical_group(group))
    async with db.pool.acquire() as conn:
        for row in await conn.fetch("SELECT telegram_id, group_name FROM users WHERE group_name IS NOT NULL"):
            name = canonical_group(row['group_name']) if active_group(row['group_name']) else None
            if name != row['group_name']:
                await conn.execute("UPDATE users SET group_name=$2 WHERE telegram_id=$1", row['telegram_id'], name)
