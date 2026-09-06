import os
import re
import json
import base64
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
    BufferedInputFile,
    WebAppInfo, MenuButtonDefault, MenuButtonWebApp
)
from aiogram.filters import CommandStart, Command, CommandObject # type: ignore
from aiogram.fsm.storage.memory import MemoryStorage # type: ignore
from aiogram.fsm.state import State, StatesGroup # type: ignore
from aiogram.fsm.context import FSMContext # type: ignore
from aiogram.exceptions import TelegramBadRequest # type: ignore
from aiogram.dispatcher.middlewares.base import BaseMiddleware # type: ignore
import redis.asyncio as redis # type: ignore
from secure_store import SecureStore
from db_manager import db_manager
from ai_manager import get_ai_response, get_chat_models, normalize_model_id, filter_chat_models, normalize_chat_image
import io

# ═══════════════════ НАСТРОЙКИ ═══════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN")
PROXY_URL = os.getenv("PROXY_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://your-bot-domain.ru")

if not BOT_TOKEN:
    raise ValueError("⚠️ BOT_TOKEN не найден! Убедитесь, что он указан в .env файле или переменных окружения.")

DATA_DIR, CACHE_DIR, USERS_FILE = "data", "cache", os.path.join("data", "users.json")
CACHE_LIFETIME = 86400
MSG_STORE_LIMIT = 172800 # 48 часов
ADMIN_IDS = [474095004]
YEKATERINBURG_TZ = timezone(timedelta(hours=5))

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

class InMemoryLogHandler(logging.Handler):
    def __init__(self, limit=100):
        super().__init__()
        self.limit = limit
        self.logs = collections.deque(maxlen=limit)

    def emit(self, record):
        try:
            msg = self.format(record)
            self.logs.append(msg)
        except Exception:
            self.handleError(record)

in_memory_logs = InMemoryLogHandler()
in_memory_logs.setFormatter(logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s'))
logging.getLogger().addHandler(in_memory_logs)

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

class BlacklistMiddleware(BaseMiddleware):
    async def __call__(self, handler, event, data):
        try:
            user = event.from_user
            if user:
                user_id = user.id
                cache_key = f"user_blacklisted:{user_id}"
                cached_val = await dao.get(cache_key)
                if cached_val is not None:
                    is_blacklisted = (cached_val == "1")
                else:
                    is_blacklisted = await db_manager.is_user_blacklisted(user_id)
                    await dao.setex(cache_key, 300, "1" if is_blacklisted else "0")
                
                if is_blacklisted:
                    if isinstance(event, Message):
                        await event.answer("⚠️ Вы заблокированы и находитесь в черном списке.")
                    elif isinstance(event, CallbackQuery):
                        await event.answer("⚠️ Вы заблокированы и находитесь в черном списке.", show_alert=True)
                    return
        except Exception as e:
            logger.error(f"Blacklist check failed: {e}")
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
                markup = getattr(method, "reply_markup", None)
                if isinstance(markup, ReplyKeyboardMarkup) and any(
                    b.text == "📅 Мое расписание" for row in markup.keyboard for b in row
                ):
                    await send_app_launcher(bot, result.chat.id)
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
dp.message.middleware(BlacklistMiddleware())
dp.callback_query.middleware(BlacklistMiddleware())
dp.message.middleware(MaintenanceMiddleware())       
dp.callback_query.middleware(MaintenanceMiddleware())
dp.message.middleware(AntiFloodMiddleware())
dp.callback_query.middleware(AntiFloodMiddleware())      

# --- DATABASES ---
from schedule_config import GROUPS_DB, CACHE_VERSION, canonical_group, active_group, merged_groups, lesson_matches_group, migrate_group_preferences

DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]        

async def get_groups_db() -> dict:
    db = merged_groups()
    try:
        redis_db = await dao.hgetall("db_groups")
        if redis_db: db = merged_groups(redis_db)
    except Exception as e: logger.error(f"Error fetching groups from Redis: {e}")
    return db

# --- SCHEDULE MANAGER ---
class ScheduleManager:
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None) -> dict:
        if wo not in (0, 1):
            return {}
        mon = datetime.now(YEKATERINBURG_TZ).date() - timedelta(days=datetime.now(YEKATERINBURG_TZ).weekday()) + timedelta(weeks=wo)
        sd = mon.strftime("%d.%m.%Y")
        key = f"data:v{CACHE_VERSION}:{sd}:{t_type}:{t_val}"
        try:
            if await dao.exists(key): return json.loads(await dao.get(key))
        except Exception as e: logger.error(f"Redis get error: {e}")
        if await dao.set(f"queued:{key}", "1", nx=True, ex=120):
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
    waiting_for_ai_prompt = State()
    waiting_for_ai_key = State()
    waiting_for_model_search = State()

class StarostStates(StatesGroup):
    waiting_for_password = State()
    waiting_for_new_pass = State()
    waiting_for_name = State()
    waiting_for_group_name = State()
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
    h = datetime.now(YEKATERINBURG_TZ).hour
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
            [KeyboardButton(text="🧹 Очистить")]
        ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True)

async def send_app_launcher(bot_client, chat_id):
    """Keep one launcher in the conversation whenever the main menu is shown."""
    previous = await dao.get(f"miniapp_launcher:{chat_id}")
    message = await bot_client.send_message(
        chat_id,
        "🎓 <b>ТУ УГМК · Кампус</b>\n\nРасписание, ИИ и жизнь университета — в одном приложении.",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(text="Открыть приложение ↗", web_app=WebAppInfo(url=f"{WEBAPP_URL.rstrip('/')}/webapp"))
        ]]),
    )
    await dao.set(f"miniapp_launcher:{chat_id}", message.message_id)
    if previous:
        try:
            await bot_client.delete_message(chat_id, int(previous))
        except TelegramBadRequest:
            pass
    return message


def get_submenu_keyboard():
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔙 Назад"), KeyboardButton(text="🧹 Очистить")]
        ],
        resize_keyboard=True
    )

