"""Bounded, allowlisted scan-failure JSON lines; never exception text or responses.

These logs may be public in a public Actions repository. No artifact, network,
credential access, retry, recommendation or publication state is created here.
"""
from datetime import datetime
import json
import os
import re
import sys
from urllib.error import HTTPError

STAGES = frozenset({'universe', 'stock_analysis', 'observation_validation',
    'quote_date_validation', 'quote_freshness', 'price_validation', 'score_validation'})
SOURCE_CODES = frozenset({'universe_unavailable', 'incomplete_us_universe',
    'incomplete_korean_universe', 'kr_universe_fetch_failed', 'kr_universe_invalid_json',
    'kr_universe_invalid_contract', 'kr_universe_incomplete', 'kr_universe_invalid_order',
    'quote_fetch_failed', 'missing_quote_history', 'invalid_quote_price',
    'invalid_observation', 'stale_quote', 'wrong_quote_session', 'invalid_price', 'score_inputs_unavailable',
    'stock_observation_failed', 'provider_request_failed', 'request_already_failed_this_run',
    'provider_rate_limited'})
GENERIC_CODES = frozenset({'quote_fetch_failed', 'stock_observation_failed',
    'provider_request_failed', 'request_already_failed_this_run'})
SUMMARIES = {
    'rate_limit': 'Provider reported a request limit; no retry is started by diagnostics.',
    'timeout': 'A typed timeout occurred; the underlying response was not recorded.',
    'connection': 'A typed connection failure occurred.',
    'http': 'An HTTP error status was available; response content was not recorded.',
    'source_data': 'Source data failed a named availability or validation check.',
    'processing': 'Observation processing failed; this does not establish a code defect.',
    'unknown': 'Failure cause is unconfirmed; exception text is intentionally omitted.',
}
NETWORK_MODULES = frozenset({'requests.exceptions', 'curl_cffi.requests.exceptions'})


def _chain(exc):
    result=[]; seen=set()
    while isinstance(exc, BaseException) and id(exc) not in seen and len(result)<8:
        seen.add(id(exc)); result.append(exc)
        exc=exc.__cause__  # Do not recover suppressed implicit exception contexts.
    return result


def _kind(exc, source_error_type):
    if isinstance(exc, source_error_type): return 'SourceDataError'
    if isinstance(exc, HTTPError): return 'HTTPError'
    for cls in (TimeoutError, ConnectionError, KeyError, IndexError, TypeError, ValueError, ArithmeticError):
        if isinstance(exc, cls): return cls.__name__
    module, name=type(exc).__module__, type(exc).__name__
    if module in NETWORK_MODULES and name in {'Timeout', 'ReadTimeout', 'ConnectTimeout', 'ConnectionError', 'HTTPError'}:
        return name
    if module=='yfinance.exceptions' and name=='YFRateLimitError': return name
    return 'OtherException'


def _http_status(exc):
    status=None
    if isinstance(exc, HTTPError): status=exc.code
    elif type(exc).__module__ in NETWORK_MODULES:
        response=getattr(exc, 'response', None)
        status=getattr(response, 'status_code', None)
    return status if type(status) is int and 400<=status<=599 else None


def _code(exc, source_error_type):
    if not isinstance(exc, source_error_type): return None
    # Exact fixed code only, never str/repr(exception) or interpolation of args.
    value=exc.args[0] if len(exc.args)==1 else None
    return value if isinstance(value, str) and value in SOURCE_CODES else None


def _identifier(value, pattern):
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else None


def failure_record(exc, *, market, ticker, stage, generated_at, source_error_type):
    chain=_chain(exc)
    selected=chain[-1] if chain else exc
    codes=[code for item in chain if (code:=_code(item, source_error_type))]
    statuses=[value for item in chain if (value:=_http_status(item)) is not None]
    kinds=[_kind(item, source_error_type) for item in chain]
    category, code='unknown', 'unconfirmed_failure'
    status=statuses[-1] if statuses else None
    if 429 in statuses:
        category, code, status='rate_limit', 'http_rate_limited', 429
    elif 'YFRateLimitError' in kinds or 'provider_rate_limited' in codes:
        category, code='rate_limit', 'provider_rate_limited'  # No invented HTTP status.
    elif any(k in {'TimeoutError','Timeout','ReadTimeout','ConnectTimeout'} for k in kinds):
        category, code='timeout', 'network_timeout'
    elif 'ConnectionError' in kinds:
        category, code='connection', 'connection_failed'
    elif status is not None:
        category, code='http', 'http_error'
    elif any(c not in GENERIC_CODES for c in codes):
        category, code='source_data', next(c for c in reversed(codes) if c not in GENERIC_CODES)
    elif kinds and kinds[-1] in {'KeyError','IndexError','TypeError','ValueError','ArithmeticError'}:
        category, code='processing', 'processing_exception'
    elif codes:
        code=codes[-1]
    safe_stage=stage if stage in STAGES else 'unknown'
    if safe_stage=='stock_analysis':
        if 'quote_fetch_failed' in codes: safe_stage='quote_fetch'
        elif any(c in {'missing_quote_history','invalid_quote_price'} for c in codes): safe_stage='quote_validation'
    timestamp=None
    if isinstance(generated_at,str) and len(generated_at)<=40:
        try:
            parsed=datetime.fromisoformat(generated_at)
            if parsed.tzinfo is not None: timestamp=parsed.isoformat()
        except ValueError: pass
    safe_market=market if market in {'KR','US'} else None
    pattern=r'[0-9]{6}\.(?:KS|KQ)' if safe_market=='KR' else r'[A-Z][A-Z0-9.-]{0,14}'
    return {'schemaVersion':1, 'event':'stock_scan_failure',
        'runId':_identifier(os.environ.get('GITHUB_RUN_ID'),r'[1-9][0-9]{0,19}'),
        'runAttempt':_identifier(os.environ.get('GITHUB_RUN_ATTEMPT'),r'[1-9][0-9]{0,3}'),
        'market':safe_market, 'ticker':_identifier(ticker,pattern) if safe_market else None,
        'stage':safe_stage, 'attemptedAt':timestamp, 'exceptionType':_kind(selected,source_error_type),
        'category':category, 'errorCode':code, 'safeSummary':SUMMARIES[category], 'httpStatus':status,
        'retryCount':0, 'retryScope':'scan_market', 'downstreamRetryCount':None}


def emit_scan_failure(exc, **context):
    """Logging must not change error counts, last-good preservation or scan decisions."""
    try:
        record=failure_record(exc, **context)
        print(json.dumps(record, ensure_ascii=True, separators=(',',':')), file=sys.stderr, flush=True)
        return True
    except Exception:
        # No exception fallback: it could itself expose an unsafe original message.
        return False
