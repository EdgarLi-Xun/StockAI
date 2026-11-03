# 验证本地 API 的身份、来源、权限、导入原子性和备份恢复行为。
import json

import pytest

from backend import db, jobs, maintenance, security
from backend.config import Settings


# 验证初始证券清单存在，但系统不会伪造尚未采集的报价。
def test_api_initial_state_has_no_fake_prices(client):
    d=client.get('/api/dashboard').json()
    assert d['stats']['watch_count']==43 and d['stats']['quote_count']==0
    assert d['candidates']==[] and all(m['price'] is None for m in d['metals'])


# 验证缺少令牌的研究接口请求被拒绝。
def test_unauthenticated_api_rejected(client):
    client.headers.pop('Authorization')
    assert client.get('/api/watchlist').status_code==401


# 验证跨站来源不能引导会话或修改数据。
def test_cross_site_bootstrap_and_mutation_rejected(client):
    assert client.get('/api/session',headers={'Origin':'https://evil.example'}).status_code==403
    assert client.post('/api/watchlist',json={},headers={'Origin':'https://evil.example'}).status_code==403


# 验证不受信任的 Host 无法利用本机服务引导身份。
def test_dns_rebinding_host_rejected(client):
    assert client.get('/api/session',headers={'Host':'evil.example:8765'}).status_code==403


# 验证带代理头的请求不能获得本机管理员引导权限。
def test_proxy_cannot_bootstrap_admin_session(client):
    assert client.get('/api/session',headers={'X-Forwarded-For':'192.168.1.20'}).status_code==403
    security.configure_password('test-proxy-admin-password')
    cfg=db.settings(); cfg.lan_enabled=True; db.save_settings(cfg)
    response=client.get('/api/session',headers={'X-Forwarded-For':'192.168.1.20'})
    assert response.status_code==200 and response.json()=={'requires_login':True}
    assert client.get('/api/watchlist',headers={'X-Forwarded-For':'192.168.1.20'}).status_code==401


# 验证默认配置不允许局域网来源访问。
def test_default_lan_disabled():
    assert not db.settings().lan_enabled
    assert not security.peer_allowed('192.168.1.10')
    assert security.loopback('127.0.0.1')


# 验证启用局域网前必须具备管理员密码及明确确认。
def test_lan_needs_password_and_explicit_confirmation(client):
    cfg=db.settings().model_dump(); cfg['lan_enabled']=True
    assert client.put('/api/settings',json={'settings':cfg,'confirm':True}).status_code==400
    assert client.post('/api/security/password',json={'password':'a-strong-test-password'}).status_code==200
    assert client.put('/api/settings',json={'settings':cfg}).status_code==400
    assert client.put('/api/settings',json={'settings':cfg,'confirm':True}).status_code==200


# 验证只读角色不能修改数据或下载数据库备份。
def test_readonly_token_cannot_mutate_or_download_database(client):
    security.configure_password('test-readonly-password','reader')
    token=security.login('test-readonly-password','reader','192.168.1.8')['token']
    client.headers['Authorization']='Bearer '+token
    assert client.get('/api/watchlist').status_code==200
    assert client.post('/api/jobs',json={'kind':'sync'}).status_code==403
    assert client.get('/api/backups').status_code==403


# 验证重复添加自选复用同一证券记录。
def test_watchlist_shared_and_deduplicated(client):
    payload={'symbol':'000001','market':'SZ','name':'历史名称','asset_class':'stock','group_name':'测试分组'}
    assert client.post('/api/watchlist',json=payload).status_code==200
    assert client.post('/api/watchlist',json=payload).status_code==200
    rows=client.get('/api/watchlist').json()
    assert sum(r['id']=='SZ:000001' for r in rows)==1
    assert db.one("SELECT COUNT(*) n FROM instruments WHERE id='SZ:000001'")['n']==1


# 验证含非法记录的整批导入不会写入前面的合法项。
def test_bad_import_does_not_partially_write(client):
    content=json.dumps([{'symbol':'000001','market':'SZ'},{'symbol':'wrong','market':'SH'}])
    assert client.post('/api/watchlist/import',json={'format':'json','content':content}).status_code==400
    assert not db.one("SELECT id FROM instruments WHERE id='SZ:000001'")


# 验证恢复备份后报告、文件及关联记录保持一致。
def test_ac_f11_backup_restore_preserves_reports_and_relationships(monkeypatch):
    from backend import research, ai
    monkeypatch.setattr(ai,'health',lambda:{'available':False,'message':'offline'})
    research.generate('backup:test')
    before=db.one('SELECT COUNT(*) n FROM instruments')['n']
    saved=maintenance.create_backup()
    db.execute("UPDATE instruments SET watched=0")
    db.execute("DELETE FROM reports")
    result=maintenance.restore_backup(saved['id'],'恢复备份')
    assert result['counts']['reports']==1 and result['counts']['instruments']==before
    assert db.one('SELECT COUNT(*) n FROM reports')['n']==1
    assert db.one('SELECT COUNT(*) n FROM instruments WHERE watched=1')['n']==43
    assert result['safety_backup']!=saved['id']


# 验证缺少指定确认文字时拒绝恢复备份。
def test_restore_confirmation_required():
    saved=maintenance.create_backup()
    with pytest.raises(ValueError):
        maintenance.restore_backup(saved['id'],'yes')


# 验证相同逻辑键只会创建一项任务。
def test_job_idempotence(monkeypatch):
    monkeypatch.setattr(jobs.EXECUTOR,'submit',lambda *a:None)
    first=jobs.enqueue('standard','standard:test')
    second=jobs.enqueue('standard','standard:test')
    assert first['id']==second['id']
    assert db.one('SELECT COUNT(*) n FROM jobs')['n']==1


# 验证任务时间、上海时区及唯一内建调度源。
def test_schedule_times_and_one_source():
    info=jobs.schedule_info()
    assert info['source']=='builtin' and info['timezone']=='Asia/Shanghai'
    assert [x['time'] for x in info['plans']]==['08:00','16:00','20:00']


# 验证错过的计划会被记录，但不会补造历史报告。
def test_missed_schedules_are_persisted_without_fake_reports():
    from datetime import datetime
    from backend.config import TZ
    db.execute("INSERT INTO settings VALUES('last_scheduler_tick',?)",('2025-01-02T18:00:00+08:00',))
    for day in ('2025-01-03','2025-01-06'):
        db.execute('INSERT INTO calendar VALUES(?,?,?)',(day,'test-fixture','2025-01-02T00:00:00+08:00'))
    jobs.record_missed(datetime(2025,1,6,9,tzinfo=TZ))
    first=db.query("SELECT logical_key,status FROM jobs")
    jobs.record_missed(datetime(2025,1,6,9,tzinfo=TZ))
    assert len(db.query('SELECT id FROM jobs'))==len(first)
    keys={r['logical_key'] for r in first}
    assert 'standard:2025-01-03' in keys and 'full:2025-01-05' in keys and 'closing:2025-01-03' in keys
    assert all(r['status']=='missed' for r in first)
    assert db.one('SELECT COUNT(*) n FROM reports')['n']==0
