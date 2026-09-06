import os
import logging
import asyncio
import aiohttp
import base64
import io
import math
from PIL import Image, ImageOps, UnidentifiedImageError
from openai import AsyncOpenAI
from ai_load import run_completion

logger = logging.getLogger("ai_manager")

from db_manager import db_manager

# Read global OpenRouter key from environment
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY")

# The catalog is deliberately kept in process memory: it is public metadata and
# must not add a network request to every chat message.  A restart simply causes
# the next request to refresh it again.
MODEL_CATALOG_TTL_SECONDS = max(60, int(os.getenv("OPENROUTER_MODELS_CACHE_TTL", "3600")))
_model_catalog_retry_at = 0.0
_model_catalog_cache: list[dict] = []
_model_catalog_updated_at = 0.0
_model_catalog_lock = None

# The free router remains available when the catalog cannot be reached.
MAX_INPUT_PRICE = 0.30  # USD per million tokens
MAX_OUTPUT_PRICE = 1.50
MAX_IMAGE_PRICE = 0.001  # USD per image
DEFAULT_AI_MODEL = "openrouter/free"
FALLBACK_MODELS = [{
    "id": DEFAULT_AI_MODEL, "name": "Автовыбор · бесплатно", "is_free": True,
    "supports_images": True, "description": "OpenRouter подберёт бесплатную модель для текста или фото",
    "input_price": 0, "output_price": 0,
}]


def normalize_model_id(model_name: str | None) -> str:
    return model_name.strip() if isinstance(model_name, str) and model_name.strip() else DEFAULT_AI_MODEL


def _is_chat_model(model: dict) -> bool:
    architecture = model.get("architecture") or {}
    input_modalities = set(architecture.get("input_modalities") or [])
    output_modalities = set(architecture.get("output_modalities") or [])
    return "text" in input_modalities and output_modalities == {"text"}


def _is_free_model(model: dict) -> bool:
    pricing = model.get("pricing") or {}
    try:
        # Missing mandatory prices, NaN, negative prices and any extra fee fail closed.
        return all(float(pricing[field]) == 0 for field in ("prompt", "completion")) and all(
            float(value) == 0 for value in pricing.values()
        )
    except (KeyError, TypeError, ValueError):
        return False


def select_chat_models(raw_models: list[dict]) -> list[dict]:
    catalog = {}
    for model in raw_models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str) or not model_id or not _is_chat_model(model):
            continue
        if any(word in model_id for word in (":batch", "content-safety", "moderation")):
            continue
        is_free = _is_free_model(model)
        try:
            pricing = {k: float(v) for k, v in model.get("pricing", {}).items()}
            input_price = pricing["prompt"] * 1_000_000
            output_price = pricing["completion"] * 1_000_000
            if not all(math.isfinite(v) and v >= 0 for v in pricing.values()):
                continue
            if input_price > MAX_INPUT_PRICE or output_price > MAX_OUTPUT_PRICE or pricing.get("request", 0) > 0:
                continue
            if pricing.get("image", 0) > MAX_IMAGE_PRICE or pricing.get("internal_reasoning", 0) * 1_000_000 > MAX_OUTPUT_PRICE:
                continue
            if model_id.endswith(":free") and not is_free:
                continue
        except (KeyError, TypeError, ValueError):
            continue
        catalog[model_id] = {
            "id": model_id, "name": model.get("name") or model_id, "is_free": is_free,
            "supports_images": "image" in model["architecture"].get("input_modalities", []),
            "description": ("Текст и фотографии" if "image" in model["architecture"].get("input_modalities", []) else "Работа с текстом"),
            "input_price": round(input_price, 6), "output_price": round(output_price, 6),
        }
    return sorted(catalog.values(), key=lambda m: (not m["is_free"], m["id"] != DEFAULT_AI_MODEL, m["output_price"], m["name"].casefold()))


def filter_chat_models(models: list[dict], category: str = "all") -> list[dict]:
    return [m for m in models if category == "all"
            or (category == "text" and not m["supports_images"])
            or (category == "photo" and m["supports_images"])
            or (category == "free" and m["is_free"])
            or (category == "cheap" and not m["is_free"])]


def normalize_chat_image(data: str) -> str:
    """Validate an untrusted upload and return a bounded JPEG for both clients."""
    if not isinstance(data, str) or len(data) > 7_000_000:
        raise ValueError("Фото слишком большое. Максимум — 5 МБ.")
    if data.startswith("data:"):
        header, separator, data = data.partition(",")
        if not separator or header not in ("data:image/jpeg;base64", "data:image/png;base64", "data:image/webp;base64"):
            raise ValueError("Выберите фотографию JPEG, PNG или WebP.")
    try:
        raw = base64.b64decode(data, validate=True)
        if not raw or len(raw) > 5 * 1024 * 1024:
            raise ValueError("Фото слишком большое или пустое. Максимум — 5 МБ.")
        with Image.open(io.BytesIO(raw)) as photo:
            if photo.format not in ("JPEG", "PNG", "WEBP") or photo.width * photo.height > 25_000_000:
                raise ValueError("Неподдерживаемый формат или разрешение фото (максимум 25 Мп).")
            photo = ImageOps.exif_transpose(photo)
            photo.thumbnail((1600, 1600))
            output = io.BytesIO()
            photo.convert("RGB").save(output, format="JPEG", quality=82)
            return base64.b64encode(output.getvalue()).decode("ascii")
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as error:
        raise ValueError("Не удалось прочитать фото. Выберите другой файл.") from error


