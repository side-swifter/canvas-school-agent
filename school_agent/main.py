"""One scan per invocation: collect -> extract -> reconcile -> sync (or dry-run) -> report/alert."""
import argparse
import fcntl
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime, date
from pathlib import Path
from zoneinfo import ZoneInfo
import requests
from . import ai, gcal, todoist
from .alerts import Alerter
from .browser import SessionExpired, BrowserCollector, PlaywrightDriver
from .canvas import CanvasClient, CanvasAuthError
from .config import Config
from .documents import strip_html, extract_document, NeedsOCR
from .google_auth import GoogleAuth, GoogleAuthError
from .net import RetryExhausted
from .normalize import normalize, digest, target_for
from .reconcile import reconcile
from .store import Store

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('school-agent')
HTML_TYPES = {'page', 'assignment', 'announcement', 'syllabus', 'quiz', 'calendar'}


@dataclass
class Deps:
    store: object
    alerter: object
    now_fn: object
    canvas: object = None          # CanvasClient-like
    collector: object = None       # BrowserCollector-like (courses(), materials(course))
    driver: object = None          # has download(url) -> PageResult
    extractor: object = None       # has extract(material) -> list
    todoist: object = None
    gcal: object = None


def live_approved(cfg):
    return (Path(cfg.secrets_dir) / 'live_writes_approved').exists()


def build_deps(cfg):
    tz = ZoneInfo(cfg.timezone)
    now_fn = lambda: datetime.now(tz)
    store = Store(cfg.db_path)
    d = Deps(store=store, alerter=Alerter(cfg, store, now_fn), now_fn=now_fn)
    if cfg.canvas_base_url and cfg.canvas_token:
        d.canvas = CanvasClient(cfg.canvas_base_url, cfg.canvas_token, cfg.timezone)
    d.extractor = ai.build_extractor(cfg, now_fn().date().isoformat(), cfg.ai_provider)
    if cfg.todoist_api_token:
        d.todoist = todoist.TodoistClient(cfg.todoist_api_token)
    auth = GoogleAuth(cfg.secrets_dir)
    if cfg.google_calendar_id and auth.configured():
        d.gcal = gcal.CalendarClient(auth, cfg.google_calendar_id)
    if cfg.canvas_base_url and cfg.canvas_browser_state and Path(cfg.canvas_browser_state).exists():
        from urllib.parse import urlparse
        hosts = {urlparse(cfg.canvas_base_url).hostname, *cfg.extra_document_hosts}
        d.driver = PlaywrightDriver(cfg.canvas_browser_state, hosts)
        d.collector = BrowserCollector(d.driver, cfg.canvas_base_url)
    return d


def browser_export_materials(filename):
    if not filename or not Path(filename).exists():
        return []
    parsed = json.loads(Path(filename).read_text(encoding='utf-8'))
    if not isinstance(parsed, list):
        raise ValueError('Browser export must be a JSON array')
    return [{'key': 'browser:' + str(e['key']), 'type': 'browser', 'course': str(e['course']), 'title': str(e['title']),
             'text': str(e['text']), 'url': str(e['url'])}
            for e in parsed if all(e.get(k) for k in ('key', 'course', 'title', 'text', 'url'))]


class Collected:
    def __init__(self):
        self.materials, self.errors, self.complete, self.session_expired = [], [], True, False


