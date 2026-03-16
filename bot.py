import os
import re
import json
import logging
import asyncio
import urllib.parse
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.types import (
    Message, CallbackQuery, InlineKeyboardButton,
    InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove,
)
from aiogram.filters import CommandStart, Command
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.context import FSMContext

# ═══════════════════ НАСТРОЙКИ ═══════════════════
BOT_TOKEN = os.getenv("BOT_TOKEN", "8458170697:AAF248VvYpM8GKapPzh--CKFLcq6_Enr8xw")
PROXY_URL = os.getenv("PROXY_URL") # Формат: http://ip:port

SCHEDULE_URL = "https://up.corp.tu-ugmk.com/student/schedule"

COOKIES = {} 

LOGIN = os.getenv("LOGIN", "uvybhjhhv@gmail.com")
PASSWORD = os.getenv("PASSWORD", "qazwsxedcip60000OP")

CACHE_DIR = "cache"
CACHE_LIFETIME = 86400 
CACHE_VERSION = 32 
# ═════════════════════════════════════════════════

# ════════════ БАЗЫ ДАННЫХ ID ═════════════════════
USERS_FILE = "users.json"
MAINTENANCE_FILE = "maintenance.json"
ADMIN_IDS = [474095004] # Можно дополнить своим ID

def is_maintenance():
    if not os.path.exists(MAINTENANCE_FILE):
        return False
    try:
        with open(MAINTENANCE_FILE, "r") as f:
            return json.load(f).get("is_active", False)
    except Exception:
        return False

def set_maintenance(state: bool):
    with open(MAINTENANCE_FILE, "w") as f:
        json.dump({"is_active": state}, f)

def get_users():
    if not os.path.exists(USERS_FILE):
        return set()
    try:
        with open(USERS_FILE, "r") as f:
            return set(json.load(f))
    except Exception:
        return set()

def save_user(user_id):
    users = get_users()
    if user_id not in users:
        users.add(user_id)
        with open(USERS_FILE, "w") as f:
            json.dump(list(users), f)

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
# ═════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Настройка прокси для aiohttp
session_kwargs = {"timeout": 120.0}
if PROXY_URL:
    session_kwargs["proxy"] = PROXY_URL
    logger.info(f"🌐 Используется прокси для aiohttp: {PROXY_URL}")

session = AiohttpSession(**session_kwargs)
bot = Bot(token=BOT_TOKEN, session=session)
dp = Dispatcher(storage=MemoryStorage())
router = Router()
dp.include_router(router)

import asyncio
import collections
from typing import Callable, Dict, Any, Awaitable
from aiogram import BaseMiddleware
from aiogram.types import Message

class LatestMessageOnlyMiddleware(BaseMiddleware):
    def __init__(self, debounce_delay: float = 0.2): # Tunable delay
        super().__init__()
        self.latest_message_ids: Dict[int, int] = collections.defaultdict(int)
        self.debounce_delay = debounce_delay

    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Message,
        data: Dict[str, Any]
    ) -> Any:
        if not isinstance(event, Message):
            # This middleware is designed for Message events. Pass other types through.
            return await handler(event, data)

        chat_id = event.chat.id
        current_message_id = event.message_id

        # Update the latest message ID seen for this chat
        self.latest_message_ids[chat_id] = max(
            self.latest_message_ids[chat_id],
            current_message_id
        )

        # Wait for a short period. During this wait, if a newer message arrives
        # for this chat, it will update self.latest_message_ids[chat_id]
        await asyncio.sleep(self.debounce_delay)

        # Check if the message we are about to process is *still* the latest.
        # If not, it means a newer message came in during our wait, so we skip this one.
        if self.latest_message_ids[chat_id] == current_message_id:
            logger.debug(f"Processing message {current_message_id} in chat {chat_id}.")
            return await handler(event, data)
        else:
            logger.info(f"Skipping old message {current_message_id} in chat {chat_id}. "
                        f"A newer message ({self.latest_message_ids[chat_id]}) arrived.")
            return None # Do not call the handler

# Register the middleware
dp.update.middleware(LatestMessageOnlyMiddleware())

class UserRegistrationMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any]
    ) -> Any:
        if hasattr(event, "from_user") and event.from_user:
            save_user(event.from_user.id)
        return await handler(event, data)

class MaintenanceMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[Message, Dict[str, Any]], Awaitable[Any]],
        event: Any,
        data: Dict[str, Any]
    ) -> Any:
        # Если включен режим техработ и пишет НЕ админ
        user_id = None
        if hasattr(event, "from_user") and event.from_user:
            user_id = event.from_user.id

        if is_maintenance() and user_id not in ADMIN_IDS:
            if isinstance(event, Message):
                await event.answer("🛠 <b>Бот находится на технических работах.</b>\nПожалуйста, попробуйте позже.", parse_mode="HTML")
            return None # Блокируем дальнейшую обработку
            
        return await handler(event, data)

