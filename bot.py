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
DATA_DIR = "data"
CACHE_DIR = "cache"
USERS_FILE = os.path.join(DATA_DIR, "users.json")
MAINTENANCE_FILE = os.path.join(DATA_DIR, "maintenance.json")

CACHE_LIFETIME = 86400 
CACHE_VERSION = 32 

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Сессия с прокси
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

ADMIN_IDS = [474095004] 

# --- БАЗЫ ДАННЫХ ---
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

DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
SHORT_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAY_EMOJI = {"Понедельник": "1️⃣", "Вторник": "2️⃣", "Среда": "3️⃣", "Четверг": "4️⃣", "Пятница": "5️⃣", "Суббота": "6️⃣", "Воскресенье": "7️⃣"}

# --- ФУНКЦИИ ХРАНИЛИЩА ---
def is_maintenance():
    if not os.path.exists(MAINTENANCE_FILE): return False
    try:
        with open(MAINTENANCE_FILE, "r") as f: return json.load(f).get("is_active", False)
    except: return False

def set_maintenance(state: bool):
    with open(MAINTENANCE_FILE, "w") as f: json.dump({"is_active": state}, f)

def get_users():
    if not os.path.exists(USERS_FILE): return set()
    try:
        with open(USERS_FILE, "r") as f: return set(json.load(f))
    except: return set()

def save_user(user_id):
    users = get_users()
    if user_id not in users:
        users.add(user_id)
        with open(USERS_FILE, "w") as f: json.dump(list(users), f)

# --- MIDDLEWARES ---
class LatestMessageOnlyMiddleware(BaseMiddleware):
    def __init__(self):
        super().__init__()
        self.latest = collections.defaultdict(int)
    async def __call__(self, handler, event, data):
        if not isinstance(event, Message): return await handler(event, data)
        self.latest[event.chat.id] = event.message_id
        await asyncio.sleep(0.2)
        if self.latest[event.chat.id] == event.message_id: return await handler(event, data)

class RegMiddleware(BaseMiddleware):
    async def __call__(self, h, e, d):
        if hasattr(e, "from_user") and e.from_user: save_user(e.from_user.id)
        return await h(e, d)

dp.update.middleware(LatestMessageOnlyMiddleware())
dp.message.middleware(RegMiddleware())

# --- REDIS DAO ---
class RedisDAO:
    def __init__(self):
        self.client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, decode_responses=True)
        self.ok = False
    async def connect(self):
        try: await self.client.ping(); self.ok = True
        except: self.ok = False
    async def get(self, k): return json.loads(await self.client.get(k)) if self.ok and await self.client.get(k) else None
    async def lpush(self, k, v):
        if self.ok: await self.client.lpush(k, json.dumps(v, ensure_ascii=False))

dao = RedisDAO()

# --- MANAGER ---
class ScheduleManager:
    async def init(self): await dao.connect()
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None):
        tid = f"{t_type}:{t_val}" if t_type and t_val else "default"
        key = f"data:v{CACHE_VERSION}:{tid}:w{wo}"
        cached = await dao.get(key)
        if cached: return cached
        await dao.lpush('schedule_jobs', {"week_offset": wo, "target_type": t_type, "target_value": t_val})
        for _ in range(120):
            await asyncio.sleep(0.5)
            res = await dao.get(key)
            if res: return res
        return {}
    async def clear_cache(self):
        if dao.ok:
            keys = await dao.client.keys(f"data:v{CACHE_VERSION}:*")
            if keys: await dao.client.delete(*keys)
        for f in os.listdir(CACHE_DIR): os.remove(os.path.join(CACHE_DIR, f))

sm = ScheduleManager()

