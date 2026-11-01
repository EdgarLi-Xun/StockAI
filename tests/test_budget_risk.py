from decimal import Decimal

import pytest

from backend import db, instruments
from backend.budget import budget_check, transaction_fees
from backend.config import Fees, Settings

RULES={'lot_size':100,'quantity_step':100,'price_tick':'0.01','verified':True}
FEES=Fees(stock_transfer=0,slippage_bps=0)


def test_ac_f02_reject_999_plus_5():
    r=budget_check('9.99',RULES,'stock',FEES,1000,100)
    assert r['total']==1004 and not r['within_budget'] and r['max_quantity']==0


def test_ac_f02_accept_990_plus_5():
    r=budget_check('9.90',RULES,'stock',FEES,1000,100)
    assert r['total']==995 and r['within_budget'] and r['max_quantity']==100


@pytest.mark.parametrize('price,allowed', [('9.95',True),('9.96',False)])
def test_one_cent_boundary(price,allowed):
    assert budget_check(price,RULES,'stock',FEES,1000,100)['within_budget'] is allowed


@pytest.mark.parametrize('value',['NaN','Infinity','-Infinity',0,-1])
def test_invalid_price(value):
    with pytest.raises(ValueError):
        budget_check(value,RULES,'stock',FEES)


@pytest.mark.parametrize('qty',[1,99,101,150])
def test_lot_step_enforced(qty):
    with pytest.raises(ValueError):
        budget_check(5,RULES,'stock',FEES,quantity=qty)


def test_etf_does_not_inherit_stock_taxes():
    etf=transaction_fees(5,100,'etf','sell',Fees())
    assert etf['stamp_tax']==0 and etf['transfer']==0
    assert etf['stamp_applicable'] is False
    assert transaction_fees(5,100,'stock','sell',Fees())['stamp_tax']>0


def test_product_specific_lot_and_tick():
    rules={**RULES,'lot_size':200,'quantity_step':1,'price_tick':'0.001'}
    r=budget_check('1.123',rules,'etf',Fees(etf_minimum=0,etf_commission=0))
    assert r['quantity']>=200 and r['total']<=1000
    with pytest.raises(ValueError):
        budget_check('1.1234',rules,'etf',Fees())


@pytest.mark.parametrize('status',['unknown','suspended','delisted'])
def test_ac_f03_disallowed_status(valid_item,status):
    valid_item['status']=status
    assert instruments.risk_reasons(valid_item)


@pytest.mark.parametrize('flag',['st','warning','unknown'])
def test_ac_f03_announcement_not_assumed_safe(valid_item,flag):
    valid_item['risk_flag']=flag
    assert instruments.risk_reasons(valid_item)


def test_verified_instrument_passes(valid_item):
    assert not instruments.risk_reasons(valid_item)


def test_expired_evidence_blocks(valid_item):
    valid_item['verified_at']='2020-01-01T12:00:00+08:00'
    assert instruments.risk_reasons(valid_item)


def test_missing_official_evidence_blocks(valid_item):
    valid_item['evidence'].pop('announcement_url')
    assert instruments.risk_reasons(valid_item)


@pytest.mark.parametrize('url',['https://sse.com.cn.evil.example/a','http://www.sse.com.cn/a','https://user:pass@www.sse.com.cn/a','https://example.com'])
def test_official_domain_validation(url):
    assert not instruments.official_url(url)


def test_1000_hard_cap_even_for_direct_engine_calls():
    assert budget_check(5,RULES,'stock',FEES,budget=5000)['total']<=1000
    with pytest.raises(ValueError):
        Settings(budget=1001)


@pytest.mark.parametrize('url',['https://127.0.0.1:11434','http://remote.example:11434','http://user:password@127.0.0.1:11434','http://127.0.0.1:11434/other'])
def test_ollama_local_only(url):
    with pytest.raises(ValueError):
        Settings(ollama_url=url)
