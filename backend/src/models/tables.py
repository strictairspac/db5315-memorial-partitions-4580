from sqlalchemy import (JSON, Column, Date, ForeignKey, Integer, MetaData,
                        String, Table, Text)

schema = MetaData()
users = Table('accounts', schema,
    Column('id', Integer, primary_key=True),
    Column('username', String(40), nullable=False, unique=True),
    Column('password_hash', String(200), nullable=False),
    Column('role', String(20), nullable=False))
cases = Table('business_cases', schema,
    Column('id', Integer, primary_key=True),
    Column('slug', String(64), nullable=False, unique=True),
    Column('title', String(120), nullable=False),
    Column('payload', JSON, nullable=False),
    Column('author', String(40), nullable=False),
    Column('created_at', String(32), nullable=False))
runs = Table('business_runs', schema,
    Column('id', Integer, primary_key=True),
    Column('case_id', ForeignKey('business_cases.id'), nullable=False, index=True),
    Column('status', String(20), nullable=False, index=True),
    Column('owner', String(40), nullable=False),
    Column('attempt_token', String(36)),
    Column('started_at', String(32)),
    Column('finished_at', String(32)),
    Column('created_at', String(32), nullable=False),
    Column('error', Text))
evidence = Table('business_evidence', schema,
    Column('id', Integer, primary_key=True),
    Column('run_id', ForeignKey('business_runs.id'), nullable=False, unique=True),
    Column('report', JSON, nullable=False),
    Column('digest', String(64), nullable=False),
    Column('reviewer', String(40)),
    Column('review_note', String(500)),
    Column('reviewed_at', String(32)))
audit = Table('audit_events', schema,
    Column('id', Integer, primary_key=True),
    Column('actor', String(40), nullable=False),
    Column('action', String(32), nullable=False),
    Column('reference', String(50), nullable=False),
    Column('created_at', String(32), nullable=False))
# 在线账簿的登记控制表。凭条本体存放在 PostgreSQL 声明式分区表 ledger 中，
# 由 src/services/ledger.py 用原生 DDL 建表（月分区 + DEFAULT 待核池）。
ledger_months = Table('ledger_months', schema,
    Column('month', Date, primary_key=True),
    Column('mode', String(20), nullable=False),
    Column('created_at', String(32), nullable=False))
ledger_migrations = Table('ledger_migrations', schema,
    Column('id', Integer, primary_key=True),
    Column('month', Date, nullable=False, unique=True),
    Column('state', String(20), nullable=False, index=True),
    Column('monthly_table', String(64), nullable=False),
    Column('daily_table', String(64), nullable=False),
    Column('checkpoint_on', Date),
    Column('checkpoint_id', String(200)),
    Column('copied', Integer, nullable=False),
    Column('batch_size', Integer, nullable=False),
    Column('last_error', Text),
    Column('created_at', String(32), nullable=False),
    Column('updated_at', String(32), nullable=False),
    Column('switched_at', String(32)))
