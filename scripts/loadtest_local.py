"""End-to-end local load test: serve the API with waitress, upload N rows, then hammer it with scripts/loadtest.py.

    python scripts/loadtest_local.py [rows] [threads] [seconds]      # defaults: 100000 20 20

Uses a throwaway DATA_DIR and a free port; nothing touches your real workspace. waitress is a threaded
production-style WSGI server (the same concurrency model as the Docker image's gunicorn --threads).
"""
import http.client
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, 'api'))

rows = int(sys.argv[1]) if len(sys.argv) > 1 else 100_000
threads = sys.argv[2] if len(sys.argv) > 2 else '20'
seconds = sys.argv[3] if len(sys.argv) > 3 else '20'
extra = ['--reads-only'] if '--reads-only' in sys.argv else []
PORT = 5056

tmp = tempfile.mkdtemp(prefix='churn_load_')
env = {**os.environ, 'DATA_DIR': tmp, 'RATE_LIMIT_PER_MIN': '10000000', 'HEAVY_RATE_LIMIT_PER_MIN': '1000',
       'LOG_LEVEL': 'WARNING', 'SYNC_JOBS': '0'}
env.pop('DATABASE_URL', None)
server = subprocess.Popen(
    [sys.executable, '-c', f'import sys; sys.path.insert(0, r"{os.path.join(ROOT, "api")}"); import index, waitress; '
                           f'waitress.serve(index.app, host="127.0.0.1", port={PORT}, threads=16)'],
    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
try:
    for _ in range(60):
        try:
            c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=2)
            c.request('GET', '/api/health')
            if c.getresponse().status == 200:
                break
        except OSError:
            time.sleep(0.5)
    else:
        raise SystemExit('server did not start')

    from churn.demo_data import generate_demo
    csv = generate_demo(rows, seed=21).to_csv(index=False).encode()
    print(f'uploading {rows:,} rows ({len(csv) / 1e6:.1f} MB)…')
    boundary = uuid.uuid4().hex
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="load.csv"\r\nContent-Type: text/csv\r\n\r\n').encode() \
        + csv + f'\r\n--{boundary}--\r\n'.encode()
    c = http.client.HTTPConnection('127.0.0.1', PORT, timeout=600)
    t = time.time()
    c.request('POST', '/api/upload', body=body, headers={'Content-Type': f'multipart/form-data; boundary={boundary}'})
    resp = c.getresponse()
    job_id = json.loads(resp.read())['job_id']
    while True:
        c.request('GET', f'/api/jobs/{job_id}')
        job = json.loads(c.getresponse().read())
        if job['status'] in ('done', 'error'):
            break
        time.sleep(2)
    print(f'training job: {job["status"]} in {time.time() - t:.0f}s' + (f' — {job["error"]}' if job['status'] == 'error' else ''))
    if job['status'] != 'done':
        raise SystemExit(1)

    # While the box is busy serving traffic, make sure a second user's job is admitted/queued sensibly.
    subprocess.check_call([sys.executable, os.path.join(ROOT, 'scripts', 'loadtest.py'), f'http://127.0.0.1:{PORT}',
                           '--threads', threads, '--seconds', seconds, *extra])
finally:
    server.terminate()
    server.wait(timeout=15)
    shutil.rmtree(tmp, ignore_errors=True)
