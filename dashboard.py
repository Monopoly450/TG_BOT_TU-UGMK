import os
import re
import base64
import html
import logging
import asyncio
import aiohttp
from network_config import telegram_connector
from ai_load import AIBusyError
from fastapi.middleware.gzip import GZipMiddleware
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from db_manager import db_manager

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

_bot_username = os.getenv("BOT_USERNAME", "TU_UGMK_bot")
_bot_username_fetched = False

async def get_bot_username() -> str:
    global _bot_username, _bot_username_fetched
    if _bot_username_fetched:
        return _bot_username
    token = os.getenv("BOT_TOKEN")
    if not token:
        _bot_username_fetched = True
        return _bot_username
        
    async def fetch_username():
        global _bot_username, _bot_username_fetched
        try:
            async with aiohttp.ClientSession(connector=telegram_connector()) as session:
                async with session.get(f"https://api.telegram.org/bot{token}/getMe", timeout=2.0) as response:
                    if response.status == 200:
                        data = await response.json()
                        if data.get("ok"):
                            _bot_username = data["result"]["username"]
                            _bot_username_fetched = True
                            logger.info(f"Fetched bot username: {_bot_username}")
        except Exception as e:
            logger.error(f"Failed to fetch bot username in background: {e}")
            
    asyncio.create_task(fetch_username())
    return _bot_username

app = FastAPI(title="TU UGMK Bot Admin Dashboard")
app.add_middleware(GZipMiddleware, minimum_size=1000)
templates = Jinja2Templates(directory="templates")

ADMIN_DASHBOARD_PASS = os.getenv("ADMIN_DASHBOARD_PASS", "admin_ugmk_pass")

@app.on_event("startup")
async def startup():
    await db_manager.connect()
    await db_manager.init_db()
    await migrate_group_preferences(dao, db_manager)
    logger.info("Admin dashboard database connection established.")

def is_authenticated(request: Request) -> bool:
    cookie_pass = request.cookies.get("admin_session")
    return cookie_pass == ADMIN_DASHBOARD_PASS

@app.get("/", response_class=HTMLResponse)
async def index(request: Request, notification: str = None):
    authenticated = is_authenticated(request)
    
    if not authenticated:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "authenticated": False,
                "error": None
            }
        )
        
    try:
        users = await db_manager.get_all_users()
        openrouter_key = await db_manager.get_setting("openrouter_api_key") or ""
        
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "authenticated": True,
                "users": users,
                "notification": notification,
                "openrouter_key": openrouter_key,
            }
        )
    except Exception as e:
        logger.error(f"Dashboard load error: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/login")
async def login(request: Request, password: str = Form(...)):
    if password == ADMIN_DASHBOARD_PASS:
        response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        response.set_cookie(key="admin_session", value=password, max_age=86400, httponly=True)
        return response
    else:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "authenticated": False,
                "error": "Неверный пароль администратора!"
            }
        )

@app.post("/logout")
async def logout():
    response = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    response.delete_cookie(key="admin_session")
    return response


@app.post("/user/toggle_blacklist")
async def toggle_blacklist(request: Request):
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")
        
    telegram_id = body.get("telegram_id")
    is_blacklisted = body.get("is_blacklisted")
    
    if telegram_id is None or is_blacklisted is None:
        raise HTTPException(status_code=400, detail="Missing telegram_id or is_blacklisted")
        
    try:
        telegram_id = int(telegram_id)
        is_blacklisted = bool(is_blacklisted)
        
        await db_manager.set_user_blacklist(telegram_id, is_blacklisted)
        
        # Update Redis cache instantly
        cache_key = f"user_blacklisted:{telegram_id}"
        await dao.setex(cache_key, 300, "1" if is_blacklisted else "0")
        
        return {"status": "ok", "telegram_id": telegram_id, "is_blacklisted": is_blacklisted}
    except Exception as e:
        logger.error(f"Error toggling blacklist: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/settings/openrouter_keys")
