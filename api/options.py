import json
import math
import os
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests

YAHOO_OPTIONS_URL = 'https://query2.finance.yahoo.com/v7/finance/options/{symbol}'
YAHOO_COOKIE_URL = 'https://fc.yahoo.com'
YAHOO_CRUMB_URL = 'https://query1.finance.yahoo.com/v1/test/getcrumb'

REQUEST_TIMEOUT = 20
AUTH_TTL_SECONDS = 55          # Yahoo getcrumb responds with max-age=60
SUCCESS_CACHE_TTL = 15         # short in-memory cache for warm requests
STALE_IF_ERROR_TTL = 900       # return recent successful data when Yahoo temporarily blocks us
DISK_CACHE_TTL = 21600         # keep successful option payloads on disk for 6 hours

CACHE_DIR = Path(os.getenv('OPTIONLENS_CACHE_DIR', '/tmp/optionlens-cache'))
CACHE_DIR.mkdir(parents=True, exist_ok=True)

SESSION = requests.Session()
SESSION.headers.update({
    'User-Agent': 'Mozilla/5.0',
    'Accept-Language': 'en-US,en;q=0.9',
})

AUTH_CACHE = {'ts': 0.0, 'crumb': None}
AUTH_LOCK = threading.Lock()
DATA_CACHE = {}
DATA_LOCK = threading.Lock()
DISK_LOCK = threading.Lock()


def _is_bad_crumb(value):
    if not value:
        return True
    text = str(value).strip()
    lowered = text.lower()
    return (
        not text
        or text.startswith('{')
        or '<html' in lowered
        or 'too many requests' in lowered
        or 'not acceptable' in lowered
        or 'invalid cookie' in lowered
        or 'invalid crumb' in lowered
    )


def _safe_float(value):
    try:
        num = float(value)
    except (TypeError, ValueError):
        return 0.0
    if math.isnan(num) or math.isinf(num):
        return 0.0
    return num


def _safe_int(value):
    return int(_safe_float(value))


def _timestamp_to_date_str(ts_value):
    return datetime.fromtimestamp(int(ts_value), tz=timezone.utc).strftime('%Y-%m-%d')


def _date_str_to_timestamp(date_str):
    return int(datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp())


def _cache_key(symbol, exp_date):
    return f"{symbol.upper().strip()}::{exp_date or ''}"


def _cache_slug(symbol, exp_date):
    exp = exp_date or 'expirations'
    return f"{symbol.upper().strip()}__{exp}.json"


def _disk_cache_path(symbol, exp_date):
    return CACHE_DIR / _cache_slug(symbol, exp_date)


def _load_memory_cached_success(symbol, exp_date, max_age):
    key = _cache_key(symbol, exp_date)
    with DATA_LOCK:
        row = DATA_CACHE.get(key)
        if not row:
            return None
        age = time.time() - row['ts']
        if age > max_age:
            return None
        return row['data']


def _store_memory_cached_success(symbol, exp_date, data):
    key = _cache_key(symbol, exp_date)
    with DATA_LOCK:
        DATA_CACHE[key] = {'ts': time.time(), 'data': data}


def _load_disk_cached_success(symbol, exp_date, max_age):
    path = _disk_cache_path(symbol, exp_date)
    if not path.exists():
        return None
    try:
        with DISK_LOCK:
            payload = json.loads(path.read_text())
    except Exception:
        return None
    ts = payload.get('ts')
    data = payload.get('data')
    if not isinstance(ts, (int, float)) or not isinstance(data, dict):
        return None
    age = time.time() - ts
    if age > max_age:
        return None
    return data


def _store_disk_cached_success(symbol, exp_date, data):
    path = _disk_cache_path(symbol, exp_date)
    tmp_path = path.with_suffix('.tmp')
    payload = {'ts': time.time(), 'data': data}
    with DISK_LOCK:
        tmp_path.write_text(json.dumps(payload, separators=(',', ':')))
        tmp_path.replace(path)


def _load_cached_success(symbol, exp_date, max_age):
    memory_hit = _load_memory_cached_success(symbol, exp_date, max_age)
    if memory_hit:
        return memory_hit, 'memory'
    disk_hit = _load_disk_cached_success(symbol, exp_date, max_age)
    if disk_hit:
        _store_memory_cached_success(symbol, exp_date, disk_hit)
        return disk_hit, 'disk'
    return None, None


def _store_cached_success(symbol, exp_date, data):
    _store_memory_cached_success(symbol, exp_date, data)
    _store_disk_cached_success(symbol, exp_date, data)


def _attach_cache_meta(data, status, layer=None):
    if not isinstance(data, dict):
        return data
    payload = dict(data)
    meta = {'status': status}
    if layer:
        meta['layer'] = layer
    payload['_cache'] = meta
    return payload


