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
PROXY_URL = os.getenv("PROXY_URL") 

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

# --- БАЗЫ ДАННЫХ ID ---
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

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
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
class UserRegistrationMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        if hasattr(event, "from_user") and event.from_user: save_user(event.from_user.id)
        return await handler(event, data)

dp.message.middleware(UserRegistrationMiddleware())
dp.callback_query.middleware(UserRegistrationMiddleware())

# --- REDIS DAO ---
class RedisDAO:
    def __init__(self):
        self.client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, decode_responses=True)
    async def connect(self): await self.client.ping()
    async def get(self, k):
        data = await self.client.get(k)
        return json.loads(data) if data else None
    async def lpush(self, k, v): await self.client.lpush(k, json.dumps(v, ensure_ascii=False))
    async def delete_many(self, pattern):
        keys = await self.client.keys(pattern)
        if keys: await self.client.delete(*keys)

dao = RedisDAO()

# --- МЕНЕДЖЕР РАСПИСАНИЯ ---
class ScheduleManager:
    async def init(self): await dao.connect()
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None):
        tid = f"{t_type}:{t_val}" if t_type and t_val else "default"
        key = f"data:v{CACHE_VERSION}:{tid}:w{wo}"
        cached = await dao.get(key)
        if cached: return cached
        await dao.lpush('schedule_jobs', {"week_offset": wo, "target_type": t_type, "target_value": t_val})
        for _ in range(120): # 60 sec timeout
            await asyncio.sleep(0.5)
            res = await dao.get(key)
            if res: return res
        return {}
    async def clear_cache(self):
        await dao.delete_many(f"data:v{CACHE_VERSION}:*")
        for f in os.listdir(CACHE_DIR):
            if f.endswith(".json"): os.remove(os.path.join(CACHE_DIR, f))

sm = ScheduleManager()

# --- UI & HANDLERS ---
def get_main_menu(val=None):
    if val:
        # Меню, когда фильтр УЖЕ выбран
        kb = [
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")],
            [KeyboardButton(text="🗓 Эта неделя"), KeyboardButton(text="➡️ След. неделя")],
            [KeyboardButton(text="🔄 Сбросить"), KeyboardButton(text="🧹 Очистить"), KeyboardButton(text="🙈 Скрыть")]
        ]
    else:
        # Меню, когда фильтр НЕ выбран
        kb = [
            [KeyboardButton(text="👥 Группы"), KeyboardButton(text="👩‍🏫 Преподаватели")],
            [KeyboardButton(text="🏫 Аудитории")]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_day_nav(di, wo):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🗓 Вся неделя", callback_data=f"showweek_{wo}"), InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_day_{di}_{wo}")]])

def get_week_nav(wo):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⏪ Пред.нед", callback_data=f"showweek_{wo-1}"), InlineKeyboardButton(text="След.нед ⏩", callback_data=f"showweek_{wo+1}")], [InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_week_{wo}")]])

def fmt_day(day, lessons, s, target_type=None):
    ds = s.get("_dates", {}).get(day, "")
    text = f"🗓 <b>{day.upper()} ({ds})</b>
" + "─"*20 + "

"
    if not lessons: return text + "😴 Нет занятий"
    for l in lessons:
        text += f"<b>{l['subject']}</b>
"
        text += f"   🕐 {l['time']} | 🏫 {l['room']}
"
        if target_type in ["teacher", "classroom"]:
            text += f"   👥 {l['group']}
"
        else:
            text += f"   👩‍🏫 {l['teacher']}
"
    return text

def fmt_week(s, wo):
    text = f"🗓 <b>НЕДЕЛЯ {wo}</b>
" + "─"*20 + "

"
    for day in DAYS_OF_WEEK[:6]:
        lessons = s.get(day, [])
        text += f"<b>{day.upper()}</b>: {len(lessons)} пар
"
        for l in lessons: text += f"   • {l['time']} | {l['subject']}
"
        text += "
"
    return text

@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("👋 Бот расписания готов к работе!", reply_markup=get_main_menu())

