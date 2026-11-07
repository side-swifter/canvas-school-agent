import json
from pathlib import Path
import pytest
from school_agent.browser import BrowserCollector, PageResult, SessionExpired, looks_like_login
from school_agent.main import scan, review_cli, browser_export_materials
from school_agent.reconcile import reconcile
from tests.helpers import *
from tests.test_reconcile_docs_canvas import ob, rec
import argparse


def test_rename_merge_does_not_collapse_distinct_numbered_items():
    d, *_ = rec([ob('a', 'Quiz 1', '2026-11-09', 'quiz'), ob('b', 'Quiz 2', '2026-11-09', 'quiz')])
    assert len(d) == 2
    d, *_ = rec([ob('a', 'Chapter 4 reading', '2026-11-09', 'assignment'), ob('b', 'Chapter 5 reading', '2026-11-09', 'assignment')])
    assert len(d) == 2


def test_rename_merge_same_date_different_sources():
    d, *_ = rec([ob('api', 'Pendulum lab report', '2026-11-20', 'assignment', structured=1), ob('pdf', 'Lab report 2', '2026-11-20', 'assignment')])
    assert len(d) == 1


class FakeDriver:
    def __init__(self, pages, expired=False): self.pages, self.expired, self.visited = pages, expired, []
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def open(self): return self
    def close(self): pass
    def goto(self, url):
        self.visited.append(url)
        if self.expired: raise SessionExpired('https://sso.example/login')
        return self.pages.get(url, PageResult(url, 'empty'))
    def download(self, url): return PageResult(url, body=make_pdf(SYLLABUS_LINES))


PAGES = {f'{BASE}/courses': PageResult(f'{BASE}/courses', links=[(f'{BASE}/courses/101', 'AP Physics\nTeacher'), (f'{BASE}/courses/101/grades', 'Grades')]),
         f'{BASE}/courses/101/modules': PageResult(f'{BASE}/courses/101/modules', 'Module 1: Unit 3 Test November 12', title='Modules',
                                                    links=[(f'{BASE}/courses/101/pages/unit-3-plan', 'plan'), ('https://evil.example/x', 'x')])}


def test_browser_collector_discovers_courses_and_follows_in_course_links_only():
    drv = FakeDriver(PAGES); col = BrowserCollector(drv, BASE)
    courses = col.courses()
    assert courses == [{'id': '101', 'name': 'AP Physics'}]
    mats = list(col.materials(courses[0]))
    assert any('Unit 3 Test November 12' in m['text'] for m in mats)
    assert f'{BASE}/courses/101/pages/unit-3-plan' in drv.visited and 'https://evil.example/x' not in drv.visited


def test_login_redirect_detection():
    assert looks_like_login('https://idp.school.org/idp/profile/SAML2/Redirect/SSO') and looks_like_login('https://x.instructure.com/login/canvas')
    assert not looks_like_login('https://x.instructure.com/courses/1')


def test_expired_browser_session_alerts_and_does_not_crash(tmp_path):
    cfg = make_cfg(tmp_path)
    deps = make_deps(cfg, canvas=None, ai=ScriptedAI(DEFAULT_AI), todo=FakeTodoist(), cal=FakeCalendar())
    deps.collector, deps.driver = BrowserCollector(FakeDriver({}, expired=True), BASE), FakeDriver({}, expired=True)
    assert scan(cfg, deps) == 1
    assert any('session expired' in a['title'] for a in deps.alerter.sent)


def test_browser_fallback_used_when_api_forbids_resource(tmp_path):
    cfg = make_cfg(tmp_path)
    canvas = FakeCanvas(physics_routes(extra={'courses/101/modules': 403}), physics_files())
    deps = make_deps(cfg, canvas, ScriptedAI({**DEFAULT_AI, 'Modules': [item('test', 'Unit 3 Test', '2026-11-12', 'Unit 3 Test November 12')]}), FakeTodoist(), FakeCalendar())
    fb_url = f'{BASE}/courses/101/modules'
    drv = FakeDriver({fb_url: PageResult(fb_url, 'Module 1: Unit 3 Test November 12', title='Modules')})
    deps.collector, deps.driver = BrowserCollector(drv, BASE), drv
    scan(cfg, deps)
    assert fb_url in drv.visited
    assert any(k.startswith('browser:') for k in deps.extractor.calls)


def test_export_json_contract(tmp_path):
    p = tmp_path / 'e.json'
    p.write_text(json.dumps([{'key': 'c:1', 'course': 'P', 'title': 'T', 'text': 'Exam on Friday Nov 13', 'url': 'u'}, {'key': 'bad'}]))
    assert [m['key'] for m in browser_export_materials(str(p))] == ['browser:c:1']


def test_webhook_failure_never_breaks_scan(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path, alert_webhook_url='http://127.0.0.1:1/hook')
    deps = make_deps(cfg, FakeCanvas({'courses': 401}), None)
    monkeypatch.setattr('school_agent.alerts.request_with_retry', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('down')))
    assert scan(cfg, deps) == 1
    assert Path(cfg.state_dir, 'alerts.jsonl').exists()


def test_review_cli_roundtrip(tmp_path, capsys):
    cfg = make_cfg(tmp_path)
    deps = make_deps(cfg, FakeCanvas(physics_routes(), physics_files()), ScriptedAI({**DEFAULT_AI, 'Unit 3 plan': [item('test', 'Unit 3 Test', None, 'Unit 3 Test is on November 12.', ambiguous=True)]}))
    scan(cfg, deps)
    review_cli(cfg, argparse.Namespace(action='list', id=None, date=None))
    assert 'ambiguous_or_missing_date' in capsys.readouterr().out
    rid = deps.store.reviews('open')[0]['id']
    assert review_cli(cfg, argparse.Namespace(action='approve', id=rid, date=None)) == 1      # dateless needs explicit date
    assert review_cli(cfg, argparse.Namespace(action='reject', id=rid, date=None)) == 0
    assert not [r for r in deps.store.reviews('open') if r['id'] == rid]
    scan(cfg, deps, force=True)
    assert not [r for r in deps.store.reviews('open') if r['id'] == rid]       # rejected item does not reappear


def test_past_items_not_newly_created_and_published_gdoc_links_ignored(tmp_path):
    from school_agent.documents import google_export_url
    assert google_export_url('https://docs.google.com/document/d/e/2PACX-abc/pub') is None
    assert google_export_url('https://docs.google.com/document/d/1Vc6uTjLkHtniLpGjpMD2-03He8m/edit')
    cfg = make_cfg(tmp_path)
    r = physics_routes(assignments=[{'id': 1, 'name': 'Old essay', 'description': '', 'due_at': '2026-11-04T04:59:59Z', 'html_url': 'u', 'submission_types': []}])
    deps = make_deps(cfg, FakeCanvas(r, physics_files()), ScriptedAI(DEFAULT_AI), FakeTodoist(), FakeCalendar())
    scan(cfg, deps)
    assert not any(a['title'] == 'Old essay' for a in json.loads(Path(cfg.state_dir, 'last-report.json').read_text())['actions'])


def test_too_large_document_is_review_item_not_error(tmp_path):
    cfg = make_cfg(tmp_path)
    canvas = FakeCanvas(physics_routes(), {**physics_files(), f'{BASE}/files/9001/download?v=1': ValueError('Document too large')})
    deps = make_deps(cfg, canvas, ScriptedAI(DEFAULT_AI), FakeTodoist(), FakeCalendar())
    assert scan(cfg, deps) == 0
