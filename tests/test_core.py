from datetime import date, timedelta
from types import SimpleNamespace
from school_agent.documents import strip_html, extract_document
from school_agent.main import normalize, browser_materials
from school_agent.store import Store

CFG = SimpleNamespace(lookahead_days=90)
MATERIAL = {'key': 'canvas:1:page:4', 'url': 'https://example.edu/courses/1/pages/4', 'course': 'Physics', 'title': 'Schedule'}

def test_html_to_text():
    assert 'Quiz Friday' in strip_html('<div><b>Quiz</b> Friday</div>')

def test_text_doc():
    assert extract_document(b'Exam November 9', 'notes.txt') == 'Exam November 9'

def test_extract_dedup_key_when_date_changes():
    day1 = (date.today() + timedelta(days=7)).isoformat()
    day2 = (date.today() + timedelta(days=8)).isoformat()
    base = {'kind': 'test', 'title': 'Unit Test', 'actionable': True, 'confidence': .99, 'evidence': 'Unit Test next week'}
    a = normalize([{**base, 'date': day1}], MATERIAL, CFG)
    b = normalize([{**base, 'date': day2}], MATERIAL, CFG)
    assert a[0]['key'] == b[0]['key']
    assert a[0]['date'] != b[0]['date']

def test_reject_low_confidence_and_invalid_dates():
    assert normalize([{'kind': 'quiz', 'title':'Quiz', 'date': 'unknown', 'evidence':'Quiz', 'confidence': 1, 'actionable': True}], MATERIAL, CFG) == []
    assert normalize([{'kind': 'quiz', 'title':'Quiz', 'date': '2026-11-10', 'evidence':'Quiz', 'confidence': .2, 'actionable': True}], MATERIAL, CFG) == []

def test_store_idempotence(tmp_path):
    store = Store(str(tmp_path / 'state.db'))
    assert not store.unchanged('a', 'v1')
    store.save_source('a', 'v1', 'contents', 'now')
    assert store.unchanged('a', 'v1')
    assert not store.unchanged('a', 'v2')
    store.save_obligation('id', 'a', {'title':'Test'}, 'fingerprint', 't1', None, 'now')
    assert store.obligation('id')['todoist_id'] == 't1'

def test_browser_export(tmp_path):
    p = tmp_path / 'export.json'
    p.write_text('[{"key":"course:1:doc:2","course":"Physics","title":"Schedule","text":"A unit exam is on Friday","url":"https://example.edu/schedule"}]')
    materials = browser_materials(str(p))
    assert len(materials) == 1
    assert materials[0]['key'] == 'browser:course:1:doc:2'
