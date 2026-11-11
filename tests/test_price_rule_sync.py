from copy import deepcopy

import pytest

from backend.cloud import market, simulation
from backend.instruments import InstrumentInput
from backend.price_rules import client_key
from backend.simulation import OrderInput
from backend.sync.business import Adapter
from test_cloud import cloud

build_cloud = cloud.__wrapped__


@pytest.fixture
def rule_peers(cloud, tmp_path, monkeypatch):
    store, _, _, _, _, uid = cloud
    target, _, _, _, _, other = build_cloud(tmp_path / 'peer', monkeypatch)
    monkeypatch.setattr(market, 'risk_reasons', lambda *a, **k: [])
    monkeypatch.setattr(simulation, 'fresh_reasons', lambda *a, **k: [])
    item = market.add_watch(store, uid, InstrumentInput(symbol='600001', market='SH'))
    account = simulation.create_account(store, uid, '同步价格规则', 10000)
    rule = simulation.create_rule(store, uid, {
        'account_id': account['id'], 'instrument_id': item['id'],
        'kind': 'close_at_or_below', 'trigger_price': '6', 'limit_price': '6',
        'quantity': 100, 'max_orders': 3,
    })
    queued = simulation.order(store, uid, OrderInput(
        client_key=client_key(rule['id'], '2026-11-10', 'buy'),
        account_id=account['id'], instrument_id=item['id'], side='buy', quantity=100, limit_price=6,
    ))
    simulation._rule_event(store, uid, rule, '2026-11-10', 'created', '已生成限价委托', queued['id'])
    source, destination = Adapter(store, uid, 'rule-space', 'a'), Adapter(target, other, 'rule-space', 'b')
    with store.tx(immediate=True) as c:
        gid = source.gid('sim_accounts', account['id'], c)
        payload = source.export('account', account['id'], c)
    return source, destination, gid, payload, rule, queued


def receive(destination, gid, payload):
    with destination.store.tx(immediate=True) as c:
        account_id = destination.project('account', gid, payload, c)
    rule = destination.store.one('SELECT * FROM sim_price_rules WHERE user_id=? AND account_id=?', (destination.uid, account_id))
    queued = destination.store.one('SELECT * FROM sim_orders WHERE user_id=? AND account_id=?', (destination.uid, account_id))
    return account_id, rule, queued


@pytest.mark.parametrize('has_event', [False, True])
def test_rule_orders_remap_ids_and_retries_remain_idempotent(rule_peers, has_event):
    source, dest, gid, payload, rule, original = rule_peers
    if not has_event:
        source.store.execute('DELETE FROM sim_price_rule_events WHERE user_id=? AND order_id=?', (source.uid, original['id']))
        with source.store.tx(immediate=True) as c:
            payload = source.export('account', original['account_id'], c)
    global_rule = payload['tables']['sim_price_rules'][0]['id']
    assert payload['tables']['sim_orders'][0]['client_key'] == client_key(global_rule, '2026-11-10', 'buy')
    account_id, received_rule, queued = receive(dest, gid, payload)
    assert received_rule['id'] != rule['id']
    assert queued['client_key'] == client_key(received_rule['id'], '2026-11-10', 'buy')
    retry = simulation.order(dest.store, dest.uid, OrderInput(
        client_key=queued['client_key'], account_id=account_id,
        instrument_id=queued['instrument_id'], side='buy', quantity=100, limit_price=6,
    ))
    assert retry['id'] == queued['id']
    assert dest.store.one('SELECT COUNT(*) n FROM sim_orders')['n'] == 1
    assert dest.store.one('SELECT order_id FROM sync_inert_orders WHERE user_id=?', (dest.uid,))['order_id'] == queued['id']
    with dest.store.tx(immediate=True) as c:
        assert dest.export('account', account_id, c) == payload
    simulation.stop_rule(dest.store, dest.uid, received_rule['id'], True)
    assert dest.store.one('SELECT status FROM sim_orders WHERE id=?', (queued['id'],))['status'] == 'cancelled'


def test_old_payload_rule_key_is_repaired_using_its_created_event(rule_peers):
    _, dest, gid, payload, original_rule, _ = rule_peers
    legacy = deepcopy(payload)
    legacy['tables']['sim_orders'][0]['client_key'] = client_key(original_rule['id'], '2026-11-10', 'buy')
    _, rule, queued = receive(dest, gid, legacy)
    assert queued['client_key'] == client_key(rule['id'], '2026-11-10', 'buy')
    simulation.stop_rule(dest.store, dest.uid, rule['id'], True)
    assert dest.store.one('SELECT status FROM sim_orders WHERE id=?', (queued['id'],))['status'] == 'cancelled'


def test_already_received_legacy_key_keeps_its_rule_association(rule_peers):
    _, dest, gid, payload, original_rule, _ = rule_peers
    account_id, rule, queued = receive(dest, gid, payload)
    wrong_key = 'sync:' + gid + ':' + client_key(original_rule['id'], '2026-11-10', 'buy')
    dest.store.execute('UPDATE sim_orders SET client_key=? WHERE id=?', (wrong_key, queued['id']))
    assert simulation.portfolio(dest.store, dest.uid)['orders'][0]['rule_id'] == rule['id']
    assert simulation._rule_state(dest.store, dest.uid, rule, '2026-11-11')[2] == {'buy'}
    with dest.store.tx(immediate=True) as c:
        repaired = dest.export('account', account_id, c)
    global_rule = repaired['tables']['sim_price_rules'][0]['id']
    assert repaired['tables']['sim_orders'][0]['client_key'] == client_key(global_rule, '2026-11-10', 'buy')
    simulation.stop_rule(dest.store, dest.uid, rule['id'], True)
    assert dest.store.one('SELECT status FROM sim_orders WHERE id=?', (queued['id'],))['status'] == 'cancelled'


def test_non_rule_order_keys_keep_the_existing_sync_namespace(rule_peers):
    _, dest, gid, payload, _, _ = rule_peers
    payload['tables']['sim_price_rule_events'] = []
    payload['tables']['sim_orders'][0]['client_key'] = 'manual-order-key'
    account_id, _, queued = receive(dest, gid, payload)
    assert queued['client_key'] == 'sync:' + gid + ':manual-order-key'
    with dest.store.tx(immediate=True) as c:
        assert dest.export('account', account_id, c)['tables']['sim_orders'][0]['client_key'] == 'manual-order-key'
