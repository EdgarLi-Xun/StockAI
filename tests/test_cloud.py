import json
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
import shutil

import pytest
from fastapi.testclient import TestClient

from backend.cloud import jobs,models,mail,simulation,workspace,backups
from backend.cloud.auth import Auth,AuthError
from backend.cloud.config import CloudConfig
from backend.cloud.crypto import Keyring,digest
from backend.cloud.lifecycle import ErasureJournal,erase,reconcile
from backend.cloud.preferences import Preferences,save_preferences
from backend.cloud.store import Store,json_text,stamp
from backend.config import ROOT,now
from backend.public_app import create_app

PASSWORD="test-cloud-password-123"
GOOD=json.dumps({"summary":"研究事实已核对","market_context":"数据有限，保持谨慎","candidates":[],"risk_notes":["历史结果不代表未来"],"missing_data":[],"source_ids":[]},ensure_ascii=False)


@pytest.fixture
def cloud(tmp_path,monkeypatch):
    monkeypatch.setattr(models,"public_ips",lambda *args:["1.1.1.1"])
    cfg=CloudConfig(data=tmp_path/"public",erasures=tmp_path/"erasures",key_file=tmp_path/"secrets"/"keys.json",public_url="http://testserver",dev=True,background=False)
    store=Store(cfg).initialize();keys=Keyring.create(cfg.key_file);auth=Auth(store,keys)
    journal=ErasureJournal(store,keys);journal.create()
    admin=auth.create_admin("admin@example.test",PASSWORD)
    uid=auth.new_user("user@example.test",PASSWORD,verified=True)
    return store,keys,auth,journal,admin,uid


def login(client,email="user@example.test"):
    csrf=client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers.update({"Origin":"http://testserver","X-CSRF-Token":csrf})
    r=client.post("/api/auth/login",json={"email":email,"password":PASSWORD});assert r.status_code==200,r.text
    client.headers["X-CSRF-Token"]=client.cookies["stockai_dev_csrf"]
    return r.json()["user"]


def profile(cloud,uid=None,**kwargs):
    store,keys,_,_,_,user=cloud;uid=uid or user
    value=models.ProfileInput(name="个人测试配置",provider="minimax",api_key="test-key-owner-1234",**kwargs)
    row=models.save_profile(store,keys,uid,value);models.set_default(store,uid,row["id"])
    return row


def run_job(cloud,uid=None,kind="standard",transport=None,**kwargs):
    store,keys,_,_,_,user=cloud;enqueued=jobs.enqueue(store,uid or user,kind,**kwargs);job=jobs.claim(store,"test-worker")
    assert job and job["id"]==enqueued["id"]
    return jobs.run(store,keys,job,transport)


def test_invite_email_verify_reset_session_csrf(cloud):
    store,keys,auth,journal,admin,uid=cloud
    invitation=auth.invitation(admin)
    with TestClient(create_app(store.cfg)) as c:
        assert c.get("/api/me/dashboard").status_code==401
        assert c.post("/api/auth/login",json={}).status_code==403
        csrf=c.get("/api/auth/csrf").json()["csrf_token"];c.headers.update({"Origin":"http://testserver","X-CSRF-Token":csrf})
        response=c.post("/api/auth/register",json={"email":"new@example.test","password":PASSWORD,"invitation":invitation["code"]})
        assert response.status_code==200,response.text
        assert c.post("/api/auth/register",json={"email":"twice@example.test","password":PASSWORD,"invitation":invitation["code"]}).status_code==400
        login(c,"new@example.test")
        assert c.get("/api/me/dashboard").status_code==403
        sent=[];assert mail.send_one(store,keys,sent.append)
        body=sent[0].get_content();token=body.split("token=")[1].split()[0]
        assert c.post("/api/auth/verify-email",json={"token":token}).status_code==200
        assert c.post("/api/auth/verify-email",json={"token":token}).status_code==400
        assert c.get("/api/me/dashboard").status_code==200
        c.post("/api/auth/request-reset",json={"email":"new@example.test"})
        sent=[];mail.send_one(store,keys,sent.append);reset=sent[0].get_content().split("token=")[1].split()[0]
        assert c.post("/api/auth/reset-password",json={"token":reset,"password":PASSWORD+"-new"}).status_code==200
        assert c.get("/api/auth/me").status_code==401
        assert not store.one("SELECT payload_json FROM mail_outbox WHERE state='sent' AND payload_json IS NOT NULL")


