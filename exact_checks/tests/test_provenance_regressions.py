"""Adversarial provenance examples, plus valid conditional and independent controls.

Synthetic M01 records only. These tests check claims on supplied structured
arguments; they do not stand in for OCR quality or expert grading.
"""
from copy import deepcopy
import unittest

from exact_checks import protocol as P
from exact_checks.operations import ExactOperation, OperationContextError, build_request
from exact_checks.tests import support as s


def interpretation(iid, units=('u1',), context=(), assumptions=(), branch='branch-main'):
    return dict(id=iid, unit_ids=list(units), meaning='Synthetic meaning with recorded dependencies.',
                status='proposed', source=dict(kind='synthetic', ref='provenance-regressions'),
                branch_id=branch, assumption_ids=list(assumptions), context_unit_ids=list(context))


def fact(fid, units=('u1',), context=(), interpretations=(), assumptions=(), depends=(), branch='branch-main'):
    return dict(id=fid, unit_ids=list(units), interpretation_ids=list(interpretations),
                claim='Synthetic supported statement.', method='model', status='supported',
                reason='Controlled synthetic provenance.', tool_version=None,
                depends_on_fact_ids=list(depends), branch_id=branch, assumption_ids=list(assumptions),
                context_unit_ids=list(context), claim_kind='other', domain='Synthetic only.', basis_sha256='0' * 64)