def get_day_pagination_kb(target_date: date):        
    today = datetime.now(YEKATERINBURG_TZ).date()
    monday = today - timedelta(days=today.weekday())
    arrows = []
    if target_date > monday:
        arrows.append(InlineKeyboardButton(text="⬅️ Пред. день", callback_data=f"day_nav:{(target_date - timedelta(days=1)).isoformat()}"))
    if target_date < monday + timedelta(days=13):
        arrows.append(InlineKeyboardButton(text="След. день ➡️", callback_data=f"day_nav:{(target_date + timedelta(days=1)).isoformat()}"))
    return InlineKeyboardMarkup(inline_keyboard=[arrows,
        [InlineKeyboardButton(text="📅 Экспорт iCal", callback_data="ical:export"),
         InlineKeyboardButton(text="🔙 Назад", callback_data="cancel_menu")]
    ])       

async def format_lesson(l: dict, day_name: str, group_name: str) -> str:
    subj, l_type, time, room, teach = (l.get(k, 'Н/Д') for k in ['subject', 'type', 'time', 'room', 'teacher'])
    link = l.get('link')

    text = f"📖 <b>{subj}</b>\n"
    if l_type and l_type != 'Н/Д':
        text += f"   📝 <i>{l_type}</i>\n"
    text += f"   └ <code>{time}</code> | 🚪 <code>{room}</code>\n"
    
    if link:
        text += f"   └ 💻 <a href='{link}'>Подключиться онлайн</a>\n"

    text += f"   └ 👤 {teach}"
    try:
        hw = await dao.hget(f"homework:{group_name}", f"{day_name}:{time}")
        if hw: text += f"\n   ✍️ <b>Д/З:</b> <i>{hw}</i>"
    except Exception as e: logger.error(f"Homework read error: {e}")

    return text

async def fmt_day(day_date: date, lessons: list, group_name: str = "") -> str:
    day_name, date_str = DAYS_OF_WEEK[day_date.weekday()], day_date.strftime("%d.%m.%Y")
    text = f"<b>📅 {day_name.upper()}</b> ({date_str})\n" + "─" * 24 + "\n\n"
    if not lessons: return text + "😴 Нет занятий"   
    sorted_lessons = sorted(lessons, key=lambda x: x.get('time', '00:00'))
    formatted_lessons = []
    for l in sorted_lessons:
        formatted_lessons.append(await format_lesson(l, day_name, group_name))
    return text + "\n\n".join(formatted_lessons)

async def fmt_week(s: dict, group_name: str = "") -> str:
    full_text = ""
    for day_name in DAYS_OF_WEEK[:6]: # type: ignore
        if d_str := s.get("_dates", {}).get(day_name):
            d_date, d_lessons = datetime.strptime(d_str, "%d.%m.%Y").date(), s.get(day_name, [])
            full_text += await fmt_day(d_date, d_lessons, group_name) + "\n\n" + "═" * 24 + "\n\n" # type: ignore
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
        
        text = (f"📈 <b>Детальная статистика бота:</b>\n\n"
                f"👤 <b>Всего пользователей:</b> {total_users}\n"
                f"🔔 <b>С активной подпиской:</b> {subbed_users}\n\n"
                f"🎓 <b>Топ-10 популярных групп:</b>\n{top_groups}\n\n"
                f"🌅 <b>Утренние рассылки (топ время):</b>\n{top_morn}\n\n"
                f"📂 <b>Размер динамической БД:</b>\n"
                f"  • Групп: {db_g_size}")
                
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
            for name in (await get_groups_db()).keys():
                for wo in [0, 1]:
                    job = {
                        "week_offset": wo,
                        "target_type": "group",
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
        try:
            subs = await dao.hgetall("user_subs")
            admin_gid = subs.get(str(c.from_user.id))
            
            if not admin_gid:
                await c.message.edit_text("❌ Вы не подписаны на утреннюю рассылку.\nПерейдите в меню '🔔 Моя подписка', выберите любую группу и попробуйте снова.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
            else:
                today = datetime.now(YEKATERINBURG_TZ).date()
                week_s = await sm.fetch_schedule(0, "group", admin_gid)
                day_name = DAYS_OF_WEEK[today.weekday()]
                day_lessons = week_s.get(day_name, [])
                is_error = not week_s or "_error" in week_s
                
                if is_error:
                    error_msg = week_s.get('_error', 'Таймаут ожидания (очередь перегружена)') if week_s else 'Таймаут ожидания'
                    await c.message.edit_text(f"❌ Ошибка при получении расписания.\nПричина: <b>{error_msg}</b>", parse_mode="HTML", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="admin:back")]]))
                else:
                    text = f"🧪 <b>ТЕСТ УТРЕННЕЙ РАССЫЛКИ</b>\n\n{get_greeting()} <b>Расписание на сегодня:</b>\n\n"
                    text += await fmt_day(today, day_lessons, admin_gid)
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
        now = datetime.now(YEKATERINBURG_TZ)
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
    tomorrow = datetime.now(YEKATERINBURG_TZ).date() + timedelta(days=1)
    count = 0
    for user_id, group_name in users.items():
        if user_times.get(user_id) != target_time:
            continue
        try:
            week_s = await sm.fetch_schedule(0, "group", group_name)
            day_lessons = week_s.get(DAYS_OF_WEEK[tomorrow.weekday()], [])
            if day_lessons:
                text = f"{get_greeting()} <b>Расписание на завтра, {tomorrow.strftime('%d.%m')}:</b>\n\n" + await fmt_day(tomorrow, day_lessons, group_name)
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
            today = datetime.now(YEKATERINBURG_TZ).date()
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
        now_dt = datetime.now(YEKATERINBURG_TZ)
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


@dp.message(Command("bot_logs"), F.from_user.id.in_(ADMIN_IDS))
async def cmd_bot_logs(m: Message):
    logs_list = list(in_memory_logs.logs)
    if not logs_list:
        await m.answer("Logs list is empty.")
        return
    text = "\n".join(logs_list[-35:])  # last 35 log lines
    if len(text) > 4000:
        text = text[-4000:]
    await m.answer(f"<pre>{text}</pre>", parse_mode="HTML")

