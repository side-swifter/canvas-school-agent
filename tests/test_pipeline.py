"""End-to-end scans against fake Canvas / model / Todoist / Calendar. No network, no credentials."""
import json
import dataclasses
from pathlib import Path
from school_agent.main import scan, review_cli, live_approved
from school_agent.store import Store
from tests.helpers import *
import argparse


def setup(tmp_path, ai=None, todo=None, cal=None, announcements=None, assignments=None, cfg_kw=None, extra=None):
    cfg = make_cfg(tmp_path, **(cfg_kw or {}))
    canvas = FakeCanvas(physics_routes(extra=extra, announcements=announcements, assignments=assignments), physics_files())
    deps = make_deps(cfg, canvas, ScriptedAI(DEFAULT_AI) if ai is None else ai, todo or FakeTodoist(), cal or FakeCalendar())
    return cfg, deps


def report(cfg):
    return json.loads(Path(cfg.state_dir, 'last-report.json').read_text())


def test_first_dry_run_reports_plan_and_writes_nothing(tmp_path):
    cfg, deps = setup(tmp_path)
    assert scan(cfg, deps) == 0
    rep = report(cfg)
    assert rep['mode'] == 'dry-run' and not deps.todoist.writes and not deps.gcal.writes
    titles = sorted((a['target'], a['title']) for a in rep['actions'])
    # Cross-source dedupe: syllabus PDF + module page + schedule docx all say "Unit 3 Test" -> one entry
    assert titles.count(('calendar', 'Unit 3 Test')) == 1 and titles.count(('calendar', 'Quiz 1')) == 1
    assert ('todoist', 'Lab report 2') in titles                          # assignments -> Todoist ONLY
    assert not any(a['target'] == 'calendar' and a['title'] == 'Lab report 2' for a in rep['actions'])
    assert all(a['action'] == 'create' and a['dedupe_checked'] for a in rep['actions'])
    assert rep['sources']['file'] == 2 and rep['sources']['page'] == 1


def test_dry_run_is_repeatable_and_does_not_reextract(tmp_path):
    cfg, deps = setup(tmp_path)
    scan(cfg, deps); first_calls = len(deps.extractor.calls); n = len(report(cfg)['actions'])
    scan(cfg, deps)
    assert len(deps.extractor.calls) == first_calls and len(report(cfg)['actions']) == n     # no repeat AI uploads


def test_live_refused_without_explicit_approval_file(tmp_path):
    cfg, deps = setup(tmp_path, cfg_kw={'dry_run': False})
    assert scan(cfg, deps) == 2 and not deps.todoist.writes and not deps.gcal.writes


def live(tmp_path, **kw):
    cfg, deps = setup(tmp_path, cfg_kw={'dry_run': False}, **kw)
    Path(cfg.secrets_dir).mkdir(parents=True, exist_ok=True); (Path(cfg.secrets_dir) / 'live_writes_approved').write_text('x')
    return cfg, deps


