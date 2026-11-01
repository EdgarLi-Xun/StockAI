import os
from datetime import datetime,timedelta

import pytest

os.environ['STOCKAI_DISABLE_SCHEDULER']='1'

from backend import db, config, maintenance, research, security
from backend.config import TZ


@pytest.fixture(autouse=True)
def isolated_data(tmp_path,monkeypatch):
    for module in (db,config,maintenance,research,security):
        monkeypatch.setattr(module,'DATA',tmp_path)
    db.initialize()
    security.TOKENS.clear()
    security.LOGIN_ATTEMPTS.clear()


@pytest.fixture
def valid_item():
    from backend import instruments
    item=instruments.get('SH:603126')
    return instruments.verify(item['id'],{
        'name':'测试证券','status':'active','risk_flag':'clear',
        'rules':{**item['rules'],'verified':True,'source_url':'https://www.sse.com.cn/lawandrules/'},
        'evidence':{'listing_url':'https://www.sse.com.cn/assortment/stock/list/share/','status_url':'https://www.sse.com.cn/disclosure/dealinstruc/suspension/','announcement_url':'https://www.cninfo.com.cn/new/disclosure','review_note':'仅为测试夹具，不写入生产数据'}
    })


def sample_bars(count=100):
    # Deterministic synthetic data is confined to the isolated test database.
    start=datetime(2025,1,2,15,tzinfo=TZ)
    out=[]
    day=start
    for i in range(count):
        while day.weekday()>4:
            day+=timedelta(days=1)
        price=round(5+(i%40)*0.01 if (i//40)%2==0 else 5.39-(i%40)*0.01,2)
        out.append({'ts':day.isoformat(),'open':price,'high':round(price+0.05,2),'low':round(price-0.05,2),'close':price,'volume':1000000,'amount':price*1000000,'adjustment':'none','source':'test-fixture','source_url':'https://www.sse.com.cn/test-fixture','fetched_at':day.isoformat(),'quality':'valid','metadata':{'historical_status_verified':True,'corporate_actions_verified':True,'status_source_url':'https://www.sse.com.cn/test-status','corporate_actions_source_url':'https://www.sse.com.cn/test-actions','trading_status':'active','risk_flag':'clear','upper_limit':round(price*1.1,2),'lower_limit':round(price*0.9,2)}})
        day+=timedelta(days=1)
    return out


@pytest.fixture
def bars():
    return sample_bars()


@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backend.app import app
    with TestClient(app) as c:
        token=c.get('/api/session').json()['token']
        c.headers['Authorization']='Bearer '+token
        yield c
