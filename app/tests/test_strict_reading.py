"""Regression coverage for actual omitted coverage.status/extra task failures."""
from copy import deepcopy
import json
import unittest
import tempfile
from pathlib import Path

from jsonschema import Draft202012Validator

from app.compact_reading import schema
from app.strict_reading import response_format, decode_reading, reading_diagnostics
from app.tests.test_compact_reading import page
from app.tests import test_fresh_checking as fixture_support


def wire_value(*pages):
    return dict(pages=list(pages), interpretations=None, branches=None,
                assumptions=None, links=None, features=None)


class StrictReadingTests(unittest.TestCase):
    def test_required_fields_and_extra_task_are_enforced_in_actual_provider_schema(self):
        output=response_format(schema(2))
        self.assertEqual(output['type'],'json_schema')
        self.assertIs(output['json_schema']['strict'],True)
        check=Draft202012Validator(output['json_schema']['schema'])
        correct=wire_value(page(1),page(2))
        self.assertFalse(list(check.iter_errors(correct)))
        for key in ('status','reason','score_scope','score_scope_reason'):
            broken=deepcopy(correct);broken['pages'][1]['coverage'].pop(key)
            with self.subTest(key=key):
                self.assertTrue(list(check.iter_errors(broken)))
                with self.assertRaisesRegex(ValueError,'strict_reading_response_invalid'):
                    decode_reading(broken,schema(2))
        self.assertTrue(list(check.iter_errors({**correct,'task':{}})))

    def test_wire_schema_has_only_supported_closed_objects(self):
        native=response_format(schema(2))['json_schema']['schema']
        def walk(s):
            self.assertFalse(set(s)&{'allOf','oneOf','not','pattern','$ref','uniqueItems','minLength','maxLength'})
            if 'properties' in s:
                self.assertEqual(set(s['properties']),set(s['required']))
                self.assertIs(s['additionalProperties'],False)
                for prop in s['properties'].values():walk(prop)
            if 'items' in s:walk(s['items'])
            for child in s.get('anyOf',[]):walk(child)
        walk(native)

    def test_null_only_means_optional_absence_and_raw_is_unchanged(self):
        raw=wire_value(page());before=deepcopy(raw)
        parsed=decode_reading(raw,schema(1))
        self.assertEqual(raw,before)
        self.assertEqual(parsed,{'pages':raw['pages']})
        raw['pages'][0]['coverage']['status']=None
        with self.assertRaisesRegex(ValueError,'strict_reading_response_invalid'):
            decode_reading(raw,schema(1))

    def test_prose_claim_never_supplies_missing_completeness(self):
        value=wire_value(page())
        coverage=value['pages'][0]['coverage'];coverage.pop('status')
        coverage.update(reason='Все записи прочитаны полностью.',score_scope_reason='Всё прочитано.')
        with self.assertRaisesRegex(ValueError,'strict_reading_response_invalid'):
            decode_reading(value,schema(1))

    def test_provider_shape_does_not_replace_local_conditional_checks(self):
        value=wire_value(page())
        value['pages'][0]['entries'][0]['text']=None
        self.assertFalse(list(Draft202012Validator(response_format(schema(1))['json_schema']['schema']).iter_errors(value)))
        with self.assertRaisesRegex(ValueError,'compact_reading_schema'):
            decode_reading(value,schema(1))

    def test_interpretation_optional_context_is_not_invented(self):
        value=wire_value(page())
        value['interpretations']=[dict(entries=['1:1'],meaning='Возможное прочтение',branch=None,assumptions=None,context=None)]
        parsed=decode_reading(value,schema(1))
        self.assertEqual(parsed['interpretations'],[dict(entries=['1:1'],meaning='Возможное прочтение')])

    def test_omitted_optional_fields_match_null_without_filling_raw_or_decoded_fields(self):
        value=dict(pages=[page()],interpretations=[dict(entries=['1:1'],meaning='Возможное прочтение')])
        before=deepcopy(value)
        decoded=decode_reading(value,schema(1))
        self.assertEqual(decoded,before)
        self.assertEqual(value,before)
        diagnostics=reading_diagnostics(value,schema(1))
        self.assertFalse(diagnostics['strict_wire_compliant'])
        self.assertIn(['interpretations',0,'branch'],diagnostics['omitted_optional_fields'])
        self.assertIn(['interpretations',0,'assumptions'],diagnostics['omitted_optional_fields'])
        self.assertIn(['interpretations',0,'context'],diagnostics['omitted_optional_fields'])
        self.assertNotIn('Возможное прочтение',str(diagnostics))

    def test_missing_interpretation_content_and_invalid_present_context_still_fail(self):
        correct=dict(pages=[page()],interpretations=[dict(entries=['1:1'],meaning='Возможное прочтение')])
        for key in ('entries','meaning'):
            value=deepcopy(correct);value['interpretations'][0].pop(key)
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'strict_reading_response_invalid'):
                decode_reading(value,schema(1))
        for key,value in (('branch','main'),('assumptions',[0]),('context',{})):
            raw=deepcopy(correct);raw['interpretations'][0][key]=value
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'strict_reading_response_invalid'):
                decode_reading(raw,schema(1))
        raw=deepcopy(correct);raw['interpretations'][0]['context']=['invented']
        with self.assertRaisesRegex(ValueError,'compact_reading_schema'):
            decode_reading(raw,schema(1))

    def test_provider_compliance_is_distinct_from_semantic_acceptance(self):
        compliant=wire_value(page())
        self.assertTrue(reading_diagnostics(compliant,schema(1))['strict_wire_compliant'])
        noncompliant={'pages':compliant['pages']}
        self.assertFalse(reading_diagnostics(noncompliant,schema(1))['strict_wire_compliant'])
        self.assertEqual(decode_reading(compliant,schema(1)),decode_reading(noncompliant,schema(1)))


