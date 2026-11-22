"""API 级端到端：登录、登记月份、闸机补传、拆分切换、按日期查账。"""

import pytest
from fastapi.testclient import TestClient

from src.main import app
from src.services import ledger

MAR = {'month': '2027-03-01'}


@pytest.fixture()
def client(db):
    with TestClient(app) as test_client:
        yield test_client


def login(client, username):
    response = client.post('/login', json={'username': username, 'password': 'Demo-Walkthrough-2026'})
    assert response.status_code == 200, response.text
    return {'Authorization': f"Bearer {response.json()['token']}"}


def test_ledger_api_end_to_end(client):
    operator = login(client, 'operator')
    viewer = login(client, 'viewer')

    overview = client.get('/ledger/overview', headers=operator).json()
    assert any(row['label'] == '2026-11' for row in overview['months'])   # 演示账簿已播种
    assert overview['migration'] is None

    # 观察员只读：可以查账，不能入账、登记或拆分
    assert client.post('/ledger/ingest', headers=viewer,
                       json={'records': [{'slip_id': 'X-1', 'entered_on': '2027-03-05'}]}).status_code == 403
    assert client.post('/ledger/months', headers=viewer, json=MAR).status_code == 403
    assert client.post('/ledger/migrations', headers=viewer, json=MAR).status_code == 403

    created = client.post('/ledger/months', headers=operator, json=MAR)
    assert created.status_code == 201, created.text
    assert created.json()['table'] == 'ledger_202703'

    acks = client.post('/ledger/ingest', headers=operator, json={'records': [
        {'slip_id': 'SM-2703-01', 'entered_on': '2027-03-05'},
        {'slip_id': 'SM-2703-02', 'entered_on': '2027-03-06'},
        {'slip_id': 'SM-2703-01', 'entered_on': '2027-03-05'},   # 重传已回执凭条
    ]}).json()['acks']
    assert [ack['result'] for ack in acks] == ['inserted', 'inserted', 'duplicate']
    assert acks[0]['partition'] == 'ledger_202703'

    started = client.post('/ledger/migrations', headers=operator, json=MAR)
    assert started.status_code == 202, started.text
    assert started.json()['state'] == 'copying'

    # 拆分期间补传照常入账（双写），查账仍显示月表
    ack = client.post('/ledger/ingest', headers=operator,
                      json={'records': [{'slip_id': 'SM-2703-03', 'entered_on': '2027-03-07'}]}).json()['acks'][0]
    assert ack['result'] == 'inserted' and ack['partition'] == 'ledger_202703'

    while ledger.advance_active():
        pass

    rows = client.get('/ledger/slips?date_from=2027-03-01&date_to=2027-03-31', headers=viewer).json()['rows']
    assert {row['slip_id']: row['partition'] for row in rows} == {
        'SM-2703-01': 'ledger_202703_daily_05',
        'SM-2703-02': 'ledger_202703_daily_06',
        'SM-2703-03': 'ledger_202703_daily_07',
    }
    overview = client.get('/ledger/overview', headers=viewer).json()
    march = next(row for row in overview['months'] if row['label'] == '2027-03')
    assert march['mode'] == 'daily' and march['count'] == 3


def test_ledger_api_requires_login(client):
    assert client.get('/ledger/overview').status_code == 401
    assert client.get('/ledger/slips?date_from=2027-03-01&date_to=2027-03-02').status_code == 401
