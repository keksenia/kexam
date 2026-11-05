"""Exact existing reserves authorized for the ordinary independent live path."""
from contextlib import closing
from copy import deepcopy
import json
import sqlite3
from .call_guard import Outcome, digest
from . import w06_live_reserve as w06
from .w06_live_reserve import ROOT, ReservedLiveGuard

VERSION = 'live-reserves-1.2.0'
AUTH_PATH = ROOT / 'planning/m17/live_reserves_2026-10-13/AUTHORIZATION.json'
SCHOOL_AUTH_PATH = ROOT / 'planning/m17/stobal_82_2026-10-26/S046_RECOVERY_AUTHORIZATION.json'
SCHOOL_NEXT_AUTH_PATH = ROOT / 'planning/m17/stobal_82_2026-10-26/S052_RECOVERY_AUTHORIZATION.json'


def authorization():
    value = json.loads(AUTH_PATH.read_text())
    reserves = value.get('reserves', [])
    if (value.get('version') != 'additional-live-reserves-1.0.0'
            or value.get('scope') != 'independent-live-checking-only'
            or value.get('actual_cost_remains_unknown') is not True
            or value.get('budget_expiry_limits_unchanged') is not True
            or value.get('agent_paid_launch_authorized') is not False
            or value.get('automatic_retries') != 0 or not value.get('user_request')
            or len(reserves) != 2 or {r['case'] for r in reserves} != {'G13-16', 'G13-10'}
            or len({r['call_id'] for r in reserves}) != 2
            or any(type(r['reserve_micro_rub']) is not int or r['reserve_micro_rub'] != 35_000_000
                   or r['failure_reason'] != 'transport_failure' for r in reserves)):
        raise ValueError('live_reserve_authorization_changed')
    if not SCHOOL_AUTH_PATH.exists():
        return value
    school = json.loads(SCHOOL_AUTH_PATH.read_text())
    target = school.get('reserve_target', {})
    if (school.get('version') != 'stobal-s046-recovery-1'
            or school.get('human_launch_only') is not True
            or school.get('agent_paid_launch_authorized') is not False
            or school.get('automatic_retries') != 0
            or school.get('actual_cost_remains_unknown') is not True
            or school.get('global_budget_expiry_limits_unchanged') is not True
            or school.get('legacy_authorization_sha256') != digest(value)
            or not school.get('user_message') or not school.get('approved_question')
            or target.get('case') != 'S046'
            or target.get('call_id') != 'Cfe19ce7b72c54b97aeffc5c21ef28a81'
            or target.get('fingerprint') != '250655fbed7323268e6954acf3a4f67a551fd1140e5b10bf01f6b4a23fb53769'
            or target.get('failure_reason') != 'transport_failure'
            or type(target.get('reserve_micro_rub')) is not int or target['reserve_micro_rub'] != 35000000
            or target.get('original_row_sha256') != 'bf5316e6075d94a3fec1903b01f373bcb1d40c8a5d668a863c48f5754ee814d4'):
        raise ValueError('school_reserve_authorization_changed')
    # Preserve both old authorizations; only the one exact newly approved row
    # gains an exception. Its full contents are still checked by eligible().
    combined = {**value, 'reserves': [*reserves, target],
                'school_recovery_authorization_sha256': digest(school)}
    if not SCHOOL_NEXT_AUTH_PATH.exists():
        return combined
    next_school = json.loads(SCHOOL_NEXT_AUTH_PATH.read_text())
    next_target = next_school.get('reserve_target', {})
    if (next_school.get('version') != 'stobal-s052-recovery-1'
            or next_school.get('human_launch_only') is not True
            or next_school.get('agent_paid_launch_authorized') is not False
            or next_school.get('automatic_retries') != 0
            or next_school.get('actual_cost_remains_unknown') is not True
            or next_school.get('global_budget_expiry_limits_unchanged') is not True
            or next_school.get('prior_authorization_sha256') != digest(combined)
            or not next_school.get('user_message') or not next_school.get('approved_question')
            or next_target.get('case') != 'S052'
            or next_target.get('call_id') != 'C813bf8f8108e4d9e877d8aac5b6bd40d'
            or next_target.get('fingerprint') != 'e16222e1a7f7da68288161eaa3a8a6fc179e6245576ed156692d4d4f928fa07a'
            or next_target.get('failure_reason') != 'transport_failure'
            or type(next_target.get('reserve_micro_rub')) is not int
            or next_target['reserve_micro_rub'] != 35000000
            or next_target.get('original_row_sha256') != '789369f1062a7cb3e2f574c2d017f9cb6f6ec0c04dc9b97fe93d74ece5d9f0eb'):
        raise ValueError('school_next_reserve_authorization_changed')
    return {**combined, 'reserves': [*combined['reserves'], next_target],
            'school_next_recovery_authorization_sha256': digest(next_school)}


def eligible(row, target):
    original = deepcopy(dict(row))
    original['reserve'] = 0
    return (row['id'] == target['call_id'] and row['fingerprint'] == target['fingerprint']
            and row['cost'] is None and row['state'] == 'unknown' and row['status'] == 'cost_unknown'
            and row['failure_reason'] == target['failure_reason']
            and row['reserve'] == target['reserve_micro_rub']
            and digest(original) == target['original_row_sha256'])


def admission_block(rows, policy, auth):
    remaining = [r for r in rows if not any(eligible(r, target) for target in auth['reserves'])]
    return w06.admission_block(remaining, policy, w06.authorization())


class LiveReservedGuard(ReservedLiveGuard):
    def __init__(self, root, policy):
        self._live_reserve_authorization = authorization()
        super().__init__(root, policy)

    def reserve(self, request, *, retry=False):
        if authorization() != self._live_reserve_authorization:
            return Outcome('live_reserve_authorization_changed')
        return super().reserve(request, retry=retry)

    def admission_block(self, selected_rows, policy):
        # Called within M14's BEGIN IMMEDIATE; budget calculation still includes
        # every retained reserve. Never change or settle an unknown charge here.
        with closing(sqlite3.connect(self.db.resolve().as_uri() + '?mode=ro', uri=True)) as con:
            con.row_factory = sqlite3.Row
            rows = con.execute('SELECT * FROM calls').fetchall()
        if len(rows) != len(selected_rows):
            return 'live_reserve_ledger_changed'
        return admission_block(rows, policy, self._live_reserve_authorization)
