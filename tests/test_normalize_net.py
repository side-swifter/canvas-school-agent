from datetime import date
import pytest
import requests
from school_agent.net import request_with_retry, RetryExhausted
from school_agent.normalize import normalize, similar_titles, date_supported
from tests.helpers import Resp, item
from types import SimpleNamespace

CFG = SimpleNamespace(lookahead_days=90, min_confidence=.85)
TODAY = date(2026, 11, 7)
M = {'key': 's1', 'course': 'Physics', 'url': 'u', 'text': 'Unit 3 Test: Thursday, November 12 in class. Test sometime next week.'}


def run(raw):
    return normalize([raw], M, CFG, TODAY)


def test_accepts_grounded_item():
    ok, rev = run(item('exam', 'Unit 3 Test', '2026-11-12', 'Unit 3 Test: Thursday, November 12 in class.'))
    assert len(ok) == 1 and ok[0]['kind'] == 'test' and not rev


def test_evidence_must_exist_in_source():
    ok, rev = run(item('test', 'Unit 3 Test', '2026-11-12', 'Unit 3 Test: Friday, November 13'))
    assert not ok and rev[0]['reason'] == 'evidence_not_found_in_source'


def test_date_must_be_stated_in_evidence():
    ok, rev = run(item('test', 'Unit 3 Test', '2026-11-19', 'Unit 3 Test: Thursday, November 12 in class.'))
    assert not ok and rev[0]['reason'] == 'date_not_stated_in_evidence'


def test_relative_date_goes_to_review_not_invented():
    ok, rev = run(item('test', 'Test', None, 'Test sometime next week.', date_text='next week'))
    assert not ok and rev[0]['reason'] == 'ambiguous_or_missing_date'


def test_low_confidence_reviewed_not_dropped():
    ok, rev = run({**item('test', 'Unit 3 Test', '2026-11-12', 'Unit 3 Test: Thursday, November 12 in class.'), 'confidence': .5})
    assert not ok and rev[0]['reason'] == 'low_confidence'


def test_model_flagged_ambiguous():
    ok, rev = run({**item('test', 'Unit 3 Test', '2026-11-12', 'Unit 3 Test: Thursday, November 12 in class.'), 'ambiguous': True})
    assert not ok and rev


def test_out_of_window_ignored():
    m = {**M, 'text': 'Unit 3 Test: Thursday, March 12 2030.'}
    assert normalize([item('test', 'T', '2030-03-12', 'Unit 3 Test: Thursday, March 12 2030.')], m, CFG, TODAY) == ([], [])


def test_date_support_formats():
    assert date_supported(date(2026, 11, 9), 'Mon 11/9 | Quiz')
    assert date_supported(date(2026, 11, 9), 'due 9th of November')
    assert date_supported(date(2026, 11, 9), 'Nov. 9')
    assert not date_supported(date(2026, 11, 9), 'Monday')


def test_title_similarity_guards_numbers():
    assert similar_titles('Unit 3 Exam', 'Unit 3 Test')
    assert not similar_titles('Quiz 1', 'Quiz 2')
    assert not similar_titles('Unit 3 Test', 'Unit 4 Test')


class Flaky:
    def __init__(self, seq): self.seq, self.n = seq, 0
    def request(self, m, u, **kw):
        r = self.seq[min(self.n, len(self.seq) - 1)]; self.n += 1
        if isinstance(r, Exception): raise r
        return r


def test_retry_on_503_then_success():
    sleeps = []
    s = Flaky([Resp({}, 503), Resp({}, 503), Resp({'ok': 1})])
    assert request_with_retry(s, 'GET', 'http://x', sleep=sleeps.append).json() == {'ok': 1}
    assert s.n == 3 and len(sleeps) == 2


def test_retry_after_header_and_connection_errors():
    r429 = Resp({}, 429); r429.headers = {'Retry-After': '3'}
    sleeps = []
    s = Flaky([r429, requests.ConnectionError('boom'), Resp({'ok': 1})])
    request_with_retry(s, 'GET', 'http://x', sleep=sleeps.append)
    assert sleeps[0] == 3


def test_no_retry_on_4xx_and_exhaustion():
    s = Flaky([Resp({}, 403)])
    assert request_with_retry(s, 'GET', 'http://x', sleep=lambda x: None).status_code == 403 and s.n == 1
    with pytest.raises(RetryExhausted):
        request_with_retry(Flaky([Resp({}, 500)]), 'GET', 'http://x', attempts=3, sleep=lambda x: None)
