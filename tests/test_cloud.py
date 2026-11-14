# 验证公网多用户隔离、模型凭据、队列并发、删除权威记录和加密备份恢复。
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


# 建立临时公网存储、密钥、认证组件及管理员和普通用户，隔离外部网络。
@pytest.fixture
def cloud(tmp_path,monkeypatch):
    monkeypatch.setattr(models,"public_ips",lambda *args:["1.1.1.1"])
    cfg=CloudConfig(data=tmp_path/"public",erasures=tmp_path/"erasures",key_file=tmp_path/"secrets"/"keys.json",public_url="http://testserver",dev=True,background=False)
    store=Store(cfg).initialize();keys=Keyring.create(cfg.key_file);auth=Auth(store,keys)
    journal=ErasureJournal(store,keys);journal.create()
    admin=auth.create_admin("admin@example.test",PASSWORD)
    uid=auth.new_user("user@example.test",PASSWORD,verified=True)
    return store,keys,auth,journal,admin,uid


# 通过测试客户端获取 CSRF 并登录，设置后续请求所需的来源与令牌。
def login(client,email="user@example.test"):
    csrf=client.get("/api/auth/csrf").json()["csrf_token"]
    client.headers.update({"Origin":"http://testserver","X-CSRF-Token":csrf})
    r=client.post("/api/auth/login",json={"email":email,"password":PASSWORD});assert r.status_code==200,r.text
    client.headers["X-CSRF-Token"]=client.cookies["stockai_dev_csrf"]
    return r.json()["user"]


# 为指定测试用户创建模型配置和测试凭据。
def profile(cloud,uid=None,**kwargs):
    store,keys,_,_,_,user=cloud;uid=uid or user
    value=models.ProfileInput(name="个人测试配置",provider="minimax",api_key="test-key-owner-1234",**kwargs)
    row=models.save_profile(store,keys,uid,value);models.set_default(store,uid,row["id"])
    return row


# 提交并领取一项个人任务，通过可替换传输运行并返回结果。
def run_job(cloud,uid=None,kind="standard",transport=None,**kwargs):
    store,keys,_,_,_,user=cloud;enqueued=jobs.enqueue(store,uid or user,kind,**kwargs);job=jobs.claim(store,"test-worker")
    assert job and job["id"]==enqueued["id"]
    return jobs.run(store,keys,job,transport)


# 验证邀请注册、邮箱验证、重置密码、会话及 CSRF 的完整流程。
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


# 验证并发注册时同一邀请码最多成功消费一次。
def test_invite_atomic_single_use(cloud):
    store,_,auth,_,admin,_=cloud;code=auth.invitation(admin)["code"]
    # 在线程中尝试消费同一邀请码，并记录成功或失败结果。
    def register(n):
        try:
            auth.register(f"race{n}@example.test",PASSWORD,code,f"peer{n}");return True
        except AuthError:
            return False
    with ThreadPoolExecutor(2) as executor:
        assert sum(executor.map(register,range(2)))==1


# 验证模型配置归属、凭据密文和任务默认配置版本快照。
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


# 验证模型事实投影不包含用户身份、备注或账户名称。
def test_projection_has_no_identity_notes_account_names(cloud):
    fixed={"allow_summary":0,"allow_trades":0}
    facts={"mode":"full","candidates":[],"sources":[],"watchlist":[{"instrument_id":"SH:600001","group_name":"SECRET-GROUP","official_name":"官方证券"}],"simulation":{"accounts":[{"id":"secret-id","name":"SECRET-ACCOUNT","cash":"1000","positions":[{"instrument_id":"SH:600001","quantity":100,"note":"SECRET-NOTE"}]}],"recent_fills":[{"instrument_id":"SH:600001","quantity":100,"note":"SECRET-TRADE"}]},"email":"SECRET-EMAIL","free_note":"SECRET-FREE"}
    text=json_text(models.project_input(facts,fixed));assert "simulation_summary" not in text and "trade_details" not in text and "SECRET" not in text
    text=json_text(models.project_input(facts,{"allow_summary":1,"allow_trades":1}));assert "simulation_summary" in text and "trade_details" in text and "SECRET" not in text and "secret-id" not in text


# 验证规则降级、补充解释版本和跨用户报告访问限制。
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


