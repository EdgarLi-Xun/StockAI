"""Regression cases for reversions, idempotency, and transactional schema upgrades."""
import json
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from test_cloud import cloud,login,profile
from test_workbench import research_data
from backend.cloud import alerts,assistant,research,resources,research_schema,simulation
from backend.cloud.auth import AuthError
from backend.cloud.store import json_text,stamp
from backend.public_app import create_app


def test_valuation_a_b_a_is_a_new_revision_even_at_same_timestamp(cloud,monkeypatch):
    store,_,_,_,_,uid=cloud;account=simulation.create_account(store,uid,'历史回退样本',10000)
    monkeypatch.setattr(research,'stamp',lambda:'2026-11-02T00:00:00+00:00')
    first=research.portfolio_risk(store,uid,account['id'])
    assert research.portfolio_risk(store,uid,account['id'])['valuation_revision']==1
    store.execute("UPDATE sim_accounts SET cash='12000' WHERE id=?",(account['id'],));second=research.portfolio_risk(store,uid,account['id'])
    store.execute("UPDATE sim_accounts SET cash='10000' WHERE id=?",(account['id'],));third=research.portfolio_risk(store,uid,account['id'])
    assert [first['valuation_revision'],second['valuation_revision'],third['valuation_revision']]==[1,2,3]
    assert [p['equity'] for p in third['curve'][1:]]==[10000,12000,10000]
    assert third['current_drawdown_pct']==pytest.approx(100/6)
    assert research.portfolio_risk(store,uid,account['id'])['valuation_revision']==3


def test_journal_a_b_a_keeps_all_calculations_and_original_judgement(research_data,monkeypatch):
    store,uid,ident,bars,at=research_data
    monkeypatch.setattr(research,'stamp',lambda:bars[0]['ts'].replace('15:00','08:00'))
    row=research.save_journal(store,uid,research.JournalInput(instrument_id=ident,rationale='最初判断'))
    original=resources.get(store,uid,'journal',row['id'])['data'];research.journal_detail(store,uid,row['id'],at=at)
    close=bars[0]['close'];store.execute('UPDATE bars SET close=? WHERE instrument_id=? AND ts=?',(close*1.1,ident,bars[0]['ts']))
    research.journal_detail(store,uid,row['id'],at=at)
    store.execute('UPDATE bars SET close=? WHERE instrument_id=? AND ts=?',(close,ident,bars[0]['ts']))
    detail=research.journal_detail(store,uid,row['id'],at=at);history=detail['versions'][0]['calculations']
    assert [h['version'] for h in history]==[1,2,3]
    assert history[0]['data']==history[2]['data'] and history[1]['data']!=history[2]['data']
    assert resources.get(store,uid,'journal',row['id'])['data']==original
    assert len(research.journal_detail(store,uid,row['id'],at=at)['versions'][0]['calculations'])==3


def legacy_history(store,uid):
    account=simulation.create_account(store,uid,'旧版账户',10000)
    journal=resources.save(store,uid,'journal',{'rationale':'原始判断'})
    valuation=json_text({'equity':10000,'quality':'verified','source':'legacy-fixture'})
    observation=json_text({'status':'calendar_unknown','targets':[]})
    with store.tx(immediate=True) as c:
        c.execute('INSERT INTO portfolio_valuations VALUES(?,?,?,?,?,?)',('legacy-val',uid,account['id'],stamp(),'old-val-hash',valuation))
        c.execute('INSERT INTO journal_observations VALUES(?,?,?,?,?,?,?,?)',('legacy-observation',uid,journal['id'],1,1,stamp(),'old-observation-hash',observation))
        c.execute("UPDATE cloud_meta SET value='1' WHERE key='research_schema_version'")
    return valuation,observation


def test_schema2_copies_legacy_verbatim_and_upgrade_is_repeatable(cloud):
    store,keys,_,_,_,uid=cloud;profile(cloud)
    cipher=store.one('SELECT envelope_json FROM model_credentials')['envelope_json'];old=legacy_history(store,uid)
    store.initialize();store.initialize()
    assert store.one("SELECT value FROM cloud_meta WHERE key='research_schema_version'")['value']=='2'
    valuation=store.one('SELECT * FROM portfolio_valuation_history');observation=store.one('SELECT * FROM journal_observation_history')
    assert valuation['payload_json']==old[0] and valuation['origin']=='schema1' and valuation['revision']==1
    assert observation['payload_json']==old[1] and observation['calculation_version']==1
    assert store.one('SELECT COUNT(*) n FROM portfolio_valuation_history')['n']==1
    assert store.one('SELECT envelope_json FROM model_credentials')['envelope_json']==cipher
    assert store.query('PRAGMA foreign_key_check')==[]


