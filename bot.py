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
    LabeledPrice, PreCheckoutQuery, SuccessfulPayment, BufferedInputFile
)
from aiogram.filters import CommandStart, Command # type: ignore
from aiogram.fsm.storage.memory import MemoryStorage # type: ignore
from aiogram.fsm.state import State, StatesGroup # type: ignore
from aiogram.fsm.context import FSMContext # type: ignore
from aiogram.exceptions import TelegramBadRequest # type: ignore
from aiogram.dispatcher.middlewares.base import BaseMiddleware # type: ignore
import redis.asyncio as redis # type: ignore
from secure_store import SecureStore
from db_manager import db_manager
from ai_manager import get_ai_response, create_openrouter_key
import vpn_manager
import io
import qrcode

# ═══════════════════ НАСТРОЙКИ ═══════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")

if not BOT_TOKEN:
    raise ValueError("⚠️ BOT_TOKEN не найден! Убедитесь, что он указан в .env файле или переменных окружения.")

DATA_DIR, CACHE_DIR, USERS_FILE = "data", "cache", os.path.join("data", "users.json")
CACHE_LIFETIME, CACHE_VERSION = 86400, 39
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

async def broadcast(text: str, save_key: str = None):
    users = await dao.smembers("bot_users")
    for user_id in users:
        try:
            msg = await bot.send_message(int(user_id), text, parse_mode="HTML")
            if save_key:
                await dao.hset(save_key, user_id, msg.message_id)
            await asyncio.sleep(0.05) # Anti-flood   
        except Exception as e:
            logger.error(f"Failed to send broadcast to {user_id}: {e}")

class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):  
        try:
            user_id = event.from_user.id if event.from_user else 0
            if await is_maintenance() and user_id not in ADMIN_IDS:
                if isinstance(event, Message):
                    old_mid = await dao.get(f"maint_msg:{user_id}")
                    if old_mid:
                        try: await bot.delete_message(user_id, int(old_mid))
                        except: pass
                    msg = await event.answer("🛠 <b>Ведутся технические работы.</b>\nБот временно недоступен. Пожалуйста, попробуйте позже.", parse_mode="HTML")      
                    await dao.setex(f"maint_msg:{user_id}", 3600, msg.message_id)
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
                
                user = getattr(event, "from_user", None)
                if user:
                    profile_data = {
                        "first_name": user.first_name,
                        "last_name": user.last_name,
                        "username": user.username,
                        "language_code": user.language_code,
                        "is_premium": getattr(user, 'is_premium', False),
                        "updated_at": datetime.now(timezone.utc).isoformat()
                    }
                    secure_store.save_user(str(user.id), profile_data)
                    # Register/update user in PostgreSQL
                    await db_manager.register_or_update_user(
                        telegram_id=user.id,
                        username=user.username,
                        group_name=await dao.hget("user_subs", str(user.id))
                    )
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

class AntiFloodMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        user_id = getattr(event.from_user, "id", 0)
        if user_id and user_id not in ADMIN_IDS:
            if not await dao.set(f"flood_lock:{user_id}", "1", ex=1, nx=True):
                if isinstance(event, CallbackQuery):
                    try: await event.answer("⚠️ Не так быстро!")
                    except: pass
                return
        return await handler(event, data)

dp = Dispatcher(storage=MemoryStorage())
dp.message.middleware(IncomingMessageTracker())      
dp.message.middleware(MaintenanceMiddleware())       
dp.callback_query.middleware(MaintenanceMiddleware())
dp.message.middleware(AntiFloodMiddleware())
dp.callback_query.middleware(AntiFloodMiddleware())      

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
    "А-23101": "71b1e8a9-1979-11ee-86ac-005056953b1b%3A57124d43-1bca-11ee-86ac-005056953b1b",
    "М-23102": "0cfbe051-196e-11ee-86ac-005056953b1b%3Ade1d410d-1bbf-11ee-86ac-005056953b1b",
    "Т-23105": "7a4b0dc4-1998-11ee-86ac-005056953b1b%3A63885cf0-245e-11ee-92f9-005056953b1b",
    "Ит-23107 гр.1": "2f95ecc0-1bd1-11ee-86ac-005056953b1b%3Aa933615b-6dd7-11f0-b44a-00155d7f1420",
    "Ит-23107 гр.2": "7900f4dd-6e9f-11ef-b448-00155d7f1420%3A90401ef5-6ea0-11ef-b448-00155d7f1420",
    "Гэм-23106": "92a56d28-1bc7-11ee-86ac-005056953b1b%3A0c22bf22-1bc4-11ee-86ac-005056953b1b",
    "Гд-23104": "2ffaff2f-1a69-11ee-86ac-005056953b1b%3A0bb2e3d9-1bca-11ee-86ac-005056953b1b",
    "Гэм-22106": "03092314-09a5-11ed-b935-005056953b1b%3Aa1b619de-0c15-11ed-b935-005056953b1b",
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
    "Гавриленко Никита Сергеевич": "000001833",
}

CLASSROOMS_DB = {
    "Ауд. 300": "2355c22e-2bcd-11e7-b191-005056953b1b",
    "Ауд. 203": "67941c0b-ca51-11ee-b440-00155d7f0e19",
}
DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]        

async def get_groups_db() -> dict:
    db = dict(GROUPS_DB)
    try:
        redis_db = await dao.hgetall("db_groups")
        if redis_db: db.update(redis_db)
    except Exception as e: logger.error(f"Error fetching groups from Redis: {e}")
    return db

async def get_teachers_db() -> dict:
    db = dict(TEACHERS_DB)
    try:
        redis_db = await dao.hgetall("db_teachers")
        if redis_db: db.update(redis_db)
    except Exception as e: logger.error(f"Error fetching teachers from Redis: {e}")
    return db

async def get_classrooms_db() -> dict:
    db = dict(CLASSROOMS_DB)
    try:
        redis_db = await dao.hgetall("db_classrooms")
        if redis_db: db.update(redis_db)
    except Exception as e: logger.error(f"Error fetching classrooms from Redis: {e}")
    return db

# --- SCHEDULE MANAGER ---
class ScheduleManager:
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None) -> dict:
        tz = timezone(timedelta(hours=5))
        mon = datetime.now(tz).date() - timedelta(days=datetime.now(tz).weekday()) + timedelta(weeks=wo)
        sd = mon.strftime("%d.%m.%Y")
        key = f"data:v{CACHE_VERSION}:{sd}:{t_type}:{t_val}"
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

class UserStates(StatesGroup):
    waiting_for_evening_time = State()
    waiting_for_morning_time = State()
    waiting_for_teacher_search = State()
    waiting_for_classroom_search = State()
    waiting_for_ai_prompt = State()
    waiting_for_ai_key = State()
    waiting_for_activation_key = State()

class StarostStates(StatesGroup):
    waiting_for_password = State()
    waiting_for_new_pass = State()
    waiting_for_name = State()
    waiting_for_course = State()
    waiting_for_group = State()
    waiting_for_message = State()
    waiting_for_message_all = State()
    waiting_for_hw_day = State()
    waiting_for_hw_lesson = State()
    waiting_for_hw_text = State()
    waiting_for_hw_delete = State()
    waiting_for_poll_question = State()
    waiting_for_poll_options = State()

class AdminStates(StatesGroup):
    waiting_for_broadcast_message = State()
    waiting_for_event_title = State()
    waiting_for_event_desc = State()
    waiting_for_event_date = State()
    waiting_for_event_link = State()
    waiting_for_channel_name = State()
    waiting_for_channel_link = State()
    waiting_for_channel_cat = State()

def get_greeting() -> str:
    tz = timezone(timedelta(hours=5))
    h = datetime.now(tz).hour
    if 5 <= h < 12: return "🌅 <b>Доброе утро!</b>"
    elif 12 <= h < 17: return "☀️ <b>Добрый день!</b>"
    elif 17 <= h < 22: return "🌆 <b>Добрый вечер!</b>"
    else: return "🌙 <b>Доброй ночи!</b>"



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
            [KeyboardButton(text="⭐ В избранное"), KeyboardButton(text="🔙 Назад")]
        ]
    else:
        kb = [
            [KeyboardButton(text="📅 Мое расписание"), KeyboardButton(text="🔔 Моя подписка")],
            [KeyboardButton(text="🤖 ИИ-Ассистент"), KeyboardButton(text="🏫 Экосистема")],
            [KeyboardButton(text="⭐ Избранное"), KeyboardButton(text="💻 Толк")],
            [KeyboardButton(text="👩‍🏫 Преподаватели"), KeyboardButton(text="🏫 Аудитории")],
            [KeyboardButton(text="🧹 Очистить")]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

def get_submenu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔙 Назад"), KeyboardButton(text="🧹 Очистить")]
        ],
        resize_keyboard=True
    )

def get_day_pagination_kb(target_date: date):        
    prev, next = (target_date - timedelta(days=1)).isoformat(), (target_date + timedelta(days=1)).isoformat()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⬅️ Пред. день", callback_data=f"day_nav:{prev}"), 
         InlineKeyboardButton(text="След. день ➡️", callback_data=f"day_nav:{next}")], 
        [InlineKeyboardButton(text="📅 Экспорт iCal", callback_data="ical:export"),
         InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]
    ])       

async def format_lesson(l: dict, t_type: str, day_name: str, group_name: str) -> str:      
    subj, l_type, time, room, grp, teach = (l.get(k, 'Н/Д') for k in ['subject', 'type', 'time', 'room', 'group', 'teacher'])
    link = l.get('link')

    text = f"📖 <b>{subj}</b>\n"
    if l_type and l_type != 'Н/Д':
        text += f"   📝 <i>{l_type}</i>\n"
    text += f"   └ <code>{time}</code> | 🚪 <code>{room}</code>\n"
    
    if link:
        text += f"   └ 💻 <a href='{link}'>Подключиться онлайн</a>\n"

    if t_type == "group":
        text += f"   └ 👤 {teach}"
        try:
            hw = await dao.hget(f"homework:{group_name}", f"{day_name}:{time}")
            if hw: text += f"\n   ✍️ <b>Д/З:</b> <i>{hw}</i>"
        except Exception as e: logger.error(f"Homework read error: {e}")
    elif t_type == "teacher":
        text += f"   └ 👥 {grp}"
    else: # classroom
        text += f"   └ 👥 {grp} | 👤 {teach}"     

    return text

async def fmt_day(day_date: date, lessons: list, t_type: str, group_name: str = "") -> str:
    day_name, date_str = DAYS_OF_WEEK[day_date.weekday()], day_date.strftime("%d.%m.%Y")
    text = f"<b>📅 {day_name.upper()}</b> ({date_str})\n" + "─" * 24 + "\n\n"
    if not lessons: return text + "😴 Нет занятий"   
    sorted_lessons = sorted(lessons, key=lambda x: x.get('time', '00:00'))
    formatted_lessons = []
    for l in sorted_lessons:
        g_name = group_name if t_type == "group" else l.get('group', '')
        formatted_lessons.append(await format_lesson(l, t_type, day_name, g_name))
    return text + "\n\n".join(formatted_lessons)

async def fmt_week(s: dict, t_type: str, group_name: str = "") -> str:
    full_text = ""
    for day_name in DAYS_OF_WEEK[:6]: # type: ignore
        if d_str := s.get("_dates", {}).get(day_name):
            d_date, d_lessons = datetime.strptime(d_str, "%d.%m.%Y").date(), s.get(day_name, [])
            full_text += await fmt_day(d_date, d_lessons, t_type, group_name) + "\n\n" + "═" * 24 + "\n\n" # type: ignore
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
async def admin_panel(m: Message, state: FSMContext):
    await state.clear()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Статус системы", callback_data="admin:status"),
         InlineKeyboardButton(text="📈 Детальная статистика", callback_data="admin:detailed_stats")],
        [InlineKeyboardButton(text="🕒 Время сервера", callback_data="admin:server_time")],
        [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin:broadcast_prompt")],
        [InlineKeyboardButton(text="🧪 Тест рассылки расписания", callback_data="admin:test_schedule_broadcast")],
        [InlineKeyboardButton(text="🚀 Запустить утреннюю рассылку (ВСЕМ)", callback_data="admin:force_broadcast")],
        [InlineKeyboardButton(text="⏳ Отложенная рассылка (через 1 мин)", callback_data="admin:delayed_broadcast")],
        [InlineKeyboardButton(text="🔄 Сбросить кэш и обновить (git pull)", callback_data="admin:update")],
        [InlineKeyboardButton(text="⚡ Предзагрузить кэш (на эту и след. неделю)", callback_data="admin:preload_cache")]
    ])
    await m.answer("🛠 <b>Панель управления</b>", reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data.startswith("admin:"), F.from_user.id.in_(ADMIN_IDS))
