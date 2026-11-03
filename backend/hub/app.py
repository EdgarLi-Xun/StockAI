from __future__ import annotations
import base64
from contextlib import asynccontextmanager,contextmanager
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import uuid
from fastapi import FastAPI,Request
from fastapi.responses import JSONResponse,Response
from backend.cloud.auth import hash_password,password_matches,email_value
from backend.cloud.crypto import atomic_private
from backend.cloud.lifecycle import ProcessLock
from backend.sync.protocol import canonical,digest,ID,MAX_OBJECT
from backend.sync.transports import safe_path
from backend.cloud import VERSION


class HubError(ValueError):
    def __init__(self,code,status=400):self.code,self.status=code,status;super().__init__(code)


@dataclass(frozen=True)
class Config:
    data:Path
    control:Path
    max_space_bytes:int=1024*1024*1024
    dev:bool=False

    @classmethod
    def environment(cls):return cls(Path(os.environ.get('STOCKAI_HUB_DATA_DIR','hub-data')).resolve(),Path(os.environ.get('STOCKAI_HUB_CONTROL_DIR','hub-control')).resolve(),int(os.environ.get('STOCKAI_HUB_SPACE_BYTES',1073741824)),os.environ.get('STOCKAI_HUB_DEV')=='1')


SCHEMA="""
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,email TEXT UNIQUE NOT NULL,password_hash TEXT NOT NULL,active INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),expires TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS spaces(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),created TEXT NOT NULL,deleted INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS devices(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),space_id TEXT NOT NULL REFERENCES spaces(id),name TEXT NOT NULL,hash TEXT UNIQUE NOT NULL,active INTEGER NOT NULL,generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS grants(id TEXT PRIMARY KEY,space_id TEXT NOT NULL,user_id TEXT NOT NULL,code_hash TEXT UNIQUE NOT NULL,nonce_hash TEXT NOT NULL,challenge TEXT,expires TEXT NOT NULL,used INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS files(space_id TEXT NOT NULL,path TEXT NOT NULL,hash TEXT NOT NULL,size INTEGER NOT NULL,sequence INTEGER NOT NULL,PRIMARY KEY(space_id,path));
CREATE TABLE IF NOT EXISTS applied(seq INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS consumed_grants(id TEXT PRIMARY KEY);
CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY,start REAL NOT NULL,count INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS changes(seq INTEGER PRIMARY KEY,space_id TEXT NOT NULL,path TEXT NOT NULL,hash TEXT NOT NULL,size INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS changes_space ON changes(space_id,seq);
CREATE TABLE IF NOT EXISTS device_cursors(device_id TEXT PRIMARY KEY,cursor INTEGER NOT NULL);
"""


def utc():return datetime.now(timezone.utc)
def hashed(value):return hashlib.sha256(value.encode()).hexdigest()