@dp.message(CommandStart())
async def start(m: Message, state: FSMContext, command: CommandObject = None):      
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
    # Get notification times
    morn_time = await dao.hget("user_morning_time", str(uid)) or "08:00"
    eve_time = await dao.hget("user_evening_time", str(uid)) or "Отключено"
    
    text = (
        "🔔 <b>Моя подписка на расписание</b>\n\n"
        f"🎓 <b>Ваша группа:</b> <code>{group_name}</code>\n\n"
        f"🕒 <b>Ежедневная рассылка расписания:</b>\n"
        f"• Утро: <code>{morn_time}</code>\n"
        f"• Вечер: <code>{eve_time}</code>"
    )
    
    kb_rows = []
    
    # Group row
    kb_rows.append([InlineKeyboardButton(text="🎓 Выбрать/Изменить группу", callback_data="sub:change_group")])
    
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
    await show_group_name_menu(c)
    await c.answer()


@dp.callback_query(F.data == "sub:back_to_menu")
async def cb_sub_back_to_menu(c: CallbackQuery):
    await show_subscription_time_menu(c)
    await c.answer()


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
        await show_group_name_menu(m)

def get_group_name(group: str) -> str:
    """Return the textual part of a group name before its numeric code."""
    return group.split("-", maxsplit=1)[0].strip()


def get_group_names() -> list[str]:
    names = {get_group_name(group).casefold(): get_group_name(group) for group in GROUPS_DB}
    return sorted(names.values(), key=str.casefold)


async def show_group_name_menu(m_or_c):
    group_names = get_group_names()
    btns = [
        [
            InlineKeyboardButton(text=group_name, callback_data=f"group_name:{group_name}")
            for group_name in group_names[index:index + 2]
        ]
        for index in range(0, len(group_names), 2)
    ]
    btns.append([InlineKeyboardButton(text="🔙 Назад в подписки", callback_data="sub:back_to_menu")])
    text = "🎓 Выберите название группы:"
    kb = InlineKeyboardMarkup(inline_keyboard=btns)
    if isinstance(m_or_c, CallbackQuery):
        await m_or_c.message.edit_text(text, reply_markup=kb)
    else:
        await m_or_c.answer(text, reply_markup=kb)

@dp.callback_query(F.data == "change_my_group")
async def cb_change_my_group(c: CallbackQuery):
    await show_group_name_menu(c)
    try: await c.answer()
    except: pass

@dp.callback_query(F.data.startswith("group_name:"))
async def cb_group_name(c: CallbackQuery):
    await c.message.delete()
    selected_name = c.data.split(":", maxsplit=1)[1].casefold()
    filtered_groups = [
        (index, group)
        for index, group in enumerate(GROUPS_DB)
        if get_group_name(group).casefold() == selected_name
    ]
    filtered_groups.sort(key=lambda item: (-int(re.search(r"\d+", item[1]).group()), item[1].casefold()))
            
    if not filtered_groups:
        await c.message.answer("😔 Группы не найдены.", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_group_names")]]))
    else:
        btns = [InlineKeyboardButton(text=n, callback_data=f"fsel:group:{n}") for i, n in filtered_groups]
        kb = InlineKeyboardMarkup(inline_keyboard=[[btn] for btn in btns] + [[InlineKeyboardButton(text="🔙 Назад", callback_data="back_to_group_names")]])
        await c.message.answer("👇 Выберите группу:", reply_markup=kb)
    try: await c.answer()
    except: pass

@dp.callback_query(F.data == "back_to_group_names")
async def cb_back_to_group_names(c: CallbackQuery):
    await show_group_name_menu(c)
    try: await c.answer()
    except: pass

@dp.callback_query(F.data.startswith("fsel:"))       
async def cb_sel(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    _, t_type, idx = c.data.split(":")
    if t_type != "group":
        await c.answer("Этот раздел больше недоступен.", show_alert=True)
        return
    # Old messages used list positions; resolve their visible label so catalog
    # changes never silently subscribe the user to a different group.
    if idx.isdecimal():
        markup = getattr(c.message, "reply_markup", None)
        idx = next((button.text for row in getattr(markup, "inline_keyboard", [])
                    for button in row if button.callback_data == c.data), "")
    t_val = canonical_group(idx)
    if not active_group(t_val) or t_val not in await get_groups_db():
        await c.answer("Эта группа больше недоступна. Выберите группу из нового списка.", show_alert=True)
        await show_group_name_menu(c.message)
        return

    await dao.hset("user_subs", str(c.from_user.id), t_val)
    await db_manager.register_or_update_user(c.from_user.id, c.from_user.username, t_val)
    await c.message.answer(f"✅ Ваша группа успешно сохранена: <b>{t_val}</b>\nТеперь вы будете получать важные уведомления от старосты.", parse_mode="HTML", reply_markup=get_submenu_keyboard())
    await show_subscription_time_menu(c.message, user_id=str(c.from_user.id))
    await c.answer()

async def display_day_schedule(message: Message | CallbackQuery, state: FSMContext, target_date: date):   
    data = await state.get_data()
    t_val, t_type = data.get("target_value"), data.get("target_type")
    chat_id = message.chat.id if isinstance(message, Message) else message.message.chat.id
    today = datetime.now(YEKATERINBURG_TZ).date()
    wo = ((target_date - timedelta(days=target_date.weekday())) - (today - timedelta(days=today.weekday()))).days // 7

    if wo not in (0, 1):
        if isinstance(message, CallbackQuery):
            await message.answer("Доступны текущая и следующая недели.", show_alert=True)
        return

    async with loading_animation(chat_id):
        week_s = await sm.fetch_schedule(wo, t_type, t_val)

    day_name = DAYS_OF_WEEK[target_date.weekday()]   
    day_lessons = week_s.get(day_name, []) # type: ignore
    is_error = not week_s or "_error" in week_s # type: ignore

    if is_error:
        text = "⚠️ <b>Ошибка загрузки.</b>\nУниверситетский сайт не ответил вовремя или произошла ошибка парсинга. Попробуйте еще раз."
    else:
        text = await fmt_day(target_date, day_lessons, t_val)

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
    await display_day_schedule(m, state, datetime.now(YEKATERINBURG_TZ).date() + timedelta(days=offset))

@dp.message(F.text.in_({"🗓 Эта неделя", "➡️ След. неделя"}), ScheduleStates.viewing)
async def handle_weeks(m: Message, state: FSMContext):
    data, wo = await state.get_data(), 1 if m.text == "➡️ След. неделя" else 0
    async with loading_animation(m.chat.id):
        s = await sm.fetch_schedule(wo, data.get("target_type"), data.get("target_value"))
    text = await fmt_week(s, data.get("target_value", "")) # type: ignore
    
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
    exclude_ids = list(exclude_ids or [])
    launcher = await dao.get(f"miniapp_launcher:{chat_id}")
    if launcher:
        exclude_ids.append(int(launcher))
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
    await state.clear()
    try: await c.message.delete()
    except: pass
    await c.message.answer("🔙 Главное меню", reply_markup=get_main_menu())
    try: await c.answer()
    except: pass

@dp.message(F.text.in_({"🔄 Сбросить", "🔙 Назад"}))
async def reset(m: Message, state: FSMContext):
    await state.clear()
    msg = await m.answer("🔙 Возвращаюсь...", reply_markup=get_main_menu())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])