class StrictRequestTests(unittest.TestCase):
    def test_full_app_request_uses_native_schema_and_charges_only_three_fixture_calls(self):
        fixture_test=fixture_support.FreshTests();fixture_test.setUp();self.addCleanup(fixture_test.doCleanups);self.addCleanup(fixture_test.tearDown)
        service,transport=fixture_test.service()
        _,job=fixture_test.run_work(service,pages=2)
        self.assertEqual(job['state'],'completed')
        read=next(r for r in transport.requests if r.stage=='solution_ocr')
        self.assertEqual(read.payload['response_format'],response_format(schema(2)))
        self.assertEqual(job['accounting']['calls'],3)
        self.assertFalse(job['accounting']['unresolved'])
        source_data=json.loads(read.payload['messages'][1]['content'][0]['text'])
        self.assertNotIn('reference_key',source_data)

    def test_gemini_control_completion_is_not_reopened_by_configuration_fix(self):
        from unittest.mock import patch
        from app.live_checking import FreshChecking
        probe=FreshChecking.__new__(FreshChecking)
        probe.work_profiles={'Efixture':'G'}
        for name in ('GEMINI_CONTROL.json','STRICT_GEMINI_CONTROL.json','CORE_GEMINI_CONTROL.json'):
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                self.check_completed_scope(probe,Path(tmp),name,patch)

    def check_completed_scope(self,probe,root,name,patch):
        report=root/'planning/m17'/name
        report.parent.mkdir(parents=True)
        report.write_text(json.dumps({'completed_at':'2026-10-08T20:30:00+00:00',
            'checks':[{'work_id':'Efixture','state':'failed'}]}))
        with patch('app.live_checking.ROOT',root), patch('app.live_checking.active_approval',
                return_value={'amendment':{'authorized_at':'2026-10-08T20:27:00+00:00'}}):
            self.assertEqual(probe._completed_scope(),{'Efixture'})
        with patch('app.live_checking.ROOT',root), patch('app.live_checking.active_approval',
                return_value={'amendment':{'authorized_at':'2026-10-08T20:40:00+00:00'}}):
            self.assertEqual(probe._completed_scope(),{"Efixture"})
