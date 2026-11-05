"""Explicit synthetic fixtures. seal() models completed reevaluation, not a runtime shortcut."""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('ege_contract_boundary', ROOT / 'validate.py')
BOUNDARY = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOUNDARY)


def example(name):
    return json.loads((ROOT / 'examples' / name).read_text())


def seal(source, result):
    """Test author explicitly certifies the synthetic facts for the current fixture."""
    result['input_ref'] = {'id': source['id'], 'revision': source['revision'], 'sha256': BOUNDARY.digest(source)}
    for fact in result['facts']:
        fact['basis_sha256'] = BOUNDARY.reading_digest(result)
    basis = BOUNDARY.assessment_digest(result)
    if result['score']['value'] is not None:
        result['score']['basis_sha256'] = basis
    for feedback in result['feedback']:
        if feedback['status'] == 'ready':
            feedback['basis_sha256'] = basis
    return source, result


def base():
    source, result = example('input_13.json'), example('result_13.json')
    source['id'] = 'synthetic-input'
    source['task']['source_label'] = 'SYNTHETIC — not a student work'
    source['task']['statement'].update(text='а) Решите sin x = 0. б) Найдите корни на [0; π].',
                                      source={'kind': 'synthetic', 'ref': 'fixture:statement'})
    source['pages'] = [dict(source['pages'][-1], id='page-1', asset_id='synthetic-image', sha256='a'*64)]
    source['privacy'] = {'status': 'unchecked', 'reviewer_ref': None}
    source['provenance'] = {'source': {'kind': 'synthetic', 'ref': 'fixture:transport'},
                            'dataset_role': 'synthetic', 'duplicate_group': None}
    result.update(id='synthetic-result', supersedes=None, status='ready', corrections=[], interpretations=[],
                  findings=[], clarifications=[], assumptions=[], relations=[], case_checks=[], scenarios=[],
                  ambiguity_groups=[], human_actions=[], answers=[])
    result['run'].update(id='synthetic-run', mode='demo', source_ref='fixture:transport', calls=[],
                         cost={'status':'not_incurred','rub':'0'})
    result['units'] = [dict(id=f'u{n}', kind='math', literal=literal, reading_status='clear',
                           activity='active', anchors=[{'page_id':'page-1','box':None}],
                           source={'kind':'synthetic','ref':'fixture:literal'}, author='student')
                       for n, literal in enumerate(['sin x = 0','x = πn, n ∈ Z','0 ≤ πn ≤ π ⇒ n ∈ {0;1}; x ∈ {0;π}'], 1)]
    result['branches'] = [dict(id='main', parent_id=None, label='Полная синтетическая попытка',
                               unit_ids=['u1','u2','u3'], assumption_ids=[], status='active')]
    result['criteria'] = [dict(id='criterion',package_version='synthetic-v2',exam_year=2026,exam_number=13,
        source_ref='fixture:rubric',source_sha256='b'*64,locator='Synthetic only',
        rule_text='Синтетическая шкала: обоснованы а и б — 2; только а — 1; иначе 0.',authority='synthetic')]
    result['facts'] = []
    result['requirements'] = []
    for part,uids in [('a',['u1','u2']),('b',['u3'])]:
        result['facts'].append(dict(id='fact-'+part,unit_ids=uids,interpretation_ids=[],claim='Пункт '+part+' обоснован.',
            method='human',status='supported',reason='Синтетическое утверждение для проверки формата, не реальная оценка.',
            tool_version=None,depends_on_fact_ids=[],branch_id='main',assumption_ids=[],context_unit_ids=[],
            claim_kind='completeness',domain='x ∈ R; отбор на [0; π]',basis_sha256='0'*64))
        result['requirements'].append(dict(id='req-'+part,criterion_id='criterion',branch_id='main',requirement='Обосновать пункт '+part,
            status='fulfilled',unit_ids=uids,fact_ids=['fact-'+part],coverage_page_ids=['page-1'],reason='Указана действующая цепочка.'))
    result['score'].update(value=2,criterion_ids=['criterion'],evidence_unit_ids=['u1','u2','u3'],finding_ids=[],
        requirement_ids=['req-a','req-b'],case_check_ids=[],ambiguity_group_ids=[],authority='project_review',
        source_ref='fixture:assessment',rationale='Синтетический пример: оба пункта обоснованы.')
    result['feedback'] = [dict(id='feedback-1',finding_ids=[],unit_ids=['u3'],text='Границы интервала включены.',
        status='ready',audience='student',kind='explanation',basis_sha256=None)]
    result['coverage'] = [dict(page_id='page-1',status='complete',reason='Синтетическая полная страница.',
        score_scope='accounted',score_scope_reason='Весь выбранный объём учтён.')]
    result['issues'] = []
    result['readiness'] = {'score':'ready','feedback':'ready'}
    return seal(source,result)