async def run_morning_broadcast(target_time: str = None):
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
        
        today = datetime.now(YEKATERINBURG_TZ).date()
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
            text += await fmt_day(today, day_lessons, gid)
            
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


async def configure_mini_app_menu_button(chat_id: int | None = None):
    """Synchronize Telegram's persistent Mini App button with WEBAPP_URL."""
    if not WEBAPP_URL.startswith("https://"):
        logger.warning("Mini App menu button was not configured: WEBAPP_URL must use HTTPS")
        return
    try:
        if chat_id is not None:
            # A per-chat WebApp button keeps its old URL forever. Reset it so the
            # chat always inherits the current global Mini App button instead.
            await bot.set_chat_menu_button(
                chat_id=chat_id,
                menu_button=MenuButtonDefault(),
            )
            return

        web_app_url = f"{WEBAPP_URL.rstrip('/')}/webapp"
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(
                text="Mini App",
                web_app=WebAppInfo(url=web_app_url),
            )
        )
        logger.info("Telegram Mini App menu button configured for %s", web_app_url)
    except Exception as error:
        logger.error("Failed to configure Telegram Mini App menu button: %s", error)
async def admin_command_listener():
    logger.info("🤖 Admin command listener started.")
    while True:
        try:
            # Keep the blocking wait below redis-py's 5-second socket timeout.
            cmd_data = await dao.blpop("admin_bot_commands", timeout=4)
            if cmd_data:
                payload = json.loads(cmd_data[1])
                command = payload.get("command")
                admin_id = payload.get("admin_id")
                
                logger.info(f"Admin command received: {command} from {admin_id}")
                
                if command == "force_broadcast":
                    try:
                        await bot.send_message(admin_id, "🚀 <b>Запуск массовой рассылки...</b>", parse_mode="HTML")
                        count = await run_morning_broadcast()
                        await bot.send_message(admin_id, f"✅ <b>Рассылка завершена!</b>\nОтправлено сообщений: <b>{count}</b>", parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Force broadcast failed: {e}")
                        try: await bot.send_message(admin_id, f"❌ Ошибка рассылки: {e}")
                        except: pass
                        
                elif command == "delayed_broadcast":
                    try:
                        await bot.send_message(admin_id, "⏳ <b>Запуск отложенной рассылки (через 60 секунд)...</b>", parse_mode="HTML")
                        await asyncio.sleep(60)
                        count = await run_morning_broadcast()
                        await bot.send_message(admin_id, f"✅ <b>Отложенная рассылка завершена!</b>\nОтправлено сообщений: <b>{count}</b>", parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Delayed broadcast failed: {e}")
                        try: await bot.send_message(admin_id, f"❌ Ошибка рассылки: {e}")
                        except: pass
                        
                elif command == "test_schedule_broadcast":
                    try:
                        await bot.send_message(admin_id, "⏳ <b>Формирую тестовую рассылку для вас...</b>", parse_mode="HTML")
                        subs = await dao.hgetall("user_subs")
                        admin_gid = subs.get(str(admin_id))
                        if not admin_gid:
                            await bot.send_message(admin_id, "❌ Вы не подписаны на утреннюю рассылку. Перейдите в меню 'Моя подписка' в боте и подпишитесь.")
                        else:
                            today = datetime.now(YEKATERINBURG_TZ).date()
                            week_s = await sm.fetch_schedule(0, "group", admin_gid)
                            day_name = DAYS_OF_WEEK[today.weekday()]
                            day_lessons = week_s.get(day_name, [])
                            is_error = not week_s or "_error" in week_s
                            if is_error:
                                error_msg = week_s.get('_error', 'Ошибка') if week_s else 'Ошибка'
                                await bot.send_message(admin_id, f"❌ Ошибка получения расписания: {error_msg}")
                            else:
                                text = f"🧪 <b>ТЕСТ УТРЕННЕЙ РАССЫЛКИ</b>\n\n{get_greeting()} <b>Расписание на сегодня:</b>\n\n"
                                text += await fmt_day(today, day_lessons, admin_gid)
                                await bot.send_message(admin_id, text, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Test schedule broadcast failed: {e}")
                        try: await bot.send_message(admin_id, f"❌ Ошибка теста: {e}")
                        except: pass
                        
                elif command == "preload_cache":
                    try:
                        await bot.send_message(admin_id, "⏳ <b>Добавляю все расписания в очередь парсеров...</b>", parse_mode="HTML")
                        count = 0
                        for name in (await get_groups_db()).keys():
                            for wo in [0, 1]:
                                job = {
                                    "week_offset": wo,
                                    "target_type": "group",
                                    "target_value": name
                                }
                                await dao.rpush("schedule_jobs", json.dumps(job))
                                count += 1
                        await bot.send_message(admin_id, f"✅ <b>Отправлено в очередь: {count}</b>\nВоркеры в фоновом режиме загрузят расписания в кэш! (Около 2 минут)", parse_mode="HTML")
                        
                        async def notify_preload_done(target_admin_id: int):
                            await asyncio.sleep(3)
                            while True:
                                left = await dao.llen("schedule_jobs")
                                if left == 0: break
                                await asyncio.sleep(2)
                            try:
                                await bot.send_message(target_admin_id, "✅ <b>Фуух, готово!</b>\nАбсолютно все расписания кэшированы и готовы к молниеносной выдаче. ⚡", parse_mode="HTML")
                            except:
                                pass
                        
                        asyncio.create_task(notify_preload_done(admin_id))
                    except Exception as e:
                        logger.error(f"Preload cache command failed: {e}")
                        try: await bot.send_message(admin_id, f"❌ Ошибка прогрева кэша: {e}")
                        except: pass
        except Exception as e:
            logger.error(f"Error in admin_command_listener: {e}")
            await asyncio.sleep(2)

async def main():
    await db_manager.init_db()
    await migrate_group_preferences(dao, db_manager)
    await configure_mini_app_menu_button()
    if PROXY_URL: logger.info("🌐 Для Telegram включён прокси")
    dp.startup.register(notify_on_startup)
    dp.shutdown.register(notify_on_shutdown)
    asyncio.create_task(main_scheduler())
    asyncio.create_task(admin_command_listener())
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
    kb.inline_keyboard.insert(0, [InlineKeyboardButton(text="Открыть удобную панель ↗", web_app=WebAppInfo(url=f"{WEBAPP_URL.rstrip('/')}/webapp?tab=profile&panel=starosta"))])
    text = f"🎓 <b>Панель старосты</b>\n\n👤 <b>{name or 'Староста'}</b>\n🏫 Ваша группа: <b>{group or 'Не выбрана'}</b>\n\nОбъявления и афиша — в приложении.\nОпросы и домашние задания — кнопками ниже:"
    
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

@dp.callback_query(F.data.in_({"st_dash:name", "st_dash:pass", "st_dash:broadcast_all", "st_dash:broadcast", "st_dash:change_group", "st_dash:back"}))
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
        group_names = get_group_names()
        kb_rows = [
            [
                InlineKeyboardButton(text=group_name, callback_data=f"st_group_name:{group_name}")
                for group_name in group_names[index:index + 2]
            ]
            for index in range(0, len(group_names), 2)
        ]
        kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")])
        
        kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
        await c.message.edit_text("📚 Выберите название группы:", reply_markup=kb, parse_mode="HTML")
        await state.set_state(StarostStates.waiting_for_group_name)
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

@dp.callback_query(F.data.startswith("st_group_name:"), StarostStates.waiting_for_group_name)
async def starost_group_name(c: CallbackQuery, state: FSMContext):
    selected_name = c.data.split(":", maxsplit=1)[1].casefold()
    groups = [group for group in GROUPS_DB if get_group_name(group).casefold() == selected_name]
    groups.sort(key=lambda group: (-int(re.search(r"\d+", group).group()), group.casefold()))
    
    kb_rows = []
    for i in range(0, len(groups), 2):
        row = [InlineKeyboardButton(text=g, callback_data=f"st_group:{g}") for g in groups[i:i+2]]
        kb_rows.append(row)
    kb_rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="st_dash:back")])
    
    kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)
    await c.message.edit_text("🏫 Выберите вашу группу:", reply_markup=kb)
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
    if t_val:
        await dao.sadd(f"favs:{m.from_user.id}", f"group:{t_val}")
        await m.answer(f"⭐ <b>{t_val}</b> успешно добавлено в избранное!", parse_mode="HTML")
    else:
        await m.answer("❌ Не удалось определить активное расписание для сохранения.")