def test_invite_atomic_single_use(cloud):
    store,_,auth,_,admin,_=cloud;code=auth.invitation(admin)["code"]
    def register(n):
        try:
            auth.register(f"race{n}@example.test",PASSWORD,code,f"peer{n}");return True
        except AuthError:
            return False
    with ThreadPoolExecutor(2) as executor:
        assert sum(executor.map(register,range(2)))==1


def test_profiles_owner_cipher_and_default_snapshot(cloud):
    store,keys,auth,_,admin,uid=cloud;p=profile(cloud)
    other=profile(cloud,admin)
    first=jobs.enqueue(store,uid);fixed=json.loads(store.one("SELECT snapshot_json FROM personal_jobs WHERE id=?",(first["id"],))["snapshot_json"])["profile"]
    p2=models.save_profile(store,keys,uid,models.ProfileInput(name="另一套",provider="ollama",model="server-model"));models.set_default(store,uid,p2["id"])
    assert fixed["profile_id"]==p["id"] and models.snapshot(store,uid)["profile_id"]==p2["id"]
    blob=store.one("SELECT * FROM model_credentials WHERE profile_id=?",(p["id"],))
    assert "test-key-owner-1234" not in blob["envelope_json"]
    assert keys.open(blob["envelope_json"],site=store.site_id,user=uid,resource=blob["id"],purpose="model-key")=="test-key-owner-1234"
    with pytest.raises(Exception):
        keys.open(blob["envelope_json"],site=store.site_id,user=admin,resource=blob["id"],purpose="model-key")
    with pytest.raises(AuthError):
        models.save_profile(store,keys,admin,models.ProfileInput(name="steal",provider="ollama"),p["id"])
    assert all("envelope" not in json_text(r) and "test-key-owner-1234" not in json_text(r) for r in models.profile_rows(store,uid))


def test_projection_has_no_identity_notes_account_names(cloud):
    fixed={"allow_summary":0,"allow_trades":0}
    facts={"mode":"full","candidates":[],"sources":[],"watchlist":[{"instrument_id":"SH:600001","group_name":"SECRET-GROUP","official_name":"官方证券"}],"simulation":{"accounts":[{"id":"secret-id","name":"SECRET-ACCOUNT","cash":"1000","positions":[{"instrument_id":"SH:600001","quantity":100,"note":"SECRET-NOTE"}]}],"recent_fills":[{"instrument_id":"SH:600001","quantity":100,"note":"SECRET-TRADE"}]},"email":"SECRET-EMAIL","free_note":"SECRET-FREE"}
    text=json_text(models.project_input(facts,fixed));assert "simulation_summary" not in text and "trade_details" not in text and "SECRET" not in text
    text=json_text(models.project_input(facts,{"allow_summary":1,"allow_trades":1}));assert "simulation_summary" in text and "trade_details" in text and "SECRET" not in text and "secret-id" not in text


def test_rules_publish_supplement_versions_and_idor(cloud):
    store,keys,auth,_,admin,uid=cloud
    first=run_job(cloud);assert first["ai_status"]=="fallback",first
    before=workspace.owned(store,"personal_reports",uid,first["report_id"])
    profile(cloud)
    second=run_job(cloud,kind="supplement",parent_id=first["report_id"],transport=lambda *args:(GOOD,{},"test"))
    assert second["ai_status"]=="generated" and second["version"]==2,second
    assert workspace.owned(store,"personal_reports",uid,first["report_id"])["payload_json"]==before["payload_json"]
    with TestClient(create_app(store.cfg)) as c:
        login(c,"admin@example.test")
        for endpoint in ("", "/export", "/export?format=json", "/export?format=html"):
            assert c.get("/api/me/reports/"+first["report_id"]+endpoint).status_code==404
        assert c.post("/api/me/reports/"+first["report_id"]+"/supplement").status_code==404
        assert c.get("/api/me/reports").json()==[]
        assert c.get("/api/admin/users").status_code==200


@pytest.mark.parametrize("code,retryable,uncertain,calls",[("RATE_LIMITED",True,False,2),("TEMPORARY_PROVIDER_ERROR",True,False,2),("CONNECT_FAILED",True,False,2),("INVALID_KEY",False,False,1),("QUOTA_EXCEEDED",False,False,1),("UNCERTAIN_TIMEOUT",False,True,1),("INVALID_OUTPUT",False,False,1)])
def test_model_retry_policy(cloud,code,retryable,uncertain,calls):
    profile(cloud);seen=[]
    def transport(*args):
        seen.append(args);raise models.ModelError(code,retryable,uncertain)
    result=run_job(cloud,transport=transport)
    assert result["ai_status"]=="fallback" and len(seen)==calls,result
    payload=workspace.report_detail(cloud[0],cloud[-1],result["report_id"])["payload"]
    assert payload["ai_error_code"]==code