dp.message.middleware(UserRegistrationMiddleware())
dp.callback_query.middleware(UserRegistrationMiddleware())
dp.message.middleware(MaintenanceMiddleware())
dp.callback_query.middleware(MaintenanceMiddleware())

DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]
SHORT_DAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
DAY_EMOJI = {"Понедельник": "1️⃣", "Вторник": "2️⃣", "Среда": "3️⃣", "Четверг": "4️⃣", "Пятница": "5️⃣", "Суббота": "6️⃣", "Воскресенье": "7️⃣"}
LESSON_TYPES = ["Лекции", "Практические", "Лабораторные", "Семинар", "Экзамен", "Зачет", "Зачёт", "Консультация", "Курсовая работа", "Курсовой проект"]



import redis.asyncio as redis
from typing import Any, Callable, Dict, Awaitable

# ═══════════════ REDIS DAO ═══════════════════════
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
            logger.warning("⚠️ Redis DAO: Ошибка подключения, используем файлы.")

    async def set(self, key: str, value: Any, ttl: int = CACHE_LIFETIME):
        if not self.is_connected: return
        try:
            val = json.dumps(value, ensure_ascii=False)
            await self.client.setex(key, ttl, val)
        except Exception as e:
            logger.error(f"DAO Set Error: {e}")

    async def get(self, key: str):
        if not self.is_connected: return None
        try:
            data = await self.client.get(key)
            return json.loads(data) if data else None
        except Exception as e:
            logger.error(f"DAO Get Error: {e}")
            return None

    async def delete(self, key: str):
        if not self.is_connected: return
        try:
            await self.client.delete(key)
        except Exception as e:
            logger.error(f"DAO Delete Error: {e}")

    async def delete_many(self, pattern: str):
        if not self.is_connected: return
        try:
            keys = await self.client.keys(pattern)
            if keys:
                await self.client.delete(*keys)
        except Exception as e:
            logger.error(f"DAO DeleteMany Error: {e}")

    async def sadd(self, name: str, value: str):
        if not self.is_connected: return
        try:
            await self.client.sadd(name, value)
            await self.client.expire(name, CACHE_LIFETIME)
        except Exception as e:
            logger.error(f"DAO SAdd Error: {e}")

    async def smembers(self, name: str):
        if not self.is_connected: return []
        try:
            return await self.client.smembers(name)
        except Exception as e:
            logger.error(f"DAO SMembers Error: {e}")
            return []

dao = RedisDAO()

