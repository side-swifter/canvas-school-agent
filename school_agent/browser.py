"""Authenticated-browser fallback for content the Canvas API cannot return.

The browser reuses a session YOU established (Playwright storage_state saved outside git). It never
types credentials, never handles MFA, and treats a redirect to a login page as an expired session
(SessionExpired -> alert) rather than trying to get around it.  Navigation is restricted to allowed hosts.
"""
import logging
import re
from dataclasses import dataclass, field
from urllib.parse import urlparse

log = logging.getLogger('school-agent.browser')
LOGIN_HINTS = ('/login', 'idp/profile', '/saml', 'accounts.google.com', 'microsoftonline.com', '/cas/')


class SessionExpired(Exception):
    pass


class HostNotAllowed(Exception):
    pass


@dataclass
class PageResult:
    url: str
    text: str = ''
    links: list = field(default_factory=list)       # [(href, label)]
    body: bytes = b''                               # set for file downloads
    content_type: str = ''
    title: str = ''


def looks_like_login(url, title=''):
    u = url.lower()
    return any(h in u for h in LOGIN_HINTS) or title.strip().lower() in ('log in', 'login', 'sign in')


class PlaywrightDriver:
    """Thin wrapper; imported lazily so the core package works without playwright installed."""

    def __init__(self, storage_state, allowed_hosts, headless=True):
        self.storage_state, self.allowed, self.headless = storage_state, set(allowed_hosts), headless
        self._pw = self._browser = self._ctx = None

    def open(self):
        """Idempotent: one browser per scan, reused for every fallback fetch."""
        if self._ctx is None:
            from playwright.sync_api import sync_playwright  # optional dependency
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=self.headless)
            self._ctx = self._browser.new_context(storage_state=self.storage_state)
        return self

    def close(self):
        for obj in (self._ctx, self._browser):
            try:
                obj and obj.close()
            except Exception:
                pass
        self._pw and self._pw.stop()
        self._pw = self._browser = self._ctx = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.close()

    def _check(self, url):
        if urlparse(url).hostname not in self.allowed:
            raise HostNotAllowed(url)

    def goto(self, url):
        self._check(url)
        page = self._ctx.new_page()
        try:
            resp = page.goto(url, wait_until='networkidle', timeout=45000)
            if looks_like_login(page.url, page.title()):
                raise SessionExpired(page.url)
            ctype = (resp.headers.get('content-type', '') if resp else '')
            text = page.evaluate("() => (document.querySelector('#content')||document.body).innerText")
            links = page.evaluate("() => [...document.querySelectorAll('#content a[href]')].map(a=>[a.href,a.innerText.trim()])")
            return PageResult(page.url, text or '', [tuple(x) for x in links], content_type=ctype, title=page.title())
        finally:
            page.close()

    def download(self, url):
        self._check(url)
        resp = self._ctx.request.get(url, timeout=45000)
        if looks_like_login(resp.url):
            raise SessionExpired(resp.url)
        return PageResult(resp.url, body=resp.body(), content_type=resp.headers.get('content-type', ''))


COURSE_ID_RE = re.compile(r'/courses/(\d+)')
LINK_PATTERNS = {'page': r'/courses/\d+/pages/[^/?#]+', 'assignment': r'/courses/\d+/assignments/\d+',
                 'announcement': r'/courses/\d+/discussion_topics/\d+', 'file': r'/courses/\d+/files/\d+'}


class BrowserCollector:
    """Walks Canvas web UI pages for a course through a driver and yields materials in the standard shape."""

    def __init__(self, driver, base_url, max_pages_per_course=60):
        self.driver, self.base, self.max_pages = driver, base_url.rstrip('/'), max_pages_per_course

    def courses(self):
        res = self.driver.goto(f'{self.base}/courses')
        found = {}
        for href, label in res.links:
            m = re.match(rf'{re.escape(self.base)}/courses/(\d+)/?$', href)
            if m and label:
                found[m.group(1)] = label.splitlines()[0].strip()
        return [{'id': cid, 'name': name} for cid, name in found.items()]

    def materials(self, course):
        cid, name = course['id'], course['name']
        queue = [f'{self.base}/courses/{cid}/{p}' for p in ('assignments/syllabus', 'modules', 'announcements', 'pages', 'files')]
        seen, count = set(), 0
        while queue and count < self.max_pages:
            url = queue.pop(0)
            if url in seen:
                continue
            seen.add(url)
            count += 1
            res = self.driver.goto(url)
            kind = next((k for k, pat in LINK_PATTERNS.items() if re.search(pat, url)), 'page')
            yield {'key': f'browser:{cid}:{re.sub(r"[^a-zA-Z0-9]+", "-", urlparse(url).path.split("/courses/")[-1])}',
                   'type': 'browser', 'course': name, 'title': res.title or url, 'text': res.text, 'url': res.url, 'cid': cid}
            for href, _label in res.links:
                if any(re.search(p, href) for p in LINK_PATTERNS.values()) and f'/courses/{cid}/' in href:
                    queue.append(href.split('#')[0])