# 验证不同错误码及结果不确定标记对应的有限重试策略。
@pytest.mark.parametrize("code,retryable,uncertain,calls",[("RATE_LIMITED",True,False,2),("TEMPORARY_PROVIDER_ERROR",True,False,2),("CONNECT_FAILED",True,False,2),("INVALID_KEY",False,False,1),("QUOTA_EXCEEDED",False,False,1),("UNCERTAIN_TIMEOUT",False,True,1),("INVALID_OUTPUT",False,False,1)])
def test_model_retry_policy(cloud,code,retryable,uncertain,calls):
    profile(cloud);seen=[]
    # 记录调用次数并模拟指定的可重试或不确定模型故障。
    def transport(*args):
        seen.append(args);raise models.ModelError(code,retryable,uncertain)
    result=run_job(cloud,transport=transport)
    assert result["ai_status"]=="fallback" and len(seen)==calls,result
    payload=workspace.report_detail(cloud[0],cloud[-1],result["report_id"])["payload"]
    assert payload["ai_error_code"]==code


# 验证模型输出校验失败不会再次消耗调用额度。
def test_bad_model_output_no_retry(cloud):
    profile(cloud);seen=[]
    # 模拟包含违规承诺或编造事实的模型输出。
    def transport(*args):
        seen.append(True);return GOOD.replace("研究事实已核对","保证盈利123"),{},""
    result=run_job(cloud,transport=transport)
    assert result["ai_status"]=="fallback" and len(seen)==1


# 验证凭据、数据授权或账号撤销后会在发送前阻断请求。
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


# 验证账号删除后进行中的调用与服务重启都不能恢复已删数据。
def test_erasure_blocks_inflight_writeback_and_restart(cloud):
    store,keys,auth,journal,admin,uid=cloud;profile(cloud)
    jobs.enqueue(store,uid);job=jobs.claim(store,"inflight");entered=threading.Event();release=threading.Event()
    # 阻塞模拟模型响应，构造请求进行中删除账号的并发窗口。
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


# 验证默认自动任务仅用规则，且不会补造过去日期的报告。
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


# 验证重启恢复时不重新发送结果不确定的已发送调用。
def test_restart_sent_call_is_not_resent(cloud):
    store,keys,_,_,_,uid=cloud;p=profile(cloud)
    jobs.enqueue(store,uid);job=jobs.claim(store,"crashed")
    facts=workspace.facts(store,uid,"standard");snap=json.loads(job["snapshot_json"]);snap["facts"]=facts
    store.execute("UPDATE personal_jobs SET snapshot_json=? WHERE id=?",(json_text(snap),job["id"]))
    fixed=snap["profile"]
    store.execute("INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,created_at) VALUES('crashed-call',?,?,?,?,1,'sent',?)",(uid,job["id"],p["id"],fixed["version"],stamp()))
    jobs.recover(store);job=jobs.claim(store,"restart");seen=[];result=jobs.run(store,keys,job,lambda *args:seen.append(True))
    assert result["ai_status"]=="fallback" and not seen


# 验证二十个用户在双工作线程限制下仍按用户公平调度。
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


# 验证模型出站地址拒绝非 HTTPS、特殊地址和不安全 URL。
@pytest.mark.parametrize("url",["http://api.example.com/v1","https://127.0.0.1/v1","https://[::1]/v1","https://169.254.169.254/v1","https://user:key@example.com/v1","https://example.com/v1?key=secret","https://example.com/#frag"])
def test_cloud_address_rejects_unsafe(url):
    with pytest.raises(models.ModelError):
        models.cloud_address(url,resolve=False)


# 验证域名只要解析到任一内网地址就会整体拒绝。
def test_dns_all_addresses_must_be_public(monkeypatch):
    monkeypatch.setattr(socket,"getaddrinfo",lambda *a,**k:[(socket.AF_INET,1,6,"",("1.1.1.1",443)),(socket.AF_INET,1,6,"",("10.0.0.1",443))])
    with pytest.raises(models.ModelError):
        models.public_ips("provider.example")


# 验证加密备份恢复后重新应用账号删除，并要求旧凭据重新连接。
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


# 读取指定测试用户的已校验个人偏好。
def get_prefs(store,uid):
    from backend.cloud.preferences import get_preferences
    return get_preferences(store,uid)


# 准备测试 age 加密身份、公钥及备份配置。
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


