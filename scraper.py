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
PROXY_URL = os.getenv("PROXY_URL")
SCHEDULE_URL = "https://up.corp.tu-ugmk.com/student/schedule"
LOGIN = os.getenv("LOGIN", "uvybhjhhv@gmail.com")
PASSWORD = os.getenv("PASSWORD", "qazwsxedcip60000OP")

CACHE_LIFETIME, CACHE_VERSION = 86400, 36

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
    "Эк-25109": "c52cf4a3-1542-11f0-b44a-00155d7f1420%3A06321270-5d88-11f0-b44a-00155d7f1420",
    "А-25101": "64345217-d3ec-11ef-b449-00155d7f1420%3A87999d48-5d7f-11f0-b44a-00155d7f1420",
    "Ит-25107": "4e6528d3-d3ef-11ef-b449-00155d7f1420%3A9a9bd9dc-5d84-11f0-b44a-00155d7f1420",
    "М-25102": "efdd4827-d3fb-11ef-b449-00155d7f1420%3Aa7f635af-5d85-11f0-b44a-00155d7f1420",
    "Т-25105": "8dd0b75a-d400-11ef-b449-00155d7f1420%3A690b7f2d-5d87-11f0-b44a-00155d7f1420",
    "Эн-25103": "3d685fd3-d402-11ef-b449-00155d7f1420%3A5dfec504-5d88-11f0-b44a-00155d7f1420",
    "Гд-25104": "8e4c58f1-d40a-11ef-b449-00155d7f1420%3A11b10f9e-5d82-11f0-b44a-00155d7f1420",
    "Гэм-25106": "ef68433a-d40c-11ef-b449-00155d7f1420%3A14e87d8c-5d84-11f0-b44a-00155d7f1420",
}
TEACHERS_DB = {"Сакулин Валерий Александрович": "000000376", "Мазитов Виктор Расульевич": "000000421", "Котельников Сергей Андреевич": "000000383", "Голубина Валентина Васильевна": "000000467", "Кабанов Александр Михайлович": "000000409", "Игумнова Юлия Олеговна": "000002912", "Тюжина Ирина Викторовна": "000002915"}
CLASSROOMS_DB = {"Толк5": "2355c22e-2bcd-11e7-b191-005056953b1b", "Ауд. 203": "67941c0b-ca51-11ee-b440-00155d7f0e19"}
DAYS_OF_WEEK = ["Понедельник", "Вторник", "Среда", "Четверг", "Пятница", "Суббота", "Воскресенье"]

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("scraper")

class RedisDAO:
    def __init__(self):
        self.client = redis.Redis(host=os.getenv("REDIS_HOST", "localhost"), port=6379, decode_responses=True)
        self.ok = False
    async def connect(self):
        try: await self.client.ping(); self.ok = True
        except Exception as e: logger.error(f"Redis error: {e}")
    async def get(self, key): return json.loads(await self.client.get(key)) if self.ok and await self.client.exists(key) else None
    async def set(self, key, value):
        if self.ok: await self.client.set(key, json.dumps(value, ensure_ascii=False), ex=CACHE_LIFETIME)
    async def blpop(self, key, timeout=0): return await self.client.blpop(key, timeout) if self.ok else None

dao = RedisDAO()

