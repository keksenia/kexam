"""One exact, user-approved historical reserve; unknown cost is never rewritten."""
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
from .call_guard import CallGuard, Outcome, digest

VERSION='w06-live-reserve-1.0.0'
ROOT=Path(__file__).resolve().parents[1]
AUTH_PATH=ROOT/'planning/m17/W06_LIVE_RESERVE_AUTHORIZATION.json'


def authorization():
    value=json.loads(AUTH_PATH.read_text())
    prior=json.loads((ROOT/'planning/catalog_benchmark_64000_2026-10-10/W06_RESERVE_AUTHORIZATION.json').read_text())
    identity=('call_id','fingerprint','original_row_sha256','reserve_micro_rub')
    if (value.get('version')!='w06-live-reserve-authorization-1.0.0'
            or value.get('scope')!='independent-live-checking-only'
            or value.get('prior_authorization_ref')!='planning/catalog_benchmark_64000_2026-10-10/W06_RESERVE_AUTHORIZATION.json'
            or value.get('prior_authorization_sha256')!=digest(prior)
            or any(value.get(k)!=prior.get(k) for k in identity)
            or type(value.get('reserve_micro_rub')) is not int or value['reserve_micro_rub']<=0
            or value.get('actual_cost_remains_unknown') is not True
            or value.get('global_budget_and_expiry_unchanged') is not True
            or value.get('agent_paid_launch_authorized') is not False
            or value.get('automatic_retries')!=0 or value.get('global_rule_removed') is not False
            or not value.get('user_request')):
        raise ValueError('w06_reserve_authorization_changed')
    return value


def eligible(row,auth):
    if row['id']!=auth['call_id'] or row['cost'] is not None: return False
    original=deepcopy(dict(row));original['reserve']=0
    return (row['state']=='unknown' and row['status']=='cost_unknown' and row['failure_reason']=='transport_timeout'
        and row['fingerprint']==auth['fingerprint'] and row['reserve']==auth['reserve_micro_rub']
        and digest(original)==auth['original_row_sha256'])


def admission_block(rows,policy,auth):
    remaining=[r for r in rows if not eligible(r,auth)]
    return CallGuard.admission_block(remaining,policy) or ('unresolved_spending' if any(r['cost'] is None for r in remaining) else None)


class ReservedLiveGuard(CallGuard):
    def __init__(self,root,policy):
        self._w06_authorization=authorization()
        super().__init__(root,policy)

    def reserve(self,request,*,retry=False):
        if retry: return Outcome('retry_forbidden')
        auth=authorization()
        if auth!=self._w06_authorization: return Outcome('w06_reserve_authorization_changed')
        if (self.policy.budget_mode!='settled_spend' or dict(provider=request.provider,model=request.payload.get('model'),stage=request.stage)
                not in auth['allowed_requests']): return Outcome('w06_reserve_scope')
        return super().reserve(request,retry=False)

    def admission_block(self,selected_rows,policy):
        # M14 calls this inside BEGIN IMMEDIATE. Another writer cannot change
        # the verified historical row before the inherited budget reservation.
        with closing(sqlite3.connect(self.db.resolve().as_uri()+'?mode=ro',uri=True)) as con:
            con.row_factory=sqlite3.Row
            rows=con.execute('SELECT * FROM calls').fetchall()
        if len(rows)!=len(selected_rows): return 'w06_reserve_ledger_changed'
        return admission_block(rows,policy,self._w06_authorization)