async def admin_actions(c: CallbackQuery, state: FSMContext):
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
    elif action == "detailed_stats":
        users = list(await dao.smembers("bot_users"))
        total_users = len(users)
        
        subs = await dao.hgetall("user_subs")
        subbed_users = len(subs)
        
        group_counts = collections.Counter(subs.values())
        top_groups = "\n".join([f"  • {grp}: {count} чел." for grp, count in group_counts.most_common(10)])
        if not top_groups: top_groups = "  Нет подписок."
            
        morn_times = await dao.hgetall("user_morning_time")
        morn_counts = collections.Counter(morn_times.values())
        top_morn = "\n".join([f"  • {t}: {count} чел." for t, count in morn_counts.most_common(5)])
        
        db_g_size = await dao.hlen("db_groups")
        db_t_size = await dao.hlen("db_teachers")
        db_c_size = await dao.hlen("db_classrooms")
        
        text = (f"📈 <b>Детальная статистика бота:</b>\n\n"
                f"👤 <b>Всего пользователей:</b> {total_users}\n"
                f"🔔 <b>С активной подпиской:</b> {subbed_users}\n\n"
                f"🎓 <b>Топ-10 популярных групп:</b>\n{top_groups}\n\n"
                f"🌅 <b>Утренние рассылки (топ время):</b>\n{top_morn}\n\n"
                f"📂 <b>Размер динамических БД:</b>\n"
                f"  • Групп: {db_g_size}\n"
                f"  • Преподавателей: {db_t_size}\n"
                f"  • Аудиторий: {db_c_size}")
                
        await c.message.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
    elif action == "update":
        await c.message.edit_text("🔄 Начинаю оповещение пользователей и подготовку к обновлению...", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
        
        async def run_update_sequence():
            await dao.set("update_in_progress", "1")
            await dao.set("update_admin_id", str(c.from_user.id))
            await dao.delete("update_msgs")
            await broadcast("⚙️ <b>Внимание!</b>\nСервер обслуживается. Бот будет недоступен несколько минут.", save_key="update_msgs")
            await dao.set("bot_update_trigger", "1")
            
        asyncio.create_task(run_update_sequence())
    elif action == "preload_cache":
        await c.message.edit_text("⏳ <b>Добавляю все расписания в очередь парсеров...</b>", parse_mode="HTML")
        try:
            import time
            count = 0
            now = time.time()
            data_dbs = [("group", await get_groups_db()), ("teacher", await get_teachers_db()), ("classroom", await get_classrooms_db())]
            for t_type, db in data_dbs:
                for name, tid in db.items():
                    for wo in [0, 1]:
                        job = {
                            "week_offset": wo,
                            "target_type": t_type,
                            "target_value": name
                        }
                        await dao.rpush("schedule_jobs", json.dumps(job))
                        count += 1
            await c.message.edit_text(f"✅ <b>Отправлено в очередь: {count}</b>\nВоркеры в фоновом режиме загрузят расписания (текущая и следующая неделя) в кэш! (Около 2 минут)", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]), parse_mode="HTML")
            
            async def notify_when_done(admin_id: int):
                await asyncio.sleep(3) # Ждем, чтобы воркеры точно подхватили список
                while True:
                    left = await dao.llen("schedule_jobs")
                    if left == 0: break
                    await asyncio.sleep(2)
                try: await bot.send_message(admin_id, "✅ <b>Фуух, готово!</b>\nАбсолютно все расписания кэшированы и готовы к молниеносной выдаче. ⚡", parse_mode="HTML")
                except: pass
            
            asyncio.create_task(notify_when_done(c.from_user.id))
        except Exception as e:
            await c.message.edit_text(f"❌ Ошибка прогрева: {e}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
    elif action == "broadcast_prompt":
        await c.message.edit_text("📢 <b>Отправьте сообщение для рассылки всем пользователям бота.</b>\n\nБот скопирует всё: фото, видео, голосовые сообщения и текст.\nЧтобы отменить рассылку, нажмите кнопку ниже.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="admin:back")]]))
        await state.set_state(AdminStates.waiting_for_broadcast_message)
    elif action == "test_schedule_broadcast":
        await c.message.edit_text("⏳ <b>Формирую тестовую рассылку для вас...</b>", parse_mode="HTML")
        tz = timezone(timedelta(hours=5))
        try:
            subs = await dao.hgetall("user_subs")
            admin_gid = subs.get(str(c.from_user.id))
            
            if not admin_gid:
                await c.message.edit_text("❌ Вы не подписаны на утреннюю рассылку.\nПерейдите в меню '🔔 Моя подписка', выберите любую группу и попробуйте снова.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
            else:
                today = datetime.now(tz).date()
                week_s = await sm.fetch_schedule(0, "group", admin_gid)
                day_name = DAYS_OF_WEEK[today.weekday()]
                day_lessons = week_s.get(day_name, [])
                is_error = not week_s or "_error" in week_s
                
                if is_error:
                    error_msg = week_s.get('_error', 'Таймаут ожидания (очередь перегружена)') if week_s else 'Таймаут ожидания'
                    await c.message.edit_text(f"❌ Ошибка при получении расписания.\nПричина: <b>{error_msg}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
                else:
                    text = f"🧪 <b>ТЕСТ УТРЕННЕЙ РАССЫЛКИ</b>\n\n{get_greeting()} <b>Расписание на сегодня:</b>\n\n"
                    text += await fmt_day(today, day_lessons, "group", admin_gid)
                    await bot.send_message(c.from_user.id, text, parse_mode="HTML")
                    await c.message.edit_text("✅ <b>Тестовая рассылка успешно отправлена!</b>\nПроверьте новые сообщения от бота.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
        except Exception as e:
            logger.error(f"Test scheduler failed: {e}")
            await c.message.edit_text(f"❌ Ошибка: {e}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
            
    elif action == "force_broadcast":
        await c.message.edit_text("🚀 <b>Запускаю массовую рассылку расписания...</b>\nПожалуйста, подождите. Это может занять некоторое время.", parse_mode="HTML")
        count = await run_morning_broadcast()
        await c.message.edit_text(f"✅ <b>Рассылка завершена!</b>\nОтправлено сообщений: <b>{count}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
        
    elif action == "delayed_broadcast":
        await c.message.edit_text("⏳ <b>Таймер запущен.</b>\nМассовая рассылка начнется ровно через 60 секунд...", parse_mode="HTML")
        await asyncio.sleep(60)
        count = await run_morning_broadcast()
        await c.message.edit_text(f"✅ <b>Отложенная рассылка завершена!</b>\nОтправлено сообщений: <b>{count}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))

    elif action == "server_time":
        tz = timezone(timedelta(hours=5))
        now = datetime.now(tz)
        await c.message.edit_text(f"🕒 <b>Текущее время на сервере (Екатеринбург):</b>\n<code>{now.strftime('%Y-%m-%d %H:%M:%S')}</code>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))

    elif action == "back":
        await state.clear()
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📊 Статус системы", callback_data="admin:status"),
             InlineKeyboardButton(text="📈 Детальная статистика", callback_data="admin:detailed_stats")],
            [InlineKeyboardButton(text="🕒 Время сервера", callback_data="admin:server_time")],
            [InlineKeyboardButton(text="📢 Сделать рассылку", callback_data="admin:broadcast_prompt")],
            [InlineKeyboardButton(text="🧪 Тест рассылки расписания", callback_data="admin:test_schedule_broadcast")],
            [InlineKeyboardButton(text="🚀 Запустить утреннюю рассылку (ВСЕМ)", callback_data="admin:force_broadcast")],
            [InlineKeyboardButton(text="⏳ Отложенная рассылка (через 1 мин)", callback_data="admin:delayed_broadcast")],
            [InlineKeyboardButton(text="🔄 Сбросить кэш и обновить (git pull)", callback_data="admin:update")],
            [InlineKeyboardButton(text="⚡ Предзагрузить кэш (на эту и след. неделю)", callback_data="admin:preload_cache")]
        ])
        await c.message.edit_text("🛠 <b>Панель управления</b>", reply_markup=kb, parse_mode="HTML")
    try: await c.answer()
    except: pass

@dp.callback_query(F.data == "sub:morning_time")
async def cb_sub_morning_time(c: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="07:00", callback_data="set_morn:07:00"),
         InlineKeyboardButton(text="07:30", callback_data="set_morn:07:30"),
         InlineKeyboardButton(text="08:00", callback_data="set_morn:08:00")],
        [InlineKeyboardButton(text="08:30", callback_data="set_morn:08:30"),
         InlineKeyboardButton(text="09:00", callback_data="set_morn:09:00")],
        [InlineKeyboardButton(text="🔕 Отключить", callback_data="set_morn:off")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]
    ])
    await c.message.edit_text("🌅 <b>Утренняя рассылка (на сегодня)</b>\n\nВыберите время из кнопок <b>ИЛИ</b> напишите желаемое время в формате <b>ЧЧ:ММ</b> прямо в чат:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(UserStates.waiting_for_morning_time)
    try: await c.answer()
    except: pass

@dp.message(UserStates.waiting_for_morning_time)
async def user_set_morning_time(m: Message, state: FSMContext):
    if re.match(r'^([0-1][0-9]|2[0-3]):[0-5][0-9]$', m.text):
        await dao.hset("user_morning_time", str(m.from_user.id), m.text)
        await state.clear()
        await m.answer(f"✅ Время рассылки на сегодня успешно установлено на <b>{m.text}</b>!", parse_mode="HTML")
        await show_subscription_time_menu(m)
    else:
        await m.answer("❌ <b>Неверный формат!</b>\nПожалуйста, введите время в формате <b>ЧЧ:ММ</b>.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("set_morn:"))
async def cb_set_morning_time_save(c: CallbackQuery, state: FSMContext):
    time_val = c.data.split(":", 1)[1]
    if time_val == "off":
        await dao.hset("user_morning_time", str(c.from_user.id), "Отключено")
        await c.answer("Рассылка на сегодня отключена")
    else:
        await dao.hset("user_morning_time", str(c.from_user.id), time_val)
        await c.answer(f"Время установлено на {time_val}")
    await state.clear()
    await c.message.delete()
    await show_subscription_time_menu(c.message, user_id=str(c.from_user.id))

@dp.callback_query(F.data == "sub:evening_time")
async def cb_sub_evening_time(c: CallbackQuery, state: FSMContext):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="18:00", callback_data="set_ev:18:00"),
         InlineKeyboardButton(text="19:00", callback_data="set_ev:19:00"),
         InlineKeyboardButton(text="20:00", callback_data="set_ev:20:00")],
        [InlineKeyboardButton(text="21:00", callback_data="set_ev:21:00"),
         InlineKeyboardButton(text="22:00", callback_data="set_ev:22:00")],
        [InlineKeyboardButton(text="🔕 Отключить", callback_data="set_ev:off")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]
    ])
    await c.message.edit_text("🕒 <b>Настройка вечерней рассылки (на завтра)</b>\n\nВыберите время из кнопок <b>ИЛИ</b> напишите желаемое время в формате <b>ЧЧ:ММ</b> прямо в чат:", reply_markup=kb, parse_mode="HTML")
    await state.set_state(UserStates.waiting_for_evening_time)
    try: await c.answer()
    except: pass

@dp.message(UserStates.waiting_for_evening_time)
async def user_set_evening_time(m: Message, state: FSMContext):
    if re.match(r'^([0-1][0-9]|2[0-3]):[0-5][0-9]$', m.text):
        await dao.hset("user_evening_time", str(m.from_user.id), m.text)
        await state.clear()
        await m.answer(f"✅ Время рассылки на завтра успешно установлено на <b>{m.text}</b>!", parse_mode="HTML")
        await show_subscription_time_menu(m)
    else:
        await m.answer("❌ <b>Неверный формат!</b>\nПожалуйста, введите время в формате <b>ЧЧ:ММ</b> (например, <code>20:30</code>). Часы от 00 до 23, минуты от 00 до 59.", parse_mode="HTML")

@dp.callback_query(F.data.startswith("set_ev:"))
async def cb_set_evening_time_save(c: CallbackQuery, state: FSMContext):
    time_val = c.data.split(":", 1)[1]
    if time_val == "off":
        await dao.hdel("user_evening_time", str(c.from_user.id))
        await c.answer("Рассылка на завтра отключена")
    else:
        await dao.hset("user_evening_time", str(c.from_user.id), time_val)
        await c.answer(f"Время установлено на {time_val}")
    await state.clear()
    await c.message.delete()
    await show_subscription_time_menu(c.message, user_id=str(c.from_user.id))

async def run_evening_broadcast(target_time: str):
    users = await dao.hgetall("user_subs")
    user_times = await dao.hgetall("user_evening_time")
    tomorrow = datetime.now(timezone(timedelta(hours=5))).date() + timedelta(days=1)
    count = 0
    for user_id, group_name in users.items():
        if user_times.get(user_id) != target_time:
            continue
        try:
            week_s = await sm.fetch_schedule(0, "group", group_name)
            day_lessons = week_s.get(DAYS_OF_WEEK[tomorrow.weekday()], [])
            if day_lessons:
                text = f"{get_greeting()} <b>Расписание на завтра, {tomorrow.strftime('%d.%m')}:</b>\n\n" + await fmt_day(tomorrow, day_lessons, "group", group_name)
                await bot.send_message(int(user_id), text, parse_mode="HTML")
                count += 1
            await asyncio.sleep(0.05)
        except: pass
    return count

async def check_schedule_changes():
    try:
        subs = await dao.hgetall("user_subs")
        if not subs: return
        active_groups = set(subs.values())
        logger.info(f"Checking schedule changes for {len(active_groups)} groups...")
        for group_name in active_groups:
            tz = timezone(timedelta(hours=5))
            today = datetime.now(tz).date()
            mon = today - timedelta(days=today.weekday())
            sd = mon.strftime("%d.%m.%Y")
            cache_key = f"data:v{CACHE_VERSION}:{sd}:group:{group_name}"
            old_schedule_str = await dao.get(cache_key)
            if not old_schedule_str:
                await sm.fetch_schedule(0, "group", group_name)
                continue
            old_schedule = json.loads(old_schedule_str)
            if "_error" in old_schedule: continue
            await dao.delete(cache_key)
            new_schedule = await sm.fetch_schedule(0, "group", group_name)
            if not new_schedule or "_error" in new_schedule:
                await dao.set(cache_key, old_schedule_str, ex=CACHE_LIFETIME)
                continue
            old_clean = {k: v for k, v in old_schedule.items() if k in DAYS_OF_WEEK}
            new_clean = {k: v for k, v in new_schedule.items() if k in DAYS_OF_WEEK}
            if old_clean != new_clean:
                logger.info(f"Schedule changed for group {group_name}!")
                target_users = [uid for uid, gid in subs.items() if gid == group_name]
                changed_days = [day for day in DAYS_OF_WEEK[:6] if old_clean.get(day) != new_clean.get(day)]
                msg = (f"🔔 <b>Внимание! Расписание группы {group_name} изменилось.</b>\n\n"
                       f"Изменения коснулись дней: {', '.join(changed_days)}.\n"
                       f"Используйте кнопку <b>«📅 Мое расписание»</b> для просмотра.")
                for uid in target_users:
                    try:
                        await bot.send_message(int(uid), msg, parse_mode="HTML")
                        await asyncio.sleep(0.05)
                    except Exception as e:
                        logger.error(f"Failed to notify user {uid} of change: {e}")
    except Exception as e:
        logger.error(f"Error checking schedule changes: {e}")

async def main_scheduler():
    while True:
        now_dt = datetime.now(timezone(timedelta(hours=5)))
        now = now_dt.strftime("%H:%M")
        await asyncio.gather(
            run_morning_broadcast(now),
            run_evening_broadcast(now)
        )
        # Check changes at 09:00, 12:00, 15:00, 18:00, 21:00
        if now_dt.minute == 0 and now_dt.hour in [9, 12, 15, 18, 21]:
            asyncio.create_task(check_schedule_changes())
        # Sleep for exactly 60 seconds to avoid multiple triggers within the same minute
        await asyncio.sleep(60)

async def copy_message_broadcast(from_chat_id: int, message_id: int):
    users = await dao.smembers("bot_users")
    for user_id in users:
        try:
            await bot.copy_message(chat_id=int(user_id), from_chat_id=from_chat_id, message_id=message_id)
            await asyncio.sleep(0.05) # Anti-flood
        except Exception as e:
            logger.error(f"Failed to copy broadcast to {user_id}: {e}")

@dp.message(AdminStates.waiting_for_broadcast_message, F.from_user.id.in_(ADMIN_IDS))
async def admin_broadcast_process(m: Message, state: FSMContext):
    await state.clear()
    await m.answer("⏳ <b>Начинаю рассылку...</b>", parse_mode="HTML")
    asyncio.create_task(copy_message_broadcast(m.chat.id, m.message_id))
    await m.answer("✅ <b>Рассылка запущена в фоновом режиме!</b>", parse_mode="HTML")

# --- HANDLERS ---
@dp.message(F.text.in_(["💻 Толк", "Толк"]))
async def talk_links(m: Message):
    msg = await m.answer("🎥 Открываю ссылки Толк...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="Толк 1", url="https://tu-ugmk.ktalk.ru/jiydkhlxmj94")],
        [InlineKeyboardButton(text="Толк 2", url="https://tu-ugmk.ktalk.ru/uiwmi2bn1khb")],
        [InlineKeyboardButton(text="Толк 3", url="https://tu-ugmk.ktalk.ru/gwj9tt76y0ow")],
        [InlineKeyboardButton(text="Толк 4", url="https://tu-ugmk.ktalk.ru/djkdcyfdh198")],
        [InlineKeyboardButton(text="Толк 5", url="https://tu-ugmk.ktalk.ru/n3us6a2ekxli")]
    ])
    await m.answer("🎥 <b>Ссылки на онлайн-комнаты Толк:</b>\nВыберите нужную комнату для подключения:", reply_markup=kb, parse_mode="HTML")

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
        # Register user in PostgreSQL
        await db_manager.register_or_update_user(
            telegram_id=m.from_user.id,
            username=m.from_user.username,
            group_name=await dao.hget("user_subs", str(m.from_user.id))
        )
        
    await state.clear()
    await m.answer("👋 <b>Бот расписания готов к работе!</b>", reply_markup=get_main_menu(), parse_mode="HTML")
    await show_subscription_time_menu(m)


@dp.message(F.text == "🔔 Моя подписка")
async def handle_sub_time_menu_message(m: Message, state: FSMContext):
    await state.clear()
    msg = await m.answer("🔔 Открываю настройки подписки...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    await show_subscription_time_menu(m)

async def show_subscription_time_menu(m: Message | CallbackQuery, user_id: str = None):
    uid = user_id or str(m.from_user.id)
    user_row = await db_manager.get_user(int(uid))
    
    if not user_row:
        await db_manager.register_or_update_user(int(uid), m.from_user.username if m.from_user else None)
        user_row = await db_manager.get_user(int(uid))
        
    group_name = user_row['group_name'] if user_row and user_row['group_name'] else "❌ Не выбрана"
    vpn_enabled = user_row['vpn_enabled'] if user_row else False
    vpn_expires_at = user_row.get('vpn_expires_at') if user_row else None
    ai_model = user_row['ai_model'] if user_row else 'gpt-4o-mini'
    has_key = bool(user_row['custom_ai_key']) if user_row else False
    ai_balance = user_row['ai_balance'] if user_row else 0
    ai_expires_at = user_row.get('ai_expires_at') if user_row else None
    
    # Format VPN status
    if vpn_enabled:
        vpn_status = "✅ Активна"
        if vpn_expires_at:
            vpn_status += f" (до {vpn_expires_at.strftime('%d.%m.%Y')})"
    else:
        vpn_status = "❌ Не активна"
        
    # Format AI status
    ai_key_status = "✅ Установлен" if has_key else "❌ Не установлен"
    ai_expiry_str = ""
    if ai_expires_at:
        ai_expiry_str = f" (до {ai_expires_at.strftime('%d.%m.%Y')})"
        
    # Get notification times
    morn_time = await dao.hget("user_morning_time", str(uid)) or "08:00"
    eve_time = await dao.hget("user_evening_time", str(uid)) or "Отключено"
    
    text = (
        "🔔 <b>Мой профиль и подписки</b>\n\n"
        f"🎓 <b>Ваша группа:</b> <code>{group_name}</code>\n\n"
        f"🔌 <b>WireGuard VPN:</b>\n"
        f"• Статус: <b>{vpn_status}</b>\n\n"
        f"🤖 <b>ИИ-Ассистент:</b>\n"
        f"• Баланс запросов: <b>{ai_balance}</b>{ai_expiry_str}\n\n"
        f"🕒 <b>Ежедневная рассылка расписания:</b>\n"
        f"• Утро: <code>{morn_time}</code>\n"
        f"• Вечер: <code>{eve_time}</code>"
    )
    
    kb_rows = []
    
    # Group row
    kb_rows.append([InlineKeyboardButton(text="🎓 Выбрать/Изменить группу", callback_data="sub:change_group")])
    
    # VPN controls row
    if vpn_enabled:
        kb_rows.append([
            InlineKeyboardButton(text="📁 Скачать WG файл", callback_data="vpn:get_file"),
            InlineKeyboardButton(text="🖼 Показать QR-код", callback_data="vpn:get_qr")
        ])
        kb_rows.append([InlineKeyboardButton(text="❌ Отключить VPN", callback_data="sub:disable_vpn")])
    else:
        kb_rows.append([InlineKeyboardButton(text="🔌 Подключить VPN", callback_data="sub:buy_vpn_only_direct")])
        
    # Buy subscription row
    kb_rows.append([InlineKeyboardButton(text="💳 Купить подписку", callback_data="sub:buy_menu")])
    
    # Daily notification setup row
    kb_rows.append([
        InlineKeyboardButton(text="🌅 Настроить Утро", callback_data="sub:morning_time"),
        InlineKeyboardButton(text="🌙 Настроить Вечер", callback_data="sub:evening_time")
    ])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    
    if isinstance(m, CallbackQuery):
        await m.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await m.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "sub:change_group")