# --- КЛАВИАТУРЫ ---
def get_main_menu(val=None):
    if not val:
        kb = [[KeyboardButton(text="👥 Группы"), KeyboardButton(text="👩‍🏫 Преподаватели")], [KeyboardButton(text="🏫 Аудитории")], [KeyboardButton(text="🧹 Очистить"), KeyboardButton(text="🙈 Скрыть")]]
    else:
        kb = [[KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")], [KeyboardButton(text="🗓 Эта неделя"), KeyboardButton(text="➡️ След. неделя")], [KeyboardButton(text="📋 Выбрать день"), KeyboardButton(text="📆 Выбрать неделю")], [KeyboardButton(text="🔄 Сбросить"), KeyboardButton(text="🧹 Очистить")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_day_nav(di, wo):
    nav = []
    if di > 0: nav.append(InlineKeyboardButton(text=f"⬅️ {SHORT_DAYS[di-1]}", callback_data=f"showday_{di-1}_{wo}"))
    nav.append(InlineKeyboardButton(text=f"📅 {SHORT_DAYS[di]}", callback_data="noop"))
    if di < 5: nav.append(InlineKeyboardButton(text=f"{SHORT_DAYS[di+1]} ➡️", callback_data=f"showday_{di+1}_{wo}"))
    return InlineKeyboardMarkup(inline_keyboard=[nav, [InlineKeyboardButton(text="🗓 Вся неделя", callback_data=f"showweek_{wo}")], [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_day_{di}_{wo}")]])

def get_week_nav(wo):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏪ Пред.нед", callback_data=f"showweek_{wo-1}"), InlineKeyboardButton(text="След.нед ⏩", callback_data=f"showweek_{wo+1}")], [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_week_{wo}")]])

# --- ФОРМАТИРОВАНИЕ ---
def fmt_day(day, lessons, s, wo):
    ds = s.get("_dates", {}).get(day, "")
    text = f"🗓 <b>{day.upper()} ({ds})</b>\n" + "─"*20 + "\n\n"
    if not lessons: return text + "😴 Нет занятий"
    for i, l in enumerate(lessons, 1):
        text += f"<b>{i}. {l['subject']}</b>\n   🕐 {l['time']}\n   🏫 {l['room']} | 👩‍🏫 {l['teacher']}\n\n"
    return text

def fmt_week(s, wo):
    text = f"🗓 <b>НЕДЕЛЯ {wo}</b>\n" + "─"*20 + "\n\n"
    for day in DAYS_OF_WEEK[:6]:
        lessons = s.get(day, [])
        text += f"{DAY_EMOJI[day]} <b>{day.upper()}</b>: {len(lessons)} пар\n"
        for l in lessons: text += f"   • {l['time']} | {l['subject']}\n"
        text += "\n"
    return text

# --- ОБРАБОТЧИКИ ---
@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 Бот расписания готов к работе!", reply_markup=get_main_menu())

@router.message(F.text == "👥 Группы")
async def gr(m: Message):
    kb = [[InlineKeyboardButton(text=g, callback_data=f"fsel:group:{i}")] for i, g in enumerate(GROUPS_DB.keys())]
    await m.answer("👥 Выберите группу:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.message(F.text == "👩‍🏫 Преподаватели")
async def tr(m: Message):
    kb = [[InlineKeyboardButton(text=t, callback_data=f"fsel:teacher:{i}")] for i, t in enumerate(TEACHERS_DB.keys())]
    await m.answer("👩‍🏫 Выберите преподавателя:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.message(F.text == "🏫 Аудитории")
async def cr(m: Message):
    kb = [[InlineKeyboardButton(text=c, callback_data=f"fsel:classroom:{i}")] for i, c in enumerate(CLASSROOMS_DB.keys())]
    await m.answer("🏫 Выберите аудиторию:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("fsel:"))
async def sel(c: CallbackQuery, state: FSMContext):
    _, t, idx = c.data.split(":")
    db = GROUPS_DB if t=="group" else TEACHERS_DB if t=="teacher" else CLASSROOMS_DB
    val = list(db.keys())[int(idx)]
    await state.update_data(target_type=t, target_value=val)
    await c.message.answer(f"✅ Выбрано: <b>{val}</b>", parse_mode="HTML", reply_markup=get_main_menu(val))
    await c.answer()

@router.message(F.text.in_({"📅 Сегодня", "📆 Завтра"}))
async def days(m: Message, state: FSMContext):
    d = await state.get_data()
    if not d.get("target_value"): return
    wd = datetime.now().weekday()
    wo, di = (0, wd) if m.text == "📅 Сегодня" else ((1, 0) if wd >= 5 else (0, wd + 1))
    s = await sm.fetch_schedule(wo, d.get("target_type"), d.get("target_value"))
    await m.answer(fmt_day(DAYS_OF_WEEK[di], s.get(DAYS_OF_WEEK[di], []), s, wo), reply_markup=get_day_nav(di, wo), parse_mode="HTML")

@router.message(F.text.in_({"🗓 Эта неделя", "➡️ След. неделя"}))
async def weeks(m: Message, state: FSMContext):
    d = await state.get_data()
    if not d.get("target_value"): return
    wo = 0 if m.text == "🗓 Эта неделя" else 1
    s = await sm.fetch_schedule(wo, d.get("target_type"), d.get("target_value"))
    await m.answer(fmt_week(s, wo), reply_markup=get_week_nav(wo), parse_mode="HTML")

@router.message(F.text == "📋 Выбрать день")
async def sel_day(m: Message):
    kb = [[InlineKeyboardButton(text=d, callback_data=f"showday_{i}_0")] for i, d in enumerate(DAYS_OF_WEEK[:6])]
    await m.answer("📋 Выберите день:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.message(F.text == "📆 Выбрать неделю")
async def sel_week(m: Message):
    kb = [[InlineKeyboardButton(text=f"Неделя {i}", callback_data=f"showweek_{i}")] for i in range(-1, 3)]
    await m.answer("📆 Выберите неделю:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.message(F.text == "🧹 Очистить")
async def clear(m: Message, state: FSMContext):
    await state.clear()
    for i in range(3):
        try: await m.bot.delete_messages(m.chat.id, list(range(m.message_id - 99, m.message_id + 1)))
        except: pass
    await m.answer("🧹 Чат очищен.", reply_markup=get_main_menu())

@router.message(F.text == "🔄 Сбросить")
async def reset(m: Message, state: FSMContext):
    await state.clear(); await m.answer("🔄 Фильтры сброшены.", reply_markup=get_main_menu())

@router.callback_query(F.data.startswith("showday_"))
async def cb_day(c: CallbackQuery, state: FSMContext):
    di, wo = map(int, c.data.replace("showday_", "").split("_"))
    d = await state.get_data()
    s = await sm.fetch_schedule(wo, d.get("target_type"), d.get("target_value"))
    await c.message.edit_text(fmt_day(DAYS_OF_WEEK[di], s.get(DAYS_OF_WEEK[di], []), s, wo), reply_markup=get_day_nav(di, wo), parse_mode="HTML")

@router.callback_query(F.data.startswith("showweek_"))
async def cb_week(c: CallbackQuery, state: FSMContext):
    wo = int(c.data.replace("showweek_", ""))
    d = await state.get_data()
    s = await sm.fetch_schedule(wo, d.get("target_type"), d.get("target_value"))
    await c.message.edit_text(fmt_week(s, wo), reply_markup=get_week_nav(wo), parse_mode="HTML")

@router.callback_query(F.data.startswith("refresh_"))
async def cb_ref(c: CallbackQuery, state: FSMContext):
    await sm.clear_cache(); await c.answer("⏳ Обновляю...")
    if "day" in c.data: await cb_day(c, state)
    else: await cb_week(c, state)

@router.message(Command("stop"))
async def stop_cmd(m: Message):
    if m.from_user.id in ADMIN_IDS:
        set_maintenance(True); await m.answer("🛠 Режим техработ ВКЛЮЧЕН.")

@router.message(F.text.startswith("/broadcast"))
async def bc_cmd(m: Message):
    if m.from_user.id in ADMIN_IDS:
        txt = m.text.replace("/broadcast ", ""); users = get_users()
        for u in users:
            try: await bot.send_message(u, txt)
            except: pass
        await m.answer(f"📢 Рассылка завершена ({len(users)} чел.)")

async def main():
    await sm.init()
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except: pass