def question(impact='score', unit='u3'):
    return dict(id='question-1', unit_ids=[unit], anchors=[{'page_id':'page-1','box':None}],
        question='Какой знак написан в этом фрагменте?',options=['+','−'],impact=impact,
        reason='Синтетический вопрос о конкретном фрагменте.',status='open',response=None,actor_ref=None,
        ambiguity_group_id=None)


def finding(category='system_uncertainty', effect='no'):
    return dict(id='finding-1',category=category,unit_ids=['u3'],fact_ids=[],description='Синтетическая проблема.',
        reason='Привязана к проверяемой записи и контексту.',affects_score=effect,criterion_ids=[],
        primary_finding_id=None,status='established',evidence_assertions=[],requirement_ids=[])


def human_action(kind, target, before, after, action_id='action-1'):
    return dict(id=action_id,kind=kind,target_section={'reading_correction':'units',
        'interpretation_change':'interpretations','score_change':'score','feedback_edit':'feedback',
        'report_used':'result'}[kind],target_id=target,before=before,after=after,
        actor_ref='fixture:teacher',actor_role='teacher',at='2026-10-07T12:00:00+03:00',
        source_ref='fixture:human-action',reason='Синтетическое действие.')


def add_correction(result):
    unit=result['units'][0]
    action=human_action('reading_correction',unit['id'],unit['literal'],'sin x = 1')
    result['human_actions'].append(action)
    result['corrections'].append(dict(id='correction-1',unit_id=unit['id'],before=action['before'],after=action['after'],
        actor_ref=action['actor_ref'],at=action['at'],reason=action['reason'],source_ref=action['source_ref'],action_id=action['id']))


def feedback_pending():
    source,result=base()
    unit=deepcopy(result['units'][0]);unit.update(id='extra-note',kind='text',literal=None,reading_status='ambiguous')
    result['units'].append(unit);result['branches'][0]['unit_ids'].append(unit['id'])
    uncertainty=finding();uncertainty.update(unit_ids=[unit['id']],reason='Алгебраическое обоснование а/б независимо; сомнение только о дополнительной записи.')
    result['findings'].append(uncertainty)
    result['status']='needs_fragment';result['readiness']['feedback']='partial'
    result['clarifications']=[question('substantive_feedback',unit['id'])]
    result['feedback'].append(dict(id='feedback-pending',finding_ids=[uncertainty['id']],unit_ids=[unit['id']],
        text='Дополнительное замечание ещё не установлено.',status='pending',audience='teacher',kind='clarification',basis_sha256=None))
    result['coverage'][0].update(status='partial',score_scope_reason='Все существенные шаги прочитаны; дополнительная подпись не меняет доказанный отбор.')
    return seal(source,result)


def omitted_root():
    source,result=base()
    result['units'][2]['literal']='0 ≤ πn ≤ π ⇒ n = 0; x = 0'
    result['facts'][1].update(claim='Корень π пропущен в итоговом отборе.',status='supported')
    result['requirements'][1].update(status='not_fulfilled',reason='Подтверждён пропуск включённой правой границы.')
    error=finding('mathematical_error','yes')
    error.update(fact_ids=['fact-b'],criterion_ids=['criterion'],requirement_ids=['req-b'],
        description='Пропущен корень π.',evidence_assertions=[{'fact_id':'fact-b','expected_status':'supported','connection':'Факт прямо утверждает пропуск требуемого корня.'}])
    result['findings']=[error]
    result['score'].update(value=1,finding_ids=[error['id']],rationale='Обоснован а; в б пропущен корень.')
    result['feedback'][0].update(finding_ids=[error['id']],text='Проверь включённую правую границу: π тоже корень.')
    return seal(source,result)