async def cb_sub_change_group(c: CallbackQuery):
    await show_courses_menu(c)
    await c.answer()

@dp.callback_query(F.data == "sub:buy_menu")
async def cb_sub_buy_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🤖 Купить отдельно ИИ", callback_data="sub:buy_ai_menu")],
        [InlineKeyboardButton(text="🔌 Купить отдельный VPN", callback_data="sub:buy_vpn_only_menu")],
        [InlineKeyboardButton(text="📦 Купить всё вместе (VPN + ИИ)", callback_data="sub:buy_bundle_menu")],
        [InlineKeyboardButton(text="🔙 Назад в подписки", callback_data="sub:back_to_menu")]
    ])
    text = (
        "💳 <b>Выбор варианта подписки:</b>\n\n"
        "Вы можете приобрести услуги по отдельности или в выгодных пакетах:\n\n"
        "1. <b>ИИ-Ассистент (отдельно):</b> доступ к передовым языковым моделям.\n"
        "2. <b>WireGuard VPN (отдельно):</b> стабильный и безопасный интернет.\n"
        "3. <b>Всё вместе (Выгодный пакет):</b> VPN и ИИ-запросы со скидкой."
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "sub:buy_ai_menu")
async def cb_sub_buy_ai_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 150 Стандарт ИИ (400 ⭐)", callback_data="ai:buy_requests")],
        [InlineKeyboardButton(text="💳 30 Премиум ИИ (500 ⭐)", callback_data="ai:buy_premium")],
        [InlineKeyboardButton(text="🔙 Назад в меню покупок", callback_data="sub:buy_menu")]
    ])
    text = (
        "🤖 <b>Приобретение запросов ИИ (отдельно):</b>\n\n"
        "Выберите пакет запросов:\n"
        "1. <b>150 стандартных запросов</b> — 400 ⭐\n"
        "2. <b>30 премиум запросов</b> — 500 ⭐\n\n"
        "При покупке вам будет автоматически сгенерирован и привязан персональный API-ключ OpenRouter со сроком действия 30 дней."
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "sub:buy_vpn_only_menu")
async def cb_sub_buy_vpn_only_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 VPN на 30 дней (100 ⭐)", callback_data="vpn:buy_only")],
        [InlineKeyboardButton(text="🔙 Назад в меню покупок", callback_data="sub:buy_menu")]
    ])
    text = (
        "🔌 <b>Подписка на WireGuard VPN (отдельно):</b>\n\n"
        "• Срок действия: <b>30 дней</b>\n"
        "• Цена: <b>100 ⭐</b>\n\n"
        "Обеспечивает надежный и быстрый доступ к зарубежным ресурсам."
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "sub:buy_bundle_menu")
async def cb_sub_buy_bundle_menu(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎁 Тест: VPN + 10 Премиум (Бесплатно)", callback_data="vpn:activate_test_free")],
        [InlineKeyboardButton(text="💳 VPN + 150 Стандарт (500 ⭐)", callback_data="vpn:buy_standard")],
        [InlineKeyboardButton(text="💳 VPN + 30 Премиум (600 ⭐)", callback_data="vpn:buy_premium")],
        [InlineKeyboardButton(text="🔙 Назад в меню покупок", callback_data="sub:buy_menu")]
    ])
    text = (
        "📦 <b>Купить всё вместе (ИИ + VPN):</b>\n\n"
        "Выберите выгодный пакет:\n"
        "• <b>Тест: VPN + 10 Премиум ИИ</b> — Бесплатно (для проверки)\n"
        "1. <b>VPN + 150 Стандарт ИИ</b> — 500 ⭐\n"
        "2. <b>VPN + 30 Премиум ИИ</b> — 600 ⭐\n\n"
        "Все тарифы действуют 30 дней с момента покупки."
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "sub:buy_vpn_only")
async def cb_sub_buy_vpn_only(c: CallbackQuery):
    await cb_sub_buy_vpn_only_menu(c)

@dp.callback_query(F.data == "sub:buy_vpn_only_direct")
async def cb_sub_buy_vpn_only_direct(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 VPN на 30 дней (100 ⭐)", callback_data="vpn:buy_only")],
        [InlineKeyboardButton(text="🔙 Назад в подписки", callback_data="sub:back_to_menu")]
    ])
    text = (
        "🔌 <b>Подписка на WireGuard VPN (отдельно):</b>\n\n"
        "• Срок действия: <b>30 дней</b>\n"
        "• Цена: <b>100 ⭐</b>\n\n"
        "Обеспечивает надежный и быстрый доступ к зарубежным ресурсам."
    )
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "sub:back_to_menu")
async def cb_sub_back_to_menu(c: CallbackQuery):
    await show_subscription_time_menu(c)
    await c.answer()

@dp.callback_query(F.data == "sub:disable_vpn")
async def cb_sub_disable_vpn(c: CallbackQuery):
    uid = c.from_user.id
    user_row = await db_manager.get_user(uid)
    await db_manager.set_user_vpn(uid, enabled=False)
    
    try:
        if user_row and user_row['vpn_key'] and vpn_manager.VPN_SSH_HOST:
            import base64
            from cryptography.hazmat.primitives.asymmetric import x25519
            from cryptography.hazmat.primitives import serialization
            
            priv_key_match = re.search(r'PrivateKey\s*=\s*([a-zA-Z0-9+/=]+)', user_row['vpn_key'])
            if priv_key_match:
                priv_key_b64 = priv_key_match.group(1)
                priv_bytes = base64.b64decode(priv_key_b64)
                private_key = x25519.X25519PrivateKey.from_private_bytes(priv_bytes)
                public_key = private_key.public_key()
                pub_bytes = public_key.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw
                )
                pub_key_b64 = base64.b64encode(pub_bytes).decode('utf-8')
                
                import asyncssh
                async with asyncssh.connect(vpn_manager.VPN_SSH_HOST, username=vpn_manager.VPN_SSH_USER, password=vpn_manager.VPN_SSH_PASSWORD, known_hosts=None) as conn:
                    await conn.run(f"sudo wg set wg0 peer {pub_key_b64} remove")
                    await conn.run(f"sudo sed -i '/{pub_key_b64}/,+2d' /etc/wireguard/wg0.conf")
    except Exception as e:
        logger.error(f"Failed to remove WG peer on server for {uid}: {e}")
        
    await c.answer("VPN успешно отключен", show_alert=True)
    await show_subscription_time_menu(c)

@dp.message(F.text == "📅 Мое расписание")
async def show_my_schedule(m: Message, state: FSMContext):
    subbed_group = await dao.hget("user_subs", str(m.from_user.id))
    if not subbed_group:
        await m.answer("❌ Сначала выберите вашу группу в меню <b>«🎓 Моя группа»</b>.", parse_mode="HTML")
        return
        
    await state.set_state(ScheduleStates.viewing)
    await state.update_data(target_type="group", target_value=subbed_group)
    msg = await m.answer(f"📅 Расписание для группы <b>{subbed_group}</b>\nВыберите день:", parse_mode="HTML", reply_markup=get_main_menu(subbed_group))
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Экспорт в iCal (Google/Apple)", callback_data="ical:export")]
    ])
    await m.answer("💡 Вы можете экспортировать расписание группы в календарь телефона:", reply_markup=kb)

@dp.message(F.text.in_({"👩‍🏫 Преподаватели", "🏫 Аудитории"}))
async def show_filter_menu(m: Message, state: FSMContext, explicit_type: str = None):
    t_type = explicit_type if explicit_type else ("teacher" if m.text == "👩‍🏫 Преподаватели" else "classroom")
    await state.set_state(UserStates.waiting_for_teacher_search if t_type == "teacher" else UserStates.waiting_for_classroom_search)
    msg = await m.answer("🔍 Открываю поиск...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    prompt = "🔍 <b>Введите фамилию преподавателя</b> (или её часть) для поиска:" if t_type == "teacher" else "🔍 <b>Введите номер или название аудитории</b> для поиска:"
    await m.answer(prompt, parse_mode="HTML")

@dp.message(F.text == "🎓 Моя группа")
async def handle_my_group_menu(m: Message):
    msg = await m.answer("🎓 Открываю меню группы...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    subbed_group = await dao.hget("user_subs", str(m.from_user.id))
    if subbed_group:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🏫 Изменить группу", callback_data="change_my_group")]
        ])
        await m.answer(f"✅ Ваша текущая сохраненная группа: <b>{subbed_group}</b>", parse_mode="HTML", reply_markup=kb)
    else:
        await show_courses_menu(m)

async def show_courses_menu(m_or_c):
    btns = [
        [InlineKeyboardButton(text="1️⃣ Первый курс", callback_data="course:25")],
        [InlineKeyboardButton(text="2️⃣ Второй курс", callback_data="course:24")],
        [InlineKeyboardButton(text="3️⃣ Третий курс", callback_data="course:23")],
        [InlineKeyboardButton(text="4️⃣ Четвертый курс", callback_data="course:22")],
        [InlineKeyboardButton(text="🔙 Назад в подписки", callback_data="sub:back_to_menu")]
    ]
    text = "🎓 Выберите курс:"
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    if isinstance(m_or_c, CallbackQuery):
        await m_or_c.message.edit_text(text, reply_markup=kb)
    else:
        await m_or_c.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "change_my_group")
async def cb_change_my_group(c: CallbackQuery):
    await show_courses_menu(c)
    try: await c.answer()
    except: pass

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
        await c.message.answer("😔 Группы не найдены.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_courses")]]))
    else:
        btns = [InlineKeyboardButton(text=n, callback_data=f"fsel:group:{i}") for i, n in filtered_groups]
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn] for btn in btns] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_courses")]])
        await c.message.answer("👇 Выберите группу:", reply_markup=kb)
    try: await c.answer()
    except: pass

@dp.callback_query(F.data == "back_to_courses")
async def cb_back_to_courses(c: CallbackQuery):
    await show_courses_menu(c)
    try: await c.answer()
    except: pass

@dp.callback_query(F.data.startswith("fsel:"))       
async def cb_sel(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    _, t_type, idx = c.data.split(":")
    db_funcs = {"group": get_groups_db, "teacher": get_teachers_db, "classroom": get_classrooms_db}
    db = await db_funcs[t_type]()
    t_val = list(db.keys())[int(idx)]
    
    if t_type == "group":
        await dao.hset("user_subs", str(c.from_user.id), t_val)
        await db_manager.register_or_update_user(c.from_user.id, c.from_user.username, t_val)
        await c.message.answer(f"✅ Ваша группа успешно сохранена: <b>{t_val}</b>\nТеперь вы будете получать важные уведомления от старосты.", parse_mode="HTML", reply_markup=get_submenu_keyboard())
        await show_subscription_time_menu(c.message, user_id=str(c.from_user.id))
        await c.answer()
    else:
        await state.set_state(ScheduleStates.viewing)
        await state.update_data(target_type=t_type, target_value=t_val)
        await c.message.answer(f"✅ Фильтр: <b>{t_val}</b>", parse_mode="HTML", reply_markup=get_main_menu(t_val))
        await c.answer()

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
        text = await fmt_day(target_date, day_lessons, t_type, t_val if t_type == "group" else "")

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
    text = await fmt_week(s, data.get("target_type"), data.get("target_value") if data.get("target_type") == "group" else "") # type: ignore      
    
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

async def clear_chat_history(chat_id: int, exclude_ids: list = None):
    exclude_ids = exclude_ids or []
    ids = list(set(await dao.smembers(f"msg_history:{chat_id}")))
    ids = [int(x) for x in ids if int(x) not in exclude_ids]
    for i in range(0, len(ids), 100):
        try: await bot.delete_messages(chat_id, ids[i:i+100]) # type: ignore
        except:
            for mid in ids[i:i+100]: # type: ignore
                try: await bot.delete_message(chat_id, mid)
                except: continue
    await dao.delete(f"msg_history:{chat_id}")
    for ex_id in exclude_ids:
        await dao.sadd(f"msg_history:{chat_id}", ex_id)

@dp.message(F.text == "🧹 Очистить")
async def clear(m: Message, state: FSMContext):      
    await state.clear()
    msg = await m.answer("✨ Чат успешно очищен.", reply_markup=get_main_menu())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])

@dp.callback_query(F.data == "cancel_menu")
async def cb_cancel_menu(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    tt = data.get("target_type")
    await state.clear()
    try: await c.message.delete()
    except: pass
    
    if tt == "group":
        await show_courses_menu(c.message)
    elif tt == "teacher":
        await show_filter_menu(c.message, explicit_type="teacher")
    elif tt == "classroom":
        await show_filter_menu(c.message, explicit_type="classroom")
    else:
        await c.message.answer("🔙 Главное меню", reply_markup=get_main_menu())
    try: await c.answer()
    except: pass

@dp.message(F.text.in_({"🔄 Сбросить", "🔙 Назад"}))
async def reset(m: Message, state: FSMContext):
    data = await state.get_data()
    tt = data.get("target_type")
    await state.clear()
    msg = await m.answer("🔙 Возвращаюсь...", reply_markup=get_main_menu())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    
    if tt == "group":
        await show_courses_menu(m)
    elif tt == "teacher":
        await show_filter_menu(m, explicit_type="teacher")



async def run_morning_broadcast(target_time: str = None):
    tz = timezone(timedelta(hours=5))
    try:
        subs = await dao.hgetall("user_subs")
        user_mornings = await dao.hgetall("user_morning_time")
        if not subs:
            return 0
            
        groups_to_users = collections.defaultdict(list)
        for uid, gid in subs.items():
            u_time = user_mornings.get(uid, "08:00")
            if target_time and u_time != target_time:
                continue
            groups_to_users[gid].append(uid)
        
        today = datetime.now(tz).date()
        wo = 0
        
        count = 0
        for gid, uids in groups_to_users.items():
            week_s = await sm.fetch_schedule(wo, "group", gid)
            day_name = DAYS_OF_WEEK[today.weekday()]
            day_lessons = week_s.get(day_name, [])
            is_error = not week_s or "_error" in week_s
            
            if is_error:
                continue
            
            text = f"{get_greeting()} <b>Расписание на сегодня:</b>\n\n"
            text += await fmt_day(today, day_lessons, "group", gid)
            
            for uid in uids:
                try:
                    await bot.send_message(int(uid), text, parse_mode="HTML")
                    count += 1
                    await asyncio.sleep(0.05)
                except Exception as e:
                    logger.error(f"Failed to send scheduled msg to {uid}: {e}")
        return count
    except Exception as e:
        logger.error(f"Scheduler failed: {e}")
        return 0



async def notify_on_startup():
    try:
        # --- MIGRATION: Convert ID strings in user_subs to group names ---
        subs = await dao.hgetall("user_subs")
        if subs:
            id_to_name = {v: k for k, v in GROUPS_DB.items()}
            for uid, val in subs.items():
                if val in id_to_name:
                    await dao.hset("user_subs", uid, id_to_name[val])
        # -----------------------------------------------------------------
        
        if await dao.get("update_in_progress") == "1":
            await dao.delete("update_in_progress")
            admin_id = await dao.get("update_admin_id")
            if admin_id:
                try: await bot.send_message(int(admin_id), "🛠 <b>ОТЧЕТ:</b> Сервер успешно обновлен и запущен!", parse_mode="HTML")
                except: pass
                await dao.delete("update_admin_id")
            
            msgs = await dao.hgetall("update_msgs")
            for uid, mid in msgs.items():
                try: 
                    await bot.delete_message(int(uid), int(mid))
                    await asyncio.sleep(0.05)
                except: pass
            await dao.delete("update_msgs")
            
            await broadcast("✅ <b>Сервер обновлен и снова работает!</b>\nВсе системы в норме.")
        else:
            await broadcast("🚀 <b>Бот запущен и снова в строю!</b>\nВсе системы работают в штатном режиме.")
    except Exception as e:
        logger.error(f"Notify on startup failed: {e}")

async def notify_on_shutdown():
    try:
        await broadcast("📴 <b>Бот временно отключается...</b>\nВ данный момент происходит перезагрузка сервера или технические работы. Пожалуйста, подождите!")
    except Exception as e:
        logger.error(f"Notify on shutdown failed: {e}")

async def main():
    await db_manager.init_db()
    if PROXY_URL: logger.info(f"🌐 Используется прокси: {PROXY_URL}")
    dp.startup.register(notify_on_startup)
    dp.shutdown.register(notify_on_shutdown)
    asyncio.create_task(main_scheduler())
    await bot.delete_webhook(drop_pending_updates=True), await dp.start_polling(bot)

@dp.message(Command("starost_admin"))
async def starost_admin_cmd(m: Message, state: FSMContext):
    await m.answer("🎓 <b>Панель старосты</b>\n\nВведите пароль доступа:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_menu")]]))
    await state.set_state(StarostStates.waiting_for_password)

