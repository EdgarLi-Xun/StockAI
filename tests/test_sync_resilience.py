from dataclasses import replace
from datetime import datetime,timezone
from email.utils import formatdate
import json
import uuid
import pytest
from backend.cloud import resources,models,jobs,alerts
from backend.cloud.auth import Auth
from backend.cloud.crypto import Keyring
from backend.cloud.lifecycle import ErasureJournal
from backend.cloud.store import Store,stamp,json_text
from backend.sync.engine import Sync
from backend.sync.protocol import SyncError,digest
from backend.sync.network import Reply
from test_cloud import cloud,GOOD
from test_sync_protocol import peers,sync,Memory
from test_sync_hub import hub


def test_repeated_usage_id_never_causes_second_send(peers):
    a,u,b,v,remote=peers;remote.server_time=datetime.now(timezone.utc);sync(a,u)
    assert a.shared_reserve(u,'one-logical-call',{'daily':5,'monthly':10},'assistant')
    a.shared_result(u,'one-logical-call','received',{'total_tokens':10})
    with pytest.raises(SyncError):a.shared_reserve(u,'one-logical-call',{'daily':5,'monthly':10},'assistant')


def test_model_before_send_checks_shared_last_quota_for_both_providers(peers):
    a,u,b,v,remote=peers;remote.server_time=datetime.now(timezone.utc);a.store.sync_service=a;b.store.sync_service=b
    for service,uid in ((a,u),(b,v)):
        p=models.save_profile(service.store,service.keys,uid,models.ProfileInput(name='local model credentials',provider='minimax',api_key='fixture-only-model-key'))
        models.set_default(service.store,uid,p['id'])
    sync(a,u);sync(b,v);a.request_limits(u,{'daily':1,'monthly':1});sync(b,v);sync(a,u)
    calls=[]
    def wire(*args):calls.append(1);return GOOD,{'total_tokens':12},''
    for service,uid in ((a,u),(b,v)):
        jobs.enqueue(service.store,uid,'profile_test');result=jobs.run(service.store,service.keys,jobs.claim(service.store,'fixture-worker'),wire)
    assert len(calls)==1 and result['error_code']=='USAGE_LIMIT'


def test_delivery_history_imported_without_resending(peers):
    a,u,b,v,_=peers;rule=resources.save(a.store,u,'alert',{'name':'historical rule','enabled':False,'email':False},status='disabled');event=uuid.uuid4().hex;delivery=uuid.uuid4().hex
    a.store.execute('INSERT INTO alert_events VALUES(?,?,?,?,?,?,?,?)',(event,u,rule['id'],1,'stable-event',stamp(),'delivered','{}'))
    a.store.execute("INSERT INTO alert_deliveries(id,user_id,event_ids_json,channel,state,next_at,created_at) VALUES(?,?,?,'email','queued',?,?)",(delivery,u,json_text([event]),stamp(),stamp()))
    a.store.execute('INSERT INTO alert_delivery_events VALUES(?,?,?)',(event,'email',delivery))
    sync(a,u);sync(b,v)
    received=b.store.one('SELECT * FROM alert_deliveries WHERE user_id=?',(v,));assert received['state']=='sync_received'
    assert json.loads(received['event_ids_json'])[0]!=event
    assert b.store.one('SELECT event_id FROM alert_delivery_events WHERE delivery_id=?',(received['id'],))
    assert alerts.send_mail(b.store,b.keys,sender=lambda message:pytest.fail('receiving must not send mail')) is False


def test_key_rotation_requires_new_recovery_material_and_old_space_kept(peers):
    a,u,b,v,remote=peers;sync(a,u);sync(b,v);old=a.recovery(u)
    a.revoke(u,b.binding(v)['device_id']);a.rotate(u,'轮换密钥并重新授权其他设备')
    assert a.recovery(u)['key_version']==2 and a.recovery(u)['key']!=old['key']
    with pytest.raises(SyncError):b.heads(b.binding(v)).read()
    assert a.heads(a.binding(u)).read()[0]['devices'][b.binding(v)['device_id']]['revoked']


def test_lost_initial_head_ack_keeps_recovery_key_and_can_continue(cloud):
    store,keys,_,_,_,uid=cloud
    class Lost(Memory):
        once=True
        def put(self,path,*args,**kwargs):
            result=super().put(path,*args,**kwargs)
            if path=='head.bin' and self.once:self.once=False;raise SyncError('OFFLINE')
            return result
    remote=Lost();service=Sync(store,keys,lambda *a:remote)
    assert service.configure(uid,{'backend':'webdav','url':'https://fixture.test'},create=True)['status']=='binding_pending'
    sync(service,uid);assert service.details(uid)['status']=='synced'


