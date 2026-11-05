"""Direct DeepSeek HTTPS; one attempt, no redirects, exact body, token accounting."""
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING
import hashlib
import json
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.request import Request, ProxyHandler, build_opener
from .call_guard import ProviderResponse, canonical, unique_object
from .hubris_transport import NoRedirect
from .transport_diagnostics import TransportFailure, detail, exception_detail

VERSION = 'deepseek-direct-1.0.1'
ENDPOINT = 'https://api.deepseek.com/chat/completions'
MAX_BYTES = 2 * 1024 * 1024


def read_credential(config):
    """Only the user-selected variable; never print or persist the secret."""
    name = config['credential_variable']
    if name != 'DEPSEEK_API_KEY': raise ValueError('deepseek_configuration_invalid')
    key = None
    try:
        with Path(config['credential_file']).open() as source:
            for line in source:
                match = re.match(r'^\s*(?:export\s+)?' + re.escape(name) + r'\s*=\s*(.*)$', line)
                if match:
                    if key is not None: raise ValueError('credential_duplicate')
                    value = match.group(1).strip()
                    if value.startswith(('"', "'")):
                        end = value.find(value[0], 1)
                        if end < 0 or value[end+1:].strip() and not value[end+1:].lstrip().startswith('#'):
                            raise ValueError('credential_missing_or_invalid')
                        value = value[1:end]
                    else:
                        value = re.split(r'\s+#', value, maxsplit=1)[0].strip()
                    key = value
    except OSError:
        raise ValueError('credential_missing_or_invalid') from None
    if not isinstance(key, str) or not 16 <= len(key) <= 256 or any(ord(c) < 33 or ord(c) > 126 for c in key):
        raise ValueError('credential_missing_or_invalid')
    return key


def validate_pricing(pricing):
    try:
        if datetime.fromisoformat(pricing['valid_until'].replace('Z', '+00:00')) <= datetime.now(timezone.utc):
            raise ValueError('pricing_expired')
        for key in ('input_usd_per_million','cached_input_usd_per_million','output_usd_per_million','usd_to_rub'):
            n = Decimal(pricing[key])
            if not n.is_finite() or n <= 0: raise ValueError('pricing_invalid')
    except (KeyError, TypeError, ArithmeticError):
        raise ValueError('pricing_invalid') from None


def parse_response(raw, pricing, *, status=200):
    try:
        data = json.loads(raw, object_pairs_hook=unique_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite')))
    except (ValueError, UnicodeError):
        data = {}
    if not isinstance(data, dict): data = {}
    choices = data.get('choices')
    choice = choices[0] if isinstance(choices, list) and len(choices)==1 and isinstance(choices[0],dict) else {}
    message = choice.get('message') or {}
    if not isinstance(message,dict): message={}
    content = message.get('content')
    finish = choice.get('finish_reason', 'invalid_envelope')
    if content is None and finish in {'length','content_filter','aborted'}: content=''
    if not isinstance(content, str): content=''; finish='invalid_envelope'
    model = data.get('model')
    usage = data.get('usage'); cost = None; evidence = {}; normalized = None
    if isinstance(usage,dict):
        inp, out = usage.get('prompt_tokens'), usage.get('completion_tokens')
        hit, miss = usage.get('prompt_cache_hit_tokens'), usage.get('prompt_cache_miss_tokens')
        details=usage.get('completion_tokens_details') or {}
        reasoning=details.get('reasoning_tokens') if isinstance(details,dict) else None
        if (all(type(n) is int and 0 <= n < 2**53 for n in (inp,out,hit,miss)) and hit+miss==inp
                and usage.get('total_tokens')==inp+out
                and (reasoning is None or type(reasoning) is int and 0 <= reasoning <= out)):
            normalized=dict(prompt_tokens=inp,completion_tokens=out,total_tokens=inp+out,
                prompt_tokens_details=dict(cached_tokens=hit))
            if reasoning is not None: normalized['completion_tokens_details']=dict(reasoning_tokens=reasoning)
            if model == 'deepseek-flash':
                validate_pricing(pricing)
                usd=(miss*Decimal(pricing['input_usd_per_million'])+hit*Decimal(pricing['cached_input_usd_per_million'])
                    +out*Decimal(pricing['output_usd_per_million']))/Decimal(1000000)
                rub=(usd*Decimal(pricing['usd_to_rub'])).quantize(Decimal('.00000001'),rounding=ROUND_CEILING)
                cost=str(rub*100)
                evidence=dict(pricing=deepcopy(pricing),usd=str(usd),rub=str(rub),output_includes_thinking=True,
                              method='conservative_peak_tariff_estimate')
        elif status == 200: finish='invalid_usage'
    elif status == 200: finish='invalid_usage'
    return ProviderResponse(content, usage=normalized, cost_kopecks=cost, model=model,
        response_id=data.get('id'), finish_reason=finish if isinstance(finish,str) else 'invalid_envelope',
        http_status=status, cost_basis='token_estimate' if cost is not None else 'unknown',
        tariff_ref=pricing['tariff_ref'] if cost is not None else None, provider_metadata=evidence)


class DeepSeekTransport:
    kind='live'
    single_attempt=True
    total_output_bound_verified=False

    def __init__(self, credential, *, allowed_body_hashes, pricing, opener=None):
        self._credential=credential; self._hashes=frozenset(allowed_body_hashes); self._pricing=deepcopy(pricing)
        validate_pricing(pricing)
        # Uses the existing system route. Does not change the user's VPN.
        self._opener=opener or build_opener(ProxyHandler({}),NoRedirect())

    def __repr__(self): return 'DeepSeekTransport(credential=<redacted>, single_attempt=True)'

    def __call__(self, payload, *, timeout_seconds):
        raw=canonical(payload).encode()
        if (hashlib.sha256(raw).hexdigest() not in self._hashes or len(raw)>8388608
                or payload.get('model')!='deepseek-flash'):
            raise ValueError('unapproved_body')
        if not isinstance(timeout_seconds,(int,float)) or isinstance(timeout_seconds,bool) or not 0 < timeout_seconds <= 300:
            raise ValueError('timeout_invalid')
        request=Request(ENDPOINT,data=raw,headers={'Authorization':'Bearer '+self._credential,
            'Content-Type':'application/json','Accept':'application/json'},method='POST')
        status=None
        try:
            try:response=self._opener.open(request,timeout=timeout_seconds)
            except HTTPError as error: response=error
            with response:
                status=response.getcode()
                raw_response=response.read(MAX_BYTES+1)
                if len(raw_response)>MAX_BYTES:
                    raise TransportFailure('provider_envelope_too_large',diagnostic=detail('response_too_large',status))
                return parse_response(raw_response,self._pricing,status=status)
        except TransportFailure:raise
        except Exception as error:
            raise TransportFailure('provider_transport_failed',diagnostic=exception_detail(error,http_status=status)) from None