def collect(cfg, deps):
    out = Collected()
    out.materials.extend(browser_export_materials(cfg.browser_export_json))
    api_ok = False
    if deps.canvas:
        try:
            courses = deps.canvas.courses()
            api_ok = True
            for course in courses:
                try:
                    out.materials.extend(deps.canvas.materials(course))
                except CanvasAuthError:
                    raise
                except Exception as exc:
                    out.complete = False
                    out.errors.append({'stage': 'canvas', 'course': course.get('name'), 'error': str(exc)})
        except CanvasAuthError as exc:
            out.complete = False
            out.errors.append({'stage': 'canvas-auth', 'error': str(exc)})
        except (RetryExhausted, requests.RequestException) as exc:
            out.complete = False
            out.errors.append({'stage': 'canvas', 'error': str(exc)})
    # Browser: full crawl if the API is unusable, otherwise only for what the API refused (403/404).
    if deps.collector:
        try:
            with deps.driver:
                if not api_ok:
                    for course in deps.collector.courses():
                        out.materials.extend(deps.collector.materials(course))
                elif deps.canvas.fallbacks:
                    for fb in deps.canvas.fallbacks:
                        res = deps.driver.goto(fb['url'])
                        out.materials.append({'key': 'browser:' + digest(fb['url'])[:20], 'type': 'browser',
                                              'course': fb.get('course', 'unknown'), 'title': res.title or fb['what'],
                                              'text': res.text, 'url': res.url})
        except SessionExpired as exc:
            out.session_expired, out.complete = True, False
            out.errors.append({'stage': 'browser-session', 'error': f'Canvas browser session expired ({exc}); please log in again'})
        except Exception as exc:
            out.complete = False
            out.errors.append({'stage': 'browser', 'error': str(exc)})
    elif not api_ok and not out.materials:
        out.complete = False
        out.errors.append({'stage': 'config', 'error': 'No Canvas API token, browser session or export configured'})
    else:
        out.session_expired = False
    if out.errors:
        out.complete = False
    return out


def enrich(m, cfg, deps, store, now):
    """Fill m['text']. Returns (material, issue) where issue is a short string for unreadable documents."""
    m = dict(m)
    maxb = cfg.max_document_mb * 1024 * 1024
    if m['type'] in ('file', 'gdoc') and m.get('download_url'):
        raw = None
        try:
            raw = deps.canvas.download(m['download_url'], maxb) if deps.canvas else None
        except (PermissionError, requests.HTTPError, RetryExhausted):
            raw = None
        if raw is None and deps.driver and m.get('browser_url'):
            with deps.driver:
                raw = deps.driver.download(m['browser_url']).body
        if raw is None:
            return m, 'inaccessible'
        if m['type'] == 'gdoc':
            text = raw.decode('utf-8', errors='replace')
            if text.lstrip().lower().startswith(('<!doctype', '<html')):
                return m, 'inaccessible'
            m['text'] = text
        else:
            try:
                m['text'] = extract_document(raw, m.get('filename') or m['title'])
            except NeedsOCR:
                return m, 'needs_ocr'
            except Exception as exc:
                log.warning('Unable to extract %s: %s', m['key'], exc)
                return m, 'unreadable'
    elif m['type'] in HTML_TYPES:
        m['text'] = strip_html(m.get('text', ''))
    return m, None


