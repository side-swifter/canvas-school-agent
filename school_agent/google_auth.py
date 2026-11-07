"""Google OAuth 2.0 with offline access. A long-lived refresh token is stored (0600) outside git and exchanged
for short-lived access tokens automatically. Uses YOUR OAuth client; no service-account shortcut.

Setup once:  school-agent auth-google   (prints a consent URL; you approve in your own browser; paste the redirect URL back)
Note: if the OAuth consent screen is in 'Testing' status Google expires refresh tokens after 7 days; publish the
app (or set it to 'In production' for personal use) to avoid weekly re-authorization."""
import json
import os
import time
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs
import requests
from .net import request_with_retry

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
SCOPE = 'https://www.googleapis.com/auth/calendar.events'
REDIRECT = 'http://127.0.0.1:8765/'


class GoogleAuthError(Exception):
    """Refresh token revoked/expired or client misconfigured: needs human re-authorization."""


class GoogleAuth:
    def __init__(self, secrets_dir, session=None, clock=time.time, sleep=None):
        self.path = Path(secrets_dir) / 'google.json'
        self.session, self.clock, self.sleep = session or requests, clock, sleep
        self._access, self._expiry = None, 0

    def configured(self):
        try:
            d = json.loads(self.path.read_text())
            return bool(d.get('client_id') and d.get('client_secret') and d.get('refresh_token'))
        except (OSError, ValueError):
            return False

    def _load(self):
        return json.loads(self.path.read_text())

    def _save(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.path.with_suffix('.tmp')
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, 'w') as fh:
            json.dump(data, fh)
        os.replace(tmp, self.path)

    def access_token(self):
        if self._access and self.clock() < self._expiry - 60:
            return self._access
        d = self._load()
        kw = {'sleep': self.sleep} if self.sleep else {}
        resp = request_with_retry(self.session, 'POST', TOKEN_URL, data={
            'client_id': d['client_id'], 'client_secret': d['client_secret'],
            'refresh_token': d['refresh_token'], 'grant_type': 'refresh_token'}, **kw)
        body = resp.json() if resp.content else {}
        if resp.status_code != 200:
            if body.get('error') in ('invalid_grant', 'invalid_client', 'unauthorized_client'):
                raise GoogleAuthError(f"Google refused the refresh token ({body.get('error')}): re-run `school-agent auth-google`")
            resp.raise_for_status()
        self._access = body['access_token']
        self._expiry = self.clock() + int(body.get('expires_in', 3600))
        if body.get('refresh_token'):     # Google may rotate
            d['refresh_token'] = body['refresh_token']
            self._save(d)
        return self._access

    # ---- one-time interactive authorization
    def consent_url(self, client_id):
        return AUTH_URL + '?' + urlencode({'client_id': client_id, 'redirect_uri': REDIRECT, 'response_type': 'code',
                                           'scope': SCOPE, 'access_type': 'offline', 'prompt': 'consent'})

    def exchange_code(self, client_id, client_secret, pasted):
        qs = parse_qs(urlparse(pasted.strip()).query)
        code = qs.get('code', [pasted.strip()])[0]
        resp = request_with_retry(self.session, 'POST', TOKEN_URL, data={
            'code': code, 'client_id': client_id, 'client_secret': client_secret,
            'redirect_uri': REDIRECT, 'grant_type': 'authorization_code'})
        body = resp.json()
        if resp.status_code != 200 or not body.get('refresh_token'):
            raise GoogleAuthError(f"Code exchange failed: {body.get('error_description') or body}")
        self._save({'client_id': client_id, 'client_secret': client_secret, 'refresh_token': body['refresh_token']})
