from contextlib import closing
from pathlib import Path
import shutil
import sqlite3
import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from backend.cloud import resources
from backend.cloud.research_control import control
from backend.sync.engine import Sync
from backend.sync.protocol import SyncError
from test_cloud import cloud
from test_sync_protocol import Memory,sync,peers


def backup(store,path):
    with closing(sqlite3.connect(store.path)) as source,closing(sqlite3.connect(path)) as target:source.backup(target);target.commit()


def test_backup_before_first_binding_keeps_keys_maps_and_unique_outbox(cloud,tmp_path):
    store,keys,_,_,_,uid=cloud;old=resources.save(store,uid,'topic',{'name':'before binding'});snapshot=tmp_path/'old.sqlite3';backup(store,snapshot)
    remote=Memory();service=Sync(store,keys,lambda *args:remote);service.configure(uid,{'backend':'webdav','url':'https://storage.example.test'},create=True)
    resources.save(store,uid,'topic',{'name':'newer local draft'},ident=old['id'],version=1);new=resources.save(store,uid,'topic',{'name':'unique offline record'});service.capture(uid)
    shutil.copy2(snapshot,store.path)
    recovered=Sync(store,keys,lambda *args:remote);recovered.recover_local()
    rows=resources.listing(store,uid,'topic')['items'];assert {r['id'] for r in rows}=={old['id'],new['id']}
    assert {r['data']['name'] for r in rows}=={'newer local draft','unique offline record'}
    assert recovered.details(uid)['pending_upload']>=2


def test_unbind_marker_survives_old_business_backup(peers,tmp_path):
    a,u,b,v,_=peers;sync(a,u);snapshot=tmp_path/'bound.sqlite3';backup(a.store,snapshot)
    a.pause(u);a.unbind(u,'解除本机绑定并保留本地资料');shutil.copy2(snapshot,a.store.path)
    a.recover_local();assert a.details(u)['status']=='unconfigured'


def test_retired_backend_credentials_are_private_files_not_control_payload(peers):
    a,u,b,v,_=peers;sync(a,u);space=a.binding(u)['space_id'];secret=a.secrets(a.binding(u))['key']
    a.pause(u);a.unbind(u,'解除本机绑定并保留本地资料')
    assert a.read_private(u,'binding-'+space)['space_id']==space
    records=a.journal.all('sync',u);assert secret not in __import__('json').dumps(records)


def test_rebind_same_space_keeps_original_ids_and_later_solo_edit(peers):
    a,u,b,v,remote=peers;row=resources.save(a.store,u,'topic',{'name':'before unbind'});sync(a,u);sync(b,v);recovery=a.recovery(u)
    a.pause(u);a.unbind(u,'解除本机绑定并保留本地资料');resources.save(a.store,u,'topic',{'name':'solo edit'},ident=row['id'],version=1)
    a.configure(u,{'backend':'webdav','url':'https://storage.example.test','recovery':recovery});sync(a,u);sync(b,v)
    assert resources.get(a.store,u,'topic',row['id'])['data']['name']=='solo edit'
    assert len(resources.listing(a.store,u,'topic')['items'])==1 and resources.listing(b.store,v,'topic')['items'][0]['data']['name']=='solo edit'


def test_returning_device_after_two_backend_moves_preserves_ids_and_deletions(peers):
    a,u,b,v,old=peers;keep=resources.save(a.store,u,'topic',{'name':'keep one'});gone=resources.save(a.store,u,'topic',{'name':'deleted elsewhere'});sync(a,u);sync(b,v)
    offline={r['data']['name']:r for r in resources.listing(b.store,v,'topic')['items']}
    resources.save(b.store,v,'topic',{'name':'offline edit to deleted'},ident=offline['deleted elsewhere']['id'],version=1);b.capture(v)
    a.store.execute('DELETE FROM research_objects WHERE user_id=? AND id=?',(u,gone['id']));sync(a,u);a.revoke(u,b.binding(v)['device_id']);a.pause(u)
    second=Memory();third=Memory();factory=lambda config,*args:second if 'second.example' in config['url'] else third if 'third.example' in config['url'] else old;a.factory=factory
    plan=a.migration_preview(u,{'backend':'webdav','url':'https://second.example.test'});a.migrate(u,plan['id']);a.pause(u)
    plan=a.migration_preview(u,{'backend':'webdav','url':'https://third.example.test'});a.migrate(u,plan['id'])
    b.pause(v);b.unbind(v,'解除本机绑定并保留本地资料');b.factory=factory
    b.configure(v,{'backend':'webdav','url':'https://third.example.test','recovery':a.recovery(u)})
    sync(b,v);sync(a,u)
    rows=resources.listing(b.store,v,'topic')['items'];assert len(rows)==1 and rows[0]['id']==offline['keep one']['id'] and rows[0]['data']['name']=='keep one'


