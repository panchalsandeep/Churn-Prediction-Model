"""Job handlers: what a worker runs for each kind of job. Importing this module registers them."""
from __future__ import annotations

from . import jobs, notify, service  # noqa: F401  (notify registers the alert listener in worker processes too)
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


@jobs.handler('pipeline_train')
def pipeline_train(ws, params, df, progress):
    from . import pipeline
    return pipeline.run_train(ws, params.get('actor'), params.get('algorithm', 'auto'), progress)


@jobs.handler('pipeline_score')
def pipeline_score(ws, params, df, progress):
    from . import pipeline
    return pipeline.run_score(ws, params.get('actor'), progress)


@jobs.handler('mlops_check')
def mlops_check(ws, params, df, progress):
    from . import retrain
    return retrain.run_check(ws, params.get('actor'), progress, bool(params.get('force')))


@jobs.handler('backtest')
def backtest_job(ws, params, df, progress):
    from . import backtest
    return backtest.run(ws, progress, int(params.get('origins', 4)), params.get('algorithm', 'logistic_regression'))
