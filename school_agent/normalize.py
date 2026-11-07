"""Validation of extracted items against their source, plus title/kind matching helpers."""
import hashlib
import re
from datetime import date, timedelta

CAL_KINDS = {'test', 'quiz', 'event'}
TODO_KINDS = {'assignment', 'deadline'}
VALID_KINDS = CAL_KINDS | TODO_KINDS | {'study'}
KIND_ALIASES = {'schedule': 'event', 'exam': 'test', 'midterm': 'test', 'final': 'test', 'homework': 'assignment',
                'project': 'assignment', 'due': 'deadline', 'lab': 'assignment'}
VALID_STATUS = {'scheduled', 'cancelled', 'rescheduled'}
STOP = {'the', 'a', 'an', 'of', 'and', 'for', 'on', 'in', 'to', 'is', 'will', 'be', 'due', 'date', 'class', 'our'}
SYN = {'exam': 'test', 'exams': 'test', 'tests': 'test', 'midterm': 'test', 'quizzes': 'quiz', 'hw': 'assignment',
       'homework': 'assignment', 'assignments': 'assignment', 'quizz': 'quiz'}
MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']


def digest(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def tokens(title):
    out = []
    for w in re.findall(r'[a-z0-9]+', (title or '').lower()):
        w = SYN.get(w, w)
        if w not in STOP:
            out.append(w)
    return out


def norm_title(title):
    return ' '.join(tokens(title))


def numbers(title):
    return {t for t in tokens(title) if t.isdigit()}


def similar_titles(a, b):
    ta, tb = set(tokens(a)), set(tokens(b))
    if not ta or not tb or numbers(a) != numbers(b):
        return False
    inter = len(ta & tb)
    return inter / min(len(ta), len(tb)) >= 0.75 and inter / len(ta | tb) >= 0.5


def kinds_compatible(k1, k2):
    return k1 == k2 or 'event' in (k1, k2) and not ({k1, k2} & TODO_KINDS) or {k1, k2} == TODO_KINDS


def target_for(kind):
    return 'calendar' if kind in CAL_KINDS else 'todoist' if kind in TODO_KINDS else None


def squash(s):
    return re.sub(r'\s+', ' ', (s or '')).strip().lower()


def evidence_in_source(evidence, text):
    return squash(evidence) in squash(text)


def date_supported(d, evidence):
    """The evidence quote must itself mention the day and the month (name or numeric)."""
    e = (evidence or '').lower()
    day, month = d.day, d.month
    if re.search(rf'\b{MONTHS[month-1]}[a-z]*\.?\s*0?{day}(st|nd|rd|th)?\b', e):
        return True
    if re.search(rf'\b0?{day}(st|nd|rd|th)?\s+(of\s+)?{MONTHS[month-1]}', e):
        return True
    if re.search(rf'\b0?{month}[/\-.]0?{day}\b', e):
        return True
    if re.search(rf'\b{d.year}-0?{month}-0?{day}\b', e):
        return True
    return False


def normalize(items, material, cfg, today=None):
    """-> (accepted, review). Nothing is silently invented: anything uncertain goes to review with a reason."""
    today = today or date.today()
    accepted, review, seen = [], [], {}
    for raw in items or []:
        if not isinstance(raw, dict) or raw.get('actionable') is False:
            continue
        kind = str(raw.get('kind', '')).lower()
        kind = KIND_ALIASES.get(kind, kind)
        title = str(raw.get('title', '')).strip()[:180]
        evidence = str(raw.get('evidence', '')).strip()[:500]
        status = str(raw.get('status') or 'scheduled').lower()
        if kind not in VALID_KINDS or not title or status not in VALID_STATUS:
            continue
        try:
            confidence = float(raw.get('confidence', 0))
        except (ValueError, TypeError):
            continue
        base = {'course': material['course'], 'kind': kind, 'title': title, 'evidence': evidence, 'status': status,
                'confidence': confidence, 'source_key': material['key'], 'source_url': material['url'],
                'time': raw.get('time') or None, 'date_text': raw.get('date_text')}

        def to_review(reason, **extra):
            review.append({**base, 'date': raw.get('date'), 'reason': reason, **extra})

        if not evidence or not evidence_in_source(evidence, material.get('text', '')):
            to_review('evidence_not_found_in_source')
            continue
        when = None
        if raw.get('date'):
            try:
                when = date.fromisoformat(str(raw['date']))
            except ValueError:
                to_review('invalid_date')
                continue
        prev = None
        if raw.get('previous_date'):
            try:
                prev = date.fromisoformat(str(raw['previous_date'])).isoformat()
            except ValueError:
                prev = None
        if raw.get('ambiguous') or when is None:
            to_review('ambiguous_or_missing_date')
            continue
        if not 0 <= confidence <= 1 or confidence < cfg.min_confidence:
            to_review('low_confidence')
            continue
        if not date_supported(when, evidence):
            to_review('date_not_stated_in_evidence')
            continue
        if not (today - timedelta(days=7) <= when <= today + timedelta(days=cfg.lookahead_days)):
            continue
        if kind == 'study':
            to_review('study_item_needs_approval')
            continue
        if status == 'rescheduled' and not prev:
            prev = None  # allowed; reconciliation will treat as plain change
        ik = digest(f'{kind}|{norm_title(title)}')
        seen[ik] = seen.get(ik, 0) + 1
        if seen[ik] > 1:
            ik = digest(f'{ik}|{seen[ik]}')
        accepted.append({**base, 'item_key': ik, 'date': when.isoformat(), 'prev_date': prev})
    return accepted, review


def loosely_similar(a, b):
    """Rename tolerance for items on the SAME date: >=2 shared words and numbers not contradictory."""
    ta, tb = set(tokens(a)), set(tokens(b))
    na, nb = numbers(a), numbers(b)
    if na and nb and na != nb:
        return False
    words = {t for t in ta & tb if not t.isdigit()}
    return len(words) >= 2
