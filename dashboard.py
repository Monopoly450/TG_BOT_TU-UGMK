import os
import re
import base64
import logging
import asyncio
import aiohttp
from fastapi.middleware.gzip import GZipMiddleware
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from db_manager import db_manager
import vpn_manager

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
            async with aiohttp.ClientSession() as session:
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
        active_vpn_count = sum(1 for u in users if u['vpn_enabled'])
        
        async with db_manager.pool.acquire() as conn:
            ai_keys = await conn.fetch(
                "SELECT k.*, u.username FROM ai_keys k LEFT JOIN users u ON u.telegram_id = k.used_by ORDER BY k.id DESC"
            )
            
        unused_keys_count = sum(1 for k in ai_keys if k['used_by'] is None)
        
        openrouter_key = await db_manager.get_setting("openrouter_api_key") or ""
        openrouter_management_key = await db_manager.get_setting("openrouter_management_key") or ""
        
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "authenticated": True,
                "users": users,
                "ai_keys": ai_keys,
                "active_vpn_count": active_vpn_count,
                "unused_keys_count": unused_keys_count,
                "notification": notification,
                "openrouter_key": openrouter_key,
                "openrouter_management_key": openrouter_management_key
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

@app.post("/user/toggle_vpn")
async def toggle_vpn(
    request: Request,
    telegram_id: int = Form(...),
    vpn_enabled: str = Form(None)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    enabled = (vpn_enabled is not None)
    try:
        user_row = await db_manager.get_user(telegram_id)
        if user_row:
            if enabled:
                # Enable VPN
                if not user_row['vpn_key']:
                    # Generate a new config
                    config_text = await vpn_manager.generate_user_vpn_config(user_row['id'])
                    await db_manager.set_user_vpn(telegram_id, enabled=True, key=config_text)
                else:
                    # Reregister peer on server using existing configuration public key and IP
                    try:
                        ip_match = re.search(r'Address\s*=\s*([0-9.]+)', user_row['vpn_key'])
                        priv_key_match = re.search(r'PrivateKey\s*=\s*([a-zA-Z0-9+/=]+)', user_row['vpn_key'])
                        if ip_match and priv_key_match:
                            client_ip = ip_match.group(1)
                            priv_key_b64 = priv_key_match.group(1)
                            
                            from cryptography.hazmat.primitives.asymmetric import x25519
                            from cryptography.hazmat.primitives import serialization
                            
                            priv_bytes = base64.b64decode(priv_key_b64)
                            private_key = x25519.X25519PrivateKey.from_private_bytes(priv_bytes)
                            public_key = private_key.public_key()
                            pub_bytes = public_key.public_bytes(
                                encoding=serialization.Encoding.Raw,
                                format=serialization.PublicFormat.Raw
                            )
                            pub_key_b64 = base64.b64encode(pub_bytes).decode('utf-8')
                            
                            await vpn_manager.register_peer_on_server(pub_key_b64, client_ip)
                    except Exception as pe:
                        logger.error(f"Failed to register peer on VPN server: {pe}")
                    await db_manager.set_user_vpn(telegram_id, enabled=True)
                msg = f"VPN успешно включен для пользователя {telegram_id}"
            else:
                # Disable VPN
                await db_manager.set_user_vpn(telegram_id, enabled=False)
                if user_row['vpn_key'] and vpn_manager.VPN_SSH_HOST:
                    try:
                        priv_key_match = re.search(r'PrivateKey\s*=\s*([a-zA-Z0-9+/=]+)', user_row['vpn_key'])
                        if priv_key_match:
                            priv_key_b64 = priv_key_match.group(1)
                            
                            from cryptography.hazmat.primitives.asymmetric import x25519
                            from cryptography.hazmat.primitives import serialization
                            
                            priv_bytes = base64.b64decode(priv_key_b64)
                            private_key = x25519.X25519PrivateKey.from_private_bytes(priv_bytes)
                            public_key = private_key.public_key()
                            pub_bytes = public_key.public_bytes(
                                encoding=serialization.Encoding.Raw,
                                format=serialization.PublicFormat.Raw
                            )
                            pub_key_b64 = base64.b64encode(pub_bytes).decode('utf-8')
                            
                            import asyncssh
                            async with asyncssh.connect(
                                vpn_manager.VPN_SSH_HOST,
                                username=vpn_manager.VPN_SSH_USER,
                                password=vpn_manager.VPN_SSH_PASSWORD,
                                known_hosts=None
                            ) as conn:
                                await conn.run(f"sudo wg set wg0 peer {pub_key_b64} remove")
                                await conn.run(f"sudo sed -i '/{pub_key_b64}/,+2d' /etc/wireguard/wg0.conf")
                    except Exception as pe:
                        logger.error(f"Failed to remove peer from VPN server: {pe}")
                msg = f"VPN успешно отключен для пользователя {telegram_id}"
                
            return RedirectResponse(url=f"/?notification={msg}", status_code=status.HTTP_303_SEE_OTHER)
        else:
            raise HTTPException(status_code=404, detail="User not found")
    except Exception as e:
        logger.error(f"Error toggling VPN status: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/user/update_balance")
async def update_balance(
    request: Request,
    telegram_id: int = Form(...),
    ai_balance: int = Form(...)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    try:
        user_row = await db_manager.get_user(telegram_id)
        if user_row:
            await db_manager.update_user_subscription(telegram_id, user_row['vpn_enabled'], ai_balance)
            msg = f"Баланс ИИ обновлен для пользователя {telegram_id}"
            return RedirectResponse(url=f"/?notification={msg}", status_code=status.HTTP_303_SEE_OTHER)
        else:
            raise HTTPException(status_code=404, detail="User not found")
    except Exception as e:
        logger.error(f"Error updating AI balance: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/keys/generate")
async def generate_key(
    request: Request,
    request_limit: int = Form(100)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    try:
        key = await db_manager.generate_ai_key(request_limit)
        msg = f"Успешно создан ключ: {key}"
        return RedirectResponse(url=f"/?notification={msg}", status_code=status.HTTP_303_SEE_OTHER)
    except Exception as e:
        logger.error(f"Error generating AI key: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/settings/openrouter_keys")
async def update_openrouter_keys(
    request: Request,
    openrouter_key: str = Form(None),
    openrouter_management_key: str = Form(None)
):
    if not is_authenticated(request):
        return RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
        
    try:
        if openrouter_key is not None:
            await db_manager.set_setting("openrouter_api_key", openrouter_key.strip())
        if openrouter_management_key is not None:
            await db_manager.set_setting("openrouter_management_key", openrouter_management_key.strip())
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
from ai_manager import get_ai_response, create_openrouter_key

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
dao = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)
ADMIN_IDS = [474095004]

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

class ScheduleManager:
    async def fetch_schedule(self, wo=0, t_type=None, t_val=None) -> dict:
        tz = timezone(timedelta(hours=5))
        mon = datetime.now(tz).date() - timedelta(days=datetime.now(tz).weekday()) + timedelta(weeks=wo)
        sd = mon.strftime("%d.%m.%Y")
        key = f"data:v39:{sd}:{t_type}:{t_val}"
        try:
            if await dao.exists(key): return json.loads(await dao.get(key))
        except Exception as e: logger.error(f"Redis get error: {e}")
        await dao.lpush('schedule_jobs', json.dumps({"week_offset": wo, "target_type": t_type, "target_value": t_val}))
        
        for _ in range(80):
            await asyncio.sleep(0.1)
            try:
                if await dao.exists(key): return json.loads(await dao.get(key))
            except Exception as e: logger.error(f"Redis poll error: {e}")
        return {}

sm = ScheduleManager()

def verify_telegram_init_data(init_data: str) -> dict | None:
    bot_token = os.getenv("BOT_TOKEN")
    try:
        parsed_data = dict(urllib.parse.parse_qsl(init_data))
        
        # Bypass for testing/debugging
        if "hash" not in parsed_data and "test_user_id" in parsed_data:
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
        
        if calculated_hash == received_hash:
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
        
    ai_expires_at = user_row.get('ai_expires_at')
    if ai_expires_at and ai_expires_at < datetime.now():
        await db_manager.set_user_ai_key(uid, None)
        async with db_manager.pool.acquire() as conn:
            await conn.execute("UPDATE users SET ai_balance = 0 WHERE telegram_id = $1", uid)
        user_row = await db_manager.get_user(uid)
        logger.info(f"Cleared expired key and balance for user {uid}")
        
    return user_row

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
        
    # Get user status details
    model = user_row['ai_model'] or 'gpt-4o-mini'
    has_key = bool(user_row['custom_ai_key'])
    
    # Auto-create key silently if they don't have one
    if not has_key:
        async def create_key_task(telegram_id: int):
            try:
                ai_key = await create_openrouter_key(limit_usd=0.00, expires_days=30)
                expires_at = datetime.now() + timedelta(days=30)
                await db_manager.set_user_ai_key(telegram_id, ai_key, expires_at)
                logger.info(f"Automatically created free-tier key for user {telegram_id} in background via verify")
            except Exception as e:
                logger.error(f"Failed to auto-create key for user {telegram_id} in background via verify: {e}")
        asyncio.create_task(create_key_task(uid))
            
    # Calculate can_chat for the selected model
    ai_balance = user_row['ai_balance'] or 0
    is_free = model in FREE_MODELS
    is_programmatic = has_key and bool(user_row.get('ai_expires_at'))
    has_real_key = has_key and not is_programmatic
    can_chat = has_real_key or (ai_balance > 0) or is_free
    
    # Retrieve group
    group = await dao.hget("user_subs", str(uid)) or user_row['group_name']
    
    # Retrieve notification settings
    morn_time = await dao.hget("user_morning_time", str(uid)) or "08:00"
    eve_time = await dao.hget("user_evening_time", str(uid)) or "Отключено"
    bot_name = await get_bot_username()
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    starosta_name = await dao.hget("starosta_name", str(uid)) or "Староста"
    
    # Pre-fetch current week's schedule if cached
    schedule = {}
    if group:
        tz = timezone(timedelta(hours=5))
        mon = datetime.now(tz).date() - timedelta(days=datetime.now(tz).weekday())
        sd = mon.strftime("%d.%m.%Y")
        cache_key = f"data:v39:{sd}:group:{group}"
        try:
            if await dao.exists(cache_key):
                schedule = json.loads(await dao.get(cache_key))
            else:
                # Add to queue in background so it starts parsing, but do NOT block!
                await dao.lpush('schedule_jobs', json.dumps({"week_offset": 0, "target_type": "group", "target_value": group}))
        except Exception as e:
            logger.error(f"Failed to check cache in verify: {e}")
            
    return {
        "status": "ok",
        "user": tg_user,
        "user_status": {
            "telegram_id": uid,
            "ai_model": model,
            "ai_balance": ai_balance,
            "vpn_enabled": user_row['vpn_enabled'] or False,
            "vpn_expires_at": user_row['vpn_expires_at'].strftime("%d.%m.%Y") if user_row['vpn_expires_at'] else None,
            "ai_expires_at": user_row['ai_expires_at'].strftime("%d.%m.%Y") if user_row['ai_expires_at'] else None,
            "group_name": group,
            "has_custom_key": has_key,
            "is_programmatic_key": is_programmatic,
            "can_chat": can_chat,
            "vpn_key": user_row['vpn_key'],
            "morning_time": morn_time,
            "evening_time": eve_time,
            "bot_username": bot_name,
            "is_starosta": is_starosta,
            "starosta_name": starosta_name,
            "starosta_group": await dao.hget("starosta_group_saved", str(uid)),
            "is_admin": uid in ADMIN_IDS
        },
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
        
    model = user_row['ai_model'] or 'gpt-4o-mini'
    has_key = bool(user_row['custom_ai_key'])
    
    # Auto-create key silently if they don't have one
    if not has_key:
        async def create_key_task(telegram_id: int):
            try:
                ai_key = await create_openrouter_key(limit_usd=0.00, expires_days=30)
                expires_at = datetime.now() + timedelta(days=30)
                await db_manager.set_user_ai_key(telegram_id, ai_key, expires_at)
                logger.info(f"Automatically created free-tier key for user {telegram_id} in background via API status")
            except Exception as e:
                logger.error(f"Failed to auto-create key for user {telegram_id} in background via API status: {e}")
        asyncio.create_task(create_key_task(uid))
            
    # Calculate can_chat for the selected model
    ai_balance = user_row['ai_balance'] or 0
    is_free = model in FREE_MODELS
    is_programmatic = has_key and bool(user_row.get('ai_expires_at'))
    has_real_key = has_key and not is_programmatic
    can_chat = has_real_key or (ai_balance > 0) or is_free
    
    # Retrieve group
    group = await dao.hget("user_subs", str(uid)) or user_row['group_name']
    
    # Retrieve notification settings
    morn_time = await dao.hget("user_morning_time", str(uid)) or "08:00"
    eve_time = await dao.hget("user_evening_time", str(uid)) or "Отключено"
    bot_name = await get_bot_username()
    is_starosta = bool(await dao.hget("starosta_group_saved", str(uid)))
    starosta_name = await dao.hget("starosta_name", str(uid)) or "Староста"
    
    return {
        "telegram_id": uid,
        "ai_model": model,
        "ai_balance": ai_balance,
        "vpn_enabled": user_row['vpn_enabled'] or False,
        "vpn_expires_at": user_row['vpn_expires_at'].strftime("%d.%m.%Y") if user_row['vpn_expires_at'] else None,
        "ai_expires_at": user_row['ai_expires_at'].strftime("%d.%m.%Y") if user_row['ai_expires_at'] else None,
        "group_name": group,
        "has_custom_key": has_key,
        "is_programmatic_key": is_programmatic,
        "can_chat": can_chat,
        "vpn_key": user_row['vpn_key'],
        "morning_time": morn_time,
        "evening_time": eve_time,
        "bot_username": bot_name,
        "is_starosta": is_starosta,
        "starosta_name": starosta_name,
        "starosta_group": await dao.hget("starosta_group_saved", str(uid)),
        "is_admin": uid in ADMIN_IDS
    }

@app.get("/api/groups")
async def api_groups():
    return {"groups": list(GROUPS_DB.keys())}

@app.post("/api/set_group")
async def api_set_group(request: Request):
    body = await request.json()
    uid = body.get("uid")
    group_name = body.get("group_name")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if group_name not in GROUPS_DB:
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
        
    # Call ScheduleManager to fetch the schedule (uses Redis queue and cache)
    schedule = await sm.fetch_schedule(week_offset, t_type, t_name)
    return {"schedule": schedule}

@app.post("/api/set_model")
async def api_set_model(request: Request):
    body = await request.json()
    uid = body.get("uid")
    model = body.get("model")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
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
    body = await request.json()
    uid = body.get("uid")
    prompt = body.get("prompt")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    user_row = await get_active_user_row(uid)
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
        
    model_name = user_row['ai_model'] or 'gpt-4o-mini'
    api_key = user_row['custom_ai_key']
    
    has_custom_key = bool(api_key)
    is_programmatic_key = has_custom_key and bool(user_row.get('ai_expires_at'))
    is_free = model_name in FREE_MODELS
    is_premium = model_name in PREMIUM_MODELS
    
    # Enforce balance checks for paid models under programmatic keys
    if (not has_custom_key or is_programmatic_key) and not is_free:
        balance = user_row['ai_balance'] or 0
        required_balance = 4 if is_premium else 1
        if balance < required_balance:
            raise HTTPException(status_code=403, detail=f"Недостаточно запросов! Требуется {required_balance} (у вас {balance})")
            
    # Fetch history from Redis
    history_key = f"ai_history:{uid}"
    history = []
    history_str = await dao.get(history_key)
    if history_str:
        try: history = json.loads(history_str)
        except Exception: history = []
        
    try:
        response_text = await get_ai_response(
            prompt=prompt,
            api_key=api_key,
            model_name=model_name,
            history=history
        )
        
        # Log request
        await db_manager.log_ai_request(
            telegram_id=uid,
            prompt=prompt,
            response=response_text,
            model_used=model_name
        )
        
        # Decrement balance if standard/premium
        new_balance = user_row['ai_balance'] or 0
        if not has_custom_key or is_programmatic_key:
            if not is_free:
                deduct_amount = 4 if is_premium else 1
                async with db_manager.pool.acquire() as conn:
                    await conn.execute("UPDATE users SET ai_balance = GREATEST(0, ai_balance - $2) WHERE telegram_id = $1", uid, deduct_amount)
                new_balance = max(0, new_balance - deduct_amount)
                
        # Append to history
        history.append({"role": "user", "content": prompt})
        history.append({"role": "assistant", "content": response_text})
        history = history[-10:]
        await dao.setex(history_key, 604800, json.dumps(history, ensure_ascii=False))
        
        return {"status": "ok", "response": response_text, "new_balance": new_balance}
        
    except Exception as e:
        logger.error(f"AI response failed: {e}")
        err_msg = str(e).lower()
        if has_custom_key and any(x in err_msg for x in ["budget", "limit", "payment", "expired", "402", "403", "401", "unauthorized", "invalid key"]):
            await db_manager.set_user_ai_key(uid, None)
            raise HTTPException(status_code=402, detail="API key limit exceeded or expired.")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/api/vpn_toggle")
async def api_vpn_toggle(request: Request):
    body = await request.json()
    uid = body.get("uid")
    init_data = body.get("init_data")
    
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    user_row = await get_active_user_row(uid)
    if not user_row:
        raise HTTPException(status_code=404, detail="User not found")
        
    enabled = not (user_row['vpn_enabled'] or False)
    
    try:
        user_db_id = user_row['id']
        if enabled:
            # Enable VPN
            if not user_row['vpn_key']:
                # Generate a new config
                config_text = await vpn_manager.generate_user_vpn_config(user_db_id)
                await db_manager.set_user_vpn(uid, enabled=True, key=config_text)
            else:
                # Reregister peer on server using existing configuration
                try:
                    ip_match = re.search(r'Address\s*=\s*([0-9.]+)', user_row['vpn_key'])
                    priv_key_match = re.search(r'PrivateKey\s*=\s*([a-zA-Z0-9+/=]+)', user_row['vpn_key'])
                    if ip_match and priv_key_match:
                        client_ip = ip_match.group(1)
                        priv_key_b64 = priv_key_match.group(1)
                        
                        from cryptography.hazmat.primitives.asymmetric import x25519
                        from cryptography.hazmat.primitives import serialization
                        
                        priv_bytes = base64.b64decode(priv_key_b64)
                        private_key = x25519.X25519PrivateKey.from_private_bytes(priv_bytes)
                        public_key = private_key.public_key()
                        pub_bytes = public_key.public_bytes(
                            encoding=serialization.Encoding.Raw,
                            format=serialization.PublicFormat.Raw
                        )
                        pub_key_b64 = base64.b64encode(pub_bytes).decode('utf-8')
                        await vpn_manager.register_peer_on_server(pub_key_b64, client_ip)
                except Exception as pe:
                    logger.error(f"Failed to register peer on VPN server: {pe}")
                await db_manager.set_user_vpn(uid, enabled=True)
            msg = "VPN успешно включен"
        else:
            # Disable VPN
            await db_manager.set_user_vpn(uid, enabled=False)
            if user_row['vpn_key'] and vpn_manager.VPN_SSH_HOST:
                try:
                    priv_key_match = re.search(r'PrivateKey\s*=\s*([a-zA-Z0-9+/=]+)', user_row['vpn_key'])
                    if priv_key_match:
                        priv_key_b64 = priv_key_match.group(1)
                        priv_bytes = base64.b64decode(priv_key_b64)
                        from cryptography.hazmat.primitives.asymmetric import x25519
                        from cryptography.hazmat.primitives import serialization
                        private_key = x25519.X25519PrivateKey.from_private_bytes(priv_bytes)
                        public_key = private_key.public_key()
                        pub_bytes = public_key.public_bytes(
                            encoding=serialization.Encoding.Raw,
                            format=serialization.PublicFormat.Raw
                        )
                        pub_key_b64 = base64.b64encode(pub_bytes).decode('utf-8')
                        
                        import asyncssh
                        async with asyncssh.connect(
                            vpn_manager.VPN_SSH_HOST,
                            username=vpn_manager.VPN_SSH_USER,
                            password=vpn_manager.VPN_SSH_PASSWORD,
                            known_hosts=None
                        ) as conn:
                            await conn.run(f"sudo wg set wg0 peer {pub_key_b64} remove")
                            await conn.run(f"sudo sed -i '/{pub_key_b64}/,+2d' /etc/wireguard/wg0.conf")
                except Exception as pe:
                    logger.error(f"Failed to remove peer from VPN server: {pe}")
            msg = "VPN успешно отключен"
            
        # Fetch updated status
        updated_user = await get_active_user_row(uid)
        return {
            "status": "ok",
            "msg": msg,
            "vpn_enabled": updated_user['vpn_enabled'] or False,
            "vpn_key": updated_user['vpn_key']
        }
    except Exception as e:
        logger.error(f"Error toggling VPN status in webapp: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/search")
async def api_search(q: str, type: str):
    q_lower = q.strip().lower()
    
    if type == "teacher":
        db = dict(TEACHERS_DB)
        try:
            redis_db = await dao.hgetall("db_teachers")
            if redis_db: db.update(redis_db)
        except Exception as e: logger.error(f"Error fetching teachers from Redis: {e}")
        matches = [name for name in db.keys() if q_lower in name.lower()]
        return {"results": matches[:20]}
        
    elif type == "classroom":
        db = dict(CLASSROOMS_DB)
        try:
            redis_db = await dao.hgetall("db_classrooms")
            if redis_db: db.update(redis_db)
        except Exception as e: logger.error(f"Error fetching classrooms from Redis: {e}")
        matches = [name for name in db.keys() if q_lower in name.lower()]
        return {"results": matches[:20]}
        
    elif type == "group":
        db = dict(GROUPS_DB)
        try:
            redis_db = await dao.hgetall("db_groups")
            if redis_db: db.update(redis_db)
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
        async with aiohttp.ClientSession() as session:
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
        
    broadcast_text = f"📢 <b>{starosta_name}:</b>\n\n{text}"
    
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
        "cache_version": 39,
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
    db_t_size = await dao.hlen("db_teachers")
    db_c_size = await dao.hlen("db_classrooms")
    
    return {
        "status": "ok",
        "total_users": total_users,
        "subbed_users": subbed_users,
        "top_groups": top_groups,
        "top_morning": top_morning,
        "db_sizes": {
            "groups": db_g_size,
            "teachers": db_t_size,
            "classrooms": db_c_size
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
                async with aiohttp.ClientSession() as session:
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
