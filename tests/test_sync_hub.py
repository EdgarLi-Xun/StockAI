import base64
from datetime import datetime,timezone
import json
from pathlib import Path
import shutil
import pytest
from fastapi.testclient import TestClient
from backend.hub.app import Config,Store,create_app
from backend.sync.protocol import canonical,digest,seal,open_sealed
from backend.sync.network import Reply
from backend.sync.transports import Hub
from test_cloud import PASSWORD


@pytest.fixture
def hub(tmp_path):
    cfg=Config(tmp_path/'data',tmp_path/'control',dev=True);store=Store(cfg);store.create_user('a@example.test',PASSWORD);store.create_user('b@example.test',PASSWORD)
    with TestClient(create_app(cfg)) as client:yield cfg,client


def login(c,email='a@example.test'):
    r=c.post('/v1/auth/login',json={'email':email,'password':PASSWORD});assert r.status_code==200;return {'Authorization':'Bearer '+r.json()['token']}


def test_hub_ciphertext_cas_identity_and_device_grant(hub):
    cfg,c=hub;account=login(c);space=c.post('/v1/spaces',json={'name':'first'},headers=account).json();headers={'Authorization':'Bearer '+space['token']};prefix='/v1/spaces/'+space['space_id']
    body=seal(b'k'*32,space['space_id'],{'private':'private facts'})
    first=c.put(prefix+'/files/head.bin',content=body,headers={**headers,'If-None-Match':'*'});assert first.status_code==201
    assert c.put(prefix+'/files/head.bin',content=b'bad',headers={**headers,'If-Match':'"wrong"'}).status_code==412
    assert c.get(prefix+'/files/head.bin',headers=headers).content==body
    assert c.get(prefix+'/files/head.bin',headers=login(c,'b@example.test')).status_code==401
    assert c.post('/v1/spaces',json={},headers=headers).status_code==401
    grant=c.post(prefix+'/grants',headers=headers).json();challenge=seal(b'k'*32,space['space_id'],{'nonce':grant['nonce']},'device-grant').decode()
    assert c.post(prefix+'/grants/'+grant['id'],headers=headers,json={'challenge':challenge}).status_code==200
    before=cfg.data/'before-bind.sqlite3';shutil.copy2(cfg.data/'hub.sqlite3',before)
    proof=open_sealed(b'k'*32,space['space_id'],c.post('/v1/bind/challenge',headers=account,json={'code':grant['code']}).json()['challenge'].encode(),'device-grant')['nonce']
    bound=c.post('/v1/bind',headers=account,json={'code':grant['code'],'proof':proof}).json();assert bound.get('token')
    assert c.post('/v1/bind',headers=account,json={'code':grant['code'],'proof':proof}).status_code==403
    # 回退业务索引后重启 Store，独立权威记录仍使授权码保持已消费。
    shutil.copy2(before,cfg.data/'hub.sqlite3');c.app.state.hub=Store(cfg)
    assert c.post('/v1/bind',headers=account,json={'code':grant['code'],'proof':proof}).status_code==403
    assert c.post(prefix+'/devices/'+bound['device_id']+'/revoke',headers=headers).status_code==200
    assert c.get(prefix+'/files/head.bin',headers={'Authorization':'Bearer '+bound['token']}).status_code==403
    assert b'private facts' not in b''.join(p.read_bytes() for p in (cfg.control/'objects').glob('*'))


def test_failed_login_rate_limit_is_durable(hub):
    _,c=hub
    for _ in range(20):assert c.post('/v1/auth/login',json={'email':'a@example.test','password':'bad'}).status_code==401
    assert c.post('/v1/auth/login',json={'email':'a@example.test','password':PASSWORD}).status_code==429