class Store:
    def __init__(self,cfg):
        if cfg.data==cfg.control or cfg.control.is_relative_to(cfg.data):raise RuntimeError('Hub 控制目录必须独立于业务数据')
        self.cfg=cfg;self.lock=threading.RLock();self._ready=False;self._recovering=False;self._needs_recovery=False;cfg.data.mkdir(parents=True,exist_ok=True);cfg.control.mkdir(parents=True,exist_ok=True)
        self.db=cfg.data/'hub.sqlite3';self.log=cfg.control/'authority.jsonl';self.anchor=cfg.control/'head.json';self.key_file=cfg.control/'key'
        if not self.key_file.exists():
            if self.db.exists() or self.log.exists():raise RuntimeError('Hub 控制密钥缺失，拒绝重新初始化')
            atomic_private(self.key_file,os.urandom(32))
        self.key=self.key_file.read_bytes()
        if len(self.key)!=32:raise RuntimeError('Hub 控制密钥长度无效')
        with self.tx() as c:c.executescript(SCHEMA)
        if not self.log.exists():
            if self.anchor.exists() or self.query('SELECT id FROM users LIMIT 1'):raise RuntimeError('Hub 权威记录缺失')
            atomic_private(self.log,b'');self.write_anchor(0,'')
        self.recover();self._ready=True
        self.prune()

    def prune(self):
        with self.lock:
            live={r['hash'] for r in self.query('SELECT hash FROM files')}
            folder=self.cfg.control/'objects'
            if folder.exists():
                for path in folder.iterdir():
                    if path.is_file() and path.name not in live and len(path.name)==64:path.unlink()

    @contextmanager
    def tx(self):
        with self.lock:
            if self._ready and self._needs_recovery and not self._recovering:self.recover()
            c=sqlite3.connect(self.db,timeout=20);c.row_factory=sqlite3.Row;c.create_function('stockai_write_capability',0,lambda:3);c.execute('PRAGMA foreign_keys=ON');c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
            try:yield c;c.commit();self._needs_recovery=False
            except BaseException:c.rollback();raise
            finally:c.close()

    def query(self,sql,args=()):
        with self.tx() as c:return [dict(r) for r in c.execute(sql,args)]

    def sign(self,value):return hmac.new(self.key,canonical(value),hashlib.sha256).hexdigest()
    def write_anchor(self,seq,chain):
        value={'seq':seq,'chain':chain};atomic_private(self.anchor,canonical({**value,'signature':self.sign(value)}))

    def recover(self):
        self._recovering=True
        try:self._recover()
        finally:self._recovering=False

    def _recover(self):
        if not self.anchor.exists():raise RuntimeError('Hub 最新控制锚点缺失')
        anchor=json.loads(self.anchor.read_bytes());signature=anchor.pop('signature')
        if not hmac.compare_digest(signature,self.sign(anchor)):raise RuntimeError('Hub 控制锚点无效')
        seq=0;previous='';matched=anchor['seq']==0
        with self.tx() as c,self.log.open('rb') as source:
            for line in source:
                item=json.loads(line);signature=item.pop('signature');seq+=1
                if item['seq']!=seq or item['previous']!=previous or not hmac.compare_digest(signature,self.sign(item)):raise RuntimeError('Hub 控制链损坏')
                if seq==anchor['seq'] and signature==anchor['chain']:matched=True
                if not c.execute('SELECT seq FROM applied WHERE seq=?',(seq,)).fetchone():self.apply(c,item['event']);c.execute('INSERT INTO applied VALUES(?)',(seq,))
                self.change(c,item['event'],seq)
                previous=signature
        if seq<anchor['seq'] or not matched:raise RuntimeError('Hub 控制链已回退')
        if seq>anchor['seq']:self.write_anchor(seq,previous)
        self.seq,self.chain=seq,previous

    def commit(self,c,event):
        self._needs_recovery=True
        value={'seq':self.seq+1,'previous':self.chain,'event':event};signature=self.sign(value)
        with self.log.open('ab') as output:output.write(canonical({**value,'signature':signature})+b'\n');output.flush();os.fsync(output.fileno())
        self.write_anchor(value['seq'],signature);self.seq,self.chain=value['seq'],signature
        self.apply(c,event);c.execute('INSERT INTO applied VALUES(?)',(self.seq,))
        self.change(c,event,self.seq)

    def change(self,c,event,seq):
        if event['type']=='file':
            r=event['row'];c.execute('INSERT OR IGNORE INTO changes VALUES(?,?,?,?,?)',(seq,r['space_id'],r['path'],r['hash'],r['size']))

    def apply(self,c,event):
        kind=event['type'];r=event['row']
        if kind=='user':c.execute('INSERT INTO users VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET active=excluded.active,password_hash=excluded.password_hash',(r['id'],r['email'],r['password_hash'],r['active']))
        elif kind=='space':c.execute('INSERT OR IGNORE INTO spaces VALUES(?,?,?,?)',(r['id'],r['user_id'],r['created'],0))
        elif kind=='device':c.execute('INSERT INTO devices VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET active=excluded.active,generation=excluded.generation',(r['id'],r['user_id'],r['space_id'],r['name'],r['hash'],r['active'],r['generation']))
        elif kind=='file':c.execute('INSERT INTO files VALUES(?,?,?,?,?) ON CONFLICT(space_id,path) DO UPDATE SET hash=excluded.hash,size=excluded.size,sequence=excluded.sequence',(r['space_id'],r['path'],r['hash'],r['size'],r['sequence']))
        elif kind=='delete':c.execute('UPDATE spaces SET deleted=1 WHERE id=?',(r['id'],));c.execute('UPDATE devices SET active=0 WHERE space_id=?',(r['id'],));c.execute('DELETE FROM files WHERE space_id=?',(r['id'],))
        elif kind=='grant_used':c.execute('INSERT OR IGNORE INTO consumed_grants VALUES(?)',(r['id'],));c.execute('UPDATE grants SET used=1 WHERE id=?',(r['id'],))
        elif kind=='bind':self.apply(c,{'type':'device','row':r['device']});self.apply(c,{'type':'grant_used','row':{'id':r['grant']}})
        elif kind=='cursor':c.execute('INSERT INTO device_cursors VALUES(?,?) ON CONFLICT(device_id) DO UPDATE SET cursor=max(cursor,excluded.cursor)',(r['device_id'],r['cursor']))
        elif kind=='disable_user':
            c.execute('UPDATE users SET active=0 WHERE id=?',(r['id'],));c.execute('DELETE FROM sessions WHERE user_id=?',(r['id'],));c.execute('UPDATE devices SET active=0,generation=generation+1 WHERE user_id=?',(r['id'],))
        else:raise RuntimeError('不支持的 Hub 控制事件')

    def create_user(self,email,password):
        row={'id':uuid.uuid4().hex,'email':email_value(email),'password_hash':hash_password(password),'active':1}
        with self.lock,self.tx() as c:self.commit(c,{'type':'user','row':row})
        return row['id']

    def disable_user(self,email):
        with self.tx() as c:
            row=c.execute('SELECT id,active FROM users WHERE email=?',(email_value(email),)).fetchone()
            if not row:raise HubError('MISSING',404)
            if row['active']:self.commit(c,{'type':'disable_user','row':{'id':row['id']}})
        return {'disabled':True,'sessions_revoked':True,'devices_revoked':True}

    def throttle(self,c,key,max_count=20):
        import time
        now=time.time();row=c.execute('SELECT * FROM limits WHERE key=?',(hashed(key),)).fetchone()
        if row and now-row['start']<300 and row['count']>=max_count:raise HubError('RATE_LIMIT',429)
        c.execute('INSERT INTO limits VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET count=CASE WHEN excluded.start-start<300 THEN count+1 ELSE 1 END,start=CASE WHEN excluded.start-start<300 THEN start ELSE excluded.start END',(hashed(key),now))

    def actor(self,c,token,space=None,device=False):
        value=hashed(token)
        row=c.execute('SELECT d.*,u.active user_active,s.deleted FROM devices d JOIN users u ON u.id=d.user_id JOIN spaces s ON s.id=d.space_id WHERE d.hash=?',(value,)).fetchone()
        if row:
            if not device:raise HubError('ACCOUNT_SESSION_REQUIRED',401)
            if not row['active'] or not row['user_active'] or row['deleted'] or (space and row['space_id']!=space):raise HubError('REVOKED',403)
            return dict(row)
        if device:raise HubError('AUTH',401)
        row=c.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.hash=? AND s.expires>? AND u.active=1',(value,utc().isoformat())).fetchone()
        if not row:raise HubError('AUTH',401)
        return {**dict(row),'user_id':row['id']}


