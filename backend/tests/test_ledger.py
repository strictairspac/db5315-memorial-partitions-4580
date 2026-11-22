"""在线账簿月表拆日分区的验收测试。

覆盖口径：
- 闸机补传幂等入账（已回执重传不丢不重），回执与查询展示真实物理分区；
- 拆分期间补传双写，查询仍走月表；
- 检查点续拷：中断（含批次中途失败）恢复后不丢不重；
- 对齐前不切换，绕过双写的迟到行先补拷，日分区多出行暂停切换；
- 对齐后整月一次性切换，其他月份与待核池不受影响。
"""

import pytest
from fastapi import HTTPException
from sqlalchemy import text

from datetime import date

from src.config.settings import engine
from src.models.tables import ledger_migrations, ledger_months
from src.services import ledger

NOV = '2026-11-01'
OCT = '2026-10-01'


def make_slips(month, day_count, per_day=1, prefix=None):
    slips = []
    for day in range(1, day_count + 1):
        for index in range(1, per_day + 1):
            slips.append({'slip_id': f'{prefix or "SM"}-{month[5:7]}{day:02d}-{index:02d}',
                          'entered_on': f'{month[:4]}-{month[5:7]}-{day:02d}'})
    return slips


def count(table):
    with engine.connect() as conn:
        return conn.execute(text(f'SELECT count(*) FROM {table}')).scalar_one()


def distinct_slips():
    with engine.connect() as conn:
        return conn.execute(text('SELECT count(DISTINCT (slip_id, entered_on)) FROM ledger')).scalar_one()


def migration_row():
    with engine.connect() as conn:
        rows = conn.execute(ledger_migrations.select().order_by(ledger_migrations.c.id.desc())).mappings().all()
    return dict(rows[0]) if rows else None


def month_mode(month):
    with engine.connect() as conn:
        return conn.execute(ledger_months.select()
                            .where(ledger_months.c.month == date.fromisoformat(month))).mappings().one()['mode']


def set_batch_size(size):
    with engine.begin() as conn:
        conn.execute(ledger_migrations.update().values(batch_size=size))


def run_to_completion(limit=200):
    steps = 0
    while ledger.advance_active():
        steps += 1
        assert steps < limit, '拆分未能在限定步数内完成'
    return steps


def partitions_in(range_from, range_to):
    return {row['slip_id']: row['partition']
            for row in ledger.query_slips(range_from, range_to)['rows']}


# ---------- 入账与查询 ----------

def test_ingest_dedup_and_partition_visibility(db):
    ledger.register_month(NOV, 'tester')
    acks = ledger.ingest([
        {'slip_id': 'SM-1105-01', 'entered_on': '2026-11-05'},
        {'slip_id': 'SM-1105-01', 'entered_on': '2026-11-05'},   # 闸机重传已回执凭条
        {'slip_id': 'SM-0731-01', 'entered_on': '2026-07-31'},   # 未登记月份 -> 待核池
    ], 'tester')
    assert [ack['result'] for ack in acks] == ['inserted', 'duplicate', 'inserted']
    assert [ack['partition'] for ack in acks] == ['ledger_202611', 'ledger_202611', 'ledger_default']
    assert count('ledger') == 2
    rows = ledger.query_slips('2026-07-01', '2026-11-30')['rows']
    assert {row['slip_id']: row['partition'] for row in rows} == {
        'SM-1105-01': 'ledger_202611', 'SM-0731-01': 'ledger_default'}


def test_ingest_validation(db):
    ledger.register_month(NOV, 'tester')
    with pytest.raises(HTTPException) as exc:
        ledger.ingest([], 'tester')
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        ledger.ingest([{'slip_id': ' SM-1', 'entered_on': '2026-11-05'}], 'tester')
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        ledger.ingest([{'slip_id': 'SM-1', 'entered_on': '2026-11-5'}], 'tester')
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        ledger.ingest([{'slip_id': f'SM-{i}', 'entered_on': '2026-11-05'} for i in range(2001)], 'tester')
    assert exc.value.status_code == 422


def test_query_validation_and_limit(db):
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 5), 'tester')
    with pytest.raises(HTTPException):
        ledger.query_slips('2026-11-10', '2026-11-01')
    with pytest.raises(HTTPException):
        ledger.query_slips('2026-01-01', '2027-12-31')
    result = ledger.query_slips('2026-11-01', '2026-11-30', limit=2)
    assert result['truncated'] is True and len(result['rows']) == 2
    result = ledger.query_slips('2026-11-01', '2026-11-30')
    assert result['truncated'] is False and len(result['rows']) == 5


# ---------- 拆分任务 ----------

