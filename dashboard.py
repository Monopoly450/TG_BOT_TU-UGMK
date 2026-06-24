import os
import re
import base64
import logging
import asyncio
from fastapi import FastAPI, Request, Form, Depends, HTTPException, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from db_manager import db_manager
import vpn_manager

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("dashboard")

app = FastAPI(title="TU UGMK Bot Admin Dashboard")
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
from datetime import datetime, timedelta, timezone
from ai_manager import get_ai_response, create_openrouter_key

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
dao = redis.Redis(host=REDIS_HOST, port=6379, decode_responses=True)

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
        
        for _ in range(600):
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
        
    return {"status": "ok", "user": tg_user}

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
        try:
            ai_key = await create_openrouter_key(limit_usd=0.00, expires_days=30)
            expires_at = datetime.now() + timedelta(days=30)
            await db_manager.set_user_ai_key(uid, ai_key, expires_at)
            has_key = True
            user_row = await get_active_user_row(uid)
            logger.info(f"Automatically created free-tier key for user {uid} via API status")
        except Exception as e:
            logger.error(f"Failed to auto-create key for user {uid} via API status: {e}")
            
    # Calculate can_chat for the selected model
    ai_balance = user_row['ai_balance'] or 0
    is_free = model in FREE_MODELS
    is_programmatic = has_key and bool(user_row.get('ai_expires_at'))
    has_real_key = has_key and not is_programmatic
    can_chat = has_real_key or (ai_balance > 0) or is_free
    
    # Retrieve group
    group = await dao.hget("user_subs", str(uid)) or user_row['group_name']
    
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
        "vpn_key": user_row['vpn_key']
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
async def api_schedule(group_name: str, week_offset: int, uid: int, init_data: str):
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user or tg_user["id"] != uid:
        raise HTTPException(status_code=401, detail="Unauthorized")
        
    if group_name not in GROUPS_DB:
        raise HTTPException(status_code=400, detail="Invalid group name")
        
    # Call ScheduleManager to fetch the schedule (uses Redis queue and cache)
    schedule = await sm.fetch_schedule(week_offset, "group", group_name)
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
