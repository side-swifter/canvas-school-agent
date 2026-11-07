import pytest
from school_agent.canvas import CanvasAuthError, local_due
from school_agent.documents import extract_document, NeedsOCR, extract_links, strip_html
from school_agent.reconcile import reconcile
from school_agent.normalize import digest
from tests.helpers import *


def ob(src, title, date, kind='test', status='scheduled', prev=None, structured=0, updated='2026-11-01', course='Physics', ik=None):
    return {'source_key': src, 'item_key': ik or digest(f'{kind}|{title}'), 'course': course, 'kind': kind, 'title': title, 'date': date,
            'time': None, 'status': status, 'prev_date': prev, 'confidence': .95, 'evidence': f'{title} {date}', 'url': f'http://{src}',
            'structured': structured, 'source_updated': updated, 'first_seen': updated}


import itertools
_ids = itertools.count()


def rec(obs, existing=(), approvals=None):
    return reconcile(obs, list(existing), approvals, now='n', new_oid=lambda: f'oid{next(_ids)}')


def test_same_test_in_three_sources_is_one_obligation():
    d, rv, _ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12'), ob('page', 'Unit 3 Exam', '2026-11-12'), ob('docx', 'Unit 3 Test', '2026-11-12')])
    assert len(d) == 1 and len(d[0]['members']) == 3 and not rv and not d[0]['held']


def test_numbered_quizzes_stay_separate():
    d, *_ = rec([ob('a', 'Quiz 1', '2026-11-09', 'quiz'), ob('a', 'Quiz 2', '2026-11-16', 'quiz'), ob('b', 'Quiz 1', '2026-11-09', 'quiz')])
    assert sorted(x['title'] for x in d) == ['Quiz 1', 'Quiz 2']


def test_weekly_repeated_titles_in_one_source_not_merged():
    d, *_ = rec([ob('sched', 'Quiz', '2026-11-09', 'quiz', ik='a'), ob('sched', 'Quiz', '2026-11-16', 'quiz', ik='b')])
    assert len(d) == 2


def test_conflicting_dates_hold_and_review():
    d, rv, _ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12'), ob('page', 'Unit 3 Test', '2026-11-13')])
    assert d[0]['held'] == 'conflict' and d[0]['date'] is None and rv[0]['type'] == 'conflict'


def test_conflict_keeps_previous_date_while_held():
    first, *_ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12')])
    d, rv, _ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12'), ob('page', 'Unit 3 Test', '2026-11-13')], first)
    assert d[0]['date'] == '2026-11-12' and d[0]['held'] and d[0]['oid'] == first[0]['oid']


def test_user_resolution_of_conflict():
    first, *_ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12'), ob('page', 'Unit 3 Test', '2026-11-13')])
    d, rv, _ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12'), ob('page', 'Unit 3 Test', '2026-11-13')], first, {first[0]['oid']: {'date': '2026-11-13'}})
    assert d[0]['date'] == '2026-11-13' and not d[0]['held'] and not rv


def test_reschedule_notice_supersedes_old_date():
    d, rv, _ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12'),
                    ob('ann', 'Unit 3 Test moved', '2026-11-19', status='rescheduled', prev='2026-11-12', updated='2026-11-05')])
    assert d[0]['date'] == '2026-11-19' and not d[0]['held'] and not rv


def test_cancellation():
    d, *_ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12', updated='2026-10-01'), ob('ann', 'Unit 3 Test', '2026-11-12', status='cancelled', updated='2026-11-05')])
    assert d[0]['status'] == 'cancelled' and not d[0]['held']


def test_cancel_vs_newer_schedule_is_conflict():
    d, rv, _ = rec([ob('ann', 'Unit 3 Test', '2026-11-12', status='cancelled', updated='2026-11-01'), ob('pdf', 'Unit 3 Test', '2026-11-20', updated='2026-11-05')])
    assert d[0]['held'] == 'conflict'


def test_canvas_structured_date_wins_over_text_but_noted():
    d, rv, _ = rec([ob('api', 'Lab report 2', '2026-11-20', 'assignment', structured=1), ob('pdf', 'Lab report 2', '2026-11-21', 'assignment')])
    assert d[0]['date'] == '2026-11-20' and 'Canvas' in d[0]['notes'] and not rv


