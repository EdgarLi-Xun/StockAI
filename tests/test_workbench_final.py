import json
import uuid
from test_cloud import cloud,profile,run_job,GOOD
from test_workbench import research_data
from backend.cloud import alerts,assistant,jobs,market,research,resources
from backend.cloud.store import json_text
from backend.cloud.research_control import control,summary


def test_repair_invalidates_shared_readiness_without_cross_user_archives(research_data,cloud):
    store,uid,ident,bars,at=research_data;admin=cloud[-2]
    from backend.instruments import InstrumentInput
    market.add_watch(store,admin,InstrumentInput(market='SH',symbol=ident.split(':')[1],asset_class='stock'))
    store.execute("UPDATE instruments SET status='unknown' WHERE id=?",(ident,));report=run_job(cloud)
    archived=store.one('SELECT payload_json FROM personal_reports WHERE id=?',(report['report_id'],))['payload_json']
    before=research.readiness(store,uid);assert before['latest_report']['id']==report['report_id']
    assert research.readiness(store,admin)['latest_report'] is None
    store.execute("UPDATE instruments SET status='active' WHERE id=?",(ident,));after=research.readiness(store,uid)
    assert after['issue_count']<before['issue_count']
    assert store.one('SELECT payload_json FROM personal_reports WHERE id=?',(report['report_id'],))['payload_json']==archived


def test_candidate_invalid_event_has_frozen_report_version(research_data,cloud):
    store,uid,ident,bars,at=research_data;published=run_job(cloud);row=store.one('SELECT payload_json FROM personal_reports WHERE id=?',(published['report_id'],));payload=json.loads(row['payload_json'])
    payload['candidates']=[{'instrument_id':ident,'quantity':100,'budget':{'total':520},'price':5.15}]
    store.execute('UPDATE personal_reports SET payload_json=? WHERE id=?',(json_text(payload),published['report_id']))
    rule=alerts.save(store,uid,alerts.AlertInput(name='候选失效',instrument_id=ident,condition='candidate_invalid',report_id=published['report_id'],expires_at=None))
    assert alerts.sample(store,uid,rule['data'],at)['satisfied'] is False
    alerts.evaluate(store,uid,at);store.execute("UPDATE instruments SET risk_flag='st' WHERE id=?",(ident,))
    assert alerts.evaluate(store,uid,at)==1
    event=store.one('SELECT * FROM alert_events WHERE rule_id=?',(rule['id'],));facts=json.loads(event['facts_json'])
    assert facts['report_id']==published['report_id'] and facts['report_version']==1 and facts['reasons']
    assert '版本 1' in alerts.event_text(event)


def test_two_report_validated_answer_keeps_both_sources_and_program_diff(cloud):
    store,keys,_,_,_,uid=cloud;profile(cloud)
    first=run_job(cloud,transport=lambda *a:(GOOD,{},''));second=run_job(cloud,kind='supplement',parent_id=first['report_id'],transport=lambda *a:(GOOD,{},''))
    session=resources.save(store,uid,'chat_session',{'title':'报告比较'})
    row=assistant.submit(store,keys,uid,session['id'],assistant.MessageInput(client_key=uuid.uuid4().hex,question='比较两份明确报告的变化',contexts=[{'kind':'report','id':r['report_id']} for r in (first,second)]))
    def answer(*args):return json_text({'answer':'报告的事实相同，差别属于解释措辞变化。','citations':['report:'+r['report_id'] for r in (first,second)]}),{},''
    result=jobs.run(store,keys,jobs.claim(store,'compare'),answer);assert result.get('status')=='success',result
    detail=assistant.request_detail(store,uid,row['id']);refs=detail['reply']['references']
    assert len([r for r in refs if r['kind']=='report'])==2 and next(r for r in refs if r['kind']=='comparison')['facts']['kind']=='explanation_change'


def test_additive_upgrade_keeps_known_legacy_usage(cloud):
    from backend.cloud.store import stamp
    store,keys,_,_,_,uid=cloud;p=profile(cloud);job=jobs.enqueue(store,uid,'profile_test')
    store.execute('INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,usage_json,created_at) VALUES(?,?,?,?,?,1,?,?,?)',(uuid.uuid4().hex,uid,job['id'],p['id'],1,'received',json_text({'total_tokens':17}),stamp()))
    control(store,keys);value=summary(store,uid)
    assert value['daily']['sent']==1 and value['daily']['known_tokens']==17 and value['daily']['unknown_usage']==0
