import os
import logging
import aiohttp
import base64
from datetime import datetime, timedelta, timezone
from openai import AsyncOpenAI

logger = logging.getLogger("ai_manager")

from db_manager import db_manager

# Read global OpenRouter key from environment
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Mapping friendly model names to OpenRouter specific IDs
MODEL_MAP = {
    # Premium
    "kimi-k2.7-code": "moonshotai/kimi-k2.7-code",
    "claude-opus-4.8": "anthropic/claude-opus-4.8",
    "gpt-4": "openai/gpt-4o",
    "gpt-5.5": "openai/gpt-5.5",
    
    # Standard
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "deepseek-v3.2": "deepseek/deepseek-v3.2",
    "minimax-m2.7": "minimax/minimax-m2.7",
    "glm-5": "z-ai/glm-5",
    
    # Free
    "nemotron-3-ultra-free": "nvidia/nemotron-3-ultra-550b-a55b:free",
    "laguna-xs-2-free": "poolside/laguna-xs.2:free",
    "qwen3-next-free": "qwen/qwen3-next-80b-a3b-instruct:free",
    "gpt-oss-free": "openai/gpt-oss-120b:free",
    "llama-3.3-free": "meta-llama/llama-3.3-70b-instruct:free"
}

async def get_ai_response(prompt: str, api_key: str, model_name: str, history: list, image_data_b64: str = None) -> str:
    """
    Sends a message to OpenRouter with conversation history.
    history parameter is a list of dicts: [{"role": "user"|"assistant", "content": "..."}]
    """
    # Use custom key, then database key, then env key
    key = api_key
    key_source = "user_custom_key"
    if not key:
        key_source = "db_global_key"
        try:
            key = await db_manager.get_setting("openrouter_api_key")
        except Exception:
            key = None
        if not key:
            key_source = "env_global_key"
            key = OPENROUTER_API_KEY
            
    if not key:
        raise ValueError("Ключ API OpenRouter не настроен. Укажите его в панели управления или .env файле.")

    # Log key info for debugging (safe mask)
    masked_key = f"{key[:10]}...{key[-4:]}" if len(key) > 15 else "too_short"
    logger.info(f"Using API key from {key_source}: {masked_key} for model {model_name}")

    # Get mapped OpenRouter model identifier
    router_model = MODEL_MAP.get(model_name, model_name)
    if "/" not in router_model:
        router_model = f"openai/{router_model}"  # default fallback

    supports_vision = any(x in router_model.lower() for x in ["gpt-4o", "gpt-5.5", "claude-opus", "claude-3-opus", "claude-3.5", "vision"])

    try:
        # Initialize OpenAI-compatible client pointing to OpenRouter
        client = AsyncOpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1"
        )
        
        # Build chat messages sequence
        messages = []
        for h in history:
            content = h["content"]
            if isinstance(content, list) and not supports_vision:
                text_parts = [item["text"] for item in content if item.get("type") == "text"]
                content = " ".join(text_parts)
            messages.append({"role": h["role"], "content": content})
            
        if image_data_b64:
            messages.append({
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt if prompt else "Что на изображении?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{image_data_b64}"
                        }
                    }
                ]
            })
        else:
            messages.append({"role": "user", "content": prompt})
        
        response = await client.chat.completions.create(
            model=router_model,
            messages=messages,
            max_tokens=4096,
            extra_headers={
                "HTTP-Referer": "https://tu-ugmk-bot.ru",
                "X-Title": "TU UGMK Bot"
            }
        )
        if not response or not getattr(response, 'choices', None) or len(response.choices) == 0 or response.choices[0] is None:
            raise ValueError("Модель ИИ временно перегружена или вернула пустой ответ. Пожалуйста, попробуйте другую модель или сделайте запрос позже.")
        return response.choices[0].message.content

    except Exception as e:
        logger.error(f"OpenRouter API error (model {router_model}): {e}")
        raise e


async def transcribe_audio(audio_data: bytes, format: str, api_key: str) -> str:
    """
    Sends audio bytes to OpenRouter audio/transcriptions API and returns the text.
    """
    key = api_key
    key_source = "user_custom_key"
    if not key:
        key_source = "db_global_key"
        try:
            key = await db_manager.get_setting("openrouter_api_key")
        except Exception:
            key = None
        if not key:
            key_source = "env_global_key"
            key = OPENROUTER_API_KEY
            
    if not key:
        raise ValueError("Ключ API OpenRouter не настроен. Укажите его в панели управления или .env файле.")

    masked_key = f"{key[:10]}...{key[-4:]}" if len(key) > 15 else "too_short"
    logger.info(f"Using API key for transcription from {key_source}: {masked_key}")

    url = "https://openrouter.ai/api/v1/audio/transcriptions"
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }
    
    b64_data = base64.b64encode(audio_data).decode("utf-8")
    payload = {
        "model": "openai/whisper-large-v3",
        "input_audio": {
            "data": b64_data,
            "format": format
        }
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.error(f"OpenRouter transcription API error: status {resp.status}, response: {text}")
                raise ValueError(f"Ошибка OpenRouter: {text}")
            
            data = await resp.json()
            return data.get("text", "")


async def create_openrouter_key(limit_usd: float, expires_days: int = 30) -> str:
    """
    Creates an OpenRouter API key programmatically using the Management API key.
    """
    mgmt_key = None
    try:
        mgmt_key = await db_manager.get_setting("openrouter_management_key")
    except Exception:
        pass
        
    if not mgmt_key:
        mgmt_key = os.getenv("OPENROUTER_MANAGEMENT_KEY")
        
    if not mgmt_key:
        # Fallback to standard key if management key is not set
        mgmt_key = os.getenv("OPENROUTER_API_KEY")
        if not mgmt_key:
            try:
                mgmt_key = await db_manager.get_setting("openrouter_api_key")
            except Exception:
                pass
                
    if not mgmt_key:
        raise ValueError("Management API Key для OpenRouter не настроен. Пожалуйста, укажите его в настройках панели управления.")
        
    url = "https://openrouter.ai/api/v1/keys"
    headers = {
        "Authorization": f"Bearer {mgmt_key}",
        "Content-Type": "application/json"
    }
    
    expires_at = (datetime.now(timezone.utc) + timedelta(days=expires_days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    name = f"tg_{int(datetime.now(timezone.utc).timestamp())}"
    
    payload = {
        "name": name,
        "expires_at": expires_at,
        "limit": limit_usd
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, json=payload) as resp:
            if resp.status not in (200, 201):
                text = await resp.text()
                logger.error(f"OpenRouter management API error: status {resp.status}, response: {text}")
                raise ValueError(f"Ошибка OpenRouter: {text}")
            
            data = await resp.json()
            api_key = data.get("key")
            if not api_key:
                # Fallback to nested data.key (used in tests)
                key_data = data.get("data")
                if isinstance(key_data, dict):
                    api_key = key_data.get("key")
            
            if not api_key:
                raise ValueError(f"Ключ не найден в ответе OpenRouter: {data}")
            return api_key