def create_app(cfg=None):
    cfg=cfg or Config.environment()
    @asynccontextmanager
    async def life(app):
        with ProcessLock(cfg.control/'service.lock'):
            app.state.hub=Store(cfg);yield
    app=FastAPI(title='StockAI Sync Hub',version=VERSION,lifespan=life,docs_url=None,redoc_url=None,openapi_url=None)
    def token(request):
        value=request.headers.get('authorization','')
        if not value.startswith('Bearer '):raise HubError('AUTH',401)
        return value[7:]
    @app.exception_handler(HubError)
    async def error(request,exc):return JSONResponse({'code':exc.code,'detail':'Hub 请求未完成：'+exc.code},exc.status)
    @app.exception_handler(ValueError)
    async def invalid(request,exc):return JSONResponse({'code':'INVALID_INPUT','detail':'输入未通过校验'},400)
    @app.middleware('http')
    async def guard(request,call_next):
        maintenance=cfg.control/'upgrades/maintenance'
        if maintenance.exists() and maintenance.read_bytes()==b'1' and request.url.path!='/v1/health':return JSONResponse({'code':'MAINTENANCE'},503)
        if int(request.headers.get('content-length','0'))>MAX_OBJECT*2:return JSONResponse({'code':'STORAGE_FULL'},413)
        if not cfg.dev and request.url.scheme!='https' and os.environ.get('STOCKAI_HUB_BEHIND_TLS_PROXY')!='1':return JSONResponse({'code':'HTTPS_REQUIRED'},403)
        response=await call_next(request);response.headers['Cache-Control']='no-store';response.headers['X-Content-Type-Options']='nosniff';return response
    @app.get('/v1/health')
    def health():return {'status':'ok','component':'hub','version':VERSION,'api':1,'protocol':1}
    @app.post('/v1/auth/login')
    def login(value:dict,request:Request):
        store=request.app.state.hub
        with store.tx() as c:store.throttle(c,'login:'+request.client.host)
        with store.lock,store.tx() as c:
            row=c.execute('SELECT * FROM users WHERE email=?',(email_value(value.get('email','')),)).fetchone()
            if not row or not row['active'] or not password_matches(row['password_hash'],str(value.get('password',''))):raise HubError('AUTH',401)
            secret=secrets.token_urlsafe(48);c.execute('INSERT INTO sessions VALUES(?,?,?)',(hashed(secret),row['id'],(utc()+timedelta(hours=8)).isoformat()))
        return {'token':secret,'expires_seconds':28800}
    @app.post('/v1/spaces')
    def space(value:dict,request:Request):
        store=request.app.state.hub
        with store.lock,store.tx() as c:
            actor=store.actor(c,token(request));ident=uuid.uuid4().hex;device=uuid.uuid4().hex;secret=secrets.token_urlsafe(48)
            store.commit(c,{'type':'space','row':{'id':ident,'user_id':actor['user_id'],'created':utc().isoformat()}})
            store.commit(c,{'type':'device','row':{'id':device,'user_id':actor['user_id'],'space_id':ident,'name':str(value.get('name','新设备'))[:80],'hash':hashed(secret),'active':1,'generation':1}})
        return {'space_id':ident,'device_id':device,'token':secret}
    @app.get('/v1/spaces/{space}/status')
    def status(space:str,request:Request):
        store=request.app.state.hub
        with store.tx() as c:
            actor=store.actor(c,token(request),space,True);size=c.execute('SELECT coalesce(sum(size),0) FROM files WHERE space_id=?',(space,)).fetchone()[0]
            devices=[dict(r) for r in c.execute('SELECT id,name,active,generation FROM devices WHERE space_id=?',(space,))]
        return {'protocol':1,'conditional_write':True,'available_bytes':max(0,cfg.max_space_bytes-size),'devices':devices}
    @app.get('/v1/spaces/{space}/files/{path:path}')
    def read_file(space:str,path:str,request:Request):
        safe_path(path);store=request.app.state.hub
        with store.tx() as c:
            store.actor(c,token(request),space,True);row=c.execute('SELECT * FROM files WHERE space_id=? AND path=?',(space,path)).fetchone()
            if not row:raise HubError('MISSING',404)
            file=cfg.control/'objects'/row['hash']
            if not file.is_file():raise HubError('CORRUPT',503)
            data=file.read_bytes()
            if digest(data)!=row['hash']:raise HubError('CORRUPT',503)
        return Response(data,headers={'ETag':'"'+row['hash']+'"'},media_type='application/octet-stream')
    @app.get('/v1/spaces/{space}/changes')
    def changes(space:str,request:Request,cursor:int=0,limit:int=50):
        if cursor<0 or not 1<=limit<=200:raise HubError('INVALID_CURSOR')
        store=request.app.state.hub
        with store.tx() as c:
            actor=store.actor(c,token(request),space,True);snapshot=store.seq
            if cursor>snapshot:raise HubError('ROLLBACK',409)
            rows=[dict(r) for r in c.execute('SELECT seq,path,hash,size FROM changes WHERE space_id=? AND seq>? AND seq<=? ORDER BY seq LIMIT ?',(space,cursor,snapshot,limit+1))]
            more=len(rows)>limit;rows=rows[:limit];next_cursor=rows[-1]['seq'] if rows else cursor
            acknowledged=c.execute('SELECT cursor FROM device_cursors WHERE device_id=?',(actor['id'],)).fetchone()
        return {'protocol':1,'cursor':cursor,'next_cursor':next_cursor,'has_more':more,'items':rows,'acknowledged':acknowledged[0] if acknowledged else 0}
    @app.post('/v1/spaces/{space}/changes/ack')
    def acknowledge(space:str,value:dict,request:Request):
        cursor=value.get('cursor');store=request.app.state.hub
        if type(cursor) is not int or cursor<0:raise HubError('INVALID_CURSOR')
        with store.tx() as c:
            actor=store.actor(c,token(request),space,True)
            if cursor>store.seq:raise HubError('ROLLBACK',409)
            before=c.execute('SELECT cursor FROM device_cursors WHERE device_id=?',(actor['id'],)).fetchone()
            if not before or cursor>before[0]:store.commit(c,{'type':'cursor','row':{'device_id':actor['id'],'cursor':cursor}})
        return {'acknowledged':cursor}
    @app.put('/v1/spaces/{space}/files/{path:path}')
    async def put_file(space:str,path:str,request:Request):
        safe_path(path);store=request.app.state.hub;body=bytearray()
        with store.tx() as c:store.actor(c,token(request),space,True)
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body)>MAX_OBJECT*2:raise HubError('STORAGE_FULL',413)
        body=bytes(body);content_hash=digest(body)
        with store.lock,store.tx() as c:
            store.actor(c,token(request),space,True);old=c.execute('SELECT * FROM files WHERE space_id=? AND path=?',(space,path)).fetchone()
            if request.headers.get('if-none-match')=='*':
                if old:raise HubError('CONFLICT',412)
            elif not old or request.headers.get('if-match')!='"'+old['hash']+'"':raise HubError('CONFLICT',412)
            if path!='head.bin' and old:raise HubError('IMMUTABLE',409)
            size=c.execute('SELECT coalesce(sum(size),0) FROM files WHERE space_id=?',(space,)).fetchone()[0]
            if size-(old['size'] if old else 0)+len(body)>cfg.max_space_bytes:raise HubError('STORAGE_FULL',507)
            target=cfg.control/'objects'/content_hash
            if not target.exists():atomic_private(target,body)
            store.commit(c,{'type':'file','row':{'space_id':space,'path':path,'hash':content_hash,'size':len(body),'sequence':old['sequence']+1 if old else 1}})
        return Response(status_code=200 if old else 201,headers={'ETag':'"'+content_hash+'"'})
    @app.post('/v1/spaces/{space}/grants')
    def grant(space:str,request:Request):
        store=request.app.state.hub
        with store.lock,store.tx() as c:
            actor=store.actor(c,token(request),space,True);ident=uuid.uuid4().hex;code=secrets.token_urlsafe(24);nonce=secrets.token_urlsafe(32);expires=(utc()+timedelta(minutes=10)).isoformat()
            c.execute('INSERT INTO grants VALUES(?,?,?,?,?,NULL,?,0)',(ident,space,actor['user_id'],hashed(code),hashed(nonce),expires))
        return {'id':ident,'code':code,'nonce':nonce,'expires':expires}
    @app.post('/v1/spaces/{space}/grants/{ident}')
    def finalize_grant(space:str,ident:str,value:dict,request:Request):
        store=request.app.state.hub
        if not isinstance(value.get('challenge'),str) or len(value['challenge'])>8192:raise HubError('INVALID_INPUT')
        with store.lock,store.tx() as c:
            actor=store.actor(c,token(request),space,True);changed=c.execute('UPDATE grants SET challenge=? WHERE id=? AND space_id=? AND user_id=? AND used=0 AND expires>?',(value['challenge'],ident,space,actor['user_id'],utc().isoformat())).rowcount
            if not changed:raise HubError('GRANT_EXPIRED',409)
        return {'ready':True}
    @app.post('/v1/bind/challenge')
    def challenge(value:dict,request:Request):
        store=request.app.state.hub
        with store.tx() as c:
            actor=store.actor(c,token(request));row=c.execute('SELECT * FROM grants WHERE code_hash=? AND user_id=? AND used=0 AND expires>? AND id NOT IN (SELECT id FROM consumed_grants)',(hashed(str(value.get('code',''))),actor['user_id'],utc().isoformat())).fetchone()
            if not row or not row['challenge']:raise HubError('GRANT_EXPIRED',409)
        return {'id':row['id'],'space_id':row['space_id'],'challenge':row['challenge']}
    @app.post('/v1/bind')
    def bind(value:dict,request:Request):
        store=request.app.state.hub
        with store.tx() as c:store.throttle(c,'bind:'+request.client.host)
        with store.lock,store.tx() as c:
            actor=store.actor(c,token(request));row=c.execute('SELECT * FROM grants WHERE code_hash=? AND user_id=? AND used=0 AND expires>? AND id NOT IN (SELECT id FROM consumed_grants)',(hashed(str(value.get('code',''))),actor['user_id'],utc().isoformat())).fetchone()
            if not row or not hmac.compare_digest(row['nonce_hash'],hashed(str(value.get('proof','')))):raise HubError('KEY_PROOF_REQUIRED',403)
            secret=secrets.token_urlsafe(48);device=uuid.uuid4().hex
            store.commit(c,{'type':'bind','row':{'device':{'id':device,'user_id':actor['user_id'],'space_id':row['space_id'],'name':str(value.get('name','新设备'))[:80],'hash':hashed(secret),'active':1,'generation':1},'grant':row['id']}})
        return {'space_id':row['space_id'],'device_id':device,'token':secret}
    @app.post('/v1/spaces/{space}/devices/{device}/revoke')
    def revoke(space:str,device:str,request:Request):
        store=request.app.state.hub
        with store.lock,store.tx() as c:
            actor=store.actor(c,token(request),space,True);row=c.execute('SELECT * FROM devices WHERE id=? AND space_id=? AND user_id=?',(device,space,actor['user_id'])).fetchone()
            if not row:raise HubError('MISSING',404)
            value=dict(row);value.update(active=0,generation=value['generation']+1);store.commit(c,{'type':'device','row':value})
        return {'revoked':True}
    @app.delete('/v1/spaces/{space}')
    def delete_space(space:str,request:Request):
        store=request.app.state.hub
        with store.lock,store.tx() as c:
            already=c.execute('SELECT s.deleted FROM devices d JOIN spaces s ON s.id=d.space_id WHERE d.hash=? AND d.space_id=?',(hashed(token(request)),space)).fetchone()
            if not already or not already['deleted']:
                store.actor(c,token(request),space,True);store.commit(c,{'type':'delete','row':{'id':space}})
        store.prune()
        return {'access_revoked':True,'cleanup':'business_ciphertexts_removed','deleted':True,'control_retained':True}
    return app


app=create_app()
