"""First-run ownership, desktop/LAN clients, and standalone process boundaries."""
from dataclasses import replace
import ipaddress
import socket
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from test_cloud import cloud,PASSWORD,login
from backend.cloud import models
from backend.cloud.crypto import Keyring
from backend.cloud.lifecycle import ErasureJournal
from backend.cloud.store import Store
from backend.public_app import create_app
from backend.processes import module_command
from desktop.host import Host,local_config,instance_id,choose_port


def empty_local(tmp_path):
    cfg=local_config(tmp_path,8891,False);store=Store(cfg).initialize();keys=Keyring.create(cfg.key_file);ErasureJournal(store,keys).create()
    return cfg,store


def csrf(client):
    value=client.get('/api/auth/csrf').json()['csrf_token']
    client.headers.update({'Origin':str(client.base_url).rstrip('/'),'X-CSRF-Token':value})


def test_local_first_run_two_clients_share_owner_and_private_data(tmp_path):
    cfg,store=empty_local(tmp_path)
    with TestClient(create_app(cfg),base_url=cfg.public_url,client=('127.0.0.1',41000)) as first:
        assert not first.get('/api/health').json()['initialized'];csrf(first)
        created=first.post('/api/desktop/setup',json={'email':'owner@example.test','password':PASSWORD})
        assert created.status_code==200 and created.json()['user']['verified']
        owner=created.json()['user']['id']
        assert first.post('/api/desktop/setup',json={'email':'other@example.test','password':PASSWORD},headers={'X-CSRF-Token':first.cookies[cfg.csrf_cookie]}).status_code==409
        # 桌面与浏览器使用不同会话，但资料始终归同一用户。
        second=TestClient(first.app,base_url=cfg.public_url,client=('127.0.0.1',41001))
        try:
            csrf(second);logged=second.post('/api/auth/login',json={'email':'owner@example.test','password':PASSWORD})
            assert logged.status_code==200 and logged.json()['user']['id']==owner
            first.headers['X-CSRF-Token']=first.cookies[cfg.csrf_cookie]
            saved=first.post('/api/me/model-profiles',json={'name':'桌面创建的本机模型','provider':'ollama','model':'test-local-model'})
            assert saved.status_code==200,saved.text
            assert second.get('/api/me/model-profiles').json()[0]['id']==saved.json()['id']
        finally:second.close()


@pytest.mark.parametrize('peer,headers',[('192.168.1.3',{}),('127.0.0.1',{'X-Forwarded-For':'127.0.0.1'}),('127.0.0.1',{'Forwarded':'for=127.0.0.1'})])
def test_local_setup_rejects_remote_and_forwarded_clients(tmp_path,peer,headers):
    cfg,store=empty_local(tmp_path)
    with TestClient(create_app(cfg),base_url=cfg.public_url,client=(peer,41000)) as c:
        csrf(c);r=c.post('/api/desktop/setup',json={'email':'owner@example.test','password':PASSWORD},headers=headers)
        assert r.status_code==403 and not store.one('SELECT id FROM users')


def test_local_setup_requires_origin_csrf_and_password_policy(tmp_path):
    cfg,store=empty_local(tmp_path)
    with TestClient(create_app(cfg),base_url=cfg.public_url,client=('127.0.0.1',41000)) as c:
        value={'email':'owner@example.test','password':PASSWORD}
        assert c.post('/api/desktop/setup',json=value).status_code==403
        csrf(c)
        assert c.post('/api/desktop/setup',json=value,headers={'Origin':'https://elsewhere.example'}).status_code==403
        assert c.post('/api/desktop/setup',json={**value,'password':'short'}).status_code==400
        assert not store.one('SELECT id FROM users')


def test_public_deployment_never_exposes_local_setup(cloud):
    store,keys,auth,journal,admin,uid=cloud
    with TestClient(create_app(store.cfg)) as c:
        login(c)
        assert c.post('/api/desktop/setup',json={'email':'owner@example.test','password':PASSWORD}).status_code==404
        assert c.get('/api/health').json()['edition']=='public'


