"""Regression for the first real format failure; synthetic texts, no calls."""
import unittest
from app.call_guard import parse_model_json
from app.live_condition import validate_condition
from app.tests.test_fresh_checking import condition, reading


class ModelJSONTests(unittest.TestCase):
    def test_complete_json_fence_and_plain_json_are_equivalent(self):
        for text in ('{"text":"ok"}', '```json\n{"text":"ok"}\n```', '```\n{"text":"ok"}\n```'):
            self.assertEqual(parse_model_json(text),{"text":"ok"})
    def test_prose_multiple_documents_duplicates_and_broken_fences_still_fail(self):
        for text in ('Here is JSON: {"text":"ok"}', '{} {}', '```json\n{}\n```\nextra', '```python\n{}\n```', '{"text":"a","text":"b"}', '```json\n{"text":"a","text":"b"}\n```'):
            with self.subTest(text=text),self.assertRaises(ValueError):parse_model_json(text)
    def test_only_exact_nonsemantic_schema_annotation_can_be_removed(self):
        value=reading(condition());value['additionalProperties']=False
        self.assertEqual(validate_condition(value,'trigonometric_equation',sources={'text'}),reading(condition()))
        for extra in ({'additionalProperties':True},{'hidden_condition':'x>5'}):
            with self.assertRaises(ValueError):validate_condition({**reading(condition()),**extra},'trigonometric_equation',sources={'text'})