# ═══════════════ ПАРСЕР ══════════════════════════
class ScheduleParser:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self._initialized = False

    async def init(self):
        if self._initialized: return
        await dao.connect()
        self.playwright = await async_playwright().start()
        
        launch_kwargs = {"headless": True}
        if PROXY_URL:
            launch_kwargs["proxy"] = {"server": PROXY_URL}
            logger.info(f"🌐 Используется прокси для Playwright: {PROXY_URL}")
            
        self.browser = await self.playwright.chromium.launch(**launch_kwargs)
        self.context = await self.browser.new_context(user_agent="Mozilla/5.0")
        if COOKIES:
            cookie_list = [{"name": n, "value": v, "domain": "up.corp.tu-ugmk.com", "path": "/"} for n, v in COOKIES.items()]
            await self.context.add_cookies(cookie_list)
        self._initialized = True

    async def _login_page(self, page):
        try:
            await page.fill('input[name="LoginForm[login]"], #openid-auth-user', LOGIN)
            await page.fill('input[name="LoginForm[password]"], #openid-auth-pwd', PASSWORD)
            await page.click('#login-submit, button[type="submit"]')
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception: pass

    async def _build_schedule_url(self, week_offset=0, target_type=None, target_value=None):
        start_date, end_date = self._get_week_dates(week_offset)
        
        db_map = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}
        type_map = {"group": "AcademicGroup", "teacher": "Teacher", "classroom": "Classroom"}

        if not target_type or not target_value:
            return f"{SCHEDULE_URL}?scheduleType=Week&startDate={start_date}&endDate={end_date}"

        obj_type = type_map.get(target_type, "AcademicGroup")
        obj_id = db_map[target_type][target_value]
        
        qs = f"scheduleType=Week&objectType={obj_type}&objectId={obj_id}&startDate={start_date}&endDate={end_date}"
        if target_type == "group":
            encoded_name = urllib.parse.quote(target_value)
            qs += f"&another_group={encoded_name}"
            
        url = f"{SCHEDULE_URL}?{qs}"
        logger.info(f"Requesting schedule URL: {url}")
        return url

    async def fetch_schedule(self, week_offset=0, target_type=None, target_value=None):
        # Детерминированные ключи
        target_id = f"{target_type}:{target_value}" if target_type else "default"
        data_key = f"data:v{CACHE_VERSION}:{target_id}:w{week_offset}"
        index_key = f"index:v{CACHE_VERSION}:{target_id}"

        cached = await self._load_cache(data_key)
        if cached is not None:
            return cached

        try:
            if not self._initialized:
                await self.init()

            url = await self._build_schedule_url(week_offset, target_type, target_value)
            page = await self.context.new_page()
            try:
                await page.goto(url, wait_until="networkidle", timeout=45000)
                if "login" in page.url.lower():
                    await self._login_page(page)
                    await page.goto(url, wait_until="networkidle", timeout=45000)

                try: await page.wait_for_selector("table", timeout=5000)
                except: pass

                html = await page.content()
                schedule = self._parse_html(html, target_type, target_value)

                if schedule and "login" not in page.url.lower():
                    await self._save_cache(schedule, data_key, index_key)

                return schedule
            finally:
                await page.close()
        except Exception as e:
            logger.error(f"Fetch error: {e}")
            self._initialized = False 
            return {}

    def _get_week_dates(self, offset):
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        target_mon = monday + timedelta(weeks=offset)
        return target_mon.strftime("%d.%m.%Y"), (target_mon + timedelta(days=6)).strftime("%d.%m.%Y")

    def _parse_html(self, html, target_type=None, target_value=None):
        soup = BeautifulSoup(html, "lxml")
        schedule, day_dates = {}, {}
        
        for text in soup.stripped_strings:
            for day in DAYS_OF_WEEK:
                m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
                if text.startswith(day) and m:
                    day_dates[day] = m.group(1)

        all_tables = soup.find_all("table")
        target_tables = []
        if target_type == "group" and target_value:
            for table in all_tables:
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) >= 4 and target_value.lower() in cells[3].get_text().lower():
                        target_tables.append(table)
                        break
        
        if not target_tables:
            if len(all_tables) >= 14 and target_type:
                target_tables = all_tables[-7:]
            else:
                target_tables = all_tables[:7]

        for i, table in enumerate(target_tables):
            day = DAYS_OF_WEEK[i] if i < len(DAYS_OF_WEEK) else f"Extra_{i}"
            schedule[day] = []
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 5: continue
                full_disc = cells[1].get_text(strip=True)
                if not full_disc: continue
                
                subject, lesson_type = self._split(list(cells[1].stripped_strings), full_disc)
                if subject:
                    schedule[day].append({
                        "time": cells[0].get_text(strip=True), "subject": subject,
                        "type": lesson_type, "room": cells[2].get_text(strip=True),
                        "group": cells[3].get_text(strip=True), "teacher": cells[4].get_text(strip=True),
                    })
        schedule["_dates"] = day_dates
        return schedule

    def _split(self, parts, full):
        if len(parts) >= 2:
            last = parts[-1].strip()
            for t in LESSON_TYPES:
                if last.lower() == t.lower(): return " ".join(parts[:-1]).strip(), last
        for t in LESSON_TYPES:
            if full.lower().endswith(t.lower()):
                s = full[:-len(t.lower())].strip()
                if s: return s, t
            idx = full.lower().rfind(t.lower())
            if idx > 0:
                s = full[:idx].strip()
                if s: return s, full[idx:idx + len(t)]
        return full, ""

    async def _load_cache(self, data_key):
        cached = await dao.get(data_key)
        if cached: return cached

        path = os.path.join(CACHE_DIR, data_key.replace(":", "_") + ".json")
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f: data = json.load(f)
                if (datetime.now() - datetime.fromisoformat(data["timestamp"])).total_seconds() >= CACHE_LIFETIME:
                    os.remove(path)
                    return None
                return data["schedule"]
            except Exception: return None
        return None

    async def _save_cache(self, schedule, data_key, index_key):
        await dao.set(data_key, schedule)
        await dao.sadd(index_key, data_key)

        os.makedirs(CACHE_DIR, exist_ok=True)
        path = os.path.join(CACHE_DIR, data_key.replace(":", "_") + ".json")
        data = {"timestamp": datetime.now().isoformat(), "schedule": schedule}
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    async def clear_cache(self, target_type=None, target_value=None):
        if target_type and target_value:
            target_id = f"{target_type}:{target_value}"
            index_key = f"index:v{CACHE_VERSION}:{target_id}"
            keys_to_del = await dao.smembers(index_key)
            for k in keys_to_del:
                await dao.delete(k)
            await dao.delete(index_key)
        else:
            await dao.delete_many(f"data:v{CACHE_VERSION}:*")
            await dao.delete_many(f"index:v{CACHE_VERSION}:*")

        if os.path.exists(CACHE_DIR):
            for f in os.listdir(CACHE_DIR):
                if f.endswith(".json"): os.remove(os.path.join(CACHE_DIR, f))

parser = ScheduleParser()

# ═══════════════ ОФОРМЛЕНИЕ И КНОПКИ ═════════════

def find_today_index(schedule):
    today_str = datetime.now().strftime("%d.%m.%Y")
    dates = schedule.get("_dates", {})
    for i, day in enumerate(DAYS_OF_WEEK):
        if dates.get(day) == today_str: return i
    return min(datetime.now().weekday(), 5)

