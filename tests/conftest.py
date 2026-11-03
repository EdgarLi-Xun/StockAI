# 构建隔离的测试数据库、官方核验证据和确定性行情，避免依赖真实账户或网络。
import os
from datetime import datetime,timedelta

import pytest

os.environ['STOCKAI_DISABLE_SCHEDULER']='1'

from backend import db, config, maintenance, research, security
from backend.config import TZ


# 为每个测试切换到临时数据目录并重置数据库与任务状态。
@pytest.fixture(autouse=True)
def isolated_data(tmp_path,monkeypatch):
    for module in (db,config,maintenance,research,security):
        monkeypatch.setattr(module,'DATA',tmp_path)
    db.initialize()
    security.TOKENS.clear()
    security.LOGIN_ATTEMPTS.clear()


# 创建带有效官方证据和产品规则的测试证券。
@pytest.fixture
def valid_item():
    from backend import instruments
    item=instruments.get('SH:603126')
    return instruments.verify(item['id'],{
        'name':'测试证券','status':'active','risk_flag':'clear',
        'rules':{**item['rules'],'verified':True,'source_url':'https://www.sse.com.cn/lawandrules/'},
        'evidence':{'listing_url':'https://www.sse.com.cn/assortment/stock/list/share/','status_url':'https://www.sse.com.cn/disclosure/dealinstruc/suspension/','announcement_url':'https://www.cninfo.com.cn/new/disclosure','review_note':'仅为测试夹具，不写入生产数据'}
    })


# 按固定算法构造带历史状态、公司行动和来源证据的日线样本。
def sample_bars(count=100):
    # 确定性合成行情仅用于隔离测试数据库，不进入正式研究数据。
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


# 提供可独立修改的标准行情样本。
@pytest.fixture
def bars():
    return sample_bars()


# 启动本地测试应用并配置可信会话，供接口用例复用。
@pytest.fixture
def client():
    from fastapi.testclient import TestClient
    from backend.app import app
    with TestClient(app) as c:
        token=c.get('/api/session').json()['token']
        c.headers['Authorization']='Bearer '+token
        yield c
