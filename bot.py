import os
import re
import json
import logging
import asyncio
import urllib.parse
import collections
from contextlib import asynccontextmanager
from datetime import datetime, date, timedelta, timezone
from typing import Dict, Any, AsyncGenerator
import psutil

from dotenv import load_dotenv # type: ignore
load_dotenv()

from aiogram import Bot, Dispatcher, F # type: ignore
from aiogram.client.session.aiohttp import AiohttpSession # type: ignore
from aiogram.client.session.middlewares.base import BaseRequestMiddleware # type: ignore
from aiogram.types import ( # type: ignore
    Message, CallbackQuery, InlineKeyboardButton,    
    InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton,
)
from aiogram.filters import CommandStart, Command # type: ignore
from aiogram.fsm.storage.memory import MemoryStorage # type: ignore
from aiogram.fsm.state import State, StatesGroup # type: ignore
from aiogram.fsm.context import FSMContext # type: ignore
from aiogram.exceptions import TelegramBadRequest # type: ignore
from aiogram.dispatcher.middlewares.base import BaseMiddleware # type: ignore
import redis.asyncio as redis # type: ignore
from secure_store import SecureStore

# ═══════════════════ НАСТРОЙКИ ═══════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

if not BOT_TOKEN:
    raise ValueError("⚠️ BOT_TOKEN не найден! Убедитесь, что он указан в .env файле или переменных окружения.")

DATA_DIR, CACHE_DIR, USERS_FILE = "data", "cache", os.path.join("data", "users.json")
CACHE_LIFETIME, CACHE_VERSION = 86400, 38
MSG_STORE_LIMIT = 172800 # 48 часов
ADMIN_IDS = [474095004]

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

STORAGE_PASSWORD = os.getenv("STORAGE_PASSWORD", "default_unsafe_password")
secure_store = SecureStore(os.path.join(DATA_DIR, "secure_users.enc"), STORAGE_PASSWORD)

dao = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, decode_responses=True)

# --- MAINTENANCE MODE ---
async def is_maintenance():
    return await dao.get("maintenance_mode") == "1"  

# --- TRACKING LOGIC ---
async def register_user(user_id: int):
    await dao.sadd("bot_users", user_id)

async def track_message(chat_id: int, message_id: int):
    key = f"msg_history:{chat_id}"
    await dao.sadd(key, message_id)
    await dao.expire(key, MSG_STORE_LIMIT)

async def broadcast(text: str):
    users = await dao.smembers("bot_users")
    for user_id in users:
        try:
            await bot.send_message(int(user_id), text, parse_mode="HTML")
            await asyncio.sleep(0.05) # Anti-flood   
        except Exception as e:
            logger.error(f"Failed to send broadcast to {user_id}: {e}")

class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):  
        try:
            user_id = event.from_user.id if event.from_user else 0
            if await is_maintenance() and user_id not in ADMIN_IDS:
                if isinstance(event, Message):       
                    await event.answer("🛠 <b>Ведутся технические работы.</b>\nБот временно недоступен. Пожалуйста, попробуйте позже.", parse_mode="HTML")      
                return
        except Exception as e:
            logger.error(f"Maintenance check failed: {e}")
        return await handler(event, data)

class IncomingMessageTracker(BaseMiddleware):        
    async def __call__(self, handler, event: Message, data: Dict[str, Any]):
        if getattr(event, "chat", None) and getattr(event, "message_id", None):
            try:
                await register_user(event.chat.id)
                await track_message(event.chat.id, event.message_id)
                logger.info(f"msg from {event.chat.id}: {event.text}")
            except Exception as e:
                logger.error(f"Tracking failed: {e}")
        return await handler(event, data)

class OutgoingMessageTracker(BaseRequestMiddleware): 
    async def __call__(self, make_request, bot, method):
        result = await make_request(bot, method)     
        if isinstance(result, Message):
            try:
                await track_message(result.chat.id, result.message_id) # type: ignore
            except Exception as e:
                logger.error(f"Outgoing tracking failed: {e}")
        return result

# --- BOT SETUP ---
session = AiohttpSession(proxy=PROXY_URL) if PROXY_URL else None
bot = Bot(token=BOT_TOKEN, session=session)
bot.session.middleware(OutgoingMessageTracker())     

dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(MaintenanceMiddleware())       
dp.callback_query.middleware(MaintenanceMiddleware())
dp.message.middleware(IncomingMessageTracker())      

