"""A rubric requirement is distinct from the model's fulfillment judgment."""
import unittest

from app.assessment_core import report_from_core
from app.tests.test_assessment_core import core_fixture


class ReportWordingTests(unittest.TestCase):
    def test_zero_score_report_does_not_present_failed_requirements_as_fulfilled(self):
        response = core_fixture("inequality")
        response["score"].update(value=0, criterion="m03-score-0")
        for requirement in response["requirements"]:
            if requirement["key"] in ("m03-transformations", "m03-answer"):
                requirement["outcome"] = "no"
        report = report_from_core(response, "inequality")
        self.assertEqual(len(report["feedback"]), 4)
        for item in report["feedback"]:
            self.assertTrue(item["text"].startswith("Требование: "))
            self.assertIn("Оценка требования: не выполнено.", item["text"])
            self.assertIn("Обоснование модели: ", item["text"])

    def test_full_score_report_preserves_explicit_positive_outcomes(self):
        report = report_from_core(core_fixture(), "trigonometric_equation")
        self.assertEqual(len(report["feedback"]), 4)
        for item in report["feedback"]:
            self.assertIn("Оценка требования: выполнено.", item["text"])
            self.assertNotIn("Оценка требования: не выполнено.", item["text"])
