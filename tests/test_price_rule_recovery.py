from datetime import datetime

import pytest

from backend import collectors, db, simulation
from backend.cloud import market, simulation as cloud_sim
from backend.instruments import InstrumentInput
from test_cloud import cloud
from test_price_rules import _ready, _enable


@pytest.mark.parametrize('verified', [False, True])
def test_local_calendar_recovery_evaluates_the_previously_blocked_day(valid_item, bars, monkeypatch, verified):
    account = _ready(valid_item, bars, monkeypatch)
    day = bars[3]['ts'][:10]
    ident = _enable(valid_item, account, day + 'T10:00:00+08:00')
    monkeypatch.setattr(simulation, 'now', lambda: datetime.fromisoformat(day + 'T16:00:00+08:00'))
    ctx = {'calendar_verified': verified, 'last_completed_session': None, 'date': day}
    monkeypatch.setattr(collectors, 'calendar_context', lambda *a, **k: ctx)
    assert simulation.evaluate_rules() == []
    assert simulation.evaluate_rules() == []
    assert db.one('SELECT COUNT(*) n FROM price_rule_events')['n'] == 1
    ctx.update(calendar_verified=True, last_completed_session=day)
    assert simulation._rule_view(db.one('SELECT * FROM price_rules WHERE id=?', (ident,)), [day], day)['next_day'] == day
    assert len(simulation.evaluate_rules()) == 1
    assert simulation.evaluate_rules() == []
    assert db.one('SELECT result FROM price_rule_events')['result'] == 'created'
    assert db.one('SELECT success_count FROM price_rules WHERE id=?', (ident,))['success_count'] == 1


@pytest.mark.parametrize('verified', [False, True])
def test_cloud_calendar_recovery_evaluates_the_previously_blocked_day(cloud, bars, monkeypatch, verified):
    store, _, _, _, _, uid = cloud
    item = market.add_watch(store, uid, InstrumentInput(symbol='600001', market='SH'))
    market.import_bars(store, item['id'], {'bars': bars[:1]})
    monkeypatch.setattr(market, 'risk_reasons', lambda *a, **k: [])
    monkeypatch.setattr(cloud_sim, 'fresh_reasons', lambda *a, **k: [])
    account = cloud_sim.create_account(store, uid, '日历恢复', 10000)
    rule = cloud_sim.create_rule(store, uid, {'account_id': account['id'], 'instrument_id': item['id'], 'kind': 'close_at_or_below', 'trigger_price': '9', 'limit_price': '6', 'quantity': 100})
    day = bars[0]['ts'][:10]
    store.execute('UPDATE sim_price_rules SET armed_at=? WHERE id=?', (day + 'T10:00:00+08:00', rule['id']))
    monkeypatch.setattr(cloud_sim, 'now', lambda: datetime.fromisoformat(day + 'T16:00:00+08:00'))
    ctx = {'calendar_verified': verified, 'last_completed_session': None, 'date': day}
    monkeypatch.setattr(market, 'calendar', lambda *a, **k: ctx)
    assert cloud_sim.evaluate(store, uid) == []
    assert cloud_sim.evaluate(store, uid) == []
    assert store.one('SELECT COUNT(*) n FROM sim_price_rule_events')['n'] == 1
    ctx.update(calendar_verified=True, last_completed_session=day)
    saved = store.one('SELECT * FROM sim_price_rules WHERE id=?', (rule['id'],))
    assert cloud_sim._rule_view(store, uid, saved, [day], day)['next_day'] == day
    assert len(cloud_sim.evaluate(store, uid)) == 1
    assert cloud_sim.evaluate(store, uid) == []
    assert store.one('SELECT result FROM sim_price_rule_events')['result'] == 'created'
    assert store.one('SELECT success_count FROM sim_price_rules WHERE id=?', (rule['id'],))['success_count'] == 1