def test_bad_model_output_no_retry(cloud):
    profile(cloud);seen=[]
    def transport(*args):
        seen.append(True);return GOOD.replace("研究事实已核对","保证盈利123"),{},""
    result=run_job(cloud,transport=transport)
    assert result["ai_status"]=="fallback" and len(seen)==1


def test_key_consent_and_disable_gate_before_send(cloud):
    store,keys,auth,_,admin,uid=cloud;p=profile(cloud,allow_simulation_summary=True)
    jobs.enqueue(store,uid);job=jobs.claim(store,"gate")
    models.save_profile(store,keys,uid,models.ProfileInput(name=p["name"],provider="minimax",allow_simulation_summary=False),p["id"])
    seen=[];result=jobs.run(store,keys,job,lambda *args:seen.append(True))
    assert not seen and result["ai_status"]=="fallback"
    actor=auth.actor(auth.login("admin@example.test",PASSWORD,"admin")[0]);jobs.enqueue(store,uid,"full")
    auth.disable(actor,uid,True)
    assert not jobs.claim(store,"disabled")
    assert store.one("SELECT status FROM personal_jobs WHERE user_id=? AND kind='full'",(uid,))["status"]=="cancelled"


def test_erasure_blocks_inflight_writeback_and_restart(cloud):
    store,keys,auth,journal,admin,uid=cloud;profile(cloud)
    jobs.enqueue(store,uid);job=jobs.claim(store,"inflight");entered=threading.Event();release=threading.Event()
    def transport(*args):
        entered.set();assert release.wait(10);return GOOD,{},""
    with ThreadPoolExecutor(1) as executor:
        future=executor.submit(jobs.run,store,keys,job,transport);assert entered.wait(10)
        token=auth.login("user@example.test",PASSWORD,"erase")[0]
        erase(store,journal,auth.actor(token),PASSWORD,"注销并删除我的全部个人数据")
        release.set();future.result(timeout=10)
    assert not store.one("SELECT id FROM users WHERE id=?",(uid,))
    assert not store.query("SELECT * FROM personal_reports WHERE user_id=?",(uid,))
    assert not store.query("SELECT * FROM model_credentials WHERE user_id=?",(uid,))
    assert reconcile(store,journal)==1


def test_schedule_defaults_rules_only_and_no_past_catchup(cloud):
    store,keys,auth,journal,admin,uid=cloud;profile(cloud)
    assert not jobs.schedule(store)
    p=Preferences(morning_enabled=True,automatic_ai=False);save_preferences(store,uid,p.model_dump())
    jobs.schedule(store,now().replace(hour=22,minute=0));job=jobs.claim(store,"automatic");seen=[]
    assert job and job["automatic"]
    result=jobs.run(store,keys,job,lambda *args:seen.append(True))
    assert not seen and result["ai_status"]=="fallback"
    old=jobs.enqueue(store,uid,"full");store.execute("UPDATE personal_jobs SET target_date='2000-01-01' WHERE id=?",(old["id"],));jobs.recover(store)
    assert store.one("SELECT status FROM personal_jobs WHERE id=?",(old["id"],))["status"]=="cancelled"


def test_restart_sent_call_is_not_resent(cloud):
    store,keys,_,_,_,uid=cloud;p=profile(cloud)
    jobs.enqueue(store,uid);job=jobs.claim(store,"crashed")
    facts=workspace.facts(store,uid,"standard");snap=json.loads(job["snapshot_json"]);snap["facts"]=facts
    store.execute("UPDATE personal_jobs SET snapshot_json=? WHERE id=?",(json_text(snap),job["id"]))
    fixed=snap["profile"]
    store.execute("INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,created_at) VALUES('crashed-call',?,?,?,?,1,'sent',?)",(uid,job["id"],p["id"],fixed["version"],stamp()))
    jobs.recover(store);job=jobs.claim(store,"restart");seen=[];result=jobs.run(store,keys,job,lambda *args:seen.append(True))
    assert result["ai_status"]=="fallback" and not seen