async def update_openrouter_keys(
    request: Request,
    openrouter_key: str = Form(None),
):
    if not is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    try:
        if openrouter_key is not None:
            await db_manager.set_setting("openrouter_api_key", openrouter_key.strip())
        return RedirectResponse(url="/?notification=Настройки OpenRouter успешно обновлены!", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        logger.error(f"Error saving OpenRouter keys: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ═══════════════════ TELEGRAM MINI APP AND APIS ═══════════════════

import redis.asyncio as redis
import json
import hmac
import hashlib
import urllib.parse
import collections
import psutil
from datetime import datetime, timedelta, timezone
from ai_manager import get_ai_response, get_chat_models, normalize_model_id, normalize_chat_image

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
dao = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
ADMIN_IDS = [474095004]

from schedule_config import GROUPS_DB, CACHE_VERSION, canonical_group, active_group, merged_groups, lesson_matches_group, migrate_group_preferences

DEFAULT_AI_MODEL = "openrouter/free"


async def get_catalog_model(model_name: str | None) -> tuple[str, dict | None]:
    """Return a canonical model ID and its catalog metadata, if it is allowed."""
    canonical_model = normalize_model_id(model_name)
    models = await get_chat_models()
    model_by_id = {model["id"]: model for model in models}
    return canonical_model, model_by_id.get(canonical_model)


async def get_valid_user_model(model_name: str | None) -> tuple[str, dict]:
    canonical_model, metadata = await get_catalog_model(model_name)
    if metadata:
        return canonical_model, metadata

    default_model, default_metadata = await get_catalog_model(DEFAULT_AI_MODEL)
    if default_metadata:
        return default_model, default_metadata
    # A provider can remove the default model from the live catalog.  In that
    # case use the first currently available text model rather than accepting a
    # stale ID.
    models = await get_chat_models()
    return models[0]["id"], models[0]

class ScheduleManager:
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None) -> dict:
        if wo not in (0, 1):
            return {}
        tz = timezone(timedelta(hours=5))
        mon = datetime.now(tz).date() - timedelta(days=datetime.now(tz).weekday()) + timedelta(weeks=wo)
        sd = mon.strftime("%d.%m.%Y")
        key = f"data:v{CACHE_VERSION}:{sd}:{t_type}:{t_val}"
        try:
            if await dao.exists(key): return json.loads(await dao.get(key))
        except Exception as e: logger.error(f"Redis get error: {e}")
        if await dao.set(f"queued:{key}", "1", nx=True, ex=120):
            await dao.lpush('schedule_jobs', json.dumps({"week_offset": wo, "target_type": t_type, "target_value": t_val}))
        
        for _ in range(80):
            await asyncio.sleep(0.1)
            try:
                if await dao.exists(key): return json.loads(await dao.get(key))
            except Exception as e: logger.error(f"Redis poll error: {e}")
        return {"_pending": True}

sm = ScheduleManager()

def verify_telegram_init_data(init_data: str) -> dict | None:
    bot_token = os.getenv("BOT_TOKEN")
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        
        # Explicit opt-in for isolated local development only. Never enabled by default.
        test_mode = os.getenv("WEBAPP_TEST_MODE", "").lower() in {"1", "true", "yes"}
        if test_mode and "hash" not in parsed_data and "test_user_id" in parsed_data:
            return {"id": int(parsed_data["test_user_id"]), "username": parsed_data.get("username", "test_user")}
            
        if "hash" not in parsed_data:
            return None
            
        if not bot_token:
            logger.error("BOT_TOKEN environment variable is not set!")
            return None
            
        received_hash = parsed_data.pop("hash")
        data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(parsed_data.items()))
        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
        
        if hmac.compare_digest(calculated_hash, received_hash):
            auth_date = int(parsed_data.get("auth_date", "0"))
            max_age = int(os.getenv("WEBAPP_AUTH_MAX_AGE", "3600"))
            now = int(datetime.now(timezone.utc).timestamp())
            if auth_date <= 0 or abs(now - auth_date) > max_age:
                logger.warning("Rejected expired Telegram WebApp init data")
                return None
            user_str = parsed_data.get("user")
            if user_str:
                return json.loads(user_str)
        return None
    except Exception as e:
        logger.error(f"Error validating Telegram init data: {e}")
        return None

async def get_active_user_row(uid: int):
    user_row = await db_manager.get_user(uid)
    if not user_row:
        return None
        
    if user_row.get('is_blacklisted'):
        raise HTTPException(status_code=403, detail="Вы находитесь в черном списке")
        
    return user_row