def scan(cfg, deps=None, force=False):
    deps = deps or build_deps(cfg)
    store, alerter = deps.store, deps.alerter
    now = deps.now_fn()
    now_iso = now.isoformat()
    today = now.date()
    Path(cfg.state_dir).mkdir(parents=True, exist_ok=True)
    if not cfg.dry_run and not live_approved(cfg):
        alerter.alert('live-unapproved', 'Live writes refused',
                      'DRY_RUN=false but live writes have not been approved (run `school-agent approve-live`).', 'error')
        return 2
    run_id = store.start_run(now_iso)
    rep = {'started': now_iso, 'mode': 'dry-run' if cfg.dry_run else 'LIVE', 'sources': {}, 'extracted': 0, 'ai_pending': 0,
           'actions': [], 'new_reviews': [], 'errors': [], 'unreadable': []}

    # 1. collect
    got = collect(cfg, deps)
    rep['errors'].extend(got.errors)
    seen = set()
    for original in got.materials:
        try:
            m, issue = enrich(original, cfg, deps, store, now_iso)
            if issue:
                rep['unreadable'].append({'source': m['key'], 'title': m['title'], 'issue': issue, 'url': m['url']})
                if store.add_review(f"unreadable:{m['key']}:{issue}", 'unreadable_document',
                                    {'source': m['key'], 'title': m['title'], 'url': m['url'], 'issue': issue,
                                     'reason': {'needs_ocr': 'image-only PDF; OCR not run automatically', 'inaccessible': 'API and browser could not open it',
                                                'unreadable': 'could not parse file'}[issue]}, now_iso):
                    rep['new_reviews'].append(f"unreadable:{m['title']}")
                continue
            text = (m.get('text') or '').strip()
            structured = [x for x in (m.get('structured') or [])
                          if 0 <= (date.fromisoformat(x['date']) - today).days + 7 <= cfg.lookahead_days + 7]
            if len(text) < 15 and not structured:
                continue
            fingerprint = digest(json.dumps({'text': text, 'title': m['title'], 'structured': structured}, sort_keys=True))
            seen.add(m['key'])
            rep['sources'][m['type']] = rep['sources'].get(m['type'], 0) + 1
            store.upsert_source(m, fingerprint, now_iso, run_id)
            if not force and not store.needs_extraction(m['key'], fingerprint):
                continue
            obs = [{**s, 'item_key': digest(f"{m['key']}|struct|{s['kind']}|{s['title']}"), 'course': m['course'],
                    'source_key': m['key'], 'source_url': m['url'], 'structured': True} for s in structured]
            extracted = False
            free_text = len(strip_free_text(text, structured)) >= 15
            if deps.extractor and free_text:
                accepted, review = normalize(deps.extractor.extract(m), m, cfg, today)
                obs.extend(accepted)
                for r in review:
                    rkey = 'item:' + digest(f"{r['source_key']}|{r['kind']}|{r['title']}|{r.get('date')}|{r['reason']}")
                    if store.add_review(rkey, r['reason'], r, now_iso):
                        rep['new_reviews'].append(f"{r['reason']}: {r['course']} - {r['title']}")
                extracted = True
                rep['extracted'] += 1
            elif free_text:
                rep['ai_pending'] += 1
            store.replace_observations(m['key'], obs)
            if extracted or not free_text:
                store.mark_extracted(m['key'], fingerprint)
        except (RetryExhausted, requests.RequestException) as exc:
            log.error('Transient failure on %s: %s', original.get('key'), exc)
            rep['errors'].append({'stage': 'process', 'source': original.get('key'), 'error': str(exc)})
        except Exception as exc:
            log.exception('Source processing failed: %s', original.get('key'))
            rep['errors'].append({'stage': 'process', 'source': original.get('key'), 'error': str(exc)})

    for key in seen:
        store.touch_source(key, run_id)
    # review-approved manual items behave like trusted observations
    approvals = inject_approved(store, now_iso, run_id)
    for sk in [s['source_key'] for s in store.conn.execute("SELECT source_key FROM sources WHERE source_key LIKE 'review:%'")]:
        store.touch_source(sk, run_id)

    # 2. sources that disappeared (only trusted when the whole collection succeeded)
    if got.complete:
        for s in store.missing_sources(run_id):
            if store.add_review(f"missing:{s['source_key']}", 'source_missing', {**s, 'reason': 'previously scanned source no longer present; related items left untouched'}, now_iso):
                rep['new_reviews'].append(f"source_missing: {s['title']}")

    # 3. reconcile
    desired, conflicts, orphans = reconcile(store.observations(), store.obligations(), approvals, now_iso)
    for ob in desired:
        store.save_obligation(ob)
    keys = set()
    for r in conflicts:
        keys.add(r['rkey'])
        if store.add_review(r['rkey'], r['type'], r['payload'], now_iso):
            rep['new_reviews'].append(f"{r['type']}: {r['payload'].get('course')} - {r['payload'].get('title')}")
    store.close_reviews_where_obsolete('conflict', keys, now_iso)
    for e in orphans:
        if got.complete and store.add_review(f"orphan:{e['oid']}", 'orphaned_obligation',
                                             {'oid': e['oid'], 'course': e['course'], 'title': e['title'], 'date': e['date'],
                                              'reason': 'no source mentions this any more; remote entries left untouched'}, now_iso):
            rep['new_reviews'].append(f"orphaned: {e['title']}")

    # 4. sync
    sync(cfg, deps, rep, store, now_iso)

    # 5. alerts + report
    if got.session_expired and any(e['stage'] == 'browser-session' for e in rep['errors']):
        alerter.alert('browser-session', 'Canvas browser session expired', 'Log in to Canvas again in the server browser; fallback scans are paused.', 'error')
    for e in rep['errors']:
        if e['stage'] == 'canvas-auth':
            alerter.alert('canvas-auth', 'Canvas token rejected', e['error'], 'error')
    if rep['ai_pending'] or (not deps.extractor and cfg.ai_base_url):
        why = 'not configured' if not cfg.ai_base_url else 'provider not approved for school data (set AI_DATA_APPROVED=true after review)'
        alerter.alert('ai-pending', 'Document extraction paused', f"{rep['ai_pending']} sources waiting: AI provider {why}.", 'warning')
    if rep['new_reviews']:
        alerter.alert('reviews:' + digest('|'.join(sorted(rep['new_reviews'])))[:12], f"{len(rep['new_reviews'])} item(s) need your review",
                      '\n'.join(rep['new_reviews'][:15]) + '\nRun: school-agent review list')
    other_errors = [e for e in rep['errors'] if e['stage'] not in ('canvas-auth', 'browser-session')]
    if other_errors:
        alerter.alert('scan-errors:' + digest(json.dumps(other_errors, sort_keys=True))[:12], f'{len(other_errors)} scan error(s)',
                      '\n'.join(f"{e.get('stage')}: {e.get('error')}" for e in other_errors[:8]), 'error')
    rep['open_reviews'] = [{'id': r['id'], 'type': r['type'], 'payload': r['payload']} for r in store.reviews('open')]
    rep['obligations'] = {'total': len(store.obligations()),
                          'held': sum(1 for o in store.obligations() if o['held'])}
    rep['finished'] = deps.now_fn().isoformat()
    status = 'error' if rep['errors'] else 'ok'
    store.finish_run(run_id, rep['finished'], status, {k: rep[k] for k in ('sources', 'extracted', 'ai_pending')})
    Path(cfg.state_dir, 'last-report.json').write_text(json.dumps(rep, indent=2, default=str), encoding='utf-8')
    log.info('Scan complete (%s): sources=%s actions=%d new_reviews=%d errors=%d', rep['mode'], rep['sources'],
             len(rep['actions']), len(rep['new_reviews']), len(rep['errors']))
    return 1 if rep['errors'] else 0