def test_identity_survives_date_change_and_rename():
    first, *_ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12')])
    again, *_ = rec([ob('pdf', 'Unit 3 Test', '2026-11-14')], first)
    assert again[0]['oid'] == first[0]['oid'] and again[0]['date'] == '2026-11-14'
    renamed, *_ = rec([ob('pdf', 'Unit Three Dynamics Exam', '2026-11-14', ik='different')], again)   # same source, same date
    assert renamed[0]['oid'] == first[0]['oid']


def test_unrelated_existing_kept_as_orphan_not_deleted():
    first, *_ = rec([ob('pdf', 'Unit 3 Test', '2026-11-12')])
    d, rv, orphans = rec([ob('x', 'Essay', '2026-11-30', 'assignment')], first)
    assert [o['oid'] for o in orphans] == [first[0]['oid']]


def test_unnumbered_quiz_with_different_dates_is_held_not_guessed():
    d, rv, _ = rec([ob('a', 'Quiz', '2026-11-09', 'quiz', ik='1'), ob('b', 'Quiz', '2026-11-16', 'quiz', ik='2')])
    assert len(d) == 1 and d[0]['held'] == 'conflict' and rv[0]['type'] == 'conflict'


def test_ambiguous_match_flagged_when_multiple_candidates_and_no_date_hint():
    d, rv, _ = rec([ob('s', 'Quiz', '2026-11-09', 'quiz', ik='1'), ob('s', 'Quiz', '2026-11-16', 'quiz', ik='2'),
                    ob('z', 'Quiz', '2026-11-30', 'quiz', ik='3')])
    assert any(r['type'] == 'ambiguous_match' for r in rv) and len(d) == 3


# ---- documents
def test_syllabus_pdf_text():
    t = extract_document(make_pdf(SYLLABUS_LINES), 'syllabus.pdf')
    assert 'Unit 3 Test: Thursday, November 12' in t


def test_scanned_pdf_needs_ocr():
    with pytest.raises(NeedsOCR):
        extract_document(blank_pdf(), 'scan.pdf')


def test_docx_table_schedule():
    t = extract_document(make_docx(SCHEDULE_ROWS), 'Schedule.docx')
    assert 'Thu 11/12 | Unit 3 Test' in t


def test_html_helpers():
    assert 'Quiz Friday' in strip_html('<div><b>Quiz</b> Friday<script>x</script></div>')
    assert extract_links('<a href="/courses/1/files/2">f</a><iframe src="https://docs.google.com/document/d/A/preview">', BASE) == \
        [f'{BASE}/courses/1/files/2', 'https://docs.google.com/document/d/A/preview']


# ---- canvas
def test_canvas_ingests_everything_and_finds_linked_docs():
    c = FakeCanvas(physics_routes(announcements=[{'id': 5, 'title': 'Test moved', 'message': '<p>Unit 3 Test moved to Nov 19</p>', 'html_url': f'{BASE}/a/5', 'posted_at': '2026-11-05T12:00:00Z'}]), physics_files())
    mats = []
    for course in c.courses():
        mats.extend(c.materials(course))
    types = sorted(m['type'] for m in mats)
    assert {'syllabus', 'assignment', 'module', 'page', 'announcement', 'file', 'gdoc'} <= set(types)
    keys = [m['key'] for m in mats]
    assert len(keys) == len(set(keys))                                   # linked/module files not double counted
    a = next(m for m in mats if m['type'] == 'assignment')
    assert a['structured'][0]['date'] == '2026-11-20' and a['structured'][0]['kind'] == 'assignment'   # 04:59Z -> 23:59 EST prior day


def test_due_at_converted_to_local_time_zone():
    assert local_due('2026-11-21T04:59:59Z', 'America/New_York') == ('2026-11-20', None)
    assert local_due('2026-11-13T15:30:00Z', 'America/New_York') == ('2026-11-13', '10:30')


def test_api_403_queues_browser_fallback_without_crashing():
    routes = physics_routes(extra={'courses/101/modules': 403})
    c = FakeCanvas(routes, physics_files())
    mats = [m for course in c.courses() for m in c.materials(course)]
    assert any(f['what'] == 'modules' for f in c.fallbacks) and mats


def test_expired_token_is_distinct_error():
    c = FakeCanvas({'courses': 401})
    with pytest.raises(CanvasAuthError):
        c.courses()
