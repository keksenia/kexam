"""Authoritative input binding; model operation text never supplies snapshots."""
from copy import deepcopy
import json
import unittest

from exact_checks import protocol as P
from exact_checks.operations import ExactOperation, all_operations
from exact_checks.tests import support as s


class InputSources(unittest.TestCase):
    def setUp(self):
        self.input, self.result = s.base(13, "authoritative-task-pointer")
        self.request = s.args("m06.pi_interval_membership", point=s.pi(0),
                              interval=s.pi_interval(0, 1), expected_member=True)
        for binding in self.request["context"]["bindings"]:
            binding.update(source_kind="input_pointer", source_ref="/task/statement/text")

    def verdict(self, snapshot=None, pointer=None):
        request = deepcopy(self.request)
        if pointer is not None:
            for binding in request["context"]["bindings"]:
                binding["source_ref"] = pointer
        fact, _, response = s.run(request["operation"], request, self.result,
                                   source_input=snapshot)
        self.assertEqual(s.m01.validate(s.attach(self.result, fact), self.input), [])
        return response["status"], response["reason_code"]

    def test_real_task_pointer_requires_trusted_snapshot(self):
        self.assertEqual(self.verdict(), ("unknown", "source_mismatch"))
        self.assertEqual(self.verdict(self.input), ("verified", "exact_true"))

    def test_missing_source_never_creates_supported_fact(self):
        self.assertEqual(self.verdict(self.input, "/task/this_field_does_not_exist"),
                         ("unknown", "source_mismatch"))

    def test_null_and_empty_source_are_not_evidence(self):
        snapshot = deepcopy(self.input)
        snapshot["task"]["source_label"] = None
        self.assertEqual(self.verdict(snapshot, "/task/source_label"), ("unknown", "source_mismatch"))
        snapshot["task"]["source_label"] = " "
        self.assertEqual(s.m01.validate(snapshot), [])
        self.result["input_ref"]["sha256"] = s.m01.digest(snapshot)
        self.input = snapshot
        self.assertEqual(self.verdict(snapshot, "/task/source_label"), ("unknown", "source_mismatch"))
        self.assertEqual(self.verdict(snapshot, "/task/statement/missing"), ("unknown", "source_mismatch"))

    def test_wrong_snapshot_id_revision_and_contents_are_stale(self):
        for key, value in (("id", "OTHER-SOURCE"), ("revision", self.input["revision"] + 1)):
            snapshot = deepcopy(self.input)
            snapshot[key] = value
            self.assertEqual(self.verdict(snapshot), ("unknown", "stale_input"))
        snapshot = deepcopy(self.input)
        snapshot["task"]["statement"]["text"] = "Changed condition"
        self.assertEqual(self.verdict(snapshot), ("unknown", "stale_input"))

    def test_invalid_snapshot_does_not_replace_input_identity(self):
        self.assertEqual(self.verdict({"kind": "input"}), ("unknown", "source_mismatch"))

    def test_json_pointer_escapes_resolve_without_aliasing(self):
        self.assertEqual(P._resolve("/a~1b/~0key", {"a/b": {"~key": "source"}}), "source")
        for pointer in ("/task/statement/text~1missing", "/task/statement/te~0xt", "/task/statement/text~2"):
            self.assertEqual(self.verdict(self.input, pointer), ("unknown", "source_mismatch"))

    def test_model_request_cannot_supply_trusted_source(self):
        request = deepcopy(self.request)
        request["source_input"] = deepcopy(self.input)
        fact, _, response = s.run(request["operation"], request, self.result)
        self.assertEqual((response["status"], response["reason_code"]), ("unknown", "source_mismatch"))
        self.assertEqual(fact["status"], "unknown")

    def test_fixed_snapshot_is_copied_and_reaches_isolated_worker(self):
        snapshot = deepcopy(self.input)
        operations = all_operations(source_input=snapshot)
        snapshot["task"]["statement"]["text"] = "Mutated caller dictionary"
        operation = next(o for o in operations if o.id == self.request["operation"])
        try:
            operation.prepare_execution(2000)
            fact = operation.execute_bounded(self.request, self.result, 250)
            response = json.loads(fact["code_check"]["evidence"])["k01_response"]
            self.assertEqual((response["status"], response["reason_code"]), ("verified", "exact_true"))
            self.assertEqual(s.m01.validate(s.attach(self.result, fact), self.input), [])
            bad = deepcopy(self.request)
            bad["context"]["bindings"][0]["source_ref"] = "/task/missing"
            refused = operation.execute_bounded(bad, self.result, 250)
            self.assertEqual(refused["status"], "unknown")
        finally:
            for item in operations:
                item.close()