def _refresh_auth_locked(force=False):
    now = time.time()
    if not force and AUTH_CACHE['crumb'] and (now - AUTH_CACHE['ts'] < AUTH_TTL_SECONDS):
        return AUTH_CACHE['crumb']

    SESSION.get(
        YAHOO_COOKIE_URL,
        headers={'User-Agent': SESSION.headers['User-Agent'], 'Accept': 'text/html,*/*;q=0.8'},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    crumb_resp = SESSION.get(
        YAHOO_CRUMB_URL,
        headers={'User-Agent': SESSION.headers['User-Agent'], 'Accept': '*/*'},
        timeout=REQUEST_TIMEOUT,
        allow_redirects=True,
    )
    crumb = crumb_resp.text.strip()
    if crumb_resp.status_code != 200 or _is_bad_crumb(crumb):
        raise RuntimeError(f'Failed to acquire Yahoo crumb: HTTP {crumb_resp.status_code} {crumb[:120]}')

    AUTH_CACHE['crumb'] = crumb
    AUTH_CACHE['ts'] = now
    return crumb


def get_yahoo_crumb(force=False):
    with AUTH_LOCK:
        return _refresh_auth_locked(force=force)


def fetch_options_payload(symbol, exp_date=None, max_retries=2):
    url = YAHOO_OPTIONS_URL.format(symbol=symbol.upper().strip())
    params = {}
    if exp_date:
        params['date'] = _date_str_to_timestamp(exp_date)

    last_error = None
    for attempt in range(max_retries):
        force_auth = attempt > 0
        crumb = get_yahoo_crumb(force=force_auth)
        req_params = dict(params)
        req_params['crumb'] = crumb
        response = SESSION.get(
            url,
            params=req_params,
            headers={'User-Agent': SESSION.headers['User-Agent'], 'Accept': 'application/json'},
            timeout=REQUEST_TIMEOUT,
        )

        if response.status_code in (401, 429):
            last_error = RuntimeError(f'Yahoo options HTTP {response.status_code}: {response.text[:160]}')
            continue

        if response.status_code >= 400:
            response.raise_for_status()

        payload = response.json()
        result = ((payload.get('optionChain') or {}).get('result') or [None])[0]
        if not result:
            last_error = RuntimeError('Yahoo options payload missing result')
            continue
        return result

    raise last_error or RuntimeError('Yahoo options request failed')


def _normalize_contracts(items):
    records = []
    for row in items or []:
        records.append({
            'strike': _safe_float(row.get('strike')),
            'lastPrice': _safe_float(row.get('lastPrice')),
            'bid': _safe_float(row.get('bid')),
            'ask': _safe_float(row.get('ask')),
            'change': _safe_float(row.get('change')),
            'percentChange': _safe_float(row.get('percentChange')),
            'volume': _safe_int(row.get('volume')),
            'openInterest': _safe_int(row.get('openInterest')),
            'impliedVolatility': _safe_float(row.get('impliedVolatility')),
            'inTheMoney': bool(row.get('inTheMoney', False)),
            'contractSymbol': str(row.get('contractSymbol', '') or ''),
        })
    return records


def _build_response(symbol, payload, exp_date=None):
    expirations = [_timestamp_to_date_str(ts) for ts in (payload.get('expirationDates') or [])]
    if not exp_date:
        return {
            'symbol': symbol,
            'expirationDates': expirations,
            'count': len(expirations),
            'source': 'Yahoo direct cookie+crumb',
        }

    option_rows = (payload.get('options') or [{}])[0]
    actual_exp = option_rows.get('expirationDate') or payload.get('expirationDate')
    actual_exp_str = _timestamp_to_date_str(actual_exp) if actual_exp else exp_date
    calls = _normalize_contracts(option_rows.get('calls'))
    puts = _normalize_contracts(option_rows.get('puts'))
    return {
        'symbol': symbol,
        'expirationDate': actual_exp_str,
        'requestedDate': exp_date,
        'allDates': expirations,
        'calls': calls,
        'puts': puts,
        'callCount': len(calls),
        'putCount': len(puts),
        'source': 'Yahoo direct cookie+crumb',
    }


def get_options_data(symbol, exp_date=None, max_retries=2):
    symbol = symbol.upper().strip()

    cached, layer = _load_cached_success(symbol, exp_date, SUCCESS_CACHE_TTL)
    if cached:
        return _attach_cache_meta(cached, 'fresh', layer)

    try:
        payload = fetch_options_payload(symbol, exp_date=exp_date, max_retries=max_retries)
        data = _build_response(symbol, payload, exp_date=exp_date)
        _store_cached_success(symbol, exp_date, data)
        return _attach_cache_meta(data, 'miss', 'network')
    except Exception as exc:
        stale, layer = _load_cached_success(symbol, exp_date, STALE_IF_ERROR_TTL)
        if stale:
            return _attach_cache_meta(stale, 'stale-if-error', layer)

        err_str = str(exc)
        lowered = err_str.lower()
        if '429' in err_str or 'too many requests' in lowered or 'rate limit' in lowered:
            return {'error': 'Rate limited by Yahoo Finance. Try again in a few minutes.', 'symbol': symbol}
        return {'error': err_str, 'symbol': symbol}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        symbol = params.get('symbol', ['AAPL'])[0].upper().strip()
        exp_date = params.get('date', [None])[0]

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'public, max-age=15, stale-while-revalidate=45')
        self.end_headers()

        result = get_options_data(symbol, exp_date)
        self.wfile.write(json.dumps(result).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.end_headers()

    def log_message(self, format, *args):
        pass
