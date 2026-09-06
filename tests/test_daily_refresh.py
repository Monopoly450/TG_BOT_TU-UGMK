import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from schedule_refresh import enqueue_daily_refresh, store_schedule_result


class RefreshTests(unittest.IsolatedAsyncioTestCase):
    async def test_0800_yekaterinburg_boundary_and_restart_catchup(self):
        client=SimpleNamespace(exists=AsyncMock(return_value=False),hgetall=AsyncMock(return_value={}),eval=AsyncMock(return_value=46))
        before = datetime(2026,9,7,2,59,tzinfo=timezone.utc)
        self.assertEqual(await enqueue_daily_refresh(client,before),0)
        client.eval.assert_not_called()
        at = datetime(2026,9,7,3,0,tzinfo=timezone.utc)
        self.assertEqual(await enqueue_daily_refresh(client,at),46)
        args=client.eval.call_args.args
        self.assertIn('2026-09-07',args[2])
        self.assertTrue(any('07.09.2026:group:Ит-24107' in a for a in args if isinstance(a,str)))
        self.assertTrue(any('14.09.2026:group:Ит-24107' in a for a in args if isinstance(a,str)))
        late = datetime(2026,9,7,10,0,tzinfo=timezone.utc)
        self.assertEqual(await enqueue_daily_refresh(client,late),46)
        client.exists.return_value=True
        self.assertEqual(await enqueue_daily_refresh(client,late),0)

    async def test_failed_refresh_keeps_usable_cached_schedule(self):
        previous={'_group':'Ит-24107','Понедельник':[{'subject':'Алгебра'}]}
        dao=SimpleNamespace(get=AsyncMock(return_value=previous),set=AsyncMock())
        self.assertFalse(await store_schedule_result(dao,'cache',{'_error':'offline'},172800))
        dao.set.assert_awaited_once_with('cache',previous,ex=172800)
        dao.set.reset_mock()
        fresh={'_group':'Ит-24107','Понедельник':[]}
        self.assertTrue(await store_schedule_result(dao,'cache',fresh,172800))
        dao.set.assert_awaited_once_with('cache',fresh,ex=172800)
