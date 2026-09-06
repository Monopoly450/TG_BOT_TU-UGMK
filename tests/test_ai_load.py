import unittest
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from ai_load import AIBusyError, RequestGate, retry_delay, run_completion


class RateError(Exception):
    status_code = 429
    def __init__(self, retry='0'):
        self.response = SimpleNamespace(headers={'Retry-After': retry})


class RetryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        @asynccontextmanager
        async def slot(*args): yield
        self.gate = SimpleNamespace(slot=slot, cooldown=AsyncMock())
        self.patch = patch('ai_load.get_gate', return_value=self.gate)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    async def test_429_retries_keep_model_and_respect_retry_after(self):
        create = AsyncMock(side_effect=[RateError('2'), 'answer'])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with patch('ai_load.asyncio.sleep', AsyncMock()) as sleep:
            self.assertEqual(await run_completion(client, 'test-key', True, model='selected:free'), 'answer')
        sleep.assert_awaited_once_with(2)
        self.gate.cooldown.assert_awaited_once_with('test-key', 2, True)
        self.assertEqual([c.kwargs['model'] for c in create.call_args_list], ['selected:free']*2)

    async def test_repeated_429_is_bounded_and_long_limits_do_not_retry(self):
        for delay, attempts in [('0', 3), ('3600', 1)]:
            create = AsyncMock(side_effect=RateError(delay))
            client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
            with self.assertRaises(AIBusyError):
                await run_completion(client, 'test-key', True, model='selected:free')
            self.assertEqual(create.await_count, attempts)

    async def test_other_errors_do_not_duplicate_requests(self):
        create = AsyncMock(side_effect=ValueError('bad request'))
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        with self.assertRaisesRegex(ValueError, 'bad request'):
            await run_completion(client, 'test-key', False, model='selected')
        create.assert_awaited_once()

    async def test_slot_released_when_request_fails(self):
        redis = SimpleNamespace(eval=AsyncMock(return_value=0), zrem=AsyncMock())
        gate = RequestGate(redis)
        with self.assertRaisesRegex(ValueError, 'failed'):
            async with gate.slot('secret-test-key', False):
                raise ValueError('failed')
        redis.zrem.assert_awaited_once()
        self.assertNotIn('secret-test-key', redis.zrem.call_args.args[0])

    async def test_queue_wait_is_bounded(self):
        redis = SimpleNamespace(eval=AsyncMock(return_value=10000), zrem=AsyncMock())
        with self.assertRaises(AIBusyError):
            async with RequestGate(redis, queue_timeout=.01).slot('key', False):
                self.fail('No slot should have been admitted')
        redis.zrem.assert_not_called()


class DelayTests(unittest.TestCase):
    def test_retry_after_seconds_and_invalid_value(self):
        self.assertEqual(retry_delay(RateError('12'),0),12)
        self.assertEqual(retry_delay(RateError('invalid'),1),4)