def test_start_migration_validation(db):
    with pytest.raises(HTTPException) as exc:
        ledger.start_migration('2026-11-15', 'tester')
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        ledger.start_migration(NOV, 'tester')
    assert exc.value.status_code == 404
    ledger.register_month(NOV, 'tester')
    ledger.register_month(OCT, 'tester')
    ledger.start_migration(NOV, 'tester')
    assert month_mode('2026-11-01') == ledger.MIGRATING
    with pytest.raises(HTTPException) as exc:
        ledger.start_migration(NOV, 'tester')
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        ledger.start_migration(OCT, 'tester')   # 已有任务进行中
    assert exc.value.status_code == 409
    with pytest.raises(HTTPException) as exc:
        ledger.register_month(NOV, 'tester')
    assert exc.value.status_code == 409


def test_split_full_cycle_atomic_switch(db):
    ledger.register_month(OCT, 'tester')
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 23), 'tester')
    ledger.ingest(make_slips('2026-10', 4), 'tester')
    ledger.ingest([{'slip_id': 'SM-0731-01', 'entered_on': '2026-07-31'}], 'tester')
    before_total = count('ledger')

    migration = ledger.start_migration(NOV, 'tester')
    assert migration['state'] == ledger.COPYING
    set_batch_size(7)

    # 追齐前：查询仍走月表
    assert set(partitions_in('2026-11-01', '2026-11-30').values()) == {'ledger_202611'}

    run_to_completion()

    row = migration_row()
    assert row['state'] == ledger.SWITCHED and row['switched_at']
    assert month_mode('2026-11-01') == ledger.DAILY
    assert month_mode('2026-10-01') == ledger.MONTHLY

    # 不丢不重：总数、逐张唯一性守恒
    assert count('ledger') == before_total == distinct_slips()
    assert count('ledger_202611_daily') == 23
    assert count('ledger_202611_archive') == 23   # 旧月表留档
    assert count('ledger_202610') == 4            # 其他月份照常
    assert count('ledger_default') == 1

    # 整月一次性切换：11 月全部落在日叶子，10 月仍在月表
    places = partitions_in('2026-10-01', '2026-11-30')
    assert all(name.startswith('ledger_202611_daily_') for slip, name in places.items() if slip.startswith('SM-11'))
    assert places['SM-1109-01'] == 'ledger_202611_daily_09'
    assert {name for slip, name in places.items() if slip.startswith('SM-10')} == {'ledger_202610'}

    # 切换后新补传直接落入日叶子
    ack = ledger.ingest([{'slip_id': 'SM-1122-99', 'entered_on': '2026-11-22'}], 'tester')[0]
    assert ack['result'] == 'inserted' and ack['partition'] == 'ledger_202611_daily_22'

    # 归档表已脱离分区树
    with engine.connect() as conn:
        attached = conn.execute(text('''
            SELECT c.relname FROM pg_inherits e
            JOIN pg_class p ON p.oid = e.inhparent
            JOIN pg_class c ON c.oid = e.inhrelid
            WHERE p.relname = 'ledger'
        ''')).scalars().all()
    assert 'ledger_202611_archive' not in attached
    assert 'ledger_202611_daily' in attached


def test_dual_write_and_resume_after_interruption(db):
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 30), 'tester')
    ledger.start_migration(NOV, 'tester')
    set_batch_size(10)

    assert ledger.advance_active() is True
    assert migration_row()['copied'] == 10

    # 拆分期间闸机补传照常入账：新凭条双写
    ack = ledger.ingest([{'slip_id': 'SM-1199-01', 'entered_on': '2026-11-15'}], 'tester')[0]
    assert ack['result'] == 'inserted'
    assert count('ledger_202611') == 31
    assert count('ledger_202611_daily') == 11

    # 已回执凭条重传：不重复
    ack = ledger.ingest([{'slip_id': 'SM-1199-01', 'entered_on': '2026-11-15'}], 'tester')[0]
    assert ack['result'] == 'duplicate'
    assert count('ledger_202611_daily') == 11

    # 中断（worker 停止推进），数据库中的检查点持久保留
    frozen = migration_row()
    assert frozen['state'] == ledger.COPYING and frozen['checkpoint_id']

    # 恢复后续跑至切换
    run_to_completion()
    assert migration_row()['state'] == ledger.SWITCHED
    assert count('ledger_202611_daily') == 31 == count('ledger_202611_archive')
    assert distinct_slips() == 31
    assert set(partitions_in('2026-11-01', '2026-11-30').values()) <= {
        f'ledger_202611_daily_{day:02d}' for day in range(1, 31)}


def test_mid_batch_failure_rolls_back_and_resumes(db, monkeypatch):
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 25), 'tester')
    ledger.start_migration(NOV, 'tester')
    set_batch_size(10)

    original = ledger._copy_batch
    def boom(conn, migration):
        raise RuntimeError('模拟批次中途崩溃')
    monkeypatch.setattr(ledger, '_copy_batch', boom)

    assert ledger.advance_active() is False
    row = migration_row()
    assert row['state'] == ledger.COPYING
    assert row['checkpoint_id'] is None and row['copied'] == 0   # 未提交，检查点未前进
    assert '模拟批次中途崩溃' in row['last_error']
    assert count('ledger_202611_daily') == 0

    monkeypatch.setattr(ledger, '_copy_batch', original)
    run_to_completion()
    assert migration_row()['state'] == ledger.SWITCHED
    assert count('ledger_202611_daily') == 25 == distinct_slips()