def test_schema2_failure_rolls_back_copies_marker_and_preserves_legacy(cloud,monkeypatch):
    store,_,_,_,_,uid=cloud;old=legacy_history(store,uid);original=research_schema._copy_legacy_history
    def failure(c):original(c);raise RuntimeError('controlled migration interruption')
    with monkeypatch.context() as m:
        m.setattr(research_schema,'_copy_legacy_history',failure)
        with pytest.raises(RuntimeError,match='controlled migration'):store.initialize()
    assert store.one("SELECT value FROM cloud_meta WHERE key='research_schema_version'")['value']=='1'
    assert not store.query('SELECT id FROM portfolio_valuation_history')
    assert not store.query('SELECT id FROM journal_observation_history')
    assert store.one('SELECT payload_json FROM portfolio_valuations')['payload_json']==old[0]
    assert store.one('SELECT payload_json FROM journal_observations')['payload_json']==old[1]
    assert store.query('PRAGMA foreign_key_check')==[]
    store.initialize();assert store.one('SELECT COUNT(*) n FROM portfolio_valuation_history')['n']==1


def test_alert_api_replay_preserves_server_expiry_and_original_creation_after_edit(research_data,cloud,monkeypatch):
    store,uid,ident,bars,at=research_data;body={'client_key':'alert-default-replay','data':{'name':'默认有效期','instrument_id':ident,'condition':'risk_changed'}}
    with TestClient(create_app(store.cfg)) as c:
        login(c);first=c.post('/api/me/alerts',json=body);assert first.status_code==200,first.text
        row=first.json();monkeypatch.setattr(alerts,'now',lambda:at+timedelta(days=90))
        repeated=c.post('/api/me/alerts',json=body);assert repeated.status_code==200 and repeated.json()['id']==row['id']
        assert repeated.json()['data']['expires_at']==row['data']['expires_at']
        edited={**body['data'],'name':'已编辑','enabled':False,'expires_at':row['data']['expires_at']}
        update=c.put('/api/me/alerts/'+row['id'],json={'version':row['version'],'data':edited});assert update.status_code==200,update.text
        assert c.post('/api/me/alerts',json=body).json()['id']==row['id']
        changed={**body,'data':{**body['data'],'name':'不同请求'}}
        assert c.post('/api/me/alerts',json=changed).status_code==409
        assert store.one("SELECT COUNT(*) n FROM research_objects WHERE kind='alert'")['n']==1


def test_percentage_edit_keeps_reference_and_disable_survives_missing_quote(research_data):
    store,uid,ident,bars,at=research_data
    row=alerts.save(store,uid,alerts.AlertInput(name='固定参考收盘',instrument_id=ident,condition='change_above',threshold=5,expires_at=None),client_key='percentage-ref')
    reference=row['data']['reference']
    store.execute('UPDATE bars SET close=close*1.1 WHERE instrument_id=? AND ts=?',(ident,bars[-1]['ts']))
    value=alerts.AlertInput(name='改阈值',instrument_id=ident,condition='change_above',threshold=8,expires_at=None)
    edited=alerts.save(store,uid,value,row['id'],row['version']);assert edited['data']['reference']==reference
    store.execute('DELETE FROM bars WHERE instrument_id=?',(ident,));store.execute('DELETE FROM user_watchlist WHERE user_id=? AND instrument_id=?',(uid,ident))
    disabled=alerts.save(store,uid,value.model_copy(update={'enabled':False}),row['id'],edited['version'])
    assert disabled['status']=='disabled' and disabled['data']['reference']==reference