def get_main_menu(target_value=None):
    if not target_value:
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="👥 Группы"), KeyboardButton(text="👩‍🏫 Преподаватели")],
            [KeyboardButton(text="🏫 Аудитории")],
            [KeyboardButton(text="🧹 Очистить"), KeyboardButton(text="🙈 Скрыть")],
        ], resize_keyboard=True, is_persistent=True)
    else:
        return ReplyKeyboardMarkup(keyboard=[
            [KeyboardButton(text="📅 Сегодня"), KeyboardButton(text="📆 Завтра")],
            [KeyboardButton(text="🗓 Эта неделя"), KeyboardButton(text="➡️ След. неделя")],
            [KeyboardButton(text="📋 Выбрать день"), KeyboardButton(text="📆 Выбрать неделю")],
            [KeyboardButton(text="🔄 Сбросить"), KeyboardButton(text="🧹 Очистить"), KeyboardButton(text="🙈 Скрыть")],
        ], resize_keyboard=True, is_persistent=True)

# Функция отправки статуса БЕЗ последующего редактирования
async def send_loading_status(message: Message, text="Загружаю данные...", is_main=True):
    chat_id = message.chat.id
    msg = await message.answer(f"⏳ <i>{text}</i>", parse_mode="HTML")
    return msg

# --- ОБРАБОТЧИКИ НОВЫХ КНОПОК ГЛАВНОГО МЕНЮ ---

@router.message(F.text == "👥 Группы")
async def btn_groups_select(message: Message, state: FSMContext):
    await cb_filter_type_internal(message, "group", "main_menu")

@router.message(F.text == "👩‍🏫 Преподаватели")
async def btn_teachers_select(message: Message, state: FSMContext):
    await cb_filter_type_internal(message, "teacher", "main_menu")

@router.message(F.text == "🏫 Аудитории")
async def btn_classrooms_select(message: Message, state: FSMContext):
    await cb_filter_type_internal(message, "classroom", "main_menu")

