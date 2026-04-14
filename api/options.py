import json
import time
import random
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

try:
    import yfinance as yf
    YFINANCE_OK = True
except ImportError:
    YFINANCE_OK = False

def get_options_data(symbol, exp_date=None, max_retries=3):
    """Fetch options data with retry logic."""
    for attempt in range(max_retries):
        try:
            # Add delay between retries with jitter
            if attempt > 0:
                time.sleep(random.uniform(1.5, 3.5))

            # Use a session with realistic headers to avoid rate limiting
            import requests
            session = requests.Session()
            session.headers.update({
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Accept-Encoding': 'gzip, deflate, br',
                'Connection': 'keep-alive',
            })

            ticker = yf.Ticker(symbol, session=session)
            all_exps = ticker.options

            if not all_exps:
                if attempt < max_retries - 1:
                    continue
                return {'error': 'No expiration dates found', 'symbol': symbol}

            if not exp_date:
                return {
                    'symbol': symbol,
                    'expirationDates': list(all_exps),
                    'count': len(all_exps)
                }

            # Find the closest matching date
            target = exp_date
            if target not in all_exps:
                target = min(all_exps, key=lambda d: abs(
                    time.mktime(time.strptime(d, '%Y-%m-%d')) -
                    time.mktime(time.strptime(exp_date, '%Y-%m-%d'))
                ))

            chain = ticker.option_chain(target)
            calls_df = chain.calls
            puts_df  = chain.puts

            def df_to_list(df):
                records = []
                for _, row in df.iterrows():
                    try:
                        records.append({
                            'strike':            float(row.get('strike', 0) or 0),
                            'lastPrice':         float(row.get('lastPrice', 0) or 0),
                            'bid':               float(row.get('bid', 0) or 0),
                            'ask':               float(row.get('ask', 0) or 0),
                            'change':            float(row.get('change', 0) or 0),
                            'percentChange':     float(row.get('percentChange', 0) or 0),
                            'volume':            int(row.get('volume', 0) or 0),
                            'openInterest':      int(row.get('openInterest', 0) or 0),
                            'impliedVolatility': float(row.get('impliedVolatility', 0) or 0),
                            'inTheMoney':        bool(row.get('inTheMoney', False)),
                            'contractSymbol':    str(row.get('contractSymbol', '') or ''),
                        })
                    except Exception:
                        pass
                return records

            return {
                'symbol':         symbol,
                'expirationDate': target,
                'requestedDate':  exp_date,
                'allDates':       list(all_exps),
                'calls':          df_to_list(calls_df),
                'puts':           df_to_list(puts_df),
                'callCount':      len(calls_df),
                'putCount':       len(puts_df),
            }

        except Exception as e:
            err_str = str(e)
            if 'rate' in err_str.lower() or '429' in err_str or 'too many' in err_str.lower():
                if attempt < max_retries - 1:
                    time.sleep(random.uniform(2, 5))
                    continue
                return {'error': 'Rate limited by Yahoo Finance. Try again in a few minutes.', 'symbol': symbol}
            if attempt < max_retries - 1:
                continue
            return {'error': err_str, 'symbol': symbol}

    return {'error': 'Max retries exceeded', 'symbol': symbol}


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        symbol   = params.get('symbol', ['AAPL'])[0].upper().strip()
        exp_date = params.get('date', [None])[0]

        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Cache-Control', 'public, max-age=60')  # cache 1 min
        self.end_headers()

        if not YFINANCE_OK:
            self.wfile.write(json.dumps(
                {'error': 'yfinance not installed on server'}
            ).encode())
            return

        result = get_options_data(symbol, exp_date)
        self.wfile.write(json.dumps(result).encode())

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.end_headers()

    def log_message(self, format, *args):
        pass
