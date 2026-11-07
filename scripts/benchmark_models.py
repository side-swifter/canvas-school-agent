#!/usr/bin/env python3
"""Benchmark candidate local models on bench/cases.py using the REAL extraction + validation path.
usage: benchmark_models.py MODEL [MODEL...] [--url http://127.0.0.1:11434] [--out FILE]
Everything stays on the local Ollama server; no data is sent elsewhere."""
import argparse, json, sys, time, dataclasses
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from school_agent.ai import Extractor, Ollama
from school_agent.config import Config
from school_agent.normalize import normalize
from bench.cases import CASES

GROUP = {'test': 'assess', 'quiz': 'assess', 'assignment': 'todo', 'deadline': 'todo', 'event': 'event', 'study': 'todo'}
REF = date(2026, 11, 7)


def score_case(case, accepted, review):
    exp = list(case['expected'])
    matched, extra, bad = 0, [], []
    pool = list(accepted)
    for g, d, st in exp:
        hit = next((a for a in pool if a['date'] == d and (GROUP[a['kind']] == g or {GROUP[a['kind']], g} == {'assess', 'event'}) and a['status'] in st), None)
        if hit:
            matched += 1; pool.remove(hit)
    allowed_extra = set(case.get('any_event_dates', [])) | set(case.get('lenient', []))
    for a in pool:
        if a['date'] in case['forbid']:
            bad.append(a)
        elif a['date'] not in allowed_extra:
            extra.append(a)
    if case.get('any_event_dates') and not matched and any(a['date'] in case['any_event_dates'] for a in accepted):
        matched = len(exp); extra = [e for e in extra if e['date'] not in case['any_event_dates']]
    return len(exp), matched, extra, bad


def main():
    ap = argparse.ArgumentParser(); ap.add_argument('models', nargs='+'); ap.add_argument('--url', default='http://127.0.0.1:11434')
    ap.add_argument('--out'); ap.add_argument('--cases', help='comma list of case ids'); a = ap.parse_args()
    cases = [c for c in CASES if not a.cases or c['id'] in a.cases.split(',')]
    results = {}
    for model in a.models:
        cfg = Config(ai_provider='ollama', ai_base_url=a.url, ai_model=model, ai_num_ctx=4096)
        backend = Ollama(cfg); ex = Extractor(backend, REF.isoformat(), 8000, True)
        tot_exp = tot_hit = tot_extra = tot_bad = 0; secs = 0.0; rows = []
        print(f'\n=== {model}', flush=True)
        for c in cases:
            m = {'key': 'bench:' + c['id'], 'course': c['course'], 'title': c['title'], 'type': c['type'], 'text': c['text'], 'url': 'bench'}
            t = time.time()
            try:
                raw = ex.extract(m)
            except Exception as e:
                raw = []; print('  ERROR', c['id'], e)
            dt = time.time() - t; secs += dt
            acc, rev = normalize(raw, m, dataclasses.replace(cfg, lookahead_days=200, min_confidence=0.85), REF)
            n, hit, extra, bad = score_case(c, acc, rev)
            tot_exp += n; tot_hit += hit; tot_extra += len(extra); tot_bad += len(bad)
            flag = 'OK ' if hit == n and not extra and not bad else 'BAD' if bad else '~  '
            print(f'  {flag} {c["id"]:20} exp={n} hit={hit} extra={[(e["kind"], e["date"], e["status"]) for e in extra]} forbidden={[b["date"] for b in bad]} '
                  f'review={len(rev)} {dt:5.0f}s', flush=True)
            rows.append(dict(id=c['id'], expected=n, hit=hit, extra=[(e['kind'], e['date'], e['status']) for e in extra], forbidden=[b['date'] for b in bad], secs=round(dt, 1)))
        s = backend.stats
        results[model] = dict(recall=round(tot_hit / max(tot_exp, 1), 3), false_accepts=tot_extra, forbidden_accepts=tot_bad,
                              total_secs=round(secs), avg_secs=round(secs / len(cases), 1), prompt_tokens=s['prompt_tokens'], gen_tokens=s['gen_tokens'], rows=rows)
        r = results[model]
        print(f'  -> recall {r["recall"]:.0%} ({tot_hit}/{tot_exp})  false_accepts={tot_extra}  FORBIDDEN={tot_bad}  total {r["total_secs"]}s  avg {r["avg_secs"]}s/case', flush=True)
    if a.out:
        Path(a.out).write_text(json.dumps(results, indent=2))


if __name__ == '__main__':
    main()
