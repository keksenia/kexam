"""End-to-end through the unmodified M04 pipeline with synthetic fixtures in a temp dir.

Shows that the eight operations plug into Pipeline(operations=...) as written,
their facts survive M04's checks and M01 validation, and reach assessment. The
reading adapter is the synthetic fixture: no OCR, model or student data.
"""
from pathlib import Path
import tempfile
import unittest

from exact_checks.tests import support as s
from app.diagnostic import SyntheticFixtureAdapter, fixture_criterion, fixture_form
from app.pipeline import Pipeline
from app.server import WorkStore
from contracts.ege_v1 import validate as m01
from exact_checks.operations import all_operations


def bind(arguments, sources):
    return [{"argument_pointer": "/" + key, "source_kind": "unit", "source_ref": sources.get(key, "solution-1-u1")}
            for key in arguments]


def request(operation, arguments, sources=None, unit_ids=("solution-1-u1",)):
    return {"operation": operation, "version": "exact-checks-1.1.0", "arguments": arguments,
            "context": {"unit_ids": list(unit_ids), "interpretation_ids": [], "branch_id": "main", "assumption_ids": [],
                        "context_unit_ids": [], "depends_on_fact_ids": [], "bindings": bind(arguments, sources or {})}}


class WithExactChecks(SyntheticFixtureAdapter):
    id = "synthetic-exact-integration"

    def read(self, req, image):
        response = super().read(req, image)
        if req["task"]["exam_number"] == 13:
            response["features"] = ["pi_families", "explicit_interval", "circle"]
            response["operation_requests"] = [
                request("m06.select_pi_roots", {"families": [{"offset": s.q(0), "period": s.q(1)}],
                                                "interval": s.pi_interval(0, 1), "claimed_roots": [s.pi(0), s.pi(1)]}),
                request("m06.circle_point_equal", {"angle": s.pi(-19, 4), "independent_point_angle": s.pi(5, 4)},
                        sources={"independent_point_angle": "solution-1-u2"},
                        unit_ids=("solution-1-u1", "solution-1-u2")),
            ]
        else:
            response["features"] = ["inequality", "rational_fraction"]
            response["operation_requests"] = [
                request("m07.set_equal", {"left": s.exact_set([s.interval(1, None, False, False)]),
                                          "right": s.exact_set([s.interval(1, None, True, False)])}),
                request("m07.denominator_nonzero", {"expression": {"representation": "original_polynomial_quotient", "variable": "x",
                                                                   "numerator": [s.q(-1), s.q(1)], "denominator": [s.q(-1), s.q(1)]},
                                                    "point": s.real(1)}),
            ]
        return response


class WithTaskSource(WithExactChecks):
    id = "synthetic-exact-task-source"

    def read(self, req, image):
        response = super().read(req, image)
        for binding in response["operation_requests"][0]["context"]["bindings"]:
            if binding["argument_pointer"] == "/interval":
                binding.update(source_kind="input_pointer", source_ref="/task/statement/text")
        return response


class PipelineIntegration(unittest.TestCase):
    def run_case(self, task_type, *, source_bound=False):
        with tempfile.TemporaryDirectory() as directory:
            works = WorkStore(Path(directory) / "works")
            work = works.save(fixture_form(task_type))
            pipeline = Pipeline(works, WithTaskSource() if source_bound else WithExactChecks(), operations=all_operations(),
                                criteria=[fixture_criterion(task_type)], allow_synthetic=True)
            try:
                run = pipeline.create(work["id"], background=False, synthetic=True)
                if source_bound:
                    # Trusted caller uses the application-owned frozen snapshot,
                    # independently of the reading adapter's operation request.
                    for item in pipeline.operations.values():
                        item.close()
                    pipeline.operations = {o.id: o for o in all_operations(source_input=run["input"])}
                run = pipeline.execute(run["id"])
            finally:
                pipeline.close()
        self.assertEqual((run["state"], run["error_code"]), ("completed", None))
        for version in run["result_versions"]:
            self.assertEqual(m01.validate(version, run["input"]), [])
        return {f["code_check"]["operation"]: f for f in run["result"]["facts"] if f["method"] == "exact_code"}

    def test_trigonometric_line(self):
        facts = self.run_case("trigonometric_equation")
        self.assertEqual(facts["m06.select_pi_roots"]["status"], "supported")
        self.assertEqual(facts["m06.circle_point_equal"]["status"], "supported")
        self.assertEqual(facts["m06.select_pi_roots"]["tool_version"], "exact-checks-1.1.0")

    def test_inequality_line(self):
        facts = self.run_case("inequality")
        self.assertEqual(facts["m07.set_equal"]["status"], "contradicted")      # strictness at 1
        self.assertEqual(facts["m07.denominator_nonzero"]["status"], "contradicted")  # Q(1) = 0

    def test_application_snapshot_reaches_task_bound_operation(self):
        facts = self.run_case("trigonometric_equation", source_bound=True)
        self.assertEqual(facts["m06.select_pi_roots"]["status"], "supported")


if __name__ == "__main__":
    unittest.main()
