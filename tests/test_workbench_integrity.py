# 验证研究工作台的历史事实、授权裁剪、删除恢复、真实加密备份和实验基准完整性。
import json
import shutil
import subprocess
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime,timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from conftest import sample_bars
from test_cloud import cloud,profile,run_job,login,PASSWORD
from test_workbench import research_data
from backend.config import ROOT,TZ
from backend.cloud import alerts,assistant,backups,experiments,jobs,market,models,research,resources,simulation,workspace
from backend.cloud.auth import AuthError
from backend.cloud.lifecycle import purge_user
from backend.cloud.research_control import Control,control,summary
from backend.cloud.store import json_text,stamp
from backend.public_app import create_app


# 验证同一事实的解释版本可比较，预算自由文本不会随引用进入模型上下文。
def test_same_facts_two_reports_safe_budget_and_deleted_context(cloud):
    store,keys,_,_,admin,uid=cloud;profile(cloud)
    first=run_job(cloud,transport=lambda *a:('{"summary":"已核对","market_context":"有限资料","candidates":[],"risk_notes":[],"missing_data":[],"source_ids":[]}',{},''))
    second=run_job(cloud,kind='supplement',parent_id=first['report_id'],transport=lambda *a:('{"summary":"另一种解释","market_context":"有限资料","candidates":[],"risk_notes":[],"missing_data":[],"source_ids":[]}',{},''))
    fixed=models.snapshot(store,uid);contexts=[{'kind':'report','id':r['report_id']} for r in (first,second)]
    refs,_=assistant.context_refs(store,uid,contexts,fixed)
    assert refs[-1]['facts']['kind']=='explanation_change'
    assert refs[0]['data_time'] and refs[1]['data_time'] and refs[0]['version']!=refs[1]['version']
    with pytest.raises(AuthError):assistant.context_refs(store,admin,contexts,fixed)
    assert assistant.classify('和上次报告比较一下',[])=='project'
    fake={'snapshot_id':'a','candidates':[{'instrument_id':'SH:1','budget':{'total':1000,'fee_note':'SECRET-NOTE','fees':{'total':5}}}]}
    fake2=json.loads(json_text(fake));fake2['candidates'][0]['budget']['total']=900
    assert 'SECRET-NOTE' not in json_text(assistant.compare_reports(fake,fake2))
    assert assistant.safe_budget(fake['candidates'][0]['budget'])=={'total':1000,'fees':{'total':5}}


# 验证历史上下文遵守最新授权，模型任务仍绑定提交时版本并正确处理撤权。
def test_history_consent_projection_deletion_and_profile_freeze(cloud):
    store,keys,_,_,_,uid=cloud;p=profile(cloud,allow_simulation_summary=True,allow_trade_details=True)
    account=simulation.create_account(store,uid,'PRIVATE-NAME',10000);session=resources.save(store,uid,'chat_session',{'title':'授权'})
    row=assistant.submit(store,keys,uid,session['id'],assistant.MessageInput(client_key='consent-request',question='解释这个账户的风险',contexts=[{'kind':'account','id':account['id']}]))
    # 核对账户名称不外发，并返回引用本人账户事实卡的受控回答。
    def transport(fixed,messages,key):
        raw=json_text(messages);assert 'PRIVATE-NAME' not in raw and 'fills' in raw
        return json_text({'answer':'当前仅有虚拟初始资金，应结合来源和估值缺口理解。','citations':['account:'+account['id']]}),{},''
    result=jobs.run(store,keys,jobs.claim(store,'account-chat'),transport);assert result.get('status')=='success',result
    assert assistant.history(store,uid,session['id'],models.snapshot(store,uid))[0]
    models.save_profile(store,keys,uid,models.ProfileInput(name='收窄授权',provider='minimax',allow_simulation_summary=False,allow_trade_details=False),p['id'])
    hist,omitted=assistant.history(store,uid,session['id'],models.snapshot(store,uid));assert not hist and omitted==1
    with pytest.raises(models.ModelError):models.check_live(store,uid,json.loads(store.one('SELECT snapshot_json FROM personal_jobs WHERE id=?',(row['job_id'],))['snapshot_json'])['profile'])
    assert assistant.request_detail(store,uid,row['id'])['reply']['answer']


# 验证逐笔成交与资金持仓汇总授权可以独立授予。
def test_trades_permission_is_independent_of_summary(cloud):
    store,keys,_,_,_,uid=cloud;profile(cloud,allow_trade_details=True)
    account=simulation.create_account(store,uid,'private',10000)
    refs,sensitivity=assistant.context_refs(store,uid,[{'kind':'account','id':account['id']}],models.snapshot(store,uid))
    assert sensitivity==['trades'] and 'fills' in refs[0]['facts'] and 'cash' not in refs[0]['facts']