# 验证本地迁移只复制数据，新用户不会自动继承他人研究资料。
def test_copy_only_migration_and_new_user_empty(cloud,tmp_path,bars,valid_item,monkeypatch):
    from backend import db,collectors,research,ai,backtest
    from backend.simulation import create_account
    from backend.cloud.migrate import migrate
    from backend.cloud.backups import file_hash
    store,keys,auth,journal,admin,uid=cloud
    # 开放普通注册后不再允许执行首次本地迁移，避免影响已有用户数据。
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


# 验证备份授权绑定资源且只能使用一次，并检查保留期清理。
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
    # 签名登记记录仍可审计，但超过保留期的备份从可下载存储中移除。
    row=next(r for r in journal.backups() if r['id']==record['id']);row.pop('id');row['created_at']='2000-01-01T00:00:00+00:00'
    encoded=json_text(row)
    with journal.tx() as c:
        c.execute("UPDATE registry SET payload_json=?,signature=? WHERE id=?",(encoded,keys.sign(encoded),record['id']))
    backups.prune(store,journal)
    assert not (store.cfg.data/'backups'/(record['id']+'.age')).exists()


# 验证权威删除账本缺失或备份密文损坏时拒绝恢复。
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


# 验证账户、回测和模型配置各接口都拒绝跨用户访问。
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


# 验证二十组独立凭据正确分流，且全站模型任务最多并发两项。
def test_twenty_distinct_credentials_two_concurrent_ai_jobs(cloud):
    import time
    store,keys,auth,journal,admin,uid=cloud;users=[uid]+[auth.new_user(f"parallel{n}@example.test",PASSWORD,verified=True) for n in range(19)]
    expected={};active=0;peak=0;lock=threading.Lock();received=[];first_pair=threading.Barrier(2,timeout=15)
    for n,user in enumerate(users):
        key=f'test-owner-key-{n:04d}'
        p=models.save_profile(store,keys,user,models.ProfileInput(name='本人模型',api_key=key));models.set_default(store,user,p['id']);expected[p['id']]=key
    submission=[]
    with TestClient(create_app(store.cfg)) as client:
        # 以指定用户登录并提交任务，记录应使用的个人凭据。
        def submit(user):
            email=store.one('SELECT email FROM users WHERE id=?',(user,))['email']
            token,csrf,_=auth.login(email,PASSWORD,'load-'+user)
            at=time.perf_counter()
            response=client.post('/api/me/jobs',json={'kind':'standard'},headers={'Origin':'http://testserver','X-CSRF-Token':csrf,'Cookie':f'stockai_dev_session={token}; stockai_dev_csrf={csrf}'})
            submission.append(time.perf_counter()-at)
            assert response.status_code==200,response.text
        with ThreadPoolExecutor(4) as executor:
            list(executor.map(submit,users))
    # 跟踪正在运行的模拟调用，核对凭据与峰值并发数。
    def transport(fixed,messages,key):
        nonlocal active,peak
        with lock:
            active+=1;peak=max(peak,active);received.append((fixed['profile_id'],key))
            wait_for_peer=len(received)<=2
        try:
            assert key==expected[fixed['profile_id']]
            # 首对调用明确重叠，不依赖数据库准备恰好落入 100ms 响应窗口。
            if wait_for_peer:first_pair.wait()
            time.sleep(.1)
            return GOOD,{'total_tokens':10},'test-owned'
        finally:
            with lock:
                active-=1
    started=time.perf_counter();latencies=[]
    # 循环领取并执行测试队列，模拟实际后台工作线程。
    def worker(name):
        while (job:=jobs.claim(store,name)):
            at=time.perf_counter();result=jobs.run(store,keys,job,transport);latencies.append(time.perf_counter()-at)
            assert result.get('ai_status')=='generated',result
    with ThreadPoolExecutor(2) as executor:
        list(executor.map(worker,['A','B']))
    assert peak==2 and len(received)==20 and len({k for _,k in received})==20
    metrics={'users':20,'concurrent_model_peak':peak,'reports':len(received),'http_submissions':len(submission),'max_http_accept_seconds':round(max(submission),3),'elapsed_seconds':round(time.perf_counter()-started,3),'max_task_seconds':round(max(latencies),3),'mean_task_seconds':round(sum(latencies)/20,3),'external_provider':'simulated 100ms response, no external key or quota'}
    evidence=ROOT/'.tmp'/'cloud-load-metrics.json';evidence.parent.mkdir(exist_ok=True);evidence.write_text(json_text(metrics),encoding='utf-8')


