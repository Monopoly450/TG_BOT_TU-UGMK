import os
import re
import json
import logging
import asyncio
import urllib.parse
import collections
from datetime import datetime, timedelta
from typing import Callable, Dict, Any, Awaitable

from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F, Router, BaseMiddleware
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
import redis.asyncio as redis

# ═══════════════════ НАСТРОЙКИ ═══════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8789288719:AAFTR5Mp2iV3yrtvHSSgdxxa5buJbbpl-uc")
PROXY_URL = os.getenv("PROXY_URL") # Формат: http://proxy:8888

SCHEDULE_URL = "https://up.corp.tu-ugmk.com/student/schedule"
COOKIES = {} 

LOGIN = os.getenv("LOGIN", "uvybhjhhv@gmail.com")
PASSWORD = os.getenv("PASSWORD", "qazwsxedcip60000OP")

DATA_DIR = "data"
CACHE_DIR = "cache"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
MAINTENANCE_FILE = os.path.join(DATA_DIR, "maintenance.json")

CACHE_LIFETIME = 86400 
CACHE_VERSION = 32 

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
# ═════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Настройка сессии с прокси
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
if PROXY_URL:
    logger.info(f"🌐 Используется прокси (AiohttpSession): {PROXY_URL}")

bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

ADMIN_IDS = [474095004] 

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def is_maintenance():
    if not os.path.exists(MAINTENANCE_FILE): return False
    try:
        with open(MAINTENANCE_FILE, "r") as f: return json.load(f).get("is_active", False)
    except Exception: return False

def set_maintenance(state: bool):
    with open(MAINTENANCE_FILE, "w") as f: json.dump({"is_active": state}, f)

def get_users():
    if not os.path.exists(USERS_FILE): return set()
    try:
        with open(USERS_FILE, "r") as f: return set(json.load(f))
    except Exception: return set()

def save_user(user_id):
    users = get_users()
    if user_id not in users:
        users.add(user_id)
        with open(USERS_FILE, "w") as f: json.dump(list(users), f)

# --- MIDDLEWARES ---
class LatestMessageOnlyMiddleware(BaseMiddleware):
    def __init__(self, debounce_delay: float = 0.2):
        super().__init__()
        self.latest_message_ids: Dict[int, int] = collections.defaultdict(int)
        self.debounce_delay = debounce_delay

    async def __call__(self, handler, event: Message, data):
        if not isinstance(event, Message): return await handler(event, data)
        chat_id = event.chat.id
        current_message_id = event.message_id
        self.latest_message_ids[chat_id] = max(self.latest_message_ids[chat_id], current_message_id)
        await asyncio.sleep(self.debounce_delay)
        if self.latest_message_ids[chat_id] == current_message_id:
            return await handler(event, data)
        return None

class UserRegistrationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if hasattr(event, "from_user") and event.from_user:
            save_user(event.from_user.id)
        return await handler(event, data)

class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = event.from_user.id if hasattr(event, "from_user") and event.from_user else None
        if is_maintenance() and user_id not in ADMIN_IDS:
            if isinstance(event, Message):
                await event.answer("🛠 <b>Бот на техработах.</b>\nПожалуйста, попробуйте позже.", parse_mode="HTML")
            return None
        return await handler(event, data)

dp.update.middleware(LatestMessageOnlyMiddleware())
dp.message.middleware(UserRegistrationMiddleware())
dp.callback_query.middleware(UserRegistrationMiddleware())
dp.message.middleware(MaintenanceMiddleware())
dp.callback_query.middleware(MaintenanceMiddleware())

# --- КОНСТАНТЫ РАСПИСАНИЯ ---
DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
SHORT_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAY_EMOJI = {"Понедельник": "1️⃣", "Вторник": "2️⃣", "Среда": "3️⃣", "Четверг": "4️⃣", "Пятница": "5️⃣", "Суббота": "6️⃣", "Воскресенье": "7️⃣"}

