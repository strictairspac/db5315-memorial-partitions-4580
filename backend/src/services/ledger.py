"""在线账簿：真实 PostgreSQL 分区表上的凭条入账、按日期查账和节庆月拆分。

物理结构（public 模式，持久表）：

- ``ledger`` 父表，``PARTITION BY RANGE (entered_on)``，主键 ``(slip_id, entered_on)``。
- 每个在册月份一张月分区 ``ledger_YYYYMM``；``ledger_default`` 为 DEFAULT 待核池。
- 拆分时为当月建影子日分区树 ``ledger_YYYYMM_daily``（每天一个叶子），
  复制追齐后在一条事务里 DETACH 月分区、ATTACH 日分区树，整月一次性切换。
  旧月表改名 ``ledger_YYYYMM_archive`` 留档。

一致性口径：

- 闸机补传按 ``(slip_id, entered_on)`` 幂等入账（ON CONFLICT DO NOTHING），
  已回执的凭条重传不会重复，也不会丢失。
- 拆分期间（copying/aligned）入账事务同时写月表和影子日分区（双写），
  因此新到凭条始终两边一致；查询始终走父表，切换前读到的仍是月表。
- 复制游标（checkpoint_on, checkpoint_id）随每批提交持久化，
  中断后从检查点续拷，重复拷贝靠主键去重，不丢不重。
- 切换前用 EXCEPT 双向对账，月表有而日分区没有的行先补拷；
  日分区多出月表的行属于异常，暂停切换并记录 last_error。
"""

import logging
from datetime import date, timedelta

from fastapi import HTTPException
from sqlalchemy import select, text

from src.config.settings import engine
from src.models.tables import audit, ledger_migrations, ledger_months
from src.utils.time import utc_time

PARENT = 'ledger'
DEFAULT_TABLE = 'ledger_default'
SCHEMA_LOCK = 531500  # pg_advisory_xact_lock key，串行化建表与拆分

MONTHLY = 'monthly'    # 月表
MIGRATING = 'migrating'  # 拆分中
DAILY = 'daily'        # 已切换日分区

COPYING = 'copying'    # 复制追齐中
ALIGNED = 'aligned'    # 已对齐，待切换
SWITCHED = 'switched'  # 已切换
ACTIVE_STATES = (COPYING, ALIGNED)

BATCH_SIZE = 500
MAX_INGEST = 2000
QUERY_LIMIT = 1000
MAX_SPAN_DAYS = 366

LEDGER_DDL = f'''
    CREATE TABLE IF NOT EXISTS {PARENT} (
        slip_id text NOT NULL,
        entered_on date NOT NULL,
        source text NOT NULL DEFAULT '闸机补传',
        received_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (slip_id, entered_on)
    ) PARTITION BY RANGE (entered_on)
'''

# 影子日分区树的列定义必须与父表完全一致，ATTACH 时才不需要改表。
DAILY_DDL = '''
    CREATE TABLE {table} (
        slip_id text NOT NULL,
        entered_on date NOT NULL,
        source text NOT NULL DEFAULT '闸机补传',
        received_at timestamptz NOT NULL DEFAULT now(),
        PRIMARY KEY (slip_id, entered_on)
    ) PARTITION BY RANGE (entered_on)
'''


def monthly_table(start):
    return f'ledger_{start:%Y%m}'


def daily_table(start):
    return f'{monthly_table(start)}_daily'