# 验证本人模拟委托撮合、跨用户隔离及资金流水幂等。
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
    assert not simulation.match(store,uid)  # 新建委托不能利用创建前的历史 K 线成交。
    store.execute("UPDATE sim_orders SET created_at=? WHERE id=? AND user_id=?",(bars[0]['ts'].replace('15:00','09:00'),queued['id'],uid))
    assert len(simulation.match(store,uid))==1
    assert simulation.order(store,uid,payload)['status']=='filled'
    assert simulation.match(store,uid)==[]
    ledger=store.query("SELECT * FROM sim_ledger WHERE user_id=? AND order_id=?",(uid,queued['id']))
    assert len(ledger)==1
    assert not simulation.portfolio(store,admin)['orders']
    with pytest.raises(AuthError):
        simulation.cancel(store,admin,queued['id'])


# 验证目录替换中断时先回滚，再允许初始化数据库。
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


# 验证实际请求字段、响应解析和供应商错误分类。
@pytest.mark.parametrize('status,body,expected,calls',[(200,{'choices':[{'message':{'content':GOOD}}],'usage':{'total_tokens':10,'unsafe':'drop'}},None,1),(401,{'error':{'message':'secret provider details'}},'INVALID_KEY',1),(402,{},'QUOTA_EXCEEDED',1),(429,{},'RATE_LIMITED',2),(503,{},'TEMPORARY_PROVIDER_ERROR',2),(200,{'base_resp':{'status_code':1008}},'QUOTA_EXCEEDED',1),(302,{},'INCOMPATIBLE_PROVIDER',1)])
def test_actual_minimax_protocol_and_classified_errors(cloud,monkeypatch,status,body,expected,calls):
    store,keys,auth,journal,admin,uid=cloud;profile(cloud);sent=[]
    # 模拟供应商 HTTP 响应，隔离真实网络并控制测试返回内容。
    class Response:
        # 初始化模拟 HTTP 状态码。
        def __init__(self):
            self.status=status
        # 返回预设 JSON 响应字节，供协议解析逻辑读取。
        def read(self,*args):
            return json.dumps(body).encode()
        # 返回固定请求标识，供响应元数据解析使用。
        def getheader(self,*args):
            return 'request-fixture'
    # 提供模拟套接字，仅实现被测传输实际需要的接口。
    class Sock:
        # 接受超时设置，避免测试依赖真实套接字。
        def settimeout(self,*args):
            return None
    # 模拟 HTTP 连接，核对目标地址并捕获请求载荷。
    class Connection:
        # 核对 MiniMax 主机、端口与固定公网 IP，并配置模拟套接字。
        def __init__(self,host,port,ip,timeout):
            assert host=='api.minimax.cn' and ip=='1.1.1.1';self.sock=Sock()
        # 提供无网络副作用的模拟连接操作。
        def connect(self):
            return None
        # 保存请求路径、请求头和 JSON 内容以供断言。
        def request(self,method,path,body,headers):
            sent.append((json.loads(body),headers,path))
        # 返回当前用例配置的模拟 HTTP 响应。
        def getresponse(self):
            return Response()
        # 提供模拟连接关闭接口，保证测试覆盖资源收尾路径。
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


# 验证过期或撤销邀请失效，且校验响应不会回显私密输入。
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


# 验证生产 Cookie 属性与 SQLite 修复版本的启动门槛。
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


# 验证置顶写入后，自选列表和总览立刻读到新状态。
def test_patch_watch_pin_is_immediately_visible(cloud):
    from backend.cloud import market
    from backend.instruments import InstrumentInput
    store,keys,auth,journal,admin,uid=cloud
    market.add_watch(store,uid,InstrumentInput(symbol='600001',market='SH',group_name='观察'))
    with TestClient(create_app(store.cfg)) as c:
        login(c)
        ident='SH:600001'
        assert c.patch('/api/me/instruments/'+ident,json={'pinned':True}).status_code==200
        rows=c.get('/api/me/watchlist').json()
        assert rows[0]['id']==ident and rows[0]['pinned'] is True
        item=next(r for r in c.get('/api/me/dashboard').json()['watchlist'] if r['id']==ident)
        assert item['pinned'] is True
        assert c.patch('/api/me/instruments/'+ident,json={'pinned':False}).status_code==200
        assert c.get('/api/me/watchlist').json()[0]['pinned'] is False


