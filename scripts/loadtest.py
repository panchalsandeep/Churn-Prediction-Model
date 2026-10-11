"""Small HTTP load test for the read-heavy endpoints (what a public dashboard actually serves).

    python scripts/loadtest.py http://localhost:5056 --threads 24 --seconds 20

Requires a trained workspace on the target. Uses one keep-alive connection per thread and a realistic mix:
dashboard payload, paged/sorted/filtered customer lists, customer detail, single predictions, alerts.
Prints throughput, error rate and p50/p95/p99 latency per endpoint. Stdlib only.
"""
import argparse
import http.client
import json
import random
import statistics
import threading
import time
from urllib.parse import urlparse


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else float('nan')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('base')
    ap.add_argument('--threads', type=int, default=16)
    ap.add_argument('--seconds', type=int, default=15)
    ap.add_argument('--reads-only', action='store_true', help='skip the CPU-heavy explain endpoints (detail, predict)')
    args = ap.parse_args()
    u = urlparse(args.base)

    def conn():
        return http.client.HTTPConnection(u.hostname, u.port, timeout=60)

    def call(c, method, path, body=None):
        headers = {'Accept-Encoding': 'gzip'}
        if body is not None:
            headers['Content-Type'] = 'application/json'
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=headers)
        r = c.getresponse()
        data = r.read()
        return r.status, data

    c0 = conn()
    status, data = call(c0, 'GET', '/api/customers?limit=200&sort=loss')
    if status != 200:
        raise SystemExit(f'No trained workspace on the target (HTTP {status}). Train one first.')
    import gzip
    page = json.loads(gzip.decompress(data) if data[:2] == b'\x1f\x8b' else data)
    ids = [c['id'] for c in page['customers']]
    total = page['total']
    status, data = call(c0, 'GET', '/api/model/schema')
    form = json.loads(gzip.decompress(data) if data[:2] == b'\x1f\x8b' else data)
    values = {f['name']: f['default'] for f in form['fields']}
    print(f'target: {args.base} · {total:,} customers · {args.threads} threads · {args.seconds}s')

    mix = [
        (4, 'GET /api/results', lambda: ('GET', '/api/results', None)),
        (6, 'GET /api/customers (page, sort by risk)', lambda: ('GET', f'/api/customers?limit=20&page={random.randint(1, 50)}&sort=prob', None)),
        (4, 'GET /api/customers (tier=High, by loss)', lambda: ('GET', f'/api/customers?tier=High&limit=20&page={random.randint(1, 20)}&sort=loss', None)),
        (3, 'GET /api/customers (search id)', lambda: ('GET', f'/api/customers?q={random.choice(ids)[:8]}&limit=10', None)),
        (3, 'GET /api/customers/<id>', lambda: ('GET', f'/api/customers/{random.choice(ids)}', None)),
        (2, 'POST /api/predict (explain)', lambda: ('POST', '/api/predict', {'values': values, 'explain': True})),
        (1, 'GET /api/alerts', lambda: ('GET', '/api/alerts?status=Open&limit=50', None)),
    ]
    if args.reads_only:
        mix = [m for m in mix if 'predict' not in m[1] and '<id>' not in m[1]]
    population = [m for m in mix for _ in range(m[0])]
    stats = {m[1]: [] for m in mix}
    errors = {m[1]: 0 for m in mix}
    lock = threading.Lock()
    stop = time.time() + args.seconds

    def worker():
        c = conn()
        while time.time() < stop:
            _, name, make = random.choice(population)
            method, path, body = make()
            t = time.perf_counter()
            try:
                status, _ = call(c, method, path, body)
                ok = status == 200
            except Exception:
                ok = False
                c.close()
                c = conn()
            ms = (time.perf_counter() - t) * 1000
            with lock:
                if ok:
                    stats[name].append(ms)
                else:
                    errors[name] += 1

    threads = [threading.Thread(target=worker) for _ in range(args.threads)]
    t0 = time.time()
    [t.start() for t in threads]
    [t.join() for t in threads]
    elapsed = time.time() - t0

    allv = [v for lst in stats.values() for v in lst]
    print(f'\n{"endpoint":<44}{"req":>7}{"err":>5}{"p50":>8}{"p95":>8}{"p99":>8}  (ms)')
    for name, vals in stats.items():
        print(f'{name:<44}{len(vals):>7}{errors[name]:>5}{percentile(vals, .5):>8.0f}{percentile(vals, .95):>8.0f}{percentile(vals, .99):>8.0f}')
    err_total = sum(errors.values())
    print(f'\ntotal {len(allv) + err_total:,} requests in {elapsed:.1f}s = {(len(allv) + err_total) / elapsed:.0f} req/s · '
          f'{err_total} errors · overall p50 {statistics.median(allv):.0f} ms, p95 {percentile(allv, .95):.0f} ms')


if __name__ == '__main__':
    main()
