"""Authenticated synchronization and operator upgrade plans, with no shell commands from HTTP."""
import base64
import json
import time
import uuid
from fastapi import Depends,Request
from backend.cloud.auth import AuthError
from backend.sync.protocol import SyncError
from backend.upgrades.trust import UpgradeError


def install(app,rt,user,admin):
    @app.exception_handler(SyncError)
    async def sync_error(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'code':exc.code,'detail':str(exc)},409 if exc.code in ('CONFLICT','LEASE','QUOTA','ROLLBACK','MAINTENANCE') else 400)

    @app.exception_handler(UpgradeError)
    async def upgrade_error(request,exc):
        from fastapi.responses import JSONResponse
        return JSONResponse({'code':exc.code,'detail':str(exc)},409)

    @app.get('/api/me/sync')
    def sync_status(request:Request,a=Depends(user)):return rt(request).store.sync_service.details(a.id)

    @app.get('/api/me/workspace-setup')
    def workspace_setup(request:Request,a=Depends(user)):
        from .workspace_setup import status
        return status(rt(request).store,a.id)

    @app.post('/api/me/workspace-setup')
    def workspace_source(value:dict,request:Request,a=Depends(user)):
        from .workspace_setup import select
        return select(rt(request).store,a.id,value)

    @app.post('/api/me/sync/migration/preview')
    def migration_preview(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.migration_preview(a.id,value)

    @app.post('/api/me/sync/migration/confirm')
    def migration_confirm(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.migrate(a.id,value.get('id'))

    @app.post('/api/me/sync/connect')
    def connect(value:dict,request:Request,a=Depends(user)):
        return rt(request).store.sync_service.configure(a.id,value,create=value.get('create') is True)

    @app.post('/api/me/sync/preview')
    def preview(request:Request,a=Depends(user)):return rt(request).store.sync_service.preview(a.id)

    @app.post('/api/me/sync/run')
    def run(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.synchronize(a.id,value.get('preview_id'))

    @app.post('/api/me/sync/pause')
    def pause(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.pause(a.id,value.get('paused') is not False,value.get('automatic'))

    @app.post('/api/me/sync/unbind')
    def unbind(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.unbind(a.id,value.get('confirmation'))

    @app.get('/api/me/sync/devices')
    def devices(request:Request,a=Depends(user)):return rt(request).store.sync_service.devices(a.id)

    @app.post('/api/me/sync/devices/revoke')
    def revoke(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.revoke(a.id,value.get('device'))

    @app.post('/api/me/sync/grant')
    def grant(request:Request,a=Depends(user)):return rt(request).store.sync_service.grant(a.id)

    @app.post('/api/me/sync/recovery')
    def recovery(value:dict,request:Request,a=Depends(user)):
        runtime=rt(request)
        from .auth import password_matches
        record=runtime.store.one('SELECT password_hash FROM users WHERE id=?',(a.id,))
        if not password_matches(record['password_hash'],str(value.get('password',''))):raise AuthError('BAD_PASSWORD','密码验证失败',403)
        return runtime.store.sync_service.recovery(a.id)

    @app.post('/api/me/sync/rotate')
    def rotate(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.rotate(a.id,value.get('confirmation'))

    @app.post('/api/me/sync/delete')
    def delete(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.request_delete(a.id,value.get('confirmation'))

    @app.post('/api/me/sync/executor')
    def executor(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.executor(a.id,value.get('device'))

    @app.post('/api/me/sync/receipts/{ident}/acknowledge')
    def receipt_acknowledge(ident:str,value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.acknowledge_receipt(a.id,ident,value.get('confirmation'))

    @app.post('/api/me/sync/account-transfer')
    def transfer(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.account_transfer(a.id,value.get('account_id'),value.get('device'))

    @app.post('/api/me/sync/limits')
    def limits(value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.request_limits(a.id,value)

    @app.get('/api/me/sync/conflicts')
    def conflicts(request:Request,a=Depends(user)):
        rows=rt(request).store.query("SELECT * FROM sync_conflicts WHERE user_id=? AND state='open' ORDER BY created_at",(a.id,))
        for row in rows:
            for key in ('base_json','local_json','remote_json','heads_json'):row[key[:-5]]=json.loads(row.pop(key) or 'null')
        return rows

    @app.post('/api/me/sync/conflicts/{ident}')
    def resolve(ident:str,value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.resolve(a.id,ident,value.get('choice'),value.get('payload'))

    @app.get('/api/me/sync/checkpoints')
    def points(request:Request,a=Depends(user)):return rt(request).store.sync_service.checkpoints(a.id)

    @app.post('/api/me/sync/checkpoints/{ident}/preview')
    def point_preview(ident:str,request:Request,a=Depends(user)):return rt(request).store.sync_service.point_preview(a.id,ident)

    @app.post('/api/me/sync/checkpoints/{ident}/restore')
    def point_restore(ident:str,value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.restore_point(a.id,ident,value.get('confirmation'))

    @app.post('/api/me/sync/conflicts/{ident}/fork')
    def fork(ident:str,value:dict,request:Request,a=Depends(user)):return rt(request).store.sync_service.fork(a.id,ident,value.get('choice'))

    @app.post('/api/me/sync/restore-deleted')
    def restore_deleted(value:dict,request:Request,a=Depends(user)):
        if value.get('confirmation')!='以新版本恢复这条记录':raise SyncError('CONFLICT')
        return rt(request).store.sync_service.restore_deleted(a.id,value.get('kind'),value.get('object'),value.get('tombstone'))

    # Web/本机服务升级均需管理员；桌面远程访问这里仍只看到远程 Web 组件计划。
    @app.get('/api/admin/upgrades')
    def upgrade_status(request:Request,a=Depends(admin)):return rt(request).upgrades.status()

    @app.post('/api/admin/upgrades/check')
    def check(request:Request,a=Depends(admin)):return rt(request).upgrades.check()

    @app.post('/api/admin/upgrades/cleanup')
    def cleanup(value:dict,request:Request,a=Depends(admin)):
        if value.get('confirmation')!='清理已过期的升级缓存':raise UpgradeError('CONFIRMATION')
        return rt(request).upgrades.cleanup(execute=True)

    @app.put('/api/admin/upgrades/settings')
    def settings(value:dict,request:Request,a=Depends(admin)):
        manager=rt(request).upgrades;current=manager.state.get('settings')
        if set(value)-{'channel','automatic_check','automatic_download'} or value.get('channel',current['channel']) not in ('stable','preview'):raise ValueError('更新设置无效')
        if any(type(value[k]) is not bool for k in ('automatic_check','automatic_download') if k in value):raise ValueError('更新开关无效')
        manager.state.put('settings',{**current,**value});return manager.status()

    @app.post('/api/admin/upgrades/plan')
    def plan(value:dict,request:Request,a=Depends(admin)):
        if value.get('local') is True:
            if 'envelope' in value:raise UpgradeError('TARGET')
            return rt(request).upgrades.prepare_local()
        return rt(request).upgrades.prepare(value.get('envelope'))

    @app.post('/api/admin/upgrades/{ident}/download')
    def download(ident:str,request:Request,a=Depends(admin)):return rt(request).upgrades.download(ident)

    @app.post('/api/admin/upgrades/{ident}/upload')
    def upload(ident:str,value:dict,request:Request,a=Depends(admin)):
        try:data=base64.b64decode(value['data'],validate=True)
        except Exception:raise ValueError('离线包分片无效') from None
        return rt(request).upgrades.upload(ident,int(value['offset']),data)

    @app.post('/api/admin/upgrades/{ident}/preflight')
    def preflight(ident:str,request:Request,a=Depends(admin)):return rt(request).upgrades.preflight(ident)

    @app.post('/api/admin/upgrades/{ident}/cancel')
    def cancel(ident:str,request:Request,a=Depends(admin)):
        manager=rt(request).upgrades
        current=manager.state.get('transaction:'+ident)
        if current and current['status']=='downloading':manager.state.put('cancel:'+ident,True);return {'status':'cancel_requested'}
        with manager.lock():
            tx=manager.state.get('transaction:'+ident)
            if not tx or tx['status'] not in ('planned','verified','download_paused','download_failed','awaiting_operator','preflight_blocked','waiting_confirmation','uploading'):raise UpgradeError('BUSY','当前阶段不能取消，请等待安全边界')
            return manager.update(tx,'cancelled')

    @app.post('/api/admin/upgrades/{ident}/execute')
    def execute(ident:str,value:dict,request:Request,a=Depends(admin)):
        runtime=rt(request);manager=runtime.upgrades
        if not runtime.cfg.desktop_mode:raise UpgradeError('EXECUTOR')
        with manager.lock():
            tx=manager.state.get('transaction:'+ident)
            if not tx or value.get('confirmation')!=tx['confirmation']:raise UpgradeError('CONFIRMATION')
            if tx['status']=='exit_requested':return tx
            if tx['status'] not in ('verified','waiting_confirmation','preflight_blocked'):raise UpgradeError('BUSY')
            manager.maintenance(True)
            try:
                tx=manager.preflight(ident,executor=True)
                if tx['status']!='waiting_confirmation':manager.maintenance(False);return tx
                return manager.update(tx,'exit_requested',actor=a.id)
            except Exception:manager.maintenance(False);raise

    @app.post('/api/me/client-state')
    def client_state(value:dict,request:Request,a=Depends(user)):
        store=rt(request).store
        if not hasattr(store,'upgrade_clients'):store.upgrade_clients={}
        key=str(value.get('id',''))
        if not key or len(key)>100:raise ValueError('客户端标识无效')
        now=time.time();store.upgrade_clients={k:v for k,v in store.upgrade_clients.items() if v['expires']>now}
        store.upgrade_clients[a.id+':'+key]={'dirty':value.get('dirty') is True,'expires':now+90}
        return {'acknowledged':True}
