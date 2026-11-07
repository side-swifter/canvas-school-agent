"""Todoist client + safe upsert. Rules:
 * a task is only modified if WE created it (marker `agent-oid:<oid>` in its description) and nobody edited it since;
 * before creating, existing tasks (all projects by default) are searched for the same item -> adopted, never duplicated;
 * a task the user deleted is never recreated (review item instead); nothing is ever deleted."""
import re
from datetime import date
from urllib.parse import quote
import requests
from .net import request_with_retry
from .normalize import similar_titles

API = 'https://api.todoist.com/api/v1'
MARK = 'agent-oid:'


def marker(oid):
    return f'{MARK}{oid}'


class TodoistClient:
    def __init__(self, token, session=None, sleep=None):
        self.session = session or requests
        self.h = {'Authorization': f'Bearer {token}'}
        self.sleep = sleep

    def _r(self, method, path, **kw):
        if self.sleep:
            kw['sleep'] = self.sleep
        resp = request_with_retry(self.session, method, API + path, headers=self.h, **kw)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json() if resp.content else {}

    def tasks(self, project_id=None):
        out, cursor = [], None
        while True:
            params = {'limit': 200}
            if project_id:
                params['project_id'] = project_id
            if cursor:
                params['cursor'] = cursor
            data = self._r('GET', '/tasks', params=params)
            out.extend(data.get('results', []) if isinstance(data, dict) else data)
            cursor = data.get('next_cursor') if isinstance(data, dict) else None
            if not cursor:
                return out

    def get(self, task_id):
        return self._r('GET', f'/tasks/{quote(str(task_id), safe="")}')

    def create(self, body):
        return self._r('POST', '/tasks', json=body)

    def update(self, task_id, body):
        return self._r('POST', f'/tasks/{quote(str(task_id), safe="")}', json=body)


def render(ob):
    cancelled = ob['status'] == 'cancelled'
    content = ('[CANCELLED] ' if cancelled else '') + f"{ob['course']}: {ob['title']}"
    sources = '\n'.join(ob.get('urls') or [])
    desc = f"{marker(ob['oid'])}\nEvidence: {ob['evidence'][:400]}\nSources:\n{sources}"
    return {'content': content, 'description': desc, 'due_date': ob['date']}


def due_of(task):
    d = task.get('due') or {}
    return (d.get('date') or '')[:10]


def find_existing(client, ob, cfg, cache):
    """Task already representing this obligation (ours by marker, or a user/Canvas-synced equivalent)."""
    if 'tasks' not in cache:
        cache['tasks'] = client.tasks(None if cfg.todoist_dedupe_all_projects else cfg.todoist_project_id)
    for t in cache['tasks']:
        if marker(ob['oid']) in (t.get('description') or ''):
            return t, 'ours'
    for t in cache['tasks']:
        if t.get('checked') or t.get('is_completed'):
            continue
        content = re.sub(r'^\[CANCELLED\]\s*', '', t.get('content') or '')
        if due_of(t) == ob['date'] and (similar_titles(content, ob['title']) or similar_titles(content, f"{ob['course']} {ob['title']}")):
            return t, 'adopt'
    return None, None


def upsert(client, ob, cfg, cache, dry_run):
    """-> (action, detail, todoist_id, remote_fp). Actions: create|update|adopt|noop|review.  Never raises on user edits."""
    want = render(ob)
    fp = lambda t: f"{t.get('content')}|{due_of(t)}"
    if ob.get('todoist_id'):
        task = client.get(ob['todoist_id'])
        if task is None or task.get('checked') or task.get('is_completed'):
            return 'review', 'task deleted or completed by user; not recreated', ob['todoist_id'], ob.get('remote_fp')
        if ob.get('adopted'):
            return ('review', f"adopted user task differs from source date {ob['date']}", ob['todoist_id'], ob.get('remote_fp')) \
                if due_of(task) != ob['date'] and ob['status'] != 'cancelled' else ('noop', '', ob['todoist_id'], ob.get('remote_fp'))
        if fp(task) != ob.get('remote_fp'):
            return 'review', 'task was edited by user since last sync; not overwritten', ob['todoist_id'], ob.get('remote_fp')
        if fp(task) == f"{want['content']}|{want['due_date']}":
            return 'noop', '', ob['todoist_id'], fp(task)
        if not dry_run:
            task = client.update(ob['todoist_id'], {'content': want['content'], 'description': want['description'], 'due_date': want['due_date']})
        return 'update', want['content'], ob['todoist_id'], f"{want['content']}|{want['due_date']}"
    existing, how = find_existing(client, ob, cfg, cache)
    if existing and how == 'ours':
        return 'adopt', 'relinked own task after state loss', str(existing['id']), fp(existing)
    if existing:
        return 'adopt', f"matches existing task '{existing.get('content')}'", str(existing['id']), None
    if ob['status'] == 'cancelled':
        return 'noop', 'cancelled item never synced', None, None
    if not cfg.todoist_project_id:
        return 'review', 'TODOIST_PROJECT_ID not set; refusing to write to default project', None, None
    if dry_run:
        return 'create', want['content'], None, None
    task = client.create({**want, 'project_id': cfg.todoist_project_id})
    cache.setdefault('tasks', []).append(task)
    return 'create', want['content'], str(task['id']), f"{want['content']}|{want['due_date']}"