@dp.message(F.text == "⭐ Избранное")
async def show_favorites(m: Message):
    msg = await m.answer("⭐ Открываю избранное...", reply_markup=get_submenu_keyboard())
    await clear_chat_history(m.chat.id, exclude_ids=[msg.message_id])
    favs = [favorite for favorite in await dao.smembers(f"favs:{m.from_user.id}") if favorite.startswith("group:")]
    if not favs:
        await m.answer("⭐ <b>Избранное</b>\n\nУ вас пока нет сохраненных расписаний.\nЧтобы добавить расписание в избранное, откройте его и нажмите кнопку <b>«⭐ В избранное»</b>.", parse_mode="HTML")
        return
    btns = []
    for f in favs:
        _, t_val = f.split(":", 1)
        btns.append([InlineKeyboardButton(text=f"🎓 {t_val}", callback_data=f"fav_select:group:{t_val}")])
    btns.append([InlineKeyboardButton(text="⚙️ Управление избранным", callback_data="fav_manage")])
    await m.answer("⭐ <b>Ваши избранные расписания:</b>", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns), parse_mode="HTML")

@dp.callback_query(F.data.startswith("fav_select:group:"))
async def cb_fav_select(c: CallbackQuery, state: FSMContext):
    await c.message.delete()
    _, _, t_val = c.data.split(":", 2)
    await state.set_state(ScheduleStates.viewing)
    await state.update_data(target_type="group", target_value=t_val)
    await c.message.answer(f"✅ Фильтр из избранного: <b>{t_val}</b>", parse_mode="HTML", reply_markup=get_main_menu(t_val))
    await display_day_schedule(c.message, state, datetime.now(YEKATERINBURG_TZ).date())
    await c.answer()

@dp.callback_query(F.data == "fav_manage")
async def cb_fav_manage(c: CallbackQuery):
    favs = [favorite for favorite in await dao.smembers(f"favs:{c.from_user.id}") if favorite.startswith("group:")]
    if not favs:
        await c.message.edit_text("Список избранного пуст.")
        return
    btns = []
    for f in favs:
        _, t_val = f.split(":", 1)
        btns.append([InlineKeyboardButton(text=f"❌ 🎓 {t_val}", callback_data=f"fav_del:group:{t_val}")])
    btns.append([InlineKeyboardButton(text="🔙 Назад", callback_data="fav_back_to_list")])
    await c.message.edit_text("Выберите элемент для удаления:", reply_markup=InlineKeyboardMarkup(inline_keyboard=btns))
    await c.answer()

@dp.callback_query(F.data == "fav_back_to_list")
async def cb_fav_back_to_list(c: CallbackQuery):
    await c.message.delete()
    await show_favorites(c.message)
    await c.answer()

@dp.callback_query(F.data.startswith("fav_del:group:"))
async def cb_fav_del(c: CallbackQuery):
    _, t_type, t_val = c.data.split(":", 2)
    await dao.srem(f"favs:{c.from_user.id}", f"{t_type}:{t_val}")
    await c.answer("Удалено из избранного")
    await cb_fav_manage(c)