async def show_starosta_dashboard(m_or_c, user_id):
    name = await dao.hget("starosta_name", str(user_id))
    group = await dao.hget("starosta_group_saved", str(user_id))
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📨 Сообщение группе", callback_data="st_dash:broadcast"),
         InlineKeyboardButton(text="📊 Создать опрос", callback_data="st_dash:create_poll")],
        [InlineKeyboardButton(text="📝 Добавить Д/З", callback_data="st_dash:add_hw"),
         InlineKeyboardButton(text="❌ Удалить Д/З", callback_data="st_dash:del_hw")],
        [InlineKeyboardButton(text="📈 Результаты опросов", callback_data="st_dash:poll_results")],
        [InlineKeyboardButton(text="🏫 Изменить группу", callback_data="st_dash:change_group"),
         InlineKeyboardButton(text="👤 Изменить имя", callback_data="st_dash:name")],
        [InlineKeyboardButton(text="🌍 Написать всем", callback_data="st_dash:broadcast_all"),
         InlineKeyboardButton(text="🔑 Изменить пароль", callback_data="st_dash:pass")],
        [InlineKeyboardButton(text="❌ Выйти", callback_data="cancel_menu")]
    ])
    text = f"🎓 <b>Панель старосты</b>\n\n👤 Сохраненное имя: <b>{name}</b>\n🏫 Ваша группа: <b>{group or 'Не выбрана'}</b>\n\nВыберите действие:"
    
    if isinstance(m_or_c, CallbackQuery):
        await m_or_c.message.edit_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await m_or_c.answer(text, parse_mode="HTML", reply_markup=kb)

@dp.message(StarostStates.waiting_for_password)
async def starost_password(m: Message, state: FSMContext):
    uid = str(m.from_user.id)
    custom_pass = await dao.hget("starosta_pass", uid)
    correct_pass = custom_pass if custom_pass else os.getenv("STAROSTA_PASS", "ugmk2026")
    
    if m.text != correct_pass:
        await m.answer("❌ <b>Неверный пароль.</b>\nПопробуйте еще раз или нажмите Отмена.", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_menu")]]))
        return
        
    name = await dao.hget("starosta_name", uid)
    if not name:
        await m.answer("✅ <b>Доступ разрешен.</b>\n\nВведите ваше <b>Имя и Фамилию</b> (так студенты вашей группы увидят, от кого пришло сообщение):", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_menu")]]))
        await state.set_state(StarostStates.waiting_for_name)
    else:
        await state.clear()
        await show_starosta_dashboard(m, uid)

@dp.callback_query(F.data.startswith("st_dash:"))
async def starost_dash_action(c: CallbackQuery, state: FSMContext):
    action = c.data.split(":")[1]
    if action == "name":
        await c.message.edit_text("✏️ Введите новое <b>Имя и Фамилию</b>:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")]]))
        await state.set_state(StarostStates.waiting_for_name)
    elif action == "pass":
        await c.message.edit_text("🔑 Введите <b>Новый пароль</b> для панели старосты:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")]]))
        await state.set_state(StarostStates.waiting_for_new_pass)
    elif action == "broadcast_all":
        await c.message.edit_text("🌍 <b>Глобальная рассылка</b>\n\nНапишите текст, который будет разослан <b>ВСЕМ</b> подписчикам бота:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")]]))
        await state.set_state(StarostStates.waiting_for_message_all)
    elif action == "broadcast":
        saved_group = await dao.hget("starosta_group_saved", str(c.from_user.id))
        if saved_group:
            await state.update_data(starosta_group=saved_group)
            await c.message.edit_text(f"📝 <b>Написание сообщения</b>\nГруппа: <b>{saved_group}</b>\n\nНапишите текст, который будет разослан всем подписчикам этой группы:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")]]))
            await state.set_state(StarostStates.waiting_for_message)
        else:
            action = "change_group" # fallback
            
    if action == "change_group":
        courses = set()
        groups_db = await get_groups_db()
        for grp in groups_db.keys():
            parts = grp.split('-')
            if len(parts) > 1 and len(parts[1]) >= 2:
                year = parts[1][:2]
                courses.add(year)
                
        courses = sorted(list(courses), reverse=True)
        kb_rows = []
        for i, year in enumerate(courses):
            kb_rows.append([InlineKeyboardButton(text=f"{i+1} курс (набор 20{year})", callback_data=f"st_course:{year}")])
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await c.message.edit_text("📚 Выберите ваш курс:", reply_markup=kb, parse_mode="HTML")
        await state.set_state(StarostStates.waiting_for_course)
    elif action == "back":
        await state.clear()
        await show_starosta_dashboard(c, str(c.from_user.id))

@dp.message(StarostStates.waiting_for_new_pass)
async def starost_new_pass(m: Message, state: FSMContext):
    await dao.hset("starosta_pass", str(m.from_user.id), m.text)
    await m.answer("✅ Пароль успешно изменен!")
    await state.clear()
    await show_starosta_dashboard(m, str(m.from_user.id))

@dp.message(StarostStates.waiting_for_name)
async def starost_name(m: Message, state: FSMContext):
    await dao.hset("starosta_name", str(m.from_user.id), m.text)
    await m.answer(f"✅ Имя сохранено: <b>{m.text}</b>", parse_mode="HTML")
    await state.clear()
    await show_starosta_dashboard(m, str(m.from_user.id))

@dp.callback_query(F.data.startswith("st_course:"), StarostStates.waiting_for_course)
async def starost_course(c: CallbackQuery, state: FSMContext):
    year = c.data.split(":")[1]
    
    groups_db = await get_groups_db()
    groups = [g for g in groups_db.keys() if len(g.split('-')) > 1 and g.split('-')[1].startswith(year)]
    
    kb_rows = []
    for i in range(0, len(groups), 2):
        row = [InlineKeyboardButton(text=g, callback_data=f"st_group:{g}") for g in groups[i:i+2]]
        kb_rows.append(row)
    kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await c.message.edit_text(f"🏫 Выберите вашу группу (набор 20{year}):", reply_markup=kb)
    await state.set_state(StarostStates.waiting_for_group)

@dp.callback_query(F.data.startswith("st_group:"), StarostStates.waiting_for_group)
async def starost_group(c: CallbackQuery, state: FSMContext):
    group = c.data.split(":")[1]
    await dao.hset("starosta_group_saved", str(c.from_user.id), group)
    await state.update_data(starosta_group=group)
    
    await c.message.edit_text(f"✅ Ваша группа <b>{group}</b> сохранена!\n\n📝 <b>Написание сообщения</b>\n\nНапишите текст, который будет разослан всем подписчикам этой группы:", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")]]))
    await state.set_state(StarostStates.waiting_for_message)

@dp.message(StarostStates.waiting_for_message)
async def starost_broadcast(m: Message, state: FSMContext):
    data = await state.get_data()
    starosta_name = await dao.hget("starosta_name", str(m.from_user.id))
    group = data.get("starosta_group")
    
    await state.clear()
    
    subs = await dao.hgetall("user_subs")
    target_users = [uid for uid, gid in subs.items() if gid == group]
    
    if not target_users:
        await m.answer(f"😔 К сожалению, на группу <b>{group}</b> в боте еще никто не подписан.", parse_mode="HTML")
        await show_starosta_dashboard(m, str(m.from_user.id))
        return
        
    await m.answer(f"🚀 <b>Рассылка запущена!</b>\nОтправляю сообщение {len(target_users)} студентам из группы {group}...", parse_mode="HTML")
    
    text = f"📢 <b>{starosta_name}:</b>\n\n{m.text}"
    
    success = 0
    for uid in target_users:
        try:
            await bot.send_message(int(uid), text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send to {uid}: {e}")
            
    await m.answer(f"✅ <b>Рассылка завершена!</b>\nУспешно доставлено: <b>{success} из {len(target_users)}</b>.", parse_mode="HTML")
    await show_starosta_dashboard(m, str(m.from_user.id))

@dp.message(StarostStates.waiting_for_message_all)
async def starost_broadcast_all(m: Message, state: FSMContext):
    starosta_name = await dao.hget("starosta_name", str(m.from_user.id))
    await state.clear()
    
    subs = await dao.hgetall("user_subs")
    target_users = list(subs.keys())
    
    if not target_users:
        await m.answer("😔 В боте еще нет подписчиков.", parse_mode="HTML")
        await show_starosta_dashboard(m, str(m.from_user.id))
        return
        
    await m.answer(f"🌍 <b>Глобальная рассылка запущена!</b>\nОтправляю сообщение {len(target_users)} студентам...", parse_mode="HTML")
    text = f"📢 <b>{starosta_name}:</b>\n\n{m.text}"
    
    success = 0
    for uid in target_users:
        try:
            await bot.send_message(int(uid), text, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send to {uid}: {e}")
            
    await m.answer(f"✅ <b>Глобальная рассылка завершена!</b>\nУспешно доставлено: <b>{success} из {len(target_users)}</b>.", parse_mode="HTML")
    await show_starosta_dashboard(m, str(m.from_user.id))

@dp.message(UserStates.waiting_for_teacher_search)
@dp.message(UserStates.waiting_for_classroom_search)
async def handle_search_input(m: Message, state: FSMContext):
    curr_state = await state.get_state()
    t_type = "teacher" if curr_state == UserStates.waiting_for_teacher_search.state else "classroom"
    search_q = m.text.strip().lower()
    
    db = await get_teachers_db() if t_type == "teacher" else await get_classrooms_db()
    matches = [(name, idx) for idx, name in enumerate(db.keys()) if search_q in name.lower()]
    
    if not matches:
        await m.answer("😔 Ничего не найдено. Пожалуйста, попробуйте ввести другой запрос:", 
                       reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_menu")]]))
        return
        
    if len(matches) == 1:
        name, idx = matches[0]
        await state.set_state(ScheduleStates.viewing)
        await state.update_data(target_type=t_type, target_value=name)
        await m.answer(f"✅ Выбрано: <b>{name}</b>", parse_mode="HTML", reply_markup=get_main_menu(name))
        await display_day_schedule(m, state, datetime.now().date())
        return
        
    btns = []
    for name, idx in matches[:40]:
        btns.append([InlineKeyboardButton(text=name, callback_data=f"fsel:{t_type}:{idx}")])
    btns.append([InlineKeyboardButton(text="❌ Отмена", callback_data="cancel_menu")])
    await m.answer("👇 Выберите подходящий вариант:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))

@dp.callback_query(F.data == "ical:export")
async def cb_ical_export(c: CallbackQuery, state: FSMContext):
    data = await state.get_data()
    t_val = data.get("target_value")
    t_type = data.get("target_type")
    
    if not t_val or not t_type:
        t_val = await dao.hget("user_subs", str(c.from_user.id))
        t_type = "group"
        
    if not t_val:
        await c.answer("❌ Сначала выберите вашу группу!", show_alert=True)
        return
        
    await c.answer("⏳ Генерация календаря...")
    
    try:
        s0 = await sm.fetch_schedule(0, t_type, t_val)
        s1 = await sm.fetch_schedule(1, t_type, t_val)
        
        lessons_by_date = {}
        for s in [s0, s1]:
            if not s or "_error" in s: continue
            dates_dict = s.get("_dates", {})
            for day_name, d_str in dates_dict.items():
                try:
                    d_date = datetime.strptime(d_str, "%d.%m.%Y").date()
                    lessons_by_date[d_date] = s.get(day_name, [])
                except: continue
                
        import uuid
        lines = [
            "BEGIN:VCALENDAR",
            "VERSION:2.0",
            "PRODID:-//TU UGMK Bot//Schedule Export//EN",
            "CALSCALE:GREGORIAN",
            "METHOD:PUBLISH"
        ]
        
        for d_date, lessons in lessons_by_date.items():
            for l in lessons:
                subj = l.get('subject', 'Занятие')
                l_type = l.get('type', '')
                time_str = l.get('time', '')
                room = l.get('room', '')
                teach = l.get('teacher', '')
                grp = l.get('group', '')
                
                time_match = re.match(r'(\d{2}):(\d{2})\s*-\s*(\d{2}):(\d{2})', time_str)
                if not time_match: continue
                sh, smin, eh, emin = map(int, time_match.groups())
                
                start_dt = datetime(d_date.year, d_date.month, d_date.day, sh, smin) - timedelta(hours=5)
                end_dt = datetime(d_date.year, d_date.month, d_date.day, eh, emin) - timedelta(hours=5)
                
                dtstart = start_dt.strftime("%Y%m%dT%H%M%SZ")
                dtend = end_dt.strftime("%Y%m%dT%H%M%SZ")
                
                desc = []
                if l_type: desc.append(f"Тип: {l_type}")
                if teach: desc.append(f"Преподаватель: {teach}")
                if grp: desc.append(f"Группа: {grp}")
                desc_str = "\\n".join(desc).replace(",", "\\,").replace(";", "\\;")
                
                uid = f"lesson-{uuid.uuid4()}@tu-ugmk-bot"
                lines.extend([
                    "BEGIN:VEVENT",
                    f"UID:{uid}",
                    f"DTSTART:{dtstart}",
                    f"DTEND:{dtend}",
                    f"SUMMARY:{subj}",
                    f"DESCRIPTION:{desc_str}",
                    f"LOCATION:{room}",
                    "END:VEVENT"
                ])
                
        lines.append("END:VCALENDAR")
        ics_content = "\r\n".join(lines)
        
        from aiogram.types import BufferedInputFile
        filename = f"schedule_{t_val.replace(' ', '_')}.ics"
        file_data = BufferedInputFile(ics_content.encode("utf-8"), filename=filename)
        
        await c.message.answer_document(
            document=file_data,
            caption=f"📅 Календарь для <b>{t_val}</b> на 2 недели.\nИмпортируйте его в календарь телефона.",
            parse_mode="HTML"
        )
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        logger.error(f"iCal export failed: {e}\n{tb}")
        await c.message.answer(f"❌ Не удалось экспортировать календарь.\nОшибка: <code>{e}</code>\n<pre>{tb[:2000]}</pre>", parse_mode="HTML")

@dp.message(F.text == "⭐ В избранное", ScheduleStates.viewing)
async def add_to_favorites(m: Message, state: FSMContext):
    data = await state.get_data()
    t_val = data.get("target_value")
    t_type = data.get("target_type")
    if t_val and t_type:
        await dao.sadd(f"favs:{m.from_user.id}", f"{t_type}:{t_val}")
        await m.answer(f"⭐ <b>{t_val}</b> успешно добавлено в избранное!", parse_mode="HTML")
    else:
        await m.answer("❌ Не удалось определить активное расписание для сохранения.")

@dp.message(F.text == "⭐ Избранное")
async def show_favorites(m: Message):
    msg = await m.answer("⭐ Открываю избранное...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    favs = list(await dao.smembers(f"favs:{m.from_user.id}"))
    if not favs:
        await m.answer("⭐ <b>Избранное</b>\n\nУ вас пока нет сохраненных расписаний.\nЧтобы добавить расписание в избранное, откройте его и нажмите кнопку <b>«⭐ В избранное»</b>.", parse_mode="HTML")
        return
    btns = []
    for f in favs:
        t_type, t_val = f.split(":", 1)
        prefix = "🎓" if t_type == "group" else ("👩‍🏫" if t_type == "teacher" else "🏫")
        btns.append([InlineKeyboardButton(text=f"{prefix} {t_val}", callback_data=f"fav_select:{t_type}:{t_val}")])
    btns.append([InlineKeyboardButton(text="⚙️ Управление избранным", callback_data="fav_manage")])
    await m.answer("⭐ <b>Ваши избранные расписания:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@dp.callback_query(F.data.startswith("fav_select:"))
async def cb_fav_select(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    _, t_type, t_val = c.data.split(":", 2)
    await state.set_state(ScheduleStates.viewing)
    await state.update_data(target_type=t_type, target_value=t_val)
    await c.message.answer(f"✅ Фильтр из избранного: <b>{t_val}</b>", parse_mode="HTML", reply_markup=get_main_menu(t_val))
    await display_day_schedule(c.message, state, datetime.now().date())
    await c.answer()

@dp.callback_query(F.data == "fav_manage")
async def cb_fav_manage(c: CallbackQuery):
    favs = list(await dao.smembers(f"favs:{c.from_user.id}"))
    if not favs:
        await c.message.edit_text("Список избранного пуст.")
        return
    btns = []
    for f in favs:
        t_type, t_val = f.split(":", 1)
        prefix = "🎓" if t_type == "group" else ("👩‍🏫" if t_type == "teacher" else "🏫")
        btns.append([InlineKeyboardButton(text=f"❌ {prefix} {t_val}", callback_data=f"fav_del:{t_type}:{t_val}")])
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="fav_back_to_list")])
    await c.message.edit_text("Выберите элемент для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await c.answer()

@dp.callback_query(F.data == "fav_back_to_list")
async def cb_fav_back_to_list(c: CallbackQuery):
    await c.message.delete()
    await show_favorites(c.message)
    await c.answer()

@dp.callback_query(F.data.startswith("fav_del:"))
async def cb_fav_del(c: CallbackQuery):
    _, t_type, t_val = c.data.split(":", 2)
    await dao.srem(f"favs:{c.from_user.id}", f"{t_type}:{t_val}")
    await c.answer("Удалено из избранного")
    await cb_fav_manage(c)

# Handle starosta HW clicks
@dp.callback_query(F.data.startswith("st_dash:"))
async def cb_st_dash_homework(c: CallbackQuery, state: FSMContext):
    action = c.data.split(":")[1]
    uid = str(c.from_user.id)
    group = await dao.hget("starosta_group_saved", uid)
    
    if action == "add_hw":
        if not group:
            await c.answer("❌ Сначала сохраните вашу группу!", show_alert=True)
            return
        btns = []
        for day in DAYS_OF_WEEK[:6]:
            btns.append([InlineKeyboardButton(text=day, callback_data=f"st_hw_day:{day}")])
        btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")])
        await c.message.edit_text("📅 <b>Выберите день для добавления Д/З:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        await state.set_state(StarostStates.waiting_for_hw_day)
        await c.answer()
        
    elif action == "del_hw":
        if not group:
            await c.answer("❌ Сначала сохраните вашу группу!", show_alert=True)
            return
        hw_dict = await dao.hgetall(f"homework:{group}")
        if not hw_dict:
            await c.message.answer("😴 У вашей группы нет сохраненных домашних заданий.")
            await show_starosta_dashboard(c.message, uid)
            await c.answer()
            return
        btns = []
        for key, val in hw_dict.items():
            day_name, t_slot = key.split(":", 1)
            btns.append([InlineKeyboardButton(text=f"❌ {day_name[:2]}. {t_slot} ({val[:15]}...)", callback_data=f"st_hw_del:{key}")])
        btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")])
        await c.message.edit_text("🗑 <b>Выберите Д/З для удаления:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
        await state.set_state(StarostStates.waiting_for_hw_delete)
        await c.answer()

@dp.callback_query(StarostStates.waiting_for_hw_day, F.data.startswith("st_hw_day:"))
async def cb_st_hw_day(c: CallbackQuery, state: FSMContext):
    day = c.data.split(":")[1]
    await state.update_data(hw_day=day)
    uid = str(c.from_user.id)
    group = await dao.hget("starosta_group_saved", uid)
    
    week_s = await sm.fetch_schedule(0, "group", group)
    lessons = week_s.get(day, []) if week_s else []
    
    btns = []
    if lessons:
        for l in lessons:
            t_slot = l.get("time")
            subj = l.get("subject")
            btns.append([InlineKeyboardButton(text=f"{t_slot} {subj}", callback_data=f"st_hw_lesson:{t_slot}")])
    else:
        slots = ["08:30 - 10:00", "10:10 - 11:40", "11:50 - 13:20", "14:00 - 15:30", "15:40 - 17:10", "17:20 - 18:50", "19:00 - 20:30"]
        for s in slots:
            btns.append([InlineKeyboardButton(text=s, callback_data=f"st_hw_lesson:{s}")])
            
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")])
    await c.message.edit_text(f"🕒 <b>Выберите время пары ({day}):</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await state.set_state(StarostStates.waiting_for_hw_lesson)
    await c.answer()

@dp.callback_query(StarostStates.waiting_for_hw_lesson, F.data.startswith("st_hw_lesson:"))
async def cb_st_hw_lesson(c: CallbackQuery, state: FSMContext):
    t_slot = c.data.split(":", 1)[1]
    await state.update_data(hw_time=t_slot)
    await c.message.edit_text("📝 <b>Введите текст домашнего задания или важной заметки:</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Отмена", callback_data="st_dash:back")]]))
    await state.set_state(StarostStates.waiting_for_hw_text)
    await c.answer()

@dp.message(StarostStates.waiting_for_hw_text)
async def starost_hw_text(m: Message, state: FSMContext):
    data = await state.get_data()
    day = data.get("hw_day")
    t_slot = data.get("hw_time")
    uid = str(m.from_user.id)
    group = await dao.hget("starosta_group_saved", uid)
    
    await dao.hset(f"homework:{group}", f"{day}:{t_slot}", m.text.strip())
    await m.answer(f"✅ Домашнее задание на <b>{day} ({t_slot})</b> успешно добавлено!", parse_mode="HTML")
    await state.clear()
    await show_starosta_dashboard(m, uid)

@dp.callback_query(StarostStates.waiting_for_hw_delete, F.data.startswith("st_hw_del:"))
async def cb_st_hw_del(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":", 1)[1]
    uid = str(c.from_user.id)
    group = await dao.hget("starosta_group_saved", uid)
    
    await dao.hdel(f"homework:{group}", key)
    await c.answer("🗑 Д/З успешно удалено!")
    await state.clear()
    await show_starosta_dashboard(c, uid)

# ═══════════════════ ИИ-АССИСТЕНТ ═══════════════════
FREE_MODELS = [
    "nemotron-3-ultra-free",
    "laguna-xs-2-free",
    "qwen3-next-free",
    "gpt-oss-free",
    "llama-3.3-free"
]

PREMIUM_MODELS = [
    "kimi-k2.7-code",
    "claude-opus-4.8",
    "gpt-4",
    "gpt-5.5"
]

@dp.message(F.text == "🤖 ИИ-Ассистент")
@dp.message(Command("ai"))
async def ai_menu(m: Message, state: FSMContext):
    await state.clear()
    msg = await m.answer("🤖 Открываю панель ИИ...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    uid = m.from_user.id
    user_row = await db_manager.get_user(uid)
    
    if not user_row:
        await db_manager.register_or_update_user(uid, m.from_user.username)
        user_row = await db_manager.get_user(uid)
        
    model = user_row['ai_model'] if user_row else 'gpt-4o-mini'
    has_key = bool(user_row['custom_ai_key']) if user_row else False
    ai_balance = user_row['ai_balance'] if user_row else 0
    key_status = "✅ Установлен" if has_key else "❌ Не установлен"
    
    text = (
        "🤖 <b>Панель ИИ-Ассистента</b>\n\n"
        f"🧠 Выбранная модель: <code>{model}</code>\n"
        f"💳 Баланс ИИ-запросов (OpenRouter): <b>{ai_balance}</b>"
    )
    
    is_free = model in FREE_MODELS
    can_chat = has_key or (ai_balance > 0) or is_free
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Начать диалог", callback_data="ai:chat") if can_chat else
         InlineKeyboardButton(text="💬 Начать диалог (нужен ключ/баланс)", callback_data="ai:need_key")],
        [InlineKeyboardButton(text="⚙️ Выбрать модель", callback_data="ai:select_model")],
        [InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="ai:clear_context")]
    ])
    await m.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "ai:chat")
async def cb_ai_chat(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    user_row = await db_manager.get_user(uid)
    has_key = bool(user_row['custom_ai_key']) if user_row else False
    ai_balance = user_row['ai_balance'] if user_row else 0
    model = user_row['ai_model'] if user_row else 'gpt-4o-mini'
    
    is_free = model in FREE_MODELS
    
    if not has_key and ai_balance <= 0 and not is_free:
        await c.answer("⚠️ У вас нет личного ключа и баланс запросов равен 0!", show_alert=True)
        return
        
    await state.set_state(UserStates.waiting_for_ai_prompt)
    await state.update_data(
        ai_key=user_row['custom_ai_key'] if has_key else None,
        ai_model=model
    )
    
    kb = ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="❌ Выйти из чата ИИ")]], resize_keyboard=True)
    await c.message.answer(
        "💬 <b>Диалог с ИИ запущен!</b>\n\n"
        "Отправьте любое сообщение, и ИИ ответит вам с учетом контекста переписки.\n"
        "Чтобы завершить общение, нажмите кнопку <b>«❌ Выйти из чата ИИ»</b> ниже.",
        reply_markup=kb,
        parse_mode="HTML"
    )
    await c.message.delete()
    await c.answer()