async def cb_filter_type_internal(message: Message, target_type: str, view_info: str):
    db_map = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}
    title_map = {"group": "группу", "teacher": "преподавателя", "classroom": "аудиторию"}
    if target_type not in db_map: return

    kb = []
    items = list(db_map[target_type].keys())
    for i, item_name in enumerate(items):
        kb.append([InlineKeyboardButton(text=item_name, callback_data=f"fsel:{target_type}:{i}:{view_info}")])
    
    await message.answer(f"👇 Выберите {title_map[target_type]} из списка:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

# --- ОБНОВЛЕННЫЕ ОБРАБОТЧИКИ ДНЕЙ И НЕДЕЛЬ ---

@router.message(F.text.in_({"📅 Сегодня", "📆 Завтра", "🗓 Эта неделя", "➡️ След. неделя", "📋 Выбрать день", "📆 Выбрать неделю"}))
async def btn_schedule_handler(message: Message, state: FSMContext):
    data = await state.get_data()
    if not data.get("target_value"):
        await message.answer("⚠️ <b>Ошибка:</b> Не выбрана Группа, Преподаватель или Аудитория.\nПожалуйста, выберите объект в меню.", parse_mode="HTML", reply_markup=get_main_menu())
        return
    
    # Вызываем соответствующую логику в зависимости от текста кнопки
    if message.text in ["📅 Сегодня", "📆 Завтра"]:
        await btn_days(message, state)
    elif message.text in ["🗓 Эта неделя", "➡️ След. неделя"]:
        await btn_weeks(message, state)
    elif message.text == "📋 Выбрать день":
        await btn_select_day_menu(message)
    elif message.text == "📆 Выбрать неделю":
        await btn_select_week_menu(message)

def get_week_dates(offset):
    now = datetime.now()
    monday = now - timedelta(days=now.weekday())
    target_mon = monday + timedelta(weeks=offset)
    target_sun = target_mon + timedelta(days=6)
    return target_mon.strftime("%d.%m"), target_sun.strftime("%d.%m.%Y")

def get_week_label(offset):
    start_date, end_date = get_week_dates(offset)
    if offset == 0: return f"Текущая неделя ({start_date}-{end_date})"
    if offset == 1: return f"Следующая неделя ({start_date}-{end_date})"
    if offset == -1: return f"Прошлая неделя ({start_date}-{end_date})"
    return f"Через {offset} нед. ({start_date}-{end_date})" if offset > 0 else f"{abs(offset)} нед. назад ({start_date}-{end_date})"

def fmt_day(day, lessons, schedule, wo=0, target_type=None):
    e = DAY_EMOJI.get(day, "📅")
    ds = schedule.get("_dates", {}).get(day, "")
    text = f"🗓 {get_week_label(wo)}" + "\n"
    text += f"─────────────────────────" + "\n"
    text += f"{e} {day.upper()} — {ds}" + "\n"
    text += f"─────────────────────────" + "\n\n"

    if not lessons: return text + "😴 Нет занятий" + "\n"
    text += f"📚 Пар: {len(lessons)}" + "\n\n"
    for i, l in enumerate(lessons, 1):
        t_lower = (l.get('type') or "").lower()
        subj_icon = "🔬" if "лабораторные" in t_lower else "📝" if "практические" in t_lower else "📗"
        text += f"{subj_icon} {i}. {l['subject']}" + "\n"
        text += f" 🕐 {l['time']}" + "\n"
        if l.get("type"): text += f" 📌 {l['type']}" + "\n"
        if target_type in ["teacher", "classroom"] and l.get("group") and l.get("group") != "-":
            text += f" 👥 Группы: {l['group']}" + "\n"
        if l.get("teacher") and l.get("teacher") != "-": text += f" 👩‍🏫 {l['teacher']}" + "\n"
        if l.get("room") and l.get("room") != "-": text += f" 🏫 Ауд. {l['room']}" + "\n\n"
    return text.strip()

def fmt_week(schedule, wo=0, target_type=None):
    text = f"🗓 {get_week_label(wo)}" + "\n"
    text += f"─────────────────────────" + "\n\n"
    today_str = datetime.now().strftime("%d.%m.%Y")
    total = 0
    for day in DAYS_OF_WEEK[:6]:
        lessons = schedule.get(day, [])
        total += len(lessons)
        d = schedule.get("_dates", {}).get(day, "")
        mark = " 👈" if d == today_str else ""
        text += f"{DAY_EMOJI[day]} {day.upper()}" + (f" ({d})" if d else "") + f"{mark}" + "\n"
        if not lessons: text += "😴 Выходной" + "\n\n"
        else:
            text += f"📚 Пар: {len(lessons)}" + "\n"
            for l in lessons:
                t_lower = (l.get('type') or "").lower()
                subj_icon = "🔬" if "лабораторные" in t_lower else "📝" if "практические" in t_lower else "📗"
                text += f" {subj_icon} {l['time']} | {l['subject']}" + "\n"
            text += "\n"
    text += f"─────────────────────────" + "\n" + f"📊 Всего: {total} пар"
    return text

def get_day_nav(di, wo=0):
    nav = []
    if di > 0: nav.append(InlineKeyboardButton(text=f"⬅️ {SHORT_DAYS[di-1]}", callback_data=f"showday_{di-1}_{wo}"))
    elif wo > -4: nav.append(InlineKeyboardButton(text="⬅️ Пт", callback_data=f"showday_4_{wo-1}"))
    nav.append(InlineKeyboardButton(text=f"📅 {SHORT_DAYS[di]}", callback_data="noop"))
    if di < 5: nav.append(InlineKeyboardButton(text=f"{SHORT_DAYS[di+1]} ➡️", callback_data=f"showday_{di+1}_{wo}"))
    elif wo < 8: nav.append(InlineKeyboardButton(text="Пн ➡️", callback_data=f"showday_0_{wo+1}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        nav, 
        [InlineKeyboardButton(text="🗓 Вся неделя", callback_data=f"showweek_{wo}")], 
        [InlineKeyboardButton(text="⚙️ Фильтр", callback_data=f"filter:day_{di}_{wo}"), InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_day_{di}_{wo}")]
    ])

def get_week_nav(wo=0):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⏪ Пред.нед", callback_data=f"showweek_{wo-1}"), InlineKeyboardButton(text="След.нед ⏩", callback_data=f"showweek_{wo+1}")],
        [InlineKeyboardButton(text="⚙️ Фильтр", callback_data=f"filter:week_{wo}"), InlineKeyboardButton(text="🔄 Обновить", callback_data=f"refresh_week_{wo}")]
    ])

# ═══════════════ ОБРАБОТЧИКИ ═════════════════════

@router.message(F.text == "🧹 Очистить")
async def btn_clear_chat(message: Message, state: FSMContext):
    await state.clear()
    msg_id = message.message_id
    
    # Пытаемся удалить последние 500 сообщений (5 пачек по 100)
    # Это наиболее эффективный способ "очистки" истории без хранения всех ID в базе
    for i in range(5):
        current_max = msg_id - (i * 100)
        if current_max <= 0:
            break
        
        msg_ids = list(range(max(1, current_max - 99), current_max + 1))
        try:
            # delete_messages удаляет до 100 сообщений за раз
            await message.bot.delete_messages(chat_id=message.chat.id, message_ids=msg_ids)
        except Exception:
            # Игнорируем ошибки (например, сообщения старее 48 часов, которые Telegram не дает удалить)
            continue
        
    await message.answer("🧹 Чат очищен (удалено до 500 последних сообщений).\n📋 Меню:", reply_markup=get_main_menu())

@router.message(F.text.in_({"🏠 Главное меню", "🔙 Главное меню"}))
async def btn_main_menu(message: Message, state: FSMContext):
    await state.set_state(None)
    await message.answer("📋 Меню:", reply_markup=get_main_menu())

@router.message(F.text == "🙈 Скрыть")
async def btn_hide_kb(message: Message):
    await message.answer("⌨️ Клавиатура скрыта. Чтобы вернуть её, напишите /start или /menu", reply_markup=ReplyKeyboardRemove())

