"""Private, provenance-bound provider summaries; never student evidence."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3

from .call_guard import StageRequest, digest

VERSION = 'reader-trace-1.0.0'


def saved_trace(ledger, fingerprints):
    """Reconstruct from immutable accepted READ, including during continuation.

    No summary is synthesized for old responses. Request artifacts already hold
    the exact prompt and transmitted image bytes; bind them instead of copying.
    """
    ledger = Path(ledger)
    with closing(sqlite3.connect((ledger/'spending.sqlite3').as_uri()+'?mode=ro', uri=True)) as con:
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute('SELECT * FROM calls ORDER BY rowid')]
    selected = None
    for row in rows:
        if row['fingerprint'] not in fingerprints or row['status'] != 'succeeded' or not row['response_ref']:
            continue
        request = json.loads((ledger/row['request_ref']).read_text())
        if request['stage'] != 'solution_ocr': continue
        response = json.loads((ledger/row['response_ref']).read_text())
        if StageRequest(**request).fingerprint != row['fingerprint'] or digest(response) != row['response_sha256']:
            raise ValueError('continuation_artifact_changed')
        summary = response.get('envelope', {}).get('provider_metadata') or {}
        summary = summary.get('thought_summary', {}) if isinstance(summary, dict) else {}
        parts = summary.get('parts', []) if isinstance(summary, dict) else []
        if not isinstance(parts, list) or not all(isinstance(p, str) for p in parts): parts = []
        selected = dict(version=VERSION, source='gemini_thought_summary',
            status='available' if any(p.strip() for p in parts) else 'unavailable', parts=parts,
            truncated=summary.get('truncated') is True if isinstance(summary, dict) else False,
            reading_fingerprint=row['fingerprint'], call_id=row['id'],
            request_ref=row['request_ref'], request_sha256=digest(request),
            response_ref=row['response_ref'], response_sha256=row['response_sha256'])
    return selected


def assessment_input(trace):
    """Keep diagnostics separate; never send filesystem paths or accounting."""
    if trace is None: return None
    return {k:trace[k] for k in ('source', 'status', 'parts', 'truncated', 'reading_fingerprint')}
