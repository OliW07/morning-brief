import datetime as dt
import getpass
from http.server import BaseHTTPRequestHandler, HTTPServer
import secrets
import time
from urllib.parse import parse_qs, urlparse
import webbrowser

import requests
from requests_oauthlib import OAuth2Session

from .storage import CONFIG, STATE, config, load, locked, save

AUTHORIZE = 'https://cloud.ouraring.com/oauth/authorize'
TOKEN_URL = 'https://api.ouraring.com/oauth/token'
API = 'https://api.ouraring.com/v2/usercollection/'
REDIRECT = 'http://localhost:8765/callback'
UA = 'MorningBrief/1.0 (personal local dashboard)'
COLLECTIONS = ('daily_sleep', 'daily_readiness', 'sleep', 'daily_activity')


class OuraError(Exception):
    pass


def connected():
    return bool(load(STATE / 'oura-token.json').get('refresh_token'))


def authorize():
    print('Register a personal application at https://developer.ouraring.com/')
    print(f'Register this exact redirect URI: {REDIRECT}')
    print('Only the daily scope is needed. Credentials stay on this laptop.\n')
    client = load(CONFIG / 'oura-client.json')
    client_id = input('Client ID' + (' [saved]' if client else '') + ': ').strip() or client.get('client_id')
    secret = getpass.getpass('Client secret' + (' [saved]' if client else '') + ': ').strip() or client.get('client_secret')
    if not client_id or not secret:
        raise OuraError('Both client ID and client secret are required.')
    state = secrets.token_urlsafe(32)
    oauth = OAuth2Session(client_id, redirect_uri=REDIRECT, scope=['daily'], state=state, pkce='S256')
    url, expected_state = oauth.authorization_url(AUTHORIZE)
    result = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass  # Never log OAuth codes or query strings.

        def do_GET(self):
            parsed = urlparse(self.path)
            query = parse_qs(parsed.query)
            valid = parsed.path == '/callback' and secrets.compare_digest(query.get('state', [''])[0], expected_state)
            if not valid:
                self.send_error(400, 'Invalid authorization callback')
                return
            result.update({k: v[0] for k, v in query.items()})
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(b'Authorization received. Return to the terminal to finish connecting Morning Brief.')

    with HTTPServer(('127.0.0.1', 8765), Callback) as server:
        server.timeout = 1
        print('\nOpening Oura consent in your browser. If it does not open:\n' + url)
        webbrowser.open(url)
        deadline = time.monotonic() + 300
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get('code'):
        raise OuraError('Authorization denied or timed out. Run connect again when ready.')
    try:
        token = oauth.fetch_token(TOKEN_URL, code=result['code'], client_secret=secret,
                                  include_client_id=True, headers={'User-Agent': UA}, timeout=20)
    except Exception:
        raise OuraError('Token exchange failed. Check your app credentials and exact redirect URI.') from None
    with locked('oauth.lock'):
        save(CONFIG / 'oura-client.json', {'client_id': client_id, 'client_secret': secret})
        save(STATE / 'oura-token.json', dict(token))
    print('Connected. Checking access to your daily data…')
    data = fetch()
    print(f'API access verified. Received {sum(len(data[k]) for k in COLLECTIONS)} records.')


def access_token(force=False):
    # Oura refresh tokens are single-use. Refresh and atomic save are serialized.
    with locked('oauth.lock'):
        token = load(STATE / 'oura-token.json')
        client = load(CONFIG / 'oura-client.json')
        if not token or not client:
            raise OuraError('Oura is not connected. Run morning-brief connect after activating membership.')
        if not force and token.get('expires_at', 0) > time.time() + 90:
            return token['access_token']
        try:
            oauth = OAuth2Session(client['client_id'], token=token)
            updated = oauth.refresh_token(TOKEN_URL, refresh_token=token['refresh_token'],
                                           client_id=client['client_id'], client_secret=client['client_secret'],
                                           headers={'User-Agent': UA}, timeout=15)
        except Exception:
            raise OuraError('Oura authorization could not refresh. Check your connection or run morning-brief connect.') from None
        save(STATE / 'oura-token.json', dict(updated))
        return updated['access_token']


def fetch(today=None):
    today = today or dt.date.today()
    retry_at = load(STATE / 'rate-limit.json').get('until', 0)
    if retry_at > time.time():
        raise OuraError(f'Oura rate limit: retry in {int(retry_at - time.time()) + 1}s.')
    bearer = access_token()
    params = {'start_date': (today - dt.timedelta(days=config()['lookback_days'])).isoformat(),
              'end_date': (today + dt.timedelta(days=1)).isoformat()}
    data = {}
    with requests.Session() as session:
        session.headers.update({'User-Agent': UA, 'Authorization': f'Bearer {bearer}'})
        refreshed = False
        for collection in COLLECTIONS:
            rows, seen, page = [], set(), dict(params)
            for _ in range(20):
                try:
                    response = session.get(API + collection, params=page, timeout=(4, 10))
                    if response.status_code == 401 and not refreshed:
                        session.headers['Authorization'] = f'Bearer {access_token(force=True)}'
                        refreshed = True
                        response = session.get(API + collection, params=page, timeout=(4, 10))
                except requests.RequestException:
                    raise OuraError('Oura is unreachable. Any displayed data is the last saved copy.') from None
                if response.status_code == 403:
                    raise OuraError('Oura denied access: check active membership and the daily permission.')
                if response.status_code == 401:
                    raise OuraError('Oura authorization expired. Run morning-brief connect.')
                if response.status_code == 429:
                    try:
                        seconds = max(1, int(response.headers.get('Retry-After', '120')))
                    except ValueError:
                        seconds = 120
                    save(STATE / 'rate-limit.json', {'until': time.time() + seconds})
                    raise OuraError(f'Oura rate limit; waiting at least {seconds}s before another request.')
                if response.status_code != 200:
                    raise OuraError(f'Oura returned HTTP {response.status_code}; keeping the previous saved copy.')
                try:
                    payload = response.json()
                    batch = payload['data']
                    if not isinstance(batch, list) or any(not isinstance(row, dict) for row in batch):
                        raise ValueError()
                except (ValueError, KeyError, TypeError):
                    raise OuraError('Unexpected Oura response; keeping the previous saved copy.') from None
                rows.extend(batch)
                next_token = payload.get('next_token')
                if not next_token:
                    break
                if next_token in seen:
                    raise OuraError('Oura repeated a pagination token; keeping the previous saved copy.')
                seen.add(next_token)
                page['next_token'] = next_token
            else:
                raise OuraError('Oura returned too many pages for the requested date range.')
            data[collection] = rows
    data['fetched_at'] = dt.datetime.now().astimezone().isoformat()
    save(STATE / 'oura-cache.json', data)
    return data


def latest(rows, target, exact=False):
    valid = [r for r in rows if r.get('day') and (r['day'] == target if exact else r['day'] <= target)]
    return max(valid, key=lambda r: (r['day'], r.get('timestamp') or ''), default={})


def main_sleep(rows, day):
    valid = [r for r in rows if r.get('day') == day and r.get('total_sleep_duration')]
    main = [r for r in valid if r.get('type') == 'long_sleep']
    return max(main or valid, key=lambda r: r.get('total_sleep_duration', 0), default={})


def today_ready(data, day):
    return all(latest(data.get(key, []), day, exact=True).get('score') is not None
               for key in ('daily_sleep', 'daily_readiness')) and bool(main_sleep(data.get('sleep', []), day))