def next_month(start):
    return date(start.year + start.month // 12, start.month % 12 + 1, 1)


def _read_date(value, label):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(422, f'{label}须为有效日期（YYYY-MM-DD）') from None
    if not isinstance(value, str) or parsed.isoformat() != value:
        raise HTTPException(422, f'{label}须为 YYYY-MM-DD 格式')
    return parsed


def _read_month(value, label):
    start = _read_date(value, label)
    if start.day != 1 or start == date(9999, 12, 1):
        raise HTTPException(422, f'{label}须为月初日期，且须有可表示的次月月初')
    return start


def _log(conn, actor, action, reference):
    conn.execute(audit.insert().values(actor=actor, action=action,
                 reference=str(reference)[:50], created_at=utc_time()))


def ensure_ledger_schema():
    """幂等建立父表与 DEFAULT 待核池；咨询锁避免并发建表冲突。"""
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': SCHEMA_LOCK})
        conn.execute(text(LEDGER_DDL))
        if not conn.execute(text('SELECT to_regclass(:name)'), {'name': f'public.{DEFAULT_TABLE}'}).scalar_one():
            conn.execute(text(f'CREATE TABLE {DEFAULT_TABLE} PARTITION OF {PARENT} DEFAULT'))


def _create_month_partition(conn, start):
    table = monthly_table(start)
    if conn.execute(text('SELECT to_regclass(:name)'), {'name': f'public.{table}'}).scalar_one():
        raise HTTPException(409, f'分区表 {table} 已存在')
    conn.execute(text(f'''
        CREATE TABLE {table} PARTITION OF {PARENT}
        FOR VALUES FROM (DATE '{start.isoformat()}') TO (DATE '{next_month(start).isoformat()}')
    '''))
    conn.execute(ledger_months.insert().values(month=start, mode=MONTHLY, created_at=utc_time()))
    return table


def register_month(raw, actor):
    start = _read_month(raw, '在册月份')
    ensure_ledger_schema()
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': SCHEMA_LOCK})
        if conn.execute(select(ledger_months).where(ledger_months.c.month == start)).first():
            raise HTTPException(409, f'{start:%Y-%m} 已在册')
        table = _create_month_partition(conn, start)
        _log(conn, actor, 'ledger_month_register', f'{start:%Y-%m}')
    return {'month': start.isoformat(), 'mode': MONTHLY, 'table': table}


def _parse_records(records):
    if not isinstance(records, list) or not records:
        raise HTTPException(422, '凭条清单须为非空数组')
    if len(records) > MAX_INGEST:
        raise HTTPException(422, f'单次补传不能超过 {MAX_INGEST} 张')
    slips = []
    for index, row in enumerate(records, 1):
        slip_id = row.get('slip_id') if isinstance(row, dict) else None
        if not isinstance(slip_id, str) or not slip_id or slip_id != slip_id.strip() \
                or '\x00' in slip_id or len(slip_id) > 200:
            raise HTTPException(422, f'第 {index} 行凭条编号须为非空文本，且不能含空字符或首尾空白')
        entered_on = _read_date(row.get('entered_on'), f'凭条 {slip_id} 入园日期')
        source = row.get('source') or '闸机补传'
        if not isinstance(source, str) or not source.strip() or len(source) > 60:
            raise HTTPException(422, f'凭条 {slip_id} 来源须为 60 字以内的非空文本')
        slips.append((slip_id, entered_on, source.strip()))
    return slips


def _partitions_for(pairs):
    """回读凭条实际落入的物理分区（tableoid），用于回执与页面展示。"""
    if not pairs:
        return {}
    ids = sorted({slip_id for slip_id, _ in pairs})
    days = sorted({entered_on for _, entered_on in pairs})
    with engine.connect() as conn:
        rows = conn.execute(text(f'''
            SELECT slip_id, entered_on, tableoid::regclass::text AS partition
            FROM {PARENT}
            WHERE slip_id = ANY(:ids) AND entered_on = ANY(:days)
        '''), {'ids': ids, 'days': days}).all()
    return {(row.slip_id, row.entered_on): row.partition for row in rows}


def ingest(records, actor):
    """闸机补传入账。整批一个事务；拆分中的月份双写影子日分区。"""
    slips = _parse_records(records)
    received_at = utc_time()
    acks = []
    with engine.begin() as conn:
        active = {row.month: row.daily_table for row in conn.execute(
            select(ledger_migrations).where(ledger_migrations.c.state.in_(ACTIVE_STATES))).mappings()}
        for slip_id, entered_on, source in slips:
            params = {'slip_id': slip_id, 'entered_on': entered_on, 'source': source, 'received_at': received_at}
            inserted = conn.execute(text(f'''
                INSERT INTO {PARENT} (slip_id, entered_on, source, received_at)
                VALUES (:slip_id, :entered_on, :source, :received_at)
                ON CONFLICT (slip_id, entered_on) DO NOTHING
                RETURNING slip_id
            '''), params).first() is not None
            shadow = active.get(entered_on.replace(day=1))
            if shadow:
                conn.execute(text(f'''
                    INSERT INTO {shadow} (slip_id, entered_on, source, received_at)
                    VALUES (:slip_id, :entered_on, :source, :received_at)
                    ON CONFLICT (slip_id, entered_on) DO NOTHING
                '''), params)
            acks.append({'slip_id': slip_id, 'entered_on': entered_on.isoformat(),
                         'result': 'inserted' if inserted else 'duplicate'})
        inserted_count = sum(ack['result'] == 'inserted' for ack in acks)
        _log(conn, actor, 'ledger_ingest', f'入账{inserted_count}/共{len(acks)}张')
    places = _partitions_for([(slip_id, entered_on) for slip_id, entered_on, _ in slips])
    for ack, (slip_id, entered_on, _) in zip(acks, slips):
        ack['partition'] = places.get((slip_id, entered_on))
    return acks


def query_slips(date_from, date_to, limit=QUERY_LIMIT):
    start = _read_date(date_from, '起始日期')
    end = _read_date(date_to, '结束日期')
    if end < start:
        raise HTTPException(422, '结束日期不能早于起始日期')
    if (end - start).days > MAX_SPAN_DAYS:
        raise HTTPException(422, f'查询跨度不能超过 {MAX_SPAN_DAYS} 天')
    with engine.connect() as conn:
        rows = conn.execute(text(f'''
            SELECT slip_id, entered_on, source, received_at, tableoid::regclass::text AS partition
            FROM {PARENT}
            WHERE entered_on >= :start AND entered_on <= :end
            ORDER BY entered_on, slip_id
            LIMIT :limit
        '''), {'start': start, 'end': end, 'limit': limit + 1}).mappings().all()
    truncated = len(rows) > limit
    return {
        'rows': [{
            'slip_id': row['slip_id'],
            'entered_on': row['entered_on'].isoformat(),
            'source': row['source'],
            'received_at': row['received_at'].isoformat(),
            'partition': row['partition'],
        } for row in rows[:limit]],
        'truncated': truncated,
    }


def _count(conn, table):
    return conn.execute(text(f'SELECT count(*) FROM {table}')).scalar_one()


def _migration_view(conn, row):
    view = dict(row)
    view['month'] = row['month'].isoformat()
    view['label'] = f"{row['month']:%Y-%m}"
    if row['state'] in ACTIVE_STATES:
        view['monthly_count'] = _count(conn, row['monthly_table'])
        view['daily_count'] = _count(conn, row['daily_table'])
    return view


def overview():
    with engine.connect() as conn:
        months = []
        for row in conn.execute(select(ledger_months).order_by(ledger_months.c.month)).mappings():
            table = daily_table(row['month']) if row['mode'] == DAILY else monthly_table(row['month'])
            months.append({
                'month': row['month'].isoformat(),
                'label': f"{row['month']:%Y-%m}",
                'mode': row['mode'],
                'table': table,
                'count': _count(conn, table),
            })
        active = conn.execute(select(ledger_migrations).where(ledger_migrations.c.state.in_(ACTIVE_STATES))
                              .order_by(ledger_migrations.c.id).limit(1)).mappings().first()
        migrations = [_migration_view(conn, row) for row in conn.execute(
            select(ledger_migrations).order_by(ledger_migrations.c.id.desc()).limit(10)).mappings()]
        migration = _migration_view(conn, active) if active else None
        default_count = _count(conn, DEFAULT_TABLE)
    return {
        'months': months,
        'default_table': DEFAULT_TABLE,
        'default_count': default_count,
        'migration': migration,
        'migrations': migrations,
    }


def start_migration(raw, actor):
    """为在册月份建立影子日分区树并进入 copying 状态。同一时间只允许一个拆分任务。"""
    start = _read_month(raw, '拆分月份')
    ensure_ledger_schema()
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': SCHEMA_LOCK})
        registry = conn.execute(select(ledger_months).where(ledger_months.c.month == start)).mappings().first()
        if not registry:
            raise HTTPException(404, f'{start:%Y-%m} 尚未在册')
        if registry['mode'] != MONTHLY:
            raise HTTPException(409, f'{start:%Y-%m} 已在拆分或已切换日分区')
        if conn.execute(select(ledger_migrations).where(ledger_migrations.c.state.in_(ACTIVE_STATES))).first():
            raise HTTPException(409, '已有拆分任务进行中，完成后才能拆分其他月份')
        daily = daily_table(start)
        conn.execute(text(DAILY_DDL.format(table=daily)))
        day = start
        while day < next_month(start):
            following = day + timedelta(days=1)
            conn.execute(text(f'''
                CREATE TABLE {daily}_{day.day:02d} PARTITION OF {daily}
                FOR VALUES FROM (DATE '{day.isoformat()}') TO (DATE '{following.isoformat()}')
            '''))
            day = following
        now = utc_time()
        migration_id = conn.execute(ledger_migrations.insert().values(
            month=start, state=COPYING, monthly_table=monthly_table(start), daily_table=daily,
            copied=0, batch_size=BATCH_SIZE, created_at=now, updated_at=now)).inserted_primary_key[0]
        conn.execute(ledger_months.update().where(ledger_months.c.month == start).values(mode=MIGRATING))
        _log(conn, actor, 'ledger_split_start', f'{start:%Y-%m}')
        return _migration_view(conn, conn.execute(
            select(ledger_migrations).where(ledger_migrations.c.id == migration_id)).mappings().one())


def _copy_batch(conn, migration):
    """从检查点续拷一批；重复行靠主键 ON CONFLICT 去重。返回 (扫描数, 实拷数, 末行键)。"""
    where = ''
    params = {'batch': migration['batch_size']}
    if migration['checkpoint_on'] is not None:
        where = 'WHERE (entered_on, slip_id) > (:ck_on, :ck_id)'
        params.update(ck_on=migration['checkpoint_on'], ck_id=migration['checkpoint_id'])
    row = conn.execute(text(f'''
        WITH batch AS (
            SELECT slip_id, entered_on, source, received_at FROM {migration['monthly_table']}
            {where}
            ORDER BY entered_on, slip_id
            LIMIT :batch
        ), ins AS (
            INSERT INTO {migration['daily_table']} (slip_id, entered_on, source, received_at)
            SELECT slip_id, entered_on, source, received_at FROM batch
            ON CONFLICT (slip_id, entered_on) DO NOTHING
            RETURNING 1
        )
        SELECT (SELECT count(*) FROM batch) AS scanned,
               (SELECT count(*) FROM ins) AS inserted,
               (SELECT entered_on FROM batch ORDER BY entered_on DESC, slip_id DESC LIMIT 1) AS last_on,
               (SELECT slip_id FROM batch ORDER BY entered_on DESC, slip_id DESC LIMIT 1) AS last_id
    '''), params).mappings().one()
    return row['scanned'], row['inserted'], row['last_on'], row['last_id']


def _count_diff(conn, left, right):
    return conn.execute(text(f'''
        SELECT count(*) FROM (
            SELECT slip_id, entered_on FROM {left}
            EXCEPT
            SELECT slip_id, entered_on FROM {right}
        ) diff
    ''')).scalar_one()


def _repair_batch(conn, migration):
    """月表有而日分区没有的行（绕过双写的迟到行）按批补拷。"""
    return conn.execute(text(f'''
        WITH missing AS (
            SELECT slip_id, entered_on FROM (
                SELECT slip_id, entered_on FROM {migration['monthly_table']}
                EXCEPT
                SELECT slip_id, entered_on FROM {migration['daily_table']}
            ) diff
            ORDER BY entered_on, slip_id
            LIMIT :batch
        ), ins AS (
            INSERT INTO {migration['daily_table']} (slip_id, entered_on, source, received_at)
            SELECT m.slip_id, m.entered_on, m.source, m.received_at
            FROM {migration['monthly_table']} AS m
            JOIN missing USING (slip_id, entered_on)
            ON CONFLICT (slip_id, entered_on) DO NOTHING
            RETURNING 1
        )
        SELECT count(*) FROM ins
    '''), {'batch': migration['batch_size']}).scalar_one()


def _advance_copy(conn, migration):
    """copying 状态推进一步。返回 True 表示有进展，worker 可立即继续。"""
    updates = {'updated_at': utc_time(), 'last_error': None}
    scanned, inserted, last_on, last_id = _copy_batch(conn, migration)
    if scanned:
        updates.update(checkpoint_on=last_on, checkpoint_id=last_id,
                       copied=migration['copied'] + inserted)
    else:
        # 检查点之后没有更多行：双向对账决定是否补齐、切换或暂停。
        missing = _count_diff(conn, migration['monthly_table'], migration['daily_table'])
        if missing:
            updates['copied'] = migration['copied'] + _repair_batch(conn, migration)
        else:
            extra = _count_diff(conn, migration['daily_table'], migration['monthly_table'])
            if extra:
                updates['last_error'] = f'日分区比月表多出 {extra} 张凭条，已暂停切换，请人工核对'
                conn.execute(ledger_migrations.update()
                             .where(ledger_migrations.c.id == migration['id']).values(**updates))
                return False
            updates['state'] = ALIGNED
    conn.execute(ledger_migrations.update()
                 .where(ledger_migrations.c.id == migration['id']).values(**updates))
    return True


def _cutover(conn, migration):
    """整月一次性切换：一条事务内 DETACH 月表、ATTACH 日分区树、旧表改名留档。"""
    monthly, daily = migration['monthly_table'], migration['daily_table']
    start = migration['month']
    counts = conn.execute(text(f'''
        SELECT (SELECT count(*) FROM {monthly}) AS monthly, (SELECT count(*) FROM {daily}) AS daily
    ''')).mappings().one()
    if counts['monthly'] != counts['daily']:
        raise RuntimeError(f"切换前对账失败：月表 {counts['monthly']} 张，日分区 {counts['daily']} 张")
    conn.execute(text(f'ALTER TABLE {PARENT} DETACH PARTITION {monthly}'))
    conn.execute(text(f'''
        ALTER TABLE {PARENT} ATTACH PARTITION {daily}
        FOR VALUES FROM (DATE '{start.isoformat()}') TO (DATE '{next_month(start).isoformat()}')
    '''))
    conn.execute(text(f'ALTER TABLE {monthly} RENAME TO {monthly}_archive'))
    now = utc_time()
    conn.execute(ledger_migrations.update().where(ledger_migrations.c.id == migration['id'])
                 .values(state=SWITCHED, switched_at=now, updated_at=now, last_error=None))
    conn.execute(ledger_months.update().where(ledger_months.c.month == start).values(mode=DAILY))
    _log(conn, 'worker', 'ledger_split_switch', f'{start:%Y-%m}')


def advance_active():
    """worker 每拍调用：把当前拆分任务推进一步。返回是否有进展。"""
    ensure_ledger_schema()
    migration_id = None
    try:
        with engine.begin() as conn:
            migration = conn.execute(
                select(ledger_migrations).where(ledger_migrations.c.state.in_(ACTIVE_STATES))
                .order_by(ledger_migrations.c.id).limit(1).with_for_update()).mappings().first()
            if not migration:
                return False
            migration_id = migration['id']
            if migration['state'] == COPYING:
                return _advance_copy(conn, dict(migration))
            _cutover(conn, dict(migration))
            return True
    except Exception as exc:
        logging.exception('Ledger migration step failed')
        if migration_id is not None:
            try:
                with engine.begin() as conn:
                    conn.execute(ledger_migrations.update()
                                 .where(ledger_migrations.c.id == migration_id)
                                 .values(last_error=str(exc)[:500], updated_at=utc_time()))
            except Exception:
                logging.exception('Failed to record migration error')
        return False


DEMO_MONTHS = ('2026-08-01', '2026-09-01', '2026-10-01', '2026-11-01', '2026-12-01')


def _demo_slips():
    """确定性演示凭条：11 月节庆高峰约千张，其他月份少量，7 月落入待核池。"""
    slips = []
    for day in range(1, 22):
        for index in range(1, 30 + (day * 17) % 45 + 1):
            source = '登记点补传' if index % 7 == 0 else '闸机补传'
            slips.append((f'SM-11{day:02d}-{index:02d}', date(2026, 11, day), source))
    for month in (8, 9, 10):
        for day in range(1, 11):
            slips.append((f'SM-{month:02d}{day:02d}-01', date(2026, month, day), '闸机补传'))
    for day in (29, 30, 31):
        slips.append((f'SM-07{day:02d}-01', date(2026, 7, day), '登记点补传'))
    return slips


def seed_ledger():
    """幂等播种在线账簿：在册月份、月分区与演示凭条。"""
    ensure_ledger_schema()
    created = 0
    with engine.begin() as conn:
        conn.execute(text('SELECT pg_advisory_xact_lock(:key)'), {'key': SCHEMA_LOCK})
        for raw in DEMO_MONTHS:
            start = date.fromisoformat(raw)
            if conn.execute(select(ledger_months).where(ledger_months.c.month == start)).first():
                continue
            _create_month_partition(conn, start)
            created += 1
        for slip_id, entered_on, source in _demo_slips():
            conn.execute(text(f'''
                INSERT INTO {PARENT} (slip_id, entered_on, source)
                VALUES (:slip_id, :entered_on, :source)
                ON CONFLICT (slip_id, entered_on) DO NOTHING
            '''), {'slip_id': slip_id, 'entered_on': entered_on, 'source': source})
    return {'months_created': created, 'demo_slips': len(_demo_slips())}