def strip_free_text(text, structured):
    """Text beyond the synthetic 'Canvas due_at' line, so purely-structured assignments don't need AI."""
    return '\n'.join(l for l in text.splitlines() if not l.startswith('Canvas due_at:')).strip()


def inject_approved(store, now, run_id):
    """Approved reviews -> trusted observations (item reviews) or conflict approvals keyed by oid."""
    approvals = {}
    for r in store.reviews('approved'):
        res, p = r['resolution'] or {}, r['payload']
        if r['type'] == 'conflict' and res.get('date'):
            approvals[p['oid']] = {'date': res['date']}
        elif r['type'] not in ('conflict', 'source_missing', 'orphaned_obligation', 'unreadable_document', 'sync_review', 'ambiguous_match') and res.get('date'):
            key = f"review:{r['id']}"
            m = {'key': key, 'course': p['course'], 'title': p['title'], 'url': p.get('source_url', ''), 'text': p.get('evidence', ''),
                 'updated_at': r['resolved_at'] or now}
            store.upsert_source(m, digest(json.dumps(res)), now, run_id)
            store.replace_observations(key, [{'item_key': digest(key), 'course': p['course'], 'kind': p['kind'], 'title': p['title'],
                                              'date': res['date'], 'time': p.get('time'), 'status': p.get('status', 'scheduled'),
                                              'prev_date': None, 'confidence': 1.0, 'evidence': p.get('evidence', '') or 'approved by user',
                                              'source_url': p.get('source_url', ''), 'structured': True}])
    return approvals