# 验证导入保留已有置顶状态，且非法批次不会部分写入。
def test_watch_import_preserves_pin_and_is_atomic(cloud):
    from backend.cloud import market
    store,keys,auth,journal,admin,uid=cloud
    rows=[{'symbol':'600001','market':'SH','group_name':'我的私有分组','pinned':'0'},{'symbol':'600002','market':'SH','group_name':'置顶','pinned':'1'}]
    assert market.import_watch(store,uid,rows)['imported']==2
    assert [r['pinned'] for r in store.query('SELECT * FROM user_watchlist WHERE user_id=? ORDER BY instrument_id',(uid,))]==[0,1]
    with pytest.raises(ValueError):
        market.import_watch(store,uid,[{'symbol':'600003','market':'SH'},{'symbol':'BAD','market':'SH'}])
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=? AND instrument_id=?',(uid,'SH:600003'))


# 验证服务器 Ollama 只能使用站点配置地址，不能被个人配置覆盖。
def test_server_ollama_uses_only_site_address(cloud,monkeypatch):
    store,keys,auth,journal,admin,uid=cloud
    store.cfg=replace(store.cfg,ollama_enabled=True,ollama_url='http://127.0.0.1:11434')
    p=models.save_profile(store,keys,uid,models.ProfileInput(name='站点本地模型',provider='ollama',base_url='http://personal-pc.invalid:11434',model='site-model'))
    models.set_default(store,uid,p['id']);seen=[]
    # 提供模拟套接字，仅实现被测传输实际需要的接口。
    class Sock:
        # 接受超时设置，避免测试依赖真实套接字。
        def settimeout(self,*args):
            return None
    # 模拟供应商 HTTP 响应，隔离真实网络并控制测试返回内容。
    class Response:
        status=200
        # 返回预设 JSON 响应字节，供协议解析逻辑读取。
        def read(self,*args):
            return json.dumps({'message':{'content':GOOD},'eval_count':10}).encode()
    # 模拟 HTTP 连接，核对目标地址并捕获请求载荷。
    class Connection:
        # 核对站点 Ollama 主机和端口，并配置模拟套接字。
        def __init__(self,host,port,timeout):
            assert host=='127.0.0.1' and port==11434;self.sock=Sock()
        # 提供无网络副作用的模拟连接操作。
        def connect(self):
            return None
        # 保存请求路径、请求头和 JSON 内容以供断言。
        def request(self,method,path,body,headers):
            seen.append((path,json.loads(body),headers))
        # 返回当前用例配置的模拟 HTTP 响应。
        def getresponse(self):
            return Response()
        # 提供模拟连接关闭接口，保证测试覆盖资源收尾路径。
        def close(self):
            return None
    monkeypatch.setattr(models.http.client,'HTTPConnection',Connection)
    result=run_job(cloud)
    assert result['ai_status']=='generated' and len(seen)==1
    assert seen[0][0]=='/api/chat' and seen[0][1]['model']=='site-model' and seen[0][1]['think'] is False
    assert 'Authorization' not in seen[0][2]


# 验证供应商回显的明文 Key 不会进入报告或请求标识。
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


# 新增和批量导入自动补全共享名称，用户输入不覆盖共享资料。
def test_watch_names_queue_automatically_and_coalesce(cloud):
    from backend.cloud import market
    from backend.instruments import InstrumentInput
    store,_,_,_,admin,uid=cloud
    item=market.add_watch(store,uid,InstrumentInput(symbol='603126',market='SH',name='用户填写的名称'))
    assert item['name']=='名称待核验'
    market.import_watch(store,uid,[{'symbol':'000875','market':'SZ'},{'symbol':'159330','market':'SZ','asset_class':'etf'}])
    market.add_watch(store,admin,InstrumentInput(symbol='603126',market='SH',name='另一用户的名称'))
    queued=store.query("SELECT * FROM site_jobs WHERE kind='names' AND status='queued'")
    assert len(queued)==1
    # 采集进行中新增证券时，保留一次后续任务，避免错过已经处理过的市场。
    store.execute("UPDATE site_jobs SET status='running' WHERE id=?",(queued[0]['id'],))
    market.add_watch(store,uid,InstrumentInput(symbol='600900',market='SH'))
    market.add_watch(store,uid,InstrumentInput(symbol='601939',market='SH'))
    assert len(store.query("SELECT id FROM site_jobs WHERE kind='names' AND status='queued'"))==1