def test_recopy_is_idempotent(db):
    """同一批被重复拷贝（崩溃重放的极端情况）不会产生重复凭条。"""
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 8), 'tester')
    ledger.start_migration(NOV, 'tester')
    set_batch_size(5)
    assert ledger.advance_active() is True
    first = migration_row()
    assert first['copied'] == 5 and first['checkpoint_id']
    # 把检查点回拨到起点，模拟上一批提交后崩溃、状态回滚的恢复方
    with engine.begin() as conn:
        conn.execute(ledger_migrations.update().values(checkpoint_on=None, checkpoint_id=None))
    assert ledger.advance_active() is True
    run_to_completion()
    assert count('ledger_202611_daily') == 8 == distinct_slips()


def test_cutover_waits_for_alignment_and_repairs(db):
    """绕过双写直写月表的迟到行：对齐前不切换，先补拷再整月切换。"""
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 12), 'tester')
    ledger.start_migration(NOV, 'tester')
    set_batch_size(10)
    assert ledger.advance_active() is True   # 拷前 10 张
    # 直写月表（绕过 API 与双写），键在检查点之前，游标扫描看不到
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO ledger_202611 (slip_id, entered_on, source)
            VALUES ('SM-1101-99', DATE '2026-11-01', '人工补录')
        '''))
    assert ledger.advance_active() is True   # 拷剩余 2 张
    assert ledger.advance_active() is True   # 扫描耗尽 -> 对账发现缺 1 张 -> 补拷
    assert count('ledger_202611_daily') == 13
    row = migration_row()
    assert row['state'] in (ledger.COPYING, ledger.ALIGNED)
    run_to_completion()
    assert migration_row()['state'] == ledger.SWITCHED
    assert count('ledger_202611_daily') == 13 == distinct_slips()
    assert partitions_in('2026-11-01', '2026-11-01')['SM-1101-99'] == 'ledger_202611_daily_01'


def test_extra_rows_in_daily_pause_cutover(db):
    """日分区多出月表没有的凭条属于异常：暂停切换并报告。"""
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 5), 'tester')
    ledger.start_migration(NOV, 'tester')
    with engine.begin() as conn:
        conn.execute(text('''
            INSERT INTO ledger_202611_daily (slip_id, entered_on, source)
            VALUES ('SM-1199-99', DATE '2026-11-19', '异常来源')
        '''))
    ledger.advance_active()   # 拷完 5 张
    assert ledger.advance_active() is False   # 对账发现多出行，暂停
    row = migration_row()
    assert row['state'] == ledger.COPYING and '多出' in row['last_error']
    assert month_mode('2026-11-01') == ledger.MIGRATING
    # 人工清理异常行后可继续完成
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM ledger_202611_daily WHERE slip_id = 'SM-1199-99'"))
    run_to_completion()
    assert migration_row()['state'] == ledger.SWITCHED


def test_seed_ledger_idempotent(db):
    first = ledger.seed_ledger()
    assert first['months_created'] == 5
    expected = len(ledger._demo_slips())
    assert count('ledger') == expected == distinct_slips()
    second = ledger.seed_ledger()   # 后端每次启动都会播种，必须幂等
    assert second['months_created'] == 0
    assert count('ledger') == expected
    view = ledger.overview()
    assert {row['label'] for row in view['months']} == {'2026-08', '2026-09', '2026-10', '2026-11', '2026-12'}
    assert view['default_count'] == 3   # 7 月未登记，凭条落入待核池


def test_overview_reflects_states(db):
    ledger.register_month(OCT, 'tester')
    ledger.register_month(NOV, 'tester')
    ledger.ingest(make_slips('2026-11', 3), 'tester')
    view = ledger.overview()
    by_label = {row['label']: row for row in view['months']}
    assert by_label['2026-11']['mode'] == 'monthly' and by_label['2026-11']['count'] == 3
    assert view['migration'] is None
    ledger.start_migration(NOV, 'tester')
    view = ledger.overview()
    assert view['migration']['state'] == 'copying'
    assert view['migration']['monthly_count'] == 3 and view['migration']['daily_count'] == 0
    run_to_completion()
    view = ledger.overview()
    assert view['migration'] is None
    by_label = {row['label']: row for row in view['months']}
    assert by_label['2026-11']['mode'] == 'daily'
    assert by_label['2026-11']['table'] == 'ledger_202611_daily'
    assert by_label['2026-10']['mode'] == 'monthly'