def test_20_users_two_workers_fair_queue(cloud):
    import time
    store,keys,auth,_,_,first=cloud
    users=[first]+[auth.new_user(f"load{n}@example.test",PASSWORD,verified=True) for n in range(19)]
    for uid in users:
        jobs.enqueue(store,uid)
        with pytest.raises(AuthError):
            jobs.enqueue(store,uid,"full")
    a=jobs.claim(store,"A");b=jobs.claim(store,"B")
    assert a["user_id"]!=b["user_id"] and not jobs.claim(store,"C")
    extra=jobs.enqueue(store,a["user_id"],"full")
    started=time.perf_counter()
    with ThreadPoolExecutor(2) as executor:
        results=list(executor.map(lambda j:jobs.run(store,keys,j),[a,b]))
        while (claimed:=jobs.claim(store,"A")):
            assert jobs.run(store,keys,claimed).get("report_id")
    assert store.one("SELECT COUNT(*) n FROM personal_reports")["n"]==21
    assert len({r["user_id"] for r in store.query("SELECT user_id FROM personal_reports")})==20
    assert all(r["ai_status"]=="fallback" for r in results)


@pytest.mark.parametrize("url",["http://api.example.com/v1","https://127.0.0.1/v1","https://[::1]/v1","https://169.254.169.254/v1","https://user:key@example.com/v1","https://example.com/v1?key=secret","https://example.com/#frag"])
def test_cloud_address_rejects_unsafe(url):
    with pytest.raises(models.ModelError):
        models.cloud_address(url,resolve=False)


def test_dns_all_addresses_must_be_public(monkeypatch):
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:[(socket.AF_INET,1,6,"",("1.1.1.1",443)),(socket.AF_INET,1,6,"",("10.0.0.1",443))])
    with pytest.raises(models.ModelError):
        models.public_ips("provider.example")


def test_age_backup_restore_applies_erasure_and_no_old_keys(cloud,tmp_path):
    store,keys,auth,journal,admin,uid=cloud
    age=Path(shutil.which("age") or ROOT/".tmp"/"tools"/"age"/"age.exe");keygen=age.with_name("age-keygen" if age.suffix!=".exe" else "age-keygen.exe")
    if not age.is_file():
        pytest.skip("真实 age 往返需要设置本地测试工具")
    import subprocess
    identity=tmp_path/"identity.txt"
    r=subprocess.run([str(keygen),"-o",str(identity)],capture_output=True);assert r.returncode==0
    public=subprocess.run([str(keygen),"-y",str(identity)],capture_output=True,text=True).stdout.strip()
    store.cfg=replace(store.cfg,age_bin=str(age),age_recipient=public)
    profile(cloud);profile(cloud,admin)
    report=run_job(cloud);original=backups.create(store,keys,journal)
    assert not (store.cfg.data/"backups"/(original["id"]+".zip")).exists()
    token=auth.login("user@example.test",PASSWORD,"erase")[0];erase(store,journal,auth.actor(token),PASSWORD,"注销并删除我的全部个人数据")
    result=backups.restore(store,keys,journal,original["id"],identity)
    assert result["erased_users_applied"]==1
    assert not store.one("SELECT id FROM users WHERE id=?",(uid,))
    assert not store.one("SELECT id FROM personal_reports WHERE id=?",(report["report_id"],))
    assert not store.query("SELECT * FROM model_credentials WHERE envelope_json IS NOT NULL")
    assert not store.query("SELECT * FROM sessions")
    assert not get_prefs(store,admin).automatic_ai
    assert store.one("SELECT needs_reconnect FROM model_profiles WHERE user_id=?",(admin,))["needs_reconnect"]


def get_prefs(store,uid):
    from backend.cloud.preferences import get_preferences
    return get_preferences(store,uid)


def age_config(store,tmp_path):
    import subprocess
    age=Path(shutil.which("age") or ROOT/".tmp"/"tools"/"age"/"age.exe")
    if not age.is_file():
        pytest.skip("此测试需要标准 age 工具")
    keygen=age.with_name("age-keygen" if age.suffix!=".exe" else "age-keygen.exe");identity=tmp_path/"ops-identity.txt"
    assert subprocess.run([str(keygen),"-o",str(identity)],capture_output=True).returncode==0
    recipient=subprocess.run([str(keygen),"-y",str(identity)],capture_output=True,text=True).stdout.strip()
    store.cfg=replace(store.cfg,age_bin=str(age),age_recipient=recipient)
    return identity