def test_desktop_configuration_cannot_become_a_public_shortcut(tmp_path):
    cfg=local_config(tmp_path,8891,False);cfg.validate()
    for bad in (replace(cfg,public_url='https://public.example'),replace(cfg,dev=False),replace(cfg,desktop_instance='')):
        with pytest.raises(ValueError):bad.validate()
    other=local_config(tmp_path/'another',8892,False)
    assert cfg.session_cookie!=other.session_cookie and cfg.csrf_cookie!=other.csrf_cookie


@pytest.mark.parametrize('address',['192.168.1.2','10.20.30.40'])
def test_lan_login_shares_workspace_and_requires_its_own_origin(tmp_path,address):
    cfg,store=empty_local(tmp_path)
    origin=f'http://{address}:8891'
    cfg=replace(cfg,desktop_origins=(origin,'http://localhost:8891'))
    with TestClient(create_app(cfg),base_url=cfg.public_url,client=('127.0.0.1',41000)) as local:
        lan=TestClient(local.app,base_url=origin,client=('192.168.1.3',41001))
        try:
            assert lan.get('/api/health').status_code==200
            assert lan.get('/api/auth/me').status_code==401
            csrf(lan)
            owner={'email':'owner@example.test','password':PASSWORD}
            assert lan.post('/api/desktop/setup',json=owner).status_code==403
            assert not store.one('SELECT id FROM users')
            csrf(local)
            created=local.post('/api/desktop/setup',json=owner)
            assert created.status_code==200
            for wrong_origin in (cfg.public_url,'http://localhost:8891','https://attacker.example','null'):
                rejected=lan.post('/api/auth/login',json=owner,headers={'Origin':wrong_origin})
                assert rejected.status_code==403 and rejected.json()['code']=='ORIGIN_REJECTED'
            assert lan.post('/api/auth/login',json=owner,headers={'X-CSRF-Token':''}).status_code==403
            logged=lan.post('/api/auth/login',json=owner)
            assert logged.status_code==200 and logged.json()['user']['id']==created.json()['user']['id']
            csrf(lan)
            saved=lan.post('/api/me/model-profiles',json={'name':'LAN 模型配置','provider':'ollama','model':'test-lan-only'})
            assert saved.status_code==200,saved.text
            assert local.get('/api/me/model-profiles').json()[0]['id']==saved.json()['id']
            for bad_host in ('attacker.example:8891','192.168.99.99:8891'):
                assert lan.get('/api/health',headers={'Host':bad_host}).status_code==400
        finally:lan.close()


@pytest.mark.parametrize('origin',[
    'http://attacker.example:8891','http://0.0.0.0:8891','http://224.0.0.1:8891',
    'http://255.255.255.255:8891','http://192.168.1.2:8892','https://192.168.1.2:8891',
    'http://user@192.168.1.2:8891','http://192.168.1.2:8891/path',
])
def test_desktop_origins_are_explicit_ipv4_addresses_on_the_service_port(tmp_path,origin):
    cfg=replace(local_config(tmp_path,8891,False),desktop_origins=(origin,))
    with pytest.raises(ValueError):cfg.validate()
    cfg=replace(cfg,desktop_mode=False,desktop_origins=('http://192.168.1.2:8891',))
    with pytest.raises(ValueError):cfg.validate()


def test_desktop_real_http_accepts_loopback_and_lan_with_separate_sessions(tmp_path):
    addresses=socket.gethostbyname_ex(socket.gethostname())[2]
    address=next((value for value in addresses if not ipaddress.ip_address(value).is_loopback),None)
    if not address:pytest.skip('No LAN IPv4 address is available')
    with socket.socket() as probe:
        probe.bind(('0.0.0.0',0));preferred=probe.getsockname()[1]
    host=Host(tmp_path,preferred,background=False).start()
    try:
        host.wait()
        with httpx.Client(base_url=host.url,trust_env=False,timeout=15) as local, httpx.Client(base_url=f'http://{address}:{host.port}',trust_env=False,timeout=15) as lan:
            assert local.get('/api/health').json()['instance']==lan.get('/api/health').json()['instance']
            assert local.get('/').status_code==lan.get('/').status_code==200
            assert lan.get('/api/me/model-profiles').status_code==401
            owner={'email':'real-http-check@stockai.test','password':PASSWORD}
            csrf(lan)
            assert lan.post('/api/desktop/setup',json=owner).status_code==403
            csrf(local)
            created=local.post('/api/desktop/setup',json=owner)
            assert created.status_code==200,created.text
            logged=lan.post('/api/auth/login',json=owner)
            assert logged.status_code==200,logged.text
            assert logged.json()['user']['id']==created.json()['user']['id']
            csrf(lan)
            saved=lan.post('/api/me/model-profiles',json={'name':'真实 HTTP 隔离测试','provider':'ollama','model':'test-lan-only'})
            assert saved.status_code==200,saved.text
            assert local.get('/api/me/model-profiles').json()[0]['id']==saved.json()['id']
    finally:host.close()


