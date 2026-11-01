import copy

import pytest

from backend import backtest, collectors
from backend.config import Fees


def params(**kwargs):
    return backtest.Parameters(instrument_id='SH:603126',initial_cash=10000,**kwargs)


def test_ac_f09_known_data_next_open_and_costs(valid_item,bars):
    fees=Fees(stock_transfer=0,slippage_bps=0)
    r=backtest.evaluate(bars,params(),valid_item,fees,1000)
    assert r['validated'] and r['full']['trades']
    assert r['out_of_sample']['equity'][0]['ts']==bars[70]['ts']
    for t in r['full']['trades']:
        assert t['signal_ts']<t['ts']
        assert t['quantity']%100==0
        if t['side']=='buy':
            assert t['price']*t['quantity']+t['fees']<=1000
    assert r['full']['metrics']['final_equity']==pytest.approx(r['full']['equity'][-1]['equity'])
    assert len(r['cost_sensitivity'])==3


def test_future_prices_do_not_change_past_fills(valid_item,bars):
    fees=Fees(slippage_bps=0)
    first=backtest.run_engine(bars,params(),valid_item,fees)
    changed=copy.deepcopy(bars)
    for b in changed[60:]:
        b.update(open=8,close=8,high=8.5,low=7.5)
    second=backtest.run_engine(changed,params(),valid_item,fees)
    cutoff=bars[60]['ts']
    assert [t for t in first['trades'] if t['ts']<cutoff]==[t for t in second['trades'] if t['ts']<cutoff]


def test_limit_up_blocks_entry(valid_item,bars):
    data=copy.deepcopy(bars)
    for b in data:
        b['metadata']['upper_limit']=b['open']
    r=backtest.run_engine(data,params(),valid_item,Fees(slippage_bps=0))
    assert not r['trades'] and r['rejected']


def test_suspended_zero_volume_never_fills(valid_item,bars):
    data=copy.deepcopy(bars)
    for b in data:
        b['volume']=0
    assert not backtest.run_engine(data,params(),valid_item,Fees())['trades']


def test_strict_rejects_missing_evidence(valid_item,bars):
    bars[5]['metadata']={}
    with pytest.raises(ValueError,match='可靠性门槛'):
        backtest.evaluate(bars,params(),valid_item,Fees(),1000)
    assert not backtest.evaluate(bars,params(strict=False),valid_item,Fees(),1000)['validated']


def test_adjusted_price_cannot_be_execution_price(valid_item,bars):
    bars[0]['adjustment']='qfq'
    with pytest.raises(ValueError,match='不复权'):
        backtest.evaluate(bars,params(),valid_item,Fees(),1000)


def test_snapshot_reproduce_identical(valid_item,bars):
    collectors.save_bars(valid_item['id'],bars)
    saved=backtest.create(params())
    bars[-1]['close']=1
    assert backtest.reproduce(saved['id'])['identical']


def test_t_plus_one_never_same_day_exit(valid_item,bars):
    r=backtest.run_engine(bars,params(fast=2,slow=3),valid_item,Fees(slippage_bps=0))
    last_entry=None
    for t in r['trades']:
        if t['side']=='buy':
            last_entry=t['ts'][:10]
        else:
            assert t['ts'][:10]>last_entry