async def show_filter_menu(m: Message, target_type: str):
    db_map = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}
    title_map = {"group": "группу", "teacher": "преподавателя", "classroom": "аудиторию"}
    kb = [[InlineKeyboardButton(text=name, callback_data=f"fsel:{target_type}:{i}")] for i, name in enumerate(db_map[target_type].keys())]
    await m.answer(f"👇 Выберите {title_map[target_type]}:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.message(F.text == "👥 Группы")
async def btn_gr(m: Message): await show_filter_menu(m, "group")

@router.message(F.text == "👩‍🏫 Преподаватели")
async def btn_tr(m: Message): await show_filter_menu(m, "teacher")

@router.message(F.text == "🏫 Аудитории")
async def btn_cr(m: Message): await show_filter_menu(m, "classroom")

@router.callback_query(F.data.startswith("fsel:"))
async def cb_sel(c: CallbackQuery, state: FSMContext):
    _, t_type, idx = c.data.split(":")
    db = GROUPS_DB if t_type=="group" else TEACHERS_DB if t_type=="teacher" else CLASSROOMS_DB
    val = list(db.keys())[int(idx)]
    await state.update_data(target_type=t_type, target_value=val)
    await c.message.delete()
    await c.message.answer(f"✅ Фильтр: <b>{val}</b>", parse_mode="HTML", reply_markup=get_main_menu(val))
    await c.answer()

async def display_schedule(m: Message, state: FSMContext, is_week: bool, wo_offset: int = 0):
    data = await state.get_data()
    target_val = data.get("target_value")
    if not target_val:
        await m.answer("⚠️ Сначала выберите группу, преподавателя или аудиторию.", reply_markup=get_main_menu())
        return

    loading_msg = await m.answer("⏳ Загружаю расписание...")
    s = await sm.fetch_schedule(wo_offset, data.get("target_type"), target_val)
    await loading_msg.delete()

    if not s:
        await m.answer("⚠️ Не удалось загрузить расписание. Попробуйте позже.")
        return

    if is_week:
        await m.answer(fmt_week(s, wo_offset), parse_mode="HTML", reply_markup=get_week_nav(wo_offset))
    else:
        di = datetime.now().weekday()
        if wo_offset == 1: di = 0 
        await m.answer(fmt_day(DAYS_OF_WEEK[di], s.get(DAYS_OF_WEEK[di], []), s, data.get("target_type")), parse_mode="HTML", reply_markup=get_day_nav(di, wo_offset))

@router.message(F.text.in_({"📅 Сегодня", "📆 Завтра"}))
async def days(m: Message, state: FSMContext):
    wo = 1 if m.text == "📆 Завтра" and datetime.now().weekday() >= 5 else 0
    await display_schedule(m, state, is_week=False, wo_offset=wo)

@router.message(F.text.in_({"🗓 Эта неделя", "➡️ След. неделя"}))
async def weeks(m: Message, state: FSMContext):
    wo = 1 if m.text == "➡️ След. неделя" else 0
    await display_schedule(m, state, is_week=True, wo_offset=wo)

@router.message(F.text == "🧹 Очистить")
async def clear(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⠀
" * 40, parse_mode=None) 
    await m.answer("🧹 Чат очищен, фильтры сброшены.", reply_markup=get_main_menu())

@router.message(F.text == "🔄 Сбросить")
async def reset(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("🔄 Фильтры сброшены.", reply_markup=get_main_menu())

@router.message(F.text == "🙈 Скрыть")
async def hide(m: Message):
    await m.answer("⌨️ Клавиатура скрыта.", reply_markup=ReplyKeyboardRemove())

@router.callback_query(F.data.startswith("showday_"))
async def cb_day(c: CallbackQuery, state: FSMContext):
    di, wo = map(int, c.data.replace("showday_", "").split("_"))
    d = await state.get_data()
    s = await sm.fetch_schedule(wo, d.get("target_type"), d.get("target_value"))
    await c.message.edit_text(fmt_day(DAYS_OF_WEEK[di], s.get(DAYS_OF_WEEK[di], []), s, d.get("target_type")), reply_markup=get_day_nav(di, wo), parse_mode="HTML")

@router.callback_query(F.data.startswith("showweek_"))
async def cb_week(c: CallbackQuery, state: FSMContext):
    wo = int(c.data.replace("showweek_", ""))
    d = await state.get_data()
    s = await sm.fetch_schedule(wo, d.get("target_type"), d.get("target_value"))
    await c.message.edit_text(fmt_week(s, wo), reply_markup=get_week_nav(wo), parse_mode="HTML")

@router.callback_query(F.data.startswith("refresh_"))
async def cb_ref(c: CallbackQuery, state: FSMContext):
    await sm.clear_cache()
    await c.answer("⏳ Обновляю...")
    if "day" in c.data: await cb_day(c, state)
    else: await cb_week(c, state)

async def main():
    await sm.init()
    if PROXY_URL:
        logger.info(f"🌐 Используется прокси: {PROXY_URL}")
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except Exception as e: logger.critical(f"Global error: {e}")