def test_journal_http_retry_does_not_retimestamp_or_duplicate(research_data,cloud):
    store,uid,ident,bars,at=research_data;body={'client_key':'journal-http-replay','data':{'instrument_id':ident,'rationale':'同一判断'}}
    with TestClient(create_app(store.cfg)) as c:
        login(c);first=c.post('/api/me/journals',json=body);second=c.post('/api/me/journals',json=body)
        assert first.status_code==second.status_code==200
        assert first.json()['id']==second.json()['id'] and first.json()['data']['judgement_at']==second.json()['data']['judgement_at']
        assert c.post('/api/me/journals',json={**body,'data':{**body['data'],'rationale':'不同判断'}}).status_code==409
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE kind='journal'")['n']==1


def test_gapped_equity_keeps_segments_without_full_interval_risk():
    curve=[{'ts':str(i),'equity':v,'quality':'verified' if v is not None else 'incomplete'} for i,v in enumerate([10000,None,12000,10800,None])]
    assert research.drawdowns([r['equity'] for r in curve])['max_drawdown_pct'] is None
    segments=research.valid_valuation_segments(curve)
    assert len(segments)==2 and segments[0]['points']==1 and segments[0]['max_drawdown_pct'] is None
    assert segments[1]['current_drawdown_pct']==10 and segments[1]['max_drawdown_pct']==10


def test_rule_expiration_visible_before_next_worker_tick_and_renewable(research_data,cloud,monkeypatch):
    store,uid,ident,bars,at=research_data
    row=alerts.save(store,uid,alerts.AlertInput(name='短有效期',instrument_id=ident,condition='risk_changed',expires_at=at+timedelta(minutes=1)))
    monkeypatch.setattr(alerts,'now',lambda:at+timedelta(minutes=2))
    with TestClient(create_app(store.cfg)) as c:
        login(c);listed=c.get('/api/me/alerts?status=expired');assert listed.status_code==200
        assert listed.json()['items'][0]['id']==row['id'] and listed.json()['items'][0]['status']=='expired'
    assert alerts.evaluate(store,uid,at+timedelta(minutes=2))==0
    original=resources.get(store,uid,'alert',row['id'])
    renewed=alerts.save(store,uid,alerts.AlertInput(name='已续期',instrument_id=ident,condition='risk_changed',expires_at=at+timedelta(days=30)),row['id'],original['version'])
    assert renewed['status']=='active'


def test_preview_uses_frozen_reference_and_rejects_other_users(research_data,cloud):
    store,uid,ident,bars,at=research_data
    value=alerts.AlertInput(name='预览基准',instrument_id=ident,condition='change_above',threshold=5,expires_at=None)
    rule=alerts.save(store,uid,value);reference=rule['data']['reference']
    store.execute('UPDATE bars SET close=close*1.1 WHERE instrument_id=? AND ts=?',(ident,bars[-1]['ts']))
    with TestClient(create_app(store.cfg)) as c:
        login(c);url=f"/api/me/alerts/preview?rule_id={rule['id']}&version={rule['version']}"
        result=c.post(url,json=value.model_dump(mode='json'));assert result.status_code==200,result.text
        assert result.json()['condition']['reference']==reference and result.json()['current']['value']==pytest.approx(10)
        c.post('/api/auth/logout');login(c,'admin@example.test');assert c.post(url,json=value.model_dump(mode='json')).status_code==404


def test_report_diff_keeps_nested_fee_and_total_changes_without_notes():
    left={'candidates':[{'instrument_id':'SH:1','budget':{'total':1000,'fees':{'total':5},'fee_note':'PRIVATE-NOTE'}}]}
    right={'candidates':[{'instrument_id':'SH:1','budget':{'total':990,'fees':{'total':4},'fee_note':'PRIVATE-NOTE'}}]}
    changes=assistant.compare_reports(left,right)['changed']
    assert changes[0]['fields']['budget']['left']=={'total':1000,'fees':{'total':5}}
    assert changes[0]['fields']['budget']['right']=={'total':990,'fees':{'total':4}}
    assert 'PRIVATE-NOTE' not in json_text(changes)


@pytest.mark.parametrize('answer',['现在可以买入。','我推荐加仓。','这个账户没有风险。','当前回撤为零。','当前交易资格已经通过。','资料不能证明安全。现在可以买入。'])
def test_assistant_cannot_turn_facts_into_new_trading_or_zero_risk_claims(answer):
    with pytest.raises(ValueError):assistant.validate_answer_text(answer,True)


