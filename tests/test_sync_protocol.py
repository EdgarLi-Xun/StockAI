import copy
import json
import os
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest
from test_cloud import cloud,profile
from backend.cloud.auth import Auth
from backend.cloud.crypto import Keyring
from backend.cloud.lifecycle import ErasureJournal
from backend.cloud.store import Store,json_text
from backend.cloud import resources,simulation
from backend.sync.protocol import SyncError,seal,open_sealed,digest
from backend.sync.engine import Sync


class Memory:
    def __init__(self):self.files={};self.lock=threading.RLock();self.offline=False;self.writes=[]
    def probe(self):return {'conditional_write':True,'available_bytes':None}
    def initialize(self):pass
    def read(self,path):
        with self.lock:
            if self.offline:raise SyncError('OFFLINE')
            if path not in self.files:raise SyncError('MISSING')
            body=self.files[path];return body,'"'+digest(body)+'"'
    def put(self,path,body,etag=None,create=False):
        with self.lock:
            if self.offline:raise SyncError('OFFLINE')
            previous=self.files.get(path)
            if create and previous is not None:raise SyncError('CONFLICT')
            if not create and (previous is None or etag!='"'+digest(previous)+'"'):raise SyncError('CONFLICT')
            self.files[path]=bytes(body);self.writes.append(path);return '"'+digest(body)+'"'


@pytest.fixture
def peers(cloud,tmp_path):
    a,keys,auth,journal,admin,uid=cloud
    cfg=replace(a.cfg,data=tmp_path/'peer2',erasures=tmp_path/'control2',key_file=tmp_path/'keys2.json')
    b=Store(cfg).initialize();key2=Keyring.create(cfg.key_file);ErasureJournal(b,key2).create();uid2=Auth(b,key2).create_admin('another@example.test','local-only-test-password')
    remote=Memory();first=Sync(a,keys,lambda *args:remote);second=Sync(b,key2,lambda *args:remote)
    first.configure(uid,{'backend':'webdav','url':'https://storage.example/sync'},create=True)
    second.configure(uid2,{'backend':'webdav','url':'https://storage.example/sync','recovery':first.recovery(uid)})
    return first,uid,second,uid2,remote


def sync(service,uid):return service.synchronize(uid,service.preview(uid)['id'])


def test_encryption_binds_space_and_detects_corruption():
    key=os.urandom(32);space=uuid.uuid4().hex;body=seal(key,space,{'text':'私人判断'})
    assert '私人'.encode() not in body and open_sealed(key,space,body)['text']=='私人判断'
    with pytest.raises(SyncError):open_sealed(os.urandom(32),space,body)
    with pytest.raises(SyncError):open_sealed(key,uuid.uuid4().hex,body)
    with pytest.raises(SyncError):open_sealed(key,space,body[:-6]+b'broken')


def test_first_preview_and_incremental_journal_roundtrip(peers):
    first,u,second,v,remote=peers
    row=resources.save(first.store,u,'topic',{'name':'离线课题','question':'保留来源'},client_key='offline-topic-key')
    with pytest.raises(SyncError):first.synchronize(u)
    sync(first,u);sync(second,v)
    received=resources.listing(second.store,v,'topic')['items']
    assert len(received)==1 and received[0]['data']['name']=='离线课题' and received[0]['id']!=row['id']
    assert not second.store.query('SELECT * FROM personal_jobs')
    count=len([p for p in remote.writes if p.startswith('objects/')]);sync(first,u);sync(second,v)
    assert len([p for p in remote.writes if p.startswith('objects/')])==count


def test_bounded_pull_does_not_report_synced_before_remaining_records_arrive(peers):
    a,u,b,v,_=peers
    for i in range(6):resources.save(a.store,u,'topic',{'name':'bounded record '+str(i)})
    sync(a,u)
    result=b.synchronize(v,b.preview(v)['id'],limit=1)
    assert result['status']=='syncing' and result['remote_pending'] and len(resources.listing(b.store,v,'topic')['items'])<6
    for _ in range(8):
        result=b.synchronize(v,limit=1)
        if result['status']=='synced':break
    assert result['status']=='synced' and len(resources.listing(b.store,v,'topic')['items'])==6


