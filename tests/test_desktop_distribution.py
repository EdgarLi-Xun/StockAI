"""First-run ownership, loopback protection, and standalone process boundaries."""
from dataclasses import replace
import socket
import sys

import pytest
from fastapi.testclient import TestClient
from test_cloud import cloud,PASSWORD,login
from backend.cloud import models
from backend.cloud.crypto import Keyring
from backend.cloud.lifecycle import ErasureJournal
from backend.cloud.store import Store
from backend.public_app import create_app
from backend.processes import module_command
from desktop.host import local_config,instance_id,choose_port


def empty_local(tmp_path):
    cfg=local_config(tmp_path,8891,False);store=Store(cfg).initialize();keys=Keyring.create(cfg.key_file);ErasureJournal(store,keys).create()
    return cfg,store


def csrf(client):
    value=client.get('/api/auth/csrf').json()['csrf_token']
    client.headers.update({'Origin':'http://127.0.0.1:8891','X-CSRF-Token':value})


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


def test_frozen_module_dispatch_is_explicit_and_bounded(monkeypatch):
    assert module_command('backend.collector_worker','calendar','{}')[-3:]==['backend.collector_worker','calendar','{}']
    monkeypatch.setattr(sys,'frozen',True,raising=False)
    assert module_command('backend.cloud.collector','sync','{}')[1:]==['--module','backend.cloud.collector','sync','{}']
    with pytest.raises(ValueError):module_command('arbitrary.module')


def test_busy_port_is_not_reused_or_stopped():
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1',0));port=occupied.getsockname()[1];occupied.listen()
        if port>65500:pytest.skip('OS selected an edge port')
        assert choose_port(port)!=port


def test_inherited_windows_code_page_is_normalized_for_worker_json(monkeypatch):
    import io,json
    from desktop.application import worker_stdio
    raw=io.BytesIO();pipe=io.TextIOWrapper(raw,encoding='cp936',newline='')
    monkeypatch.setattr(sys,'stdout',pipe);monkeypatch.setattr(sys,'stderr',pipe)
    worker_stdio()
    value={'error':'未知采集动作；中文与扩展字符 😃'}
    print(json.dumps(value,ensure_ascii=False));pipe.flush()
    assert json.loads(raw.getvalue().decode('utf-8'))==value
