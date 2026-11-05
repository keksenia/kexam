"""DeepSeek returns literal solution text, then a structured pupil assessment."""
from copy import deepcopy
from pathlib import Path
import json
from jsonschema import Draft202012Validator
from .call_guard import StageRequest, canonical, digest
from .reading_trace import assessment_input as trace_input
from .condition_precheck import MATH_SCHEMA

ROOT = Path(__file__).resolve().parents[1]
VERSION = 'independent-assessment-1.0.0'  # Stable saved-result format; historical jobs remain readable.
CODE_VERSION = 'independent-assessment-code-1.4.1'
SOLVE_VERSION = 'independent-solve-text-1.0.0'
CONFIG_PATH = ROOT / 'planning/m17/DEEPSEEK_DIRECT_CONFIG.json'
PROMPTS = ROOT / 'app/independent_prompts'
CATALOG = ROOT / 'planning/error_types_2026-10-10/catalog.json'
TEXT = {'type': 'string', 'minLength': 1, 'pattern': r'\S'}


def config():
    value = json.loads(CONFIG_PATH.read_text())
    if (value['model'] != 'deepseek-flash' or value['endpoint'] != 'https://api.deepseek.com/chat/completions'
            or value['reasoning_effort'] != 'high' or value['timeout_seconds'] != 300
            or value['automatic_retries'] != 0):
        raise ValueError('deepseek_configuration_invalid')
    return value


def number(task_type):
    if task_type not in {'trigonometric_equation', 'inequality'}:
        raise ValueError('task_type_invalid')
    return 13 if task_type == 'trigonometric_equation' else 15


def object_schema(properties):
    return dict(type='object', properties=properties, required=list(properties), additionalProperties=False)


def solve_schema():
    return object_schema({'solution': TEXT})


def catalog():
    return json.loads(CATALOG.read_text())['types']


def assessment_schema():
    item = object_schema(dict(line={'type': ['string', 'null'], 'pattern': r'^[1-9][0-9]*:[1-9][0-9]*$'},
        type={'enum': [t['id'] for t in catalog()] + ['typo', 'unclassified', 'system_uncertainty']},
        explanation=TEXT, criterion=TEXT, hint=TEXT))
    return object_schema(dict(score={'enum': [0, 1, 2, None]}, reason=TEXT,
                              errors={'type': 'array', 'items': item}))


def validate_solution(value):
    if list(Draft202012Validator(solve_schema()).iter_errors(value)):
        raise ValueError('independent_solution_schema')
    return value


def accept_solution_text(value):
    """Wrap the exact visible text; never parse or repair its mathematics."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError('independent_solution_empty')
    return validate_solution({'solution': value})


def validate_assessment(value, lines):
    if list(Draft202012Validator(assessment_schema()).iter_errors(value)) or type(value.get('score')) is bool:
        raise ValueError('independent_assessment_schema')
    for item in value['errors']:
        if item['line'] is not None and item['line'] not in lines:
            raise ValueError('independent_assessment_line')
    return value


def reading_input(reading):
    """Keep literal entries, uncertainty and diagrams; never infer mathematical links."""
    lines = []
    for page in reading['pages']:
        for n, entry in enumerate(page['entries'], 1):
            lines.append(dict(line=f"{page['page']}:{n}", **deepcopy(entry)))
    return dict(lines=lines, pages=[dict(page=p['page'], coverage=deepcopy(p['coverage'])) for p in reading['pages']],
        interpretations=deepcopy(reading.get('interpretations', [])),
        branches=deepcopy(reading.get('branches', [])), assumptions=deepcopy(reading.get('assumptions', [])),
        links=deepcopy(reading.get('links', [])))


def criteria(task_type):
    n = number(task_type)
    if n == 13:
        return json.loads((ROOT / 'planning/m02/criteria.json').read_text())
    return json.loads((ROOT / 'planning/m03/rules.json').read_text())['criteria']


def request(stage, task_type, condition, *, solution=None, reading=None, code_answer=None, reader_trace=None, source_guard):
    """Build from a whitelist: solving has no route for key, reading or pupil data."""
    c = config(); n = number(task_type)
    if stage not in {'solve', 'compare'} or not isinstance(condition, str) or not condition.strip():
        raise ValueError('independent_request_input')
    # Task 13 has one ordered instruction; task 15 and solving retain their prompts.
    system = (PROMPTS / f'{stage}_{n}.txt').read_text()
    if stage != 'compare' or n != 13:
        system = (PROMPTS / f'{stage}_common.txt').read_text() + '\n' + system
    data = dict(task_number=n, condition=condition)
    response_schema = None
    if stage == 'compare':
        validate_solution(solution)
        if not isinstance(reading, dict): raise ValueError('independent_reading_missing')
        data.update(independent_solution=solution['solution'], student=reading_input(reading), criteria=criteria(task_type),
                    error_types=[{k: t[k] for k in ('id', 'name', 'confirm_if', 'do_not_apply_if', 'wrong_example', 'correct_contrast')}
                                 for t in catalog()])
        if reader_trace is not None:
            data['reader_diagnostics'] = trace_input(reader_trace)
        if code_answer is not None:
            if (not isinstance(code_answer, dict) or set(code_answer) != {'scope', 'answer', 'math'}
                    or code_answer['scope'] != ('13a' if n == 13 else '15')
                    or not isinstance(code_answer['answer'], str) or not code_answer['answer'].strip()
                    or not isinstance(code_answer['math'], dict)
                    or list(Draft202012Validator(MATH_SCHEMA).iter_errors(code_answer['math']))):
                raise ValueError('independent_code_answer_invalid')
            data['code_answer'] = deepcopy(code_answer)
        response_schema = assessment_schema()
    elif solution is not None or reading is not None or code_answer is not None or reader_trace is not None:
        raise ValueError('independent_solver_reference_forbidden')
    if stage == 'compare':
        system += '\nВерни только JSON по схеме, без Markdown:\n' + canonical(response_schema)
    payload = dict(model=c['model'], messages=[dict(role='system', content=system), dict(role='user', content=canonical(data))],
        reasoning_effort='high', thinking={'type': 'enabled'}, response_format={'type': 'text' if stage == 'solve' else 'json_object'},
        max_tokens=c['max_tokens'], stream=False)
    if len(canonical(payload).encode()) > 8388608: raise ValueError('http_body_limit')
    context = dict(source_guard=source_guard, operation=stage, content_sha256=digest(data),
                   prompt_sha256=digest(system), configuration_sha256=digest(c))
    if stage == 'solve':
        context.update(response_kind='solution_text', saved_solution_schema_sha256=digest(solve_schema()))
    else:
        context['response_schema_sha256'] = digest(response_schema)
    return StageRequest(stage='other' if stage == 'solve' else 'grading', provider='deepseek', payload=payload,
        context=context,
        versions=dict(contract='independent-1', pipeline=VERSION, prompt=SOLVE_VERSION if stage == 'solve' else VERSION,
                      code=CODE_VERSION, validator=SOLVE_VERSION if stage == 'solve' else VERSION),
        condition_complete=True)