# --- DATABASES ---
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
    "Эк-25109": "c52cf4a3-1542-11f0-b44a-00155d7f1420%3A06321270-5d88-11f0-b44a-00155d7f1420",
    "А-25101": "64345217-d3ec-11ef-b449-00155d7f1420%3A87999d48-5d7f-11f0-b44a-00155d7f1420",
    "Ит-25107": "4e6528d3-d3ef-11ef-b449-00155d7f1420%3A9a9bd9dc-5d84-11f0-b44a-00155d7f1420",
    "М-25102": "efdd4827-d3fb-11ef-b449-00155d7f1420%3Aa7f635af-5d85-11f0-b44a-00155d7f1420",
    "Т-25105": "8dd0b75a-d400-11ef-b449-00155d7f1420%3A690b7f2d-5d87-11f0-b44a-00155d7f1420",
    "Эн-25103": "3d685fd3-d402-11ef-b449-00155d7f1420%3A5dfec504-5d88-11f0-b44a-00155d7f1420",
    "Гд-25104": "8e4c58f1-d40a-11ef-b449-00155d7f1420%3A11b10f9e-5d82-11f0-b44a-00155d7f1420",
    "Гэм-25106": "ef68433a-d40c-11ef-b449-00155d7f1420%3A14e87d8c-5d84-11f0-b44a-00155d7f1420",
}

TEACHERS_DB = {
    "Сакулин Валерий Александрович": "000000376",
    "Мазитов Виктор Расульевич": "000000421",
    "Котельников Сергей Андреевич": "000000383",
    "Голубина Валентина Васильевна": "000000467",
    "Кабанов Александр Михайлович": "000000409",
    "Игумнова Юлия Олеговна": "000002912",
    "Тюжина Ирина Викторовна": "000002915",
    "Ивлев Андрей Дмитриевич": "000002261",
}

CLASSROOMS_DB = {
    "Толк5": "2355c22e-2bcd-11e7-b191-005056953b1b",
    "Ауд. 203": "67941c0b-ca51-11ee-b440-00155d7f0e19",
}
DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]        

# --- SCHEDULE MANAGER ---
class ScheduleManager:
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None) -> dict:
        key = f"data:v{CACHE_VERSION}:{t_type}:{t_val}:w{wo}"
        try:
            if await dao.exists(key): return json.loads(await dao.get(key))
        except Exception as e: logger.error(f"Redis get error: {e}")
        await dao.lpush('schedule_jobs', json.dumps({"week_offset": wo, "target_type": t_type, "target_value": t_val}))
        
        for _ in range(600): # 60 сек таймаут (0.1s интервал)
            await asyncio.sleep(0.1)
            try:
                if await dao.exists(key): return json.loads(await dao.get(key))
            except Exception as e: logger.error(f"Redis poll error: {e}")
        return {}

sm = ScheduleManager()

# --- UTILS ---
class ScheduleStates(StatesGroup): viewing = State() 



