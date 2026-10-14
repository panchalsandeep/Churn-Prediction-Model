"""Dedicated job worker:  python -m churn.worker  (run from the api/ directory, or with api/ on PYTHONPATH).

Scale out by running more of these (same DATABASE_URL, same blob storage, same master key) and set EMBEDDED_WORKER=0 on web pods.
"""
from __future__ import annotations

import argparse
import logging

from . import config, db, handlers, jobs, pipeline  # noqa: F401  (handlers registers the job kinds; pipeline registers the schedule tick)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--concurrency', type=int, default=None, help='jobs to run at once (default: MAX_CONCURRENT_JOBS)')
    args = p.parse_args()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    db.get_engine()              # run migrations / verify connectivity before accepting work
    jobs.run_worker(args.concurrency)


if __name__ == '__main__':
    main()
