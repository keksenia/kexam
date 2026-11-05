"""Independent offline oracle for finite protocol fixtures, not a production M06/M07 module.

Only the data contract's eight operations are exercised; this helper neither consumes
photographs nor assigns grades, and is never invoked on student requests. Set comparison
uses the common endpoint partition, not the future runtime's normalization algorithm.
"""
from fractions import Fraction as F

def q(r): return F(int(r['numerator']),int(r['denominator']))
def value(v): return q(v['coefficient'])
def r(v):
    v=F(v);return {'numerator':str(v.numerator),'denominator':str(v.denominator)}
def exact(v,basis='pi'):return {'basis':basis,'coefficient':r(v)}
def member_interval(v,i):
    l=None if i['left'] is None else value(i['left']);h=None if i['right'] is None else value(i['right'])
    return (l is None or v>l or (v==l and i['left_closed'])) and (h is None or v<h or (v==h and i['right_closed']))
def member(v,s):return any(v==value(p) for p in s['points']) or any(member_interval(v,i) for i in s['intervals'])
def partition(sets):
    ends=sorted({value(v) for s in sets for i in s['intervals'] for v in [i['left'],i['right']] if v is not None}|{value(v) for s in sets for v in s['points']})
    return [F(0)] if not ends else [ends[0]-1,*ends,*[(a+b)/2 for a,b in zip(ends,ends[1:])],ends[-1]+1]
def status(truth):return 'verified' if truth else 'contradicted'
def output_too_wide(obj, limit):
    if isinstance(obj,dict):
        if set(obj)=={'numerator','denominator'}:
            v=q(obj)
            return max(len(str(abs(v.numerator))),len(str(v.denominator)))>limit
        return any(output_too_wide(v,limit) for v in obj.values())
    return isinstance(obj,list) and any(output_too_wide(v,limit) for v in obj)

def fixture_valid(e,limits):
    a=e['arguments'];o=e['operation'];c=e['expected_computed'];gate=e['gate']
    if gate!='resolved':
        return gate in {'unknown_assumption','series_operator_unresolved','angle_variable_unmapped','set_variables_unmapped'} and e['expected_status']=='unknown' and e['reason_code']=='unresolved_premise' and c is None
    result=None;truth=None
    if o=='m06.pi_interval_membership':
        yes=member_interval(value(a['point']),a['interval']);result={'member':yes};truth=yes==a['expected_member']
    elif o=='m06.select_pi_roots':
        i=a['interval'];lo=value(i['left']);hi=value(i['right']);roots=set();count=0
        for f in a['families']:
            offset,step=q(f['offset']),q(f['period'])
            if step==0:
                if member_interval(offset,i): roots.add(offset);count+=1
                continue
            x,y=sorted([(lo-offset)/step,(hi-offset)/step]);first=-((-x.numerator)//x.denominator);last=y.numerator//y.denominator
            # Compute count before iteration. Remove at most the two inadmissible open endpoints.
            if first<=last and not member_interval(offset+step*first,i):first+=1
            if first<=last and not member_interval(offset+step*last,i):last-=1
            count+=max(0,last-first+1)
            if count>limits['enumerated_root_candidates_before_dedup']:
                return e['expected_status']=='unknown' and e['reason_code']=='resource_limit' and c is None
            roots.update(offset+step*k for k in range(first,last+1))
        claimed=[value(v) for v in a['claimed_roots']];claimset=set(claimed)
        result={'selected_roots':[exact(v) for v in sorted(roots)],'missing_roots':[exact(v) for v in sorted(roots-claimset)],'extra_roots':[exact(v) for v in sorted(claimset-roots)],'duplicate_claim_count':len(claimed)-len(claimset)};truth=roots==claimset
    elif o=='m06.circle_point_equal':
        l,h=value(a['angle']),value(a['independent_point_angle']);turn=(l-h)/2
        result={'same_point':turn.denominator==1,'same_real_number':l==h,'difference_over_two_pi':r(turn),'angle_residue':exact(l%2),'point_residue':exact(h%2)};truth=turn.denominator==1
    elif o=='m07.set_membership':
        yes=member(value(a['point']),a['set']);result={'member':yes};truth=yes==a['expected_member']
    elif o=='m07.denominator_nonzero':
        x=value(a['point']);v=sum(q(coef)*x**i for i,coef in enumerate(a['expression']['denominator']))
        if max(len(str(abs(v.numerator))),len(str(v.denominator)))>limits['integer_output_digits']:
            return e['expected_status']=='unknown' and e['reason_code']=='resource_limit' and c is None
        result={'denominator_value':r(v),'nonzero':v!=0};truth=v!=0
    elif o in {'m07.set_union','m07.set_intersection','m07.set_equal'}:
        if c is None:return False
        if o=='m07.set_equal':
            sets=[a['left'],a['right'],c['symmetric_difference']];pts=partition(sets)
            diff=lambda p: member(p,a['left']) != member(p,a['right'])
            truth=not any(diff(p) for p in pts)
            good=c['equal']==truth and all(member(p,c['symmetric_difference'])==diff(p) for p in pts)
        else:
            sets=[*a['sets'],a['claimed_set'],c['computed_set'],c['symmetric_difference']];pts=partition(sets)
            comb=any if o=='m07.set_union' else all
            actual=lambda p: comb(member(p,s) for s in a['sets'])
            diff=lambda p:actual(p)!=member(p,a['claimed_set'])
            truth=not any(diff(p) for p in pts)
            good=all(member(p,c['computed_set'])==actual(p) and member(p,c['symmetric_difference'])==diff(p) for p in pts)
        return good and e['expected_status']==status(truth) and e['reason_code']==('exact_true' if truth else 'exact_false')
    else:return False
    if output_too_wide(result,limits['integer_output_digits']):
        return e['expected_status']=='unknown' and e['reason_code']=='resource_limit' and c is None
    return c==result and e['expected_status']==status(truth) and e['reason_code']==('exact_true' if truth else 'exact_false')
