# 按显式开关运行受控多用户负载夹具，只记录规模、延迟和并发元数据，不调用真实模型或邮件。
"""Opt-in scale fixture; emits metadata/timings, never credentials or private bodies."""
import json
import os
import platform
import sqlite3
import statistics
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_cloud import cloud,profile,login,PASSWORD,GOOD
from conftest import sample_bars
from backend.config import ROOT,TZ
from backend.cloud import alerts,assistant,jobs,market,models,research,resources,workspace
from backend.cloud.preferences import Preferences
from backend.cloud.research_control import control,summary
from backend.cloud.store import json_text,stamp
from backend.instruments import Rules
from backend.public_app import create_app


# 构造二十用户大规模研究档案，验证读取延迟、混合队列、公平调度和停止确认。
@pytest.mark.skipif(os.environ.get('STOCKAI_RESEARCH_LOAD')!='1',reason='opt-in full research scale fixture')
def test_twenty_user_scale_reads_mixed_queue_recovery(cloud,monkeypatch):
    store,keys,auth,journal,admin,uid=cloud;started=time.monotonic();users=[uid]
    for n in range(19):users.append(auth.new_user(f'load{n}@example.test',PASSWORD,verified=True))
    frozen=workspace.facts(store,uid,'standard');cfg=store.cfg;ts=stamp();reply=json_text({'answer':'已保存的项目说明。','citations':['help:models'],'source':'validated_model'})
    rule=Rules().model_dump_json();evidence=json_text({});sessions={};topics={}
    with store.tx(immediate=True) as c:
        items=[('SH:'+str(600000+i),str(600000+i),'SH','受控证券'+str(i),'stock','',0,rule) for i in range(300)]
        c.executemany('INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,?,?,?)',items)
        bars=sample_bars(60)
        c.executemany('INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',[(r[0],'1d',b['ts'],b['open'],b['high'],b['low'],b['close'],b['volume'],b['amount'],'none',b['source'],b['source_url'],b['fetched_at'],'valid',json_text(b['metadata'])) for r in items for b in bars])
        for user in users:
            c.executemany('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,0,?)',[(user,r[0],'受控分组',ts) for r in items])
            objects=[];versions=[]
            topic='topic:'+user;topics[user]=topic
            objects.append((topic,user,'topic',1,'active',ts,ts,json_text({'name':'受控课题'}),None));versions.append((topic,user,1,ts,json_text({'name':'受控课题'})))
            for n in range(50):
                ident=f'alert:{user}:{n}';payload=json_text({'name':'受控提醒','instrument_id':items[n][0],'condition':'risk_changed','cooldown_minutes':60,'expires_at':None,'enabled':True,'email':False,'currency':'CNY','unit':'元','rule_version':research.RULE_VERSION})
                objects.append((ident,user,'alert',1,'active',ts,ts,payload,None));versions.append((ident,user,1,ts,payload))
            for n in range(100):
                ident=f'journal:{user}:{n}';data={'rationale':'受控研究判断','instrument_id':items[n][0],'instrument':{'id':items[n][0],'name':'受控证券','market':'SH'},'judgement_at':ts,'order_ids':[]};payload=json_text(data)
                objects.append((ident,user,'journal',10,'active',ts,ts,payload,None));versions.extend((ident,user,v,ts,payload) for v in range(1,11))
            for n in range(100):
                ident=f'chat:{user}:{n}';payload=json_text({'title':f'受控对话{n}'})
                if n==0:sessions[user]=ident
                objects.append((ident,user,'chat_session',1,'active',ts,ts,payload,None));versions.append((ident,user,1,ts,payload))
            c.executemany('INSERT INTO research_objects VALUES(?,?,?,?,?,?,?,?,?)',objects);c.executemany('INSERT INTO research_versions VALUES(?,?,?,?,?)',versions)
            params=json_text({'instrument_id':items[0][0],'initial_cash':10000,'fast':5,'slow':20,'train_ratio':.7,'stop_loss_pct':.05,'strict':True})
            c.executemany("INSERT INTO research_experiments(id,user_id,topic_id,client_key,fingerprint,status,attempt_no,params_json,benchmark_json,created_at,started_at,finished_at,error_code) VALUES(?,?,?,?,?,'failed',?,?,?, ?,?,?,'CONTROLLED_MISSING_EVIDENCE')",[(f'exp:{user}:{n}',user,topic,f'fixture-{n}',str(n),n+1,params,'{}',ts,ts,ts) for n in range(200)])
            messages=[]
            for n in range(100):
                for m in range(200):
                    ident=f'msg:{user}:{n}:{m}';messages.append((ident,user,f'chat:{user}:{n}',f'fixture-message-{n}-{m}',ident,None,'模型配置如何使用',json_text({'contexts':[]}), 'success',ts,ts,reply,None,0,'[]',None))
            c.executemany('INSERT INTO assistant_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',messages)
    fixture_seconds=time.monotonic()-started
    assert store.one('SELECT COUNT(*) n FROM assistant_requests')['n']==400000
    timings={};failures=[]
    with TestClient(create_app(cfg)) as client:
        login(client)
        paths=['/api/me/readiness?size=20','/api/me/usage','/api/me/alerts?size=20','/api/me/journals?size=20','/api/me/experiment-topics?size=20','/api/me/experiments?size=20','/api/me/assistant/sessions?size=20','/api/me/assistant/sessions/'+sessions[uid]+'?size=20','/api/me/research/options','/api/me/journals/journal:'+uid+':0']
        for path in paths:
            values=[]
            for n in range(5):
                before=time.perf_counter();r=client.get(path);values.append(time.perf_counter()-before)
                if r.status_code!=200:failures.append((path,r.status_code))
            timings[path]={'count':len(values),'p95':sorted(values)[-1],'max':max(values)}
    assert not failures,failures
    # 并发读取指定用户的就绪状态与会话索引，收集每轮总耗时。
    with TestClient(create_app(cfg)) as multi:
        tokens={}
        for i,user in enumerate(users):
            email=store.one('SELECT email FROM users WHERE id=?',(user,))['email'];tokens[user]=auth.login(email,PASSWORD,'controlled-load-'+str(i))[0]
        def concurrent_read(user):
            samples=[]
            for n in range(3):
                for path in ('/api/me/readiness?size=20','/api/me/assistant/sessions?size=20'):
                    before=time.perf_counter();response=multi.get(path,headers={'Cookie':cfg.session_cookie+'='+tokens[user]});samples.append(time.perf_counter()-before)
                    assert response.status_code==200,(path,response.status_code)
            return samples
        with ThreadPoolExecutor(20) as pool:parallel=[v for values in pool.map(concurrent_read,users) for v in values]
    claims=[];accept=[];latencies=[];lock=threading.Lock();active=0;max_active=0;completed=[]
    for user in users:profile((store,keys,auth,journal,admin,user))
    for i,user in enumerate(users):
        before=time.perf_counter()
        if i%3==0:
            request=assistant.submit(store,keys,user,sessions[user],assistant.MessageInput(client_key='mixed-'+user,question='模型配置怎么使用'));jid=request['job_id']
        else:
            queued=jobs.enqueue(store,user,'profile_test' if i%3==1 else 'standard');jid=queued['id'];job=store.one('SELECT snapshot_json FROM personal_jobs WHERE id=?',(jid,));snap=json.loads(job['snapshot_json']);snap['facts']=frozen;store.execute('UPDATE personal_jobs SET snapshot_json=? WHERE id=?',(json_text(snap),jid))
        accept.append(time.perf_counter()-before)
    # 模拟有限延迟的模型响应，跟踪实际并发数并按任务返回合法结构。
    def transport(fixed,messages,key):
        nonlocal active,max_active
        with lock:active+=1;max_active=max(max_active,active)
        time.sleep(.05)
        with lock:active-=1
        if 'question' in json.loads(messages[-1]['content']):return json_text({'answer':'请打开个人模型配置页检查设置。','citations':['help:models']}),{'total_tokens':12},''
        return GOOD,{'total_tokens':12},''
    # 重复领取混合任务，记录用户与结果，并等待队列中的运行任务收尾。
    def worker(number):
        while True:
            job=jobs.claim(store,'load-'+str(number))
            if not job:
                if not store.one("SELECT id FROM personal_jobs WHERE status IN('queued','running')"):return
                time.sleep(.01);continue
            with lock:claims.append(job['user_id'])
            result=jobs.run(store,keys,job,transport)
            with lock:completed.append({'kind':job['kind'],'status':store.one('SELECT status FROM personal_jobs WHERE id=?',(job['id'],))['status'],'error_code':result.get('error_code')})
    with ThreadPoolExecutor(4) as pool:list(pool.map(worker,range(4)))
    assert len(claims)==20 and len(set(claims))==20 and max_active<=2
    assert all(r['status']=='success' for r in completed),completed
    pending=assistant.submit(store,keys,uid,sessions[uid],assistant.MessageInput(client_key='load-stop-request',question='模型配置怎么使用'))
    before=time.perf_counter();assistant.stop(store,uid,pending['id']);stop_time=time.perf_counter()-before
    alert_start=time.perf_counter()
    for user in users:alerts.evaluate(store,user)
    alert_seconds=time.perf_counter()-alert_start
    jobs.recover(store);assert len(claims)==20
    p95=lambda values:sorted(values)[min(len(values)-1,int(len(values)*.95))]
    try:
        import resource
        peak_rss=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except ImportError:
        peak_rss=None  # Windows 无此可选进程指标；不影响业务、并发或延迟断言。
    memory=Path('/proc/meminfo').read_text().splitlines()[0] if Path('/proc/meminfo').exists() else 'unavailable'
    evidence={'platform':platform.platform(),'python':platform.python_version(),'sqlite':sqlite3.sqlite_version,'cpus':os.cpu_count(),'memory':memory,'peak_process_rss_kib':peak_rss,'fixture':{'users':20,'authenticated_sessions':20,'shared_securities':300,'watchlist_per_user':300,'alerts_per_user':50,'journal_versions_per_user':1000,'experiments_per_user':200,'sessions_per_user':100,'messages_per_session':200,'shared_daily_bars':18000,'data_quality':'synthetic historical bars; current calendar and risk verification intentionally unknown'},'fixture_seconds':fixture_seconds,'database_bytes':store.path.stat().st_size,'sequential_http':timings,'concurrent_http_read':{'requests':len(parallel),'p95':p95(parallel),'max':max(parallel)},'mixed':{'jobs':len(claims),'completed':completed,'model_delay_seconds':.05,'max_model_concurrency':max_active,'accept_p95':p95(accept),'stop_ack':stop_time,'all_users_dispatched':len(set(claims))},'alerts_full_cycle':alert_seconds,'failures':failures,'real_provider':False,'real_smtp':False}
    target=Path(os.environ.get('STOCKAI_LOAD_EVIDENCE',ROOT/'.tmp/research-load.json'));target.parent.mkdir(parents=True,exist_ok=True);target.write_text(json.dumps(evidence,ensure_ascii=False,indent=2),encoding='utf-8')
    assert max(v['p95'] for v in timings.values())<2,evidence
    assert p95(parallel)<2 and p95(accept)<1 and stop_time<1 and alert_seconds<60,evidence
