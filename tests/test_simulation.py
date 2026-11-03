# 验证虚拟资金、持仓、委托、费用和流水在时序及预算约束下保持一致。
import pytest

from backend import collectors, db, simulation
from backend.config import Fees


# 准备明确费用、已核验证券、行情及虚拟账户的撮合环境。
@pytest.fixture
def environment(valid_item,bars,monkeypatch):
    cfg=db.settings(); cfg.fees=Fees(stock_transfer=0,slippage_bps=0); db.save_settings(cfg)
    collectors.save_bars(valid_item['id'],bars[:4])
    monkeypatch.setattr(collectors,'fresh_reasons',lambda *a:[])
    account=simulation.create_account('测试虚拟账户',10000)
    return valid_item,bars,account


# 构造固定数量和方向相关限价的模拟委托输入。
def make_order(item,account,side='buy',key='unique-test-order-1'):
    return simulation.OrderInput(client_key=key,account_id=account['id'],instrument_id=item['id'],side=side,quantity=100,limit_price=6 if side=='buy' else 4)


# 验证成交前后的现金、数量、费用和流水逐项一致。
def test_ac_f08_cash_position_fee_ledger_consistency(environment):
    item,bars,account=environment
    order=simulation.order(make_order(item,account))
    db.execute('UPDATE orders SET created_at=? WHERE id=?',(bars[0]['ts'],order['id']))
    fills=simulation.match_orders()
    assert len(fills)==1
    fill=db.one('SELECT * FROM orders WHERE id=?',(order['id'],))
    assert fill['status']=='filled' and fill['source_ts']==bars[1]['ts']
    buy_cost=100*bars[1]['open']+5
    assert float(db.one('SELECT cash FROM accounts WHERE id=?',(account['id'],))['cash'])==pytest.approx(10000-buy_cost)
    assert db.one('SELECT SUM(quantity) n FROM lots')['n']==100
    simulation.match_orders()
    assert db.one('SELECT COUNT(*) n FROM ledger WHERE order_id IS NOT NULL')['n']==1
    sell=simulation.order(make_order(item,account,'sell','unique-test-sell'))
    db.execute('UPDATE orders SET created_at=? WHERE id=?',(bars[1]['ts'],sell['id']))
    simulation.match_orders()
    assert db.one('SELECT SUM(quantity) n FROM lots')['n']==0
    cash=float(db.one('SELECT cash FROM accounts WHERE id=?',(account['id'],))['cash'])
    expected=10000-buy_cost+100*bars[2]['open']-5-round(100*bars[2]['open']*0.0005,2)
    assert cash==pytest.approx(expected)


# 验证盘中创建的委托不能使用已经过去的当日开盘成交。
def test_midday_order_cannot_use_same_day_open(environment):
    item,bars,account=environment
    order=simulation.order(make_order(item,account))
    created=bars[1]['ts'].replace('15:00:00','10:00:00')
    db.execute('UPDATE orders SET created_at=? WHERE id=?',(created,order['id']))
    simulation.match_orders()
    assert db.one('SELECT source_ts FROM orders WHERE id=?',(order['id'],))['source_ts']==bars[2]['ts']


# 验证同一幂等键重复提交不会重复预占现金。
def test_order_idempotent_same_cash_reservation(environment):
    item,bars,account=environment
    payload=make_order(item,account)
    first=simulation.order(payload)
    second=simulation.order(payload)
    assert first['id']==second['id']
    assert db.one('SELECT COUNT(*) n FROM orders')['n']==1


# 验证多笔待成交委托不能绕过同一证券的预算限制。
def test_multiple_orders_cannot_bypass_per_symbol_budget(environment):
    item,bars,account=environment
    simulation.order(make_order(item,account))
    with pytest.raises(ValueError,match='同一证券'):
        simulation.order(make_order(item,account,key='second-distinct-order'))


# 验证 ETF 限价按产品规定的千分位报价单位校验。
def test_etf_limit_uses_mill_tick(environment):
    item,bars,account=environment
    rules={**item['rules'],'price_tick':'0.001'}
    db.execute("UPDATE instruments SET asset_class='etf',rules_json=? WHERE id=?",(db.dumps(rules),item['id']))
    changed=[dict(b) for b in bars[:2]]
    changed[0].update(open=1.008,high=1.02,low=1.0,close=1.008)
    changed[1].update(open=1.109,high=1.12,low=1.10,close=1.109)
    collectors.save_bars(item['id'],changed)
    payload=make_order(item,account).model_copy(update={'limit_price':1.11})
    order=simulation.order(payload)
    db.execute('UPDATE orders SET created_at=? WHERE id=?',(changed[0]['ts'],order['id']))
    assert simulation.match_orders()==[]
    assert db.one('SELECT status FROM orders WHERE id=?',(order['id'],))['status']=='pending'


# 验证证券交易状态未知时拒绝创建买入委托。
def test_buy_with_unknown_status_rejected(bars,monkeypatch):
    account=simulation.create_account('虚拟',10000)
    with pytest.raises(ValueError,match='交易状态'):
        simulation.order(simulation.OrderInput(client_key='test-unknown',account_id=account['id'],instrument_id='SH:603126',side='buy',quantity=100,limit_price=5))


# 验证过期行情不能用于创建新买入委托。
def test_stale_data_cannot_create_buy(valid_item,bars):
    collectors.save_bars(valid_item['id'],bars)
    account=simulation.create_account('虚拟',10000)
    with pytest.raises(ValueError):
        simulation.order(make_order(valid_item,account))


# 验证创建账户必须提供明确且有限的正数虚拟本金。
def test_explicit_initial_capital_required():
    for cash in (0,-100,'NaN'):
        with pytest.raises(ValueError):
            simulation.create_account('虚拟',cash)
