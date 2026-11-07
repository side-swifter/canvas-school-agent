import json, os, stat
import pytest
from school_agent import todoist, gcal
from school_agent.google_auth import GoogleAuth, GoogleAuthError
from tests.helpers import *

CFG = make_cfg(__import__('pathlib').Path('/tmp/unused'))


def obl(**kw):
    o = {'oid': 'o1', 'course': 'AP Physics', 'kind': 'assignment', 'title': 'Lab report 2', 'date': '2026-11-20', 'time': None,
         'status': 'scheduled', 'evidence': 'due Nov 20', 'urls': ['http://src'], 'todoist_id': None, 'calendar_id': None,
         'adopted': 0, 'remote_fp': None}
    return {**o, **kw}


# ---- Todoist
def test_todoist_create_then_noop_no_duplicate():
    t, cache = FakeTodoist(), {}
    a, _, tid, fp = todoist.upsert(t, obl(), CFG, cache, False)
    assert a == 'create' and len(t.tasks()) == 1
    a2, *_ = todoist.upsert(t, obl(todoist_id=tid, remote_fp=fp), CFG, {}, False)
    assert a2 == 'noop' and len(t.writes) == 1


def test_todoist_state_loss_relinks_instead_of_duplicating():
    t = FakeTodoist(); a, _, tid, _ = todoist.upsert(t, obl(), CFG, {}, False)
    a2, detail, tid2, _ = todoist.upsert(t, obl(), CFG, {}, False)     # DB lost: no todoist_id
    assert a2 == 'adopt' and tid2 == tid and len(t.tasks()) == 1


def test_todoist_adopts_user_or_canvas_synced_task_in_any_project():
    t = FakeTodoist([{'id': '77', 'content': 'Lab report 2', 'description': '', 'due': {'date': '2026-11-20'}, 'project_id': 'other'}])
    a, detail, tid, _ = todoist.upsert(t, obl(), CFG, {}, False)
    assert a == 'adopt' and tid == '77' and not t.writes


def test_todoist_adopted_task_never_modified():
    t = FakeTodoist([{'id': '77', 'content': 'Lab report 2', 'description': '', 'due': {'date': '2026-11-18'}, 'project_id': 'x'}])
    a, *_ = todoist.upsert(t, obl(todoist_id='77', adopted=1, date='2026-11-20'), CFG, {}, False)
    assert a == 'review' and not t.writes


def test_todoist_user_edit_not_overwritten():
    t = FakeTodoist(); _, _, tid, fp = todoist.upsert(t, obl(), CFG, {}, False)
    t._tasks[tid]['content'] = 'my own wording'
    a, detail, *_ = todoist.upsert(t, obl(todoist_id=tid, remote_fp=fp, date='2026-11-25'), CFG, {}, False)
    assert a == 'review' and len(t.writes) == 1


def test_todoist_updates_own_unedited_task_on_date_change():
    t = FakeTodoist(); _, _, tid, fp = todoist.upsert(t, obl(), CFG, {}, False)
    a, _, _, fp2 = todoist.upsert(t, obl(todoist_id=tid, remote_fp=fp, date='2026-11-25'), CFG, {}, False)
    assert a == 'update' and t.get(tid)['due']['date'] == '2026-11-25' and len(t.tasks()) == 1


def test_todoist_deleted_or_completed_by_user_not_recreated():
    t = FakeTodoist()
    a, *_ = todoist.upsert(t, obl(todoist_id='gone'), CFG, {}, False)
    assert a == 'review' and not t.writes


def test_todoist_dry_run_and_missing_project_write_nothing():
    t = FakeTodoist()
    assert todoist.upsert(t, obl(), CFG, {}, True)[0] == 'create' and not t.writes
    import dataclasses
    assert todoist.upsert(t, obl(), dataclasses.replace(CFG, todoist_project_id=''), {}, False)[0] == 'review'


def test_todoist_cancelled_marks_not_deletes():
    t = FakeTodoist(); _, _, tid, fp = todoist.upsert(t, obl(), CFG, {}, False)
    todoist.upsert(t, obl(todoist_id=tid, remote_fp=fp, status='cancelled'), CFG, {}, False)
    assert t.get(tid)['content'].startswith('[CANCELLED]')


# ---- Calendar
def tob(**kw): return obl(**{'kind': 'test', 'title': 'Unit 3 Test', 'date': '2026-11-12', **kw})