def sync(cfg, deps, rep, store, now_iso):
    cache, dry = {}, cfg.dry_run
    for ob in store.obligations():
        if ob['held'] or not ob['date'] or ob['desired_fp'] == ob['synced_fp']:
            continue
        target = target_for(ob['kind'])
        if not target:
            continue
        client = deps.gcal if target == 'calendar' else deps.todoist
        entry = {'oid': ob['oid'], 'target': target, 'course': ob['course'], 'title': ob['title'], 'date': ob['date'],
                 'status': ob['status'], 'sources': ob['urls']}
        try:
            if client is None:
                if not dry:
                    rep['errors'].append({'stage': 'sync', 'error': f'{target} client not configured; live write skipped for {ob["title"]}'})
                    continue
                rep['actions'].append({**entry, 'action': 'would-create', 'dedupe_checked': False,
                                       'note': f'{target} not connected: duplicate check against existing entries NOT performed'})
                continue
            if target == 'calendar':
                action, detail, rid, rfp = gcal.upsert(client, ob, cfg, dry)
            else:
                action, detail, rid, rfp = todoist.upsert(client, ob, cfg, cache, dry)
        except GoogleAuthError as exc:
            rep['errors'].append({'stage': 'google-auth', 'error': str(exc)})
            deps.alerter.alert('google-auth', 'Google authorization needs renewal', str(exc), 'error')
            continue
        except (RetryExhausted, requests.RequestException) as exc:
            rep['errors'].append({'stage': 'sync', 'error': f'{target}: {exc}'})
            continue
        rep['actions'].append({**entry, 'action': action, 'detail': detail, 'dedupe_checked': True, 'mode': 'dry-run' if dry else 'applied'})
        if action == 'review':
            if store.add_review('sync:' + digest(f"{ob['oid']}|{ob['desired_fp']}|{detail}"), 'sync_review',
                                {**entry, 'reason': detail}, now_iso):
                rep['new_reviews'].append(f"sync_review: {ob['title']} ({detail})")
            continue
        if dry:
            continue
        ours = detail.startswith('relinked own')
        key = 'todoist_id' if target == 'todoist' else 'calendar_id'
        updates = {key: rid or ob[key], 'remote_fp': rfp if rid else ob['remote_fp'], 'synced_fp': ob['desired_fp'], 'updated_at': now_iso}
        if action in ('adopt', 'collision') and not ours:
            updates['adopted'] = 1
        store.save_obligation({**ob, **updates})


def review_cli(cfg, args):
    store = Store(cfg.db_path)
    now = datetime.now(ZoneInfo(cfg.timezone)).isoformat()
    if args.action == 'list':
        for r in store.reviews('open'):
            p = r['payload']
            print(f"#{r['id']:<4} {r['type']:<28} {p.get('course', '')} | {p.get('title', '')} | {p.get('date') or p.get('candidates') or p.get('reason', '')}")
            print(f"       why: {p.get('reason', '')}\n       evidence: {(p.get('evidence') or '')[:160]}  {p.get('source_url') or p.get('url') or ''}")
        return 0
    r = store.review(args.id)
    if not r:
        print('No such review'); return 1
    if args.action == 'show':
        print(json.dumps(r, indent=2)); return 0
    if args.action == 'reject':
        store.resolve_review(r['id'], 'rejected', {}, now); print('rejected'); return 0
    resolution = {'date': args.date} if args.date else {}
    if args.date:
        date.fromisoformat(args.date)
    if r['type'] in ('conflict',) and not args.date:
        print('Conflicts need --date YYYY-MM-DD (the correct one)'); return 1
    if r['type'] not in ('conflict', 'source_missing', 'orphaned_obligation', 'unreadable_document', 'sync_review', 'ambiguous_match') \
            and not (args.date or r['payload'].get('date')):
        print('This item has no date; pass --date YYYY-MM-DD'); return 1
    if not resolution and r['payload'].get('date'):
        resolution = {'date': r['payload']['date']}
    store.resolve_review(r['id'], 'approved', resolution, now)
    print('approved (applied on next scan; writes still obey DRY_RUN)')
    return 0


