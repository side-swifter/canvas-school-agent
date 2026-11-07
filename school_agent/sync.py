"""Controlled idempotent sync to Todoist and a dedicated Google calendar."""
from datetime import date, timedelta
from urllib.parse import quote
import requests

class Sync:
    def __init__(self, cfg):
        self.cfg = cfg

    def todoist(self, item, existing_id):
        if not self.cfg.todoist_api_token:
            return existing_id
        endpoint = 'https://api.todoist.com/api/v1/tasks'
        body = {'content': f"{item['course']}: {item['title']}",
                'description': f"Source: {item['source_url']}\nEvidence: {item['evidence']}"}
        if item.get('date'):
            body['due_date'] = item['date']
        if self.cfg.todoist_project_id:
            body['project_id'] = self.cfg.todoist_project_id
        headers = {'Authorization': f'Bearer {self.cfg.todoist_api_token}'}
        if existing_id:
            body.pop('project_id', None)
        url = endpoint + ('/' + quote(str(existing_id), safe='') if existing_id else '')
        resp = requests.request('POST', url, headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        if not existing_id:
            return str(resp.json()['id'])
        return existing_id

    def calendar(self, item, existing_id):
        if not (self.cfg.google_access_token and self.cfg.google_calendar_id):
            return existing_id
        d = date.fromisoformat(item['date'])
        payload = {'summary': f"{item['course']}: {item['title']}",
                   'description': f"Source: {item['source_url']}\nEvidence: {item['evidence']}",
                   'start': {'date': d.isoformat()},
                   'end': {'date': (d + timedelta(days=1)).isoformat()},
                   'extendedProperties': {'private': {'agent_source': item['source_key'],
                                                      'agent_obligation': item['key']}}}
        root = 'https://www.googleapis.com/calendar/v3/calendars/' + quote(self.cfg.google_calendar_id, safe='') + '/events'
        url = root + ('/' + quote(existing_id, safe='') if existing_id else '')
        response = requests.request('PATCH' if existing_id else 'POST', url, json=payload,
                                    headers={'Authorization': 'Bearer ' + self.cfg.google_access_token}, timeout=30)
        response.raise_for_status()
        return existing_id or response.json()['id']
