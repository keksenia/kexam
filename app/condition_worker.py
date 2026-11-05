"""Isolated symbolic condition solver. No eval, string sympify or pupil solution."""
import ast
import json
import sys

try:
    import resource
    resource.setrlimit(resource.RLIMIT_CPU, (6, 8))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
except (ImportError, ValueError, OSError):
    pass  # Parent still imposes a wall deadline and kills the worker on timeout.

VERSION = "condition-solver-1.1.3"


def solve(condition):
    import sympy as s
    import mpmath
    from sympy.calculus.util import periodicity
    if s.__version__ != "1.14.0" or mpmath.__version__ != "1.3.0":
        return dict(status="technical_error", reason="solver_runtime_version", version=VERSION)
    x = s.Symbol("x", real=True)
    denominators, domain_conditions = [], []
    periodic_constraints = []
    relations = {"=": s.Eq, "!=": s.Ne, "<": s.Lt, "<=": s.Le, ">": s.Gt, ">=": s.Ge}
    comparison_ops = {ast.Eq:"=", ast.NotEq:"!=", ast.Lt:"<", ast.LtE:"<=", ast.Gt:">", ast.GtE:">="}

    def expression(text, *, restriction=False):
        if not isinstance(text, str) or not 0 < len(text) <= 512:
            raise ValueError("expression_size")
        if restriction:
            import re
            text = re.sub(r"(?<![<>=!])=(?!=)", "==", text)
        tree = ast.parse(text, mode="eval")
        if sum(1 for _ in ast.walk(tree)) > 120:
            raise ValueError("expression_complexity")

        def visit(n, depth=0):
            if depth > 24: raise ValueError("expression_depth")
            if isinstance(n, ast.Constant) and type(n.value) is int and abs(n.value) <= 10000:
                return s.Integer(n.value)
            if isinstance(n, ast.Name) and n.id in {"x", "pi", "e"}:
                return {"x":x, "pi":s.pi, "e":s.E}[n.id]
            if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.UAdd, ast.USub)):
                value = visit(n.operand, depth+1)
                return value if isinstance(n.op, ast.UAdd) else -value
            if isinstance(n, ast.BinOp):
                a, b = visit(n.left, depth+1), visit(n.right, depth+1)
                if isinstance(n.op, ast.Add): return s.Add(a, b, evaluate=False)
                if isinstance(n.op, ast.Sub): return s.Add(a, -b, evaluate=False)
                if isinstance(n.op, ast.Mult): return s.Mul(a, b, evaluate=False)
                if isinstance(n.op, ast.Div):
                    denominators.append(b)
                    return s.Mul(a, s.Pow(b, -1, evaluate=False), evaluate=False)
                if isinstance(n.op, ast.Pow):
                    b = s.cancel(b)
                    if b.is_Rational and abs(b) <= 12 and b.q <= 12:
                        if not b.is_Integer and b.q % 2:
                            raise ValueError("odd_variable_radical_not_supported")
                        # Rational noninteger powers use the real nonnegative base.
                        # Odd roots of negative numbers need explicit support; never guess.
                        if not b.is_Integer: domain_conditions.append((a, s.S.Zero, ">=" if b > 0 else ">"))
                        if b < 0: denominators.append(a)
                        return s.Pow(a, b, evaluate=False)
                    if b.has(x) and not a.has(x) and a.is_positive is True and a != 1:
                        return s.Pow(a, b, evaluate=False)
                    raise ValueError("power_not_supported")
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and not n.keywords:
                name = n.func.id
                if name == "log" and len(n.args) in (1, 2):
                    a = visit(n.args[0], depth+1)
                    domain_conditions.append((a, s.S.Zero, ">"))
                    value = s.log(a, evaluate=False)
                    if len(n.args) == 2:
                        b = visit(n.args[1], depth+1)
                        domain_conditions.extend([(b, s.S.Zero, ">"), (b, s.S.One, "!=")])
                        denominator = s.log(b, evaluate=False)
                        denominators.append(denominator)
                        value = s.Mul(value, s.Pow(denominator, -1, evaluate=False), evaluate=False)
                    return value
                if name in {"sin", "cos", "tan", "sqrt", "exp"} and len(n.args) == 1:
                    a = visit(n.args[0], depth+1)
                    if name == "sqrt":
                        domain_conditions.append((a, s.S.Zero, ">="))
                        return s.Pow(a, s.Rational(1,2), evaluate=False)
                    if name == "tan": denominators.append(s.cos(a))
                    return getattr(s, name)(a)
            raise ValueError("expression_syntax_not_supported")

        if restriction:
            n = tree.body
            if not isinstance(n, ast.Compare) or not 1 <= len(n.ops) <= 3:
                raise ValueError("restriction_syntax_not_supported")
            terms = [visit(v) for v in [n.left] + n.comparators]
            if any(type(op) not in comparison_ops for op in n.ops): raise ValueError("restriction_relation")
            return [(terms[i], terms[i+1], comparison_ops[type(op)]) for i, op in enumerate(n.ops)]
        return visit(tree.body)

    def explicit(answer):
        if answer in (s.S.Reals, s.S.EmptySet): return True
        if isinstance(answer, (s.Interval, s.FiniteSet)): return not answer.has(s.ConditionSet)
        return isinstance(answer, s.Union) and all(explicit(v) for v in answer.args)

    def rational_answer(expr, variable, relation):
        numerator, denominator = s.fraction(s.cancel(expr))
        for polynomial in (numerator, denominator):
            p = s.Poly(polynomial, variable, extension=True)
            if p.degree() > 12 or any(c.is_real is not True for c in p.all_coeffs()):
                raise ValueError("polynomial_not_supported")
        return s.solve_univariate_inequality(relations[relation](expr, 0), variable, relational=False)

    def compare(a, b, relation, *, substitutions=True):
        expr = s.cancel(a-b)
        if expr.has(s.zoo, s.nan, s.oo, -s.oo): raise ValueError("undefined_expression")
        if relation in ("=", "!="):
            zeros = s.solveset(expr, x, domain=s.S.Reals)
            if zeros.has(s.ConditionSet): raise ValueError("domain_not_solved")
            return zeros if relation == "=" else s.S.Reals - zeros
        if expr.is_rational_function(x): return rational_answer(expr, x, relation)
        # An exact substitution for a rational expression in ONE logarithm.
        logs = [v for v in expr.atoms(s.log) if v.has(x)]
        if substitutions and len(logs) == 1:
            u = s.Symbol("u", real=True)
            reduced = expr.xreplace({logs[0]: u})
            if not reduced.has(x) and reduced.is_rational_function(u):
                return preimage(rational_answer(reduced, u, relation), logs[0], logarithm=True)
        # Rational polynomials in commensurate positive exponential bases.
        powers = [v for v in expr.atoms(s.Pow) if not v.base.has(x) and v.exp.has(x)]
        powers += [v for v in expr.atoms(s.exp) if v.has(x)]
        if substitutions and powers:
            base = powers[0].base if isinstance(powers[0], s.Pow) else s.E
            u = s.Symbol("u", positive=True)
            replacements = {}
            for power in powers:
                a, angle = (power.base, power.exp) if isinstance(power, s.Pow) else (s.E, power.args[0])
                ratio = s.simplify(s.log(a)/s.log(base))
                polynomial = s.Poly(angle, x)
                if polynomial.degree() != 1 or not ratio.is_Rational:
                    break
                exponent = ratio * polynomial.nth(1)
                if not exponent.is_Integer: break
                replacements[power] = a**polynomial.nth(0) * u**exponent
            else:
                reduced = expr.xreplace(replacements)
                if not reduced.has(x) and reduced.is_rational_function(u):
                    answer = rational_answer(reduced, u, relation) & s.Interval.open(0, s.oo)
                    return preimage(answer, s.Pow(base, x), logarithm=False)
        answer = s.solve_univariate_inequality(relations[relation](expr, 0), x, relational=False)
        if not explicit(answer): raise ValueError("solution_not_complete")
        # SymPy returns only one period for a nonconstant periodic inequality.
        # A finite interval must never masquerade as the answer on all reals.
        if answer not in (s.S.Reals, s.S.EmptySet) and periodicity(expr, x) not in (None, s.S.Zero):
            raise ValueError("periodic_inequality_not_complete")
        return answer

    def filter_periodic_roots(answer):
        """Prove domain constraints on entire root families, not sampled roots."""
        if not periodic_constraints or answer == s.S.EmptySet: return answer

        def allowed(root):
            truths = [s.simplify(relations[r](a.subs(x, root), b.subs(x, root)))
                      for a, b, r, _ in periodic_constraints]
            if s.S.false in truths: return False
            if all(v is s.S.true for v in truths): return True
            raise ValueError("periodic_root_domain_unresolved")

        if isinstance(answer, s.FiniteSet):
            return s.FiniteSet(*(v for v in answer if allowed(v)))
        if isinstance(answer, s.Union):
            return s.Union(*(filter_periodic_roots(v) for v in answer.args))
        if not isinstance(answer, s.ImageSet) or answer.base_sets != (s.S.Integers,):
            raise ValueError("periodic_root_family_not_supported")
        n = answer.lamda.variables[0]
        polynomial = s.Poly(answer.lamda.expr, n)
        if polynomial.degree() != 1: raise ValueError("periodic_root_family_not_supported")
        step = polynomial.nth(1)
        count = 1
        for _, _, _, period in periodic_constraints:
            ratio = s.simplify(step / period)
            if not ratio.is_Rational: raise ValueError("periodic_root_step_not_supported")
            count = s.ilcm(count, ratio.q)
            if count > 32: raise ValueError("periodic_root_residue_limit")
        k = s.Symbol("k", integer=True)
        families = []
        for residue in range(int(count)):
            root = s.expand(answer.lamda.expr.subs(n, count*k + residue))
            # Each proof still contains arbitrary integer k. Together all
            # residues cover every original n, including negative integers.
            if allowed(root): families.append(s.ImageSet(s.Lambda(k, root), s.S.Integers))
        return s.Union(*families)

    def preimage(answer, function, *, logarithm):
        if answer == s.S.EmptySet: return answer
        if answer == s.S.Reals: return s.S.Reals
        if isinstance(answer, s.Union):
            return s.Union(*(preimage(v, function, logarithm=logarithm) for v in answer.args))
        if isinstance(answer, s.FiniteSet):
            # Boundary equalities use solveset; reject unresolved branches.
            return s.Union(*(compare(function.args[0] if logarithm else function,
                s.exp(v) if logarithm else v, "=") for v in answer))
        if not isinstance(answer, s.Interval): raise ValueError("substitution_set_not_supported")
        result = s.S.Reals
        target = function.args[0] if logarithm else function
        for bound, relation in ((answer.start, ">" if answer.left_open else ">="),
                                (answer.end, "<" if answer.right_open else "<=")):
            if bound in (s.oo, -s.oo): continue
            result &= compare(target, s.exp(bound) if logarithm else bound, relation, substitutions=False)
        return result

    lhs, rhs = expression(condition["lhs"]), expression(condition["rhs"])
    restrictions = []
    for text in condition.get("restrictions", []): restrictions.extend(expression(text, restriction=True))
    interval = condition.get("interval")
    left = right = None
    if interval is not None:
        left, right = expression(interval["left"]), expression(interval["right"])
        difference = s.simplify(right-left)
        if condition.get("precheck_only") and (left.has(x) or right.has(x) or
                difference.is_positive is None):
            raise ValueError("interval_order_unknown")
        if left.has(x) or right.has(x) or difference.is_positive is not True:
            return dict(status="suspicious", reason="interval_order", version=VERSION)
        if not all(type(interval[k]) is bool for k in ("left_open", "right_open")): raise ValueError("interval_flags")
        # Exact set comparisons need canonical endpoints. Parsing above keeps
        # original denominators/domain constraints before any simplification.
        left, right = s.simplify(left), s.simplify(right)
    domain = s.S.Reals
    for a, b, relation in domain_conditions + restrictions:
        expr = s.cancel(a-b)
        period = (periodicity(expr, x) if condition["task_type"] == "trigonometric_equation"
                  and relation not in ("=", "!=") and expr.has(s.sin, s.cos, s.tan) else None)
        if period is not None and period.is_positive is True:
            periodic_constraints.append((a, b, relation, period))
        else:
            domain &= compare(a, b, relation)
    for denominator in denominators:
        domain &= compare(denominator, s.S.Zero, "!=")
    # Exact domain notation is diagnostic only; the key is formed from root
    # families whose membership in these constraints has actually been proved.
    displayed_domain = (s.ConditionSet(x, s.And(*(relations[r](a, b)
        for a, b, r, _ in periodic_constraints)), domain) if periodic_constraints else domain)
    relation = condition["relation"]
    diff = s.together(lhs-rhs)
    if diff.has(s.zoo, s.nan, s.oo, -s.oo):
        return dict(status="suspicious", reason="undefined_expression", version=VERSION)
    if condition["task_type"] == "inequality":
        if relation not in {"<", "<=", ">", ">="}: raise ValueError("inequality_profile")
        # Solve on the original domain (especially sqrt/log expressions).
        answer = compare(lhs, rhs, relation) & domain
        if interval is not None:
            answer &= s.Interval(left, right, interval["left_open"], interval["right_open"])
        general = answer
        if not explicit(answer): raise ValueError("solution_not_complete")
    else:
        if relation != "=" or (interval is None and not condition.get("precheck_only")): raise ValueError("equation_profile")
        calls = lhs.atoms(s.sin, s.cos, s.tan) | rhs.atoms(s.sin, s.cos, s.tan)
        if not calls: raise ValueError("trigonometry_expected")
        for call in calls:
            p = s.Poly(call.args[0], x)
            if p.degree() != 1 or not p.nth(1).is_Rational or not (p.nth(0)/s.pi).is_Rational:
                raise ValueError("trig_angle_not_supported")
        general = filter_periodic_roots(s.solveset(diff, x, domain=s.S.Reals)) & domain
        if condition.get("precheck_only"):
            if general.has(s.ConditionSet): raise ValueError("solution_not_complete")
            return dict(status="solved", empty=general == s.S.EmptySet, domain=s.sstr(displayed_domain),
                general=s.sstr(general), answer=s.sstr(general), scope="13a", version=VERSION)
        answer = general & s.Interval(left, right, interval["left_open"], interval["right_open"])
        if not isinstance(answer, (s.FiniteSet, type(s.S.EmptySet))): raise ValueError("finite_selection_not_solved")
        for root in answer:
            if s.simplify((lhs-rhs).subs(x, root)) != 0: raise ValueError("root_substitution_failed")
            if any(s.simplify(d.subs(x, root)) == 0 for d in denominators): raise ValueError("original_denominator_zero")
    if general.has(s.ConditionSet) or answer.has(s.ConditionSet): raise ValueError("solution_not_complete")
    return dict(status="solved", empty=answer == s.S.EmptySet, domain=s.sstr(displayed_domain),
        general=s.sstr(general), answer=s.sstr(answer), answer_latex=s.latex(answer),
        general_latex=s.latex(general), original_denominators=[s.sstr(v) for v in denominators],
        original_domain_conditions=[dict(lhs=s.sstr(a), rhs=s.sstr(b), relation=r) for a,b,r in domain_conditions],
        restrictions=condition.get("restrictions", []), version=VERSION, sympy_version=s.__version__,
        evidence="Exact real symbolic sets/sign analysis and monotone substitutions, with original domain restrictions; no pupil solution used.")


if __name__ == "__main__":
    try:
        raw = sys.stdin.buffer.read(16385)
        if len(raw) > 16384: raise ValueError("condition_size")
        result = solve(json.loads(raw))
    except (ValueError, TypeError, KeyError, SyntaxError, NotImplementedError) as error:
        reason = str(error) if isinstance(error, ValueError) else type(error).__name__
        import re
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,79}", reason): reason = "solver_not_supported"
        result = dict(status="unsupported", reason=reason, version=VERSION)
    except ImportError:
        result = dict(status="technical_error", reason="solver_runtime_missing", version=VERSION)
    except Exception:
        result = dict(status="unsupported", reason="solver_not_supported", version=VERSION)
    print(json.dumps(result, ensure_ascii=False))
