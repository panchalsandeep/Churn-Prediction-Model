"""Alembic environment. Migrations run programmatically from churn.db (see db.migrate) on the app's own connection."""
import os
import sys

import sqlalchemy as sa
from alembic import context

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from churn import config as app_config, db  # noqa: E402

config = context.config
target_metadata = db.metadata


def run_migrations_online() -> None:
    connection = config.attributes.get('connection')
    owns = connection is None
    if owns:                                          # `alembic` CLI usage: build our own engine
        engine = sa.create_engine(app_config.database_url())
        connection = engine.connect()
    try:
        context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
    finally:
        if owns:
            connection.close()


run_migrations_online()