@dp.callback_query(F.data == "ai:need_key")
async def cb_ai_need_key(c: CallbackQuery):
    await c.answer("⚠️ У вас нет личного ключа и ваш баланс ИИ равен 0. Пожалуйста, пополните баланс!", show_alert=True)

@dp.message(UserStates.waiting_for_ai_prompt)
async def ai_chat_message(m: Message, state: FSMContext):
    if m.text == "❌ Выйти из чата ИИ":
        await state.clear()
        await m.answer("👋 Диалог завершен.", reply_markup=get_main_menu())
        return
        
    data = await state.get_data()
    api_key = data.get("ai_key")
    model_name = data.get("ai_model", "gpt-4o-mini")
    uid = m.from_user.id
    
    user_row = await db_manager.get_user(uid)
    has_custom_key = bool(api_key)
    is_programmatic_key = has_custom_key and bool(user_row.get('ai_expires_at')) if user_row else False
    is_free = model_name in FREE_MODELS
    is_premium = model_name in PREMIUM_MODELS
    
    if not has_custom_key and not is_free:
        balance = await db_manager.check_user_ai_balance(uid)
        required_balance = 4 if is_premium else 1
        if balance < required_balance:
            await state.clear()
            await m.answer(
                f"❌ <b>Недостаточно запросов!</b>\n"
                f"Для использования этой модели требуется минимум <b>{required_balance}</b> 💳 (ваш баланс: <b>{balance}</b>).\n"
                f"Чат завершен. Пожалуйста, пополните баланс.",
                reply_markup=get_main_menu(),
                parse_mode="HTML"
            )
            return
            
    history_key = f"ai_history:{uid}"
    history = []
    history_str = await dao.get(history_key)
    if history_str:
        try:
            history = json.loads(history_str)
        except Exception:
            history = []
            
    async with loading_animation(m.chat.id):
        try:
            response_text = await get_ai_response(
                prompt=m.text,
                api_key=api_key,
                model_name=model_name,
                history=history
            )
            
            await db_manager.log_ai_request(
                telegram_id=uid,
                prompt=m.text,
                response=response_text,
                model_used=model_name
            )
            
            if not has_custom_key or is_programmatic_key:
                if not is_free:
                    deduct_amount = 4 if is_premium else 1
                    async with db_manager.pool.acquire() as conn:
                        await conn.execute("UPDATE users SET ai_balance = GREATEST(0, ai_balance - $2) WHERE telegram_id = $1", uid, deduct_amount)
                    new_bal = await db_manager.check_user_ai_balance(uid)
                    response_text += f"\n\n<i>(Осталось запросов: {new_bal} 💳)</i>"
                else:
                    response_text += f"\n\n<i>(🆓 Бесплатный запрос)</i>"
            
            history.append({"role": "user", "content": m.text})
            history.append({"role": "assistant", "content": response_text})
            history = history[-10:]
            
            await dao.setex(history_key, 3600, json.dumps(history, ensure_ascii=False))
            
            if len(response_text) > 4096:
                for chunk in [response_text[i:i+4000] for i in range(0, len(response_text), 4000)]:
                    await m.answer(chunk)
            else:
                await m.answer(response_text, parse_mode="HTML")
                
        except Exception as e:
            logger.error(f"AI response failed: {e}")
            err_msg = str(e).lower()
            if has_custom_key and any(x in err_msg for x in ["budget", "limit", "payment", "expired", "402", "403", "401", "unauthorized", "invalid key", "credential", "user not found"]):
                await db_manager.set_user_ai_key(uid, None)
                await state.clear()
                await m.answer(
                    "⚠️ <b>Ваш персональный ключ OpenRouter исчерпал баланс, истек или был удален.</b>\n\n"
                    "Бот автоматически сбросил ключ. Пожалуйста, приобретите новый пакет запросов в меню ИИ.",
                    reply_markup=get_main_menu(),
                    parse_mode="HTML"
                )
            else:
                await m.answer(
                    f"❌ <b>Ошибка вызова ИИ:</b>\n<code>{str(e)}</code>\n\n"
                    "Пожалуйста, обратитесь к администратору.",
                    parse_mode="HTML"
                )

@dp.callback_query(F.data == "ai:set_key")
async def cb_ai_set_key(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_ai_key)
    await c.message.answer(
        "🔑 <b>Установка API-ключа ИИ</b>\n\n"
        "Отправьте ваш API-ключ в ответ на это сообщение.\n"
        "• Для <b>Google Gemini</b> ключ обычно начинается с <code>AIzaSy...</code>\n"
        "• Для <b>OpenAI GPT</b> ключ начинается с <code>sk-...</code>\n\n"
        "Ваш ключ будет сохранен в базе данных.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="ai_cancel_settings")]]),
        parse_mode="HTML"
    )
    await c.answer()

@dp.message(UserStates.waiting_for_ai_key)
async def process_ai_key(m: Message, state: FSMContext):
    key = m.text.strip()
    if len(key) < 20:
        await m.answer("❌ Слишком короткий ключ. Пожалуйста, проверьте и пришлите корректный ключ.")
        return
        
    await db_manager.set_user_ai_key(m.from_user.id, key)
    await state.clear()
    
    try: await m.delete()
    except Exception: pass
        
    await m.answer("✅ <b>API-ключ успешно сохранен!</b> Ваше сообщение с ключом было удалено из чата для безопасности.", parse_mode="HTML")
    await show_ai_menu_directly(m)

@dp.callback_query(F.data == "ai_ignore")
async def cb_ai_ignore(c: CallbackQuery):
    await c.answer()

@dp.callback_query(F.data == "ai:select_model")
async def cb_ai_select_model(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💎 --- СТАНДАРТНЫЕ (1 💳) ---", callback_data="ai_ignore")],
        [InlineKeyboardButton(text="🧠 GPT-4o-mini", callback_data="ai_set_mod:gpt-4o-mini"),
         InlineKeyboardButton(text="🔮 DeepSeek v3.2", callback_data="ai_set_mod:deepseek-v3.2")],
        [InlineKeyboardButton(text="🤖 MiniMax M2.7", callback_data="ai_set_mod:minimax-m2.7"),
         InlineKeyboardButton(text="🔮 GLM-5", callback_data="ai_set_mod:glm-5")],
         
        [InlineKeyboardButton(text="🔥 --- ПРЕМИУМ (4 💳) ---", callback_data="ai_ignore")],
        [InlineKeyboardButton(text="🌙 Kimi K2.7 Code", callback_data="ai_set_mod:kimi-k2.7-code"),
         InlineKeyboardButton(text="🦉 Claude Opus 4.8", callback_data="ai_set_mod:claude-opus-4.8")],
        [InlineKeyboardButton(text="🧠 GPT-4", callback_data="ai_set_mod:gpt-4"),
         InlineKeyboardButton(text="🧠 GPT-5.5", callback_data="ai_set_mod:gpt-5.5")],
         
        [InlineKeyboardButton(text="🆓 --- БЕСПЛАТНЫЕ (0 💳) ---", callback_data="ai_ignore")],
        [InlineKeyboardButton(text="⚡ Nemotron 3 Ultra (Free)", callback_data="ai_set_mod:nemotron-3-ultra-free")],
        [InlineKeyboardButton(text="💧 Laguna XS.2 (Free)", callback_data="ai_set_mod:laguna-xs-2-free"),
         InlineKeyboardButton(text="🐉 Qwen 3 Next (Free)", callback_data="ai_set_mod:qwen3-next-free")],
        [InlineKeyboardButton(text="🧠 GPT OSS 120B (Free)", callback_data="ai_set_mod:gpt-oss-free"),
         InlineKeyboardButton(text="🦙 Llama 3.3 70B (Free)", callback_data="ai_set_mod:llama-3.3-free")],
         
        [InlineKeyboardButton(text="🔙 Назад", callback_data="ai:back_to_menu")]
    ])
    await c.message.edit_text("⚙️ <b>Выберите модель ИИ:</b>", reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("ai_set_mod:"))
