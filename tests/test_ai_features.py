"""Offline regression checks: python -m unittest discover -s tests."""
import base64
import io
import json
import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
import ai_manager as ai
import dashboard as api
import ai_load


def photo():
    output = io.BytesIO()
    Image.new("RGB", (20, 20), "green").save(output, "PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def model(model_id="test/default:free", inputs=None, prompt="0", completion="0"):
    return {"id": model_id, "name": model_id, "pricing": {"prompt": prompt, "completion": completion},
            "architecture": {"input_modalities": inputs or ["text", "image"], "output_modalities": ["text"]}}


class CatalogTests(unittest.TestCase):
    def test_cheap_models_included_and_expensive_excluded(self):
        values = [model(), model("cheap/unknown", prompt="0.00000001"), model("expensive/unknown", prompt="0.01")]
        self.assertEqual([m["id"] for m in ai.select_chat_models(values)], ["test/default:free", "cheap/unknown"])

    def test_paid_price_caps_and_per_request_fees(self):
        allowed = model('cheap/boundary', prompt='0.0000003', completion='0.0000015')
        self.assertEqual(len(ai.select_chat_models([allowed])), 1)
        self.assertEqual(len(ai.filter_chat_models(ai.select_chat_models([allowed]), 'cheap')), 1)
        for field, value in [('prompt', '0.000000301'), ('completion', '0.000001501'), ('request', '0.00001'), ('image', '0.00101')]:
            candidate = model('cheap/reject')
            candidate['pricing'][field] = value
            self.assertEqual(ai.select_chat_models([candidate]), [])

    def test_free_vision_and_text_filters(self):
        values = [model("test/vision:free", prompt="0", completion="0"),
                  model("test/text:free", inputs=["text"], prompt="0", completion="0"), model()]
        catalog = ai.select_chat_models(values)
        self.assertEqual(len(ai.filter_chat_models(catalog, "free")), 3)
        self.assertEqual(len(ai.filter_chat_models(catalog, "photo")), 2)
        self.assertEqual(len(ai.filter_chat_models(catalog, "text")), 1)

    def test_unknown_pricing_not_free_or_allowed(self):
        missing = model("test/missing:free")
        missing["pricing"] = {}
        self.assertFalse(ai._is_free_model(missing))
        self.assertEqual(ai.select_chat_models([missing]), [])

    def test_hidden_fees_and_invalid_prices_are_excluded(self):
        for price in ["NaN", "Infinity", "-1", "0.00001", None]:
            candidate = model()
            candidate["pricing"]["image"] = price
            self.assertEqual(ai.select_chat_models([candidate]), [])

    def test_new_models_are_included_and_duplicates_removed(self):
        candidate = model("new-vendor/new-model:free")
        self.assertEqual(len(ai.select_chat_models([candidate, candidate])), 1)

    def test_generated_images_and_batch_excluded(self):
        generated = model()
        generated["architecture"]["output_modalities"].append("image")
        self.assertEqual(ai.select_chat_models([generated, model("openai/gpt-4o-mini:batch")]), [])

    def test_image_validation_and_jpeg_normalization(self):
        encoded = ai.normalize_chat_image(photo())
        with Image.open(io.BytesIO(base64.b64decode(encoded))) as image:
            self.assertEqual(image.format, "JPEG")
        for invalid in ("not base64", "data:image/svg+xml;base64,PHN2Zz4=", 42, "a" * 7_000_001):
            with self.assertRaises(ValueError):
                ai.normalize_chat_image(invalid)


class CatalogRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_refresh_adds_new_model_and_cache_avoids_network(self):
        response = SimpleNamespace(status=200, json=AsyncMock(return_value={"data": [model("new/model:free")]}))
        response_context = AsyncMock()
        response_context.__aenter__.return_value = response
        session = MagicMock()
        session.get.return_value = response_context
        session_context = AsyncMock()
        session_context.__aenter__.return_value = session
        with patch.object(ai, "_model_catalog_cache", ai.FALLBACK_MODELS.copy()), \
             patch.object(ai, "_model_catalog_updated_at", -ai.MODEL_CATALOG_TTL_SECONDS), \
             patch.object(ai, "_model_catalog_retry_at", 0), \
             patch.object(ai, "_model_catalog_lock", None), \
             patch.object(ai.db_manager, "get_setting", AsyncMock(return_value=None)), \
             patch.object(ai.aiohttp, "ClientSession", return_value=session_context) as client:
            first = await ai.get_chat_models()
            self.assertEqual(first[0]["id"], "new/model:free")
            self.assertEqual(await ai.get_chat_models(), first)
            client.assert_called_once()

    async def test_outage_retains_catalog_and_backs_off(self):
        with patch.object(ai, "_model_catalog_cache", ai.FALLBACK_MODELS.copy()), \
             patch.object(ai, "_model_catalog_updated_at", -ai.MODEL_CATALOG_TTL_SECONDS), \
             patch.object(ai, "_model_catalog_retry_at", 0), \
             patch.object(ai, "_model_catalog_lock", None), \
             patch.object(ai.db_manager, "get_setting", AsyncMock(return_value=None)), \
             patch.object(ai.aiohttp, "ClientSession", side_effect=RuntimeError("offline")) as client:
            self.assertEqual(await ai.get_chat_models(), ai.FALLBACK_MODELS)
            self.assertEqual(await ai.get_chat_models(), ai.FALLBACK_MODELS)
            client.assert_called_once()


class ChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        @asynccontextmanager
        async def slot(*args):
            yield
        self.gate_patch = patch.object(ai_load, 'get_gate', return_value=SimpleNamespace(slot=slot, cooldown=AsyncMock()))
        self.gate_patch.start()
        self.addCleanup(self.gate_patch.stop)

    async def test_vision_payload_and_followup_retain_image(self):
        catalog = ai.select_chat_models([model()])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(
            return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="Ответ"))])))))
        history = [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": photo()}}]}]
        with patch.object(ai, "get_chat_models", AsyncMock(return_value=catalog)), patch.object(ai, "AsyncOpenAI", return_value=client):
            result = await ai.get_ai_response("Вопрос", "test-key", catalog[0]["id"], history, ai.normalize_chat_image(photo()))
        self.assertEqual(result, "Ответ")
        self.assertEqual(client.chat.completions.create.call_args.kwargs["extra_body"]["provider"]["max_price"], {"prompt": 0, "completion": 0, "request": 0, "image": 0})
        messages = client.chat.completions.create.call_args.kwargs["messages"]
        self.assertEqual(messages[0]['role'], 'system')
        self.assertIn('на русском', messages[0]['content'])
        self.assertIsInstance(messages[1]["content"], list)
        self.assertEqual(messages[2]["content"][1]["type"], "image_url")

    async def test_text_model_rejects_photo_before_provider(self):
        catalog = ai.select_chat_models([model("deepseek/deepseek-v3.2", inputs=["text"])])
        with patch.object(ai, "get_chat_models", AsyncMock(return_value=catalog)), patch.object(ai, "AsyncOpenAI") as client:
            with self.assertRaisesRegex(ValueError, "только с текстом"):
                await ai.get_ai_response("Фото", "test-key", catalog[0]["id"], [], "photo")
            client.assert_not_called()

    async def test_api_photo_only_free_no_charge_and_shared_history(self):
        metadata = ai.select_chat_models([model("test/vision:free", prompt="0", completion="0")])[0]
        user = {"ai_model": metadata["id"], "custom_ai_key": None}
        body = {"uid": 1, "prompt": "", "image": photo(), "init_data": "signed"}
        request = SimpleNamespace(body=AsyncMock(return_value=json.dumps(body).encode()), json=AsyncMock(return_value=body))
        dao = SimpleNamespace(get=AsyncMock(return_value=None), setex=AsyncMock())
        db = SimpleNamespace(log_ai_request=AsyncMock(), pool=None)
        with patch.object(api, "verify_telegram_init_data", return_value={"id": 1}), \
             patch.object(api, "get_active_user_row", AsyncMock(return_value=user)), \
             patch.object(api, "get_valid_user_model", AsyncMock(return_value=(metadata["id"], metadata))), \
             patch.object(api, "get_ai_response", AsyncMock(return_value="На фото задание")) as answer, \
             patch.object(api, "dao", dao), patch.object(api, "db_manager", db):
            result = await api.api_ai_chat(request)
        self.assertEqual(result, {"status": "ok", "response": "На фото задание"})
        self.assertTrue(answer.call_args.kwargs["image_data_b64"])
        history = json.loads(dao.setex.call_args.args[2])
        self.assertEqual(history[0]["content"][1]["type"], "image_url")
        self.assertEqual(history[1]["content"], "На фото задание")

    async def test_unavailable_model_is_replaced(self):
        catalog = ai.FALLBACK_MODELS
        with patch.object(api, "get_chat_models", AsyncMock(return_value=catalog)):
            selected, metadata = await api.get_valid_user_model("removed/old-model")
        self.assertEqual(selected, "openrouter/free")
        self.assertTrue(metadata["is_free"])

    async def test_schedule_only_accepts_current_and_next_week(self):
        with patch.object(api, "verify_telegram_init_data", return_value={"id": 1}), \
             patch.object(api.sm, "fetch_schedule", AsyncMock(return_value={})) as fetch:
            for offset in [-1, -52, 2, 100]:
                with self.assertRaises(api.HTTPException) as error:
                    await api.api_schedule(offset, 1, "signed", group_name="Ит-25107")
                self.assertEqual(error.exception.status_code, 400)
            fetch.assert_not_called()
            for offset in [0, 1]:
                await api.api_schedule(offset, 1, "signed", group_name="Ит-25107")
            self.assertEqual(fetch.await_count, 2)

    async def test_set_model_rejects_model_outside_catalog(self):
        body = {"uid": 1, "model": "expensive/model", "init_data": "signed"}
        request = SimpleNamespace(json=AsyncMock(return_value=body))
        with patch.object(api, "verify_telegram_init_data", return_value={"id": 1}), \
             patch.object(api, "get_catalog_model", AsyncMock(return_value=("expensive/model", None))), \
             patch.object(api.db_manager, "set_user_ai_model", AsyncMock()) as save:
            with self.assertRaises(api.HTTPException) as error:
                await api.api_set_model(request)
            self.assertEqual(error.exception.status_code, 400)
            save.assert_not_called()

    async def test_api_rejects_unauthorized_before_processing_photo(self):
        body = {"uid": 1, "image": photo()}
        request = SimpleNamespace(body=AsyncMock(return_value=b"{}"), json=AsyncMock(return_value=body))
        with patch.object(api, "verify_telegram_init_data", return_value=None), patch.object(api, "normalize_chat_image") as normalize:
            with self.assertRaises(api.HTTPException) as error:
                await api.api_ai_chat(request)
        self.assertEqual(error.exception.status_code, 401)
        normalize.assert_not_called()


if __name__ == "__main__":
    unittest.main()