# 名称采集独立于行情与交易资格，使用已有交易所/ETF数据适配器。
def test_names_collector_fills_names_without_verifying_trading(cloud,monkeypatch,capsys):
    import sys
    from backend import db,collectors
    from backend.cloud import collector,market
    store,_,_,_,_,uid=cloud
    market.import_watch(store,uid,[{'symbol':'603126','market':'SH'},{'symbol':'000875','market':'SZ'},{'symbol':'159330','market':'SZ','asset_class':'etf'}])
    rows={
        'master_sh':[{'证券代码':'603126','证券简称':'测试沪市简称'}],
        'master_sz':[{'A股代码':'000875','A股简称':'测试深市简称'}],
        'etf_names':[{'代码':'159330','名称':'测试ETF简称'}],
    }
    called=[]
    def source(action,*args,**kwargs):
        called.append(action)
        return rows[action]
    monkeypatch.setattr(db,'DATA',store.cfg.data)
    monkeypatch.setattr(collectors,'call',source)
    monkeypatch.setattr(sys,'argv',['collector','names','{}'])
    collector.main()
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='success' and result['unresolved']==[]
    assert set(called)==set(rows) and len(called)==3
    for ident,name in [('SH:603126','测试沪市简称'),('SZ:000875','测试深市简称'),('SZ:159330','测试ETF简称')]:
        item=market.instrument(store,ident)
        assert item['name']==name
        assert item['status']=='unknown' and item['risk_flag']=='unknown'
        assert item['verified_at'] is None and not item['rules']['verified']
    assert not store.one('SELECT instrument_id FROM bars LIMIT 1')


# 已有占位名称可在启动时重新排队；不支持的市场不会反复发起无效采集。
def test_pending_name_recovery_and_source_failure_preserve_cache(cloud,monkeypatch):
    from backend import db,collectors
    from backend.cloud import collector,market
    from backend.instruments import InstrumentInput
    store,_,_,_,_,uid=cloud
    market.add_watch(store,uid,InstrumentInput(symbol='603126',market='SH'))
    market.add_watch(store,uid,InstrumentInput(symbol='600900',market='SH'))
    store.execute("UPDATE instruments SET name='已有缓存简称' WHERE id='SH:600900'")
    store.execute('DELETE FROM site_jobs')
    assert collector.queue_names(store)['status']=='queued'
    monkeypatch.setattr(db,'DATA',store.cfg.data)
    called=[]
    def offline(action,*args,**kwargs):
        called.append(action)
        raise ValueError('测试数据源不可用')
    monkeypatch.setattr(collectors,'call',offline)
    result=collectors.sync_names()
    assert result['status']=='unavailable' and result['errors']
    assert called==['master_sh']
    assert market.instrument(store,'SH:603126')['name']=='名称待核验'
    assert market.instrument(store,'SH:600900')['name']=='已有缓存简称'
    store.execute("UPDATE instruments SET name='已解析简称' WHERE id='SH:603126'")
    store.execute('DELETE FROM site_jobs')
    market.add_watch(store,uid,InstrumentInput(symbol='AAPL',market='US'))
    assert collector.queue_names(store) is None
    assert not store.one("SELECT id FROM site_jobs WHERE kind='names'")


# 名称未能补全时保留具体原因，不将采集失败记成成功。
def test_name_job_preserves_unavailable_result(cloud,monkeypatch):
    from types import SimpleNamespace
    from backend.cloud import collector,market
    from backend.instruments import InstrumentInput
    store,_,_,_,_,uid=cloud
    market.add_watch(store,uid,InstrumentInput(symbol='603126',market='SH'))
    result={'status':'unavailable','sources':[],'errors':[{'source':'master_sh','error':'测试数据源不可用'}],'unresolved':['SH:603126']}
    monkeypatch.setattr(collector.subprocess,'run',lambda *args,**kwargs:SimpleNamespace(returncode=0,stdout=json_text(result)))
    assert collector.run_one(store)
    job=store.one("SELECT * FROM site_jobs WHERE kind='names'")
    saved=json.loads(job['result_json'])
    assert job['status']=='failed'
    assert {key:saved[key] for key in result}==result
    if '_request' in saved:
        assert saved['_request']=={'instrument_id':None,'timeframe':'1d'}