async def cb_ai_set_model_save(c: CallbackQuery):
    model = c.data.split(":")[1]
    await db_manager.set_user_ai_model(c.from_user.id, model)
    await c.answer(f"Модель изменена на {model}")
    await show_ai_menu_directly(c, user_id=c.from_user.id)

@dp.callback_query(F.data == "ai:clear_context")
async def cb_ai_clear_context(c: CallbackQuery):
    history_key = f"ai_history:{c.from_user.id}"
    await dao.delete(history_key)
    await c.answer("🧹 Контекст диалога успешно очищен!", show_alert=True)

@dp.callback_query(F.data == "ai_cancel_settings")
@dp.callback_query(F.data == "ai:close")
async def cb_ai_close(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await c.message.delete()
    await c.message.answer("🔙 Главное меню", reply_markup=get_main_menu())
    await c.answer()

async def show_ai_menu_directly(message: Message | CallbackQuery, user_id: int = None):
    uid = user_id or message.from_user.id
    user_row = await db_manager.get_user(int(uid))
    model = user_row['ai_model'] if user_row else 'gpt-4o-mini'
    has_key = bool(user_row['custom_ai_key']) if user_row else False
    ai_balance = user_row['ai_balance'] if user_row else 0
    
    text = (
        "🤖 <b>Панель ИИ-Ассистента</b>\n\n"
        f"🧠 Выбранная модель: <code>{model}</code>\n"
        f"💳 Баланс ИИ-запросов (OpenRouter): <b>{ai_balance}</b>"
    )
    
    is_free = model in FREE_MODELS
    can_chat = has_key or (ai_balance > 0) or is_free
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Начать диалог", callback_data="ai:chat") if can_chat else
         InlineKeyboardButton(text="💬 Начать диалог (нужен ключ/баланс)", callback_data="ai:need_key")],
        [InlineKeyboardButton(text="⚙️ Выбрать модель", callback_data="ai:select_model")],
        [InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="ai:clear_context")]
    ])
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    else:
        await message.answer(text, reply_markup=kb, parse_mode="HTML")


@dp.callback_query(F.data == "ai:back_to_menu")
async def cb_ai_back_to_menu(c: CallbackQuery):
    uid = c.from_user.id
    user_row = await db_manager.get_user(uid)
    model = user_row['ai_model'] if user_row else 'gpt-4o-mini'
    has_key = bool(user_row['custom_ai_key']) if user_row else False
    ai_balance = user_row['ai_balance'] if user_row else 0
    key_status = "✅ Установлен" if has_key else "❌ Не установлен"
    
    text = (
        "🤖 <b>Панель ИИ-Ассистента</b>\n\n"
        f"🧠 Выбранная модель: <code>{model}</code>\n"
        f"💳 Баланс ИИ-запросов (OpenRouter): <b>{ai_balance}</b>"
    )
    
    is_free = model in FREE_MODELS
    can_chat = has_key or (ai_balance > 0) or is_free
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💬 Начать диалог", callback_data="ai:chat") if can_chat else
         InlineKeyboardButton(text="💬 Начать диалог (нужен ключ/баланс)", callback_data="ai:need_key")],
        [InlineKeyboardButton(text="⚙️ Выбрать модель", callback_data="ai:select_model")],
        [InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="ai:clear_context")]
    ])
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()


# ═══════════════════ VPN-СЕРВИС ═══════════════════
@dp.message(F.text == "🔌 VPN-сервис")
@dp.message(Command("vpn"))
async def vpn_menu(m: Message, state: FSMContext):
    await state.clear()
    msg = await m.answer("🔌 Открываю VPN-сервис...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    uid = m.from_user.id
    user_row = await db_manager.get_user(uid)
    
    if not user_row:
        await db_manager.register_or_update_user(uid, m.from_user.username)
        user_row = await db_manager.get_user(uid)
        
    vpn_enabled = user_row['vpn_enabled'] if user_row else False
    
    if vpn_enabled:
        text = (
            "🔌 <b>Ваша подписка на VPN активна!</b>\n\n"
            "Вы можете скачать файл конфигурации или отсканировать QR-код для быстрого импорта в приложение WireGuard.\n\n"
            "<b>Инструкция по настройке:</b>\n"
            "1. Установите приложение <b>WireGuard</b> из App Store или Google Play.\n"
            "2. Отсканируйте QR-код ниже или импортируйте файл конфигурации.\n"
            "3. Включите соединение в приложении."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 Скачать файл .conf", callback_data="vpn:get_file"),
             InlineKeyboardButton(text="🖼 Показать QR-код", callback_data="vpn:get_qr")],
            [InlineKeyboardButton(text="❌ Отключить VPN", callback_data="vpn:disable")]
        ])
    else:
        text = (
            "🔌 <b>Собственный VPN-сервис</b>\n\n"
            "Мы предоставляем стабильный, быстрый и безопасный доступ к зарубежным образовательным платформам и библиотекам.\n\n"
            "Выберите вариант подписки:\n"
            "1. <b>WireGuard VPN (отдельно)</b> — 100 ⭐ (на 30 дней)\n"
            "2. <b>VPN + 150 Стандарт ИИ</b> — 500 ⭐ (на 30 дней)\n"
            "3. <b>VPN + 30 Премиум ИИ (Claude, GPT, Kimi, Qwen)</b> — 600 ⭐ (на 30 дней)\n\n"
            "<i>Все тарифы рассчитаны для обеспечения 30%+ чистой прибыли в месяц для развития бота.</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔌 VPN на 30 дней (100 ⭐)", callback_data="vpn:buy_only")],
            [InlineKeyboardButton(text="💳 VPN + 150 Стандарт (500 ⭐)", callback_data="vpn:buy_standard")],
            [InlineKeyboardButton(text="💳 VPN + 30 Премиум (600 ⭐)", callback_data="vpn:buy_premium")]
        ])
        
    await m.answer(text, reply_markup=kb, parse_mode="HTML")

@dp.callback_query(F.data == "vpn:enable")
async def cb_vpn_enable(c: CallbackQuery):
    uid = c.from_user.id
    await c.message.edit_text("⏳ <b>Генерация персональных ключей и настройка сервера...</b>\nПожалуйста, подождите.", parse_mode="HTML")
    
    try:
        user_row = await db_manager.get_user(uid)
        user_db_id = user_row['id'] if user_row else 1
        
        config_text = await vpn_manager.generate_user_vpn_config(user_db_id)
        await db_manager.set_user_vpn(uid, enabled=True, key=config_text)
        
        await c.message.delete()
        
        from aiogram.types import BufferedInputFile
        file_data = BufferedInputFile(config_text.encode("utf-8"), filename=f"tu_ugmk_vpn_{uid}.conf")
        await c.message.answer_document(
            document=file_data,
            caption="✅ <b>VPN успешно подключен!</b>\n\nИмпортируйте этот файл в приложение WireGuard.\nВы также можете получить QR-код для настройки через меню.",
            parse_mode="HTML"
        )
        await show_vpn_menu_directly(c.message, user_id=uid)
        
    except Exception as e:
        logger.error(f"VPN activation failed for {uid}: {e}")
        await c.message.edit_text(
            f"❌ <b>Не удалось активировать VPN:</b>\n<code>{str(e)}</code>\n\nПожалуйста, обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="ai:close")]]),
            parse_mode="HTML"
        )
    await c.answer()

@dp.callback_query(F.data == "vpn:get_file")
async def cb_vpn_get_file(c: CallbackQuery):
    uid = c.from_user.id
    user_row = await db_manager.get_user(uid)
    if not user_row or not user_row['vpn_enabled'] or not user_row['vpn_key']:
        await c.answer("⚠️ У вас нет активного VPN-ключа!", show_alert=True)
        return
        
    from aiogram.types import BufferedInputFile
    config_text = user_row['vpn_key']
    file_data = BufferedInputFile(config_text.encode("utf-8"), filename=f"tu_ugmk_vpn_{uid}.conf")
    
    await c.message.answer_document(
        document=file_data,
        caption="📁 Ваш файл конфигурации WireGuard."
    )
    await c.answer()

@dp.callback_query(F.data == "vpn:get_qr")
async def cb_vpn_get_qr(c: CallbackQuery):
    uid = c.from_user.id
    user_row = await db_manager.get_user(uid)
    if not user_row or not user_row['vpn_enabled'] or not user_row['vpn_key']:
        await c.answer("⚠️ У вас нет активного VPN-ключа!", show_alert=True)
        return
        
    await c.answer("⏳ Генерация QR-кода...")
    
    config_text = user_row['vpn_key']
    
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_L,
        box_size=10,
        border=4,
    )
    qr.add_data(config_text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    bio = io.BytesIO()
    img.save(bio, "PNG")
    bio.seek(0)
    
    from aiogram.types import BufferedInputFile
    photo_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
    await c.message.answer_photo(
        photo=photo_file,
        caption="🖼 <b>QR-код для импорта в WireGuard:</b>\nОтсканируйте его камерой из приложения WireGuard для мгновенной настройки.",
        parse_mode="HTML"
    )

@dp.callback_query(F.data == "vpn:disable")
async def cb_vpn_disable(c: CallbackQuery):
    uid = c.from_user.id
    user_row = await db_manager.get_user(uid)
    await db_manager.set_user_vpn(uid, enabled=False)
    
    # Try cleaning peer from WireGuard server if configured
    try:
        if user_row and user_row['vpn_key'] and vpn_manager.VPN_SSH_HOST:
            import base64
            from cryptography.hazmat.primitives.asymmetric import x25519
            from cryptography.hazmat.primitives import serialization
            
            priv_key_match = re.search(r'PrivateKey\s*=\s*([a-zA-Z0-9+/=]+)', user_row['vpn_key'])
            if priv_key_match:
                priv_key_b64 = priv_key_match.group(1)
                priv_bytes = base64.b64decode(priv_key_b64)
                private_key = x25519.X25519PrivateKey.from_private_bytes(priv_bytes)
                public_key = private_key.public_key()
                pub_bytes = public_key.public_bytes(
                    encoding=serialization.Encoding.Raw,
                    format=serialization.PublicFormat.Raw
                )
                pub_key_b64 = base64.b64encode(pub_bytes).decode('utf-8')
                
                import asyncssh
                async with asyncssh.connect(vpn_manager.VPN_SSH_HOST, username=vpn_manager.VPN_SSH_USER, password=vpn_manager.VPN_SSH_PASSWORD, known_hosts=None) as conn:
                    await conn.run(f"sudo wg set wg0 peer {pub_key_b64} remove")
                    await conn.run(f"sudo sed -i '/{pub_key_b64}/,+2d' /etc/wireguard/wg0.conf")
    except Exception as e:
        logger.error(f"Failed to remove WG peer on server for {uid}: {e}")
        
    await c.answer("VPN успешно отключен", show_alert=True)
    await c.message.delete()
    await show_vpn_menu_directly(c.message, user_id=uid)

async def show_vpn_menu_directly(message: Message, user_id: int = None):
    uid = user_id or message.chat.id
    user_row = await db_manager.get_user(uid)
    vpn_enabled = user_row['vpn_enabled'] if user_row else False
    
    if vpn_enabled:
        text = (
            "🔌 <b>Ваша подписка на VPN активна!</b>\n\n"
            "Вы можете скачать файл конфигурации или отсканировать QR-код для быстрого импорта в приложение WireGuard.\n\n"
            "<b>Инструкция по настройке:</b>\n"
            "1. Установите приложение <b>WireGuard</b> из App Store или Google Play.\n"
            "2. Отсканируйте QR-код ниже или импортируйте файл конфигурации.\n"
            "3. Включите соединение в приложении."
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📁 Скачать файл .conf", callback_data="vpn:get_file"),
             InlineKeyboardButton(text="🖼 Показать QR-код", callback_data="vpn:get_qr")],
            [InlineKeyboardButton(text="❌ Отключить VPN", callback_data="vpn:disable")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="ai:close")]
        ])
    else:
        text = (
            "🔌 <b>Собственный VPN-сервис</b>\n\n"
            "Мы предоставляем стабильный, быстрый и безопасный доступ к зарубежным образовательным платформам и библиотекам.\n\n"
            "Выберите вариант подписки:\n"
            "1. <b>WireGuard VPN (отдельно)</b> — 100 ⭐ (на 30 дней)\n"
            "2. <b>VPN + 150 Стандарт ИИ</b> — 500 ⭐ (на 30 дней)\n"
            "3. <b>VPN + 30 Премиум ИИ (Claude, GPT, Kimi, Qwen)</b> — 600 ⭐ (на 30 дней)\n\n"
            "<i>Все тарифы рассчитаны для обеспечения 30%+ чистой прибыли в месяц для развития бота.</i>"
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔌 VPN на 30 дней (100 ⭐)", callback_data="vpn:buy_only")],
            [InlineKeyboardButton(text="💳 VPN + 150 Стандарт (500 ⭐)", callback_data="vpn:buy_standard")],
            [InlineKeyboardButton(text="💳 VPN + 30 Премиум (600 ⭐)", callback_data="vpn:buy_premium")],
            [InlineKeyboardButton(text="🔙 Назад", callback_data="ai:close")]
        ])
    await message.answer(text, reply_markup=kb, parse_mode="HTML")


# ═══════════════════ ПЛАТЕЖНЫЕ ХЭНДЛЕРЫ И АКТИВАЦИЯ КЛЮЧЕЙ ═══════════════════

@dp.callback_query(F.data == "vpn:activate_test_free")
async def cb_vpn_activate_test_free(c: CallbackQuery):
    uid = c.from_user.id
    try:
        await c.answer()
    except Exception:
        pass
    await c.message.answer("⏳ <b>Настройка вашего бесплатного тестового подключения...</b>", parse_mode="HTML")
    try:
        user_row = await db_manager.get_user(uid)
        if not user_row:
            await db_manager.register_or_update_user(uid, c.from_user.username)
            user_row = await db_manager.get_user(uid)
            
        user_db_id = user_row['id'] if user_row else 1
        
        # Compute new expiration times
        now = datetime.now()
        
        # VPN
        current_vpn_expires = user_row.get('vpn_expires_at') if user_row else None
        if current_vpn_expires and current_vpn_expires > now:
            new_vpn_expires = current_vpn_expires + timedelta(days=30)
        else:
            new_vpn_expires = now + timedelta(days=30)
            
        # AI
        current_ai_expires = user_row.get('ai_expires_at') if user_row else None
        if current_ai_expires and current_ai_expires > now:
            new_ai_expires = current_ai_expires + timedelta(days=30)
        else:
            new_ai_expires = now + timedelta(days=30)
            
        expires_days = int((new_ai_expires - now).total_seconds() / 86400)
        if expires_days < 30:
            expires_days = 30
        
        # Generate config and update user VPN status
        config_text = await vpn_manager.generate_user_vpn_config(user_db_id)
        await db_manager.set_user_vpn(uid, enabled=True, key=config_text, expires_at=new_vpn_expires, purchased_at=now)
        
        # Generate actual OpenRouter key
        limit_usd = 0.50  # 10 premium queries
        ai_key = await create_openrouter_key(limit_usd=limit_usd, expires_days=expires_days)
        await db_manager.set_user_ai_key(uid, ai_key, new_ai_expires, purchased_at=now)
        
        # Set AI balance to 10 queries
        async with db_manager.pool.acquire() as conn:
            await conn.execute("UPDATE users SET ai_balance = ai_balance + 10 WHERE telegram_id = $1", uid)
        
        # Send VPN file
        file_data = BufferedInputFile(config_text.encode("utf-8"), filename=f"tu_ugmk_vpn_{uid}.conf")
        await c.message.answer_document(
            document=file_data,
            caption="✅ <b>VPN успешно подключен!</b>\n\nИмпортируйте этот файл в приложение WireGuard.\nВы также можете получить QR-код для настройки через меню.",
            parse_mode="HTML"
        )
        
        # Generate & Send QR code
        qr = qrcode.QRCode(
            version=1,
            error_correction=qrcode.constants.ERROR_CORRECT_L,
            box_size=10,
            border=4,
        )
        qr.add_data(config_text)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        bio = io.BytesIO()
        img.save(bio, "PNG")
        bio.seek(0)
        photo_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
        
        await c.message.answer_photo(
            photo=photo_file,
            caption="🖼 <b>QR-код для импорта в WireGuard:</b>\nОтсканируйте его из приложения WireGuard для настройки.",
            parse_mode="HTML"
        )
        
        await c.message.answer(
            f"🎉 <b>Бесплатный тест успешно активирован!</b>\n\n"
            f"🔑 Мы сгенерировали для вас персональный API-ключ OpenRouter (10 премиум запросов):\n"
            f"<code>{ai_key}</code>\n\n"
            f"Он уже автоматически активирован и привязан к вашему профилю! Вы можете сразу общаться с ИИ.",
            parse_mode="HTML"
        )
    except Exception as e:
        logger.error(f"Failed to complete free VPN test setup: {e}")
        await c.message.answer(
            f"⚠️ <b>Произошла ошибка при настройке теста:</b>\n<code>{str(e)}</code>\n\n"
            f"Пожалуйста, обратитесь к администратору.",
            parse_mode="HTML"
        )

