from datetime import datetime,timezone
import json
import pytest
from backend.cloud import resources,simulation
from backend.sync.engine import Sync
from backend.sync.protocol import SyncError
from test_sync_protocol import peers,sync,Memory
from test_cloud import cloud,login
from backend.public_app import create_app
from fastapi.testclient import TestClient


def test_limit_waits_for_offline_device_and_counts_offline_usage(peers):
    a,u,b,v,remote=peers;remote.server_time=datetime.now(timezone.utc)
    sync(a,u);sync(b,v)
    result=a.request_limits(u,{'daily':1,'monthly':2});assert result['proposal'] and result['limits']['daily'] is None
    remote.offline=True;assert b.shared_reserve(v,'offline-before-cap',{'daily':None,'monthly':None},'assistant') is False
    with pytest.raises(SyncError):a.shared_reserve(u,'finite-offline',{'daily':1,'monthly':2},'assistant')
    remote.offline=False;sync(b,v);sync(a,u)
    assert a.devices(u)['limits']['daily']==1
    with pytest.raises(SyncError,match='上限'):a.shared_reserve(u,'later-call',{'daily':1,'monthly':2},'assistant')


def test_owner_epoch_and_imported_account_are_readonly(peers):
    a,u,b,v,remote=peers;a.store.sync_service=a;b.store.sync_service=b
    account=simulation.create_account(a.store,u,'single writer',10000);sync(a,u);sync(b,v)
    local=b.store.one('SELECT id FROM sim_accounts WHERE user_id=?',(v,))['id']
    with pytest.raises(SyncError,match='只读'):b.account_guard(v,local)
    a.account_transfer(u,account['id'],b.binding(v)['device_id']);sync(b,v)
    b.account_guard(v,local)
    with pytest.raises(SyncError):a.account_guard(u,account['id'])
    with pytest.raises(SyncError):a.automatic(u,'report','standard:2026-11-02') if a.executor(u,b.binding(v)['device_id']) else None


def test_explicit_point_restore_and_deleted_record_does_not_silently_return(peers):
    a,u,b,v,remote=peers;row=resources.save(a.store,u,'topic',{'name':'original'});sync(a,u);sync(b,v)
    point=a.checkpoint(u,'test recovery point')
    resources.save(a.store,u,'topic',{'name':'edited'},ident=row['id'],version=1);sync(a,u)
    preview=a.point_preview(u,point['id']);a.restore_point(u,point['id'],preview['id']);sync(a,u)
    assert resources.get(a.store,u,'topic',row['id'])['data']['name']=='original'
    a.store.execute('DELETE FROM research_objects WHERE id=? AND user_id=?',(row['id'],u));sync(a,u)
    preview=a.point_preview(u,point['id']);assert any(v['kind']=='object' for v in preview['skipped'])
    gid=a.store.one("SELECT object_id FROM sync_entities WHERE user_id=? AND kind='object'",(u,))['object_id'];head,_=a.heads(a.binding(u)).read();tombstone=head['tombstones']['object:'+gid]['hash']
    a.restore_deleted(u,'object',gid,tombstone);sync(a,u);sync(b,v)
    assert resources.listing(b.store,v,'topic')['items'][0]['data']['name']=='original'


def test_unbind_then_new_space_does_not_mix_old_encrypted_outbox(peers):
    a,u,b,v,remote=peers;resources.save(a.store,u,'topic',{'name':'kept local'});sync(a,u);a.pause(u)
    a.unbind(u,'解除本机绑定并保留本地资料');fresh=Memory();a.factory=lambda *args:fresh
    a.configure(u,{'backend':'webdav','url':'https://new.example.test'},create=True);sync(a,u)
    assert resources.listing(a.store,u,'topic')['items'][0]['data']['name']=='kept local'
    assert a.details(u)['pending_upload']==0


def test_backend_migration_keeps_identifiers_usage_and_tombstones(peers):
    a,u,b,v,old=peers;row=resources.save(a.store,u,'topic',{'name':'move evidence'});sync(a,u);sync(b,v)
    a.revoke(u,b.binding(v)['device_id']);a.pause(u);new=Memory();a.factory=lambda config,*args:new if 'new.example' in config['url'] else old
    preview=a.migration_preview(u,{'backend':'webdav','url':'https://new.example.test'})
    candidate=a.journal.get('sync',u,'migration-candidate')
    assert 'secret' not in candidate and a.candidate_secret(u,candidate)['key'] not in json.dumps(a.journal.all('sync',u))
    result=a.migrate(u,preview['id']);assert result['status']=='synced' and result['url']=='https://new.example.test'
    assert resources.get(a.store,u,'topic',row['id'])['data']['name']=='move evidence'
    sync(a,u);assert len(a.heads(a.binding(u)).read()[0]['objects'])>=1
    with pytest.raises(SyncError):b.heads(b.binding(v)).read()


def test_system_api_scopes_and_upgrade_operator_plan_only(cloud,monkeypatch):
    from backend.upgrades.download import Fetcher
    from backend.upgrades.trust import UpgradeError
    def offline(*args):raise UpgradeError('NETWORK')
    monkeypatch.setattr(Fetcher,'metadata',offline)
    store,keys,_,_,admin,uid=cloud
    with TestClient(create_app(store.cfg)) as c:
        login(c);assert c.get('/api/me/sync').json()['status']=='unconfigured'
        assert c.get('/api/admin/upgrades').status_code==403
        assert c.post('/api/admin/upgrades/anything/execute',json={}).status_code==403
    with TestClient(create_app(store.cfg)) as c:
        login(c,'admin@example.test');result=c.post('/api/admin/upgrades/check').json();assert result['status']=='check_failed'
        assert c.post('/api/admin/upgrades/anything/execute',json={}).json()['code']=='EXECUTOR'


def test_stale_browser_cannot_write_with_current_api(cloud):
    store,*_=cloud
    from backend.cloud import VERSION
    with TestClient(create_app(store.cfg)) as c:
        login(c)
        result=c.post('/api/me/client-state',json={'id':'stale-cache','dirty':False},headers={'X-StockAI-Client-Version':'3.1.0-preview.1'})
        assert result.status_code==409 and result.json()['code']=='CLIENT_VERSION'
        assert c.post('/api/me/client-state',json={'id':'current-cache','dirty':False},headers={'X-StockAI-Client-Version':VERSION}).status_code==200