def test_migration_lost_target_ack_resumes_single_backend(peers):
    a,u,b,v,old=peers;resources.save(a.store,u,'topic',{'name':'must survive handover'});sync(a,u);sync(b,v);a.revoke(u,b.binding(v)['device_id']);a.pause(u)
    class Lost(Memory):
        once=True
        def put(self,path,*args,**kwargs):
            result=super().put(path,*args,**kwargs)
            if path=='head.bin' and self.once:self.once=False;raise SyncError('OFFLINE')
            return result
    target=Lost();a.factory=lambda config,*args:target if 'new.example' in config['url'] else old
    plan=a.migration_preview(u,{'backend':'webdav','url':'https://new.example.test'})
    with pytest.raises(SyncError):a.migrate(u,plan['id'])
    assert a.details(u)['migration']['status']=='source_closed'
    assert a.migrate(u,plan['id'])['url']=='https://new.example.test'
    assert len(resources.listing(a.store,u,'topic')['items'])==1


def test_full_hub_login_grant_bind_and_business_exchange(cloud,hub,tmp_path,monkeypatch):
    a,keys,*rest=cloud;uid=rest[-1];cfg,c=hub
    class Bridge:
        def request(self,method,path,body=None,headers=None,limit=100000000):
            response=c.request(method,path,content=body,headers=headers or {})
            return Reply(response.status_code,{**dict(response.headers),'date':formatdate(usegmt=True)},response.content)
    from backend.sync import transports
    monkeypatch.setattr(transports,'client_for',lambda *args:Bridge())
    first=Sync(a,keys);first.configure(uid,{'backend':'hub','url':'https://hub.example.test','email':'a@example.test','password':'test-cloud-password-123'},create=True)
    cfg2=replace(a.cfg,data=tmp_path/'peer2',erasures=tmp_path/'control2',key_file=tmp_path/'secrets2/keys.json');b=Store(cfg2).initialize();keys2=Keyring.create(cfg2.key_file);ErasureJournal(b,keys2).create();v=Auth(b,keys2).create_admin('different@example.test','test-cloud-password-123')
    grant=first.grant(uid);second=Sync(b,keys2);second.configure(v,{'backend':'hub','url':'https://hub.example.test','email':'a@example.test','password':'test-cloud-password-123','grant':grant['code'],'recovery':first.recovery(uid)})
    resources.save(a,uid,'topic',{'name':'opaque Hub business data'});sync(first,uid);sync(second,v)
    assert resources.listing(b,v,'topic')['items'][0]['data']['name']=='opaque Hub business data'
    assert first.details(uid)['metrics']['uploaded_operations']>=1


def test_erasure_cleans_current_and_retired_hub_spaces_then_forgets_private_material(cloud,hub,monkeypatch):
    store,keys,_,journal,_,uid=cloud;cfg,c=hub
    class Bridge:
        def request(self,method,path,body=None,headers=None,limit=100000000):
            response=c.request(method,path,content=body,headers=headers or {})
            return Reply(response.status_code,{**dict(response.headers),'date':formatdate(usegmt=True)},response.content)
    from backend.sync import transports
    from backend.cloud.auth import Actor
    from backend.cloud.lifecycle import erase
    monkeypatch.setattr(transports,'client_for',lambda *args:Bridge());service=Sync(store,keys);store.sync_service=service
    value={'backend':'hub','url':'https://hub.example.test','email':'a@example.test','password':'test-cloud-password-123'}
    service.configure(uid,value,create=True);sync(service,uid);service.pause(uid);service.unbind(uid,'解除本机绑定并保留本地资料')
    service.configure(uid,value,create=True);sync(service,uid)
    assert len(c.app.state.hub.query('SELECT id FROM spaces WHERE deleted=0'))==2
    result=erase(store,journal,Actor(uid,'user@example.test','user','active',stamp()),'test-cloud-password-123','注销并删除我的全部个人数据')
    assert result['remote_cleanup']['status']=='completed' and not store.active(uid)
    assert not c.app.state.hub.query('SELECT id FROM spaces WHERE deleted=0') and not c.app.state.hub.query('SELECT * FROM files')
    assert not service.private_folder(uid).exists()