def robust_readings():
    source,result=base()
    result['units'][1].update(literal=None,reading_status='ambiguous')
    result['facts']=[];result['requirements']=[];result['scenarios']=[]
    for n,description in [(1,'Написано одно семейство x=πn, n∈Z.'),(2,'Написаны две серии x=2πk и x=π+2πk.')]:
        bid=f'branch-{n}';aid=f'premise-{n}';fid=f'fact-{n}';rid=f'req-{n}'
        result['branches'].append(dict(id=bid,parent_id='main',label=description,unit_ids=['u2'],assumption_ids=[aid],status='active'))
        result['assumptions'].append(dict(id=aid,claim=description,origin='hypothesis',source_ref='fixture:reading-'+str(n),status='conditional',branch_id=bid))
        result['facts'].append(dict(id=fid,unit_ids=['u1','u2','u3'],interpretation_ids=[],claim='В этой версии чтения оба пункта обоснованы.',
            method='human',status='supported',reason=description,tool_version=None,depends_on_fact_ids=[],branch_id=bid,
            assumption_ids=[aid],context_unit_ids=['u1','u3'],claim_kind='completeness',domain='x ∈ R; [0;π]',basis_sha256='0'*64))
        result['requirements'].append(dict(id=rid,criterion_id='criterion',branch_id=bid,requirement='Обоснованы оба пункта при этом чтении.',
            status='fulfilled',unit_ids=['u1','u2','u3'],fact_ids=[fid],coverage_page_ids=['page-1'],reason=description))
        result['scenarios'].append(dict(id=f'scenario-{n}',branch_id=bid,unit_ids=['u2'],condition=description,
            status='established',score_value=2,requirement_ids=[rid],criterion_ids=['criterion'],rationale=description+' Отбор 0 и π обоснован.'))
    result['ambiguity_groups']=[dict(id='readings',unit_ids=['u2'],scenario_ids=['scenario-1','scenario-2'],exhaustive=True,
        reason='В синтетическом сценарии остаются ровно два указанных чтения; это декларация проверяющего, не вывод валидатора.')]
    result['score'].update(requirement_ids=['req-1','req-2'],ambiguity_group_ids=['readings'],rationale='При каждом оставшемся чтении 2/2; основания показываются условно.')
    result['status']='needs_fragment'
    q=question('score_basis','u2');q['ambiguity_group_id']='readings';result['clarifications']=[q]
    return seal(source,result)


def blank():
    source,result=base();source['capture']['student_attempt']='blank'
    for key in ('units','facts','feedback','answers'):result[key]=[]
    result['branches'][0]['unit_ids']=[]
    for requirement in result['requirements']:
        requirement.update(status='not_fulfilled',unit_ids=[],fact_ids=[],reason='Полный представленный лист осмотрен: решение отсутствует.')
    result['score'].update(value=0,evidence_unit_ids=[],rationale='Полное условие и все страницы; решение отсутствует.')
    return seal(source,result)


def joined_cases():
    source,result=base()
    # Transport-only illustration of mathematical case analysis, not alternative OCR.
    result['units'][0]['literal']='случай 1: x > 0; случай 2: x ≤ 0'
    for n,label in [(1,'x > 0'),(2,'x ≤ 0')]:
        aid=f'case-assumption-{n}';bid=f'case-{n}';fid=f'case-fact-{n}'
        result['branches'].append(dict(id=bid,parent_id='main',label=label,unit_ids=['u1'],assumption_ids=[aid],status='active'))
        result['assumptions'].append(dict(id=aid,claim=label,origin='student_unit',source_ref='u1',status='conditional',branch_id=bid))
        fact=deepcopy(result['facts'][0]);fact.update(id=fid,branch_id=bid,assumption_ids=[aid],claim='Установлен результат внутри случая '+label)
        result['facts'].append(fact)
    coverage=deepcopy(result['facts'][0]);coverage.update(id='case-coverage',claim='x>0 и x≤0 исчерпывают действительную область.',claim_kind='completeness')
    result['facts'].append(coverage)
    result['facts'][0].update(depends_on_fact_ids=['case-coverage','case-fact-1','case-fact-2'],
        branch_join={'branch_ids':['case-1','case-2'],'coverage_fact_id':'case-coverage'})
    return seal(source,result)
