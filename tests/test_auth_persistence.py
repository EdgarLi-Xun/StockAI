"""Remembered and temporary sessions share expiry, CSRF and revocation checks."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from http.cookies import SimpleCookie
import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from backend.cloud.auth import REMEMBERED_SESSION_TTL, SESSION_TTL, AuthError
from backend.cloud.crypto import digest
from backend.public_app import create_app
from test_cloud import PASSWORD, cloud
from test_desktop_distribution import empty_local


def csrf(client):
    token=client.get('/api/auth/csrf').json()['csrf_token']
    client.headers.update({'Origin':str(client.base_url).rstrip('/'),'X-CSRF-Token':token})


def sign_in(client,remember=True):
    csrf(client)
    response=client.post('/api/auth/login',json={'email':'user@example.test','password':PASSWORD,'remember':remember})
    assert response.status_code==200,response.text
    return response


def cookies(response):
    result=SimpleCookie()
    for name,value in response.headers.raw:
        if name.lower()==b'set-cookie':result.load(value.decode('latin1'))
    return result


def config(store,desktop):
    return replace(store.cfg,desktop_mode=desktop,desktop_instance='remembertestinstance',public_url='http://127.0.0.1:8897' if desktop else store.cfg.public_url)


@pytest.mark.parametrize('desktop',[False,True])
@pytest.mark.parametrize('remember',[True,False,None])
def test_login_choice_controls_cookie_persistence_and_server_expiry(cloud,desktop,remember):
    store,_,_,_,_,uid=cloud
    cfg=config(store,desktop)
    ttl=REMEMBERED_SESSION_TTL if remember else SESSION_TTL
    with TestClient(create_app(cfg),base_url=cfg.public_url) as client:
        csrf(client)
        payload={'email':'user@example.test','password':PASSWORD}
        if remember is not None:payload['remember']=remember
        response=client.post('/api/auth/login',json=payload)
        assert response.status_code==200,response.text
        saved=cookies(response)
        assert saved[cfg.session_cookie]['max-age']==(str(ttl) if remember else '')
        assert saved[cfg.csrf_cookie]['max-age']==str(ttl)
        assert saved[cfg.session_cookie]['httponly']
        assert saved[cfg.session_cookie]['samesite']=='lax'
        row=store.one('SELECT * FROM sessions WHERE token_hash=?',(digest(client.cookies[cfg.session_cookie]),))
        remaining=(datetime.fromisoformat(row['expires_at'])-datetime.now(timezone.utc)).total_seconds()
        assert ttl-10<remaining<=ttl
        assert row['user_id']==uid
        assert client.cookies[cfg.session_cookie] not in str(row)
        assert client.get('/api/auth/me').json()['id']==uid


@pytest.mark.parametrize('desktop',[False,True])
def test_remembered_login_survives_service_restart_and_csrf_refresh(cloud,desktop):
    store,_,_,_,_,uid=cloud
    cfg=config(store,desktop)
    with TestClient(create_app(cfg),base_url=cfg.public_url) as first:
        sign_in(first)
        saved={name:first.cookies[name] for name in (cfg.session_cookie,cfg.csrf_cookie)}
        saved_jar=httpx.Cookies(first.cookies)
        session_hash=digest(saved[cfg.session_cookie])
        # 模拟第29天重新打开；刷新 CSRF 不续期原来的登录会话。
        expires=(datetime.now(timezone.utc)+timedelta(days=1)).isoformat(timespec='microseconds')
        store.execute('UPDATE sessions SET expires_at=? WHERE token_hash=?',(expires,session_hash))
    with TestClient(create_app(cfg),base_url=cfg.public_url) as reopened:
        reopened.cookies.update(saved_jar)
        response=reopened.get('/api/auth/csrf')
        assert 86390<int(cookies(response)[cfg.csrf_cookie]['max-age'])<=86400
        assert cfg.session_cookie not in cookies(response)
        assert store.one('SELECT expires_at FROM sessions WHERE token_hash=?',(session_hash,))['expires_at']==expires
        reopened.headers.update({'Origin':cfg.public_url,'X-CSRF-Token':response.json()['csrf_token']})
        assert reopened.get('/api/auth/me').json()['id']==uid
        # 旧的 CSRF 不再能修改当前会话。
        assert reopened.post('/api/auth/logout',headers={'X-CSRF-Token':saved[cfg.csrf_cookie]}).status_code==403
        logged_out=reopened.post('/api/auth/logout')
        assert logged_out.status_code==200
        assert cookies(logged_out)[cfg.session_cookie]['max-age']=='0'
        assert cookies(logged_out)[cfg.csrf_cookie]['max-age']=='0'
        assert not reopened.cookies.get(cfg.session_cookie)
        assert not reopened.cookies.get(cfg.csrf_cookie)
        assert reopened.get('/api/auth/me').status_code==401
        reopened.cookies.update(saved_jar)
        assert reopened.get('/api/auth/me').status_code==401
        csrf(reopened)
        assert not reopened.cookies.get(cfg.session_cookie)
        assert reopened.post('/api/auth/login',json={'email':'user@example.test','password':PASSWORD,'remember':False}).status_code==200


def test_https_remembered_cookies_keep_security_attributes(cloud):
    store,_,auth,*_=cloud
    cfg=replace(store.cfg,dev=False,public_url='https://testserver')
    # 直接验证生产路由签发的 Cookie；真实生产启动仍由 SQLite 修复版本门槛测试覆盖。
    app=create_app(cfg);app.state.runtime=SimpleNamespace(store=store,auth=auth)
    endpoint=next(route.endpoint for route in app.routes if route.path=='/api/auth/login')
    request=Request({'type':'http','app':app,'client':('127.0.0.1',42000),'headers':[]})
    saved=cookies(endpoint({'email':'user@example.test','password':PASSWORD,'remember':True},request))
    for name in (cfg.session_cookie,cfg.csrf_cookie):
        assert name.startswith('__Host-')
        assert saved[name]['secure'] and saved[name]['path']=='/' and not saved[name]['domain']
        assert saved[name]['samesite']=='lax'
        assert saved[name]['max-age']==str(REMEMBERED_SESSION_TTL)
    assert saved[cfg.session_cookie]['httponly']


@pytest.mark.parametrize('reason',['expired','password-reset','disabled'])
def test_remembered_sessions_still_expire_and_revoke(cloud,reason):
    store,keys,auth,_,admin,uid=cloud
    with TestClient(create_app(store.cfg)) as client:
        sign_in(client)
        if reason=='expired':
            expires=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat(timespec='microseconds')
            store.execute('UPDATE sessions SET expires_at=? WHERE user_id=?',(expires,uid))
        elif reason=='password-reset':
            with store.tx(immediate=True) as connection:auth._mail_token(connection,uid,'reset')
            row=store.one("SELECT * FROM mail_outbox WHERE user_id=? AND purpose='reset'",(uid,))
            token=json.loads(keys.open(row['payload_json'],site=store.site_id,user=uid,resource=row['id'],purpose='mail'))['token']
            csrf(client)
            response=client.post('/api/auth/reset-password',json={'token':token,'password':PASSWORD+'-new'})
            assert response.status_code==200,response.text
        else:
            admin_token=auth.login('admin@example.test',PASSWORD,'admin')[0]
            auth.disable(auth.actor(admin_token),uid,True)
        assert client.get('/api/auth/me').status_code==401
        csrf(client)
        assert not client.cookies.get(store.cfg.session_cookie)


def test_invalid_remember_flag_cannot_enable_persistence(cloud):
    store,_,auth,_,_,_=cloud
    with TestClient(create_app(store.cfg)) as client:
        csrf(client)
        for value in ('false',1,None,[]):
            response=client.post('/api/auth/login',json={'email':'user@example.test','password':PASSWORD,'remember':value})
            assert response.status_code==400
            assert not store.one('SELECT * FROM sessions')
            with pytest.raises(AuthError):auth.login('user@example.test',PASSWORD,'test',remember=value)


@pytest.mark.parametrize('remember',[True,False])
def test_first_desktop_account_respects_remember_choice(tmp_path,remember):
    cfg,store=empty_local(tmp_path)
    with TestClient(create_app(cfg),base_url=cfg.public_url,client=('127.0.0.1',42000)) as client:
        csrf(client)
        payload={'email':'owner@example.test','password':PASSWORD,'remember':'false'}
        assert client.post('/api/desktop/setup',json=payload).status_code==400
        assert not store.one('SELECT id FROM users')
        response=client.post('/api/desktop/setup',json={**payload,'remember':remember})
        assert response.status_code==200,response.text
        assert cookies(response)[cfg.session_cookie]['max-age']==(str(REMEMBERED_SESSION_TTL) if remember else '')
        assert client.get('/api/auth/me').json()['email']=='owner@example.test'