# 验证上下文超预算时先裁剪旧轮次，保留当前问题及必要资料。
def test_long_history_trim_and_current_turn_protected(cloud):
    store,keys,_,_,_,uid=cloud;profile(cloud,context_window=16384,output_limit=4096)
    session=resources.save(store,uid,'chat_session',{'title':'长对话'})
    for n in range(12):
        row=assistant.submit(store,keys,uid,session['id'],assistant.MessageInput(client_key='history-key-'+str(n),question='模型配置如何使用'+str(n)))
        store.execute("UPDATE assistant_requests SET status='success',reply_json=? WHERE id=?",(json_text({'answer':'项目配置说明。'*80,'citations':['help:models']}),row['id']))
        store.execute("UPDATE personal_jobs SET status='success' WHERE id=?",(row['job_id'],))
    row=assistant.submit(store,keys,uid,session['id'],assistant.MessageInput(client_key='history-current',question='当前模型配置如何设置'))
    raw=store.one('SELECT * FROM assistant_requests WHERE id=?',(row['id'],));proto,refs,sensitive,omitted=assistant.protocol(store,uid,raw,models.snapshot(store,uid))
    assert omitted>0 and len(proto['messages'])<=22 and '当前模型配置如何设置' in proto['messages'][-1]['content']
    assert len(json_text(proto['messages']).encode())<=assistant.context_budget(models.snapshot(store,uid))


# 验证助手任务跨日仍可领取，已发送意图恢复后不重复调用模型。
def test_chat_queued_across_day_and_crashed_intent_no_resend(cloud,monkeypatch):
    store,keys,_,_,_,uid=cloud;profile(cloud);session=resources.save(store,uid,'chat_session',{'title':'跨日'})
    row=assistant.submit(store,keys,uid,session['id'],assistant.MessageInput(client_key='cross-day-message',question='模型配置怎么设置'))
    store.execute("UPDATE personal_jobs SET target_date='2020-01-01' WHERE id=?",(row['job_id'],));job=jobs.claim(store,'crash')
    assert job and job['id']==row['job_id']
    ctl=control(store,keys);ctl.reserve(uid,job['id']+':1','profile','assistant',{'daily':None,'monthly':None});ctl.transition(job['id']+':1','sent')
    fixed=json.loads(job['snapshot_json'])['profile']
    store.execute('INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,created_at) VALUES(?,?,?,?,?,1,?,?)',(uuid.uuid4().hex,uid,job['id'],fixed['profile_id'],fixed['version'],'prepared',stamp()))
    jobs.recover(store);recovered=jobs.claim(store,'recovery')
    result=jobs.run(store,keys,recovered,lambda *a:pytest.fail('uncertain send retried'))
    assert result['error_code']=='UNCERTAIN_TIMEOUT' and assistant.request_detail(store,uid,row['id'])['status']=='uncertain'
    assert summary(store,uid)['daily']['sent']==1


# 验证请求进行中删除会话或用户后，迟到结果无法复活私人记录。
def test_inflight_session_delete_and_erasure_cannot_revive(cloud):
    store,keys,_,_,_,uid=cloud;profile(cloud);session=resources.save(store,uid,'chat_session',{'title':'删除'})
    row=assistant.submit(store,keys,uid,session['id'],assistant.MessageInput(client_key='delete-during-send',question='模型配置怎么设置'))
    # 模拟供应商返回前删除会话，覆盖删除与结果回写的竞争场景。
    def transport(*args):
        resources.delete(store,uid,'chat_session',session['id'])
        return json_text({'answer':'请打开个人配置。','citations':['help:models']}),{},''
    jobs.run(store,keys,jobs.claim(store,'delete'),transport)
    assert not store.one('SELECT * FROM assistant_requests WHERE id=?',(row['id'],))
    topic=resources.save(store,uid,'topic',{'name':'保留课题'})
    with store.publish_lock,store.user_lock(uid):purge_user(store,uid)
    assert not store.one('SELECT id FROM research_objects WHERE user_id=?',(uid,))