async def _build_user_status_dict(uid: int, user_row):
    model, model_metadata = await get_valid_user_model(user_row['ai_model'])
    if model != user_row['ai_model']:
        await db_manager.set_user_ai_model(uid, model)
    has_key = bool(user_row['custom_ai_key'])
    group = await dao.hget("user_subs", str(uid)) or user_row['group_name']
    morn_time = await dao.hget("user_morning_time", str(uid)) or "08:00"
    eve_time = await dao.hget("user_evening_time", str(uid)) or "Отключено"
    bot_name = await get_bot_username()
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    starosta_name = await dao.hget("starosta_name", str(uid)) or "Староста"
    return {
        "telegram_id": uid,
        "ai_model": model,
        "group_name": group,
        "has_custom_key": has_key,
        "can_chat": True,
        "morning_time": morn_time,
        "evening_time": eve_time,
        "bot_username": bot_name,
        "is_starosta": is_starosta,
        "starosta_name": starosta_name,
        "starosta_group": await dao.hget("starosta_group_saved", str(uid)),
        "is_admin": uid in ADMIN_IDS
    }

@app.get("/webapp", response_class=HTMLResponse)
async def webapp(request: Request):
    return templates.TemplateResponse(request=request, name="webapp.html")

@app.post("/api/verify")
async def api_verify(request: Request):
    body = await request.json()
    init_data = body.get("init_data")
    if not init_data:
        raise HTTPException(status_code=400, detail="Missing init_data")
        
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user:
        raise HTTPException(status_code=401, detail="Unauthorized init_data")
        
    uid = tg_user["id"]
    username = tg_user.get("username")
    
    # Load or register user
    user_row = await get_active_user_row(uid)
    if not user_row:
        await db_manager.register_or_update_user(uid, username)
        user_row = await get_active_user_row(uid)
        

    # Retrieve user status
    user_status = await _build_user_status_dict(uid, user_row)
    
    # Pre-fetch current week's schedule if cached
    schedule = {}
    group = user_status["group_name"]
    if group:
        tz = timezone(timedelta(hours=5))
        mon = datetime.now(tz).date() - timedelta(days=datetime.now(tz).weekday())
        sd = mon.strftime("%d.%m.%Y")
        cache_key = f"data:v{CACHE_VERSION}:{sd}:group:{group}"
        try:
            if await dao.exists(cache_key):
                schedule = json.loads(await dao.get(cache_key))
            else:
                # Add to queue in background so it starts parsing, but do NOT block!
                if await dao.set(f"queued:{cache_key}", "1", nx=True, ex=120):
                    await dao.lpush('schedule_jobs', json.dumps({"week_offset": 0, "target_type": "group", "target_value": group}))
        except Exception as e:
            logger.error(f"Failed to check cache in verify: {e}")
            
    return {
        "status": "ok",
        "user": tg_user,
        "user_status": user_status,
        "initial_schedule": schedule
    }

@app.get("/api/user_status")
async def api_user_status(uid: int, init_data: str):
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    user_row = await get_active_user_row(uid)
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
        

    return await _build_user_status_dict(uid, user_row)


@app.get("/api/models")
async def api_models(uid: int, init_data: str):
    """Models are fetched server-side so the OpenRouter key never reaches Telegram clients."""
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"models": await get_chat_models()}


@app.get("/api/groups")
async def api_groups():
    return {"groups": list(merged_groups(await dao.hgetall("db_groups")))}

@app.post("/api/set_group")
async def api_set_group(request: Request):
    body = await request.json()
    uid = body.get("uid")
    group_name = body.get("group_name")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    group_name = canonical_group(group_name)
    if not active_group(group_name) or group_name not in merged_groups(await dao.hgetall("db_groups")):
        raise HTTPException(status_code=400, detail="Invalid group name")
        
    await dao.hset("user_subs", str(uid), group_name)
    await db_manager.register_or_update_user(uid, tg_user.get("username"), group_name)
    return {"status": "ok", "group_name": group_name}

@app.get("/api/schedule")
async def api_schedule(week_offset: int, uid: int, init_data: str, group_name: str = None, target_type: str = "group", target_name: str = None):
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    t_type = target_type
    t_name = target_name if target_name else group_name
    
    if not t_name:
        raise HTTPException(status_code=400, detail="Target name is required")

    if week_offset not in (0, 1):
        raise HTTPException(status_code=400, detail="Invalid week offset")

    if t_type != "group":
        raise HTTPException(status_code=400, detail="Only group schedules are supported")
    t_name = canonical_group(t_name)
    if not active_group(t_name) or (t_name not in GROUPS_DB and not await dao.hexists("db_groups", t_name)):
        raise HTTPException(status_code=400, detail="Unknown schedule target")
        
    # Call ScheduleManager to fetch the schedule (uses Redis queue and cache)
    schedule = await sm.fetch_schedule(week_offset, t_type, t_name)
    return {"schedule": schedule, "group_name": t_name, "week_offset": week_offset}

