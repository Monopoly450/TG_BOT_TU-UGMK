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
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL") 

DATA_DIR = "data"
CACHE_DIR = "cache"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
CACHE_LIFETIME = 86400 
CACHE_VERSION = 32 

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Сессия с прокси и инициализация бота
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

ADMIN_IDS = [474095004] 
sent_messages = collections.defaultdict(list)

# --- БАЗЫ ДАННЫХ ID ---
GROUPS_DB = {
    "Ит-24107 гр.1": "756cb41d-42af-11ef-b448-00155d7f1420%3A309c2eb3-6dea-11f0-b44a-00155d7f1420",
    "А-24101": "b47ff74e-3d0f-11ef-b448-00155d7f1420%3A715cc0fc-3eb1-11ef-b448-00155d7f1420",
}
TEACHERS_DB = {
    "Сакулин Валерий Александрович": "000000376",
    "Мазитов Виктор Расульевич": "000000421",
}
CLASSROOMS_DB = { "Ауд. 203": "67941c0b-ca51-11ee-b440-00155d7f0e19" }
DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

# --- MIDDLEWARES ---
class UserRegistrationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if hasattr(event, "from_user") and event.from_user:
            users = set()
            if os.path.exists(USERS_FILE):
                with open(USERS_FILE, "r") as f: users = set(json.load(f))
            if event.from_user.id not in users:
                users.add(event.from_user.id)
                with open(USERS_FILE, "w") as f: json.dump(list(users), f)
        return await handler(event, data)

class SentMessageTracker(BaseMiddleware):
    async def __call__(self, handler, event: Message, data: Dict[str, Any]):
        original_answer = event.answer
        async def answer_with_tracking(*args, **kwargs):
            msg = await original_answer(*args, **kwargs)
            if msg: sent_messages[event.chat.id].append(msg.message_id)
            return msg
        event.answer = answer_with_tracking
        return await handler(event, data)

dp.message.middleware(UserRegistrationMiddleware())
dp.message.middleware(SentMessageTracker())

# --- REDIS & SCHEDULE MANAGER ---
dao = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, decode_responses=True)

class ScheduleManager:
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None):
        key = f"data:v{CACHE_VERSION}:{t_type}:{t_val}:w{wo}"
        if await dao.exists(key): return json.loads(await dao.get(key))
        await dao.lpush('schedule_jobs', json.dumps({"week_offset": wo, "target_type": t_type, "target_value": t_val}))
        for _ in range(120):
            await asyncio.sleep(0.5)
            if await dao.exists(key): return json.loads(await dao.get(key))
        return {}
    async def clear_cache(self):
        keys = await dao.keys(f"data:v{CACHE_VERSION}:*")
        if keys: await dao.delete(*keys)
        for f in os.listdir(CACHE_DIR): os.remove(os.path.join(CACHE_DIR, f))

sm = ScheduleManager()

# --- UI ---
def get_main_menu(val=None):
    if val:
        kb = [[KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")], [KeyboardButton(text="🗓 Эта неделя"), KeyboardButton(text="➡️ След. неделя")], [KeyboardButton(text="🔄 Сбросить"), KeyboardButton(text="🧹 Очистить")]]
    else:
        kb = [[KeyboardButton(text="👥 Группы"), KeyboardButton(text="👩‍🏫 Преподаватели")], [KeyboardButton(text="🏫 Аудитории")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def fmt_day(day, lessons, s, target_type=None):
    text = f"🗓 <b>{day.upper()}</b>\n" + "─"*20 + "\n\n"
    if not lessons: return text + "😴 Нет занятий"
    for l in lessons:
        text += f"<b>{l['subject']}</b>\n"
        text += f"   🕐 {l['time']} | 🏫 {l['room']}\n"
        if target_type in ["teacher", "classroom"]: text += f"   👥 {l['group']}\n"
        else: text += f"   👩‍🏫 {l['teacher']}\n"
    return text

def fmt_week(s, wo):
    text = f"🗓 <b>НЕДЕЛЯ {wo}</b>\n" + "─"*20 + "\n\n"
    for day_name in DAYS_OF_WEEK[:6]:
        lessons = s.get(day_name, [])
        text += f"<b>{day_name}</b>: {len(lessons)} пар\n"
    return text

@router.message(F.text.in_({"📅 Сегодня", "📆 Завтра"}))
async def days(m: Message, state: FSMContext):
    await display_schedule(m, state, is_week=False, wo_offset=1 if m.text == "📆 Завтра" else 0)

@router.message(F.text.in_({"🗓 Эта неделя", "➡️ След. неделя"}))
async def weeks(m: Message, state: FSMContext):
    await display_schedule(m, state, is_week=True, wo_offset=1 if m.text == "➡️ След. неделя" else 0)

@router.message(F.text == "🧹 Очистить")
async def clear(m: Message, state: FSMContext):
    await state.clear()
    chat_id = m.chat.id
    
    # Добавляем ID команды "Очистить" в список
    if chat_id in sent_messages:
        sent_messages[chat_id].append(m.message_id)
    
    message_ids_to_delete = list(sent_messages.get(chat_id, []))
    
    # Пытаемся удалить все сообщения пачками по 100
    for i in range(0, len(message_ids_to_delete), 100):
        try:
            await m.bot.delete_messages(chat_id, message_ids_to_delete[i:i+100])
        except Exception as e:
            logger.error(f"Partial clear failed: {e}")

    sent_messages[chat_id] = []
    new_msg = await m.answer("🧹 Чат очищен, фильтры сброшены.", reply_markup=get_main_menu())
    sent_messages[chat_id].append(new_msg.message_id)

@router.message(F.text == "🔄 Сбросить")
async def reset(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🔄 Фильтры сброшены.", reply_markup=get_main_menu())

async def main():
    if PROXY_URL: logger.info(f"🌐 Используется прокси: {PROXY_URL}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e: logger.critical(f"Global error: {e}")