@router.message(Command("menu"))
async def cmd_menu(message: Message, state: FSMContext):
    data = await state.get_data()
    await message.answer("📋 Меню:", reply_markup=get_main_menu(data.get("target_value")))


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()

    # Если пишет админ и бот в режиме техработ - выходим из него
    if message.from_user.id in ADMIN_IDS and is_maintenance():
        set_maintenance(False)
        users = get_users()
        await message.answer("✅ <b>Режим технических работ отключен.</b>\nРассылаю уведомление пользователям...", parse_mode="HTML")
        
        count = 0
        for user_id in users:
            try:
                if user_id == message.from_user.id: continue
                await bot.send_message(user_id, "✅ <b>Технические работы завершены!</b>\nБот снова работает в штатном режиме.", parse_mode="HTML")
                count += 1
                await asyncio.sleep(0.05)
            except Exception: pass
        
        await message.answer(f"📢 Уведомление доставлено {count} пользователям.")

    msg = await message.answer("👋 Бот расписания\nИспользуй кнопки ниже 👇", reply_markup=get_main_menu())


@router.message(F.text == "🔄 Сбросить")
async def btn_reset(message: Message, state: FSMContext):
    await state.clear()
    msg = await message.answer("✅ Фильтры сброшены. Теперь отображается ваше стандартное расписание.", reply_markup=get_main_menu())


@router.message(F.text == "📋 Выбрать день")
async def btn_select_day_menu(message: Message):

    kb = [[InlineKeyboardButton(text=f"{DAY_EMOJI[DAYS_OF_WEEK[i]]} {DAYS_OF_WEEK[i]}", callback_data=f"showday_{i}_0")] for i in range(6)]
    msg = await message.answer("📋 Выберите день (текущая неделя):", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.message(F.text == "📆 Выбрать неделю")
async def btn_select_week_menu(message: Message):

    kb = [[InlineKeyboardButton(text=get_week_label(-1), callback_data="showweek_-1")], [InlineKeyboardButton(text=get_week_label(0), callback_data="showweek_0")], [InlineKeyboardButton(text=get_week_label(1), callback_data="showweek_1")], [InlineKeyboardButton(text=get_week_label(2), callback_data="showweek_2")]]
    msg = await message.answer("📆 Выберите неделю:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))


@router.message(F.text.in_({"📅 Сегодня", "📆 Завтра"}))
async def btn_days(message: Message, state: FSMContext):
    data = await state.get_data()
    logger.info(f"State data in btn_days: {data}")


    status_msg = await send_loading_status(message, "Загружаю расписание...", is_main=False)

    try:
        wd = datetime.now().weekday()
        target_type = data.get("target_type")
        target_value = data.get("target_value")

        if message.text == "📅 Сегодня":
            s = await parser.fetch_schedule(0, target_type, target_value)
            idx = find_today_index(s)
            wo = 0
        else: # Завтра
            wo, idx = (1, 0) if wd >= 5 else (0, wd + 1)
            s = await parser.fetch_schedule(wo, target_type, target_value)
        
        try:
            await status_msg.delete()
        except Exception as e:
            logger.warning(f"Failed to delete status message: {e}")

        new_msg_text = ""
        if target_value:
            new_msg_text = f"✅ Фильтр: {target_value}\n"
        new_msg_text += fmt_day(DAYS_OF_WEEK[idx], s.get(DAYS_OF_WEEK[idx], []), s, wo, target_type)
        inline_markup = get_day_nav(min(idx, 5), wo)
        
        # Send schedule with back menu
        schedule_msg = await message.answer(new_msg_text, parse_mode="HTML", reply_markup=inline_markup)


    except Exception as e:
        logger.error(f"Error in btn_days handler: {e}")
        try:
            await status_msg.edit_text("⚠️ Ошибка при загрузке расписания дня. Попробуйте позже.")
        except Exception as e_inner:
            logger.error(f"Failed to even edit the status message: {e_inner}")

@router.message(F.text.in_({"🗓 Эта неделя", "➡️ След. неделя"}))
async def btn_weeks(message: Message, state: FSMContext):
    data = await state.get_data()
    logger.info(f"State data in btn_weeks: {data}")


    status_msg = await send_loading_status(message, "Загружаю расписание...", is_main=False)

    try:
        wo = 0 if message.text == "🗓 Эта неделя" else 1
        target_type = data.get("target_type")
        target_value = data.get("target_value")
        s = await parser.fetch_schedule(wo, target_type, target_value)
        
        try:
            await status_msg.delete()
        except Exception as e:
            logger.warning(f"Failed to delete status message: {e}")

        new_msg_text = ""
        if target_value:
            new_msg_text = f"✅ Фильтр: {target_value}\n"
        new_msg_text += fmt_week(s, wo, target_type)
        inline_markup = get_week_nav(wo)

        # Send schedule with back menu
        schedule_msg = await message.answer(new_msg_text, parse_mode="HTML", reply_markup=inline_markup)


    except Exception as e:
        logger.error(f"Error in btn_weeks handler: {e}")
        try:
            await status_msg.edit_text("⚠️ Ошибка при загрузке расписания недели. Попробуйте позже.")
        except Exception as e_inner:
            logger.error(f"Failed to even edit the status message: {e_inner}")

@router.callback_query(F.data == "noop")
async def cb_noop(c: CallbackQuery): await c.answer("Используй боковые стрелки ⬅️ ➡️")

# --- ИНЛАЙН КНОПКИ (Редактируют сообщение без проблем) ---

async def show_day_view(message: Message, state: FSMContext, di: int, wo: int):
    data = await state.get_data()
    target_type = data.get("target_type")
    target_value = data.get("target_value")
    s = await parser.fetch_schedule(wo, target_type, target_value)
    text = ""
    if target_value:
        text = f"✅ Фильтр: {target_value}\n"
    text += fmt_day(DAYS_OF_WEEK[di], s.get(DAYS_OF_WEEK[di], []), s, wo, target_type)
    await message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_day_nav(di, wo)
    )

async def show_week_view(message: Message, state: FSMContext, wo: int):
    data = await state.get_data()
    target_type = data.get("target_type")
    target_value = data.get("target_value")
    s = await parser.fetch_schedule(wo, target_type, target_value)
    text = ""
    if target_value:
        text = f"✅ Фильтр: {target_value}\n"
    text += fmt_week(s, wo, target_type)
    await message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=get_week_nav(wo)
    )

