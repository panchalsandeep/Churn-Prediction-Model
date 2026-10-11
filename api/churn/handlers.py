"""Job handlers: what a worker runs for each kind of job. Importing this module registers them."""
from __future__ import annotations

from . import jobs, service
from .demo_data import generate_demo


@jobs.handler('train')
def train(ws, params, df, progress):
    return service.run_training(ws, df, params.get('algorithm', 'auto'), params.get('actor'), progress, params.get('promote'),
                                params.get('source', 'upload'), params.get('target'), params.get('exclude'), params.get('allow_pii'))


@jobs.handler('demo')
def demo(ws, params, df, progress):
    return service.run_training(ws, generate_demo(int(params.get('rows', 8000))), params.get('algorithm', 'auto'), params.get('actor'),
                                progress, None, 'demo')


@jobs.handler('score')
def score(ws, params, df, progress):
    return service.run_batch_scoring(ws, df, params.get('actor'), progress, bool(params.get('allow_missing')))