def test_newer_signed_delete_wins_over_outbox_from_before_first_binding(cloud,tmp_path):
    store,keys,_,_,_,uid=cloud;authority=control(store,keys)
    row=resources.save(store,uid,'topic',{'name':'must stay deleted'})
    snapshot=tmp_path/'before-binding.sqlite3';backup(store,snapshot)
    remote=Memory();service=Sync(store,keys,lambda *args:remote)
    service.configure(uid,{'backend':'webdav','url':'https://storage.example.test'},create=True);service.capture(uid)
    original=store.one("SELECT * FROM sync_entities WHERE user_id=? AND kind='object' AND local_id=?",(uid,row['id']))
    resources.delete(store,uid,'topic',row['id']);shutil.copy2(snapshot,store.path);authority.reconcile()
    recovered=Sync(store,keys,lambda *args:remote);recovered.recover_local()
    assert not store.one('SELECT id FROM research_objects WHERE id=?',(row['id'],))
    assert authority.deleted(uid,'topic',row['id'])
    mapping=store.one("SELECT local_id FROM sync_rows WHERE user_id=? AND table_name='research_objects' AND global_id=?",(uid,original['object_id']))
    assert mapping['local_id']==row['id']
    entity=store.one("SELECT * FROM sync_entities WHERE user_id=? AND kind='object' AND object_id=?",(uid,original['object_id']))
    operation=store.one('SELECT payload_json FROM sync_operations WHERE user_id=? AND hash=?',(uid,json.loads(entity['heads_json'])[0]))
    assert entity['deleted'] and json.loads(original['heads_json'])[0] in json.loads(operation['payload_json'])['parents']
    sync(recovered,uid);head,_=recovered.heads(recovered.binding(uid)).read()
    assert head['objects']['object:'+original['object_id']]['deleted']
    assert 'object:'+original['object_id'] in head['tombstones']
    recovered.recover_local();assert not resources.listing(store,uid,'topic')['items']


def test_explicit_restore_supersedes_only_current_signed_delete_and_survives_recovery(peers):
    a,u,b,v,_=peers;row=resources.save(a.store,u,'topic',{'name':'authorized restore'});sync(a,u);sync(b,v)
    authority=control(a.store,a.keys);resources.delete(a.store,u,'topic',row['id']);sync(a,u);sync(b,v)
    gid=a.store.one("SELECT object_id FROM sync_entities WHERE user_id=? AND kind='object' AND local_id=?",(u,row['id']))['object_id']
    head,_=a.heads(a.binding(u)).read();tombstone=head['tombstones']['object:'+gid]['hash']
    assert authority.deleted(u,'topic',row['id'])
    a.restore_deleted(u,'object',gid,tombstone);authority.verify();authority.reconcile();a.recover_local()
    assert not authority.deleted(u,'topic',row['id'])
    assert resources.get(a.store,u,'topic',row['id'])['data']['name']=='authorized restore'
    with authority.tx() as c:
        intent=json.loads(c.execute("SELECT payload FROM ledger WHERE json_extract(payload,'$.type')='restore' ORDER BY seq DESC LIMIT 1").fetchone()[0])
    assert intent['tombstone']==tombstone and intent['deleted_at']
    sync(a,u);sync(b,v);head,_=a.heads(a.binding(u)).read()
    assert head['tombstones']['object:'+gid]['hash']==tombstone
    assert resources.listing(b.store,v,'topic')['items'][0]['data']['name']=='authorized restore'
    resources.delete(a.store,u,'topic',row['id']);sync(a,u)
    with pytest.raises(SyncError) as stale:a.restore_deleted(u,'object',gid,tombstone)
    assert stale.value.code=='CONFLICT'
    authority.verify();authority.reconcile();a.recover_local()
    assert authority.deleted(u,'topic',row['id']) and not resources.listing(a.store,u,'topic')['items']


def test_peer_tombstone_survives_restoring_older_local_database(peers,tmp_path):
    a,u,b,v,_=peers;row=resources.save(a.store,u,'topic',{'name':'peer deletion'});sync(a,u);sync(b,v)
    snapshot=tmp_path/'before-peer-delete.sqlite3';backup(a.store,snapshot)
    received=resources.listing(b.store,v,'topic')['items'][0];resources.delete(b.store,v,'topic',received['id']);sync(b,v);sync(a,u)
    before,_=a.heads(a.binding(u)).read();shutil.copy2(snapshot,a.store.path);a.recover_local()
    assert not resources.listing(a.store,u,'topic')['items']
    sync(a,u);after,_=a.heads(a.binding(u)).read()
    assert after['tombstones']==before['tombstones']


