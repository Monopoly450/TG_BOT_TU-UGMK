import os
import re
import json
import logging
import asyncio
import urllib.parse
import collections
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta
from typing import Dict, Any

from dotenv import load_dotenv
load_dotenv()

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.filters import CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext
from aiogram.exceptions import TelegramBadRequest
from aiogram.dispatcher.middlewares.base import BaseMiddleware
import redis.asyncio as redis

# ═══════════════════ НАСТРОЙКИ ═══════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL") 

if not BOT_TOKEN:
    raise ValueError("⚠️ BOT_TOKEN не найден! Убедитесь, что он указан в .env файле или переменных окружения.")

DATA_DIR, CACHE_DIR, USERS_FILE = "data", "cache", os.path.join("data", "users.json")
CACHE_LIFETIME, CACHE_VERSION = 86400, 33
MSG_STORE_LIMIT = 172800 # 48 часов в секундах

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ADMIN_IDS = [474095004] 
dao = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, decode_responses=True)

# --- TRACKING LOGIC (REDIS BASED) ---
async def track_message(chat_id: int, message_id: int):
    key = f"msg_history:{chat_id}"
    await dao.sadd(key, message_id)
    await dao.expire(key, MSG_STORE_LIMIT)

class IncomingMessageTracker(BaseMiddleware):
    async def __call__(self, handler, event: Message, data: Dict[str, Any]):
        if getattr(event, "chat", None) and getattr(event, "message_id", None):
            await track_message(event.chat.id, event.message_id)
        return await handler(event, data)

class OutgoingMessageTracker(BaseRequestMiddleware):
    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)
        if result and hasattr(result, "message_id") and hasattr(result, "chat"):
            await track_message(result.chat.id, result.message_id)
        return result

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

# --- BOT SETUP ---
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session)
bot.session.middleware(OutgoingMessageTracker())

dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(UserRegistrationMiddleware())
dp.message.middleware(IncomingMessageTracker())

# --- DATABASES ---
GROUPS_DB = {"Ит-24107 гр.1": "756cb41d-42af-11ef-b448-00155d7f1420%3A309c2eb3-6dea-11f0-b44a-00155d7f1420", "Ит-24107 гр.2": "ea53e266-6dd2-11f0-b44a-00155d7f1420%3A5bbb50dd-6dea-11f0-b44a-00155d7f1420", "Ит-24107 гр.3": "e694ebbb-6dd3-11f0-b44a-00155d7f1420%3A9293ef2e-6dea-11f0-b44a-00155d7f1420", "А-24101": "b47ff74e-3d0f-11ef-b448-00155d7f1420%3A715cc0fc-3eb1-11ef-b448-00155d7f1420", "М-24102": "926cd860-42b2-11ef-b448-00155d7f1420%3A372960bb-4374-11ef-b448-00155d7f1420", "Т-24105": "0e9d8133-42b5-11ef-b448-00155d7f1420%3A5873fb74-4373-11ef-b448-00155d7f1420", "Эн-24103": "171f74fb-3d19-11ef-b448-00155d7f1420%3A19692d41-3ead-11ef-b448-00155d7f1420", "ГД-24104": "14064fbf-4335-11ef-b448-00155d7f1420%3A148d5959-4376-11ef-b448-00155d7f1420", "Гэм-24106": "d53322fa-4338-11ef-b448-00155d7f1420%3A629425ac-4375-11ef-b448-00155d7f1420"}
TEACHERS_DB = {"Сакулин Валерий Александрович": "000000376", "Мазитов Виктор Расульевич": "000000421", "Котельников Сергей Андреевич": "000000383", "Голубина Валентина Васильевна": "000000467", "Кабанов Александр Михайлович": "000000409", "Игумнова Юлия Олеговна": "000002912", "Тюжина Ирина Викторовна": "000002915"}
CLASSROOMS_DB = {"Толк5": "2355c22e-2bcd-11e7-b191-005056953b1b", "Ауд. 203": "67941c0b-ca51-11ee-b440-00155d7f0e19"}
DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

