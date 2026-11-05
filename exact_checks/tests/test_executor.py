"""Real local subprocess checks; fault injection exists only in this test file."""
from copy import deepcopy
import json
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

from exact_checks.executor import IsolatedExecutor, ExecutionFailure
from exact_checks.operations import ExactOperation, all_operations, source_fingerprint
from exact_checks.tests import support as s

REAL_POPEN = subprocess.Popen


def fixture_process(code):
    """Replace only the supervisor's fixed program with a local test fixture."""
    def spawn(_command, **kwargs):
        return REAL_POPEN([sys.executable, "-B", "-u", "-c", code], **kwargs)
    return spawn


class IsolatedExecution(unittest.TestCase):
    def request(self):
        inp, result = s.base(13, 'isolated-exact')
        request = s.args('m06.pi_interval_membership', point=s.pi(1),
                         interval=s.pi_interval(0, 2), expected_member=True)
        return inp, result, request

    def test_real_worker_positive_fact_and_reuse(self):
        operations = all_operations()
        op = next(x for x in operations if x.id == 'm06.pi_interval_membership')
        inp, result, request = self.request()
        try:
            op.prepare_execution(2000)
            pid = op._executor.process.pid
            fact = op.execute_bounded(request, result, 250)
            self.assertEqual(fact['status'], 'supported')
            self.assertEqual(s.m01.validate(s.attach(result, fact), inp), [])
            second = next(x for x in operations if x.id == 'm07.set_equal')
            self.assertIs(op._executor, second._executor)
            second.prepare_execution(2000)
            self.assertEqual(second._executor.process.pid, pid)
        finally:
            for item in operations:
                item.close()

    def test_actual_stuck_process_is_killed_and_not_retried(self):
        fingerprint = source_fingerprint()
        fixture = ("import json,sys,time\n"
                   f"print(json.dumps(dict(ready=True,source_sha256={fingerprint!r})),flush=True)\n"
                   "sys.stdin.readline()\n"
                   "time.sleep(10)\n")
        executor = IsolatedExecutor()
        with patch('exact_checks.executor.subprocess.Popen', side_effect=fixture_process(fixture)):
            executor.prepare(2000, fingerprint)
        child = executor.process
        _, result, request = self.request()
        started = time.monotonic()
        with self.assertRaises(ExecutionFailure) as caught:
            executor.execute(request['operation'], request, result, 60, fingerprint)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertLess(time.monotonic() - started, 1)
        self.assertIsNotNone(child.poll())
        self.assertIsNone(executor.process)
        with self.assertRaises(ExecutionFailure) as caught:
            executor.execute(request['operation'], request, result, 250, fingerprint)
        self.assertEqual(caught.exception.code, 'worker_unavailable')

    def test_real_worker_refuses_unavailable_memory_budget(self):
        fingerprint = source_fingerprint()
        code = 'from exact_checks import worker\nworker.MEMORY_BYTES=1\nworker.main()\n'
        executor = IsolatedExecutor()
        with patch('exact_checks.executor.subprocess.Popen', side_effect=fixture_process(code)):
            with self.assertRaises(ExecutionFailure) as caught:
                executor.prepare(2000, fingerprint)
        self.assertEqual(caught.exception.code, 'resource_limit')
        self.assertIsNone(executor.process)

    def test_peak_memory_overrun_never_publishes_fact(self):
        fingerprint = source_fingerprint()
        code = ('from exact_checks import worker\n'
                'from exact_checks.operations import ExactOperation\n'
                'def excessive(self,request,result,**kwargs):\n'
                '    data=bytearray(80*1024*1024)\n'
                '    return {"status":"supported"}\n'
                'ExactOperation.execute=excessive\n'
                'worker.main()\n')
        executor = IsolatedExecutor()
        try:
            with patch('exact_checks.executor.subprocess.Popen', side_effect=fixture_process(code)):
                executor.prepare(2000, fingerprint)
            _, result, request = self.request()
            with self.assertRaises(ExecutionFailure) as caught:
                executor.execute(request['operation'], request, result, 250, fingerprint)
            self.assertEqual(caught.exception.code, 'resource_limit')
            self.assertIsNone(executor.process)
        finally:
            executor.close()

    def test_changed_fingerprint_does_not_use_warm_worker(self):
        op = ExactOperation('m06.pi_interval_membership')
        try:
            op.prepare_execution(2000)
            _, result, request = self.request()
            with self.assertRaises(ExecutionFailure) as caught:
                op._executor.execute(op.id, request, result, 250, '0' * 64)
            self.assertEqual(caught.exception.code, 'configuration_changed')
            self.assertIsNone(op._executor.process)
        finally:
            op.close()

    def test_timeout_before_transport_is_safe(self):
        op = ExactOperation('m06.pi_interval_membership')
        try:
            op.prepare_execution(2000)
            _, result, request = self.request()
            with self.assertRaises(ExecutionFailure) as caught:
                op.execute_bounded(request, result, 0)
            self.assertEqual(caught.exception.code, 'timeout')
        finally:
            op.close()

    def test_bad_context_keeps_worker_for_independent_check(self):
        op = ExactOperation('m06.pi_interval_membership')
        try:
            op.prepare_execution(2000)
            pid = op._executor.process.pid
            inp, result, request = self.request()
            bad = deepcopy(request)
            bad['context']['branch_id'] = 'missing'
            with self.assertRaises(ExecutionFailure) as caught:
                op.execute_bounded(bad, result, 250)
            self.assertEqual(caught.exception.code, 'invalid_operation_context')
            self.assertEqual(op._executor.process.pid, pid)
            good = op.execute_bounded(request, result, 250)
            self.assertEqual(good['status'], 'supported')
            self.assertEqual(s.m01.validate(s.attach(result, good), inp), [])
            metadata = json.loads(good['code_check']['evidence'])['execution']
            self.assertEqual(metadata['isolation'], 'subprocess')
            self.assertLessEqual(metadata['peak_rss_bytes'], 64 * 1024 * 1024)
        finally:
            op.close()


if __name__ == '__main__':
    unittest.main()