def test_peer_explicit_restore_can_supersede_the_same_local_delete(peers):
    a,u,b,v,_=peers;row=resources.save(a.store,u,'topic',{'name':'restore from authorized peer'});sync(a,u);sync(b,v)
    authority=control(a.store,a.keys);resources.delete(a.store,u,'topic',row['id']);sync(a,u);sync(b,v)
    entity=a.store.one("SELECT object_id FROM sync_entities WHERE user_id=? AND kind='object' AND local_id=?",(u,row['id']))
    gid=entity['object_id'];head,_=b.heads(b.binding(v)).read();tombstone=head['tombstones']['object:'+gid]['hash']
    b.restore_deleted(v,'object',gid,tombstone);sync(b,v);sync(a,u)
    authority.verify();authority.reconcile();a.recover_local()
    assert not authority.deleted(u,'topic',row['id'])
    assert resources.get(a.store,u,'topic',row['id'])['data']['name']=='restore from authorized peer'


def test_old_peer_restore_cannot_clear_newer_delete_after_business_rollback(peers,tmp_path):
    a,u,b,v,_=peers;row=resources.save(a.store,u,'topic',{'name':'new deletion must win'});sync(a,u);sync(b,v)
    authority=control(a.store,a.keys);resources.delete(a.store,u,'topic',row['id']);sync(a,u);sync(b,v)
    gid=a.store.one("SELECT object_id FROM sync_entities WHERE user_id=? AND kind='object' AND local_id=?",(u,row['id']))['object_id']
    head,_=a.heads(a.binding(u)).read();tombstone=head['tombstones']['object:'+gid]['hash']
    old_intent=authority.deletion(u,'topic',row['id'])['signature']
    snapshot=tmp_path/'deleted-business.sqlite3';backup(a.store,snapshot)
    b.restore_deleted(v,'object',gid,tombstone);sync(b,v);sync(a,u)
    assert not authority.deleted(u,'topic',row['id'])
    resources.delete(a.store,u,'topic',row['id']);new_intent=authority.deletion(u,'topic',row['id'])['signature']
    assert new_intent!=old_intent
    assert authority.restore(u,'topic',row['id'],tombstone,old_intent) is False
    shutil.copy2(snapshot,a.store.path);authority.reconcile();a.recover_local();authority.verify()
    assert authority.deletion(u,'topic',row['id'])['signature']==new_intent
    assert not resources.listing(a.store,u,'topic')['items']
    sync(a,u);sync(b,v);assert not resources.listing(b.store,v,'topic')['items']


def test_same_user_key_rotations_serialize_and_failed_start_keeps_current_keys(peers,monkeypatch):
    a,u,b,v,remote=peers;sync(a,u);sync(b,v)
    entered=threading.Event();release=threading.Event();second_waiting=threading.Event();second_inside=threading.Event();role=threading.local()
    original_lock=a.store.user_lock;original_write=a.write_private
    class ObservedLock:
        def __enter__(self):
            if getattr(role,'value',None)=='second':second_waiting.set()
            original_lock(u).__enter__();return self
        def __exit__(self,*args):original_lock(u).__exit__(*args)
    monkeypatch.setattr(a.store,'user_lock',lambda uid:ObservedLock() if uid==u else original_lock(uid))
    def write(uid,name,value):
        if name=='keys-'+a.binding(u)['space_id']:
            if getattr(role,'value',None)=='first':entered.set();assert release.wait(10)
            elif getattr(role,'value',None)=='second':second_inside.set()
        return original_write(uid,name,value)
    monkeypatch.setattr(a,'write_private',write)
    def rotate(label):
        role.value=label;return a.rotate(u,'轮换密钥并重新授权其他设备')
    with ThreadPoolExecutor(2) as pool:
        first=pool.submit(rotate,'first');assert entered.wait(10)
        second=pool.submit(rotate,'second')
        try:
            assert second_waiting.wait(10) and not second_inside.is_set()
        finally:release.set()
        assert [first.result()['key_version'],second.result()['key_version']]==[2,3]
    head,_=a.heads(a.binding(u)).read();assert head['key_version']==3
    current=a.secrets(a.binding(u));remote.offline=True
    with pytest.raises(SyncError):a.rotate(u,'轮换密钥并重新授权其他设备')
    remote.offline=False
    assert a.secrets(a.binding(u))==current and a.recovery(u)['key_version']==3
