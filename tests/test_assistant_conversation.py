"""Regression for greetings, selected research questions and permitted conversational follow-ups."""
import json
import uuid
import pytest
from backend.cloud import assistant,jobs,resources
from backend.cloud.auth import AuthError
from backend.cloud.store import json_text
from test_cloud import cloud,profile


def session(store,uid):return resources.save(store,uid,'chat_session',{'title':'conversation regression'})['id']
def submit(store,keys,uid,sid,question,contexts=None):
    return assistant.submit(store,keys,uid,sid,assistant.MessageInput(client_key=uuid.uuid4().hex,question=question,contexts=contexts or []))


@pytest.mark.parametrize('question',['你好','你好呀👋','您好！','hello','Hi!','你能做什么？','怎么开始？','你好，请介绍一下自己'])
def test_greeting_and_getting_started_do_not_require_research_data(cloud,question):
    store,keys,*_,uid=cloud;row=submit(store,keys,uid,session(store,uid),question)
    assert row['status']=='help' and row['reply']['source']=='project_help'
    assert 'StockAI' in row['reply']['answer'] and '请在下方附带' not in row['reply']['answer']
    assert not row['job_id'] and not store.one('SELECT id FROM model_calls')


def test_greeting_uses_selected_model_and_returns_a_validated_reply(cloud):
    store,keys,*_,uid=cloud;profile(cloud);row=submit(store,keys,uid,session(store,uid),'你好')
    assert row['status']=='queued',row
    sent=[]
    def wire(fixed,messages,key):
        sent.append(messages);return json_text({'answer':'你好，我是StockAI项目助手，可以帮助你了解项目操作。','citations':['help:assistant']}),{'total_tokens':12},'fixture'
    assert jobs.run(store,keys,jobs.claim(store,'greeting'),wire)['status']=='success'
    result=assistant.request_detail(store,uid,row['id'])
    assert result['reply']['source']=='validated_model' and len(sent)==1


@pytest.mark.parametrize('question',['分析一下','帮我分析一下','帮我分析这只股票','总结所选资料','继续','再解释一下'])
def test_short_research_question_with_explicit_context_reaches_model(cloud,question):
    store,keys,*_,uid=cloud;profile(cloud);instrument=store.one('SELECT id FROM instruments LIMIT 1')['id']
    context=[{'kind':'instrument','id':instrument}];row=submit(store,keys,uid,session(store,uid),question,context)
    assert row['status']=='queued',row
    def wire(fixed,messages,key):
        sources=json.loads(messages[-1]['content'])['sources'];assert any(s['id']=='instrument:'+instrument for s in sources)
        return json_text({'answer':'请结合所选证券的证据状态核对，缺失行情时不能据此判断买卖。','citations':['instrument:'+instrument]}),{},'fixture'
    assert jobs.run(store,keys,jobs.claim(store,'context'),wire)['status']=='success'


def test_followup_keeps_permitted_help_topic_and_model_history(cloud):
    store,keys,*_,uid=cloud;profile(cloud);sid=session(store,uid)
    first=submit(store,keys,uid,sid,'模型配置怎么使用？')
    first_answer='请在模型配置页保存个人配置并设为默认。'
    jobs.run(store,keys,jobs.claim(store,'first'),lambda *args:(json_text({'answer':first_answer,'citations':['help:models']}),{},'fixture'))
    assert assistant.request_detail(store,uid,first['id'])['status']=='success'
    follow=submit(store,keys,uid,sid,'继续')
    assert follow['status']=='queued',follow
    def wire(fixed,messages,key):
        assert any(m['role']=='assistant' and m['content']==first_answer for m in messages)
        current=json.loads(messages[-1]['content']);assert any(s['id']=='help:models' for s in current['sources'])
        return json_text({'answer':'保存后可以查看连接状态，再检查当前的数据授权。','citations':['help:models']}),{},'fixture'
    assert jobs.run(store,keys,jobs.claim(store,'follow'),wire)['status']=='success'


def test_followup_without_model_stays_on_static_help_topic(cloud):
    store,keys,*_,uid=cloud;sid=session(store,uid)
    assert submit(store,keys,uid,sid,'预算怎么配置？')['reply']['citations']==['help:budget']
    follow=submit(store,keys,uid,sid,'继续')
    assert follow['status']=='help' and follow['reply']['citations']==['help:budget']
    assert not follow['job_id']


def test_missing_object_and_unrelated_requests_still_stop_before_model(cloud):
    store,keys,*_,uid=cloud;profile(cloud);sid=session(store,uid)
    for question,state in [('分析一下','clarification'),('继续','clarification'),('删除最近的报告','refused')]:
        row=submit(store,keys,uid,sid,question);assert row['status']==state and not row['job_id']
    assert not store.one('SELECT id FROM model_calls')


def test_greeting_does_not_bypass_context_ownership(cloud):
    store,keys,_,_,admin,uid=cloud;profile(cloud)
    from backend.cloud.simulation import create_account
    other=create_account(store,admin,'other owner',1000)
    with pytest.raises(AuthError):submit(store,keys,uid,session(store,uid),'你好',[{'kind':'account','id':other['id']}])


def test_cleared_research_context_is_not_silently_restored_from_history(cloud):
    store,keys,*_,uid=cloud;profile(cloud);sid=session(store,uid);instrument=store.one('SELECT id FROM instruments LIMIT 1')['id']
    row=submit(store,keys,uid,sid,'解释这只证券的风险',[{'kind':'instrument','id':instrument}])
    jobs.run(store,keys,jobs.claim(store,'source'),lambda *args:(json_text({'answer':'请核对该证券的证据和行情质量。','citations':['instrument:'+instrument]}),{},'fixture'))
    assert assistant.request_detail(store,uid,row['id'])['status']=='success'
    follow=submit(store,keys,uid,sid,'继续')
    assert follow['status']=='clarification' and not follow['job_id']