# ETF 名称只读取基金基础资料，避免实时行情限流阻断名称补全。
def test_etf_name_worker_uses_reference_metadata(monkeypatch):
    import akshare as ak
    import pandas as pd
    from backend.collector_worker import collect
    monkeypatch.setattr(ak,'fund_name_em',lambda:pd.DataFrame([
        {'基金代码':'159330','基金简称':'测试ETF名称','基金类型':'指数型-股票'},
    ]))
    def no_spot_quotes():
        raise AssertionError('名称补全不应依赖实时行情接口')
    monkeypatch.setattr(ak,'fund_etf_spot_em',no_spot_quotes)
    rows=collect('etf_names',{}).to_dict('records')
    assert rows==[{'代码':'159330','名称':'测试ETF名称'}]


# 没有写入权时不判断价格规则，同步来的待成交委托也不再撮合。
def test_rules_skip_without_write_lease_and_inert_orders_do_not_fill(cloud,bars,monkeypatch):
    from backend.cloud import market
    from backend.instruments import InstrumentInput
    from backend.simulation import OrderInput
    from backend.sync.protocol import SyncError
    store,keys,auth,journal,admin,uid=cloud
    item=market.add_watch(store,uid,InstrumentInput(symbol='600001',market='SH'))
    evidence={k:'https://www.sse.com.cn/test' for k in ('listing_url','status_url','announcement_url')};evidence['review_note']='隔离测试'
    market.verify(store,item['id'],{'name':'测试证券','status':'active','risk_flag':'clear','rules':{**item['rules'],'verified':True,'source_url':'https://www.sse.com.cn/rules'},'evidence':evidence})
    market.import_bars(store,item['id'],{'bars':bars[:4]})
    monkeypatch.setattr(market,'calendar',lambda *a,**kw:{'calendar_verified':True,'last_completed_session':bars[3]['ts'][:10],'date':bars[3]['ts'][:10]})
    monkeypatch.setattr('backend.collectors.fresh_reasons',lambda *a,**k:[])
    account=simulation.create_account(store,uid,'规则账户',10000)
    saved=simulation.create_rule(store,uid,{'account_id':account['id'],'instrument_id':item['id'],'kind':'close_at_or_below','trigger_price':'9','limit_price':'6','quantity':100,'source_kind':'manual'})
    store.execute('UPDATE sim_price_rules SET armed_at=? WHERE id=? AND user_id=?',(bars[0]['ts'][:10]+'T10:00:00+08:00',saved['id'],uid))
    class Guard:
        def account_guard(self,user,account_id):
            raise SyncError('LEASE')
    store.sync_service=Guard()
    assert simulation.evaluate(store,uid)==[]
    assert store.one('SELECT COUNT(*) n FROM sim_orders WHERE user_id=?',(uid,))['n']==0
    store.sync_service=None
    queued=simulation.order(store,uid,OrderInput(client_key='inert-order-key',account_id=account['id'],instrument_id=item['id'],side='buy',quantity=100,limit_price=6))
    store.execute('UPDATE sim_orders SET created_at=? WHERE id=? AND user_id=?',(bars[0]['ts'][:10]+'T10:00:00+08:00',queued['id'],uid))
    store.execute('INSERT INTO sync_inert_orders VALUES(?,?)',(queued['id'],uid))
    assert simulation.match(store,uid)==[]


# 最新价只进入观察报价。自选列表改显示这笔价格，日线收盘保持不动。
def test_watchlist_shows_spot_newer_than_daily_close(cloud):
    from test_assistant_lookup import daily_bar, listed_stock
    from backend.cloud import market
    store,*_,uid=cloud
    ident=listed_stock(store)
    daily_bar(store,ident,'2026-11-09T15:00:00+08:00',9)
    daily_bar(store,ident,'2026-11-10T15:00:00+08:00',10)
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at,cost_price,quantity) VALUES(?,?,?,?,?,?,?)',(uid,ident,'原有自选',0,stamp(),'8',100))
    store.execute('INSERT INTO market_quotes VALUES(?,?)',(ident,json.dumps({"id":ident,"price":12,"prev_close":10,"change_pct":20,"quote_time":"2026-11-11T11:03:00+08:00","fetched_at":"2026-11-11T11:03:05+08:00","source":"东方财富最新价","source_url":"https://quote.eastmoney.com/sh600519.html","kind":"spot"})))
    row=market.watch_rows(store,uid)[0]
    assert row["quote"]["close"]==12 and row["quote"]["ts"]=="2026-11-11T11:03:00+08:00"
    assert row["change_pct"]==20
    assert row["position"]["market_value"]==1200 and row["position"]["daily_pnl"]==200 and row["position"]["total_pnl"]==400
    assert market.bars_for(store,ident,limit=1)[-1]["close"]==10
    store.execute('UPDATE market_quotes SET payload_json=? WHERE instrument_id=?',(json.dumps({"id":ident,"price":4,"prev_close":10,"quote_time":"2026-11-10T14:00:00+08:00","kind":"spot","source":"东方财富最新价","source_url":"https://quote.eastmoney.com/sh600519.html"}),ident))
    again=market.watch_rows(store,uid)[0]
    assert again["quote"]["close"]==10 and again["quote"].get("kind")!="spot"


