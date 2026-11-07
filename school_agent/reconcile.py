"""Cross-source reconciliation. Pure functions: observations + existing obligations -> desired obligations + reviews.

Identity: every obligation has a persistent `oid`. Members are (source_key::item_key) pairs, so an obligation
survives renames, date edits, and corroboration from additional sources. Nothing here deletes anything."""
import json
import uuid
from .normalize import digest, kinds_compatible, similar_titles, loosely_similar


def stamp(o):
    return o.get('source_updated') or o.get('first_seen') or ''


def member_id(o):
    return f"{o['source_key']}::{o['item_key']}"


def _cluster(observations):
    clusters = []
    ordered = sorted(observations, key=lambda o: (-o.get('structured', 0), o['source_key'], o['item_key']))
    ambiguous = []
    for o in ordered:
        cands = [c for c in clusters
                 if c['course'] == o['course'] and kinds_compatible(c['kind'], o['kind'])
                 and similar_titles(c['title'], o['title'])
                 and all(m['source_key'] != o['source_key'] for m in c['obs'])]
        dates = {o['date'], o.get('prev_date')} - {None}
        by_date = [c for c in cands if dates & {m['date'] for m in c['obs']}]
        if by_date:
            pick = by_date[0]
        elif len(cands) == 1:
            pick = cands[0]
        else:
            pick = None
            if len(cands) > 1:
                ambiguous.append(o)
        if pick:
            pick['obs'].append(o)
            if pick['kind'] == 'event' and o['kind'] != 'event':
                pick['kind'] = o['kind']
        else:
            clusters.append({'course': o['course'], 'kind': o['kind'], 'title': o['title'], 'obs': [o]})
    return _merge_renames(clusters), ambiguous


def _merge_renames(clusters):
    """Second pass: same course, same date, loosely similar titles, no shared source -> same item renamed somewhere."""
    out = []
    for c in clusters:
        dates = {m['date'] for m in c['obs'] if m['status'] != 'cancelled'}
        for t in out:
            tdates = {m['date'] for m in t['obs'] if m['status'] != 'cancelled'}
            if (t['course'] == c['course'] and kinds_compatible(t['kind'], c['kind']) and dates and dates == tdates
                    and loosely_similar(t['title'], c['title'])
                    and not ({m['source_key'] for m in t['obs']} & {m['source_key'] for m in c['obs']})):
                t['obs'].extend(c['obs'])
                break
        else:
            out.append(c)
    return out


def _resolve(c, approval):
    """-> dict(date,time,status,held,reason,notes,conflict_dates)"""
    obs = c['obs']
    superseded = set()
    for o in obs:
        if o['status'] == 'rescheduled' and o.get('prev_date'):
            for p in obs:
                if p is not o and p['status'] == 'scheduled' and p['date'] == o['prev_date']:
                    superseded.add(member_id(p))
    live = [o for o in obs if member_id(o) not in superseded]
    cancelled = [o for o in live if o['status'] == 'cancelled']
    scheduled = [o for o in live if o['status'] != 'cancelled']
    notes = []
    if superseded:
        notes.append(f'{len(superseded)} older source value(s) superseded by a reschedule notice')
    if cancelled:
        newest_cancel = max(stamp(o) for o in cancelled)
        newer_live = [o for o in scheduled if stamp(o) > newest_cancel and o['date'] != cancelled[0]['date']]
        if newer_live:
            return {'held': 'conflict', 'reason': 'cancelled in one source but scheduled in a newer one',
                    'dates': sorted({o['date'] for o in live}), 'notes': notes}
        top = max(cancelled, key=stamp)
        return {'date': top['date'], 'time': top.get('time'), 'status': 'cancelled', 'notes': notes}
    structured = [o for o in scheduled if o.get('structured')]
    pool = structured or scheduled
    dates = sorted({o['date'] for o in pool})
    if len(dates) == 1:
        if structured:
            text_dates = sorted({o['date'] for o in scheduled if not o.get('structured')} - {dates[0]})
            if text_dates:
                notes.append(f'Canvas data says {dates[0]}; other sources mention {", ".join(text_dates)} (Canvas used)')
        pick = max(pool, key=lambda o: (o.get('structured', 0), stamp(o)))
        return {'date': dates[0], 'time': pick.get('time'), 'status': 'completed' if pick['status'] == 'completed' else 'scheduled', 'notes': notes}
    if approval and approval.get('date') in dates:
        pick = [o for o in pool if o['date'] == approval['date']][0]
        return {'date': approval['date'], 'time': pick.get('time'), 'status': 'scheduled', 'notes': notes + ['conflict resolved by user']}
    return {'held': 'conflict', 'reason': 'sources disagree on the date', 'dates': dates, 'notes': notes}