@app.post("/api/set_model")
async def api_set_model(request: Request):
    body = await request.json()
    uid = body.get("uid")
    model = body.get("model")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    model, metadata = await get_catalog_model(model)
    if not metadata:
        raise HTTPException(status_code=400, detail="Unsupported AI model")
        
    await db_manager.set_user_ai_model(uid, model)
    
    # Clear context history upon model change
    history_key = f"ai_history:{uid}"
    await dao.delete(history_key)
    
    return {"status": "ok", "model": model}

@app.post("/api/clear_context")
async def api_clear_context(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    history_key = f"ai_history:{uid}"
    await dao.delete(history_key)
    return {"status": "ok"}

@app.get("/api/ai_history")
async def api_ai_history(uid: int, init_data: str):
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    history_key = f"ai_history:{uid}"
    history_str = await dao.get(history_key)
    if history_str:
        try:
            return {"history": json.loads(history_str)}
        except Exception:
            pass
    return {"history": []}

@app.get("/api/request_history")
async def api_request_history(uid: int, init_data: str):
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    requests = await db_manager.get_user_ai_requests(uid)
    serialized = []
    for r in requests:
        serialized.append({
            "id": r["id"],
            "prompt": r["prompt"],
            "response": r["response"],
            "model_used": r["model_used"],
            "created_at": r["created_at"].isoformat() if r["created_at"] else None
        })
    return {"requests": serialized}

@app.post("/api/ai_chat")
async def api_ai_chat(request: Request):
    if len(await request.body()) > 7_100_000:
        raise HTTPException(status_code=413, detail="Фото слишком большое. Максимум — 5 МБ.")
    body = await request.json()
    uid = body.get("uid")
    prompt = body.get("prompt")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")

    image_data = body.get("image")
    if image_data is not None:
        try:
            image_data = await asyncio.to_thread(normalize_chat_image, image_data)
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error))
    if prompt is None and image_data:
        prompt = ""
    if not isinstance(prompt, str) or (not prompt.strip() and not image_data):
        raise HTTPException(status_code=400, detail="Prompt is required")
    prompt = prompt.strip() or "Разбери изображение и помоги с заданием."
    if len(prompt) > 20_000:
        raise HTTPException(status_code=400, detail="Prompt is too long")
        
    user_row = await get_active_user_row(uid)
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
        
    model_name, model_metadata = await get_valid_user_model(user_row['ai_model'])
    if image_data and not model_metadata["supports_images"]:
        raise HTTPException(status_code=400, detail="Для фотографии выберите модель с отметкой «Фото».")
    if model_name != user_row['ai_model']:
        await db_manager.set_user_ai_model(uid, model_name)
    api_key = user_row['custom_ai_key']
    
    has_custom_key = bool(api_key)
    history_key = f"ai_history:{uid}"
    history = []
    history_str = await dao.get(history_key)
    if history_str:
        try:
            history = json.loads(history_str)
        except Exception:
            history = []

    try:
        response_text = await get_ai_response(
            prompt=prompt,
            api_key=api_key,
            model_name=model_name,
            history=history,
            image_data_b64=image_data,
        )
        
        # Log request
        await db_manager.log_ai_request(
            telegram_id=uid,
            prompt=f"[Фото] {prompt}" if image_data else prompt,
            response=response_text,
            model_used=model_name
        )
        
        # Append to history
        content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data}"}}] if image_data else prompt
        history.append({"role": "user", "content": content})
        history.append({"role": "assistant", "content": response_text})
        history = history[-10:]
        await dao.setex(history_key, 604800, json.dumps(history, ensure_ascii=False))
        
        return {"status": "ok", "response": response_text}
        
    except Exception as e:
        logger.error(f"AI response failed: {e}")
        err_msg = str(e).lower()
        if isinstance(e, AIBusyError):
            raise HTTPException(status_code=503, detail=str(e), headers={"Retry-After": "10"})
        if getattr(e, "status_code", None) == 429 or "429" in err_msg or "rate limit" in err_msg:
            raise HTTPException(status_code=429, detail="Модель временно занята или достигнут лимит OpenRouter. Подождите или выберите другую модель.")
        if has_custom_key and any(x in err_msg for x in ["401", "unauthorized", "invalid key"]):
            await db_manager.set_user_ai_key(uid, None)
            raise HTTPException(status_code=401, detail="Личный ключ OpenRouter недействителен. Повторите запрос с общим ключом бота.")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/search")