# --- SCHEDULE MANAGER ---
class ScheduleManager:
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None):
        key = f"data:v{CACHE_VERSION}:{t_type}:{t_val}:w{wo}"
        try:
            if await dao.exists(key): return json.loads(await dao.get(key))
        except Exception as e: logger.error(f"Redis get error: {e}")
        await dao.lpush('schedule_jobs', json.dumps({"week_offset": wo, "target_type": t_type, "target_value": t_val}))
        for _ in range(120):
            await asyncio.sleep(0.5)
            try:
                if await dao.exists(key): return json.loads(await dao.get(key))
            except Exception as e: logger.error(f"Redis poll error: {e}")
        return {}

sm = ScheduleManager()

# --- UTILS ---
class ScheduleStates(StatesGroup): viewing = State()

@asynccontextmanager
async def loading_animation(chat_id: int):
    async def _typing(chat_id):
        while True:
            try: await bot.send_chat_action(chat_id=chat_id, action="typing"), await asyncio.sleep(4)
            except asyncio.CancelledError: break
            except: break
    task = asyncio.create_task(_typing(chat_id))
    try: yield
    finally: task.cancel()

# --- UI & FORMATTING ---
def get_main_menu(val=None):
    kb = [[KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")], [KeyboardButton(text="🗓 Эта неделя"), KeyboardButton(text="➡️ След. неделя")], [KeyboardButton(text="🔄 Сбросить"), KeyboardButton(text="🧹 Очистить")]] if val else [[KeyboardButton(text="👥 Группы"), KeyboardButton(text="👩‍🏫 Преподаватели")], [KeyboardButton(text="🏫 Аудитории")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_day_pagination_kb(target_date: date):
    prev, next = (target_date - timedelta(days=1)).isoformat(), (target_date + timedelta(days=1)).isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Пред. день", callback_data=f"day_nav:{prev}"), InlineKeyboardButton(text="След. день ➡️", callback_data=f"day_nav:{next}")]])

def format_lesson(l: dict, t_type: str) -> str:
    subj, time, room, grp, teach = (l.get(k, 'Н/Д') for k in ['subject', 'time', 'room', 'group', 'teacher'])
    text = f"📖 <b>{subj}</b>\n   └ <code>{time}</code> | 🚪 <code>{room}</code> | "
    return text + (f"👥 {grp}" if t_type in ["teacher", "classroom"] else f"👨‍🏫 {teach}")

def fmt_day(day_date: date, lessons: list, t_type: str) -> str:
    day_name, date_str = DAYS_OF_WEEK[day_date.weekday()], day_date.strftime("%d.%m.%Y")
    text = f"<b>🗓 {day_name.upper()}</b> ({date_str})\n" + "─" * 24 + "\n\n"
    if not lessons: return text + "😴 Нет занятий"
    sorted_lessons = sorted(lessons, key=lambda x: x.get('time', '00:00'))
    return text + "\n\n".join([format_lesson(l, t_type) for l in sorted_lessons])

def fmt_week(s: dict, t_type: str) -> str:
    full_text = ""
    for day_name in DAYS_OF_WEEK[:6]:
        if d_str := s.get("_dates", {}).get(day_name):
            d_date, d_lessons = datetime.strptime(d_str, "%d.%m.%Y").date(), s.get(day_name, [])
            full_text += fmt_day(d_date, d_lessons, t_type) + "\n\n" + "═" * 24 + "\n\n"
    return full_text if full_text.strip() else "😴 На этой неделе занятий нет."

# --- HANDLERS ---
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear(), await m.answer("👋 Бот расписания готов!", reply_markup=get_main_menu())

@dp.message(F.text.in_({"👥 Группы", "👩‍🏫 Преподаватели", "🏫 Аудитории"}))
async def show_filter_menu(m: Message):
    t_type = "group" if m.text == "👥 Группы" else "teacher" if m.text == "👩‍🏫 Преподаватели" else "classroom"
    db = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}[t_type]
    btns = [InlineKeyboardButton(text=n, callback_data=f"fsel:{t_type}:{n}") for n in db]
    kb = InlineKeyboardMarkup(inline_keyboard=[btns[i:i+2] for i in range(0, len(btns), 2)])
    await m.answer("👇 Выберите:", reply_markup=kb)

@dp.callback_query(F.data.startswith("fsel:"))
async def cb_sel(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    _, t_type, t_val = c.data.split(":", 2)
    await state.set_state(ScheduleStates.viewing), await state.update_data(target_type=t_type, target_value=t_val)
    await c.message.answer(f"✅ Фильтр: <b>{t_val}</b>", parse_mode="HTML", reply_markup=get_main_menu(t_val)), await c.answer()

async def display_day_schedule(message: Message | CallbackQuery, state: FSMContext, target_date: date):
    data = await state.get_data()
    t_val, t_type = data.get("target_value"), data.get("target_type")
    chat_id = message.chat.id if isinstance(message, Message) else message.message.chat.id
    today = datetime.now().date()
    wo = ((target_date - timedelta(days=target_date.weekday())) - (today - timedelta(days=today.weekday()))).days // 7
    async with loading_animation(chat_id):
        week_s = await sm.fetch_schedule(wo, t_type, t_val)
    day_name = DAYS_OF_WEEK[target_date.weekday()]
    day_lessons = week_s.get(day_name, [])
    text, kb = fmt_day(target_date, day_lessons, t_type), get_day_pagination_kb(target_date)
    if isinstance(message, CallbackQuery):
        try: await message.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest: pass
        await message.answer()
    else: await message.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("day_nav:"))
async def cb_day_nav(c: CallbackQuery, state: FSMContext):
    await display_day_schedule(c, state, date.fromisoformat(c.data.split(":")[1]))

@dp.message(F.text.in_({"📅 Сегодня", "📆 Завтра"}), ScheduleStates.viewing)
async def handle_days(m: Message, state: FSMContext):
    offset = 1 if m.text == "📆 Завтра" else 0
    await display_day_schedule(m, state, datetime.now().date() + timedelta(days=offset))

@dp.message(F.text.in_({"🗓 Эта неделя", "➡️ След. неделя"}), ScheduleStates.viewing)
async def handle_weeks(m: Message, state: FSMContext):
    data, wo = await state.get_data(), 1 if m.text == "➡️ След. неделя" else 0
    async with loading_animation(m.chat.id):
        s = await sm.fetch_schedule(wo, data.get("target_type"), data.get("target_value"))
    text = fmt_week(s, data.get("target_type"))
    if len(text) > 4096:
        for i in range(0, len(text), 4096): await m.answer(text[i:i+4096], parse_mode="HTML")
    else: await m.answer(text, parse_mode="HTML")

@dp.message(F.text == "🧹 Очистить")
async def clear(m: Message, state: FSMContext):
    chat_id = m.chat.id
    key = f"msg_history:{chat_id}"
    ids = [int(x) for x in await dao.smembers(key)]
    
    if not ids:
        await m.answer("🧹 Чат уже чист.", reply_markup=get_main_menu())
        return

    # Удаляем сообщения пачками
    for i in range(0, len(ids), 100):
        batch = ids[i:i+100]
        try:
            await bot.delete_messages(chat_id, batch)
        except:
            for mid in batch:
                try: await bot.delete_message(chat_id, mid)
                except: continue
    
    await dao.delete(key)
    await m.answer("🧹 Чат очищен за последние 2 дня.", reply_markup=get_main_menu())

@dp.message(F.text == "🔄 Сбросить")
async def reset(m: Message, state: FSMContext):
    await state.clear(), await m.answer("🔄 Фильтры сброшены.", reply_markup=get_main_menu())

@dp.message(ScheduleStates.viewing)
async def require_filter_message(m: Message):
    await m.answer("⚠️ Пожалуйста, сначала выберите один из фильтров.", reply_markup=get_main_menu())

async def main():
    if PROXY_URL: logger.info(f"🌐 Используется прокси: {PROXY_URL}")
    await bot.delete_webhook(drop_pending_updates=True), await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Бот остановлен.")
    except Exception as e: logger.critical(f"Критическая ошибка: {e}", exc_info=True)