GROUPS_DB = {
    "Ит-24107 гр.1": "756cb41d-42af-11ef-b448-00155d7f1420%3A309c2eb3-6dea-11f0-b44a-00155d7f1420",
    "Ит-24107 гр.2": "ea53e266-6dd2-11f0-b44a-00155d7f1420%3A5bbb50dd-6dea-11f0-b44a-00155d7f1420",
    "Ит-24107 гр.3": "e694ebbb-6dd3-11f0-b44a-00155d7f1420%3A9293ef2e-6dea-11f0-b44a-00155d7f1420",
    "А-24101": "b47ff74e-3d0f-11ef-b448-00155d7f1420%3A715cc0fc-3eb1-11ef-b448-00155d7f1420",
    "М-24102": "926cd860-42b2-11ef-b448-00155d7f1420%3A372960bb-4374-11ef-b448-00155d7f1420",
    "Т-24105": "0e9d8133-42b5-11ef-b448-00155d7f1420%3A5873fb74-4373-11ef-b448-00155d7f1420",
    "Эн-24103": "171f74fb-3d19-11ef-b448-00155d7f1420%3A19692d41-3ead-11ef-b448-00155d7f1420",
    "ГД-24104": "14064fbf-4335-11ef-b448-00155d7f1420%3A148d5959-4376-11ef-b448-00155d7f1420",
    "Гэм-24106": "d53322fa-4338-11ef-b448-00155d7f1420%3A629425ac-4375-11ef-b448-00155d7f1420",
}
TEACHERS_DB = {
    "Сакулин Валерий Александрович": "000000376",
    "Мазитов Виктор Расульевич": "000000421",
    "Котельников Сергей Андреевич": "000000383",
    "Голубина Валентина Васильевна": "000000467",
    "Кабанов Александр Михайлович": "000000409",
    "Игумнова Юлия Олеговна": "000002912",
    "Тюжина Ирина Викторовна": "000002915",
}
CLASSROOMS_DB = {
    "Толк5": "2355c22e-2bcd-11e7-b191-005056953b1b",
    "Ауд. 203": "67941c0b-ca51-11ee-b440-00155d7f0e19",
}

# --- REDIS DAO ---
class RedisDAO:
    def __init__(self, host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), db=0):
        self.client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        self.is_connected = False

    async def connect(self):
        try:
            await self.client.ping()
            self.is_connected = True
            logger.info("✅ Redis DAO: Подключено.")
        except Exception:
            self.is_connected = False
            logger.warning("⚠️ Redis DAO: Ошибка подключения.")

    async def set(self, key, value, ttl=CACHE_LIFETIME):
        if self.is_connected: await self.client.setex(key, ttl, json.dumps(value, ensure_ascii=False))

    async def get(self, key):
        if not self.is_connected: return None
        data = await self.client.get(key)
        return json.loads(data) if data else None

    async def delete_many(self, pattern):
        if self.is_connected:
            keys = await self.client.keys(pattern)
            if keys: await self.client.delete(*keys)

    async def sadd(self, name, value):
        if self.is_connected:
            await self.client.sadd(name, value)
            await self.client.expire(name, CACHE_LIFETIME)

    async def smembers(self, name):
        return await self.client.smembers(name) if self.is_connected else []

    async def lpush(self, key, value):
        if self.is_connected: await self.client.lpush(key, json.dumps(value, ensure_ascii=False))

dao = RedisDAO()