async def api_search(q: str, type: str):
    q_lower = q.strip().lower()
    
    if type == "group":
        db = merged_groups()
        try:
            redis_db = await dao.hgetall("db_groups")
            if redis_db: db = merged_groups(redis_db)
        except Exception as e: logger.error(f"Error fetching groups from Redis: {e}")
        matches = [name for name in db.keys() if q_lower in name.lower()]
        return {"results": matches[:20]}
        
    return {"results": []}

@app.post("/api/set_notifications")
async def api_set_notifications(request: Request):
    body = await request.json()
    uid = body.get("uid")
    morning_time = body.get("morning_time")
    evening_time = body.get("evening_time")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if morning_time:
        await dao.hset("user_morning_time", str(uid), morning_time)
    if evening_time:
        await dao.hset("user_evening_time", str(uid), evening_time)
        
    return {"status": "ok"}

@app.get("/api/ecosystem")
async def api_ecosystem(uid: int, init_data: str):
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    events = await db_manager.get_events()
    channels = await db_manager.get_channels()
    
    serialized_events = []
    for ev in events:
        serialized_events.append({
            "id": ev["id"],
            "title": ev["title"],
            "description": ev["description"],
            "event_date": ev["event_date"].isoformat() if ev["event_date"] else None,
            "link": ev["link"]
        })
        
    serialized_channels = []
    for ch in channels:
        serialized_channels.append({
            "id": ch["id"],
            "name": ch["name"],
            "category": ch["category"] if "category" in dict(ch) else "",
            "link": ch["link"]
        })
        
    return {"events": serialized_events, "channels": serialized_channels}

# --- STAROSTA API ENDPOINTS ---
from datetime import datetime

async def send_telegram_message(token: str, chat_id: int, text: str):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML"
    }
    try:
        async with aiohttp.ClientSession(connector=telegram_connector()) as session:
            async with session.post(url, json=payload) as response:
                return response.status == 200
    except Exception as e:
        logger.error(f"Failed to send Telegram message to {chat_id}: {e}")
        return False

@app.post("/api/starosta/add_event")
async def api_starosta_add_event(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    title = body.get("title")
    description = body.get("description")
    event_date_str = body.get("event_date")
    link = body.get("link")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    if not is_starosta:
        raise HTTPException(status_code=403, detail="Forbidden: Not a starosta")

    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=400, detail="Название мероприятия обязательно")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=400, detail="Описание мероприятия обязательно")
    title = title.strip()
    description = description.strip()
    if len(title) > 255:
        raise HTTPException(status_code=400, detail="Название мероприятия слишком длинное")
        
    event_date = None
    if event_date_str:
        try:
            event_date = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
        except ValueError:
            pass
            
    event_id = await db_manager.add_event(title, description, event_date, link)
    return {"status": "ok", "event_id": event_id}

@app.post("/api/starosta/broadcast")
async def api_starosta_broadcast(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    text = body.get("text")
    target = body.get("target")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    if not is_starosta:
        raise HTTPException(status_code=403, detail="Forbidden: Not a starosta")

    if not isinstance(text, str) or not text.strip():
        raise HTTPException(status_code=400, detail="Текст рассылки обязателен")
    text = text.strip()
    if len(text) > 3500:
        raise HTTPException(status_code=400, detail="Текст рассылки слишком длинный")
    if target not in {"group", "all"}:
        raise HTTPException(status_code=400, detail="Неизвестный получатель рассылки")
        
    starosta_name = await dao.hget("starosta_name", str(uid)) or "Староста"
    starosta_group = await dao.hget("starosta_group_saved", str(uid))
    
    subs = await dao.hgetall("user_subs")
    if target == "group":
        if not starosta_group:
            raise HTTPException(status_code=400, detail="Starosta group not configured")
        target_users = [user_id for user_id, gid in subs.items() if gid == starosta_group]
    else:
        target_users = list(subs.keys())
        
    if not target_users:
        return {"status": "ok", "delivered": 0, "total": 0}
        
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="Bot token not configured")
        
    broadcast_text = f"📢 <b>{html.escape(str(starosta_name))}:</b>\n\n{html.escape(str(text or ''))}"
    
    async def run_broadcast_task():
        success = 0
        for target_uid in target_users:
            ok = await send_telegram_message(token, int(target_uid), broadcast_text)
            if ok:
                success += 1
            await asyncio.sleep(0.05)
        logger.info(f"Starosta broadcast by {uid} finished. Delivered to {success}/{len(target_users)}")
        
    asyncio.create_task(run_broadcast_task())
    return {"status": "ok", "total": len(target_users)}

