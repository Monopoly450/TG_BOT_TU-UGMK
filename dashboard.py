import os
import re
import base64
import logging
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
        return templates.TemplateResponse("index.html", {
            "request": request,
            "authenticated": False,
            "error": None
        })
        
    try:
        users = await db_manager.get_all_users()
        active_vpn_count = sum(1 for u in users if u['vpn_enabled'])
        
        async with db_manager.pool.acquire() as conn:
            ai_keys = await conn.fetch(
                "SELECT k.*, u.username FROM ai_keys k LEFT JOIN users u ON u.telegram_id = k.used_by ORDER BY k.id DESC"
            )
            
        unused_keys_count = sum(1 for k in ai_keys if k['used_by'] is None)
        
        return templates.TemplateResponse("index.html", {
            "request": request,
            "authenticated": True,
            "users": users,
            "ai_keys": ai_keys,
            "active_vpn_count": active_vpn_count,
            "unused_keys_count": unused_keys_count,
            "notification": notification
        })
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
        return templates.TemplateResponse("index.html", {
            "request": request,
            "authenticated": False,
            "error": "Неверный пароль администратора!"
        })

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