def test_authority_commit_recovery_before_next_live_request(hub):
    cfg,c=hub;headers=login(c);space=c.post('/v1/spaces',json={},headers=headers).json();store=c.app.state.hub
    with pytest.raises(RuntimeError):
        with store.tx() as db:
            store.commit(db,{'type':'delete','row':{'id':space['space_id']}})
            raise RuntimeError('fault after durable append before SQLite commit')
    assert c.get('/v1/spaces/'+space['space_id']+'/status',headers={'Authorization':'Bearer '+space['token']}).status_code==403


def test_remote_client_profiles_are_separate_and_https_only():
    from desktop.remote import origin,profile_id
    assert origin('https://EXAMPLE.com:443/')=='https://example.com'
    assert len({profile_id('https://one.test','a'),profile_id('https://two.test','a'),profile_id('https://one.test','b')})==3
    for value in ('http://example.test','https://u:p@example.test','https://example.test/private','file:///a'):
        with pytest.raises(ValueError):origin(value)


def test_hub_disabled_account_revokes_all_devices(hub):
    cfg,c=hub;account=login(c);space=c.post('/v1/spaces',headers=account,json={}).json()
    c.app.state.hub.disable_user('a@example.test')
    assert c.get('/v1/spaces/'+space['space_id']+'/status',headers={'Authorization':'Bearer '+space['token']}).status_code==403
    assert c.post('/v1/auth/login',json={'email':'a@example.test','password':PASSWORD}).status_code==401


def test_cursor_pages_are_owned_deduplicated_and_ack_survives_index_restore(hub):
    cfg,c=hub;account=login(c);space=c.post('/v1/spaces',headers=account,json={}).json();headers={'Authorization':'Bearer '+space['token']};prefix='/v1/spaces/'+space['space_id']
    before=cfg.data/'before-pages.sqlite3';shutil.copy2(cfg.data/'hub.sqlite3',before)
    for i in range(5):
        data=('cipher '+str(i)).encode();path=prefix+'/files/objects/'+digest(data)+'.bin'
        assert c.put(path,content=data,headers={**headers,'If-None-Match':'*'}).status_code==201
        assert c.put(path,content=data,headers={**headers,'If-None-Match':'*'}).status_code==412
    cursor=0;received=[]
    for _ in range(3):
        page=c.get(prefix+'/changes',params={'cursor':cursor,'limit':2},headers=headers).json();assert len(page['items'])<=2
        received+=page['items'];cursor=page['next_cursor']
    assert len({r['seq'] for r in received})==5 and not page['has_more']
    assert c.post(prefix+'/changes/ack',json={'cursor':cursor},headers=headers).status_code==200
    assert c.get(prefix+'/changes',headers=login(c,'b@example.test')).status_code==401
    assert c.get(prefix+'/changes',params={'cursor':cursor+1000},headers=headers).status_code==409
    shutil.copy2(before,cfg.data/'hub.sqlite3');c.app.state.hub=Store(cfg)
    replay=c.get(prefix+'/changes',params={'cursor':cursor},headers=headers).json()
    assert replay['items']==[] and replay['acknowledged']==cursor
    assert len(c.get(prefix+'/changes',headers=headers).json()['items'])==5


def test_expired_device_grant_cannot_finalize_or_bind(hub):
    cfg,c=hub;account=login(c);space=c.post('/v1/spaces',json={},headers=account).json();headers={'Authorization':'Bearer '+space['token']};prefix='/v1/spaces/'+space['space_id'];grant=c.post(prefix+'/grants',headers=headers).json()
    with c.app.state.hub.tx() as db:db.execute("UPDATE grants SET expires='2000-01-01T00:00:00+00:00' WHERE id=?",(grant['id'],))
    assert c.post(prefix+'/grants/'+grant['id'],headers=headers,json={'challenge':'opaque fixture'}).status_code==409
    assert c.post('/v1/bind/challenge',headers=account,json={'code':grant['code']}).status_code==409
    assert c.post('/v1/bind',headers=account,json={'code':grant['code'],'proof':grant['nonce']}).status_code==403
