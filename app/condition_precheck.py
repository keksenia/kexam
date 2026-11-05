"""Bounded, optional symbolic precheck. No model, network or pupil data."""
from copy import deepcopy
import re
from .live_condition import solve_condition, parse_expression

VERSION = 'condition-precheck-1.2.0'
INTERVAL_SCHEMA = dict(type=['object', 'null'], properties={
    'left': {'type': 'string', 'maxLength': 512}, 'right': {'type': 'string', 'maxLength': 512},
    'left_open': {'type': 'boolean'}, 'right_open': {'type': 'boolean'}},
    required=['left', 'right', 'left_open', 'right_open'], additionalProperties=False)
MATH_SCHEMA = dict(type=['object', 'null'], properties={
    'lhs': {'type': 'string', 'minLength': 1, 'maxLength': 512},
    'rhs': {'type': 'string', 'minLength': 1, 'maxLength': 512},
    'relation': {'enum': ['=', '<', '<=', '>', '>=']},
    'interval': INTERVAL_SCHEMA,
    'restrictions': {'type': 'array', 'maxItems': 12, 'items': {'type': 'string', 'maxLength': 512}}},
    required=['lhs', 'rhs', 'relation', 'interval', 'restrictions'], additionalProperties=False)
QUESTIONS = {
    'interval_order': 'Проверьте порядок и знаки границ промежутка пункта б.',
    'undefined_expression': 'Проверьте дроби и основания функций в условии: код обнаружил неопределённое выражение.',
    'empty_domain': 'Код получил пустую область допустимых значений. Подтвердите или исправьте условие.',
    'empty_real_answer': 'Код не нашёл действительных решений исходного условия. Подтвердите или исправьте условие.'}


def from_text(text, task_type):
    """Accept only a whole simple machine-readable statement; never drop prose."""
    text = text.strip().replace('−', '-').replace('≤', '<=').replace('≥', '>=').replace('π', 'pi')
    # This is intentionally narrow. Unknown prose/LaTeX falls through to DeepSeek.
    found = re.fullmatch(r'(.+?)\s*(<=|>=|=|<|>)\s*([^;\n]+?)(?:;\s*x\s*(?:in|∈)\s*([\[(])\s*([^;]+);\s*([^\])]+)\s*([\])]))?', text)
    if not found: return None
    lhs, relation, rhs, opening, left, right, closing = found.groups()
    interval = None if opening is None else dict(left=left.strip(), right=right.strip(),
        left_open=opening == '(', right_open=closing == ')')
    try:
        for expression in (lhs.strip(), rhs.strip()): parse_expression(expression)
        if interval:
            for field in ('left', 'right'): parse_expression(interval[field])
    except (ValueError, SyntaxError): return None
    return dict(lhs=lhs.strip(), rhs=rhs.strip(), relation=relation, interval=interval, restrictions=[])


def check(math, task_type, *, solver=solve_condition):
    if math is None: return dict(status='unsupported', reason='structured_condition_unavailable', version=VERSION)
    value = deepcopy(math)
    try:
        from jsonschema import Draft202012Validator
        if list(Draft202012Validator(MATH_SCHEMA).iter_errors(value)):
            return dict(status='unsupported', reason='structured_condition_invalid', version=VERSION)
        for field in ('lhs', 'rhs'): parse_expression(value[field])
        for restriction in value['restrictions']: parse_expression(restriction, restriction=True)
        if value['interval']:
            for field in ('left', 'right'): parse_expression(value['interval'][field])
        if task_type == 'trigonometric_equation' and value['relation'] != '=':
            return dict(status='task_type_mismatch', reason='equation_profile', version=VERSION,
                question='Выбран №13, но в условии записано неравенство. Исправьте условие или сохраните работу с верным номером задания.')
        if task_type == 'inequality' and value['relation'] == '=':
            return dict(status='task_type_mismatch', reason='inequality_profile', version=VERSION,
                question='Выбран №15, но в условии записано уравнение. Исправьте условие или сохраните работу с верным номером задания.')
        if task_type == 'inequality' and value['interval'] is not None:
            return dict(status='unsupported', reason='inequality_interval_not_supported', version=VERSION)
    except (ValueError, SyntaxError, TypeError):
        return dict(status='unsupported', reason='expression_syntax_not_supported', version=VERSION)
    input_math = deepcopy(value)
    value.update(status='clear', task_type=task_type, precheck_only=True)
    result = solver(value, timeout=2)
    scope = '13a' if task_type == 'trigonometric_equation' else '15'
    result = dict(result, scope=scope, precheck_version=VERSION, input_math=input_math)
    # A failure of the solver is never evidence of a malformed condition.
    if result.get('status') == 'solved':
        if result.get('domain') == 'EmptySet': result.update(status='suspicious', reason='empty_domain')
        elif result.get('empty') is True: result.update(status='suspicious', reason='empty_real_answer')
    if result.get('status') == 'suspicious':
        if result.get('reason') not in QUESTIONS:
            result.update(status='unsupported')
        else: result['question'] = QUESTIONS[result['reason']]
    return result


def reference(result):
    if (result.get('status') != 'solved' or not isinstance(result.get('answer'), str)
            or not isinstance(result.get('input_math'), dict)): return None
    return dict(scope=result['scope'], answer=result['answer'], math=deepcopy(result['input_math']))
