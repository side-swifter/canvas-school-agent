"""Read-only Canvas REST ingestion: syllabus, assignments, quizzes, modules (+items), pages, announcements,
files, calendar events, and documents linked from any of them. API-first; failures that need a logged-in
browser are recorded in `fallbacks` so the browser collector can retry exactly those resources."""
import logging
import re
from datetime import datetime, timezone
from urllib.parse import quote, urlparse
from zoneinfo import ZoneInfo
import requests
from .documents import extract_links, google_export_url, SUPPORTED_EXT
from .net import request_with_retry

log = logging.getLogger('school-agent.canvas')
TEST_WORDS = re.compile(r'\b(exam|test|midterm|final|unit test)\b', re.I)
FILE_LINK = re.compile(r'/courses/(\d+)/files/(\d+)')


class CanvasAuthError(Exception):
    pass


def local_due(iso_utc, tz):
    if not iso_utc:
        return None, None
    dt = datetime.fromisoformat(iso_utc.replace('Z', '+00:00')).astimezone(ZoneInfo(tz))
    time = None if (dt.hour, dt.minute) in ((23, 59), (0, 0)) else dt.strftime('%H:%M')
    return dt.date().isoformat(), time


class CanvasClient:
    def __init__(self, base_url, token, timezone_name='America/New_York', session=None, sleep=None):
        self.base = base_url.rstrip('/')
        self.host = urlparse(self.base).hostname
        self.tz = timezone_name
        self.session = session or requests.Session()
        self.session.headers.update({'Authorization': f'Bearer {token}'})
        self.sleep = sleep
        self.fallbacks = []     # resources the API refused; for the browser collector
        self.seen_files = set()

    # ---- HTTP
    def _req(self, url, params=None):
        kw = {'params': params} if params else {}
        if self.sleep:
            kw['sleep'] = self.sleep
        resp = request_with_retry(self.session, 'GET', url, **kw)
        if resp.status_code == 401:
            raise CanvasAuthError('Canvas rejected the token (401)')
        resp.raise_for_status()
        return resp

    def pages(self, route, **params):
        url = f"{self.base}/api/v1/{route.lstrip('/')}"
        while url:
            resp = self._req(url, params or None)
            payload = resp.json()
            if not isinstance(payload, list):
                raise ValueError(f'Expected collection at {route}')
            yield from payload
            url = resp.links.get('next', {}).get('url')
            params = None

    def get(self, route, **params):
        return self._req(f"{self.base}/api/v1/{route.lstrip('/')}", params or None).json()

    def courses(self):
        self.seen_files, self.fallbacks = set(), []      # fresh per scan, even if the client is reused
        return [c for c in self.pages('courses', enrollment_state='active', per_page=100,
                                      **{'include[]': ['syllabus_body', 'term']})
                if 'id' in c and not c.get('access_restricted_by_date') and c.get('name')]

    # ---- helpers
    def _soft(self, what, url, fn):
        """Run fn; treat 403/404 as unavailable (queue browser fallback), re-raise everything else."""
        try:
            return fn()
        except requests.HTTPError as err:
            code = err.response.status_code if err.response is not None else 0
            if code in (403, 404):
                log.warning('API unavailable (%s) for %s', code, what)
                self.fallbacks.append({'what': what, 'url': url, 'status': code})
                return None
            raise

    def _mat(self, cid, course, typ, rid, title, text, url, updated='', structured=None, **extra):
        m = {'key': f'canvas:{cid}:{typ}:{rid}', 'type': typ, 'course': course, 'title': title or str(rid),
             'text': text or '', 'url': url or '', 'updated_at': updated or '', 'cid': cid}
        if structured:
            m['structured'] = structured
        m.update(extra)
        return m

    def _structured(self, kind, title, iso, evidence_label):
        date, time = local_due(iso, self.tz)
        if not date:
            return []
        return [{'kind': kind, 'title': title, 'date': date, 'time': time, 'status': 'scheduled', 'confidence': 1.0,
                 'evidence': f'Canvas {evidence_label}: {iso}'}]

    def _linked(self, cid, course, html, origin_url):
        """Documents linked from an HTML body: Canvas files via API, public Google Docs via export."""
        for link in extract_links(html, self.base):
            fm = FILE_LINK.search(link)
            if fm and urlparse(link).hostname == self.host and fm.group(2) not in self.seen_files:
                self.seen_files.add(fm.group(2))
                meta = self._soft(f'file {fm.group(2)}', link, lambda: self.get(f'files/{fm.group(2)}'))
                if meta:
                    yield self._file_material(cid, course, meta)
                continue
            g = google_export_url(link)
            if g:
                url, doc_id = g
                yield self._mat(cid, course, 'gdoc', doc_id, f'Linked Google doc {doc_id}', '', link,
                                download_url=url, filename='linked.txt', linked_from=origin_url, external=True)

    def _file_material(self, cid, course, f):
        return self._mat(cid, course, 'file', f['id'], f.get('display_name') or f.get('filename'), '',
                         f.get('html_url') or f"{self.base}/courses/{cid}/files/{f['id']}",
                         f.get('updated_at') or f.get('modified_at'), download_url=f.get('url'),
                         filename=f.get('filename') or f.get('display_name'),
                         browser_url=f"{self.base}/courses/{cid}/files/{f['id']}")

    # ---- main entry
    def materials(self, course):
        cid, name = course['id'], course.get('name', str(course['id']))
        prefix = f'courses/{cid}'
        if course.get('syllabus_body'):
            yield self._mat(cid, name, 'syllabus', 'syllabus', f'{name} syllabus', course['syllabus_body'],
                            f'{self.base}/courses/{cid}/assignments/syllabus')
            yield from self._linked(cid, name, course['syllabus_body'], f'{self.base}/courses/{cid}/assignments/syllabus')

        def collect(what, route, **params):
            return self._soft(what, f'{self.base}/{prefix}/{what}', lambda: list(self.pages(route, per_page=100, **params))) or []

        for a in collect('assignments', f'{prefix}/assignments'):
            title = a.get('name')
            kind = 'quiz' if 'online_quiz' in (a.get('submission_types') or []) else 'assignment'
            if TEST_WORDS.search(title or '') and kind != 'assignment':
                kind = 'test' if re.search(r'exam|test|midterm|final', title, re.I) else kind
            body = (a.get('description') or '')
            text = body + (f"\nCanvas due_at: {a.get('due_at')}" if a.get('due_at') else '')
            yield self._mat(cid, name, 'assignment', a['id'], title, text, a.get('html_url'), a.get('updated_at'),
                            structured=self._structured(kind, title, a.get('due_at'), 'due_at'))
            yield from self._linked(cid, name, body, a.get('html_url') or '')

        for q in collect('quizzes', f'{prefix}/quizzes'):
            if q.get('assignment_id'):
                continue        # already represented by its assignment
            yield self._mat(cid, name, 'quiz', q['id'], q.get('title'), q.get('description') or '', q.get('html_url'),
                            q.get('updated_at'), structured=self._structured('quiz', q.get('title'), q.get('due_at'), 'due_at'))

        for mod in collect('modules', f'{prefix}/modules', **{'include[]': ['items']}):
            lines = [f"Module: {mod.get('name')}" + (f" (unlocks {mod['unlock_at']})" if mod.get('unlock_at') else '')]
            for item in mod.get('items') or []:
                lines.append(f"- {item.get('type')}: {item.get('title')}")
                if item.get('type') == 'Page' and item.get('page_url'):
                    page = self._soft(f"page {item['page_url']}", item.get('html_url') or '',
                                      lambda: self.get(f"{prefix}/pages/{quote(item['page_url'], safe='')}"))
                    if page:
                        yield self._page_material(cid, name, page)
                elif item.get('type') == 'File' and item.get('content_id') and str(item['content_id']) not in self.seen_files:
                    self.seen_files.add(str(item['content_id']))
                    meta = self._soft(f"file {item['content_id']}", item.get('html_url') or '',
                                      lambda: self.get(f"files/{item['content_id']}"))
                    if meta:
                        yield self._file_material(cid, name, meta)
                elif item.get('type') == 'ExternalUrl' and item.get('external_url'):
                    g = google_export_url(item['external_url'])
                    if g:
                        yield self._mat(cid, name, 'gdoc', g[1], item.get('title'), '', item['external_url'],
                                        download_url=g[0], filename='linked.txt', external=True)
            yield self._mat(cid, name, 'module', mod['id'], f"Module {mod.get('name')}", '\n'.join(lines),
                            f'{self.base}/courses/{cid}/modules')

        for p in collect('pages', f'{prefix}/pages'):
            if not p.get('body') and p.get('url'):
                p = self._soft(f"page {p['url']}", p.get('html_url') or '',
                               lambda p=p: self.get(f"{prefix}/pages/{quote(p['url'], safe='')}")) or p
            if p.get('body') or p.get('title'):
                yield self._page_material(cid, name, p)
                yield from self._linked(cid, name, p.get('body') or '', p.get('html_url') or '')

        for a in collect('announcements', 'announcements', **{'context_codes[]': [f'course_{cid}']}):
            yield self._mat(cid, name, 'announcement', a['id'], a.get('title'), a.get('message'), a.get('html_url'),
                            a.get('posted_at') or a.get('updated_at'))
            yield from self._linked(cid, name, a.get('message') or '', a.get('html_url') or '')

        for f in collect('files', f'{prefix}/files'):
            if str(f['id']) not in self.seen_files and (f.get('filename') or '').lower().endswith(SUPPORTED_EXT):
                self.seen_files.add(str(f['id']))
                yield self._file_material(cid, name, f)

        for e in collect('calendar_events', 'calendar_events', type='event', **{'context_codes[]': [f'course_{cid}']},
                         start_date='2000-01-01', end_date='2100-01-01'):
            yield self._mat(cid, name, 'calendar', e['id'], e.get('title'), e.get('description') or '', e.get('html_url'),
                            e.get('updated_at'), structured=self._structured('event', e.get('title'), e.get('start_at'), 'event start'))

    def _page_material(self, cid, name, p):
        return self._mat(cid, name, 'page', p.get('page_id') or p.get('url'), p.get('title'), p.get('body'),
                         p.get('html_url'), p.get('updated_at'))

    # ---- downloads
    def download(self, url, max_bytes):
        if not url.startswith(('https://', 'http://')):
            raise ValueError('Invalid document URL')
        host = urlparse(url).hostname
        # Never send the Canvas bearer token to any other host.
        headers = {'Authorization': self.session.headers.get('Authorization')} if host == self.host else {}
        resp = request_with_retry(requests, 'GET', url, headers=headers, timeout=45, stream=True, allow_redirects=True)
        with resp:
            if resp.status_code in (401, 403, 404):
                raise PermissionError(f'{resp.status_code} fetching document')
            resp.raise_for_status()
            if int(resp.headers.get('Content-Length', '0') or 0) > max_bytes:
                raise ValueError('Document too large')
            chunks, total = [], 0
            for part in resp.iter_content(chunk_size=65536):
                total += len(part)
                if total > max_bytes:
                    raise ValueError('Document too large')
                chunks.append(part)
            return b''.join(chunks)