# Handle starosta HW clicks
@dp.callback_query(F.data.in_({"st_dash:add_hw", "st_dash:del_hw"}))
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
MODEL_PAGE_SIZE = 8


async def ensure_current_chat_model(uid: int, user_row: dict | None) -> str:
    """Replace deleted/legacy models with an available free chat model."""
    models = await get_chat_models()
    available_ids = {model["id"] for model in models}
    model = normalize_model_id(user_row["ai_model"] if user_row else None)
    if model not in available_ids:
        model = models[0]["id"]
    if user_row and model != user_row.get("ai_model"):
        await db_manager.set_user_ai_model(uid, model)
    return model


async def clear_ai_ui_messages(chat_id: int, exclude_ids: list[int] | None = None):
    """Remove the bot's previous AI panels before displaying a new one."""
    exclude_ids = exclude_ids or []
    ids = [int(message_id) for message_id in await dao.smembers(f"ai_ui_messages:{chat_id}") if int(message_id) not in exclude_ids]
    for message_id in ids:
        try:
            await bot.delete_message(chat_id, message_id)
        except Exception:
            # Telegram cannot delete some historical messages (for example,
            # after its deletion window); do not prevent opening the new UI.
            pass
    await dao.delete(f"ai_ui_messages:{chat_id}")
    for message_id in exclude_ids:
        await remember_ai_ui_message(chat_id, message_id)


async def remember_ai_ui_message(chat_id: int, message_id: int):
    key = f"ai_ui_messages:{chat_id}"
    await dao.sadd(key, message_id)
    await dao.expire(key, MSG_STORE_LIMIT)

async def get_active_user_row(uid: int):
    user_row = await db_manager.get_user(uid)
    if not user_row:
        return None
        
        
    return user_row

@dp.message(F.text == "🤖 ИИ-Ассистент")
@dp.message(Command("ai"))
async def ai_menu(m: Message, state: FSMContext):
    await state.clear()
    await configure_mini_app_menu_button(m.chat.id)
    await clear_ai_ui_messages(m.chat.id)
    navigation_message = await m.answer(
        "🤖 Панель ИИ открыта. Для выхода используйте «🔙 Назад» в нижней панели.",
        reply_markup=get_submenu_keyboard(),
    )
    await clear_chat_history(m.chat.id, exclude_ids=[navigation_message.message_id])
    uid = m.from_user.id
    user_row = await get_active_user_row(uid)
    
    if not user_row:
        await db_manager.register_or_update_user(uid, m.from_user.username)
        user_row = await get_active_user_row(uid)
        
    model = await ensure_current_chat_model(int(uid), user_row)


    text = (
        "🤖 <b>Панель ИИ-Ассистента</b>\n\n"
        f"🧠 Выбранная модель: <code>{model}</code>\n"
        "Бесплатные и недорогие модели · текст и фото"
    )
    
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Чат в Mini App", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?tab=chat"))],
        [InlineKeyboardButton(text="💬 Начать диалог", callback_data="ai:chat")],
        [InlineKeyboardButton(text="⚙️ Выбрать модель", callback_data="ai:select_model")],
        [InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="ai:clear_context")]
    ])
    panel_message = await m.answer(text, reply_markup=kb, parse_mode="HTML")
    await remember_ai_ui_message(m.chat.id, panel_message.message_id)

