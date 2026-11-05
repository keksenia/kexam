"""Nonblank reading evidence and explicit unreadability; synthetic inputs only."""
from copy import deepcopy
import unittest

from app.tests import test_compact_reading as fixtures


class NonblankReadingTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompactReadingTests(methodName="test_two_pages_order_and_app_owned_anchors")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)

    def test_whitespace_cannot_become_a_clear_math_or_diagram_record(self):
        for kind in ("math", "text", "diagram"):
            for text in (" ", "\t\n", "\u00a0"):
                reading = deepcopy(self.fixture.reading)
                reading["pages"][0]["entries"][0].update(kind=kind, text=text)
                with self.subTest(kind=kind, text=repr(text)), self.assertRaisesRegex(ValueError, "compact_reading_schema"):
                    self.fixture.materialize(reading)

    def test_explicit_unreadable_null_stays_unreadable_without_added_content(self):
        reading = deepcopy(self.fixture.reading)
        reading["pages"][0]["entries"][0].update(text=None, reading="unreadable")
        reading["pages"][0]["coverage"].update(status="partial", score_scope="unresolved")
        result = self.fixture.materialize(reading)
        self.assertIsNone(result["units"][0]["literal"])
        self.assertEqual(result["units"][0]["reading_status"], "unreadable")
        self.assertIsNone(result["score"]["value"])

    def test_nonblank_literal_is_preserved_without_trimming_or_math_repair(self):
        reading = deepcopy(self.fixture.reading)
        reading["pages"][0]["entries"][0]["text"] = "  2 + 2 = 5\n"
        result = self.fixture.materialize(reading)
        self.assertEqual(result["units"][0]["literal"], "  2 + 2 = 5\n")