@router.callback_query(F.data.startswith("showday_"))
async def cb_show_day(c: CallbackQuery, state: FSMContext):
    await c.answer("⏳ Обновляю...")
    try:
        di, wo = map(int, c.data.replace("showday_", "").split("_"))
        await show_day_view(c.message, state, di, wo)
    except Exception as e:
        logger.error(f"Error in cb_show_day: {e}")
        try:
            await c.message.edit_text("⚠️ Ошибка при обновлении. Попробуйте снова.")
        except Exception:
            pass

@router.callback_query(F.data.startswith("showweek_"))
async def cb_show_week(c: CallbackQuery, state: FSMContext):
    await c.answer("⏳ Обновляю...")
    try:
        wo = int(c.data.replace("showweek_", ""))
        await show_week_view(c.message, state, wo)
    except Exception as e:
        logger.error(f"Error in cb_show_week: {e}")
        try:
            await c.message.edit_text("⚠️ Ошибка при обновлении. Попробуйте снова.")
        except Exception:
            pass

@router.callback_query(F.data.startswith("filter:"))
async def cb_filter(c: CallbackQuery, state: FSMContext):
    await c.answer("Выберите тип фильтра")
    view_info = c.data.split(":", 1)[1]
    kb = [
        [InlineKeyboardButton(text="Группа", callback_data=f"ftype:group:{view_info}")],
        [InlineKeyboardButton(text="Преподаватель", callback_data=f"ftype:teacher:{view_info}")],
        [InlineKeyboardButton(text="Аудитория", callback_data=f"ftype:classroom:{view_info}")],
        [InlineKeyboardButton(text="🔙 Назад", callback_data=f"back:{view_info}")]
    ]
    await c.message.edit_text("⚙️ Выберите, по какому параметру фильтровать:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("ftype:"))
async def cb_filter_type(c: CallbackQuery, state: FSMContext):
    await c.answer()
    _prefix, target_type, view_info = c.data.split(":", 2)

    db_map = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}
    title_map = {"group": "группу", "teacher": "преподавателя", "classroom": "аудиторию"}
    
    if target_type not in db_map: return

    kb = []
    # Using indices to keep callback_data short (limit is 64 bytes)
    items = list(db_map[target_type].keys())
    for i, item_name in enumerate(items):
        kb.append([InlineKeyboardButton(text=item_name, callback_data=f"fsel:{target_type}:{i}:{view_info}")])

    kb.append([InlineKeyboardButton(text="🔙 Назад", callback_data=f"filter:{view_info}")])
    
    await c.message.edit_text(f"👇 Выбери {title_map[target_type]} из списка:", reply_markup=InlineKeyboardMarkup(inline_keyboard=kb))

@router.callback_query(F.data.startswith("fsel:"))
async def cb_filter_select(c: CallbackQuery, state: FSMContext):
    await c.answer("⏳ Обновляю...")
    try:
        _prefix, target_type, item_idx, view_info = c.data.split(":", 3)
        item_idx = int(item_idx)
        
        db_map = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}
        items = list(db_map[target_type].keys())
        target_value = items[item_idx]
        
        parser.clear_cache()
        await state.update_data(target_type=target_type, target_value=target_value)
        
        if view_info == "main_menu":
            try:
                await c.message.delete()
            except Exception:
                pass
            await c.message.answer(f"✅ Фильтр установлен: <b>{target_value}</b>\nВыберите период расписания:", parse_mode="HTML", reply_markup=get_main_menu(target_value))
        elif view_info.startswith("day_"):
            di, wo = map(int, view_info.replace("day_", "").split("_"))
            await show_day_view(c.message, state, di, wo)
        elif view_info.startswith("week_"):
            wo = int(view_info.replace("week_", ""))
            await show_week_view(c.message, state, wo)
    except Exception as e:
        logger.error(f"Error in cb_filter_select: {e}")
        try:
            await c.message.edit_text("⚠️ Ошибка при обновлении. Попробуйте снова.")
        except Exception:
            pass

