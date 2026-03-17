import os
import re
import json
import logging
import asyncio
import urllib.parse
from datetime import datetime, timedelta
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import redis.asyncio as redis
from typing import Any

# ═══════════════════ НАСТРОЙКИ ═══════════════════
PROXY_URL = os.getenv("PROXY_URL") # Формат: http://ip:port
SCHEDULE_URL = "https://up.corp.tu-ugmk.com/student/schedule"
COOKIES = {} 
LOGIN = os.getenv("LOGIN", "uvybhjhhv@gmail.com")
PASSWORD = os.getenv("PASSWORD", "qazwsxedcip60000OP")

DATA_DIR = "data"
CACHE_DIR = "cache"

CACHE_LIFETIME = 86400 
CACHE_VERSION = 32

# Создаем папки
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)
# ═════════════════════════════════════════════════

# ════════════ БАЗЫ ДАННЫХ ID ═════════════════════
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
LESSON_TYPES = ["Лекции", "Практические", "Лабораторные", "Семинар", "Экзамен", "Зачет", "Зачёт", "Консультация", "Курсовая работа", "Курсовой проект"]
# ═════════════════════════════════════════════════

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scraper")

class RedisDAO:
    def __init__(self, host=os.getenv("REDIS_HOST", "localhost"), port=int(os.getenv("REDIS_PORT", 6379)), db=0):
        self.client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        self.is_connected = False

    async def connect(self):
        try:
            await self.client.ping()
            self.is_connected = True
            logger.info("✅ Redis DAO: Подключено.")
        except Exception as e:
            self.is_connected = False
            logger.warning(f"⚠️ Redis DAO: Ошибка подключения: {e}")
            raise

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
            
    async def sadd(self, name: str, value: str):
        if not self.is_connected: return
        try:
            await self.client.sadd(name, value)
            await self.client.expire(name, CACHE_LIFETIME)
        except Exception as e:
            logger.error(f"DAO SAdd Error: {e}")

    async def blpop(self, key: str, timeout: int = 0):
        if not self.is_connected: return None
        try:
            return await self.client.blpop(key, timeout)
        except Exception as e:
            logger.error(f"DAO BLPOP Error: {e}")
            return None

dao = RedisDAO()

class ScheduleParser:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self._initialized = False

    async def init(self):
        if self._initialized: return
        
        self.playwright = await async_playwright().start()
        
        launch_kwargs = {"headless": True}
        if PROXY_URL:
            launch_kwargs["proxy"] = {"server": PROXY_URL}
            logger.info(f"🌐 Используется прокси для Playwright: {PROXY_URL}")
            
        self.browser = await self.playwright.chromium.launch(**launch_kwargs)
        self._initialized = True
        logger.info("✅ Playwright инициализирован.")

    async def _login_page(self, page):
        try:
            await page.fill('input[name="LoginForm[login]"], #openid-auth-user', LOGIN)
            await page.fill('input[name="LoginForm[password]"], #openid-auth-pwd', PASSWORD)
            await page.click('#login-submit, button[type="submit"]')
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception: pass

    def _get_week_dates(self, offset):
        now = datetime.now()
        monday = now - timedelta(days=now.weekday())
        target_mon = monday + timedelta(weeks=offset)
        return target_mon.strftime("%d.%m.%Y"), (target_mon + timedelta(days=6)).strftime("%d.%m.%Y")

    def _build_schedule_url(self, week_offset=0, target_type=None, target_value=None):
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
        return url

    async def fetch_and_parse(self, week_offset=0, target_type=None, target_value=None):
        context = await self.browser.new_context(user_agent="Mozilla/5.0")
        if COOKIES:
            cookie_list = [{"name": n, "value": v, "domain": "up.corp.tu-ugmk.com", "path": "/"} for n, v in COOKIES.items()]
            await context.add_cookies(cookie_list)
        
        page = await context.new_page()
        try:
            url = self._build_schedule_url(week_offset, target_type, target_value)
            logger.info(f"Fetching: {url}")
            await page.goto(url, wait_until="networkidle", timeout=45000)
            if "login" in page.url.lower():
                await self._login_page(page)
                await page.goto(url, wait_until="networkidle", timeout=45000)

            try: await page.wait_for_selector("table", timeout=5000)
            except: pass

            html = await page.content()
            return self._parse_html(html, target_type, target_value)
        finally:
            await page.close()
            await context.close()

    def _find_schedule_tables(self, all_tables, target_type, target_value):
        """Find the correct tables for the given target."""
        if target_type == "group" and target_value:
            target_tables = []
            for table in all_tables:
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) >= 4 and target_value.lower() in cells[3].get_text().lower():
                        target_tables.append(table)
                        break
            if target_tables:
                return target_tables

        # Fallback for other types or if group search fails
        if len(all_tables) >= 14 and target_type:
            return all_tables[-7:]
        return all_tables[:7]

    def _parse_html(self, html, target_type=None, target_value=None):
        soup = BeautifulSoup(html, "lxml")
        schedule, day_dates = {}, {}
        
        for text in soup.stripped_strings:
            for day in DAYS_OF_WEEK:
                m = re.search(r"(\d{2}\.\d{2}\.\d{4})", text)
                if text.startswith(day) and m:
                    day_dates[day] = m.group(1)

        all_tables = soup.find_all("table")
        target_tables = self._find_schedule_tables(all_tables, target_type, target_value)

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

async def main():
    logger.info("🚀 Scraper worker запущен...")
    await dao.connect()
    
    parser = ScheduleParser()
    await parser.init()

    while True:
        try:
            # Ожидаем задачу из очереди 'schedule_jobs'
            job_data = await dao.blpop('schedule_jobs')
            if not job_data:
                continue

            _, job_json = job_data
            logger.info(f"Получена новая задача: {job_json}")
            
            job = json.loads(job_json)
            week_offset = job.get('week_offset', 0)
            target_type = job.get('target_type')
            target_value = job.get('target_value')
            
            # --- Логика получения и сохранения ---
            target_id = f"{target_type}:{target_value}" if target_type else "default"
            data_key = f"data:v{CACHE_VERSION}:{target_id}:w{week_offset}"
            index_key = f"index:v{CACHE_VERSION}:{target_id}"

            # Проверяем кэш на всякий случай, вдруг другой воркер уже сделал
            cached = await dao.get(data_key)
            if cached is not None:
                logger.info(f"Задача уже была в кэше: {data_key}")
                continue

            # Парсим
            schedule = await parser.fetch_and_parse(week_offset, target_type, target_value)
            
            # Сохраняем в кэш
            if schedule:
                logger.info(f"Сохраняю в кэш: {data_key}")
                await dao.set(data_key, schedule)
                await dao.sadd(index_key, data_key)
                
                # Сохранение в файловый кэш (опционально, но сохраним для консистентности)
                os.makedirs(CACHE_DIR, exist_ok=True)
                path = os.path.join(CACHE_DIR, data_key.replace(":", "_") + ".json")
                data = {"timestamp": datetime.now().isoformat(), "schedule": schedule}
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)

        except json.JSONDecodeError as e:
            logger.error(f"Ошибка декодирования JSON: {e}")
        except Exception as e:
            logger.error(f"Критическая ошибка в цикле воркера: {e}", exc_info=True)
            # Пауза перед следующей попыткой, чтобы избежать постоянных падений
            await asyncio.sleep(5)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Scraper worker остановлен.")