@asynccontextmanager
async def loading_animation(chat_id: int) -> AsyncGenerator[None, None]:
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
    if val:
        kb = [
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")],
            [KeyboardButton(text="🗓 Эта неделя"), KeyboardButton(text="➡️ След. неделя")],
            [KeyboardButton(text="🔙 Назад")]
        ]
    else:
        kb = [
            [KeyboardButton(text="🎓 Курс"), KeyboardButton(text="🔔 Моя подписка")],
            [KeyboardButton(text="👩‍🏫 Преподаватели"), KeyboardButton(text="🏫 Аудитории")],
            [KeyboardButton(text="🧹 Очистить")]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_day_pagination_kb(target_date: date):        
    prev, next = (target_date - timedelta(days=1)).isoformat(), (target_date + timedelta(days=1)).isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ Пред. день", callback_data=f"day_nav:{prev}"), InlineKeyboardButton(text="След. день ➡️", callback_data=f"day_nav:{next}")], [InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]])       

def format_lesson(l: dict, t_type: str) -> str:      
    subj, l_type, time, room, grp, teach = (l.get(k, 'Н/Д') for k in ['subject', 'type', 'time', 'room', 'group', 'teacher'])

    text = f"📖 <b>{subj}</b>\n"
    if l_type and l_type != 'Н/Д':
        text += f"   📝 <i>{l_type}</i>\n"
    text += f"   └ <code>{time}</code> | 🚪 <code>{room}</code>\n"

    if t_type == "group":
        text += f"   └ 👤 {teach}"
    elif t_type == "teacher":
        text += f"   └ 👥 {grp}"
    else: # classroom
        text += f"   └ 👥 {grp} | 👤 {teach}"     

    return text

def fmt_day(day_date: date, lessons: list, t_type: str) -> str:
    day_name, date_str = DAYS_OF_WEEK[day_date.weekday()], day_date.strftime("%d.%m.%Y")
    text = f"<b>📅 {day_name.upper()}</b> ({date_str})\n" + "─" * 24 + "\n\n"
    if not lessons: return text + "😴 Нет занятий"   
    sorted_lessons = sorted(lessons, key=lambda x: x.get('time', '00:00'))
    return text + "\n\n".join([format_lesson(l, t_type) for l in sorted_lessons])

def fmt_week(s: dict, t_type: str) -> str:
    full_text = ""
    for day_name in DAYS_OF_WEEK[:6]: # type: ignore
        if d_str := s.get("_dates", {}).get(day_name):
            d_date, d_lessons = datetime.strptime(d_str, "%d.%m.%Y").date(), s.get(day_name, [])
            full_text += fmt_day(d_date, d_lessons, t_type) + "\n\n" + "═" * 24 + "\n\n" # type: ignore
    return full_text if full_text.strip() else "😴 На этой неделе занятий нет." # type: ignore

# --- ADMIN HANDLERS ---
@dp.message(Command("stop"), F.from_user.id.in_(ADMIN_IDS))
async def admin_stop(m: Message):
    await dao.set("maintenance_mode", "1")
    msg = "🛠 <b>Бот уходит на технические работы.</b>\nВременно недоступен."
    await m.answer(f"🔴 {msg}")
    asyncio.create_task(broadcast(msg))

@dp.message(Command("start_admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_start(m: Message):
    await dao.delete("maintenance_mode")
    msg = "✅ <b>Технические работы завершены.</b>\nБот снова онлайн и готов к работе!"
    await m.answer(f"🟢 {msg}")
    asyncio.create_task(broadcast(msg))

@dp.message(Command("admin"), F.from_user.id.in_(ADMIN_IDS))
async def admin_panel(m: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="admin:status")],
        [InlineKeyboardButton(text="🔄 Обновить бота (git pull)", callback_data="admin:update")]
    ])
    await m.answer("🔧 <b>Панель администратора</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin:"), F.from_user.id.in_(ADMIN_IDS))
async def admin_actions(c: CallbackQuery):
    action = c.data.split(":")[1]
    if action == "status":
        cpu = psutil.cpu_percent()
        ram = psutil.virtual_memory().percent
        try:
            redis_ping = await dao.ping()
            redis_status = "✅ Работает" if redis_ping else "❌ Сбой"
        except:
            redis_status = "❌ Сбой"
            
        workers = await dao.llen('schedule_jobs')
        
        text = (f"📊 <b>Статус контейнера:</b>\n\n"
                f"<b>CPU:</b> {cpu}%\n"
                f"<b>RAM:</b> {ram}%\n"
                f"<b>Redis БД:</b> {redis_status}\n"
                f"<b>Кэш версия:</b> {CACHE_VERSION}\n"
                f"<b>Очередь воркеров:</b> {workers} задач")
        await c.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
    elif action == "update":
        await c.message.edit_text("🔄 Начинаю оповещение пользователей и подготовку к обновлению...", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
        
        async def run_update_sequence():
            await dao.set("update_in_progress", "1")
            await dao.set("update_admin_id", str(c.from_user.id))
            await broadcast("⚙️ <b>Внимание!</b>\nСервер обновляется. Бот будет недоступен несколько минут.")
            await dao.set("bot_update_trigger", "1")
            
        asyncio.create_task(run_update_sequence())
    elif action == "back":
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статус системы", callback_data="admin:status")],
            [InlineKeyboardButton(text="🔄 Обновить бота (git pull)", callback_data="admin:update")]
        ])
        await c.message.edit_text("🔧 <b>Панель администратора</b>", reply_markup=kb, parse_mode="HTML")
    try: await c.answer()
    except: pass

# --- HANDLERS ---
@dp.message(CommandStart())
async def start(m: Message, state: FSMContext):      
    await register_user(m.from_user.id)
    if m.from_user:
        profile_data = {
            "first_name": m.from_user.first_name,
            "last_name": m.from_user.last_name,
            "username": m.from_user.username,
            "language_code": m.from_user.language_code,
            "is_premium": getattr(m.from_user, 'is_premium', False),
            "updated_at": datetime.now(timezone.utc).isoformat()
        }
        secure_store.save_user(str(m.from_user.id), profile_data)
        
    await state.clear()
    await m.answer("👋 <b>Бот расписания готов к работе!</b>", reply_markup=get_main_menu(), parse_mode="HTML")
    await show_subscription_menu(m)