@dp.callback_query(F.data == "vpn:buy_standard")
async def cb_vpn_buy_standard(c: CallbackQuery):
    uid = c.from_user.id
    prices = [LabeledPrice(label="VPN + 150 Стандарт ИИ", amount=500)]
    try:
        await c.message.answer_invoice(
            title="VPN + 150 Стандарт ИИ",
            description="Подписка WireGuard VPN на 30 дней и промокод на 150 стандартных запросов к ИИ.",
            payload="vpn_sub_standard",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        await c.answer("Счет выставлен!")
    except Exception as e:
        logger.error(f"Failed to send invoice for VPN standard: {e}")
        await c.answer("⚠️ Не удалось выставить счет. Обратитесь к администратору.", show_alert=True)

@dp.callback_query(F.data == "vpn:buy_premium")
async def cb_vpn_buy_premium(c: CallbackQuery):
    uid = c.from_user.id
    prices = [LabeledPrice(label="VPN + 30 Премиум ИИ", amount=600)]
    try:
        await c.message.answer_invoice(
            title="VPN + 30 Премиум ИИ",
            description="Подписка WireGuard VPN на 30 дней и промокод на 30 премиум запросов к ИИ (Claude, GPT, Kimi, Qwen).",
            payload="vpn_sub_premium",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        await c.answer("Счет выставлен!")
    except Exception as e:
        logger.error(f"Failed to send invoice for VPN premium: {e}")
        await c.answer("⚠️ Не удалось выставить счет. Обратитесь к администратору.", show_alert=True)

@dp.callback_query(F.data == "vpn:buy_only")
async def cb_vpn_buy_only(c: CallbackQuery):
    uid = c.from_user.id
    prices = [LabeledPrice(label="WireGuard VPN на 30 дней", amount=100)]
    try:
        await c.message.answer_invoice(
            title="WireGuard VPN на 30 дней",
            description="Подписка на высокоскоростной WireGuard VPN сроком на 30 дней.",
            payload="vpn_only_30_days",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        await c.answer("Счет выставлен!")
    except Exception as e:
        logger.error(f"Failed to send invoice for VPN only: {e}")
        await c.answer("⚠️ Не удалось выставить счет. Обратитесь к администратору.", show_alert=True)

@dp.callback_query(F.data == "ai:buy_requests")
async def cb_ai_buy_requests(c: CallbackQuery):
    uid = c.from_user.id
    prices = [LabeledPrice(label="150 Стандарт ИИ-запросов", amount=400)]
    try:
        await c.message.answer_invoice(
            title="150 стандартных запросов к ИИ",
            description="Пополнение баланса ИИ-Ассистента на 150 стандартных (или 37 премиум) запросов.",
            payload="ai_150_requests",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        await c.answer("Счет выставлен!")
    except Exception as e:
        logger.error(f"Failed to send invoice for AI: {e}")
        await c.answer("⚠️ Не удалось выставить счет. Обратитесь к администратору.", show_alert=True)

@dp.callback_query(F.data == "ai:buy_premium")
async def cb_ai_buy_premium(c: CallbackQuery):
    uid = c.from_user.id
    prices = [LabeledPrice(label="30 Премиум ИИ-запросов", amount=500)]
    try:
        await c.message.answer_invoice(
            title="30 премиум запросов к ИИ",
            description="Пополнение баланса ИИ-Ассистента на 30 премиум (или 120 стандартных) запросов.",
            payload="ai_30_premium",
            provider_token="",
            currency="XTR",
            prices=prices
        )
        await c.answer("Счет выставлен!")
    except Exception as e:
        logger.error(f"Failed to send invoice for AI premium: {e}")
        await c.answer("⚠️ Не удалось выставить счет. Обратитесь к администратору.", show_alert=True)

@dp.pre_checkout_query()
async def process_pre_checkout_query(pre_checkout_query: PreCheckoutQuery):
    await pre_checkout_query.answer(ok=True)

@dp.message(F.successful_payment)
async def process_successful_payment(m: Message):
    payload = m.successful_payment.invoice_payload
    uid = m.from_user.id
    
    if payload in ["vpn_sub_standard", "vpn_sub_premium", "vpn_sub_test"]:
        is_premium = (payload in ["vpn_sub_premium", "vpn_sub_test"])
        is_test = (payload == "vpn_sub_test")
        await m.answer("⏳ <b>Настройка вашего VPN-подключения и генерация ключей...</b>", parse_mode="HTML")
        try:
            user_row = await db_manager.get_user(uid)
            if not user_row:
                await db_manager.register_or_update_user(uid, m.from_user.username)
                user_row = await db_manager.get_user(uid)
                
            user_db_id = user_row['id'] if user_row else 1
            
            # Compute new expiration times
            now = datetime.now()
            
            # VPN
            current_vpn_expires = user_row.get('vpn_expires_at') if user_row else None
            if current_vpn_expires and current_vpn_expires > now:
                new_vpn_expires = current_vpn_expires + timedelta(days=30)
            else:
                new_vpn_expires = now + timedelta(days=30)
                
            # AI
            current_ai_expires = user_row.get('ai_expires_at') if user_row else None
            if current_ai_expires and current_ai_expires > now:
                new_ai_expires = current_ai_expires + timedelta(days=30)
            else:
                new_ai_expires = now + timedelta(days=30)
                
            expires_days = int((new_ai_expires - now).total_seconds() / 86400)
            if expires_days < 30:
                expires_days = 30
            
            # Generate config and update user VPN status
            config_text = await vpn_manager.generate_user_vpn_config(user_db_id)
            await db_manager.set_user_vpn(uid, enabled=True, key=config_text, expires_at=new_vpn_expires, purchased_at=now)
            
            # Generate actual OpenRouter key
            if is_test:
                limit_usd = 0.50
            else:
                limit_usd = 1.50 if is_premium else 0.15
            ai_key = await create_openrouter_key(limit_usd=limit_usd, expires_days=expires_days)
            await db_manager.set_user_ai_key(uid, ai_key, new_ai_expires, purchased_at=now)
            
            # Set AI balance queries
            balance_add = 10 if is_test else (30 if is_premium else 150)
            async with db_manager.pool.acquire() as conn:
                await conn.execute("UPDATE users SET ai_balance = ai_balance + $2 WHERE telegram_id = $1", uid, balance_add)
            
            # Send VPN file
            file_data = BufferedInputFile(config_text.encode("utf-8"), filename=f"tu_ugmk_vpn_{uid}.conf")
            await m.answer_document(
                document=file_data,
                caption="✅ <b>VPN успешно подключен!</b>\n\nИмпортируйте этот файл в приложение WireGuard.\nВы также можете получить QR-код для настройки через меню.",
                parse_mode="HTML"
            )
            
            # Generate & Send QR code
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(config_text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, "PNG")
            bio.seek(0)
            photo_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
            
            await m.answer_photo(
                photo=photo_file,
                caption="🖼 <b>QR-код для импорта в WireGuard:</b>\nОтсканируйте его из приложения WireGuard для настройки.",
                parse_mode="HTML"
            )
            
            pkg_name = "10 премиум" if is_test else ("30 премиум" if is_premium else "150 стандартных")
            await m.answer(
                f"🎉 <b>Спасибо за покупку!</b>\n\n"
                f"🔑 Мы сгенерировали для вас персональный API-ключ OpenRouter ({pkg_name} запросов):\n"
                f"<code>{ai_key}</code>\n\n"
                f"Он уже автоматически активирован и привязан к вашему профилю! Вы можете сразу общаться с ИИ.",
                parse_mode="HTML"
            )
            
        except Exception as e:
            logger.error(f"Failed to complete VPN setup after payment: {e}")
            await m.answer(
                f"⚠️ <b>Произошла ошибка при настройке VPN:</b>\n<code>{str(e)}</code>\n\n"
                f"Пожалуйста, свяжитесь с администратором. Ваша оплата зафиксирована.",
                parse_mode="HTML"
            )
            
    elif payload in ["ai_150_requests", "ai_30_premium"]:
        is_premium = (payload == "ai_30_premium")
        try:
            user_row = await db_manager.get_user(uid)
            if not user_row:
                await db_manager.register_or_update_user(uid, m.from_user.username)
                user_row = await db_manager.get_user(uid)
                
            now = datetime.now()
            
            # AI Expiry only
            current_ai_expires = user_row.get('ai_expires_at') if user_row else None
            if current_ai_expires and current_ai_expires > now:
                new_ai_expires = current_ai_expires + timedelta(days=30)
            else:
                new_ai_expires = now + timedelta(days=30)
                
            expires_days = int((new_ai_expires - now).total_seconds() / 86400)
            if expires_days < 30:
                expires_days = 30
                
            # Generate actual OpenRouter key
            limit_usd = 1.50 if is_premium else 0.15
            ai_key = await create_openrouter_key(limit_usd=limit_usd, expires_days=expires_days)
            await db_manager.set_user_ai_key(uid, ai_key, new_ai_expires, purchased_at=now)
            
            # Set AI balance queries
            balance_add = 30 if is_premium else 150
            async with db_manager.pool.acquire() as conn:
                await conn.execute("UPDATE users SET ai_balance = ai_balance + $2 WHERE telegram_id = $1", uid, balance_add)
            
            pkg_name = "30 премиум" if is_premium else "150 стандартных"
            await m.answer(
                f"🎉 <b>Оплата прошла успешно!</b>\n\n"
                f"🔑 Персональный API-ключ OpenRouter с лимитом на {pkg_name} запросов привязан к вашему профилю:\n"
                f"<code>{ai_key}</code>\n\n"
                f"Вы можете сразу приступать к общению с ИИ!",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to add requests after payment: {e}")
            await m.answer(
                f"⚠️ <b>Произошла ошибка при обновлении баланса ИИ:</b>\n<code>{str(e)}</code>\n\n"
                f"Свяжитесь с администратором для начисления.",
                parse_mode="HTML"
            )
            
    elif payload == "vpn_only_30_days":
        await m.answer("⏳ <b>Настройка вашего VPN-подключения и генерация ключей...</b>", parse_mode="HTML")
        try:
            user_row = await db_manager.get_user(uid)
            if not user_row:
                await db_manager.register_or_update_user(uid, m.from_user.username)
                user_row = await db_manager.get_user(uid)
                
            user_db_id = user_row['id'] if user_row else 1
            
            now = datetime.now()
            current_vpn_expires = user_row.get('vpn_expires_at') if user_row else None
            if current_vpn_expires and current_vpn_expires > now:
                new_vpn_expires = current_vpn_expires + timedelta(days=30)
            else:
                new_vpn_expires = now + timedelta(days=30)
                
            config_text = await vpn_manager.generate_user_vpn_config(user_db_id)
            await db_manager.set_user_vpn(uid, enabled=True, key=config_text, expires_at=new_vpn_expires, purchased_at=now)
            
            # Send VPN file
            file_data = BufferedInputFile(config_text.encode("utf-8"), filename=f"tu_ugmk_vpn_{uid}.conf")
            await m.answer_document(
                document=file_data,
                caption="✅ <b>VPN успешно подключен!</b>\n\nИмпортируйте этот файл в приложение WireGuard.\nВы также можете получить QR-код для настройки через меню.",
                parse_mode="HTML"
            )
            
            # Generate & Send QR code
            qr = qrcode.QRCode(
                version=1,
                error_correction=qrcode.constants.ERROR_CORRECT_L,
                box_size=10,
                border=4,
            )
            qr.add_data(config_text)
            qr.make(fit=True)
            img = qr.make_image(fill_color="black", back_color="white")
            bio = io.BytesIO()
            img.save(bio, "PNG")
            bio.seek(0)
            photo_file = BufferedInputFile(bio.read(), filename="vpn_qr.png")
            
            await m.answer_photo(
                photo=photo_file,
                caption="🖼 <b>QR-код для импорта в WireGuard:</b>\nОтсканируйте его из приложения WireGuard для настройки.",
                parse_mode="HTML"
            )
            
            await m.answer("🎉 <b>VPN успешно продлен на 30 дней!</b>", parse_mode="HTML")
        except Exception as e:
            logger.error(f"Failed to complete VPN setup after payment: {e}")
            await m.answer(
                f"⚠️ <b>Произошла ошибка при настройке VPN:</b>\n<code>{str(e)}</code>\n\n"
                f"Пожалуйста, свяжитесь с администратором. Ваша оплата зафиксирована.",
                parse_mode="HTML"
            )

@dp.callback_query(F.data == "ai:activate_key")
async def cb_ai_activate_key(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_activation_key)
    await c.message.answer(
        "🔑 <b>Активация ИИ-ключа</b>\n\n"
        "Пожалуйста, пришлите ваш ключ доступа (в формате <code>UGMK-AI-XXXXXX</code>) в ответ на это сообщение.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="ai_cancel_settings")]]),
        parse_mode="HTML"
    )
    await c.answer()

@dp.message(UserStates.waiting_for_activation_key)
async def process_ai_activation_key(m: Message, state: FSMContext):
    key_val = m.text.strip().upper()
    uid = m.from_user.id
    
    limit = await db_manager.activate_ai_key(key_val, uid)
    if limit > 0:
        await state.clear()
        await m.answer(
            f"🎉 <b>Успешно активировано!</b>\n"
            f"На ваш баланс зачислено <b>{limit}</b> ИИ-запросов.",
            parse_mode="HTML"
        )
        await show_ai_menu_directly(m)
    else:
        await m.answer(
            "❌ <b>Неверный или уже использованный ключ!</b>\n"
            "Пожалуйста, проверьте правильность ввода или обратитесь к администратору.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="ai_cancel_settings")]]),
            parse_mode="HTML"
        )

@dp.message(Command("activate"))
async def cmd_activate_key(m: Message):
    parts = m.text.split(maxsplit=1)
    if len(parts) < 2:
        await m.answer(
            "⚠️ <b>Использование команды:</b>\n"
            "<code>/activate UGMK-AI-XXXXXX</code>",
            parse_mode="HTML"
        )
        return
        
    key_val = parts[1].strip().upper()
    uid = m.from_user.id
    
    limit = await db_manager.activate_ai_key(key_val, uid)
    if limit > 0:
        await m.answer(
            f"🎉 <b>Успешно активировано!</b>\n"
            f"На ваш баланс зачислено <b>{limit}</b> ИИ-запросов.",
            parse_mode="HTML"
        )
    else:
        await m.answer(
            "❌ <b>Неверный или уже использованный ключ!</b>\n"
            "Пожалуйста, проверьте правильность ввода.",
            parse_mode="HTML"
        )

@dp.message(F.text.regexp(r'(?i)UGMK-AI-[A-Z0-9]{8}'))
async def auto_activate_key(m: Message):
    match = re.search(r'(?i)UGMK-AI-[A-Z0-9]{8}', m.text)
    if not match:
        return
    key_val = match.group(0).upper()
    uid = m.from_user.id
    
    limit = await db_manager.activate_ai_key(key_val, uid)
    if limit > 0:
        await m.answer(
            f"🎉 <b>Обнаружен ключ активации!</b>\n"
            f"Ключ: <code>{key_val}</code>\n"
            f"На ваш баланс зачислено <b>{limit}</b> ИИ-запросов.",
            parse_mode="HTML"
        )
    else:
        await m.answer(
            "❌ <b>Обнаружен ключ, но он недействителен или уже активирован.</b>",
            parse_mode="HTML"
        )


# ═══════════════════ СТУДЕНЧЕСКАЯ ЭКОСИСТЕМА ═══════════════════
@dp.message(F.text == "🏫 Экосистема")
@dp.message(Command("ecosystem"))
async def ecosystem_menu(m: Message, state: FSMContext):
    await state.clear()
    msg = await m.answer("🏫 Открываю экосистему...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    uid = m.from_user.id
    is_admin = uid in ADMIN_IDS
    
    text = (
        "🏫 <b>Студенческая экосистема ТУ УГМК</b>\n\n"
        "Добро пожаловать в единую экосистему! Здесь вы найдете:\n"
        "• 📅 <b>Афишу мероприятий</b> — будьте в курсе главных событий университета.\n"
        "• 📢 <b>Каталог сообществ</b> — ссылки на студенческие чаты, клубы и полезные каналы."
    )
    
    kb_rows = [
        [InlineKeyboardButton(text="📅 Афиша мероприятий", callback_data="eco:events"),
         InlineKeyboardButton(text="📢 Каталог сообществ", callback_data="eco:channels")]
    ]
    if is_admin:
        kb_rows.append([InlineKeyboardButton(text="⚙️ Панель редактора афиши/каталога", callback_data="eco:admin_panel")])
    
    await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")

@dp.callback_query(F.data == "eco:events")
async def cb_eco_events(c: CallbackQuery):
    events = await db_manager.get_events()
    if not events:
        await c.message.edit_text(
            "📅 <b>Афиша мероприятий ТУ УГМК</b>\n\n"
            "😴 Пока нет запланированных мероприятий. Следите за обновлениями!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="eco:back")]]),
            parse_mode="HTML"
        )
        await c.answer()
        return
        
    text = ["📅 <b>Предстоящие мероприятия:</b>\n"]
    for i, ev in enumerate(events, 1):
        date_str = ev['event_date'].strftime("%d.%m.%Y %H:%M") if ev['event_date'] else "Н/Д"
        link_str = f" | <a href='{ev['link']}'>Подробнее</a>" if ev['link'] else ""
        text.append(
            f"{i}️⃣ <b>{ev['title']}</b>\n"
            f"   🕒 <code>{date_str}</code>\n"
            f"   📝 {ev['description'] or 'Без описания'}{link_str}\n"
            "────────────────────"
        )
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="eco:back")]])
    await c.message.edit_text("\n".join(text), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data == "eco:channels")
async def cb_eco_channels(c: CallbackQuery):
    channels = await db_manager.get_channels()
    if not channels:
        await c.message.edit_text(
            "📢 <b>Каталог студенческих сообществ</b>\n\n"
            "😴 Каталог временно пуст.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="eco:back")]]),
            parse_mode="HTML"
        )
        await c.answer()
        return
        
    cats = {}
    for ch in channels:
        cat = ch['category'] or "Разное"
        if cat not in cats:
            cats[cat] = []
        cats[cat].append(ch)
        
    text = ["📢 <b>Каталог студенческих сообществ:</b>\n"]
    for cat, items in cats.items():
        text.append(f"📂 <b>{cat.upper()}</b>")
        for item in items:
            text.append(f"   • <a href='{item['link']}'>{item['name']}</a>")
        text.append("")
        
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="eco:back")]])
    await c.message.edit_text("\n".join(text), reply_markup=kb, parse_mode="HTML", disable_web_page_preview=True)
    await c.answer()

