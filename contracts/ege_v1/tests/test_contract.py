"""Offline transport invariants and counterexamples. No provider, OCR or math solver."""
from copy import deepcopy
import unittest
from fixtures import (BOUNDARY, example, seal, base, question, finding, human_action,
                      add_correction, feedback_pending, omitted_root, robust_readings, blank, joined_cases)


class ContractBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.source, self.result = base()

    def valid(self, result=None, source=None):
        self.assertEqual(BOUNDARY.validate(result or self.result, source or self.source), [])

    def invalid(self, fragment=None, result=None, source=None):
        errors = BOUNDARY.validate(result or self.result, source or self.source)
        self.assertTrue(errors, 'Invalid contract was accepted')
        if fragment:
            self.assertTrue(any(fragment in error for error in errors), errors)

    def test_real_examples_and_synthetic_states_validate(self):
        for number in (13, 15):
            source,result=example(f'input_{number}.json'),example(f'result_{number}.json')
            self.assertEqual(BOUNDARY.validate(source),[])
            self.valid(result,source)
        for case in example('states.json')['examples']:
            self.valid(case['result'],case['input'])
        for make in (base,feedback_pending,omitted_root,robust_readings,blank,joined_cases):
            with self.subTest(make=make.__name__):
                source,result=make();self.valid(result,source)

    def test_result_requires_exact_input(self):
        self.assertEqual(BOUNDARY.validate(self.result),['result: exact source input is required'])

    def test_changed_photo_statement_revision_break_input_binding(self):
        for change in ('photo','statement','revision'):
            with self.subTest(change=change):
                source=deepcopy(self.source)
                if change=='photo':source['pages'][0]['sha256']='0'*64
                elif change=='statement':source['task']['statement']['text']+=' Changed.'
                else:source['revision']+=1
                self.invalid('input identity/revision/hash mismatch',source=source)

    def test_old_contract_is_not_silently_accepted(self):
        self.result['schema_version']='1.0.0';self.invalid('const')

    def test_incomplete_input_cannot_receive_score(self):
        for which,states in [('statement',['unchecked','incomplete','conflicting']),('pages',['unknown','missing'])]:
            for state in states:
                source,result=base()
                if which=='statement':source['task']['statement']['completeness']=state
                else:source['capture']['pages_complete']=state
                seal(source,result)
                self.invalid('input completeness unconfirmed',result,source)

    def test_unprocessed_page_cannot_be_declared_immaterial(self):
        for status in ('unreadable','not_processed'):
            result=deepcopy(self.result);result['coverage'][0]['status']=status
            self.invalid('unread page cannot be declared immaterial',result)

    def test_partial_scope_without_accounting_blocks_score(self):
        self.result['coverage'][0].update(status='partial',score_scope='unresolved')
        self.invalid('material reading scope unaccounted')

    def test_second_page_cannot_disappear_from_coverage(self):
        page=deepcopy(self.source['pages'][0]);page.update(id='page-2',asset_id='asset-2',order=2)
        self.source['pages'].append(page);seal(self.source,self.result)
        self.invalid('each solution page requires a status')

    def test_missing_or_task_sheet_anchor_rejected(self):
        self.result['units'][0]['anchors'][0]['page_id']='other-page';self.invalid('unknown page')
        self.result['units'][0]['anchors'][0]['page_id']='page-1'
        self.source['pages'][0]['role']='task';seal(self.source,self.result)
        self.invalid('student evidence must refer to solution pages')

    def test_order_duplicate_ids_and_region_rejected(self):
        self.source['pages'][0]['order']=2
        self.assertIn('pages: per-role order must be contiguous and in array order',BOUNDARY.validate(self.source))
        self.source['pages'][0]['order']=1
        self.result['units'].append(deepcopy(self.result['units'][0]));self.invalid('duplicate identifiers')
        self.source,self.result=base();self.source['pages'][0]['region']=[0,.5,1,1]
        self.result['units'][0]['anchors'][0]['box']=[0,0,.4,.4];seal(self.source,self.result)
        self.invalid('outside selected problem region')

    def test_wrong_rubric_year_or_task_rejected(self):
        for field,value in [('exam_year',2027),('exam_number',15)]:
            result=deepcopy(self.result);result['criteria'][0][field]=value
            self.invalid('const' if field=='exam_year' else 'wrong year/task',result)

    def test_ready_score_survives_pending_additional_feedback(self):
        self.source,self.result=feedback_pending();self.valid()
        self.assertEqual(self.result['score']['value'],2)
        self.assertEqual(self.result['readiness']['feedback'],'partial')

    def test_score_changing_question_blocks_grade(self):
        for impact in ('score','both'):
            self.source,self.result=feedback_pending()
            self.result['clarifications'][0]['impact']=impact
            self.invalid('unresolved score-changing clarification')

    def test_pending_feedback_cannot_be_sent_to_student(self):
        self.source,self.result=feedback_pending()
        self.result['feedback'][-1]['audience']='student';self.invalid('unfinished claim cannot be sent to student')

    def test_ready_feedback_cannot_assert_pending_finding(self):
        f=finding();f['status']='pending';self.result['findings']=[f]
        self.result['feedback'][0]['finding_ids']=[f['id']];seal(self.source,self.result)
        self.invalid('pending finding cannot be presented as established')

    def test_ready_summary_requires_both_parts(self):
        self.source,self.result=feedback_pending();self.result['status']='ready';self.invalid('both parts must be ready')

    def test_three_classes_and_supported_omission_are_representable(self):
        self.source,self.result=omitted_root();self.valid()
        self.result['findings'][0]['category']='solution_deficiency';seal(self.source,self.result);self.valid()
        self.assertEqual(self.result['facts'][1]['status'],'supported')

    def test_unknown_fact_cannot_establish_student_error(self):
        self.source,self.result=omitted_root();self.result['facts'][1]['status']='unknown';seal(self.source,self.result)
        self.invalid('assertion polarity is not supported')

    def test_contradicted_claim_can_support_error_with_explicit_connection(self):
        self.source,self.result=omitted_root()
        self.result['facts'][1].update(claim='Отбор содержит все корни.',status='contradicted')
        self.result['findings'][0]['evidence_assertions'][0].update(expected_status='contradicted',connection='Опровергнута полнота отбора.')
        seal(self.source,self.result);self.valid()

    def test_system_doubt_cannot_penalize(self):
        self.source,self.result=omitted_root();self.result['findings'][0]['category']='system_uncertainty';seal(self.source,self.result)
        self.invalid('system uncertainty cannot penalize student')

    def test_learning_note_is_feedback_not_penalty_class(self):
        f=finding();f['category']='learning_note';self.result['findings']=[f];self.invalid('enum')

    def test_penalty_cannot_reference_nonpenalizing_finding(self):
        self.result['findings']=[finding()];self.result['score']['finding_ids']=['finding-1'];seal(self.source,self.result)
        self.invalid('penalty reasons cannot be learning notes or system doubts')

    def test_missing_requirement_evidence_is_not_fulfilled(self):
        self.result['requirements'][0]['fact_ids']=[];seal(self.source,self.result)
        self.invalid('fulfillment needs student evidence and checked facts')

    def test_unknown_requirement_cannot_support_current_score(self):
        self.result['requirements'][0]['status']='unknown';seal(self.source,self.result)
        self.invalid('unresolved selected requirement')

    def test_absence_requires_fact_or_fully_inspected_scope(self):
        req=self.result['requirements'][0];req.update(status='not_fulfilled',unit_ids=[],fact_ids=[],coverage_page_ids=[])
        seal(self.source,self.result);self.invalid('absence needs checked facts or full inspected scope')

    def test_blank_full_attempt_can_score_zero_without_fake_writing(self):
        self.source,self.result=blank();self.valid()
        self.source['capture']['pages_complete']='missing';seal(self.source,self.result)
        self.invalid('absence needs checked facts or full inspected scope')

    def test_teacher_template_or_crossed_out_writing_not_positive_student_evidence(self):
        for author,activity in [('teacher','active'),('template','active'),('unknown','active'),('student','crossed_out')]:
            self.source,self.result=base();self.result['units'][0].update(author=author,activity=activity)
            seal(self.source,self.result);self.invalid('active student evidence required')

    def test_assumption_requires_source_and_appropriate_branch(self):
        self.source,self.result=robust_readings()
        self.result['facts'][0]['assumption_ids']=['premise-2'];seal(self.source,self.result)
        self.invalid('assumption from unrelated branch')

    def test_system_hypothesis_cannot_be_declared_established(self):
        self.source,self.result=robust_readings();self.result['assumptions'][0]['status']='established';seal(self.source,self.result)
        self.invalid('system hypothesis is not established')

    def test_conditional_evidence_needs_scenarios_before_current_score(self):
        self.source,self.result=robust_readings();self.result['score']['ambiguity_group_ids']=[]
        self.result['clarifications']=[];self.result['status']='ready';seal(self.source,self.result)
        self.invalid('conditional evidence needs exhaustive independently scored scenarios')

    def test_human_premise_cannot_use_proposed_model_interpretation(self):
        self.result['interpretations']=[dict(id='interpretation',unit_ids=['u1'],meaning='x > 0',status='proposed',
            source={'kind':'model_output','ref':'fixture:model'},branch_id='main',assumption_ids=[],context_unit_ids=[])]
        self.result['assumptions']=[dict(id='assumption',claim='x > 0',origin='human_interpretation',source_ref='interpretation',status='established',branch_id='main')]
        seal(self.source,self.result);self.invalid('human premise needs human-confirmed interpretation')

    def test_supported_fact_cannot_depend_on_unknown_premise(self):
        self.source,self.result=robust_readings();self.result['assumptions'][0]['status']='unknown';seal(self.source,self.result)
        self.invalid('unresolved premise cannot prove claim')

    def test_cyclic_fact_or_branch_dependencies_rejected(self):
        self.result['facts'][0]['depends_on_fact_ids']=['fact-b'];self.result['facts'][1]['depends_on_fact_ids']=['fact-a'];seal(self.source,self.result)
        self.invalid('cyclic dependencies')
        self.source,self.result=robust_readings();self.result['branches'][0]['parent_id']='branch-1';seal(self.source,self.result)
        self.invalid('cyclic parents')

    def test_root_cause_cycle_rejected(self):
        a=finding();b=dict(deepcopy(a),id='finding-2');a['primary_finding_id']=b['id'];b['primary_finding_id']=a['id']
        self.result['findings']=[a,b];seal(self.source,self.result);self.invalid('cyclic root causes')

    def test_math_case_join_discharges_local_conditions(self):
        self.source,self.result=joined_cases();self.valid()
        del self.result['facts'][0]['branch_join'];seal(self.source,self.result)
        self.invalid('dependency from unrelated branch')

    def test_case_join_cannot_hide_unproved_coverage_or_ocr_guess(self):
        self.source,self.result=joined_cases()
        self.result['facts'][-1]['status']='unknown';seal(self.source,self.result)
        self.invalid('branch join needs supported parent-scope coverage fact')
        self.source,self.result=joined_cases();self.result['assumptions'][0].update(origin='hypothesis',source_ref='fixture:guess')
        seal(self.source,self.result);self.invalid('conditional evidence needs exhaustive independently scored scenarios')

    def test_same_score_different_grounds_allowed(self):
        self.source,self.result=robust_readings();self.valid()
        self.assertNotEqual(self.result['scenarios'][0]['rationale'],self.result['scenarios'][1]['rationale'])

    def test_different_or_unknown_alternative_score_blocks_current_score(self):
        for change in ('different','unknown','not_exhaustive'):
            self.source,self.result=robust_readings()
            if change=='different':self.result['scenarios'][1]['score_value']=1
            elif change=='unknown':self.result['scenarios'][1].update(status='unknown',score_value=None)
            else:self.result['ambiguity_groups'][0]['exhaustive']=False
            seal(self.source,self.result);self.invalid('every remaining plausible reading must independently justify the same score')

    def test_question_cannot_use_unrelated_robust_group(self):
        self.source,self.result=robust_readings();self.result['clarifications'][0]['unit_ids']=['u3']
        self.invalid('question outside alternative-reading scope')

    def test_group_cannot_hide_uncovered_ambiguous_unit(self):
        self.source,self.result=robust_readings();self.result['ambiguity_groups'][0]['unit_ids'].append('u3')
        self.result['units'][2]['reading_status']='ambiguous';seal(self.source,self.result)
        self.invalid('scenarios do not cover every source unit')

    def test_similar_corner_case_does_not_automatically_apply(self):
        case=dict(id='case-1',requirement_ids=['req-a'],source_ref='fixture:case',source_sha256='c'*64,
            locator='Synthetic contrast',exam_year=2026,status='not_applicable',fact_ids=['fact-a'],matches=[],
            exclusions=['Иной математический механизм.'],contrast_ref='fixture:contrast',reason='Сходство названия недостаточно.')
        self.result['case_checks']=[case];seal(self.source,self.result);self.valid()
        self.result['score']['case_check_ids']=['case-1'];self.invalid('unestablished corner-case applicability')
        case.update(status='applicable',matches=['Предположительно похоже.']);seal(self.source,self.result)
        self.invalid('applicability requires matches, evidence and no excluding conditions')

    def test_human_reading_edit_invalidates_facts_score_and_feedback(self):
        add_correction(self.result)
        self.invalid('stale reading/context snapshot')
        self.invalid('stale assessment snapshot')
        self.invalid('stale assessment basis')

    def test_correction_chain_retains_literal_and_requires_scoped_action(self):
        original=self.result['units'][0]['literal'];add_correction(self.result)
        correction=deepcopy(self.result['corrections'][0]);correction.update(id='correction-2',before=correction['after'],after='sin x = -1',action_id='action-2')
        action=human_action('reading_correction','u1',correction['before'],correction['after'],'action-2')
        self.result['human_actions'].append(action);self.result['corrections'].append(correction)
        seal(self.source,self.result);self.valid();self.assertEqual(self.result['units'][0]['literal'],original)
        correction['before']=original;self.invalid('broken before/after chain')

    def test_missing_action_cannot_attest_reading_change(self):
        add_correction(self.result);self.result['human_actions']=[];seal(self.source,self.result)
        self.invalid('matching scoped human action required')

    def test_findings_change_invalidates_score_and_ready_feedback(self):
        self.source,self.result=omitted_root()
        self.result['findings'][0]['description']='Существенно другое обвинение.'
        self.invalid('stale assessment snapshot')
        self.invalid('stale assessment basis')

    def test_material_coverage_change_invalidates_assessment(self):
        self.result['coverage'][0]['score_scope_reason']='Иное обоснование существенности.'
        self.invalid('stale assessment snapshot')

    def test_wording_edit_chain_does_not_require_new_ocr_or_grade(self):
        before=self.result['feedback'][0]['text'];reading_hash=BOUNDARY.reading_digest(self.result);grade_hash=BOUNDARY.assessment_digest(self.result)
        self.result['feedback'][0]['text']='Последняя формулировка.'
        self.result['human_actions']=[human_action('feedback_edit','feedback-1',before,'Промежуточная.','edit-1'),
            human_action('feedback_edit','feedback-1','Промежуточная.','Последняя формулировка.','edit-2')]
        self.valid();self.assertEqual(reading_hash,BOUNDARY.reading_digest(self.result));self.assertEqual(grade_hash,BOUNDARY.assessment_digest(self.result))
        self.result['human_actions'][1]['before']='Неверное прошлое.';self.invalid('broken before/after chain')

    def test_report_used_does_not_confirm_model_facts_or_score(self):
        self.result['score'].update(status='proposed',authority='model')
        self.result['facts'][0]['method']='model';seal(self.source,self.result)
        self.result['human_actions']=[human_action('report_used',self.result['id'],False,True)]
        self.valid();self.assertEqual(self.result['score']['status'],'proposed')
        self.result['review']={'status':'accepted'};self.invalid('additionalProperties')

    def test_stale_score_is_hidden_with_previous_result_link(self):
        self.result.update(status='needs_review',supersedes='previous-result',issues=[{'code':'other','message':'После правки нужна проверка.','retryable':False,'blocks':['score']}])
        self.result['score'].update(status='stale',value=None)
        self.result['readiness']['score']='stale';self.valid()
        self.result['score']['value']=2;self.invalid('status/value mismatch')

    def test_partial_failure_preserves_independent_score(self):
        self.result.update(status='processing_error',issues=[{'code':'timeout','message':'Сбой этапа дополнительных комментариев.','retryable':True,'blocks':['feedback']}])
        self.result['readiness']['feedback']='partial';self.valid()
        self.result['issues'][0]['blocks'].append('score');self.invalid('blocking issues remain')

    def test_schema_rejects_invalid_timestamp_and_unknown_field(self):
        self.result['created_at']='yesterday';self.invalid('format')
        self.result['created_at']='2026-10-07T12:00:00+03:00';self.result['fake_acceptance']=True;self.invalid('additionalProperties')

    @staticmethod
    def call(call_id='call-1',cost='0.1',retry_of=None):
        return dict(id=call_id,stage='grading',provider='synthetic',model_id='synthetic',settings={},
            prompt_sha256='a'*64,request_sha256='b'*64,prompt_ref='fixture:prompt',request_ref='fixture:request',
            response_ref='fixture:response',status='succeeded',retry_of=retry_of,
            usage={'input_tokens':10,'output_tokens':20,'reasoning_tokens':None,'reasoning_in_output':None},
            cost_rub=cost,cost_basis='provider_report',tariff_ref=None,latency_ms=10)

    def test_unknown_usage_never_becomes_zero_cost(self):
        self.result['run']['mode']='imported'
        call=self.call();call.update(cost_basis='unknown',cost_rub=None)
        self.result['run'].update(calls=[call],cost={'status':'unknown','rub':None});self.valid()
        call['cost_rub']='0';self.invalid('cost basis/value mismatch')

    def test_unknown_attempt_prevents_known_total(self):
        self.result['run']['mode']='imported';call=self.call();call.update(cost_basis='unknown',cost_rub=None)
        self.result['run'].update(calls=[call],cost={'status':'known','rub':'0'});self.invalid('unknown attempt hidden in total')

    def test_retries_include_failed_attempt_cost_and_cannot_reference_future(self):
        self.result['run']['mode']='imported'
        first=self.call();first.update(status='failed',response_ref=None)
        second=self.call('call-2','0.2','call-1')
        self.result['run'].update(calls=[first,second],cost={'status':'known','rub':'0.3'});self.valid()
        self.result['run']['cost']['rub']='0.2';self.invalid('total must include all attempts exactly')
        self.result['run']['cost']['rub']='0.3';first['retry_of']='call-2';self.invalid('retry must refer to earlier call')

    def test_exact_code_requires_operation_evidence_and_version(self):
        self.result['facts'][0]['method']='exact_code';seal(self.source,self.result);self.invalid('code needs operation, evidence and version')
        self.result['facts'][0].update(tool_version='fixture-1',code_check={'operation':'new_supported_operation','arguments':{},'evidence':'Synthetic witness.'})
        seal(self.source,self.result);self.valid()

    def test_live_model_score_requires_grading_call_and_official_rubric(self):
        self.result['run']['mode']='live';self.result['score'].update(status='proposed',authority='model')
        self.invalid('live model assessment lacks a successful grading call')
        self.result['run'].update(calls=[self.call()],cost={'status':'known','rub':'0.1'})
        self.invalid('live result lacks official rubric')

    def test_infinite_endpoint_open_and_nonzero_pi_period(self):
        source,result=example('input_13.json'),example('result_13.json')
        result['answers'][1]['exact_set']={'points':[],'intervals':[{'left':None,'right':None,'left_closed':False,'right_closed':False}]}
        self.valid(result,source)
        result['answers'][1]['exact_set']['intervals'][0]['left_closed']=True;self.invalid('infinity cannot be closed',result,source)
        result['answers'][1]['exact_set']['intervals'][0]['left_closed']=False
        result['answers'][0]['pi_families'][0]['period']['numerator']='0';self.invalid('zero family period',result,source)


if __name__ == '__main__':
    unittest.main()