def test_frozen_module_dispatch_is_explicit_and_bounded(monkeypatch):
    assert module_command('backend.collector_worker','calendar','{}')[-3:]==['backend.collector_worker','calendar','{}']
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    assert module_command('backend.cloud.collector','sync','{}')[1:]==['--module','backend.cloud.collector','sync','{}']
    with pytest.raises(ValueError):module_command('arbitrary.module')


def test_busy_port_is_not_reused_or_stopped():
    # Windows may reserve every port following an ephemeral allocation; first
    # establish that the next port is available on all interfaces as well.
    for port in range(38000,39000,2):
        with socket.socket() as occupied, socket.socket() as available:
            try:
                occupied.bind(('0.0.0.0',port));available.bind(('0.0.0.0',port+1))
            except OSError:continue
            occupied.listen();available.close()
            assert choose_port(port)==port+1
            assert occupied.getsockname()[1]==port
            return
    pytest.skip('No adjacent IPv4 test ports are available')


def test_desktop_page_cache_is_cleared_without_touching_login_storage(tmp_path):
    from desktop.host import prepare_page_cache
    cache=tmp_path/'desktop-cache'
    stale=cache/'Cache'/'old.js'
    stale.parent.mkdir(parents=True)
    stale.write_text('v3.2.5',encoding='utf-8')
    profile_dir=tmp_path/'desktop-profile'
    profile_dir.mkdir()
    (profile_dir/'Cookies').write_text('keep',encoding='utf-8')
    class Profile:
        class HttpCacheType:
            NoCache='nocache'
        def __init__(self):
            self.path=None
            self.kind=None
        def setCachePath(self,path):self.path=path
        def setHttpCacheType(self,kind):self.kind=kind
    profile=Profile()
    prepare_page_cache(profile,cache)
    assert not stale.exists() and cache.is_dir() and not any(cache.rglob('*'))
    assert (profile_dir/'Cookies').read_text(encoding='utf-8')=='keep'
    assert profile.path==str(cache) and profile.kind=='nocache'


def test_shell_document_is_not_cached(cloud):
    store,*_=cloud
    with TestClient(create_app(store.cfg)) as c:
        page=c.get('/')
        if page.status_code==404:pytest.skip('frontend build unavailable')
        assert page.status_code==200 and page.headers['cache-control']=='no-store'


def test_debug_session_stays_on_source_instead_of_installed_build(monkeypatch):
    from desktop.application import keep_source_process
    monkeypatch.setattr(sys,'gettrace',lambda:None)
    monkeypatch.delitem(sys.modules,'pydevd',raising=False)
    monkeypatch.delitem(sys.modules,'debugpy',raising=False)
    assert keep_source_process() is False
    assert keep_source_process(True) is True
    monkeypatch.setattr(sys,'gettrace',lambda:object())
    assert keep_source_process() is True
    monkeypatch.setattr(sys,'gettrace',lambda:None)
    monkeypatch.setitem(sys.modules,'debugpy',object())
    assert keep_source_process() is True


def test_inherited_windows_code_page_is_normalized_for_worker_json(monkeypatch):
    import io,json
    from desktop.application import worker_stdio
    raw=io.BytesIO();pipe=io.TextIOWrapper(raw,encoding='cp936',newline='')
    monkeypatch.setattr(sys,'stdout',pipe);monkeypatch.setattr(sys,'stderr',pipe)
    worker_stdio()
    value={'error':'未知采集动作；中文与扩展字符 😃'}
    print(json.dumps(value,ensure_ascii=False));pipe.flush()
    assert json.loads(raw.getvalue().decode('utf-8'))==value