@app.post("/api/starosta/setup")
async def api_starosta_setup(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    password = body.get("password")
    name = body.get("name")
    group = body.get("group")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    uid_str = str(uid)
    custom_pass = await dao.hget("starosta_pass", uid_str)
    correct_pass = custom_pass if custom_pass else os.getenv("STAROSTA_PASS", "ugmk2026")
    
    if password != correct_pass:
        raise HTTPException(status_code=403, detail="Неверный пароль старосты")
        
    group = canonical_group(group)
    if not active_group(group) or group not in merged_groups(await dao.hgetall("db_groups")):
        raise HTTPException(status_code=400, detail="Выберите действующую учебную группу")
    if name:
        await dao.hset("starosta_name", uid_str, name)
    if group:
        await dao.hset("starosta_group_saved", uid_str, group)
        
    return {"status": "ok", "message": "Статус старосты успешно активирован"}

@app.post("/api/starosta/logout")
async def api_starosta_logout(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    uid_str = str(uid)
    await dao.hdel("starosta_group_saved", uid_str)
    await dao.hdel("starosta_name", uid_str)
    
    return {"status": "ok", "message": "Вы вышли из режима старосты"}

@app.post("/api/starosta/change_password")
async def api_starosta_change_password(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    new_password = body.get("new_password")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    if not is_starosta:
        raise HTTPException(status_code=403, detail="Forbidden: Not a starosta")
        
    if not new_password or len(new_password.strip()) < 3:
        raise HTTPException(status_code=400, detail="Пароль должен состоять минимум из 3 символов")
        
    await dao.hset("starosta_pass", str(uid), new_password.strip())
    return {"status": "ok", "message": "Пароль старосты успешно изменен"}

@app.post("/api/starosta/delete_event")
async def api_starosta_delete_event(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    event_id = body.get("event_id")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    if not is_starosta:
        raise HTTPException(status_code=403, detail="Forbidden: Not a starosta")
        
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event_id")
        
    await db_manager.delete_event(int(event_id))
    return {"status": "ok", "message": "Мероприятие удалено"}

@app.post("/api/starosta/update_event")
async def api_starosta_update_event(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    event_id = body.get("event_id")
    title = body.get("title")
    description = body.get("description")
    event_date_str = body.get("event_date")
    link = body.get("link")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    if not is_starosta:
        raise HTTPException(status_code=403, detail="Forbidden: Not a starosta")
        
    if not event_id:
        raise HTTPException(status_code=400, detail="Missing event_id")

    if not isinstance(title, str) or not title.strip():
        raise HTTPException(status_code=400, detail="Название мероприятия обязательно")
    if not isinstance(description, str) or not description.strip():
        raise HTTPException(status_code=400, detail="Описание мероприятия обязательно")
    title = title.strip()
    description = description.strip()
    if len(title) > 255:
        raise HTTPException(status_code=400, detail="Название мероприятия слишком длинное")
        
    event_date = None
    if event_date_str:
        try:
            event_date = datetime.fromisoformat(event_date_str.replace("Z", "+00:00"))
        except ValueError:
            pass
            
    await db_manager.update_event(int(event_id), title, description, event_date, link)
    return {"status": "ok", "message": "Мероприятие обновлено"}

# --- ADMIN API ENDPOINTS ---
@app.post("/api/admin/status")
async def api_admin_status(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid or uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Forbidden: Not an admin")
        
    cpu = psutil.cpu_percent()
    ram = psutil.virtual_memory().percent
    try:
        redis_ping = await dao.ping()
        redis_status = "✅ Работает" if redis_ping else "❌ Сбой"
    except Exception:
        redis_status = "❌ Сбой"
        
    workers = await dao.llen('schedule_jobs')
    
    return {
        "status": "ok",
        "cpu": cpu,
        "ram": ram,
        "redis_status": redis_status,
        "cache_version": CACHE_VERSION,
        "workers": workers
    }

@app.post("/api/admin/detailed_stats")
async def api_admin_detailed_stats(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid or uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Forbidden: Not an admin")
        
    users = list(await dao.smembers("bot_users"))
    total_users = len(users)
    
    subs = await dao.hgetall("user_subs")
    subbed_users = len(subs)
    
    group_counts = collections.Counter(subs.values())
    top_groups = [{"name": grp, "count": count} for grp, count in group_counts.most_common(10)]
        
    morn_times = await dao.hgetall("user_morning_time")
    morn_counts = collections.Counter(morn_times.values())
    top_morning = [{"time": t, "count": count} for t, count in morn_counts.most_common(5)]
    
    db_g_size = await dao.hlen("db_groups")
    
    return {
        "status": "ok",
        "total_users": total_users,
        "subbed_users": subbed_users,
        "top_groups": top_groups,
        "top_morning": top_morning,
        "db_sizes": {
            "groups": db_g_size
        }
    }

@app.post("/api/admin/server_time")
async def api_admin_server_time(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid or uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Forbidden: Not an admin")
        
    tz = timezone(timedelta(hours=5))
    now = datetime.now(tz)
    return {
        "status": "ok",
        "server_time": now.strftime('%Y-%m-%d %H:%M:%S')
    }

@app.post("/api/admin/broadcast")
async def api_admin_broadcast(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    text = body.get("text")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid or uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Forbidden: Not an admin")
        
    if not text:
        raise HTTPException(status_code=400, detail="Missing text")
        
    users = list(await dao.smembers("bot_users"))
    if not users:
        return {"status": "ok", "total": 0}
        
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="Bot token not configured")
        
    async def run_broadcast_task():
        success = 0
        for target_uid in users:
            ok = await send_telegram_message(token, int(target_uid), text)
            if ok:
                success += 1
            await asyncio.sleep(0.05)
        logger.info(f"Admin broadcast by {uid} finished. Delivered to {success}/{len(users)}")
        
    asyncio.create_task(run_broadcast_task())
    return {"status": "ok", "total": len(users)}

@app.post("/api/admin/trigger_command")
async def api_admin_trigger_command(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    command = body.get("command")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid or uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Forbidden: Not an admin")
        
    if command not in ["force_broadcast", "delayed_broadcast", "test_schedule_broadcast", "preload_cache"]:
        raise HTTPException(status_code=400, detail="Invalid command")
        
    # Queue the command in Redis for bot.py to handle
    payload = {
        "command": command,
        "admin_id": uid
    }
    await dao.rpush("admin_bot_commands", json.dumps(payload))
    return {"status": "ok", "message": "Команда успешно отправлена на выполнение боту"}

@app.post("/api/admin/update")
async def api_admin_update(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid or uid not in ADMIN_IDS:
        raise HTTPException(status_code=403, detail="Forbidden: Not an admin")
        
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise HTTPException(status_code=500, detail="Bot token not configured")
        
    async def run_update_sequence():
        await dao.set("update_in_progress", "1")
        await dao.set("update_admin_id", str(uid))
        await dao.delete("update_msgs")
        
        maintenance_msg = "⚙️ <b>Внимание!</b>\nСервер обслуживается. Бот будет недоступен несколько минут."
        users = list(await dao.smembers("bot_users"))
        
        success_msgs = {}
        for target_uid in users:
            try:
                url = f"https://api.telegram.org/bot{token}/sendMessage"
                payload = {"chat_id": int(target_uid), "text": maintenance_msg, "parse_mode": "HTML"}
                async with aiohttp.ClientSession(connector=telegram_connector()) as session:
                    async with session.post(url, json=payload) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            msg_id = data.get("result", {}).get("message_id")
                            if msg_id:
                                success_msgs[str(target_uid)] = str(msg_id)
            except Exception:
                pass
            await asyncio.sleep(0.02)
            
        if success_msgs:
            await dao.hset("update_msgs", mapping=success_msgs)
            
        await dao.set("bot_update_trigger", "1")
        
    asyncio.create_task(run_update_sequence())
    return {"status": "ok", "message": "Процесс обновления запущен"}
