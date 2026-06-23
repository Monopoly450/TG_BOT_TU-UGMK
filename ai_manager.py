import os
import logging
from openai import AsyncOpenAI

logger = logging.getLogger("ai_manager")

# Read global OpenRouter key from environment
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# Mapping friendly model names to OpenRouter specific IDs
MODEL_MAP = {
    "gemini-1.5-flash": "google/gemini-flash-1.5",
    "gemini-1.5-pro": "google/gemini-pro-1.5",
    "gpt-4o-mini": "openai/gpt-4o-mini",
    "gpt-4o": "openai/gpt-4o"
}

async def get_ai_response(prompt: str, api_key: str, model_name: str, history: list) -> str:
    """
    Sends a message to OpenRouter with conversation history.
    history parameter is a list of dicts: [{"role": "user"|"assistant", "content": "..."}]
    """
    # Use global OpenRouter key if no custom user key is provided
    key = api_key if api_key else OPENROUTER_API_KEY
    if not key:
        raise ValueError("Ключ API OpenRouter не настроен в файле .env.")

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
