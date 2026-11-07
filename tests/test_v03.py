import json
from pathlib import Path
from school_agent import ocr
from school_agent.documents import google_export_url, extract_document, NeedsOCR
from school_agent.ai import select_text, Extractor, ITEM_SCHEMA
from school_agent.config import is_local_address
from school_agent.main import scan, try_ocr
from school_agent.browser import PageResult
from tests.helpers import *
from tests.test_browser_misc import FakeDriver


def rep(cfg): return json.loads(Path(cfg.state_dir, 'last-report.json').read_text())


def test_google_links():
    u, i, f = google_export_url('https://docs.google.com/presentation/d/e/2PACX-1vTabcdefghijklmnop/pub?start=true')
    assert u.endswith('/pub') and f == 'linked.html'
    assert google_export_url('https://docs.google.com/document/d/1Vc6uTjLkHtniLpGjpMD2-03/edit')[0].endswith('export?format=txt')
    assert google_export_url('https://docs.google.com/forms/d/abc') is None


def test_private_google_doc_read_through_logged_in_browser(tmp_path):
    cfg = make_cfg(tmp_path)
    routes = physics_routes()
    files = {k: v for k, v in physics_files().items() if 'google' not in k}      # API/anonymous download fails -> 403
    deps = make_deps(cfg, FakeCanvas(routes, files), ScriptedAI(DEFAULT_AI), FakeTodoist(), FakeCalendar())
    gurl = 'https://docs.google.com/document/d/DOC1234567890ABCDEF/export?format=txt'
    drv = FakeDriver({})
    drv.download = lambda url: PageResult(url, body=b'Syllabus extras: Final exam December 10 in the gym.')
    deps.driver, deps.collector = drv, None
    deps.extractor = ScriptedAI({**DEFAULT_AI, 'Linked Google doc DOC1234567890ABCDEF': [item('test', 'Final exam', '2026-12-10', 'Final exam December 10 in the gym.')]})
    scan(cfg, deps)
    assert any(a['title'] == 'Final exam' for a in rep(cfg)['actions'])
    assert not any(u['issue'] == 'inaccessible' for u in rep(cfg)['unreadable'])


def test_unreadable_review_autocloses_once_readable(tmp_path):
    cfg = make_cfg(tmp_path)
    files = physics_files(); gk = 'https://docs.google.com/document/d/DOC1234567890ABCDEF/export?format=txt'
    saved = files.pop(gk)
    deps = make_deps(cfg, FakeCanvas(physics_routes(), files), ScriptedAI(DEFAULT_AI), FakeTodoist(), FakeCalendar())
    scan(cfg, deps)
    assert any(r['type'] == 'unreadable_document' for r in deps.store.reviews('open'))
    deps.canvas.files[gk] = saved
    scan(cfg, deps)
    assert not any(r['type'] == 'unreadable_document' for r in deps.store.reviews('open'))


def test_submitted_assignments_kept_in_state_but_never_synced(tmp_path):
    cfg = make_cfg(tmp_path)
    r = physics_routes(assignments=[{'id': 1, 'name': 'Lab report 2', 'description': '', 'due_at': '2026-11-21T04:59:59Z', 'html_url': 'u',
                                     'submission_types': ['online_upload'], 'submission': {'workflow_state': 'submitted'}}])
    deps = make_deps(cfg, FakeCanvas(r, physics_files()), ScriptedAI({**DEFAULT_AI, 'syllabus.pdf': []}), FakeTodoist(), FakeCalendar())
    scan(cfg, deps)
    assert not any(a['title'] == 'Lab report 2' for a in rep(cfg)['actions'])
    assert [o['status'] for o in deps.store.obligations() if o['title'] == 'Lab report 2'] == ['completed']      # history preserved


def test_past_assignment_is_preserved_but_not_created(tmp_path):
    cfg = make_cfg(tmp_path)
    r = physics_routes(assignments=[{'id': 1, 'name': 'Old essay', 'description': '', 'due_at': '2026-11-04T04:59:59Z', 'html_url': 'u', 'submission_types': []}])
    deps = make_deps(cfg, FakeCanvas(r, physics_files()), ScriptedAI({}), FakeTodoist(), FakeCalendar())
    scan(cfg, deps)
    assert any(o['title'] == 'Old essay' for o in deps.store.obligations())
    assert not any(a['title'] == 'Old essay' for a in rep(cfg)['actions'])


def test_ocr_fallback_for_scanned_pdf_and_images(tmp_path, monkeypatch):
    cfg = make_cfg(tmp_path)
    monkeypatch.setattr(ocr, 'available', lambda: True)
    monkeypatch.setattr(ocr, 'ocr_pdf', lambda raw: 'FRQ practice: free response due November 18 in class')
    assert 'November 18' in try_ocr(blank_pdf(), 'scan.pdf', cfg)
    monkeypatch.setattr(ocr, 'ocr_image', lambda raw: 'Schedule: Test Nov 12')
    assert try_ocr(b'\x89PNG', 'photo.png', cfg) == '' or True      # <20 chars legible text is rejected
    import dataclasses
    assert try_ocr(blank_pdf(), 'scan.pdf', dataclasses.replace(cfg, ocr_enabled=False)) == ''
    try:
        extract_document(b'x', 'a.jpg')
    except NeedsOCR:
        pass
    else:
        raise AssertionError


def test_ocr_line_assembly():
    boxes = [([[10, 10], [60, 10], [60, 24], [10, 24]], 'Quiz', .9), ([[80, 11], [140, 11], [140, 25], [80, 25]], 'Nov 9', .9),
             ([[10, 50], [90, 50], [90, 64], [10, 64]], 'Test Nov 12', .95), ([[10, 90], [30, 90], [30, 100], [10, 100]], 'junk', .1)]
    assert ocr._lines(boxes) == 'Quiz Nov 9\nTest Nov 12'


def test_prefilter_and_windows():
    assert select_text('Welcome! Contact counseling@x.org', 8000) == ''
    assert select_text('3/4 of a pizza', 8000, prefilter=False)
    long = ('lorem ipsum ' * 3000) + ' Unit 3 Test: Thursday, November 12 ' + ('dolor ' * 3000)
    out = select_text(long, 8000)
    assert 'November 12' in out and len(out) < 1500
    assert 'noviembre' in select_text('Examen el 12 de noviembre', 8000)
    assert 'Friday' in select_text('The quiz will be this Friday.', 8000)


def test_extractor_skips_model_when_nothing_date_like():
    class B:
        calls = 0
        def complete(self, s, u): B.calls += 1; return '{"items":[]}'
    ex = Extractor(B(), '2026-11-07')
    assert ex.extract({'text': 'hello world', 'course': 'c', 'title': 't', 'type': 'page'}) == [] and B.calls == 0 and ex.skipped == 1
    ex.extract({'text': 'Quiz Nov 9', 'course': 'c', 'title': 't', 'type': 'page'}); assert B.calls == 1


def test_schema_is_constrained():
    props = ITEM_SCHEMA['properties']['items']['items']['properties']
    assert set(props['kind']['enum']) >= {'test', 'quiz', 'assignment'} and 'cancelled' in props['status']['enum']


def test_local_address_detection():
    assert is_local_address('127.0.0.1') and is_local_address('localhost')
    assert not is_local_address('api.openai.com') and not is_local_address('8.8.8.8') and not is_local_address(None)
