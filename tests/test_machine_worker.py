import unittest
from finance_service.machine_worker import job_context, next_run
from economic_machine.values import MachineError

class MachineWorkerContextTests(unittest.TestCase):
    def test_hash_starting_with_digit_is_valid_scoped_worker_context(self):
        at='2026-09-29T09:00:00+00:00'
        scope=dict(tenant_id='test',owner_id='owner',wallet='41'+'1'*40,network='tron-nile')
        context=job_context({'scope':scope,'job_id':'9'+'a'*63},lambda:at)
        self.assertEqual(context.authorize('2026-09-29T09:00:30Z'),scope)
        with self.assertRaises(MachineError):context.authorize('2026-09-29T09:05:00Z')
    def test_daily_schedule_advances_in_user_timezone(self):
        self.assertEqual(next_run('2026-09-29T00:30:00+00:00','09:00','Asia/Seoul'),'2026-09-30T00:00:00+00:00')