class ScheduleParser:
    def __init__(self):
        self.playwright, self.browser, self._initialized = None, None, False
    async def init(self):
        if self._initialized: return
        self.playwright = await async_playwright().start()
        l_kwargs = {"headless": True}
        if PROXY_URL: l_kwargs["proxy"] = {"server": PROXY_URL}
        self.browser = await self.playwright.chromium.launch(**l_kwargs)
        self._initialized = True
    async def _login(self, page):
        try:
            logger.info(f"Attempting login as {LOGIN}...")
            await page.wait_for_selector('input[name="LoginForm[login]"], #openid-auth-user', timeout=10000)
            await page.fill('input[name="LoginForm[login]"], #openid-auth-user', LOGIN)
            await page.fill('input[name="LoginForm[password]"], #openid-auth-pwd', PASSWORD)
            
            # Нажимаем кнопку входа
            await page.click('#login-submit, button[type="submit"]')
            # Ждем завершения загрузки страницы
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            
            logger.info(f"Login submitted. Final URL: {page.url}")
            if "login" in page.url.lower():
                logger.error("Login failed: Still on login page. Check credentials!")
                await page.screenshot(path="debug_login_error.png")
                return False
            return True
        except Exception as e: 
            logger.error(f"Login process error: {e}")
            await page.screenshot(path="debug_login_crash.png")
            return False

    def _get_dates(self, offset):
        mon = datetime.now() - timedelta(days=datetime.now().weekday()) + timedelta(weeks=offset)
        return mon.strftime("%d.%m.%Y"), (mon + timedelta(days=6)).strftime("%d.%m.%Y")

    def _build_url(self, wo=0, t_type=None, t_val=None):
        sd, ed = self._get_dates(wo)
        db = {"group": GROUPS_DB, "teacher": TEACHERS_DB, "classroom": CLASSROOMS_DB}
        tm = {"group": "AcademicGroup", "teacher": "Teacher", "classroom": "Classroom"}
        oid = db[t_type][t_val]
        url = f"{SCHEDULE_URL}?scheduleType=Week&objectType={tm[t_type]}&objectId={oid}&startDate={sd}&endDate={ed}&_referrer=%2Fstudent%2Findex"
        if t_type == "group": url += f"&another_group={urllib.parse.quote(t_val)}"
        return url

    async def fetch(self, wo=0, t_type=None, t_val=None):
        ctx = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        page = await ctx.new_page()
        try:
            url = self._build_url(wo, t_type, t_val)
            logger.info(f"[{t_type}] Fetching: {url}")
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if "login" in page.url.lower():
                if not await self._login(page): return {"_error": "Login failed"}
                if page.url != url: await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Ждем появления контейнеров с расписанием или сообщения об ошибке
            try:
                await page.wait_for_selector(".day-container, .alert-danger, .empty-result, table.table", timeout=15000)
                # Даем немного времени на отрисовку JS если нужно
                await asyncio.sleep(0.5)
            except:
                logger.warning("Timeout waiting for specific selectors, proceeding to parse anyway.")
            
            logger.info(f"Page title: {await page.title()}")
            html = await page.content()
            with open("debug_last_fetch.html", "w", encoding="utf-8") as f: f.write(html)
            res = self._parse(html, t_type, t_val)
            if not res or not res.get("_dates"):
                if "ошибка" in html.lower() or "не найден" in html.lower(): return {"_error": "Site error message"}
                return {"_error": "No data parsed"}
            return res
        except Exception as e: 
            logger.error(f"Fetch error: {e}")
            return {"_error": str(e)}
        finally: 
            await page.close()
            await ctx.close()

    def _parse(self, html, t_type=None, t_val=None):
        soup, schedule, dates = BeautifulSoup(html, "lxml"), {}, {}
        
        # Находим контейнеры дней
        day_containers = soup.find_all("div", class_="day-container")
        
        if not day_containers:
            logger.warning("No .day-container found, using legacy table parser")
            return self._parse_legacy(soup, t_type, t_val)

        for container in day_containers:
            header = container.find("strong", class_="day-name")
            if not header: continue
            
            header_text = header.get_text(separator=" ", strip=True)
            # Упрощенный поиск дня
            day_name = None
            for d in DAYS_OF_WEEK:
                if d.lower() in header_text.lower():
                    day_name = d
                    break
            
            if not day_name: continue
            
            # Извлекаем дату
            date_match = re.search(r"(\d{2}\.\d{2}\.\d{4})", header_text)
            if date_match:
                dates[day_name] = date_match.group(1)
            
            table = container.find("table")
            lessons = []
            if table:
                for row in table.find_all("tr"):
                    cells = row.find_all("td")
                    if len(cells) < 5: continue
                    
                    disc_cell = cells[1]
                    disc_text = disc_cell.get_text(separator=" ", strip=True)
                    if not disc_text or len(disc_text) < 2: continue
                    
                    l_type_span = disc_cell.find("span", class_="lesson-type")
                    l_type = l_type_span.get_text(strip=True) if l_type_span else ""
                    subject = disc_text.replace(l_type, "").strip() if l_type else disc_text
                    
                    lessons.append({
                        "time": cells[0].get_text(strip=True), 
                        "subject": subject,
                        "type": l_type,
                        "room": cells[2].get_text(strip=True),
                        "group": cells[3].get_text(strip=True), 
                        "teacher": cells[4].get_text(strip=True),
                    })
            schedule[day_name] = lessons
            
        schedule["_dates"] = dates
        return schedule

    def _parse_legacy(self, soup, t_type, t_val):
        # Резервный метод на случай если .day-container пропадет
        schedule, dates = {}, {}
        for text in soup.stripped_strings:
            for d in DAYS_OF_WEEK:
                if text.startswith(d) and (m := re.search(r"(\d{2}\.\d{2}\.\d{4})", text)): dates[d] = m.group(1)
        
        tables = soup.find_all("table")
        for i, table in enumerate(tables[:7]):
            day = DAYS_OF_WEEK[i] if i < len(DAYS_OF_WEEK) else f"Extra_{i}"
            lessons = []
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 5: continue
                disc_text = cells[1].get_text(strip=True)
                if not disc_text: continue
                lessons.append({
                    "time": cells[0].get_text(strip=True), 
                    "subject": disc_text,
                    "room": cells[2].get_text(strip=True),
                    "group": cells[3].get_text(strip=True), 
                    "teacher": cells[4].get_text(strip=True),
                })
            schedule[day] = lessons
        schedule["_dates"] = dates
        return schedule

async def main():
    await dao.connect()
    p = ScheduleParser(); await p.init()
    logger.info("🚀 Scraper ready.")
    while True:
        try:
            job_data = await dao.blpop('schedule_jobs')
            if not job_data: continue
            job = json.loads(job_data[1])
            wo, tt, tv = job.get('week_offset', 0), job.get('target_type'), job.get('target_value')
            key = f"data:v{CACHE_VERSION}:{tt}:{tv}:w{wo}"
            res = await p.fetch(wo, tt, tv)
            # Даже если расписание пустое, сохраняем пустой словарь, чтобы бот не висел
            await dao.set(key, res if res else {"_empty": True})
        except Exception as e: logger.error(f"Loop error: {e}"); await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