def test_live_scans_are_idempotent_no_duplicates(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    t, c = len(deps.todoist.tasks()), len(deps.gcal.events)
    assert t == 1 and c == 4            # Lab report 2 | Unit 3 Test, Quiz 1, Quiz 2, No class
    for _ in range(3):
        scan(cfg, deps, force=True)
    assert len(deps.todoist.tasks()) == t and len(deps.gcal.events) == c


def test_rebuilt_state_does_not_duplicate(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    tmp = tmp_path / 'fresh'; tmp.mkdir()
    cfg2 = dataclasses.replace(cfg, db_path=str(tmp / 'a.sqlite'))
    deps2 = make_deps(cfg2, deps.canvas, ScriptedAI(DEFAULT_AI), deps.todoist, deps.gcal)
    scan(cfg2, deps2)
    assert len(deps.todoist.tasks()) == 1 and len(deps.gcal.events) == 4


def test_existing_user_task_and_canvas_feed_event_not_duplicated(tmp_path):
    todo = FakeTodoist([{'id': '5', 'content': 'Lab report 2', 'description': '', 'due': {'date': '2026-11-20'}, 'project_id': 'inbox'}])
    cal = FakeCalendar(other={'canvas-feed': [{'id': 'f', 'summary': 'Unit 3 Test [AP Physics]', 'start': {'date': '2026-11-12'}}]})
    cfg, deps = live(tmp_path, todo=todo, cal=cal)
    scan(cfg, deps)
    assert not todo.writes and 'Unit 3 Test' not in ' '.join(w[1] for w in cal.writes)
    assert len(todo.tasks()) == 1 and len(cal.events) == 3


def test_modified_due_date_updates_instead_of_creating(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    deps.canvas.session.routes['courses/101/assignments'][0].update(due_at='2026-11-25T04:59:59Z', updated_at='2026-11-08T00:00:00Z')
    scan(cfg, deps)
    tasks = deps.todoist.tasks()
    assert len(tasks) == 1 and tasks[0]['due']['date'] == '2026-11-24'


def test_assignment_rename_in_canvas_updates_same_task(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    deps.canvas.session.routes['courses/101/assignments'][0].update(name='Pendulum lab report', updated_at='2026-11-08T00:00:00Z')
    scan(cfg, deps)
    assert len(deps.todoist.tasks()) == 1 and 'Pendulum' in deps.todoist.tasks()[0]['content']


def test_reschedule_announcement_moves_test_not_duplicates(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    ann = {'id': 5, 'title': 'Test moved', 'message': '<p>Unit 3 Test moved from November 12 to November 19.</p>', 'html_url': f'{BASE}/a/5',
           'posted_at': '2026-11-09T12:00:00Z'}
    deps.canvas.session.routes['announcements'] = [ann]
    deps.extractor.by_title['Test moved'] = [item('test', 'Unit 3 Test', '2026-11-19', 'Unit 3 Test moved from November 12 to November 19.',
                                                  status='rescheduled', previous_date='2026-11-12')]
    scan(cfg, deps)
    days = sorted(e['start']['date'] for e in deps.gcal.events.values())
    assert days.count('2026-11-19') == 1 and '2026-11-12' not in days and len(deps.gcal.events) == 4
    assert report(cfg)['open_reviews'] == [] or all(r['type'] != 'conflict' for r in report(cfg)['open_reviews'])


def test_conflicting_sources_hold_item_alert_user_and_do_not_write(tmp_path):
    ai = ScriptedAI({**DEFAULT_AI, 'Unit 3 plan': [item('test', 'Unit 3 Test', '2026-11-13', 'Unit 3 Test is on November 13.')]})
    cfg, deps = setup(tmp_path, ai=ai, cfg_kw={'dry_run': True})
    # the page text must actually contain the (wrong) date for evidence validation
    deps.canvas.session.routes['courses/101/pages/unit-3-plan']['body'] = '<p>Unit 3 Test is on November 13.</p>'
    scan(cfg, deps)
    rep = report(cfg)
    assert any(r['type'] == 'conflict' for r in rep['open_reviews'])
    assert not any(a['title'] == 'Unit 3 Test' for a in rep['actions'])
    assert any('need your review' in a['title'] for a in deps.alerter.sent)
    # user resolves it
    rid = next(r['id'] for r in rep['open_reviews'] if r['type'] == 'conflict')
    review_cli(cfg, argparse.Namespace(action='approve', id=rid, date='2026-11-12'))
    scan(cfg, deps)
    assert any(a['title'] == 'Unit 3 Test' and a['date'] == '2026-11-12' for a in report(cfg)['actions'])


def test_ambiguous_date_goes_to_review_and_is_never_invented(tmp_path):
    ai = ScriptedAI({**DEFAULT_AI, 'Test moved': [item('test', 'Unit 4 Test', None, 'Unit 4 Test next Friday', ambiguous=True, date_text='next Friday')]})
    ann = [{'id': 6, 'title': 'Test moved', 'message': '<p>Unit 4 Test next Friday</p>', 'html_url': 'u', 'posted_at': '2026-11-05T00:00:00Z'}]
    cfg, deps = setup(tmp_path, ai=ai, announcements=ann)
    scan(cfg, deps)
    rep = report(cfg)
    assert any(r['payload'].get('title') == 'Unit 4 Test' for r in rep['open_reviews'])
    assert not any(a['title'] == 'Unit 4 Test' for a in rep['actions'])


def test_ai_not_approved_blocks_uploads_but_structured_canvas_dates_still_work(tmp_path):
    cfg, deps = setup(tmp_path, cfg_kw={'ai_base_url': 'https://api.example.com/v1', 'ai_model': 'm', 'ai_data_approved': False})
    assert not cfg.ai_allowed
    deps.extractor = None
    scan(cfg, deps)
    rep = report(cfg)
    assert rep['ai_pending'] >= 3 and [a['title'] for a in rep['actions']] == ['Lab report 2']
    assert any('extraction paused' in a['title'] for a in deps.alerter.sent)


def test_provider_gating_rules():
    base = dict(ai_base_url='https://api.example.com/v1', ai_model='m')
    assert not make_cfg(Path('/x'), **base).ai_allowed
    assert make_cfg(Path('/x'), ai_data_approved=True, **base).ai_allowed
    assert make_cfg(Path('/x'), ai_base_url='http://localhost:11434/v1', ai_model='m').ai_allowed


def test_unreadable_and_scanned_documents_are_reported_not_silently_skipped(tmp_path):
    cfg, deps = setup(tmp_path)
    deps.canvas.files[f'{BASE}/files/9001/download?v=1'] = blank_pdf()
    scan(cfg, deps)
    assert any(u['issue'] == 'needs_ocr' for u in report(cfg)['unreadable'])
    assert any(r['type'] == 'unreadable_document' for r in report(cfg)['open_reviews'])


def test_user_edited_todoist_task_is_preserved_and_flagged(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    tid = deps.todoist.tasks()[0]['id']; deps.todoist._tasks[tid]['content'] = 'my custom title'
    deps.canvas.session.routes['courses/101/assignments'][0].update(due_at='2026-11-26T04:59:59Z')
    scan(cfg, deps)
    assert deps.todoist.get(tid)['content'] == 'my custom title' and deps.todoist.get(tid)['due']['date'] == '2026-11-20'
    assert any(r['type'] == 'sync_review' for r in report(cfg)['open_reviews'])


def test_canvas_outage_is_reported_and_removes_nothing(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    deps.canvas.session.routes['courses'] = 401
    code = scan(cfg, deps)
    rep = report(cfg)
    assert code == 1 and any(e['stage'] == 'canvas-auth' for e in rep['errors'])
    assert len(deps.todoist.tasks()) == 1 and len(deps.gcal.events) == 4
    assert not any(r['type'] == 'source_missing' for r in rep['open_reviews'])     # incomplete scan can't claim sources vanished
    assert any('token rejected' in a['title'] for a in deps.alerter.sent)


def test_alerts_deduplicated_across_hourly_runs(tmp_path):
    cfg, deps = setup(tmp_path, ai=None)
    deps.extractor = None
    scan(cfg, deps); n = len(deps.alerter.sent); scan(cfg, deps)
    assert len(deps.alerter.sent) == n


def test_vanished_source_flagged_for_review_only(tmp_path):
    cfg, deps = live(tmp_path)
    scan(cfg, deps)
    deps.canvas.session.routes['courses/101/pages/unit-3-plan'] = {'page_id': 55, 'url': 'x', 'title': 'x', 'body': '', 'html_url': 'u'}
    deps.canvas.session.routes['courses/101/modules'][0]['items'].pop(0)
    scan(cfg, deps)
    assert any(r['type'] == 'source_missing' for r in report(cfg)['open_reviews'])
    assert len(deps.gcal.events) == 4


def test_dst_aware_clock_and_timer_syntax():
    import subprocess, shutil
    t = Path(__file__).parent.parent / 'deploy' / 'school-agent.timer'
    txt = t.read_text()
    assert txt.count('OnCalendar=') == 3 and all('America/New_York' in l for l in txt.splitlines() if l.startswith('OnCalendar'))
    if shutil.which('systemd-analyze'):
        for l in txt.splitlines():
            if l.startswith('OnCalendar='):
                assert subprocess.run(['systemd-analyze', 'calendar', l.split('=', 1)[1]], capture_output=True).returncode == 0