def reconcile(observations, existing, approvals=None, now='', new_oid=lambda: uuid.uuid4().hex[:16]):
    approvals = approvals or {}
    clusters, ambiguous = _cluster(observations)
    existing = list(existing)
    claimed, desired, reviews = set(), [], []


    for c in clusters:
        mids = {member_id(o) for o in c['obs']}
        best, overlap = None, 0
        for e in existing:
            if e['oid'] in claimed:
                continue
            n = len(mids & set(e['members']))
            if n > overlap:
                best, overlap = e, n
        if not best:
            srcs = {o['source_key'] for o in c['obs']}
            for e in existing:
                if e['oid'] in claimed or e['course'] != c['course'] or not kinds_compatible(e['kind'], c['kind']):
                    continue
                e_srcs = {m.split('::')[0] for m in e['members']}
                if similar_titles(e['title'], c['title']) or (srcs & e_srcs and any(o['date'] == e['date'] for o in c['obs'])):
                    best = e
                    break
        oid = best['oid'] if best else new_oid()
        claimed.add(oid)
        res = _resolve(c, approvals.get(oid))
        title_obs = sorted(c['obs'], key=lambda o: (-o.get('structured', 0), _neg(stamp(o)), len(o['title']), o['title']))[0]
        base = dict(best) if best else {'oid': oid, 'todoist_id': None, 'calendar_id': None, 'adopted': 0, 'remote_fp': None,
                                        'synced_fp': None, 'created_at': now}
        held = res.get('held')
        date = res.get('date') or (best['date'] if best else None)
        ob = {**base, 'oid': oid, 'course': c['course'], 'kind': c['kind'], 'title': title_obs['title'],
              'date': date, 'time': res.get('time') if not held else (best or {}).get('time'),
              'status': res.get('status', (best or {}).get('status', 'active')) if not held else (best or {}).get('status', 'scheduled'),
              'members': sorted(mids), 'evidence': ' || '.join(o['evidence'] for o in c['obs'][:3])[:900],
              'urls': sorted({o['url'] for o in c['obs'] if o.get('url')}),
              'notes': '; '.join(res.get('notes', [])), 'held': held, 'updated_at': now}
        if ob['status'] == 'active':
            ob['status'] = 'scheduled'
        ob['desired_fp'] = digest(json.dumps([ob['course'], ob['kind'], ob['title'], ob['date'], ob['time'], ob['status']]))
        desired.append(ob)
        if held:
            reviews.append({'rkey': f"conflict:{oid}:{digest(','.join(res.get('dates') or []))[:10]}", 'type': 'conflict', 'payload': {
                'oid': oid, 'course': c['course'], 'title': ob['title'], 'kind': c['kind'], 'reason': res['reason'],
                'candidates': [{'date': o['date'], 'status': o['status'], 'source': o['source_key'], 'url': o.get('url'),
                                'evidence': o['evidence']} for o in c['obs']]}})
    for o in ambiguous:
        reviews.append({'rkey': 'ambiguous_match:' + digest(member_id(o)), 'type': 'ambiguous_match', 'payload': {
            'course': o['course'], 'title': o['title'], 'date': o['date'], 'source': o['source_key'], 'url': o.get('url'),
            'evidence': o['evidence'], 'reason': 'could not tell which existing item this refers to'}})
    orphans = [e for e in existing if e['oid'] not in claimed]
    return desired, reviews, orphans


def _neg(s):
    return ''.join(chr(0x10FFFF - ord(ch)) for ch in s)
