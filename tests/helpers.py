"""Test doubles: realistic documents, a fake Canvas HTTP session, fake Todoist/Calendar, scripted AI."""
import json
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from zoneinfo import ZoneInfo
from docx import Document
from school_agent.alerts import Alerter
from school_agent.canvas import CanvasClient
from school_agent.config import Config
from school_agent.main import Deps
from school_agent.store import Store

NOW = datetime(2026, 11, 7, 10, 0, tzinfo=ZoneInfo('America/New_York'))
BASE = 'https://school.instructure.com'


def make_pdf(lines):
    """Minimal text PDF (readable by pypdf) standing in for a teacher's syllabus."""
    def esc(s):
        return s.replace('\\', '\\\\').replace('(', '\\(').replace(')', '\\)')
    stream = 'BT /F1 11 Tf 40 760 Td 14 TL ' + ' '.join(f'({esc(l)}) Tj T*' for l in lines) + ' ET'
    objs = ['<< /Type /Catalog /Pages 2 0 R >>', '<< /Type /Pages /Kids [3 0 R] /Count 1 >>',
            '<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>',
            f'<< /Length {len(stream)} >>\nstream\n{stream}\nendstream', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    out, offsets = b'%PDF-1.4\n', []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f'{i} 0 obj\n{o}\nendobj\n'.encode()
    xref = len(out)
    out += f'xref\n0 {len(objs)+1}\n0000000000 65535 f \n'.encode()
    out += ''.join(f'{o:010d} 00000 n \n' for o in offsets).encode()
    out += f'trailer\n<< /Size {len(objs)+1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF'.encode()
    return out


def blank_pdf():
    from pypdf import PdfWriter
    w = PdfWriter(); w.add_blank_page(612, 792); b = BytesIO(); w.write(b); return b.getvalue()


def make_docx(rows, heading='Weekly schedule'):
    d = Document(); d.add_paragraph(heading)
    t = d.add_table(rows=0, cols=2)
    for a, b in rows:
        c = t.add_row().cells; c[0].text, c[1].text = a, b
    b = BytesIO(); d.save(b); return b.getvalue()


SYLLABUS_LINES = ['AP Physics Syllabus 2026-2027', 'Unit 3 Test: Thursday, November 12 in class.',
                  'Quiz 1: November 9 covers chapter 4.', 'Lab report 2 is due November 20.']
SCHEDULE_ROWS = [('Mon 11/9', 'Quiz 1'), ('Thu 11/12', 'Unit 3 Test'), ('Fri 11/13', 'No class - teacher workday'), ('Mon 11/16', 'Quiz 2')]


class Resp:
    def __init__(self, data, status=200, links=None):
        self.status_code, self._data, self.links = status, data, links or {}
        self.headers, self.content = {}, b'x'

    def json(self): return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            e = requests.HTTPError(f'{self.status_code}'); e.response = self; raise e


class FakeSession:
    """Routes API paths (without query) to canned JSON. Unlisted -> 404."""
    def __init__(self, routes, base=BASE):
        self.routes, self.headers, self.base, self.calls = routes, {}, base, []

    def request(self, method, url, **kw):
        path = url.split('?')[0].replace(self.base + '/api/v1/', '')
        self.calls.append(path)
        v = self.routes.get(path)
        if isinstance(v, int):
            return Resp({}, v)
        return Resp(v, 404) if v is None else Resp(v)


class FakeCanvas(CanvasClient):
    def __init__(self, routes, files=None):
        super().__init__(BASE, 'tok', session=FakeSession(routes))
        self.files = files or {}

    def download(self, url, max_bytes):
        if url not in self.files:
            raise PermissionError('403')
        v = self.files[url]
        if isinstance(v, Exception):
            raise v
        return v


def physics_routes(extra=None, announcements=None, assignments=None):
    r = {
        'courses': [{'id': 101, 'name': 'AP Physics', 'syllabus_body': '<p>See <a href="%s/courses/101/files/9001">syllabus</a> and the '
             '<a href="https://docs.google.com/document/d/DOC123/edit">schedule doc</a>.</p>' % BASE}],
        'courses/101/assignments': assignments if assignments is not None else [
            {'id': 1, 'name': 'Lab report 2', 'description': '<p>Write up the pendulum lab.</p>', 'due_at': '2026-11-21T04:59:59Z',
             'html_url': f'{BASE}/courses/101/assignments/1', 'updated_at': '2026-11-01T00:00:00Z', 'submission_types': ['online_upload']}],
        'courses/101/quizzes': [], 'courses/101/pages': [],
        'courses/101/modules': [{'id': 7, 'name': 'Unit 3 Dynamics', 'items': [
            {'type': 'Page', 'title': 'Unit 3 plan', 'page_url': 'unit-3-plan', 'html_url': f'{BASE}/courses/101/pages/unit-3-plan'},
            {'type': 'File', 'title': 'Schedule.docx', 'content_id': 9002, 'html_url': f'{BASE}/courses/101/files/9002'}]}],
        'courses/101/pages/unit-3-plan': {'page_id': 55, 'url': 'unit-3-plan', 'title': 'Unit 3 plan', 'html_url': f'{BASE}/courses/101/pages/unit-3-plan',
                                          'body': '<p>Unit 3 Test is on November 12.</p>', 'updated_at': '2026-11-02T00:00:00Z'},
        'announcements': announcements or [],
        'courses/101/files': [], 'calendar_events': [],
        'files/9001': {'id': 9001, 'display_name': 'syllabus.pdf', 'filename': 'syllabus.pdf', 'url': f'{BASE}/files/9001/download?v=1', 'updated_at': '2026-09-01T00:00:00Z'},
        'files/9002': {'id': 9002, 'display_name': 'Schedule.docx', 'filename': 'Schedule.docx', 'url': f'{BASE}/files/9002/download?v=1', 'updated_at': '2026-10-01T00:00:00Z'},
    }
    r.update(extra or {})
    return r


def physics_files():
    return {f'{BASE}/files/9001/download?v=1': make_pdf(SYLLABUS_LINES),
            f'{BASE}/files/9002/download?v=1': make_docx(SCHEDULE_ROWS),
            'https://docs.google.com/document/d/DOC123/export?format=txt': b'Class notes: nothing dated here'}


class ScriptedAI:
    """Stands in for the model: returns canned structured output per source title (evidence must be real quotes)."""
    def __init__(self, by_title=None):
        self.by_title, self.calls = by_title or {}, []

    def extract(self, material):
        self.calls.append(material['key'])
        v = self.by_title.get(material['title'], [])
        return v(material) if callable(v) else v


def item(kind, title, date, evidence, **kw):
    return {'kind': kind, 'title': title, 'date': date, 'evidence': evidence, 'confidence': .95, 'actionable': True,
            'status': 'scheduled', **kw}


DEFAULT_AI = {
    'AP Physics syllabus': [], 'syllabus.pdf': [
        item('test', 'Unit 3 Test', '2026-11-12', 'Unit 3 Test: Thursday, November 12 in class.'),
        item('quiz', 'Quiz 1', '2026-11-09', 'Quiz 1: November 9 covers chapter 4.'),
        item('assignment', 'Lab report 2', '2026-11-20', 'Lab report 2 is due November 20.')],
    'Unit 3 plan': [item('test', 'Unit 3 Test', '2026-11-12', 'Unit 3 Test is on November 12.')],
    'Schedule.docx': [item('quiz', 'Quiz 1', '2026-11-09', 'Mon 11/9 | Quiz 1'), item('test', 'Unit 3 Test', '2026-11-12', 'Thu 11/12 | Unit 3 Test'),
                      item('event', 'No class - teacher workday', '2026-11-13', 'Fri 11/13 | No class - teacher workday'),
                      item('quiz', 'Quiz 2', '2026-11-16', 'Mon 11/16 | Quiz 2')],
}


class FakeTodoist:
    def __init__(self, tasks=None):
        self._tasks, self.writes, self.n = {str(t['id']): t for t in (tasks or [])}, [], 1000

    def tasks(self, project_id=None): return list(self._tasks.values())
    def get(self, tid): return self._tasks.get(str(tid))

    def create(self, body):
        self.n += 1
        t = {'id': str(self.n), 'content': body['content'], 'description': body['description'], 'due': {'date': body['due_date']}, 'project_id': body.get('project_id')}
        self._tasks[t['id']] = t; self.writes.append(('create', body['content'])); return t

    def update(self, tid, body):
        t = self._tasks[str(tid)]; t.update(content=body['content'], description=body['description'], due={'date': body['due_date']})
        self.writes.append(('update', body['content'])); return t


class FakeCalendar:
    def __init__(self, other=None):
        self.events, self.other, self.writes, self.n = {}, other or {}, [], 0

    def find_by_oid(self, oid):
        return next((e for e in self.events.values() if e.get('extendedProperties', {}).get('private', {}).get('agent_oid') == oid), None)

    def list_day(self, cal, day):
        src = self.events.values() if cal == 'school-cal' else self.other.get(cal, [])
        return [e for e in src if (e['start'].get('date') or e['start'].get('dateTime', '')[:10]) == day]

    def get(self, eid): return self.events.get(eid)

    def insert(self, body):
        self.n += 1; e = {**body, 'id': f'ev{self.n}'}; self.events[e['id']] = e; self.writes.append(('insert', body['summary'])); return e

    def patch(self, eid, body):
        self.events[eid].update(body); self.writes.append(('patch', body['summary'])); return self.events[eid]


def make_cfg(tmp_path, **kw):
    base = dict(canvas_base_url=BASE, canvas_token='t', dry_run=True, todoist_project_id='proj-test', google_calendar_id='school-cal',
                google_readonly_calendar_ids=('canvas-feed',), secrets_dir=str(tmp_path / 'secrets'),
                db_path=str(tmp_path / 'state' / 'agent.sqlite'), state_dir=str(tmp_path / 'state'),
                ai_base_url='http://127.0.0.1:11434/v1', ai_model='local')
    base.update(kw)
    return Config(**base)


def make_deps(cfg, canvas=None, ai=None, todo=None, cal=None, now=NOW):
    store = Store(cfg.db_path)
    clock = {'now': now}
    d = Deps(store=store, alerter=Alerter(cfg, store, lambda: clock['now']), now_fn=lambda: clock['now'],
             canvas=canvas, extractor=ai, todoist=todo, gcal=cal)
    d.clock = clock
    return d