@dp.callback_query(F.data == "eco:back")
async def cb_eco_back(c: CallbackQuery, state: FSMContext):
    await state.clear()
    uid = c.from_user.id
    is_admin = uid in ADMIN_IDS
    text = (
        "🏫 <b>Студенческая экосистема ТУ УГМК</b>\n\n"
        "Добро пожаловать в единую экосистему! Здесь вы найдете:\n"
        "• 📅 <b>Афишу мероприятий</b> — будьте в курсе главных событий университета.\n"
        "• 📢 <b>Каталог сообществ</b> — ссылки на студенческие чаты, клубы и полезные каналы."
    )
    kb_rows = [
        [InlineKeyboardButton(text="📅 Афиша мероприятий", callback_data="eco:events"),
         InlineKeyboardButton(text="📢 Каталог сообществ", callback_data="eco:channels")]
    ]
    if is_admin:
        kb_rows.append([InlineKeyboardButton(text="⚙️ Панель редактора афиши/каталога", callback_data="eco:admin_panel")])
    await c.message.edit_text(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=kb_rows), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data == "eco:admin_panel", F.from_user.id.in_(ADMIN_IDS))
async def cb_eco_admin(c: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Добавить событие", callback_data="eco_adm:add_event"),
         InlineKeyboardButton(text="🗑 Удалить событие", callback_data="eco_adm:del_event")],
        [InlineKeyboardButton(text="📢 Добавить ссылку", callback_data="eco_adm:add_chan"),
         InlineKeyboardButton(text="🗑 Удалить ссылку", callback_data="eco_adm:del_chan")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="eco:back")]
    ])
    await c.message.edit_text("⚙️ <b>Панель управления афишей и каталогом:</b>", reply_markup=kb, parse_mode="HTML")
    await c.answer()

# Add Event
@dp.callback_query(F.data == "eco_adm:add_event", F.from_user.id.in_(ADMIN_IDS))
async def cb_eco_add_event(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_event_title)
    await c.message.edit_text(
        "📅 <b>Добавление события в афишу</b>\n\nВведите название мероприятия:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="eco:admin_panel")]]),
        parse_mode="HTML"
    )
    await c.answer()

@dp.message(AdminStates.waiting_for_event_title, F.from_user.id.in_(ADMIN_IDS))
async def process_event_title(m: Message, state: FSMContext):
    await state.update_data(ev_title=m.text.strip())
    await state.set_state(AdminStates.waiting_for_event_desc)
    await m.answer("📅 Введите описание мероприятия (или «-», если описания нет):")

@dp.message(AdminStates.waiting_for_event_desc, F.from_user.id.in_(ADMIN_IDS))
async def process_event_desc(m: Message, state: FSMContext):
    desc = m.text.strip()
    await state.update_data(ev_desc="" if desc == "-" else desc)
    await state.set_state(AdminStates.waiting_for_event_date)
    await m.answer("📅 Введите дату и время в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b> (например, <code>15.09.2026 18:00</code>):", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_event_date, F.from_user.id.in_(ADMIN_IDS))
async def process_event_date(m: Message, state: FSMContext):
    try:
        dt = datetime.strptime(m.text.strip(), "%d.%m.%Y %H:%M")
        await state.update_data(ev_date=dt)
        await state.set_state(AdminStates.waiting_for_event_link)
        await m.answer("📅 Введите ссылку на мероприятие (или «-», если ссылки нет):")
    except ValueError:
        await m.answer("❌ <b>Неверный формат даты!</b> Введите дату в формате <b>ДД.ММ.ГГГГ ЧЧ:ММ</b>:")

@dp.message(AdminStates.waiting_for_event_link, F.from_user.id.in_(ADMIN_IDS))
async def process_event_link(m: Message, state: FSMContext):
    data = await state.get_data()
    title = data.get("ev_title")
    desc = data.get("ev_desc")
    dt = data.get("ev_date")
    link = m.text.strip()
    link = "" if link == "-" else link
    
    await state.clear()
    await db_manager.add_event(title, desc, dt, link)
    await m.answer("✅ <b>Мероприятие успешно добавлено в афишу!</b>", parse_mode="HTML")
    await show_eco_admin_panel(m)

# Delete Event
@dp.callback_query(F.data == "eco_adm:del_event", F.from_user.id.in_(ADMIN_IDS))
async def cb_eco_del_event(c: CallbackQuery):
    events = await db_manager.get_events()
    if not events:
        await c.answer("Афиша уже пуста!", show_alert=True)
        return
        
    btns = []
    for ev in events:
        btns.append([InlineKeyboardButton(text=f"❌ {ev['title'][:30]}", callback_data=f"eco_adm:del_ev_id:{ev['id']}")])
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="eco:admin_panel")])
    
    await c.message.edit_text("🗑 <b>Выберите мероприятие для удаления:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("eco_adm:del_ev_id:"), F.from_user.id.in_(ADMIN_IDS))
async def cb_eco_del_event_confirm(c: CallbackQuery):
    ev_id = int(c.data.split(":")[3])
    await db_manager.delete_event(ev_id)
    await c.answer("Событие удалено")
    await cb_eco_del_event(c)

# Add Channel
@dp.callback_query(F.data == "eco_adm:add_chan", F.from_user.id.in_(ADMIN_IDS))
async def cb_eco_add_chan(c: CallbackQuery, state: FSMContext):
    await state.set_state(AdminStates.waiting_for_channel_name)
    await c.message.edit_text(
        "📢 <b>Добавление чата/канала в каталог</b>\n\nВведите название сообщества/канала:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="eco:admin_panel")]]),
        parse_mode="HTML"
    )
    await c.answer()

@dp.message(AdminStates.waiting_for_channel_name, F.from_user.id.in_(ADMIN_IDS))
async def process_chan_name(m: Message, state: FSMContext):
    await state.update_data(ch_name=m.text.strip())
    await state.set_state(AdminStates.waiting_for_channel_link)
    await m.answer("📢 Введите ссылку на сообщество (например, <code>https://t.me/...</code>):", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_channel_link, F.from_user.id.in_(ADMIN_IDS))
async def process_chan_link(m: Message, state: FSMContext):
    link = m.text.strip()
    if not link.startswith("http"):
        await m.answer("❌ Ссылка должна начинаться с http/https. Введите ссылку снова:")
        return
    await state.update_data(ch_link=link)
    await state.set_state(AdminStates.waiting_for_channel_cat)
    await m.answer("📢 Введите категорию (например: <code>Студсовет</code>, <code>Спорт</code>, <code>Культура</code>, <code>Обучение</code>):", parse_mode="HTML")

@dp.message(AdminStates.waiting_for_channel_cat, F.from_user.id.in_(ADMIN_IDS))
async def process_chan_cat(m: Message, state: FSMContext):
    data = await state.get_data()
    name = data.get("ch_name")
    link = data.get("ch_link")
    cat = m.text.strip()
    
    await state.clear()
    await db_manager.add_channel(name, link, cat)
    await m.answer(f"✅ <b>Сообщество «{name}» успешно добавлено в каталог!</b>", parse_mode="HTML")
    await show_eco_admin_panel(m)

# Delete Channel
@dp.callback_query(F.data == "eco_adm:del_chan", F.from_user.id.in_(ADMIN_IDS))
async def cb_eco_del_chan(c: CallbackQuery):
    channels = await db_manager.get_channels()
    if not channels:
        await c.answer("Каталог уже пуст!", show_alert=True)
        return
        
    btns = []
    for ch in channels:
        btns.append([InlineKeyboardButton(text=f"❌ [{ch['category']}] {ch['name'][:25]}", callback_data=f"eco_adm:del_ch_id:{ch['id']}")])
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="eco:admin_panel")])
    await c.message.edit_text("🗑 <b>Выберите ссылку для удаления:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("eco_adm:del_ch_id:"), F.from_user.id.in_(ADMIN_IDS))
async def cb_eco_del_chan_confirm(c: CallbackQuery):
    ch_id = int(c.data.split(":")[3])
    await db_manager.delete_channel(ch_id)
    await c.answer("Ссылка удалена")
    await cb_eco_del_chan(c)

async def show_eco_admin_panel(message: Message):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📅 Добавить событие", callback_data="eco_adm:add_event"),
         InlineKeyboardButton(text="🗑 Удалить событие", callback_data="eco_adm:del_event")],
        [InlineKeyboardButton(text="📢 Добавить ссылку", callback_data="eco_adm:add_chan"),
         InlineKeyboardButton(text="🗑 Удалить ссылку", callback_data="eco_adm:del_chan")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="eco:back")]
    ])
    await message.answer("⚙️ <b>Панель управления афишей и каталогом:</b>", reply_markup=kb, parse_mode="HTML")


# ═══════════════════ ОПРОСЫ СТАРОСТЫ ═══════════════════
@dp.callback_query(F.data == "st_dash:create_poll")
async def cb_st_create_poll(c: CallbackQuery, state: FSMContext):
    uid = str(c.from_user.id)
    group = await dao.hget("starosta_group_saved", uid)
    if not group:
        await c.answer("❌ Сначала выберите вашу группу!", show_alert=True)
        return
        
    await c.message.edit_text(
        "📊 <b>Создание опроса группы</b>\n\n"
        "Шаг 1: Введите текст вопроса для студентов вашей группы:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="st_dash:back")]]),
        parse_mode="HTML"
    )
    await state.set_state(StarostStates.waiting_for_poll_question)
    await c.answer()

@dp.message(StarostStates.waiting_for_poll_question)
async def process_poll_question(m: Message, state: FSMContext):
    question = m.text.strip()
    await state.update_data(poll_question=question)
    await state.set_state(StarostStates.waiting_for_poll_options)
    await m.answer(
        "📊 <b>Создание опроса группы</b>\n\n"
        "Шаг 2: Введите варианты ответов.\n"
        "Каждый вариант должен быть на <b>новой строке</b>.\n"
        "Пример:\n"
        "<code>Да\nНет\nНе смогу прийти</code>\n\n"
        "Минимум 2 варианта, максимум 10.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Отмена", callback_data="st_dash:back")]]),
        parse_mode="HTML"
    )

@dp.message(StarostStates.waiting_for_poll_options)
async def process_poll_options(m: Message, state: FSMContext):
    data = await state.get_data()
    question = data.get("poll_question")
    uid = str(m.from_user.id)
    group = await dao.hget("starosta_group_saved", uid)
    starosta_name = await dao.hget("starosta_name", uid)
    
    options = [opt.strip() for opt in m.text.split("\n") if opt.strip()]
    if len(options) < 2 or len(options) > 10:
        await m.answer("❌ Вариантов должно быть от 2 до 10. Пожалуйста, отправьте список вариантов снова:")
        return
        
    await state.clear()
    
    poll_id = await db_manager.create_poll(
        creator_id=m.from_user.id,
        group_name=group,
        question=question,
        options=options
    )
    
    subs = await dao.hgetall("user_subs")
    target_users = [uid_sub for uid_sub, gid in subs.items() if gid == group]
    
    if not target_users:
        await m.answer(f"✅ Опрос создан, но в группе <b>{group}</b> еще нет подписчиков.", parse_mode="HTML")
        await show_starosta_dashboard(m, m.from_user.id)
        return
        
    await m.answer(f"🚀 <b>Опрос успешно создан!</b> Рассылаю {len(target_users)} студентам группы...", parse_mode="HTML")
    
    poll_text = f"📊 <b>Опрос от старосты ({starosta_name}):</b>\n\n💬 <code>{question}</code>"
    
    kb_btns = []
    for i, opt in enumerate(options):
        kb_btns.append([InlineKeyboardButton(text=opt, callback_data=f"vote:{poll_id}:{i}")])
    kb = InlineKeyboardMarkup(inline_keyboard=kb_btns)
    
    success = 0
    for t_uid in target_users:
        try:
            await bot.send_message(int(t_uid), poll_text, reply_markup=kb, parse_mode="HTML")
            success += 1
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Failed to send poll to {t_uid}: {e}")
            
    await m.answer(f"✅ Опрос успешно разослан! Доставлено: <b>{success} из {len(target_users)}</b>.", parse_mode="HTML")
    await show_starosta_dashboard(m, m.from_user.id)

@dp.callback_query(F.data == "st_dash:poll_results")
async def cb_st_poll_results(c: CallbackQuery):
    uid = str(c.from_user.id)
    group = await dao.hget("starosta_group_saved", uid)
    if not group:
        await c.answer("❌ Сначала выберите вашу группу!", show_alert=True)
        return
        
    poll = await db_manager.get_active_poll_for_group(group)
    if not poll:
        await c.message.edit_text(
            "😴 <b>В вашей группе еще не создавались опросы.</b>",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")]]),
            parse_mode="HTML"
        )
        await c.answer()
        return
        
    poll_id = poll['id']
    question = poll['question']
    options = json.loads(poll['options'])
    
    results, total_votes = await db_manager.get_poll_results(poll_id)
    
    res_text = [f"📈 <b>Результаты опроса:</b>\n«<code>{question}</code>»\n"]
    for i, opt in enumerate(options):
        votes = results.get(i, 0)
        pct = (votes / total_votes * 100) if total_votes > 0 else 0
        res_text.append(f"• <b>{opt}</b>: {votes} чел. ({pct:.1f}%)")
        
    res_text.append(f"\n👥 Всего проголосовало: <b>{total_votes}</b> чел.")
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить результаты", callback_data="st_dash:poll_results")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")]
    ])
    
    await c.message.edit_text("\n".join(res_text), reply_markup=kb, parse_mode="HTML")
    await c.answer()

@dp.callback_query(F.data.startswith("vote:"))
async def cb_user_vote(c: CallbackQuery):
    _, poll_id_str, opt_idx_str = c.data.split(":")
    poll_id = int(poll_id_str)
    opt_idx = int(opt_idx_str)
    uid = c.from_user.id
    
    poll = await db_manager.get_poll(poll_id)
    if not poll:
        await c.answer("❌ Опрос не найден.", show_alert=True)
        return
        
    options = json.loads(poll['options'])
    chosen_option = options[opt_idx]
    
    await db_manager.vote_poll(poll_id, uid, opt_idx)
    await c.answer(f"✅ Ваш голос за «{chosen_option}» учтен!", show_alert=True)


@dp.message(F.text)
async def fallback_message(m: Message, state: FSMContext):        
    data = await state.get_data()
    val = data.get("target_value")
    await m.answer("👇 Пожалуйста, воспользуйтесь кнопками меню внизу.", reply_markup=get_main_menu(val))     

if __name__ == "__main__":
    try: asyncio.run(main())
    except (KeyboardInterrupt, SystemExit): logger.info("Бот остановлен.")
    except Exception as e: logger.critical(f"Критическая ошибка: {e}", exc_info=True)
