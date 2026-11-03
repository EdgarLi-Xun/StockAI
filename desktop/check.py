"""Offline packaged-runtime checks; no provider or market requests."""
import http.cookiejar
import json
import secrets
import subprocess
import sys
import urllib.request


def self_test(home,preferred):
    import akshare
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    from PySide6.QtCore import qVersion
    from backend.processes import module_command,hidden_process
    from backend.cloud.config import CloudConfig
    from desktop.host import Host,local_config
    host=Host(home,preferred,background=False).start()
    try:
        host.wait();cfg=local_config(home,host.port,False);password=secrets.token_urlsafe(24);email='desktop-check@stockai.test'
        def client():return urllib.request.build_opener(urllib.request.ProxyHandler({}),urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        first=client();second=client()
        def call(opener,path,payload=None):
            headers={'Origin':host.url}
            if payload is not None:
                jar=next(h.cookiejar for h in opener.handlers if isinstance(h,urllib.request.HTTPCookieProcessor))
                headers.update({'X-CSRF-Token':next(c.value for c in jar if c.name==cfg.csrf_cookie),'Content-Type':'application/json'})
            req=urllib.request.Request(host.url+path,data=json.dumps(payload).encode() if payload is not None else None,headers=headers)
            with opener.open(req,timeout=20) as r:return json.load(r)
        call(first,'/api/auth/csrf');created=call(first,'/api/desktop/setup',{'email':email,'password':password})
        call(second,'/api/auth/csrf');logged=call(second,'/api/auth/login',{'email':email,'password':password})
        same=created['user']['id']==logged['user']['id']
        ready=call(second,'/api/me/readiness');usage=call(first,'/api/me/usage')
        with first.open(host.url,timeout=10) as r:static=r.status==200 and b'/assets/' in r.read()
        if getattr(sys,'frozen',False):
            result=subprocess.run(module_command('backend.collector_worker','unknown','{}'),capture_output=True,encoding='utf-8',timeout=60,**hidden_process())
            value=json.loads(result.stdout);worker=result.returncode==1 and '未知采集动作' in value.get('error','')
        else:worker=True
        assert same and static and worker
        assert (home/'secrets/backup-identity.txt').is_file()
        from backend.cloud.store import Store
        store=Store(cfg);mode=store.one('PRAGMA journal_mode')
        # 覆盖真实打包后的对话 API 与执行路径，不能只把窗口加载成功当作助手可用。
        from backend.cloud import jobs,models
        from backend.cloud.crypto import Keyring
        uid=created['user']['id'];keys=Keyring(cfg.key_file)
        session=call(first,'/api/me/assistant/sessions',{'title':'Offline assistant check','client_key':secrets.token_hex(16)})
        path='/api/me/assistant/sessions/'+session['id']+'/messages'
        welcome=call(second,path,{'question':'你好','client_key':secrets.token_hex(16)})
        assert welcome['status']=='help' and 'StockAI' in welcome['reply']['answer']
        profile=models.save_profile(store,keys,uid,models.ProfileInput(name='Offline check fixture',provider='ollama',model='offline-test-only'))
        calls=[]
        def answer(fixed,messages,key):
            calls.append(messages)
            return json.dumps({'answer':'你好，我是StockAI项目助手，可以解释项目操作。','citations':['help:assistant']},ensure_ascii=False),{'total_tokens':12},'offline-check'
        for question in ('你好','继续'):
            pending=call(first,path,{'question':question,'profile_id':profile['id'],'client_key':secrets.token_hex(16)})
            assert pending['status']=='queued'
            result=jobs.run(store,keys,jobs.claim(store,'offline-assistant'),answer);assert result['status']=='success'
            completed=call(second,'/api/me/assistant/requests/'+pending['id'])
            assert completed['status']=='success' and completed['reply']['source']=='validated_model'
        assert len(calls)==2 and any(m['role']=='assistant' for m in calls[-1])
        return {'passed':True,'two_clients_same_owner':same,'readiness':ready['status'],'usage_available':bool(usage),'frontend_assets':static,'frozen_collector_dispatch':worker,'assistant_static_greeting':True,'assistant_model_greeting':True,'assistant_followup':True,'assistant_test_transport_calls':len(calls),'qt':qVersion(),'akshare':akshare.__version__,'sqlite_journal':list(mode.values())[0],'backup_identity_present':True,'real_provider':False,'real_market_requests':False}
    finally:host.close()
