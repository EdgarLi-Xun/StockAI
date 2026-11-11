import pytest

from backend import db, simulation


@pytest.mark.parametrize('changes', [
    {'fast': None}, {'slow': []}, {'stop_loss_pct': None},
    {'fast': 5.9}, {'max_orders': []}, {'max_orders': True},
    {'kind': 'close_at_or_below', 'trigger_price': '6', 'limit_price': '6', 'quantity': 100.9},
])
def test_invalid_rule_numbers_are_rejected_without_creating_rules(client, valid_item, changes):
    account = simulation.create_account('参数校验', 10000)
    payload = {'account_id': account['id'], 'instrument_id': valid_item['id'], 'kind': 'ma_forward',
               'fast': 5, 'slow': 20, 'stop_loss_pct': .05, **changes}
    response = client.post('/api/simulation/rules', json=payload)
    assert response.status_code == 400
    assert db.one('SELECT COUNT(*) n FROM price_rules')['n'] == 0