def test_copy_only_migration_and_new_user_empty(cloud,tmp_path,bars,valid_item,monkeypatch):
    from backend import db,collectors,research,ai,backtest
    from backend.simulation import create_account
    from backend.cloud.migrate import migrate
    from backend.cloud.backups import file_hash
    store,keys,auth,journal,admin,uid=cloud
    # Migration is deliberately unavailable after registration has opened.
    with pytest.raises(ValueError):
        migrate(store,keys,journal,tmp_path,admin)
    with store.tx(immediate=True) as c:
        c.execute("DELETE FROM users WHERE id=?",(uid,))
    age_config(store,tmp_path)
    collectors.save_bars(valid_item['id'],bars)
    monkeypatch.setattr(ai,"explain",lambda *args:(None,"测试规则报告"))
    report=research.generate("standard:test","standard")
    create_account("原模拟账户",10000)
    experiment=backtest.create(backtest.Parameters(instrument_id=valid_item['id'],initial_cash=10000,strict=False))
    before_db=db.query("SELECT * FROM reports")
    before_bytes=(tmp_path/"reports"/(report["report_id"]+".md")).read_bytes()
    result=migrate(store,keys,journal,tmp_path,admin)
    assert result["counts"]["reports"]==1 and result["counts"]["accounts"]==1
    assert db.query("SELECT * FROM reports")==before_db
    saved=workspace.owned(store,"personal_reports",admin,report["report_id"])
    assert (store.cfg.data/saved["markdown_path"]).read_bytes()==before_bytes
    assert json.loads(saved['payload_json'])['source_facts']['snapshot_id']==json.loads(before_db[0]['payload_json'])['snapshot_id']
    assert workspace.reproduce(store,admin,experiment["id"])["identical"]
    assert migrate(store,keys,journal,tmp_path,admin)["already_migrated"]
    new=auth.new_user("after@example.test",PASSWORD,verified=True)
    assert not workspace.report_list(store,new) and simulation.portfolio(store,new)["accounts"]==[]
    assert not store.query("SELECT * FROM user_watchlist WHERE user_id=?",(new,))
    assert store.query("SELECT * FROM bars") and not store.query("SELECT * FROM reports")
    assert not get_prefs(store,admin).automatic_ai


def test_backup_reauth_grant_bound_single_use_and_retention(cloud,tmp_path):
    store,keys,auth,journal,admin,uid=cloud;age_config(store,tmp_path)
    record=backups.create(store,keys,journal)
    with TestClient(create_app(store.cfg)) as c:
        login(c,"admin@example.test")
        url="/api/admin/backups/"+record["id"]
        assert c.post(url+"/download").status_code==403
        grant=c.post(url+"/reauth",json={"password":PASSWORD}).json()["grant"]
        headers={"X-Reauth-Grant":grant}
        assert c.post(url+"/download",headers=headers).status_code==200
        assert c.post(url+"/download",headers=headers).status_code==403
        grant=c.post(url+"/reauth",json={"password":PASSWORD}).json()["grant"]
        login(c,"admin@example.test")
        assert c.post(url+"/download",headers={"X-Reauth-Grant":grant}).status_code==403
        login(c)
        assert c.get("/api/admin/backups").status_code==403
    # A signed registry row beyond retention is removed from downloadable storage.
    row=next(r for r in journal.backups() if r['id']==record['id']);row.pop('id');row['created_at']='2000-01-01T00:00:00+00:00'
    encoded=json_text(row)
    with journal.tx() as c:
        c.execute("UPDATE registry SET payload_json=?,signature=? WHERE id=?",(encoded,keys.sign(encoded),record['id']))
    backups.prune(store,journal)
    assert not (store.cfg.data/'backups'/(record['id']+'.age')).exists()


def test_restore_refuses_missing_journal_or_corrupt_ciphertext(cloud,tmp_path):
    store,keys,auth,journal,admin,uid=cloud;identity=age_config(store,tmp_path)
    record=backups.create(store,keys,journal);file=store.cfg.data/'backups'/(record['id']+'.age')
    original=file.read_bytes();file.write_bytes(original+b'corruption')
    with pytest.raises(ValueError):
        backups.restore(store,keys,journal,record['id'],identity)
    assert store.active(uid)
    file.write_bytes(original);journal.head.unlink()
    with pytest.raises(RuntimeError):
        backups.restore(store,keys,journal,record['id'],identity)
    assert store.active(uid)