@pytest.mark.parametrize('answer',['预算通过不代表可以买入，资格仍需独立核验。','不要把该结果理解为没有风险。','历史资料仅用于解释当时记录。'])
def test_assistant_keeps_legitimate_qualified_explanations(answer):
    assistant.validate_answer_text(answer,True)


def test_instrument_context_marks_missing_or_unverified_quotes(research_data,cloud):
    store,uid,ident,bars,at=research_data
    store.execute("UPDATE bars SET quality='invalid' WHERE instrument_id=? AND ts=?",(ident,bars[-1]['ts']))
    refs,_=assistant.context_refs(store,uid,[{'kind':'instrument','id':ident}],None)
    assert refs[0]['facts']['quote_quality']=='unverified' and refs[0]['facts']['quote_notes']
    store.execute('DELETE FROM bars WHERE instrument_id=?',(ident,));refs,_=assistant.context_refs(store,uid,[{'kind':'instrument','id':ident}],None)
    assert refs[0]['facts']['quote_quality']=='unavailable' and refs[0]['facts']['quote'] is None


@pytest.mark.parametrize('question',['我的账户回撤为什么变化？','当前账户资金为什么少了？','最近报告有哪些变化？'])
def test_personal_fact_question_requires_selected_context(question):
    assert assistant.classify(question,[])=='clarify'


def test_operating_help_does_not_require_personal_account_selection():
    assert assistant.classify('如何创建我的账户？',[])=='project'
    with pytest.raises(ValueError):assistant.validate_answer_text('当前回撤为零。',False)


@pytest.mark.parametrize('usage,expected',[({'eval_count':3},None),({'prompt_eval_count':5},None),({'eval_count':3,'prompt_eval_count':5},8),({'eval_count':True,'prompt_eval_count':5},None),({'total_tokens':1.5},None),({'total_tokens':8.0},8),({'total_tokens':float('inf')},None),({'total_tokens':10**1000},None)])
def test_partial_provider_usage_is_not_a_complete_zero_filled_total(usage,expected):
    from backend.cloud.research_control import known_tokens
    assert known_tokens(usage)==expected


def test_legacy_usage_batch_interruption_restarts_without_partial_or_duplicate_counts(cloud,monkeypatch):
    from backend.cloud import models
    from backend.cloud.research_control import Control,control,summary
    store,keys,_,_,_,uid=cloud;p=profile(cloud);snapshot=json_text({'profile':models.snapshot(store,uid)});ts=stamp()
    with store.tx(immediate=True) as c:
        for i in range(20):
            jid='legacy-job-'+str(i)
            c.execute("INSERT INTO personal_jobs(id,user_id,logical_key,kind,target_date,status,snapshot_json,created_at) VALUES(?,?,?,'profile_test','2026-11-01','success',?,?)",(jid,uid,jid,snapshot,ts))
            c.execute("INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,usage_json,created_at) VALUES(?,?,?,?,1,1,'received',?,?)",('call-'+str(i),uid,jid,p['id'],json_text({'total_tokens':10}),ts))
    append=Control.append;calls=[]
    def interrupted(self,c,value):
        calls.append(value['id']);result=append(self,c,value)
        if len(calls)==3:raise RuntimeError('controlled authority migration interruption')
        return result
    with monkeypatch.context() as m:
        m.setattr(Control,'append',interrupted)
        with pytest.raises(RuntimeError,match='authority migration'):control(store,keys)
    assert not store.one("SELECT value FROM cloud_meta WHERE key='research_authority_initialized'")
    ctl=control(store,keys);assert ctl.verify()['usage']==20
    value=summary(store,uid);assert value['daily']['sent']==20 and value['daily']['known_tokens']==200
    ctl.import_legacy_usage();assert summary(store,uid)['daily']['sent']==20


def test_selected_context_must_match_the_personal_object_in_question():
    assert assistant.classify('我的账户回撤为什么变化？',[{'kind':'report','id':'r'}])=='clarify'
    assert assistant.classify('比较两份报告的变化',[{'kind':'report','id':'r'}])=='clarify'
    assert assistant.classify('我的回测为什么失败？',[{'kind':'instrument','id':'i'}])=='clarify'
    assistant.validate_answer_text('我不推荐买入，应先核验资料。',True)