def test_sync_spots_saves_latest_price_without_touching_bars(monkeypatch):
    from backend import collectors, db
    db.execute("UPDATE instruments SET watched=0")
    db.execute("INSERT INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES('SH:689001','689001','SH','最新价测试','stock','',1,'{}')")
    monkeypatch.setattr(collectors,"fetch_spots",lambda secids:[{"f12":"689001","f13":1,"f2":1400.5,"f3":1.25,"f18":1383.2,"f124":None}] if "1.689001" in secids else [])
    before=db.one("SELECT COUNT(*) n FROM bars")["n"]
    result=collectors.sync_spots()
    assert result["updated"]==1 and result["status"]=="success"
    assert db.one("SELECT COUNT(*) n FROM bars")["n"]==before
    saved=json.loads(db.one("SELECT payload_json FROM market_quotes WHERE instrument_id='SH:689001'")["payload_json"])
    assert saved["kind"]=="spot" and saved["price"]==1400.5 and saved["prev_close"]==1383.2


def test_spot_targets_cover_index_hk_us_and_fund():
    from backend import collectors, db
    db.execute("UPDATE instruments SET watched=0")
    for ident, symbol, market, name, asset, watched in (
        ("SH:999991","999991","SH","测试指数","index",0),
        ("HK:00700","00700","HK","腾讯","stock",1),
        ("US:AAPL","AAPL","US","苹果","stock",1),
        ("FUND:110022","110022","FUND","测试基金","fund",1),
    ):
        db.execute("INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,'',?,?)",(ident,symbol,market,name,asset,watched,'{}'))
        db.execute("UPDATE instruments SET asset_class=?,watched=?,name=? WHERE market=? AND symbol=?",(asset,watched,name,market,symbol))
    found={(row["market"],row["symbol"],row["asset_class"]) for row in collectors._spot_targets()}
    assert ("SH","999991","index") in found
    assert ("HK","00700","stock") in found
    assert ("US","AAPL","stock") in found
    assert ("FUND","110022","fund") in found
    assert collectors._secids({"market":"HK","symbol":"00700"})==["116.00700"]
    assert collectors._secids({"market":"US","symbol":"AAPL"})==["105.AAPL","106.AAPL","107.AAPL"]
    assert collectors._secids({"market":"FUND","symbol":"110022"})==[]
    assert collectors._prefer_us([{"f2":10,"f6":5,"f13":105},{"f2":99,"f6":50,"f13":106}])["f2"]==99
    assert collectors._prefer_us([{"f2":10,"f6":1,"f13":105,"f14":"Apple"},{"f2":99,"f6":50,"f13":106,"f14":"Other"}],{"name":"苹果 NASDAQ"})["f13"]==105


def test_owner_can_queue_minute_chart_refresh(cloud):
    from test_assistant_lookup import listed_stock
    store,*_,uid=cloud
    ident=listed_stock(store)
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,0,?)',(uid,ident,'观察',stamp()))
    with TestClient(create_app(store.cfg)) as c:
        login(c)
        assert c.post('/api/me/instruments/'+ident+'/minute-sync',json={'timeframe':'1d'}).status_code==400
        queued=c.post('/api/me/instruments/'+ident+'/minute-sync',json={'timeframe':'15m'})
        assert queued.status_code==200,queued.text
        assert queued.json()['status']=='queued' and queued.json()['reused'] is False
        again=c.post('/api/me/instruments/'+ident+'/minute-sync',json={'timeframe':'15m'})
        assert again.json()['reused'] is True
        assert c.get('/api/me/instruments/'+ident+'/minute-sync',params={'timeframe':'15m'}).json()['status']=='queued'
        assert c.post('/api/me/instruments/SH:000000/minute-sync',json={'timeframe':'60m'}).status_code==404