def test_personal_account_backtest_profile_idor_matrix(cloud,bars):
    from backend.instruments import InstrumentInput
    from backend.backtest import Parameters
    from backend.cloud import market
    store,keys,auth,journal,admin,uid=cloud
    account=simulation.create_account(store,uid,"仅属于我",10000)
    market.add_watch(store,uid,InstrumentInput(symbol='600001',market='SH'))
    market.import_bars(store,'SH:600001',{'bars':bars})
    experiment=workspace.create_backtest(store,uid,Parameters(instrument_id='SH:600001',initial_cash=10000,strict=False))
    p=profile(cloud)
    with TestClient(create_app(store.cfg)) as c:
        login(c,"admin@example.test")
        for path in (f"/accounts/{account['id']}",f"/backtests/{experiment['id']}",f"/backtests/{experiment['id']}/export"):
            assert c.get('/api/me'+path).status_code==404
        assert c.post('/api/me/backtests/'+experiment['id']+'/reproduce').status_code==404
        assert c.delete('/api/me/model-profiles/'+p['id']).status_code==404
        assert c.put('/api/me/model-profiles/'+p['id'],json={'name':'盗用','provider':'ollama'}).status_code==404
        assert c.post('/api/me/model-profiles/default',json={'id':p['id']}).status_code==404
        assert c.get('/api/me/backtests').json()==[] and c.get('/api/me/simulation').json()['accounts']==[]
        login(c)
        assert c.get('/api/admin/users').status_code==403
        assert c.post('/api/admin/instruments/SH:600001/verify',json={}).status_code==403


def test_twenty_distinct_credentials_two_concurrent_ai_jobs(cloud):
    import time
    store,keys,auth,journal,admin,uid=cloud;users=[uid]+[auth.new_user(f"parallel{n}@example.test",PASSWORD,verified=True) for n in range(19)]
    expected={};active=0;peak=0;lock=threading.Lock();received=[]
    for n,user in enumerate(users):
        key=f'test-owner-key-{n:04d}'
        p=models.save_profile(store,keys,user,models.ProfileInput(name='本人模型',api_key=key));models.set_default(store,user,p['id']);expected[p['id']]=key
    submission=[]
    with TestClient(create_app(store.cfg)) as client:
        def submit(user):
            email=store.one('SELECT email FROM users WHERE id=?',(user,))['email']
            token,csrf,_=auth.login(email,PASSWORD,'load-'+user)
            at=time.perf_counter()
            response=client.post('/api/me/jobs',json={'kind':'standard'},headers={'Origin':'http://testserver','X-CSRF-Token':csrf,'Cookie':f'stockai_dev_session={token}; stockai_dev_csrf={csrf}'})
            submission.append(time.perf_counter()-at)
            assert response.status_code==200,response.text
        with ThreadPoolExecutor(4) as executor:
            list(executor.map(submit,users))
    def transport(fixed,messages,key):
        nonlocal active,peak
        with lock:
            active+=1;peak=max(peak,active);received.append((fixed['profile_id'],key))
        assert key==expected[fixed['profile_id']]
        time.sleep(.1)
        with lock:
            active-=1
        return GOOD,{'total_tokens':10},'test-owned'
    started=time.perf_counter();latencies=[]
    def worker(name):
        while (job:=jobs.claim(store,name)):
            at=time.perf_counter();result=jobs.run(store,keys,job,transport);latencies.append(time.perf_counter()-at)
            assert result.get('ai_status')=='generated',result
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(worker,['A','B']))
    assert peak==2 and len(received)==20 and len({k for _,k in received})==20
    metrics={'users':20,'concurrent_model_peak':peak,'reports':len(received),'http_submissions':len(submission),'max_http_accept_seconds':round(max(submission),3),'elapsed_seconds':round(time.perf_counter()-started,3),'max_task_seconds':round(max(latencies),3),'mean_task_seconds':round(sum(latencies)/20,3),'external_provider':'simulated 100ms response, no external key or quota'}
    evidence=ROOT/'.tmp'/'cloud-load-metrics.json';evidence.parent.mkdir(exist_ok=True);evidence.write_text(json_text(metrics),encoding='utf-8')


def test_owned_simulation_fills_and_idempotent_ledger(cloud,bars,monkeypatch):
    from backend.cloud import market
    from backend.instruments import InstrumentInput
    from backend.simulation import OrderInput
    store,keys,auth,journal,admin,uid=cloud
    item=market.add_watch(store,uid,InstrumentInput(symbol='600001',market='SH'))
    evidence={k:'https://www.sse.com.cn/test' for k in ('listing_url','status_url','announcement_url')};evidence['review_note']='隔离测试'
    market.verify(store,item['id'],{'name':'测试证券','status':'active','risk_flag':'clear','rules':{**item['rules'],'verified':True,'source_url':'https://www.sse.com.cn/rules'},'evidence':evidence})
    market.import_bars(store,item['id'],{'bars':bars})
    monkeypatch.setattr(market,'calendar',lambda *a,**kw:{'calendar_verified':True,'last_completed_session':bars[-1]['ts'][:10]})
    account=simulation.create_account(store,uid,'虚拟测试',10000)
    payload=OrderInput(client_key='owned-test-order',account_id=account['id'],instrument_id=item['id'],side='buy',quantity=100,limit_price=6)
    queued=simulation.order(store,uid,payload)
    assert not simulation.match(store,uid)  # Historical bars cannot fill a new order.
    store.execute("UPDATE sim_orders SET created_at=? WHERE id=? AND user_id=?",(bars[0]['ts'].replace('15:00','09:00'),queued['id'],uid))
    assert len(simulation.match(store,uid))==1
    assert simulation.order(store,uid,payload)['status']=='filled'
    assert simulation.match(store,uid)==[]
    ledger=store.query("SELECT * FROM sim_ledger WHERE user_id=? AND order_id=?",(uid,queued['id']))
    assert len(ledger)==1
    assert not simulation.portfolio(store,admin)['orders']
    with pytest.raises(AuthError):
        simulation.cancel(store,admin,queued['id'])


