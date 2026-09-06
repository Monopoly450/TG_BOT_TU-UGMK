import os
import re
import json
import logging
import asyncio
import urllib.parse
from datetime import datetime, timedelta, timezone
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright
import redis.asyncio as redis
from typing import Any
from network_config import configure_direct_network

# ═══════════════════ НАСТРОЙКИ ═══════════════════
SCHEDULE_URL = "https://up.corp.tu-ugmk.com/student/schedule"
LOGIN = os.getenv("LOGIN")
PASSWORD = os.getenv("PASSWORD")

if not LOGIN or not PASSWORD:
    raise RuntimeError("LOGIN and PASSWORD must be configured in .env")

CACHE_LIFETIME = 86400
YEKATERINBURG_TZ = timezone(timedelta(hours=5))

# ════════════ БАЗЫ ДАННЫХ ID ═════════════════════
from schedule_config import GROUPS_DB, CACHE_VERSION, canonical_group, active_group, merged_groups, lesson_matches_group, migrate_group_preferences

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
    async def set(self, key, value, ex=CACHE_LIFETIME):
        if self.ok: await self.client.set(key, json.dumps(value, ensure_ascii=False), ex=ex)
    async def blpop(self, key, timeout=4):
        # redis-py 8 uses a 5-second socket timeout. Return from BLPOP before
        # that deadline so an empty queue is not logged as a network failure.
        return await self.client.blpop(key, timeout) if self.ok else None

dao = RedisDAO()

