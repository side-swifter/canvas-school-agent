"""Google Calendar client + safe upsert into ONE dedicated school calendar.
 * events are found by private extended property (survives lost DB), then adopted collisions on read-only calendars
   (Canvas subscription / Todoist overlay) are linked rather than duplicated;
 * events we did not create, or that the user edited, are never overwritten; nothing is deleted."""
from datetime import date, timedelta
from urllib.parse import quote
from .net import request_with_retry
from .normalize import similar_titles

API = 'https://www.googleapis.com/calendar/v3/calendars/'


class CalendarClient:
    def __init__(self, auth, calendar_id, session=None, sleep=None):
        import requests
        self.auth, self.cal, self.session, self.sleep = auth, calendar_id, session or requests, sleep

    def _r(self, method, calendar_id, suffix='', **kw):
        if self.sleep:
            kw['sleep'] = self.sleep
        resp = request_with_retry(self.session, method, API + quote(calendar_id, safe='') + '/events' + suffix,
                                  headers={'Authorization': 'Bearer ' + self.auth.access_token()}, **kw)
        if resp.status_code in (404, 410):
            return None
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def find_by_oid(self, oid):
        data = self._r('GET', self.cal, params={'privateExtendedProperty': f'agent_oid={oid}', 'showDeleted': 'false', 'maxResults': 5})
        items = (data or {}).get('items', [])
        return items[0] if items else None

    def list_day(self, calendar_id, day):
        d = date.fromisoformat(day)
        data = self._r('GET', calendar_id, params={'timeMin': f'{day}T00:00:00Z', 'timeMax': f'{(d + timedelta(days=2)).isoformat()}T00:00:00Z',
                                                   'singleEvents': 'true', 'maxResults': 250})
        return (data or {}).get('items', [])

    def get(self, event_id):
        return self._r('GET', self.cal, '/' + quote(event_id, safe=''))

    def insert(self, body):
        return self._r('POST', self.cal, json=body)

    def patch(self, event_id, body):
        return self._r('PATCH', self.cal, '/' + quote(event_id, safe=''), json=body)


def render(ob):
    d = date.fromisoformat(ob['date'])
    cancelled = ob['status'] == 'cancelled'
    summary = ('[CANCELLED] ' if cancelled else '') + f"{ob['course']}: {ob['title']}"
    return {'summary': summary,
            'description': 'Source:\n' + '\n'.join(ob.get('urls') or []) + f"\nEvidence: {ob['evidence'][:400]}\n(managed by school-agent)",
            'start': {'date': d.isoformat()}, 'end': {'date': (d + timedelta(days=1)).isoformat()},
            'extendedProperties': {'private': {'agent_oid': ob['oid']}},
            'transparency': 'transparent'}


def event_fp(ev):
    return f"{ev.get('summary')}|{(ev.get('start') or {}).get('date') or (ev.get('start') or {}).get('dateTime', '')[:10]}"


def _start_day(ev):
    s = ev.get('start') or {}
    return s.get('date') or (s.get('dateTime') or '')[:10]


def upsert(client, ob, cfg, dry_run):
    """-> (action, detail, event_id, remote_fp)."""
    want = render(ob)
    want_fp = event_fp(want)
    if ob.get('calendar_id'):
        ev = client.get(ob['calendar_id'])
        if ev is None or ev.get('status') == 'cancelled':
            return 'review', 'calendar event deleted by user; not recreated', ob['calendar_id'], ob.get('remote_fp')
        if ob.get('adopted'):
            if _start_day(ev) != ob['date'] and ob['status'] != 'cancelled':
                return 'review', f"linked existing event is on {_start_day(ev)}, source says {ob['date']}", ob['calendar_id'], ob.get('remote_fp')
            return 'noop', '', ob['calendar_id'], ob.get('remote_fp')
        if event_fp(ev) != ob.get('remote_fp'):
            return 'review', 'event was edited by user since last sync; not overwritten', ob['calendar_id'], ob.get('remote_fp')
        if event_fp(ev) == want_fp:
            return 'noop', '', ob['calendar_id'], want_fp
        if not dry_run:
            client.patch(ob['calendar_id'], {k: want[k] for k in ('summary', 'description', 'start', 'end')})
        return 'update', want['summary'], ob['calendar_id'], want_fp
    ours = client.find_by_oid(ob['oid'])
    if ours:
        return 'adopt', 'relinked own event after state loss', ours['id'], event_fp(ours)
    # collision check on read-only calendars (Canvas feed, Todoist overlay) and on the school calendar itself
    for cal in (cfg.google_calendar_id, *cfg.google_readonly_calendar_ids):
        for ev in client.list_day(cal, ob['date']):
            if ev.get('status') == 'cancelled' or _start_day(ev) != ob['date']:
                continue
            name = ev.get('summary') or ''
            if similar_titles(name, ob['title']) or similar_titles(name, f"{ob['course']} {ob['title']}"):
                if cal == cfg.google_calendar_id:
                    return 'adopt', f"matches existing event '{name}'", ev['id'], None
                return 'collision', f"already displayed on a read-only calendar as '{name}'", None, None
    if ob['status'] == 'cancelled':
        return 'noop', 'cancelled item never synced', None, None
    if not cfg.google_calendar_id:
        return 'review', 'GOOGLE_CALENDAR_ID not set', None, None
    if dry_run:
        return 'create', want['summary'], None, None
    ev = client.insert(want)
    return 'create', want['summary'], ev['id'], want_fp