def test_interrupted_directory_swap_rolls_back_before_initialize(cloud,tmp_path):
    from backend.cloud.lifecycle import recover_swap
    from backend.cloud.crypto import atomic_private
    store,keys,auth,journal,admin,uid=cloud;site=store.site_id
    prepared=tmp_path/('.stockai-restore-'+site+'-test')/'prepared';prepared.mkdir(parents=True)
    old=tmp_path/('.stockai-replaced-'+site+'-test')
    value={'target':str(store.cfg.data.resolve()),'old':str(old.resolve()),'prepared':str(prepared.resolve()),'site_id':site,'key_version':keys.active};value['signature']=keys.sign(json_text(value))
    atomic_private(store.cfg.erasures/'pending-swap.json',json_text(value).encode())
    store.cfg.data.rename(old)
    recover_swap(store.cfg,keys)
    assert store.active(uid) and not old.exists()
    assert not (store.cfg.erasures/'pending-swap.json').exists()


@pytest.mark.parametrize('status,body,expected,calls',[(200,{'choices':[{'message':{'content':GOOD}}],'usage':{'total_tokens':10,'unsafe':'drop'}},None,1),(401,{'error':{'message':'secret provider details'}},'INVALID_KEY',1),(402,{},'QUOTA_EXCEEDED',1),(429,{},'RATE_LIMITED',2),(503,{},'TEMPORARY_PROVIDER_ERROR',2),(200,{'base_resp':{'status_code':1008}},'QUOTA_EXCEEDED',1),(302,{},'INCOMPATIBLE_PROVIDER',1)])
def test_actual_minimax_protocol_and_classified_errors(cloud,monkeypatch,status,body,expected,calls):
    store,keys,auth,journal,admin,uid=cloud;profile(cloud);sent=[]
    class Response:
        def __init__(self):
            self.status=status
        def read(self,*args):
            return json.dumps(body).encode()
        def getheader(self,*args):
            return 'request-fixture'
    class Sock:
        def settimeout(self,*args):
            return None
    class Connection:
        def __init__(self,host,port,ip,timeout):
            assert host=='api.minimax.cn' and ip=='1.1.1.1';self.sock=Sock()
        def connect(self):
            return None
        def request(self,method,path,body,headers):
            sent.append((json.loads(body),headers,path))
        def getresponse(self):
            return Response()
        def close(self):
            return None
    monkeypatch.setattr(models,'PinnedHTTPS',Connection)
    result=run_job(cloud)
    assert len(sent)==calls
    payload,headers,path=sent[0]
    assert headers['Authorization']=='Bearer test-key-owner-1234' and path=='/v1/chat/completions'
    assert payload['reasoning_split'] and payload['reasoning_effort']=='low' and payload['max_completion_tokens']==8192
    assert not {'options','think','num_ctx'}&set(payload)
    report=workspace.report_detail(store,uid,result['report_id'])['payload']
    assert report['ai_error_code']==expected
    assert 'secret provider details' not in json_text(report) and 'test-key-owner-1234' not in json_text(report)


def test_expired_revoked_invites_and_validation_redacts_inputs(cloud):
    store,keys,auth,journal,admin,uid=cloud
    invitation=auth.invitation(admin)
    store.execute("UPDATE invitations SET expires_at='2000-01-01' WHERE id=?",(invitation['id'],))
    with pytest.raises(AuthError):
        auth.register('expired@example.test',PASSWORD,invitation['code'],'expired')
    invitation=auth.invitation(admin);store.execute("UPDATE invitations SET revoked_at=? WHERE id=?",(stamp(),invitation['id']))
    with pytest.raises(AuthError):
        auth.register('revoked@example.test',PASSWORD,invitation['code'],'revoked')
    with TestClient(create_app(store.cfg)) as c:
        login(c)
        response=c.post('/api/me/model-profiles',json={'name':'x'*100,'api_key':'MUST-NOT-ECHO-SECRET'})
        assert response.status_code==422 and 'MUST-NOT-ECHO-SECRET' not in response.text
        assert c.post('/api/me/jobs',json={},headers={'Origin':'https://attacker.example'}).status_code==403