@dp.message(F.text == "🔔 Моя подписка")
async def show_subscription_menu(m: Message):
    subbed_group = await dao.hget("user_subs", str(m.from_user.id))
    db = GROUPS_DB
    btns = [InlineKeyboardButton(text=n, callback_data=f"sub:{i}") for i, n in enumerate(db)]
    
    inline_kb = []
    for i in range(0, len(btns), 2):
        inline_kb.append(btns[i:i+2])
    inline_kb.append([InlineKeyboardButton(text="🔕 Отписаться", callback_data="sub:unsubscribe")])
    inline_kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")])
    kb = InlineKeyboardMarkup(inline_keyboard=inline_kb)
    
    text = "🗓 <b>Утренняя рассылка (08:00 МСК+2)</b>\n\n"
    if subbed_group:
        group_name = next((k for k, v in db.items() if v == subbed_group), "Неизвестно")
        text += f"✅ Текущая подписка: <b>{group_name}</b>\n\nВыберите новую или нажмите Отписаться:"
    else:
        text += "❌ Вы не подписаны.\nВыберите группу из списка ниже:"
        
    await m.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.callback_query(F.data.startswith("sub:"))
async def cb_sub(c: CallbackQuery):
    idx = c.data.split(":")[1]
    if idx == "unsubscribe":
        await dao.hdel("user_subs", str(c.from_user.id))
        await c.message.edit_text("🔕 Вы отписались от утренней рассылки.")
    else:
        db = GROUPS_DB
        gid = list(db.keys())[int(idx)]
        gval = db[gid]
        await dao.hset("user_subs", str(c.from_user.id), gval)
        await c.message.edit_text(f"✅ Вы успешно подписались на утреннюю рассылку для группы <b>{gid}</b>!\nКаждое утро в 08:00 бот будет присылать вам расписание на день.", parse_mode="HTML")
    try: await c.answer()
    except: pass

@dp.message(F.text.in_({"👩‍🏫 Преподаватели", "🏫 Аудитории"}))
async def show_filter_menu(m: Message):
    t_type = "teacher" if m.text == "👩‍🏫 Преподаватели" else "classroom"
    db = {"teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}[t_type]
    btns = [InlineKeyboardButton(text=n, callback_data=f"fsel:{t_type}:{i}") for i, n in enumerate(db)]   
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn] for btn in btns] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]])
    await m.answer("👇 Выберите:", reply_markup=kb)  

@dp.message(F.text == "🎓 Курс")
async def show_courses_menu(m: Message):
    btns = [
        [InlineKeyboardButton(text="1️⃣ Первый курс", callback_data="course:25")],
        [InlineKeyboardButton(text="2️⃣ Второй курс", callback_data="course:24")],
        [InlineKeyboardButton(text="3️⃣ Третий курс", callback_data="course:23")],
        [InlineKeyboardButton(text="4️⃣ Четвертый курс", callback_data="course:22")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]
    ]
    await m.answer("🎓 Выберите курс:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data.startswith("course:"))
async def cb_course(c: CallbackQuery):
    await c.message.delete()
    prefix = c.data.split(":")[1]
    
    filtered_groups = []
    for i, name in enumerate(GROUPS_DB.keys()):
        # Ищем числа в названии группы (например, Ит-24107 -> 24)
        match = re.search(r'\d+', name)
        if match and match.group(0).startswith(prefix):
            filtered_groups.append((i, name))
            
    if not filtered_groups:
        await c.message.answer("😔 Группы для этого курса не найдены.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]]))
    else:
        btns = [InlineKeyboardButton(text=n, callback_data=f"fsel:group:{i}") for i, n in filtered_groups]
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn] for btn in btns] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]])
        await c.message.answer("👇 Выберите группу:", reply_markup=kb)
    try: await c.answer()
    except: pass

@dp.callback_query(F.data.startswith("fsel:"))       
async def cb_sel(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    _, t_type, idx = c.data.split(":", 2)
    db = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}[t_type]
    t_val = list(db.keys())[int(idx)]
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
    day_lessons = week_s.get(day_name, []) # type: ignore
    is_error = not week_s or "_error" in week_s # type: ignore

    if is_error:
        text = "⚠️ <b>Ошибка загрузки.</b>\nУниверситетский сайт не ответил вовремя или произошла ошибка парсинга. Попробуйте еще раз."
    else:
        text = fmt_day(target_date, day_lessons, t_type)

    kb = get_day_pagination_kb(target_date)

    if isinstance(message, CallbackQuery):
        try:
            await message.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
        except TelegramBadRequest:
            await message.message.answer(text, parse_mode="HTML", reply_markup=kb)
        try: await message.answer()
        except: pass
    else:
        await message.answer(text, parse_mode="HTML", reply_markup=kb)

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
    text = fmt_week(s, data.get("target_type")) # type: ignore      
    
    messages = []
    current_msg = ""
    for chunk in text.split("═" * 24 + "\n\n"):
        if not chunk.strip(): continue
        if len(current_msg) + len(chunk) + 26 > 4096:
            if current_msg:
                messages.append(current_msg)
            current_msg = chunk
        else:
            if current_msg:
                current_msg += "═" * 24 + "\n\n" + chunk
            else:
                current_msg = chunk
    if current_msg:
        messages.append(current_msg)
        
    for msg in messages:
        await m.answer(msg, parse_mode="HTML")