def auth_google(cfg):
    import getpass
    auth = GoogleAuth(cfg.secrets_dir)
    cid = input('OAuth client ID: ').strip()
    secret = getpass.getpass('OAuth client secret: ').strip()
    print('\nOpen this URL in your own browser, approve access, then copy the FULL address of the page it redirects to\n'
          '(it will fail to load; that is expected):\n\n' + auth.consent_url(cid) + '\n')
    auth.exchange_code(cid, secret, input('Redirected URL: '))
    print(f'Saved refresh token to {auth.path} (mode 600).')
    return 0


def write_env(path, updates):
    """Update KEY=VALUE lines in place (other lines kept), atomically, mode 600, directory mode 700."""
    import os
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lines = path.read_text().splitlines() if path.exists() else []
    done = set()
    for i, line in enumerate(lines):
        key = line.split('=', 1)[0].strip()
        if key in updates and not line.lstrip().startswith('#'):
            lines[i], done = f'{key}={updates[key]}', done | {key}
    lines += [f'{k}={v}' for k, v in updates.items() if k not in done]
    tmp = path.with_name(path.name + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as fh:
        fh.write('\n'.join(lines) + '\n')
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def setup_canvas(cfg, base_url, getpass_fn=None, session=None):
    """Hidden-input token entry; verifies with read-only GET /users/self; keeps DRY_RUN=true. Never prints the token."""
    import getpass
    token = (getpass_fn or getpass.getpass)('Canvas API token (input hidden): ').strip()
    if not token:
        print('No token entered; nothing saved.'); return 1
    base = base_url.rstrip('/')
    sess = session or requests.Session()
    try:
        resp = sess.get(f'{base}/api/v1/users/self', headers={'Authorization': f'Bearer {token}'}, timeout=30)
    except requests.RequestException as exc:
        print(f'Could not reach Canvas ({type(exc).__name__}); nothing saved.'); return 1
    if resp.status_code != 200:
        print(f'Canvas rejected the token (HTTP {resp.status_code}); nothing saved.'); return 1
    write_env(Path(cfg.secrets_dir) / 'env', {'CANVAS_BASE_URL': base, 'CANVAS_TOKEN': token, 'DRY_RUN': 'true'})
    print(f'Authenticated as: {resp.json().get("name", "(name hidden)")}')
    print(f'Saved to {Path(cfg.secrets_dir) / "env"} (mode 600). DRY_RUN=true; no synchronization enabled.')
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description='Course document scan and deadline sync')
    sub = parser.add_subparsers(dest='command')
    sc = sub.add_parser('scan'); sc.add_argument('--force', action='store_true', help='Re-extract unchanged sources')
    rv = sub.add_parser('review')
    rv.add_argument('action', choices=['list', 'show', 'approve', 'reject']); rv.add_argument('id', nargs='?', type=int)
    rv.add_argument('--date')
    sc2 = sub.add_parser('setup-canvas'); sc2.add_argument('--base-url', default='https://wcpss.instructure.com')
    sub.add_parser('auth-google'); sub.add_parser('approve-live'); sub.add_parser('status')
    args = parser.parse_args(argv)
    cfg = Config.from_env()
    cmd = args.command or 'scan'
    if cmd == 'review':
        return review_cli(cfg, args)
    if cmd == 'setup-canvas':
        return setup_canvas(cfg, args.base_url)
    if cmd == 'auth-google':
        return auth_google(cfg)
    if cmd == 'approve-live':
        input('This allows DRY_RUN=false to write to Todoist and Google Calendar. Type YES to confirm: ') == 'YES' or sys.exit(1)
        Path(cfg.secrets_dir).mkdir(parents=True, exist_ok=True, mode=0o700)
        (Path(cfg.secrets_dir) / 'live_writes_approved').write_text(datetime.now().isoformat())
        return 0
    if cmd == 'status':
        print(Path(cfg.state_dir, 'last-report.json').read_text()); return 0
    Path(cfg.state_dir).mkdir(exist_ok=True)
    with open(Path(cfg.state_dir) / 'scan.lock', 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log.warning('Another scan is running; exiting'); return 0
        return scan(cfg, force=getattr(args, 'force', False))


if __name__ == '__main__':
    sys.exit(main())
