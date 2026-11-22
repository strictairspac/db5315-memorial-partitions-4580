import os
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

os.environ.setdefault('DB_DRIVER', 'postgresql+psycopg')
os.environ.setdefault('DB_HOST', '127.0.0.1')
os.environ.setdefault('DB_PORT', '15432')
os.environ.setdefault('DB_USER', 'app')
os.environ.setdefault('DB_PASSWORD', 'local-db-demo')
os.environ.setdefault('DB_NAME', 'workflow_test')
os.environ['AUTH_KEY'] = 'test-key'
os.environ.setdefault('REDIS_URL', 'redis://127.0.0.1:6399/0')

import psycopg
import pytest


def _ensure_database():
    dsn = {'host': os.environ['DB_HOST'], 'port': int(os.environ['DB_PORT']),
           'user': os.environ['DB_USER'], 'password': os.environ['DB_PASSWORD']}
    with psycopg.connect(dbname='postgres', autocommit=True, **dsn) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname = %s",
                            (os.environ['DB_NAME'],)).fetchone():
            conn.execute(f'CREATE DATABASE {os.environ["DB_NAME"]}')


_ensure_database()

from sqlalchemy import text  # noqa: E402

from src.config.settings import engine  # noqa: E402
from src.models.tables import schema  # noqa: E402
from src.services import ledger  # noqa: E402


@pytest.fixture()
def db():
    """每个用例重置：清空账簿分区树与工作流表，重建空 schema。"""
    with engine.begin() as conn:
        names = [row[0] for row in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public' AND tablename LIKE 'ledger%'"))]
        for name in names:
            conn.execute(text(f'DROP TABLE IF EXISTS {name} CASCADE'))
    schema.drop_all(engine)
    schema.create_all(engine)
    ledger.ensure_ledger_schema()
    yield engine