async def get_chat_models(force_refresh: bool = False) -> list[dict]:
    """Refresh free and economical text and vision models from the public catalog hourly."""
    global _model_catalog_cache, _model_catalog_updated_at, _model_catalog_lock, _model_catalog_retry_at
    now = asyncio.get_running_loop().time()
    if not force_refresh and _model_catalog_cache and (now < _model_catalog_retry_at or now - _model_catalog_updated_at < MODEL_CATALOG_TTL_SECONDS):
        return _model_catalog_cache

    if _model_catalog_lock is None:
        _model_catalog_lock = asyncio.Lock()

    async with _model_catalog_lock:
        now = asyncio.get_running_loop().time()
        if not force_refresh and _model_catalog_cache and (now < _model_catalog_retry_at or now - _model_catalog_updated_at < MODEL_CATALOG_TTL_SECONDS):
            return _model_catalog_cache

        headers = {}
        try:
            key = await db_manager.get_setting("openrouter_api_key")
        except Exception:
            key = None
        key = key or OPENROUTER_API_KEY
        if key:
            headers["Authorization"] = f"Bearer {key}"

        try:
            timeout = aiohttp.ClientTimeout(total=12)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(
                    "https://openrouter.ai/api/v1/models",
                    headers=headers,
                    params={"input_modalities": "text", "output_modalities": "text", "sort": "most-popular"},
                ) as response:
                    if response.status != 200:
                        raise RuntimeError(f"OpenRouter returned HTTP {response.status}")
                    payload = await response.json()

            catalog = select_chat_models(payload.get("data", []))

            if not catalog:
                raise RuntimeError("OpenRouter returned no eligible chat models")
            _model_catalog_cache = catalog
            _model_catalog_updated_at = asyncio.get_running_loop().time()
            logger.info("Updated OpenRouter chat model catalog: %d models", len(catalog))
        except Exception as error:
            logger.warning("Unable to refresh OpenRouter model catalog: %s", error)
            _model_catalog_retry_at = asyncio.get_running_loop().time() + 60
            if not _model_catalog_cache:
                _model_catalog_cache = FALLBACK_MODELS.copy()

    return _model_catalog_cache

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

    logger.info("Using %s for model %s", key_source, model_name)

    router_model = normalize_model_id(model_name)

    metadata = next((m for m in await get_chat_models() if m["id"] == router_model), None)
    if not metadata:
        raise ValueError("Модель больше недоступна. Выберите другую модель.")
    supports_vision = metadata["supports_images"]
    if image_data_b64 and not supports_vision:
        raise ValueError("Эта модель работает только с текстом. Выберите модель в разделе «Фото».")

    try:
        # Initialize OpenAI-compatible client pointing to OpenRouter
        client = AsyncOpenAI(
            api_key=key,
            base_url="https://openrouter.ai/api/v1",
            # Retry and concurrency budgets are shared by bot and Mini App.
            max_retries=0,
            timeout=25.0,
        )
        
        # Build chat messages sequence
        messages = [{"role": "system", "content": "Ты учебный помощник студентов ТУ УГМК. Отвечай на русском языке, если пользователь явно не попросил другой язык. Объясняй понятно и по делу."}]
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
        
        response = await run_completion(
            client, key, metadata["is_free"],
            model=router_model,
            messages=messages,
            max_tokens=4096,
            extra_body={"provider": {"sort": "price", "max_price": {"prompt": 0 if metadata["is_free"] else MAX_INPUT_PRICE, "completion": 0 if metadata["is_free"] else MAX_OUTPUT_PRICE, "request": 0, "image": 0 if metadata["is_free"] else MAX_IMAGE_PRICE}}},
            extra_headers={
                "HTTP-Referer": "https://tu-ugmk-bot.ru",
                "X-Title": "TU UGMK Bot"
            }
        )
        if not response or not getattr(response, 'choices', None) or len(response.choices) == 0 or response.choices[0] is None:
            raise ValueError("Модель ИИ временно перегружена или вернула пустой ответ. Пожалуйста, попробуйте другую модель или сделайте запрос позже.")
        content = response.choices[0].message.content
        if not content or not content.strip():
            raise ValueError("Модель вернула пустой ответ. Попробуйте другой запрос или модель.")
        return content

    except Exception as e:
        logger.error(f"OpenRouter API error (model {router_model}): {e}")
        raise e
    finally:
        if 'client' in locals() and callable(getattr(client, 'close', None)):
            await client.close()
