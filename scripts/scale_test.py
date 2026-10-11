"""Measure what a big upload costs: training time per stage, peak memory, database size and query latency.

    python scripts/scale_test.py [rows]        # default 100000

Runs in a throwaway DATA_DIR (deleted afterwards), so it never touches your workspace data.
Memory is read with psutil when installed (pip install psutil), otherwise skipped.
"""
import os
import shutil
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))

rows = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
tmp = tempfile.mkdtemp(prefix='churn_scale_')
os.environ['DATA_DIR'] = tmp
os.environ.pop('DATABASE_URL', None)

from churn import db, service  # noqa: E402
from churn.demo_data import generate_demo  # noqa: E402

try:
    import psutil
    proc = psutil.Process()
except ImportError:
    proc = None

peak = [0]


def sample_memory():
    while not stop.is_set():
        peak[0] = max(peak[0], proc.memory_info().rss)
        time.sleep(0.25)


stop = threading.Event()
if proc:
    threading.Thread(target=sample_memory, daemon=True).start()

t0 = time.time()
print(f'generating {rows:,} rows…')
df = generate_demo(rows, seed=3)
print(f'  done in {time.time() - t0:.1f}s ({df.memory_usage(deep=True).sum() / 1e6:.0f} MB in memory)')

stages = {}
last = [time.time(), 'start']


def progress(stage, pct):
    now = time.time()
    stages[last[1]] = stages.get(last[1], 0) + now - last[0]
    last[0], last[1] = now, stage


t = time.time()
result = service.run_training('scale', df, 'auto', 'scale-test', progress)
total = time.time() - t
stages[last[1]] = stages.get(last[1], 0) + time.time() - last[0]
print(f'\ntraining {rows:,} rows: {total:.0f}s total · status {result["status"]} · ROC-AUC {result["metrics"]["roc_auc"]} · PR-AUC {result["metrics"]["pr_auc"]}')
for stage, secs in sorted(stages.items(), key=lambda kv: -kv[1])[:6]:
    print(f'  {stage:<38}{secs:>7.1f}s')
print('  after training:', ', '.join(f'{k} {v}s' for k, v in result['steps'].items()))

dbfile = os.path.join(tmp, 'churnsight.db')
size = lambda p: sum(os.path.getsize(os.path.join(d, f)) for d, _, fs in os.walk(p) for f in fs) if os.path.isdir(p) else os.path.getsize(p)
print(f'\nstorage: database {size(dbfile) / 1e6:.0f} MB · model artifacts + snapshot {size(os.path.join(tmp, "blobs")) / 1e6:.0f} MB')
if proc:
    print(f'peak memory (RSS): {peak[0] / 1e6:.0f} MB')


def timed(label, fn, n=5):
    ts = []
    for _ in range(n):
        s = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - s) * 1000)
    ts.sort()
    print(f'  {label:<48}p50 {ts[len(ts) // 2]:>7.1f} ms   max {ts[-1]:>7.1f} ms')


print('\nquery latency:')
service._results_cache.clear()
from churn import registry
_s = service.get_settings('scale')
_row, _bundle = registry.champion_bundle('scale')
_thr = service.effective_thresholds(_s, _bundle)
timed('  · summary() aggregates', lambda: service.summary('scale', _s, _thr), 3)
timed('  · top-50 by expected loss', lambda: service.query_customers('scale', sort='loss', limit=50), 3)
timed('  · registry.champion + meta', lambda: registry.champion('scale'), 3)
timed('results payload (uncached)', lambda: (service._results_cache.clear(), service.build_results('scale')), 3)
timed('results payload (cached)', lambda: service.build_results('scale'))
timed('customers page 1 (sort by risk)', lambda: service.query_customers('scale', limit=20))
timed('customers page 500 (offset 10,000)', lambda: service.query_customers('scale', page=500, limit=20))
timed('customers tier=High by expected loss', lambda: service.query_customers('scale', tier='High', sort='loss', limit=20))
timed('search by ID prefix', lambda: service.query_customers('scale', q='CUST-0420', limit=10))
timed('customer detail (explained on demand)', lambda: service.customer_detail('scale', 'CUST-%06d' % (rows // 2)))
timed('predict (explain)', lambda: service.predict('scale', {}), 3)
s = time.perf_counter()
n = sum(1 for _ in service.export_csv_rows('scale', None, None, None, 'prob', 'desc', cap=20000))
print(f'  export of {n:,} rows (keyset streaming){"":<20}{(time.perf_counter() - s) * 1000:>9.0f} ms')

stop.set()
db.reset_engine()
shutil.rmtree(tmp, ignore_errors=True)