async def clear_chat_history(chat_id: int):
    ids = list(set(await dao.smembers(f"msg_history:{chat_id}")))
    ids = [int(x) for x in ids]
    for i in range(0, len(ids), 100):
        try: await bot.delete_messages(chat_id, ids[i:i+100]) # type: ignore
        except:
            for mid in ids[i:i+100]: # type: ignore
                try: await bot.delete_message(chat_id, mid)
                except: continue
    await dao.delete(f"msg_history:{chat_id}")

@dp.message(F.text == "🧹 Очистить")
async def clear(m: Message, state: FSMContext):      
    await clear_chat_history(m.chat.id)
    await state.clear()
    await m.answer("🧹 Чат очищен.", reply_markup=get_main_menu())

@dp.callback_query(F.data == "cancel_menu")
async def cb_cancel_menu(c: CallbackQuery, state: FSMContext):
    await state.clear()
    try: await c.message.delete()
    except: pass
    await c.message.answer("🔙 Главное меню", reply_markup=get_main_menu())
    try: await c.answer()
    except: pass

@dp.message(F.text.in_({"🔄 Сбросить", "🔙 Назад"}))
async def reset(m: Message, state: FSMContext):
    await clear_chat_history(m.chat.id)
    await state.clear(), await m.answer("🔙 Возврат в главное меню.", reply_markup=get_main_menu())

@dp.message(ScheduleStates.viewing)
async def require_filter_message(m: Message, state: FSMContext):        
    data = await state.get_data()
    val = data.get("target_value")
    await m.answer("⚠️ Пожалуйста, используйте кнопки меню для навигации.", reply_markup=get_main_menu(val))     

async def daily_scheduler():
    tz = timezone(timedelta(hours=5))
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=8, minute=0, second=0, microsecond=0)
        if now >= target:
            target += timedelta(days=1)
        wait_seconds = (target - now).total_seconds()
        
        await asyncio.sleep(wait_seconds)
        
        try:
            subs = await dao.hgetall("user_subs")
            groups_to_users = collections.defaultdict(list)
            for uid, gid in subs.items():
                groups_to_users[gid].append(uid)
            
            today = datetime.now(tz).date()
            wo = 0
            
            for gid, uids in groups_to_users.items():
                week_s = await sm.fetch_schedule(wo, "group", gid)
                day_name = DAYS_OF_WEEK[today.weekday()]
                day_lessons = week_s.get(day_name, [])
                is_error = not week_s or "_error" in week_s
                
                if is_error:
                    continue
                
                text = f"🌅 <b>Доброе утро! Расписание на сегодня:</b>\n\n"
                text += fmt_day(today, day_lessons, "group")
                
                for uid in uids:
                    try:
                        await bot.send_message(uid, text, parse_mode="HTML")
                        await asyncio.sleep(0.05)
                    except:
                        pass
        except Exception as e:
            logger.error(f"Scheduler failed: {e}")

async def notify_on_startup():
    try:
        if await dao.get("update_in_progress") == "1":
            await dao.delete("update_in_progress")
            admin_id = await dao.get("update_admin_id")
            if admin_id:
                try: await bot.send_message(int(admin_id), "🛠 <b>ОТЧЕТ:</b> Сервер успешно обновлен и запущен!", parse_mode="HTML")
                except: pass
                await dao.delete("update_admin_id")
            
            await broadcast("✅ <b>Сервер обновлен и снова работает!</b>\nВсе системы в норме.")
    except Exception as e:
        logger.error(f"Notify on startup failed: {e}")

async def main():
    if PROXY_URL: logger.info(f"🌐 Используется прокси: {PROXY_URL}")
    asyncio.create_task(daily_scheduler())
    asyncio.create_task(notify_on_startup())
    await bot.delete_webhook(drop_pending_updates=True), await dp.start_polling(bot)

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Бот остановлен.")
    except Exception as e: logger.critical(f"Критическая ошибка: {e}", exc_info=True)