@dp.callback_query(F.data == "ai:chat")
async def cb_ai_chat(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    user_row = await get_active_user_row(uid)
    has_key = bool(user_row['custom_ai_key']) if user_row else False
    model = await ensure_current_chat_model(uid, user_row)


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


def format_ai_response_to_html(text: str) -> str:
    import html
    escaped = html.escape(text)
    
    def replace_code_block(match):
        code = match.group(2)
        return f"<pre><code>{code.strip()}</code></pre>"
        
    pattern_block = re.compile(r'```([a-zA-Z0-9+#-]+)?(?:\s*\n)?(.*?)\n?```', re.DOTALL)
    processed = pattern_block.sub(replace_code_block, escaped)
    
    pattern_inline = re.compile(r'`([^`\n]+)`')
    processed = pattern_inline.sub(r'<code>\1</code>', processed)
    
    processed = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', processed)
    processed = re.sub(r'__([^_]+)__', r'<b>\1</b>', processed)
    processed = re.sub(r'(?<!\*)\*([^*]+)\*(?!\*)', r'<i>\1</i>', processed)
    processed = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<a href="\2">\1</a>', processed)
    
    return processed

@dp.message(UserStates.waiting_for_ai_prompt)
async def ai_chat_message(m: Message, state: FSMContext):
    if m.text == "❌ Выйти из чата ИИ":
        await state.clear()
        await m.answer("👋 Диалог завершен.", reply_markup=get_main_menu())
        return
        
    data = await state.get_data()
    api_key = data.get("ai_key")
    model_name = data.get("ai_model", "openrouter/free")
    uid = m.from_user.id
    
    user_row = await get_active_user_row(uid)
    has_custom_key = bool(api_key)
    model_name = await ensure_current_chat_model(uid, user_row)
    await state.update_data(ai_model=model_name)
    
    prompt = ""
    image_data_b64 = None
    
    if m.text:
        prompt = m.text
    elif m.photo:
        metadata = next((model for model in await get_chat_models() if model["id"] == model_name), None)
        if not metadata or not metadata["supports_images"]:
            await m.answer("Для фото выберите модель в разделе «📷 Фото».", reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="📷 Выбрать модель для фото", callback_data="ai_model_filter:photo")]
            ]))
            return
        if (m.photo[-1].file_size or 0) > 5 * 1024 * 1024:
            await m.answer("Фото слишком большое. Максимум — 5 МБ.")
            return
        photo_bytes = io.BytesIO()
        await bot.download(m.photo[-1], destination=photo_bytes)
        try:
            image_data_b64 = await asyncio.to_thread(normalize_chat_image, base64.b64encode(photo_bytes.getvalue()).decode("ascii"))
        except ValueError as error:
            await m.answer(str(error))
            return
        prompt = m.caption or "Разбери изображение и помоги с заданием."
    else:
        await m.answer("⚠️ <b>Бот принимает только текстовые сообщения или фотографии.</b>", parse_mode="HTML")
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
                prompt=prompt,
                api_key=api_key,
                model_name=model_name,
                history=history,
                image_data_b64=image_data_b64
            )
            
            await db_manager.log_ai_request(
                telegram_id=uid,
                prompt=prompt if not image_data_b64 else f"[Изображение] {prompt}",
                response=response_text,
                model_used=model_name
            )
            
            clean_response = response_text
            formatted_response = format_ai_response_to_html(clean_response)
            
            
            if image_data_b64:
                history_content = [
                    {"type": "text", "text": prompt if prompt else "Что на изображении?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data_b64}"
                        }
                    }
                ]
            else:
                history_content = prompt
                
            history.append({"role": "user", "content": history_content})
            history.append({"role": "assistant", "content": clean_response})
            history = history[-10:]
            
            await dao.setex(history_key, 3600, json.dumps(history, ensure_ascii=False))
            
                
            if len(formatted_response) > 4096:
                for chunk in [formatted_response[i:i+4000] for i in range(0, len(formatted_response), 4000)]:
                    await m.answer(chunk)
            else:
                await m.answer(formatted_response, parse_mode="HTML")
                
        except Exception as e:
                
            logger.error(f"AI response failed: {e}")
            err_msg = str(e).lower()
            is_rate_limit = "rate" in err_msg or "429" in err_msg or type(e).__name__ == "RateLimitError"
            is_vision_unsupported = any(x in err_msg for x in ["vision", "multimodal", "image input", "format"]) or ("400" in err_msg and image_data_b64 is not None)
            
            if is_rate_limit:
                await m.answer(
                    "⏳ <b>Превышен лимит запросов (Rate Limit) от провайдера OpenRouter.</b>\n\n"
                    "Пожалуйста, подождите несколько минут или смените модель ИИ в настройках.",
                    parse_mode="HTML"
                )
            elif is_vision_unsupported:
                await m.answer(
                    "❌ <b>Выбранная модель ИИ не поддерживает анализ изображений (Vision).</b>\n\n"
                    "Выберите модель в разделе <b>«📷 Фото»</b> в настройках ИИ.",
                    parse_mode="HTML"
                )
            elif has_custom_key and any(x in err_msg for x in ["401", "unauthorized", "invalid key", "invalid credential", "user not found"]):
                await db_manager.set_user_ai_key(uid, None)
                await state.clear()
                await m.answer(
                    "⚠️ <b>Персональный ключ OpenRouter недействителен.</b>\n\n"
                    "Ключ сброшен. Откройте ИИ заново, чтобы использовать общий ключ бота.",
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
        "Ключ <b>OpenRouter</b> начинается с <code>sk-or-...</code>.\n\n"
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
    await show_openrouter_models(c)


async def show_openrouter_models(c: CallbackQuery, page: int = 0, query: str = "", category: str = "all"):
    models = await get_chat_models()
    models = filter_chat_models(models, category)
    await dao.setex(f"ai_model_filter:{c.from_user.id}", 3600, category)
    query = query.strip().casefold()
    if query:
        models = [model for model in models if query in model["name"].casefold() or query in model["id"].casefold()]

    await dao.setex(f"ai_model_options:{c.from_user.id}", 3600, json.dumps(models, ensure_ascii=False))
    await dao.setex(f"ai_model_query:{c.from_user.id}", 3600, query)
    max_page = max(0, (len(models) - 1) // MODEL_PAGE_SIZE)
    page = min(max(page, 0), max_page)
    chunk = models[page * MODEL_PAGE_SIZE:(page + 1) * MODEL_PAGE_SIZE]

    rows = [[InlineKeyboardButton(text=("✓ " if category == key else "") + label, callback_data=f"ai_model_filter:{key}")
             for key, label in (("all", "Все"), ("text", "Текст"), ("photo", "📷 Фото"), ("free", "Free"), ("cheap", "Недорогие"))]]
    for offset, model in enumerate(chunk):
        index = page * MODEL_PAGE_SIZE + offset
        label = f"{'📷' if model['supports_images'] else '📝'} {model['name']} · {'Free' if model['is_free'] else '$'}"
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"ai_model_pick:{index}")])

    navigation = [InlineKeyboardButton(text="🔎 Поиск", callback_data="ai:model_search")]
    if page > 0:
        navigation.append(InlineKeyboardButton(text="◀", callback_data=f"ai_models_page:{page - 1}"))
    if page < max_page:
        navigation.append(InlineKeyboardButton(text="▶", callback_data=f"ai_models_page:{page + 1}"))
    rows.extend([navigation, [InlineKeyboardButton(text="🔙 Назад", callback_data="ai:back_to_menu")]])

    title = "🔎 Результаты поиска" if query else "⚙️ Модели OpenRouter"
    subtitle = f"Показано {page * MODEL_PAGE_SIZE + 1}–{page * MODEL_PAGE_SIZE + len(chunk)} из {len(models)}"
    if not chunk:
        subtitle = "По вашему запросу моделей не найдено."
    await c.message.edit_text(
        f"<b>{title}</b>\n\n{subtitle}\n\n📷 — текст и фотографии. 📝 — только текст.\nБесплатные и недорогие модели. Каталог обновляется из OpenRouter. Возможны лимиты провайдера.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        parse_mode="HTML",
    )
    await remember_ai_ui_message(c.message.chat.id, c.message.message_id)
    await c.answer()


@dp.callback_query(F.data.startswith("ai_model_filter:"))
async def cb_ai_model_filter(c: CallbackQuery):
    category = c.data.rsplit(":", 1)[1]
    if category not in {"all", "text", "photo", "free", "cheap"}:
        await c.answer()
        return
    await show_openrouter_models(c, category=category)


@dp.callback_query(F.data.startswith("ai_models_page:"))
async def cb_ai_models_page(c: CallbackQuery):
    try:
        page = int(c.data.rsplit(":", 1)[1])
        query = await dao.get(f"ai_model_query:{c.from_user.id}") or ""
    except ValueError:
        await c.answer("Некорректная страница", show_alert=True)
        return
    category = await dao.get(f"ai_model_filter:{c.from_user.id}") or "all"
    await show_openrouter_models(c, page, query, category)