def test_calendar_creates_all_day_event_once():
    c = FakeCalendar(); a, _, eid, fp = gcal.upsert(c, tob(), CFG, False)
    assert a == 'create' and c.events[eid]['start'] == {'date': '2026-11-12'} and c.events[eid]['end'] == {'date': '2026-11-13'}
    assert gcal.upsert(c, tob(calendar_id=eid, remote_fp=fp), CFG, False)[0] == 'noop' and len(c.events) == 1


def test_calendar_state_loss_relinks_by_extended_property():
    c = FakeCalendar(); _, _, eid, _ = gcal.upsert(c, tob(), CFG, False)
    a, _, eid2, _ = gcal.upsert(c, tob(), CFG, False)
    assert a == 'adopt' and eid2 == eid and len(c.events) == 1


def test_calendar_collision_with_canvas_feed_creates_nothing():
    c = FakeCalendar(other={'canvas-feed': [{'id': 'x', 'summary': 'Unit 3 Test [AP Physics]', 'start': {'date': '2026-11-12'}}]})
    a, detail, eid, _ = gcal.upsert(c, tob(), CFG, False)
    assert a == 'collision' and eid is None and not c.writes


def test_calendar_user_edit_preserved_and_update_on_change():
    c = FakeCalendar(); _, _, eid, fp = gcal.upsert(c, tob(), CFG, False)
    a, *_ = gcal.upsert(c, tob(calendar_id=eid, remote_fp=fp, date='2026-11-19'), CFG, False)
    assert a == 'update' and c.events[eid]['start']['date'] == '2026-11-19'
    c.events[eid]['summary'] = 'My renamed'
    a, *_ = gcal.upsert(c, tob(calendar_id=eid, remote_fp=fp, date='2026-11-26'), CFG, False)
    assert a == 'review'


def test_calendar_dry_run_and_deleted_event():
    c = FakeCalendar(); assert gcal.upsert(c, tob(), CFG, True)[0] == 'create' and not c.writes
    assert gcal.upsert(c, tob(calendar_id='nope'), CFG, False)[0] == 'review'


# ---- Google OAuth
class TokenSession:
    def __init__(self, replies): self.replies, self.calls = replies, 0
    def request(self, m, url, **kw):
        r = self.replies[min(self.calls, len(self.replies) - 1)]; self.calls += 1
        class R:
            status_code = r[0]; content = b'x'; headers = {}
            def json(s): return r[1]
            def raise_for_status(s): raise RuntimeError('http')
        return R()


def auth_with(tmp_path, replies, clock=lambda: 1000.0):
    a = GoogleAuth(str(tmp_path), session=TokenSession(replies), clock=clock, sleep=lambda s: None)
    a._save({'client_id': 'c', 'client_secret': 's', 'refresh_token': 'r'})
    return a


def test_secrets_file_is_private(tmp_path):
    a = auth_with(tmp_path, [(200, {'access_token': 'A', 'expires_in': 3600})])
    assert stat.S_IMODE(os.stat(a.path).st_mode) == 0o600 and a.configured()


def test_refresh_is_cached_until_expiry(tmp_path):
    t = [1000.0]
    a = auth_with(tmp_path, [(200, {'access_token': 'A', 'expires_in': 3600}), (200, {'access_token': 'B', 'expires_in': 3600})], clock=lambda: t[0])
    assert a.access_token() == 'A' and a.access_token() == 'A' and a.session.calls == 1
    t[0] += 3700
    assert a.access_token() == 'B'


def test_revoked_refresh_token_raises_actionable_error(tmp_path):
    a = auth_with(tmp_path, [(400, {'error': 'invalid_grant'})])
    with pytest.raises(GoogleAuthError, match='auth-google'):
        a.access_token()


def test_code_exchange_stores_refresh_token(tmp_path):
    a = GoogleAuth(str(tmp_path), session=TokenSession([(200, {'refresh_token': 'R', 'access_token': 'A'})]))
    a.exchange_code('cid', 'sec', 'http://127.0.0.1:8765/?code=4%2Fabc&scope=x')
    assert json.loads(a.path.read_text())['refresh_token'] == 'R'
    assert 'access_type=offline' in a.consent_url('cid') and 'prompt=consent' in a.consent_url('cid')
