"""One scan per invocation. Scheduler and browser collector run separately."""
import argparse
import hashlib
import json
import logging
import re
import sys
from datetime import datetime, date, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from .ai import extract_items
from .canvas import CanvasClient
from .config import Config
from .documents import strip_html, extract_document
from .store import Store
from .sync import Sync

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('school-agent')

def digest(s):
    return hashlib.sha256(s.encode('utf-8')).hexdigest()

def browser_materials(filename):
    if not filename:
        return []
    p = Path(filename)
    if not p.exists():
        log.warning('Browser export not found: %s', p)
        return []
    parsed = json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(parsed, list):
        raise ValueError('Browser export must be a JSON array')
    out = []
    for entry in parsed:
        if not all(entry.get(k) for k in ('key', 'course', 'title', 'text', 'url')):
            continue
        out.append({'key': 'browser:' + str(entry['key']), 'type': 'browser',
                    'course': str(entry['course']), 'title': str(entry['title']),
                    'text': str(entry['text']), 'url': str(entry['url'])})
    return out

def normalize(items, material, cfg):
    now = date.today()
    valid_kinds = {'test', 'quiz', 'deadline', 'assignment', 'event', 'study'}
    output = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get('actionable'):
            continue
        kind = str(raw.get('kind', '')).lower()
        title = str(raw.get('title', '')).strip()[:180]
        evidence = str(raw.get('evidence', '')).strip()[:500]
        date_str = raw.get('date')
        if kind not in valid_kinds or not title or not evidence:
            continue
        try:
            confidence = float(raw.get('confidence', 0))
        except (ValueError, TypeError):
            continue
        if not 0 <= confidence <= 1 or confidence < 0.85:
            continue
        if date_str:
            try:
                when = date.fromisoformat(str(date_str))
            except ValueError:
                continue
            if not (now - timedelta(days=7) <= when <= now + timedelta(days=cfg.lookahead_days)):
                continue
        else:
            when = None
        key = digest(f"{material['key']}|{kind}|{re.sub(r'[^a-z0-9]+', '', title.lower())}")
        output.append({'key': key, 'source_key': material['key'], 'source_url': material['url'],
                       'course': material['course'], 'kind': kind, 'title': title,
                       'date': when.isoformat() if when else None,
                       'evidence': evidence, 'confidence': confidence})
    return output

def enrich(material, canvas, max_bytes):
    m = material.copy()
    if m.get('type') == 'file' and m.get('download_url') and canvas:
        filename = m.get('filename') or m['title']
        if filename.lower().endswith(('.pdf', '.docx', '.txt', '.csv', '.md', '.html', '.htm')):
            try:
                raw = canvas.download(m['download_url'], max_bytes)
                m['text'] = extract_document(raw, filename)
            except Exception as exc:
                log.warning('Unable to extract %s: %s', m['key'], exc)
                m['text'] = ''
    if m.get('type') in ('page', 'assignment', 'announcement'):
        m['text'] = strip_html(m.get('text', ''))
    return m

def scan(cfg, force=False):
    store = Store(cfg.db_path)
    sync = Sync(cfg)
    now = datetime.now(ZoneInfo(cfg.timezone)).isoformat()
    materials = browser_materials(cfg.browser_export_json)
    canvas = None
    if cfg.canvas_base_url and cfg.canvas_token:
        canvas = CanvasClient(cfg.canvas_base_url, cfg.canvas_token)
        for course in canvas.courses():
            materials.extend(canvas.materials(course))
    elif not materials:
        log.warning('No Canvas API credentials or browser export. Nothing to scan.')
    results = {'sources': 0, 'changed': 0, 'detected': 0, 'review': [], 'actions': [], 'errors': []}
    for original in materials:
        try:
            m = enrich(original, canvas, cfg.max_document_mb * 1024 * 1024)
            text = m.get('text', '')
            if not text or len(text.strip()) < 15:
                continue
            fingerprint = digest(json.dumps({'text': text, 'title': m['title']}, sort_keys=True))
            results['sources'] += 1
            if not force and store.unchanged(m['key'], fingerprint):
                continue
            results['changed'] += 1
            raw = extract_items(cfg, m)
            if not (cfg.ai_base_url and cfg.ai_model and cfg.ai_api_key):
                log.warning('AI not configured; source %s indexed without extraction', m['key'])
                continue
            for item in normalize(raw, m, cfg):
                results['detected'] += 1
                existing = store.obligation(item['key'])
                item_fingerprint = digest(json.dumps(item, sort_keys=True))
                if existing and existing['fingerprint'] == item_fingerprint:
                    continue
                if not item['date'] or item['kind'] == 'study':
                    results['review'].append(item)
                    continue
                target = 'calendar' if item['kind'] in ('test', 'quiz', 'event') else 'todoist'
                if cfg.dry_run:
                    results['actions'].append({'mode': 'dry-run', 'target': target, 'item': item})
                    continue
                todo_id = existing['todoist_id'] if existing else None
                cal_id = existing['calendar_id'] if existing else None
                if target == 'calendar':
                    if not (cfg.google_access_token and cfg.google_calendar_id):
                        results['review'].append(item)
                        continue
                    cal_id = sync.calendar(item, cal_id)
                else:
                    if not cfg.todoist_api_token:
                        results['review'].append(item)
                        continue
                    todo_id = sync.todoist(item, todo_id)
                store.save_obligation(item['key'], m['key'], item, item_fingerprint, todo_id, cal_id, now)
                results['actions'].append({'mode': 'applied', 'target': target, 'item': item})
            if not cfg.dry_run:
                store.save_source(m['key'], fingerprint, text[:100000], now)
        except Exception as exc:
            log.exception('Source processing failed: %s', original.get('key'))
            results['errors'].append({'source': original.get('key'), 'error': str(exc)})
    Path('state').mkdir(exist_ok=True)
    Path('state/last-report.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    log.info('Scan complete: %s', {k: len(v) if isinstance(v, list) else v for k,v in results.items()})
    return 1 if results['errors'] else 0

def main():
    parser = argparse.ArgumentParser(description='Course document scan and deadline sync')
    parser.add_argument('command', nargs='?', choices=['scan'], default='scan')
    parser.add_argument('--force', action='store_true', help='Reprocess previously scanned materials')
    args = parser.parse_args()
    cfg = Config.from_env()
    return scan(cfg, force=args.force)

if __name__ == '__main__':
    sys.exit(main())