# 验证已知价格变化的观察收益，并明确区分目标缺价、停牌与尚未到期。
def test_journal_exact_five_percent_and_missing_target_states(research_data,monkeypatch):
    store,uid,ident,bars,at=research_data;before=datetime.fromisoformat(bars[0]['ts']).replace(hour=8)
    monkeypatch.setattr(research,'stamp',lambda:before.isoformat())
    row=research.save_journal(store,uid,research.JournalInput(instrument_id=ident,rationale='确定样本'))
    store.execute('UPDATE bars SET open=10,high=11,low=9,close=10.5 WHERE instrument_id=? AND ts=?',(ident,bars[0]['ts']))
    observation=research.observation(store,uid,row,at=at);assert observation['targets'][0]['return_pct']==5
    assert [t['day'] for t in observation['targets']]==[bars[i]['ts'][:10] for i in (0,4,19)]
    store.execute('DELETE FROM bars WHERE instrument_id=? AND ts=?',(ident,bars[4]['ts']))
    assert research.observation(store,uid,row,at=at)['targets'][1]['status']=='price_missing'
    store.execute('UPDATE bars SET metadata_json=? WHERE instrument_id=? AND ts=?',(json_text({**bars[19]['metadata'],'trading_status':'suspended'}),ident,bars[19]['ts']))
    assert research.observation(store,uid,row,at=at)['targets'][2]['status']=='suspended'
    assert research.observation(store,uid,row,at=before)['targets'][0]['status']=='not_due'


