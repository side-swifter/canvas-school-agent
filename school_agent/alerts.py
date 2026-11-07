"""Alerts: always logged to state/alerts.jsonl; optionally POSTed to a webhook (ntfy / generic). Deduplicated."""
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
import requests
from .net import request_with_retry

log = logging.getLogger('school-agent.alerts')
SUPPRESS_HOURS = 20


class Alerter:
    def __init__(self, cfg, store, now_fn):
        self.cfg, self.store, self.now_fn = cfg, store, now_fn
        self.sent = []

    def alert(self, key, title, body, severity='warning'):
        """Returns True if emitted (not suppressed as a recent repeat)."""
        now = self.now_fn()
        last = self.store.alert_last(key)
        if last and now - datetime.fromisoformat(last) < timedelta(hours=SUPPRESS_HOURS):
            return False
        self.store.alert_mark(key, now.isoformat())
        record = {'at': now.isoformat(), 'key': key, 'severity': severity, 'title': title, 'body': body}
        self.sent.append(record)
        log.warning('ALERT [%s] %s: %s', severity, title, body)
        path = Path(self.cfg.state_dir)
        path.mkdir(parents=True, exist_ok=True)
        with (path / 'alerts.jsonl').open('a', encoding='utf-8') as fh:
            fh.write(json.dumps(record) + '\n')
        if self.cfg.alert_webhook_url:
            try:
                request_with_retry(requests, 'POST', self.cfg.alert_webhook_url, attempts=2,
                                   data=body.encode('utf-8'), headers={'Title': title, 'Priority': 'high' if severity == 'error' else 'default'})
            except Exception as exc:  # alert delivery must never break a scan
                log.error('Webhook delivery failed: %s', exc)
        return True
