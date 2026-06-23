import os
import logging
from openai import AsyncOpenAI

logger = logging.getLogger("ai_manager")

from db_manager import db_manager

# Read global OpenRouter key from environment
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Mapping friendly model names to OpenRouter specific IDs
MODEL_MAP = {
    "gemini-1.5-flash": "google/gemini-flash-1.5",
    "gemini-1.5-pro": "google/gemini-pro-1.5",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o",
    "claude-opus-4.8": "anthropic/claude-3-opus",
    "gpt-5.5": "openai/gpt-4o",
    "kimi-k2.7": "moonshot/kimi-k1.5",
    "qwen-3.7-plus": "qwen/qwen-2.5-72b-instruct",
    "nous-hermes-3-free": "nousresearch/hermes-3-llama-3.1-405b:free",
    "gemma-2-free": "google/gemma-2-9b-it:free",
    "llama-3-free": "meta-llama/llama-3-8b-instruct:free"
}

async def get_ai_response(prompt: str, api_key: str, model_name: str, history: list) -> str:
    """
    Sends a message to OpenRouter with conversation history.
    history parameter is a list of dicts: [{"role": "user"|"assistant", "content": "..."}]
    """
    # Use custom key, then database key, then env key
    key = api_key
    if not key:
        try:
            key = await db_manager.get_setting("openrouter_api_key")
        except Exception:
            key = None
        if not key:
            key = OPENROUTER_API_KEY
            
    if not key:
        raise ValueError("Ключ API OpenRouter не настроен. Укажите его в панели управления или .env файле.")

    # Get mapped OpenRouter model identifier
    router_model = MODEL_MAP.get(model_name, model_name)
    if "/" not in router_model:
        router_model = f"openai/{router_model}"  # default fallback

    try:
        # Initialize OpenAI-compatible client pointing to OpenRouter
        client = AsyncOpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1"
        )
        
        # Build chat messages sequence
        messages = []
        for h in history:
            messages.append({"role": h["role"], "content": h["content"]})
        messages.append({"role": "user", "content": prompt})
        
        response = await client.chat.completions.create(
            model=router_model,
            messages=messages,
            extra_headers={
                "HTTP-Referer": "https://tu-ugmk-bot.ru",
                "X-Title": "TU UGMK Bot"
            }
        )
        return response.choices[0].message.content

    except Exception as e:
        logger.error(f"OpenRouter API error (model {router_model}): {e}")
        raise e
