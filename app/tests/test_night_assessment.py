"""Concrete score/provenance regressions from the offline October 9 audit."""
from copy import deepcopy
import unittest
from unittest.mock import patch

from app.assessment_core import (materialize_core, materialize_report,
                                 report_from_core, core_response)
from app.compact_assessment import materialize_compact_assessment
from app.tests import test_compact_assessment as fixtures
from app.tests.test_assessment_core import core_fixture
from app.tests.test_compact_reading import entry
from contracts.ege_v1 import validate as contract


class NightAssessmentTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.CompactAssessmentTests(methodName='test_both_types_full_score_with_code_owned_links')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        blocker = patch('socket.socket.connect', side_effect=AssertionError('Network forbidden'))
        blocker.start()
        self.addCleanup(blocker.stop)
        self.setup_work()

    def setup_work(self, kind='trigonometric_equation'):
        self.fixture.setup_work(kind)
        self.response = core_fixture(kind)

    def materialize(self):
        result = materialize_core(self.response, self.fixture.baseline, self.fixture.source)
        self.assertEqual(contract.validate(result, self.fixture.source), [])
        return result

    def partial_a(self):
        self.response['score'].update(value=1, criterion='M02-FIPI13-1')
        self.response['checks'].append(dict(statement='The written B roots are outside the interval.',
            entries=['1:1'], holds=False, reason='Synthetic explicit interval comparison.',
            kind='membership', domain='R'))
        for r in self.response['requirements']:
            if r['key'].startswith('B-'):
                r.update(outcome='no', checks=[2], reason='The wrong B root is outside the interval.')
            if r['key']=='CALC-ONLY':
                r.update(outcome='unused', entries=[], checks=[])

    def test_partial_a_validates_and_explains_the_b_evidence_excluding_two_points(self):
        self.partial_a()
        result = self.materialize()
        self.assertEqual(result['score']['value'], 1)
        self.assertEqual(result['score']['requirement_ids'], [
            'assess-r-A-ANSWER','assess-r-A-JUSTIFICATION','assess-r-B-ANSWER','assess-r-B-JUSTIFICATION'])
        draft = report_from_core(self.response, 'trigonometric_equation')
        self.assertTrue(any('wrong B root' in f['text'] for f in draft['feedback']))
        report = materialize_report(self.response, draft, self.fixture.baseline, self.fixture.source)
        self.assertEqual(report['score']['requirement_ids'], result['score']['requirement_ids'])
        self.assertEqual(report['readiness'], dict(score='ready', feedback='ready'))

    def test_conditional_b_failure_cannot_silently_reduce_a_correct_a_to_one(self):
        self.partial_a()
        self.fixture.baseline['branches'].append(dict(id='b-reading',parent_id='main',label='B reading',
            status='active',unit_ids=['solution-1-u1'],assumption_ids=['b-hypothesis']))
        self.fixture.baseline['assumptions'].append(dict(id='b-hypothesis',claim='One possible B reading',
            origin='hypothesis',source_ref='synthetic',branch_id='b-reading',status='conditional'))
        self.response['checks'][1].update(branch='b-reading',assumptions=['b-hypothesis'])
        for r in self.response['requirements']:
            if r['key'].startswith('B-'): r['branch']='b-reading'
        with self.assertRaisesRegex(ValueError, 'full_m01_contract'):
            self.materialize()
        # An independently established alternative may still prove exactly one
        # point. It must not inherit the conditional B evidence by link selection.
        next(r for r in self.response['requirements'] if r['key']=='CALC-ONLY').update(
            outcome='yes', entries=['1:1'], checks=[1])
        result=self.materialize()
        self.assertEqual(result['score']['requirement_ids'], ['assess-r-CALC-ONLY'])

    def test_legacy_partial_a_cannot_omit_b_from_its_grade_basis(self):
        self.partial_a()
        legacy = core_response(self.response, 'trigonometric_equation')
        legacy['score']['requirements']=['A-ANSWER','A-JUSTIFICATION']
        with self.assertRaisesRegex(ValueError, 'assessment_score_basis'):
            materialize_compact_assessment(legacy, self.fixture.baseline, self.fixture.source)

    def test_independent_calculation_point_survives_unclear_b_requirements(self):
        self.response['score'].update(value=1, criterion='M02-FIPI13-1')
        for r in self.response['requirements']:
            if r['key'].startswith('B-'): r['outcome']='unclear'
            if r['key']=='CALC-ONLY': r['outcome']='yes'
        result=self.materialize()
        self.assertEqual(result['score']['requirement_ids'], ['assess-r-CALC-ONLY'])

    def test_inequality_special_point_cannot_override_a_fully_supported_two(self):
        for criterion, key in (('m03-score-1-boundary','m03-boundary-exception'),
                               ('m03-score-1-calculation','m03-calculation-exception')):
            with self.subTest(criterion=criterion):
                self.setup_work('inequality')
                self.response['score'].update(value=1, criterion=criterion)
                next(r for r in self.response['requirements'] if r['key']==key)['outcome']='yes'
                with self.assertRaisesRegex(ValueError, 'assessment_score_basis'): self.materialize()
                next(r for r in self.response['requirements'] if r['key']=='m03-answer')['outcome']='no'
                self.assertEqual(self.materialize()['score']['value'], 1)

    def test_full_score_rejects_an_explicitly_fulfilled_incompatible_partial_branch(self):
        for kind, key in (('trigonometric_equation','CALC-ONLY'),
                          ('inequality','m03-boundary-exception'),
                          ('inequality','m03-calculation-exception')):
            self.setup_work(kind)
            next(r for r in self.response['requirements'] if r['key']==key)['outcome']='yes'
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, 'assessment_score_basis'):
                self.materialize()

    def test_independent_full_score_tolerates_unused_or_unresolved_unselected_alternatives(self):
        for kind in ('trigonometric_equation','inequality'):
            for outcome in ('no','unused','unclear'):
                self.setup_work(kind)
                for r in self.response['requirements']:
                    if r['key']=='CALC-ONLY' or r['key'].endswith('exception'): r['outcome']=outcome
                with self.subTest(kind=kind,outcome=outcome):
                    self.assertEqual(self.materialize()['score']['value'], 2)

    def teacher_check(self):
        self.fixture.reading['pages'][0]['entries']=[entry('Student writes x=0'),entry('Teacher supplies the proof')]
        self.fixture.reading['pages'][0]['entries'][1]['author']='teacher'
        self.fixture.rebuild()
        self.response['checks'][0].update(entries=['1:2'],statement='The teacher proof obtains the right roots.',
            reason='Only the teacher proof was checked.')

    def test_teacher_proof_cannot_fulfill_an_unrelated_student_requirement(self):
        self.teacher_check()
        with self.assertRaisesRegex(ValueError, 'assessment_student_proof_link'): self.materialize()

    def test_empty_checks_key_and_teacher_context_do_not_fabricate_student_proof(self):
        self.teacher_check()
        original=deepcopy(self.response)
        for change in (dict(entries=[]), dict(entries=[],uses_key=True),
                       dict(context=['1:1']), dict(entries=[],depends_on=[1])):
            self.response=deepcopy(original)
            self.response['checks'][0].update(change)
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, 'assessment_student_proof_link'):
                self.materialize()

    def test_checked_different_student_entry_does_not_prove_the_cited_one(self):
        self.fixture.reading['pages'][0]['entries']=[entry('x=0'),entry('Separate note')]
        self.fixture.rebuild()
        self.response['checks'][0]['entries']=['1:2']
        with self.assertRaisesRegex(ValueError, 'assessment_student_proof_link'): self.materialize()

    def test_derived_check_follows_an_explicit_supported_student_proof(self):
        self.response['checks'].append(dict(statement='The derived conclusion follows from check 1.',
            entries=[],holds=True,reason='Explicit derivation.',kind='implication',domain='R',depends_on=[1]))
        for r in self.response['requirements']: r['checks']=[2]
        self.assertEqual(self.materialize()['score']['value'], 2)
        for holds in (False,None):
            self.response['checks'][0]['holds']=holds
            with self.subTest(holds=holds), self.assertRaisesRegex(ValueError, 'assessment_student_proof_link'):
                self.materialize()

    def test_only_teacher_dependencies_do_not_fulfill_student_requirements(self):
        self.teacher_check()
        self.response['checks'].append(dict(statement='Derived from the teacher proof.',entries=[],holds=True,
            reason='Explicit teacher-only derivation.',kind='implication',domain='R',depends_on=[1]))
        for r in self.response['requirements']: r['checks']=[2]
        with self.assertRaisesRegex(ValueError, 'assessment_student_proof_link'): self.materialize()

    def test_established_student_premise_can_explicitly_connect_a_check(self):
        self.fixture.reading['assumptions']=[dict(claim='x=0',origin='student_unit',entry='1:1',
            branch=0,status='established')]
        self.fixture.rebuild()
        self.response['checks'][0].update(entries=[],assumptions=['read-a1'])
        self.assertEqual(self.materialize()['score']['value'], 2)

    def test_conditional_student_premise_without_checked_student_entries_is_insufficient(self):
        self.fixture.reading['assumptions']=[dict(claim='x=0',origin='student_unit',entry='1:1',
            branch=0,status='conditional')]
        self.fixture.rebuild()
        self.response['checks'][0].update(entries=[],assumptions=['read-a1'])
        with self.assertRaisesRegex(ValueError, 'assessment_student_proof_link'): self.materialize()

    def uncertain_interpretations(self, reading='ambiguous'):
        self.fixture.reading['pages'][0]['entries'][0].update(reading=reading,
            text=None if reading=='unreadable' else 'x = 0 or pi')
        self.fixture.reading['interpretations']=[dict(entries=['1:1'],meaning='x=0'),
                                                dict(entries=['1:1'],meaning='x=pi')]
        self.fixture.rebuild()
        self.response['checks'][0]['interpretations']=['read-i1']
        self.response['uncertainties']=[dict(entries=['1:1'],effect='none',
            reason='Synthetic model declaration of independence, not a proof of all readings.')]

    def test_selected_proposed_uncertain_reading_cannot_be_confirmed_by_none(self):
        for reading in ('ambiguous','unreadable'):
            self.setup_work()
            self.uncertain_interpretations(reading)
            with self.subTest(reading=reading), self.assertRaisesRegex(ValueError, 'assessment_reading_alternatives'):
                self.materialize()

    def test_transitive_selected_check_cannot_hide_its_uncertain_interpretation(self):
        self.uncertain_interpretations()
        self.response['checks'].extend([
            dict(statement='First derived check.',entries=[],holds=True,reason='Uses check 1.',
                 kind='implication',domain='R',depends_on=[1]),
            dict(statement='Second derived check.',entries=[],holds=True,reason='Uses check 2.',
                 kind='implication',domain='R',depends_on=[2])])
        for r in self.response['requirements']: r['checks']=[3]
        with self.assertRaisesRegex(ValueError, 'assessment_reading_alternatives'): self.materialize()

    def test_refuted_b_check_also_needs_an_established_reading_to_exclude_two(self):
        self.partial_a()
        self.uncertain_interpretations()
        self.response['checks'][0].pop('interpretations')
        self.response['checks'][1]['interpretations']=['read-i1']
        with self.assertRaisesRegex(ValueError, 'assessment_reading_alternatives'): self.materialize()

    def test_human_confirmed_interpretation_and_clear_diagram_are_valid_evidence(self):
        self.uncertain_interpretations()
        self.fixture.baseline['interpretations'][0].update(status='human_confirmed',
            source=dict(kind='human_review',ref='synthetic-reviewer'))
        self.assertEqual(self.materialize()['score']['value'], 2)
        self.setup_work()
        self.fixture.reading['pages'][0]['entries'][0].update(kind='diagram',text='A clearly drawn point at 0.')
        self.fixture.rebuild()
        self.response['checks'][0]['interpretations']=['read-d1-1']
        self.assertEqual(self.materialize()['score']['value'], 2)

    def test_nonselected_uncertain_observation_does_not_block_independent_calc(self):
        self.fixture.reading['pages'][0]['entries']=[entry('Clear independent solution'),
                                                   {**entry('0 or pi'),'reading':'ambiguous'}]
        self.fixture.reading['interpretations']=[dict(entries=['1:2'],meaning='0'),
                                                dict(entries=['1:2'],meaning='pi')]
        self.fixture.rebuild()
        self.response['checks'].append(dict(statement='An additional uncertain observation.',entries=['1:2'],
            holds=True,reason='One proposed reading, separate from the grade basis.',kind='observation',
            domain='R',interpretations=['read-i1']))
        for r in self.response['requirements']:
            if r['key'].startswith('B-'): r.update(outcome='unclear',entries=['1:2'],checks=[2])
            if r['key']=='CALC-ONLY': r['outcome']='yes'
        self.response['score'].update(value=1,criterion='M02-FIPI13-1')
        self.response['uncertainties']=[dict(entries=['1:2'],effect='none',
            reason='The clear independent CALC proof supplies the grade; this observation is unused.')]
        result=self.materialize()
        self.assertEqual(result['score']['requirement_ids'], ['assess-r-CALC-ONLY'])

    def test_none_without_explicit_interpretation_choice_remains_a_model_judgment(self):
        self.uncertain_interpretations()
        self.response['checks'][0].pop('interpretations')
        self.assertEqual(self.materialize()['score']['value'], 2)


if __name__ == '__main__': unittest.main()