class ProvenanceRegressions(unittest.TestCase):
    def setUp(self):
        self.inp, self.result = s.base(13, 'provenance-regressions')

    def membership(self):
        return s.args('m06.pi_interval_membership', point=s.pi(1),
                      interval=s.pi_interval(0, 2), expected_member=True)

    def add_unit(self, uid, status='clear'):
        self.result['units'].append(s.unit(uid, reading_status=status))
        self.result['branches'][0]['unit_ids'].append(uid)

    def add_assumption(self, aid='a1', status='unknown', origin='hypothesis', source='u1', branch='branch-main'):
        self.result['assumptions'].append(dict(id=aid, claim='A recorded selected reading or premise.',
            origin=origin, source_ref=source, status=status, branch_id=branch))
        next(b for b in self.result['branches'] if b['id'] == branch)['assumption_ids'].append(aid)

    def refresh(self):
        digest = s.m01.reading_digest(self.result)
        for item in self.result['facts']:
            item['basis_sha256'] = digest

    def run_checked(self, request, status, reason=None):
        self.refresh()
        self.assertEqual(s.m01.validate(self.result, self.inp), [])
        output, envelope, response = s.run(request['operation'], request, self.result)
        self.assertEqual(response['status'], status, response)
        if reason:
            self.assertEqual(response['reason_code'], reason, response)
        self.assertEqual(s.m01.validate(s.attach(self.result, output), self.inp), [])
        self.assertEqual(list(P.RESPONSE_SCHEMA.iter_errors(response)), [])
        return output, envelope, response

    def use_interpretation(self, request, iid='i1'):
        request['context']['interpretation_ids'] = [iid]
        for binding in request['context']['bindings']:
            binding.update(source_kind='interpretation', source_ref=iid)
        return request

    def test_interpretation_inherits_ambiguous_semantic_context(self):
        self.add_unit('u2', 'ambiguous')
        self.result['interpretations'] = [interpretation('i1', context=['u2'])]
        self.run_checked(self.use_interpretation(self.membership()), 'unknown', 'unresolved_reading')

    def test_interpretation_inherits_unknown_assumption(self):
        self.add_assumption()
        self.result['interpretations'] = [interpretation('i1', assumptions=['a1'])]
        output, _, _ = self.run_checked(self.use_interpretation(self.membership()), 'unknown', 'unresolved_premise')
        self.assertIn('a1', output['assumption_ids'])

    def test_branch_scope_assumption_cannot_be_dropped(self):
        self.add_assumption()
        output, _, _ = self.run_checked(self.membership(), 'unknown', 'unresolved_premise')
        self.assertIn('a1', output['assumption_ids'])

    def test_conditional_reading_is_computed_and_preserved(self):
        self.result['units'][0]['reading_status'] = 'ambiguous'
        self.add_assumption(status='conditional', origin='student_unit')
        self.result['interpretations'] = [interpretation('i1', assumptions=['a1'])]
        output, envelope, _ = self.run_checked(self.use_interpretation(self.membership()), 'verified')
        self.assertIn('a1', output['assumption_ids'])
        self.assertIn('a1', envelope['context']['assumption_ids'])
        self.assertEqual(self.result['assumptions'][0]['status'], 'conditional')

    def test_fact_chain_inherits_interpretation_context(self):
        self.add_unit('u2', 'ambiguous')
        self.result['interpretations'] = [interpretation('i1', context=['u2'])]
        self.result['facts'] = [fact('f0', interpretations=['i1']), fact('f1', depends=['f0'])]
        request = self.membership()
        request['context']['depends_on_fact_ids'] = ['f1']
        for binding in request['context']['bindings']:
            binding.update(source_kind='fact', source_ref='f1')
        output, _, _ = self.run_checked(request, 'unknown', 'unresolved_reading')
        self.assertIn('f0', output['depends_on_fact_ids'])
        self.assertIn('u2', output['context_unit_ids'])

    def test_unrelated_uncertainty_does_not_block_local_fact(self):
        self.add_unit('u2', 'ambiguous')
        self.result['interpretations'] = [interpretation('unused', units=['u2'])]
        self.run_checked(self.membership(), 'verified')

    def circle(self):
        self.add_unit('u2')
        request = s.args('m06.circle_point_equal', angle=s.pi(-19, 4), independent_point_angle=s.pi(5, 4))
        request['context']['unit_ids'].append('u2')
        return request

    def test_circle_self_support_through_semantic_context_is_rejected(self):
        request = self.circle()
        self.result['interpretations'] = [interpretation('i1', units=['u2'], context=['u1'])]
        request['context']['interpretation_ids'] = ['i1']
        request['context']['bindings'][1].update(source_kind='interpretation', source_ref='i1')
        self.run_checked(request, 'unknown', 'source_mismatch')

    def test_circle_self_support_through_fact_chain_is_rejected(self):
        request = self.circle()
        self.result['facts'] = [fact('f0'), fact('f1', units=['u2'], depends=['f0']),
                                fact('f2', units=['u2'], depends=['f1'])]
        request['context']['depends_on_fact_ids'] = ['f2']
        request['context']['bindings'][1].update(source_kind='fact', source_ref='f2')
        self.run_checked(request, 'unknown', 'source_mismatch')

    def test_circle_self_support_through_assumption_source_is_rejected(self):
        request = self.circle()
        self.add_assumption(status='established', origin='student_unit')
        self.result['interpretations'] = [interpretation('i1', units=['u2'], assumptions=['a1'])]
        request['context']['interpretation_ids'] = ['i1']
        request['context']['bindings'][1].update(source_kind='interpretation', source_ref='i1')
        self.run_checked(request, 'unknown', 'source_mismatch')

    def test_circle_independent_chain_is_computed(self):
        request = self.circle()
        self.result['interpretations'] = [interpretation('i1', units=['u2'])]
        self.result['facts'] = [fact('f0', units=['u2'], interpretations=['i1']),
                                fact('f1', units=['u2'], depends=['f0'])]
        request['context']['depends_on_fact_ids'] = ['f1']
        request['context']['bindings'][1].update(source_kind='fact', source_ref='f1')
        self.run_checked(request, 'verified')

    def test_circle_independent_interpretations_may_share_task_premise(self):
        request = self.circle()
        self.add_assumption(status='established', origin='task_statement', source=self.inp['id'])
        self.result['interpretations'] = [interpretation('label', units=['u1'], assumptions=['a1']),
                                          interpretation('point', units=['u2'], assumptions=['a1'])]
        request['context']['interpretation_ids'] = ['label', 'point']
        for binding, ref in zip(request['context']['bindings'], ('label', 'point')):
            binding.update(source_kind='interpretation', source_ref=ref)
        self.run_checked(request, 'verified')

    def test_established_premise_can_reuse_same_scope_independent_fact(self):
        self.result['facts'] = [fact('f0')]
        self.add_assumption(status='established', origin='derived_fact', source='f0')
        output, _, _ = self.run_checked(self.membership(), 'verified')
        self.assertIn('a1', output['assumption_ids'])
        self.assertIn('f0', output['depends_on_fact_ids'])

    def test_established_premise_can_reuse_transitive_independent_proof(self):
        self.result['interpretations'] = [interpretation('i1')]
        self.result['facts'] = [fact('f1', interpretations=['i1']), fact('f0', depends=['f1'])]
        self.add_assumption(status='established', origin='derived_fact', source='f0')
        output, _, _ = self.run_checked(self.membership(), 'verified')
        self.assertIn('a1', output['assumption_ids'])
        self.assertIn('f1', output['depends_on_fact_ids'])

    def test_established_premise_can_reuse_independent_human_interpretation(self):
        item = interpretation('i1')
        item.update(status='human_confirmed', source=dict(kind='human_review', ref='synthetic-human-review'))
        self.result['interpretations'] = [item]
        self.add_assumption(status='established', origin='human_interpretation', source='i1')
        output, _, _ = self.run_checked(self.membership(), 'verified')
        self.assertIn('a1', output['assumption_ids'])
        self.assertIn('i1', output['interpretation_ids'])

    def test_explicit_fact_self_premise_is_still_a_cycle(self):
        self.result['facts'] = [fact('f0', assumptions=['a1'])]
        self.add_assumption(status='established', origin='derived_fact', source='f0')
        with self.assertRaises(P.ContextError):
            P.effective_context(self.membership()['context'], self.result)

    def test_unknown_self_premise_is_not_laundered_as_established(self):
        self.result['facts'] = [fact('f0')]
        self.add_assumption(origin='derived_fact', source='f0')
        with self.assertRaises(P.ContextError):
            P.effective_context(self.membership()['context'], self.result)

    def test_only_left_endpoint_does_not_cover_interval(self):
        request = self.membership()
        request['context']['bindings'][1]['argument_pointer'] = '/interval/left'
        for right in (0, 2):
            with self.subTest(right=right):
                request['arguments']['interval']['right'] = s.pi(right)
                self.run_checked(request, 'unknown', 'source_mismatch')

    def test_both_endpoint_flags_require_sources(self):
        request = self.membership()
        complete = ['/point/coefficient', '/interval/left', '/interval/right',
                    '/interval/left_closed', '/interval/right_closed', '/expected_member']
        for missing in ('/interval/left_closed', '/interval/right_closed'):
            request['context']['bindings'] = [dict(argument_pointer=p, source_kind='unit', source_ref='u1')
                                              for p in complete if p != missing]
            with self.subTest(missing=missing):
                self.run_checked(request, 'unknown', 'source_mismatch')
        request['context']['bindings'] = [dict(argument_pointer=p, source_kind='unit', source_ref='u1') for p in complete]
        self.run_checked(request, 'verified')

    def test_one_rational_leaf_does_not_cover_denominator(self):
        request = self.membership()
        request['context']['bindings'][0]['argument_pointer'] = '/point/coefficient/numerator'
        self.run_checked(request, 'unknown', 'source_mismatch')

    def test_array_sibling_requires_its_own_source(self):
        request = s.args('m06.select_pi_roots', families=[dict(offset=s.q(0), period=s.q(2))],
                        interval=s.pi_interval(0, 2), claimed_roots=[s.pi(0), s.pi(2)])
        request['context']['bindings'][-1]['argument_pointer'] = '/claimed_roots/0'
        self.run_checked(request, 'unknown', 'source_mismatch')

    def test_empty_container_is_a_claim_not_an_implicit_default(self):
        request = s.args('m07.set_equal', left=s.exact_set(), right=s.exact_set())
        request['context']['bindings'][0]['argument_pointer'] = '/left/intervals'
        self.run_checked(request, 'unknown', 'source_mismatch')

    def test_variable_is_not_schema_constant(self):
        request = s.args('m07.denominator_nonzero', expression=dict(
            representation='original_polynomial_quotient', variable='x', numerator=[s.q(1)], denominator=[s.q(1)]), point=s.real(0))
        request['context']['bindings'] = [dict(argument_pointer=p, source_kind='unit', source_ref='u1')
                                          for p in ('/expression/numerator', '/expression/denominator', '/point/coefficient')]
        self.run_checked(request, 'unknown', 'source_mismatch')
        request['context']['bindings'].append(dict(argument_pointer='/expression/variable', source_kind='unit', source_ref='u1'))
        self.run_checked(request, 'verified')

    def test_coincident_endpoints_explanation_covers_both_ends(self):
        for left_closed, right_closed in ((True, False), (False, True), (False, False), (True, True)):
            request = s.args('m06.pi_interval_membership', point=s.pi(1),
                interval=s.pi_interval(1, 1, left_closed, right_closed), expected_member=left_closed and right_closed)
            with self.subTest(left_closed=left_closed, right_closed=right_closed):
                _, _, response = self.run_checked(request, 'verified')
                explanation = response['evidence'][0]
                self.assertIn('обоими', explanation)
                self.assertIn('левый ' + ('включён' if left_closed else 'не включён'), explanation)
                self.assertIn('правый ' + ('включён' if right_closed else 'не включён'), explanation)
                self.assertIn('состоит' if left_closed and right_closed else 'пуст', explanation)

    def test_malformed_operation_types_get_valid_unknown(self):
        envelope = build_request('m06.pi_interval_membership', self.membership(), self.result)
        for invalid in ([], {}, None, True, 13):
            with self.subTest(invalid=invalid):
                response = P.execute({**envelope, 'operation': invalid}, self.result)
                self.assertEqual((response['status'], response['reason_code']), ('unknown', 'unsupported_operation'))
                self.assertEqual(list(P.RESPONSE_SCHEMA.iter_errors(response)), [])

    def test_effective_context_is_deterministic_and_does_not_mutate_input(self):
        self.add_unit('u2')
        self.result['interpretations'] = [interpretation('i1', context=['u2'])]
        request = self.use_interpretation(self.membership())
        original = deepcopy(request['context'])
        first = P.effective_context(request['context'], self.result)
        self.assertEqual(request['context'], original)
        self.assertEqual(first, P.effective_context(request['context'], self.result))
        self.assertEqual(first, P.effective_context(first, self.result))
        self.assertIn('u2', first['context_unit_ids'])

    def test_cross_type_provenance_cycle_is_a_structural_error(self):
        self.add_assumption(status='conditional', origin='human_interpretation', source='i1')
        self.result['interpretations'] = [interpretation('i1', assumptions=['a1'])]
        request = self.use_interpretation(self.membership())
        with self.assertRaises(P.ContextError):
            P.effective_context(request['context'], self.result)
        envelope = build_request(request['operation'], request, self.result)
        response = P.execute(envelope, self.result)
        self.assertEqual((response['status'], response['reason_code']), ('unknown', 'invalid_input'))

    def test_supported_case_join_keeps_child_conditions_in_child_scope(self):
        for bid in ('case-left', 'case-right'):
            self.result['branches'].append(dict(id=bid, parent_id='branch-main', label=bid,
                                               unit_ids=['u1'], assumption_ids=[], status='active'))
            self.add_assumption(aid='a-' + bid, status='conditional', origin='task_statement',
                                source=self.inp['id'], branch=bid)
        self.result['facts'] = [fact('coverage'),
            fact('left', branch='case-left', assumptions=['a-case-left']),
            fact('right', branch='case-right', assumptions=['a-case-right']),
            fact('joined', depends=['coverage', 'left', 'right'])]
        self.result['facts'][-1]['branch_join'] = dict(branch_ids=['case-left', 'case-right'], coverage_fact_id='coverage')
        request = self.membership()
        request['context']['depends_on_fact_ids'] = ['joined']
        for binding in request['context']['bindings']:
            binding.update(source_kind='fact', source_ref='joined')
        output, _, _ = self.run_checked(request, 'verified')
        self.assertEqual(output['assumption_ids'], [])
        self.assertIn('joined', output['depends_on_fact_ids'])
        self.assertNotIn('left', output['depends_on_fact_ids'])
        self.assertNotIn('right', output['depends_on_fact_ids'])


if __name__ == '__main__':
    unittest.main()