@dp.callback_query(F.data == "ai:model_search")
async def cb_ai_model_search(c: CallbackQuery, state: FSMContext):
    await state.set_state(UserStates.waiting_for_model_search)
    await c.message.edit_text(
        "🔎 <b>Поиск модели OpenRouter</b>\n\nВведите название, разработчика или часть ID модели.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔙 Назад", callback_data="ai:select_model")]]),
        parse_mode="HTML",
    )
    await c.answer()


@dp.message(UserStates.waiting_for_model_search)
async def ai_model_search_input(m: Message, state: FSMContext):
    query = (m.text or "").strip()
    if not query:
        await m.answer("Введите название или ID модели.")
        return
    await state.clear()
    models = await get_chat_models()
    matches = [model for model in models if query.casefold() in model["name"].casefold() or query.casefold() in model["id"].casefold()]
    category = await dao.get(f"ai_model_filter:{m.from_user.id}") or "all"
    matches = filter_chat_models(matches, category)
    await dao.setex(f"ai_model_options:{m.from_user.id}", 3600, json.dumps(matches, ensure_ascii=False))
    await dao.setex(f"ai_model_query:{m.from_user.id}", 3600, query.casefold())
    chunk = matches[:MODEL_PAGE_SIZE]
    rows = [[InlineKeyboardButton(text=f"{model['name']} · {model['id']}"[:60], callback_data=f"ai_model_pick:{index}")] for index, model in enumerate(chunk)]
    navigation = [InlineKeyboardButton(text="🔎 Новый поиск", callback_data="ai:model_search")]
    if len(matches) > MODEL_PAGE_SIZE:
        navigation.append(InlineKeyboardButton(text="▶", callback_data="ai_models_page:1"))
    rows.append(navigation)
    rows.append([InlineKeyboardButton(text="🔙 Назад", callback_data="ai:select_model")])
    text = f"🔎 <b>Результаты: {len(matches)}</b>\n\nВыберите модель:" if matches else "🔎 <b>Ничего не найдено.</b>\nПопробуйте другой запрос."
    result_message = await m.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=rows), parse_mode="HTML")
    await remember_ai_ui_message(m.chat.id, result_message.message_id)


@dp.callback_query(F.data.startswith("ai_model_pick:"))
async def cb_ai_set_model_save(c: CallbackQuery):
    try:
        index = int(c.data.rsplit(":", 1)[1])
        options_raw = await dao.get(f"ai_model_options:{c.from_user.id}")
        options = json.loads(options_raw) if options_raw else []
        model = options[index]["id"]
    except (ValueError, IndexError, KeyError, TypeError, json.JSONDecodeError):
        await c.answer("Список моделей устарел. Откройте его ещё раз.", show_alert=True)
        return

    if not any(item["id"] == model for item in await get_chat_models()):
        await c.answer("Модель больше недоступна. Откройте список заново.", show_alert=True)
        return
    await db_manager.set_user_ai_model(c.from_user.id, model)
    await dao.delete(f"ai_history:{c.from_user.id}")
    await clear_ai_ui_messages(c.message.chat.id, exclude_ids=[c.message.message_id])
    await c.answer("Модель изменена. Контекст очищен.")
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
    # Старые сообщения с inline-кнопкой не удаляем: при удалении Telegram
    # кратко показывает системную кнопку Start до появления нижней клавиатуры.
    try:
        await c.message.edit_text("🔙 Главное меню")
    except Exception:
        pass
    await c.message.answer("🔙 Главное меню", reply_markup=get_main_menu())
    await c.answer()

async def show_ai_menu_directly(message: Message | CallbackQuery, user_id: int = None):
    uid = user_id or message.from_user.id
    user_row = await get_active_user_row(int(uid))
    if not user_row:
        username = message.from_user.username if hasattr(message, 'from_user') else None
        await db_manager.register_or_update_user(int(uid), username)
        user_row = await get_active_user_row(int(uid))
        
    model = await ensure_current_chat_model(int(uid), user_row)


    text = (
        "🤖 <b>Панель ИИ-Ассистента</b>\n\n"
        f"🧠 Выбранная модель: <code>{model}</code>\n"
        "Бесплатные и недорогие модели · текст и фото"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Чат в Mini App", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?tab=chat"))],
        [InlineKeyboardButton(text="💬 Начать диалог", callback_data="ai:chat")],
        [InlineKeyboardButton(text="⚙️ Выбрать модель", callback_data="ai:select_model")],
        [InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="ai:clear_context")]
    ])
    if isinstance(message, CallbackQuery):
        await message.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
        await remember_ai_ui_message(message.message.chat.id, message.message.message_id)
    else:
        panel_message = await message.answer(text, reply_markup=kb, parse_mode="HTML")
        await remember_ai_ui_message(panel_message.chat.id, panel_message.message_id)


@dp.callback_query(F.data == "ai:back_to_menu")
async def cb_ai_back_to_menu(c: CallbackQuery):
    uid = c.from_user.id
    user_row = await get_active_user_row(uid)
    model = await ensure_current_chat_model(uid, user_row)
    
    text = (
        "🤖 <b>Панель ИИ-Ассистента</b>\n\n"
        f"🧠 Выбранная модель: <code>{model}</code>\n"
        "Бесплатные и недорогие модели · текст и фото"
    )
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Чат в Mini App", web_app=WebAppInfo(url=f"{WEBAPP_URL}/webapp?tab=chat"))],
        [InlineKeyboardButton(text="💬 Начать диалог", callback_data="ai:chat")],
        [InlineKeyboardButton(text="⚙️ Выбрать модель", callback_data="ai:select_model")],
        [InlineKeyboardButton(text="🧹 Очистить контекст", callback_data="ai:clear_context")]
    ])
    await c.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await c.answer()


# ═══════════════════ ПЛАТЕЖНЫЕ ХЭНДЛЕРЫ И АКТИВАЦИЯ КЛЮЧЕЙ ═══════════════════


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
    ev_id = int(c.data.split(":")[-1])
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
    ch_id = int(c.data.split(":")[-1])
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