def test_production_cookie_and_sqlite_version_gate(cloud):
    import sqlite3
    from backend.cloud.store import wal_fixed
    store,keys,auth,journal,admin,uid=cloud
    cfg=replace(store.cfg,dev=False,public_url='https://testserver')
    if not wal_fixed(sqlite3.sqlite_version):
        with pytest.raises(RuntimeError,match='SQLite'):
            Store(cfg).initialize()
        return
    with TestClient(create_app(cfg),base_url='https://testserver') as c:
        csrf=c.get('/api/auth/csrf').json()['csrf_token']
        response=c.post('/api/auth/login',json={'email':'user@example.test','password':PASSWORD},headers={'Origin':'https://testserver','X-CSRF-Token':csrf})
        assert response.status_code==200,response.text
        cookie=response.headers.get_list('set-cookie')[0]
        assert '__Host-stockai_session=' in cookie and 'HttpOnly' in cookie and 'Secure' in cookie and 'SameSite=lax' in cookie and 'Path=/' in cookie
        assert c.get('/api/me/dashboard').status_code==200


def test_watch_import_preserves_pin_and_is_atomic(cloud):
    from backend.cloud import market
    store,keys,auth,journal,admin,uid=cloud
    rows=[{'symbol':'600001','market':'SH','group_name':'我的私有分组','pinned':'0'},{'symbol':'600002','market':'SH','group_name':'置顶','pinned':'1'}]
    assert market.import_watch(store,uid,rows)['imported']==2
    assert [r['pinned'] for r in store.query('SELECT * FROM user_watchlist WHERE user_id=? ORDER BY instrument_id',(uid,))]==[0,1]
    with pytest.raises(ValueError):
        market.import_watch(store,uid,[{'symbol':'600003','market':'SH'},{'symbol':'BAD','market':'SH'}])
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=? AND instrument_id=?',(uid,'SH:600003'))


def test_server_ollama_uses_only_site_address(cloud,monkeypatch):
    store,keys,auth,journal,admin,uid=cloud
    store.cfg=replace(store.cfg,ollama_enabled=True,ollama_url='http://127.0.0.1:11434')
    p=models.save_profile(store,keys,uid,models.ProfileInput(name='站点本地模型',provider='ollama',base_url='http://personal-pc.invalid:11434',model='site-model'))
    models.set_default(store,uid,p['id']);seen=[]
    class Sock:
        def settimeout(self,*args):
            return None
    class Response:
        status=200
        def read(self,*args):
            return json.dumps({'message':{'content':GOOD},'eval_count':10}).encode()
    class Connection:
        def __init__(self,host,port,timeout):
            assert host=='127.0.0.1' and port==11434;self.sock=Sock()
        def connect(self):
            return None
        def request(self,method,path,body,headers):
            seen.append((path,json.loads(body),headers))
        def getresponse(self):
            return Response()
        def close(self):
            return None
    monkeypatch.setattr(models.http.client,'HTTPConnection',Connection)
    result=run_job(cloud)
    assert result['ai_status']=='generated' and len(seen)==1
    assert seen[0][0]=='/api/chat' and seen[0][1]['model']=='site-model' and seen[0][1]['think'] is False
    assert 'Authorization' not in seen[0][2]


def test_provider_cannot_echo_plaintext_key_into_report_or_request_id(cloud):
    store,keys,auth,journal,admin,uid=cloud
    secret='testletterssecretwithoutdigits'
    p=models.save_profile(store,keys,uid,models.ProfileInput(name='凭据回显测试',api_key=secret));models.set_default(store,uid,p['id'])
    first=run_job(cloud,transport=lambda *args:(GOOD,{},secret))
    assert first['ai_status']=='generated'
    assert store.one('SELECT provider_request_id FROM model_calls WHERE user_id=?',(uid,))['provider_request_id']==''
    reflected=GOOD.replace('研究事实已核对',secret)
    result=run_job(cloud,kind='supplement',parent_id=first['report_id'],transport=lambda *args:(reflected,{},secret))
    assert result['ai_status']=='fallback'
    assert secret not in json_text(workspace.report_detail(store,uid,result['report_id']))