# --- МЕНЕДЖЕР РАСПИСАНИЯ ---
class ScheduleManager:
    def __init__(self, dao_instance): self.dao = dao_instance
    async def init(self):
        if not self.dao.is_connected: await self.dao.connect()

    async def fetch_schedule(self, week_offset=0, target_type=None, target_value=None):
        target_id = f"{target_type}:{target_value}" if target_type and target_value else "default"
        data_key = f"data:v{CACHE_VERSION}:{target_id}:w{week_offset}"
        
        cached = await self.dao.get(data_key)
        if cached: return cached

        path = os.path.join(CACHE_DIR, data_key.replace(":", "_") + ".json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f: data = json.load(f)
                if (datetime.now() - datetime.fromisoformat(data["timestamp"])).total_seconds() < CACHE_LIFETIME:
                    return data["schedule"]
            except Exception: pass

        await self.dao.lpush('schedule_jobs', {"week_offset": week_offset, "target_type": target_type, "target_value": target_value})
        for _ in range(120): # 60 sec
            await asyncio.sleep(0.5)
            res = await self.dao.get(data_key)
            if res: return res
        return {}

    async def clear_cache(self):
        await self.dao.delete_many(f"data:v{CACHE_VERSION}:*")
        if os.path.exists(CACHE_DIR):
            for f in os.listdir(CACHE_DIR):
                if f.endswith(".json"): os.remove(os.path.join(CACHE_DIR, f))

schedule_manager = ScheduleManager(dao)

# --- UI & HANDLERS ---
def get_main_menu(val=None):
    kb = [
        [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")],
        [KeyboardButton(text="🗓 Эта неделя"), KeyboardButton(text="➡️ След. неделя")],
        [KeyboardButton(text="👥 Группы"), KeyboardButton(text="👩‍🏫 Преподаватели")],
        [KeyboardButton(text="🔄 Сбросить"), KeyboardButton(text="🧹 Очистить")]
    ] if val else [
        [KeyboardButton(text="👥 Группы"), KeyboardButton(text="👩‍🏫 Преподаватели")],
        [KeyboardButton(text="🏫 Аудитории")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_day_nav(di, wo=0):
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_day_{di}_{wo}"),
        InlineKeyboardButton(text="⚙️ Фильтр", callback_data=f"filter:day_{di}_{wo}")
    ]])

def fmt_day(day, lessons, schedule, wo=0, t_type=None):
    ds = schedule.get("_dates", {}).get(day, "")
    text = f"🗓 {day} ({ds})\n" + "─"*15 + "\n"
    if not lessons: return text + "😴 Выходной"
    for i, l in enumerate(lessons, 1):
        text += f"{i}. {l['time']} | {l['subject']}\n   📍 {l['room']} | {l['teacher']}\n\n"
    return text

def fmt_week(s, wo=0, t_type=None):
    text = f"🗓 Неделя {wo}\n" + "─"*15 + "\n"
    for day in DAYS_OF_WEEK[:6]:
        lessons = s.get(day, [])
        text += f"🔹 {day}: {len(lessons)} пар\n"
    return text

@router.message(CommandStart())
async def cmd_start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 Привет! Выбери группу или преподавателя:", reply_markup=get_main_menu())

@router.message(F.text == "👥 Группы")
async def btn_groups(m: Message):
    kb = [[InlineKeyboardButton(text=g, callback_data=f"fsel:group:{i}:menu")] for i, g in enumerate(GROUPS_DB.keys())]
    await m.answer("👇 Выбери группу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("fsel:"))
async def cb_fsel(c: CallbackQuery, state: FSMContext):
    _, t_type, idx, _ = c.data.split(":")
    val = list(GROUPS_DB.keys() if t_type=="group" else TEACHERS_DB.keys())[int(idx)]
    await state.update_data(target_type=t_type, target_value=val)
    await c.message.answer(f"✅ Выбрано: {val}", reply_markup=get_main_menu(val))
    await c.answer()

@router.message(F.text.in_({"📅 Сегодня", "📆 Завтра"}))
async def btn_days(m: Message, state: FSMContext):
    data = await state.get_data()
    wo = 0
    di = datetime.now().weekday()
    if m.text == "📆 Завтра":
        if di >= 6: wo = 1; di = 0
        else: di += 1
    s = await schedule_manager.fetch_schedule(wo, data.get("target_type"), data.get("target_value"))
    day_name = DAYS_OF_WEEK[di] if di < 7 else "Понедельник"
    await m.answer(fmt_day(day_name, s.get(day_name, []), s, wo), reply_markup=get_day_nav(di, wo), parse_mode="HTML")

@router.message(F.text == "🧹 Очистить")
async def btn_clear(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🧹 Очищено.", reply_markup=get_main_menu())

async def main():
    await schedule_manager.init()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except KeyboardInterrupt: pass