def test_concurrent_edits_preserve_both_branches(peers):
    first,u,second,v,remote=peers
    row=resources.save(first.store,u,'topic',{'name':'基线'},client_key='topic-baseline')
    sync(first,u);sync(second,v);local=resources.listing(second.store,v,'topic')['items'][0]
    resources.save(first.store,u,'topic',{'name':'本机修改'},ident=row['id'],version=row['version'])
    resources.save(second.store,v,'topic',{'name':'另一端修改'},ident=local['id'],version=local['version'])
    first.capture(u);second.capture(v);sync(first,u);sync(second,v);sync(first,u)
    conflicts=first.store.query("SELECT * FROM sync_conflicts WHERE user_id=? AND state='open'",(u,))
    assert conflicts and '另一端修改' in conflicts[-1]['remote_json']+conflicts[-1]['local_json']


def test_deleted_record_does_not_resurrect_from_offline_edit(peers):
    first,u,second,v,remote=peers
    row=resources.save(first.store,u,'topic',{'name':'待删除'},client_key='topic-deleting')
    sync(first,u);sync(second,v);local=resources.listing(second.store,v,'topic')['items'][0]
    resources.save(second.store,v,'topic',{'name':'离线旧改动'},ident=local['id'],version=local['version']);second.capture(v)
    first.store.execute('DELETE FROM research_objects WHERE id=? AND user_id=?',(row['id'],u));sync(first,u);sync(second,v);sync(first,u)
    assert not resources.listing(first.store,u,'topic')['items']
    assert not resources.listing(second.store,v,'topic')['items']


def test_remote_rollback_is_rejected_without_clearing_local(peers):
    first,u,second,v,remote=peers;old=remote.files['head.bin']
    resources.save(first.store,u,'topic',{'name':'保留的资料'},client_key='keep-topic-key');sync(first,u)
    remote.files['head.bin']=old
    with pytest.raises(SyncError,match='回退'):first.preview(u)
    assert resources.listing(first.store,u,'topic')['items']


def test_account_snapshot_is_not_replayed_as_trades(peers):
    first,u,second,v,remote=peers;account=simulation.create_account(first.store,u,'独立账户',10000)
    sync(first,u);sync(second,v);sync(second,v)
    accounts=second.store.query('SELECT * FROM sim_accounts WHERE user_id=?',(v,));assert len(accounts)==1
    assert accounts[0]['cash']=='10000.00'
    ledger=second.store.query('SELECT * FROM sim_ledger WHERE user_id=?',(v,));assert len(ledger)==1 and ledger[0]['balance']=='10000.00'


def test_shared_last_quota_allows_exactly_one_sender(peers):
    first,u,second,v,remote=peers
    def reserve(args):
        service,uid,ident=args
        try:service.heads(service.binding(uid)).reserve_usage(ident,'2026-11-02','2026-11',{'daily':1,'monthly':1},'assistant');return True
        except SyncError:return False
    with ThreadPoolExecutor(2) as pool:result=list(pool.map(reserve,[(first,u,'request-first'),(second,v,'request-second')]))
    assert sum(result)==1


def test_credentials_and_local_authorizations_are_not_exported(peers,cloud):
    first,u,second,v,remote=peers;profile(cloud,allow_simulation_summary=True,allow_trade_details=True)
    sync(first,u);sync(second,v)
    row=second.store.one('SELECT * FROM model_profiles WHERE user_id=?',(v,));assert row['needs_reconnect'] and not row['active']
    assert not second.store.query('SELECT * FROM model_credentials')
    raw=first.store.query('SELECT payload_json FROM sync_operations WHERE user_id=?',(u,))
    assert 'test-key-owner' not in json_text(raw) and 'credential_id' not in json_text(raw)