@router.callback_query(F.data.startswith("back:"))
async def cb_back_to_schedule(c: CallbackQuery, state: FSMContext):
    await c.answer()
    view_info = c.data.split(":", 1)[1]
    try:
        if view_info == "main_menu":
            try:
                await c.message.delete()
            except Exception:
                pass
            await c.message.answer("📋 Меню:", reply_markup=get_main_menu())
        elif view_info.startswith("day_"):
            di, wo = map(int, view_info.replace("day_", "").split("_"))
            await show_day_view(c.message, state, di, wo)
        elif view_info.startswith("week_"):
            wo = int(view_info.replace("week_", ""))
            await show_week_view(c.message, state, wo)
    except Exception as e:
        logger.error(f"Error in cb_back_to_schedule: {e}")
        try:
            await c.message.edit_text("⚠️ Ошибка при обновлении. Попробуйте снова.")
        except Exception:
            pass

@router.callback_query(F.data.startswith("refresh_"))
async def cb_refresh(c: CallbackQuery, state: FSMContext):
    await c.answer("⏳ Обновляю...")
    parser.clear_cache()
    data = await state.get_data()
    action = c.data.replace("refresh_", "")
    
    try:
        if action.startswith("week_"):
            wo = int(action.replace("week_", ""))
            s = await parser.fetch_schedule(wo, data.get("target_type"), data.get("target_value"))
            await c.message.edit_text(
                fmt_week(s, wo, data.get("target_type")),
                parse_mode="HTML",
                reply_markup=get_week_nav(wo)
            )
        elif action.startswith("day_"):
            di, wo = map(int, action.replace("day_", "").split("_"))
            s = await parser.fetch_schedule(wo, data.get("target_type"), data.get("target_value"))
            await c.message.edit_text(
                fmt_day(DAYS_OF_WEEK[di], s.get(DAYS_OF_WEEK[di], []), s, wo, data.get("target_type")),
                parse_mode="HTML",
                reply_markup=get_day_nav(di, wo)
            )
    except Exception as e:
        logger.error(f"Error in cb_refresh: {e}")
        try:
            await c.message.edit_text("⚠️ Ошибка при обновлении. Попробуйте снова.")
        except Exception:
            pass

@router.message(F.text == "/stop")
async def cmd_stop_maintenance(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    set_maintenance(True)
    users = get_users()
    await message.answer(f"🛑 <b>Включен режим технических работ.</b>\nНачинаю рассылку для {len(users)} пользователей...", parse_mode="HTML")
    
    count = 0
    for user_id in users:
        try:
            if user_id == message.from_user.id: continue
            await bot.send_message(
                user_id, 
                "⚠️ <b>Внимание:</b> Бот уходит на технические работы.\n"
                "Ваши запросы будут проигнорированы до завершения работ. Приносим извинения за неудобства.",
                parse_mode="HTML"
            )
            count += 1
            await asyncio.sleep(0.05)
        except Exception: pass
            
    await message.answer(f"✅ Рассылка завершена ({count} чел.). Бот переведен в режим обслуживания.")
    logger.info("Maintenance mode enabled by admin.")

@router.message(F.text.startswith("/broadcast"))
async def cmd_broadcast(message: Message):
    if message.from_user.id not in ADMIN_IDS:
        return

    command_parts = message.text.split(" ", 1)
    if len(command_parts) < 2:
        await message.answer("📝 Использование: `/broadcast Текст сообщения`", parse_mode="Markdown")
        return

    broadcast_text = command_parts[1]
    users = get_users()
    
    count = 0
    await message.answer(f"🚀 Начинаю рассылку на {len(users)} пользователей...")
    
    for user_id in users:
        try:
            await bot.send_message(user_id, broadcast_text)
            count += 1
            await asyncio.sleep(0.05) # Небольшая задержка, чтобы не поймать лимиты
        except Exception as e:
            logger.error(f"Failed to send broadcast to {user_id}: {e}")
            
    await message.answer(f"✅ Рассылка завершена! Получили: {count} из {len(users)}")

@router.message()
async def fallback_any_text(message: Message, state: FSMContext):
    await message.answer("👇 Используй меню ниже:", reply_markup=get_main_menu())

async def main():
    # Пропускаем сообщения, пришедшие когда бот был выключен
    await bot.delete_webhook(drop_pending_updates=True)
    await dp.start_polling(bot)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nБот остановлен.")