class ScheduleParser:
    def __init__(self):
        self.playwright, self.browser, self._initialized = None, None, False
    async def init(self):
        if self._initialized: return
        configure_direct_network()
        self.playwright = await async_playwright().start()
        l_kwargs = {"headless": True, "args": ["--no-proxy-server"]}
        self.browser = await self.playwright.chromium.launch(**l_kwargs)
        self.ctx = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080}
        )
        self.page = await self.ctx.new_page()
        self._initialized = True
    async def _login(self, page):
        try:
            logger.info("Attempting schedule-site login...")
            await page.wait_for_selector('input[name="LoginForm[login]"], #openid-auth-user, input[type="text"]', timeout=15000)
            
            # Заполняем форму логина (1C или локальную)
            user_input = await page.query_selector('input[name="LoginForm[login]"]') or await page.query_selector('#openid-auth-user') or await page.query_selector('input[type="text"]')
            pwd_input = await page.query_selector('input[name="LoginForm[password]"]') or await page.query_selector('#openid-auth-pwd') or await page.query_selector('input[type="password"]')
            
            if user_input: await user_input.fill(LOGIN)
            if pwd_input: await pwd_input.fill(PASSWORD)
            
            # Нажимаем кнопку входа
            submit_btn = await page.query_selector('#login-submit, button[type="submit"], input[type="submit"], .btn-primary, button:has-text("Войти")')
            if submit_btn:
                await submit_btn.click()
            else:
                await page.keyboard.press("Enter")
                
            # Ждем завершения редиректов (важно для OAuth и SSO)
            logger.info("Waiting for SSO redirect to finish...")
            await asyncio.sleep(5)
            await page.wait_for_load_state("domcontentloaded", timeout=30000)
            
            logger.info(f"Login submitted. Final URL: {page.url}")
            if "login" in page.url.lower() or "auth" in page.url.lower():
                logger.error("Login failed: Still on auth/login page. Check credentials!")
                await page.screenshot(path="debug_login_error.png")
                return False
            return True
        except Exception as e: 
            logger.error(f"Login process error: {e}")
            await page.screenshot(path="debug_login_crash.png")
            return False

    def _get_dates(self, offset):
        mon = datetime.now(YEKATERINBURG_TZ) - timedelta(days=datetime.now(YEKATERINBURG_TZ).weekday()) + timedelta(weeks=offset)
        return mon.strftime("%d.%m.%Y"), (mon + timedelta(days=6)).strftime("%d.%m.%Y")

    def _build_url(self, wo=0, t_type=None, t_val=None, oid=None):
        sd, ed = self._get_dates(wo)
        oid = urllib.parse.quote(urllib.parse.unquote(oid), safe="")
        url = f"{SCHEDULE_URL}?scheduleType=Week&objectType=AcademicGroup&objectId={oid}&startDate={sd}&endDate={ed}&_referrer=%2Fstudent%2Findex"
        url += f"&another_group={urllib.parse.quote(t_val)}"
        return url

    async def get_entity_id(self, t_type, t_val):
        if t_type != "group":
            return None
        t_val = canonical_group(t_val)
        if not active_group(t_val):
            return None
        discovered = await dao.client.hgetall("db_groups") if dao.ok else {}
        return merged_groups(discovered).get(t_val)

    async def discover_entities(self, html):
        try:
            if not dao.ok: await dao.connect()
            if not dao.ok: return
            soup = BeautifulSoup(html, "lxml")
            for a in soup.find_all("a", href=True):
                href = a["href"]
                if "/student/schedule" in href:
                    parsed_url = urllib.parse.urlparse(href)
                    qs = urllib.parse.parse_qs(parsed_url.query)
                    obj_type = qs.get("objectType", [None])[0]
                    obj_id = qs.get("objectId", [None])[0]
                    if not obj_type or not obj_id: continue
                    name = a.get_text(strip=True)
                    if not name: continue
                    if obj_type == "AcademicGroup":
                        group_name = qs.get("another_group", [None])[0]
                        group_name = urllib.parse.unquote(group_name) if group_name else name
                        if active_group(group_name) and lesson_matches_group(name, group_name):
                            await dao.client.hset("db_groups", canonical_group(group_name), obj_id)
        except Exception as e:
            logger.error(f"Error in discover_entities: {e}")

    async def fetch(self, wo=0, t_type=None, t_val=None):
        try:
            t_val = canonical_group(t_val)
            if wo not in (0, 1) or not active_group(t_val):
                return {"_error": "Выберите действующую группу и неделю"}
            oid = await self.get_entity_id(t_type, t_val)
            if not oid:
                return {"_error": f"ID for {t_type} '{t_val}' not found"}
            url = self._build_url(wo, t_type, t_val, oid)
            logger.info(f"[{t_type}] Fetching: {url}")
            await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if "login" in self.page.url.lower() or "auth" in self.page.url.lower():
                if not await self._login(self.page): return {"_error": "Login failed"}
                # Wait for any post-login redirects
                await asyncio.sleep(3)
                if self.page.url != url: await self.page.goto(url, wait_until="domcontentloaded", timeout=60000)
            
            # Ждем появления контейнеров с расписанием или сообщения об ошибке
            try:
                await self.page.wait_for_selector(".day-container, .alert-danger, .empty-result, table.table, h3", timeout=3000)
                # Даем немного времени на отрисовку JS если нужно
                await asyncio.sleep(0.5)
            except:
                logger.warning("Timeout waiting for specific selectors, proceeding to parse anyway.")
            
            logger.info(f"Page title: {await self.page.title()}")
            html = await self.page.content()
            with open("debug_last_fetch.html", "w", encoding="utf-8") as f: f.write(html)
            await self.discover_entities(html)
            res = self._parse(html, t_type, t_val)
            if res.get("_error"):
                return res
            res["_group"] = t_val
            
            has_lessons = any(isinstance(v, list) and len(v) > 0 for k, v in res.items() if k != "_dates")
            
            if not res or (not res.get("_dates") and not has_lessons):
                if "ошибка" in html.lower(): 
                    return {"_error": "Site error message"}
                if "не найден" in html.lower() or "нет данных" in html.lower():
                    return {"_empty": True}
                return {"_error": "No data parsed"}
            return res
        except Exception as e: 
            logger.error(f"Fetch error: {e}")
            return {"_error": str(e)}

    def _parse(self, html, t_type=None, t_val=None):
        soup, schedule, dates = BeautifulSoup(html, "lxml"), {}, {}
        
        # Находим контейнеры дней
        day_containers = soup.find_all("div", class_="day-container")
        
        if not day_containers:
            logger.warning("No .day-container found, using legacy table parser")
            return self._parse_legacy(soup, t_type, t_val)

        for container in day_containers:
            # Находим заголовок или любой тег, содержащий день недели
            day_name = None
            header_text = ""
            
            # Ищем сначала явные заголовки, если нет - любые элементы с текстом
            for tag in container.find_all(["h3", "h4", "strong", "span", "div", "p"]):
                txt = tag.get_text(separator=" ", strip=True)
                for d in DAYS_OF_WEEK:
                    if txt.lower().startswith(d.lower()) or f" {d.lower()} " in f" {txt.lower()} ":
                        day_name = d
                        header_text = txt
                        break
                if day_name: break
            
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
                    if len(cells) < 3: continue
                    
                    disc_cell = cells[1]
                    disc_text = disc_cell.get_text(separator=" ", strip=True)
                    if not disc_text or len(disc_text) < 2: continue
                    
                    l_type_span = disc_cell.find("span", class_="lesson-type")
                    l_type = l_type_span.get_text(strip=True) if l_type_span else ""
                    subject = disc_text.replace(l_type, "").strip() if l_type else disc_text
                    
                    # Ищем любые внешние ссылки в ячейках строки (например, Толк/Телемост)
                    link_url = ""
                    for cell in cells:
                        for a_tag in cell.find_all("a", href=True):
                            href = a_tag["href"]
                            if href.startswith("http") and "/student/schedule" not in href:
                                link_url = href
                                break
                        if link_url:
                            break
                    
                    lessons.append({
                        "time": cells[0].get_text(strip=True), 
                        "subject": subject,
                        "type": l_type,
                        "room": cells[2].get_text(strip=True) if len(cells) > 2 else "",
                        "group": cells[3].get_text(strip=True) if len(cells) > 3 else "", 
                        "teacher": cells[-1].get_text(strip=True) if len(cells) > 3 else "",
                        "link": link_url,
                    })
            schedule[day_name] = lessons
            
        schedule["_dates"] = dates
        if t_type == "group":
            rows = [lesson for lessons in schedule.values() if isinstance(lessons, list) for lesson in lessons]
            if any(not lesson_matches_group(lesson.get("group"), t_val) for lesson in rows):
                return {"_error": "Сайт вернул расписание другой группы. Обновите список групп и попробуйте снова."}
        return schedule

    def _parse_legacy(self, soup, t_type, t_val):
        # Резервный метод на случай если .day-container пропадет
        schedule, dates = {}, {}
        for text in soup.stripped_strings:
            for d in DAYS_OF_WEEK:
                if text.startswith(d) and (m := re.search(r"(\d{2}\.\d{2}\.\d{4})", text)): dates[d] = m.group(1)
        
        tables = soup.find_all("table")
        day_idx = 0
        for table in tables:
            lessons = []
            has_data_rows = False
            for row in table.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) < 3: continue
                disc_text = cells[1].get_text(strip=True)
                if not disc_text: continue
                
                # Ищем любые внешние ссылки в ячейках строки (например, Толк/Телемост)
                link_url = ""
                for cell in cells:
                    for a_tag in cell.find_all("a", href=True):
                        href = a_tag["href"]
                        if href.startswith("http") and "/student/schedule" not in href:
                            link_url = href
                            break
                    if link_url:
                        break
                
                lessons.append({
                    "time": cells[0].get_text(strip=True), 
                    "subject": disc_text,
                    "room": cells[2].get_text(strip=True) if len(cells) > 2 else "",
                    "group": cells[3].get_text(strip=True) if len(cells) > 3 else "", 
                    "teacher": cells[-1].get_text(strip=True) if len(cells) > 3 else "",
                    "link": link_url,
                })
                has_data_rows = True
            
            if has_data_rows:
                day = DAYS_OF_WEEK[day_idx] if day_idx < len(DAYS_OF_WEEK) else f"Extra_{day_idx}"
                schedule[day] = lessons
                day_idx += 1
        schedule["_dates"] = dates
        if t_type == "group":
            rows = [lesson for lessons in schedule.values() if isinstance(lessons, list) for lesson in lessons]
            if any(not lesson_matches_group(lesson.get("group"), t_val) for lesson in rows):
                return {"_error": "Сайт вернул расписание другой группы. Обновите список групп и попробуйте снова."}
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
            if wo not in (0, 1):
                continue
            mon = datetime.now(YEKATERINBURG_TZ).date() - timedelta(days=datetime.now(YEKATERINBURG_TZ).weekday()) + timedelta(weeks=wo)
            sd = mon.strftime("%d.%m.%Y")
            key = f"data:v{CACHE_VERSION}:{sd}:{tt}:{tv}"
            res = await p.fetch(wo, tt, tv)
            if res and "_error" in res:
                await dao.set(key, res, ex=60)
            else:
                await dao.set(key, res if res else {"_empty": True}, ex=CACHE_LIFETIME)
            if dao.ok:
                await dao.client.delete(f"queued:{key}")
        except Exception as e: logger.error(f"Loop error: {e}"); await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