# 直接保存确定的已成交虚拟委托，供费用后收益和先进先出配对断言。
def saved_order(store,uid,account,ident,side,price,quantity=100,fee=5):
    oid=uuid.uuid4().hex
    store.execute('INSERT INTO sim_orders VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(oid,uid,oid,account,ident,side,quantity,str(price),None,'filled',stamp(),stamp(),str(price),str(fee),str(float(price)*quantity+fee),stamp(),None,'{}','{}'))
    return oid


# 验证闭合模拟成交扣费后收益为已知金额，部分卖出保留剩余持仓。
def test_saved_actual_fills_fifo_net_forty_and_partial(research_data):
    store,uid,ident,bars,at=research_data;account=simulation.create_account(store,uid,'虚拟',10000)
    buy=saved_order(store,uid,account['id'],ident,'buy',9.9);sell=saved_order(store,uid,account['id'],ident,'sell',10.4)
    result=research.simulation_return(store,uid,[buy,sell]);assert result['status']=='closed' and result['realized_pnl']==40
    store.execute('UPDATE sim_orders SET quantity=50 WHERE id=?',(sell,))
    result=research.simulation_return(store,uid,[buy,sell]);assert result['status']=='partial' and result['remaining_quantity']==50 and result['closed_quantity']==50


# 验证未知行业仍计入持仓分母，预占资金与账户归属校验正确。
def test_portfolio_unknown_sector_denominator_reserved_cash_and_account_owner(research_data,cloud):
    store,uid,ident,bars,at=research_data;account=simulation.create_account(store,uid,'样本',10000)
    store.execute("UPDATE sim_accounts SET cash='4000' WHERE id=?",(account['id'],))
    names=['SH:600101','SH:600102','SH:600103']
    for i,other in enumerate(names):
        from backend.instruments import InstrumentInput
        market.add_watch(store,uid,InstrumentInput(market='SH',symbol=other.split(':')[1],asset_class='stock'))
        market.import_bars(store,other,{'bars':[{**bars[-1],'open':10,'high':10,'low':10,'close':10}]})
        qty=[300,200,100][i]
        store.execute('INSERT INTO sim_lots VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,uid,account['id'],other,qty,'1000',at.date().isoformat(),1))
        if i<2:store.execute('INSERT INTO industry_classifications VALUES(?,?,?,?,?)',(other,'甲行业' if i==0 else '乙行业','https://www.sse.com.cn/fixture',at.date().isoformat(),at.isoformat()))
    pending=saved_order(store,uid,account['id'],names[0],'buy',5);store.execute("UPDATE sim_orders SET status='pending',cost='1000' WHERE id=?",(pending,))
    risk=research.portfolio_risk(store,uid,account['id'])
    assert risk['equity']==10000 and risk['position_pct']==60 and risk['cash_pct']==40
    assert risk['reserved_cash']==1000 and risk['available_cash']==3000 and risk['market_value']==6000
    assert risk['industry_concentration_pct']==50 and risk['industry_equity_pct']==30 and risk['classification_coverage_pct']==pytest.approx(5000/6000*100)
    with pytest.raises(AuthError):research.portfolio_risk(store,cloud[-2],account['id'])


# 验证实验实际尝试编号、失败和中断状态，并从归档重演共同区间指数基准。
def test_experiment_started_failure_interrupt_and_index_reproduce(research_data,cloud):
    store,uid,ident,bars,at=research_data;topic=resources.save(store,uid,'topic',{'name':'基准'})
    market.import_bars(store,'SH:000001',{'bars':bars[10:-5]})
    value=experiments.ExperimentInput(client_key='index-experiment',topic_id=topic['id'],index_id='SH:000001',parameters={'instrument_id':ident,'initial_cash':10000})
    first=experiments.enqueue(store,uid,value);jobs.run(store,cloud[1],jobs.claim(store,'index'))
    first=experiments.owned(store,uid,first['id']);assert first['status']=='success'
    index=first['result']['benchmarks']['index'];assert index['points'][0]['index']==100 and index['points'][0]['strategy']==100 and index['common_range'][0]==bars[10]['ts']
    store.execute('DELETE FROM bars WHERE instrument_id=?',('SH:000001',));assert workspace.reproduce(store,uid,first['result_id'])['benchmark_identical']
    failed=experiments.enqueue(store,uid,value.model_copy(update={'client_key':'failed-experiment'}));store.execute('DELETE FROM bars WHERE instrument_id=?',(ident,));jobs.run(store,cloud[1],jobs.claim(store,'failure'))
    failed=experiments.owned(store,uid,failed['id']);assert failed['status']=='failed' and failed['attempt_no']==2
    third=experiments.enqueue(store,uid,value.model_copy(update={'client_key':'interrupted-experiment'}));job=jobs.claim(store,'interrupted')
    store.execute("UPDATE research_experiments SET status='running',started_at=?,attempt_no=3 WHERE id=?",(stamp(),third['id']));jobs.recover(store)
    assert experiments.owned(store,uid,third['id'])['status']=='interrupted'


# 使用真实 age 备份恢复旧档，验证已删除对象与备份后用量以独立权威记录为准。
def test_deleted_resources_usage_survive_real_age_old_backup(cloud,tmp_path):
    store,keys,auth,journal,admin,uid=cloud
    age=Path(shutil.which('age') or ROOT/'.tmp/tools/age/age.exe');keygen=age.with_name('age-keygen'+age.suffix)
    if not age.is_file():pytest.skip('real age not installed')
    identity=tmp_path/'identity';assert subprocess.run([str(keygen),'-o',str(identity)],capture_output=True).returncode==0
    public=subprocess.run([str(keygen),'-y',str(identity)],capture_output=True,text=True).stdout.strip();store.cfg=replace(store.cfg,age_bin=str(age),age_recipient=public)
    ctl=control(store,keys);session=resources.save(store,uid,'chat_session',{'title':'不得复活'})
    note=resources.save(store,uid,'journal',{'rationale':'私人判断'})
    rule=resources.save(store,uid,'alert',{'name':'不得重发','instrument_id':'SH:000001','enabled':True})
    retained_rule=resources.save(store,uid,'alert',{'name':'恢复后需复核','instrument_id':'SH:000001','enabled':True})
    before=backups.create(store,keys,journal)
    for kind,row in [('chat_session',session),('journal',note),('alert',rule)]:resources.delete(store,uid,kind,row['id'])
    ctl.reserve(uid,'after-backup-send','p','assistant',{'daily':None,'monthly':None});ctl.transition('after-backup-send','sent')
    backups.restore(store,keys,journal,before['id'],identity)
    assert not store.one('SELECT id FROM research_objects WHERE id IN(?,?,?)',(session['id'],note['id'],rule['id']))
    assert summary(store,uid)['daily']['sent']==1
    assert resources.get(store,uid,'alert',retained_rule['id'])['status']=='disabled'
    assert resources.get(store,uid,'alert',retained_rule['id'])['data']['pause_reason']=='RESTORE_REQUIRES_REVIEW'
    ctl.head.unlink()
    with pytest.raises(RuntimeError):Control(store,keys)


# 验证移除自选会停用关联提醒，且接口保持分页和跨用户访问限制。
def test_watch_removal_disables_rule_and_private_api_states(research_data,cloud):
    store,uid,ident,bars,at=research_data
    rule=alerts.save(store,uid,alerts.AlertInput(name='关联提醒',instrument_id=ident,condition='price_above',threshold=5,expires_at=None))
    with TestClient(create_app(store.cfg)) as c:
        login(c);assert c.get('/api/me/research/options').status_code==200
        removed=c.delete('/api/me/watchlist/'+ident);assert removed.status_code==200 and removed.json()['disabled_alerts']==1
        assert resources.get(store,uid,'alert',rule['id'])['status']=='disabled'
        assert c.get('/api/me/readiness?size=21').status_code==400
        c.post('/api/auth/logout');login(c,'admin@example.test')
        assert c.put('/api/me/alerts/'+rule['id'],json={}).status_code==404
