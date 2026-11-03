"""Durable encrypted operation DAG, explicit first merge, and independent progress anchors."""
from __future__ import annotations
import base64
import copy
from datetime import datetime,timedelta,timezone
import json
import os
import sqlite3
import uuid
import time
from backend.cloud.auth import AuthError
from backend.cloud.store import json_text,stamp
from backend.coordination import Journal
from .business import Adapter,KINDS,ALLOWED
from .heads import Heads
from .protocol import SyncError,digest,seal,open_sealed,parse_key,key_text,keyring,validate_operation,timestamp,ID
from .transports import transport
from .controls import Controls
from .migration import Migration
from .local_state import LocalState
from .lineage import Lineage


def three_way(base,local,remote):
    if local==remote:return local
    if local==base:return remote
    if remote==base:return local
    if all(isinstance(v,dict) for v in (base,local,remote)):
        missing=object();result={}
        for key in base.keys()|local.keys()|remote.keys():
            a,b,c=base.get(key,missing),local.get(key,missing),remote.get(key,missing)
            if b==c:value=b
            elif b==a:value=c
            elif c==a:value=b
            elif all(isinstance(v,dict) for v in (a,b,c)):value=three_way(a,b,c)
            else:raise SyncError('CONFLICT')
            if value is not missing:result[key]=value
        return result
    raise SyncError('CONFLICT')


class Sync(Controls,Migration,LocalState,Lineage):
    def __init__(self,store,keys,factory=None):
        self.store,self.keys=store,keys;self.journal=Journal(store,keys);self.custom_factory=factory is not None;self.factory=factory or (lambda config,secrets:transport(store,config,secrets))

    def binding(self,uid):
        if not self.store.active(uid):raise AuthError('ACCOUNT_DISABLED','账号不可用',403)
        return self.store.one('SELECT * FROM sync_bindings WHERE user_id=?',(uid,))

    def details(self,uid):
        row=self.binding(uid)
        if not row:return {'status':'unconfigured','automatic':False,'pending_upload':0,'conflicts':0}
        config=json.loads(row['payload_json']);state=self.journal.get('sync',uid,'status',{})
        state['missing_dependencies']=self.store.one('SELECT count(*) n FROM sync_pending WHERE user_id=?',(uid,))['n']
        migration=self.journal.get('sync',uid,'migration',{})
        if migration.get('status') in ('staged','source_closed'):
            state['migration']={'id':migration['id'],'status':migration['status'],'target':migration['candidate']['config']['url']}
        return {**config,**state,'space_id':row['space_id'],'device_id':row['device_id'],'backend':row['backend'],'version':row['version'],'automatic':bool(row['automatic']),'status':state.get('status',row['status']),'credential_present':True,'pending_upload':self.store.one("SELECT count(*) n FROM sync_operations WHERE user_id=? AND state='ready'",(uid,))['n'],'conflicts':self.store.one("SELECT count(*) n FROM sync_conflicts WHERE user_id=? AND state='open'",(uid,))['n']}

    def secrets(self,row):
        try:
            private=self.read_private(row['user_id'],'keys-'+row['space_id'])
            if private is not None:return private
            envelope=self.journal.get('sync',row['user_id'],'keys:'+row['space_id'],row['secret_json'])
            return json.loads(self.keys.open(envelope,site=self.store.site_id,user=row['user_id'],resource=row['space_id'],purpose='sync-credentials'))
        except Exception:raise SyncError('KEY') from None

    def remote(self,row):return self.factory({**json.loads(row['payload_json']),'backend':row['backend'],'space_id':row['space_id']},self.secrets(row))

    def heads(self,row,remote=None):
        uid=row['user_id'];secret=self.secrets(row)
        return Heads(remote or self.remote(row),keyring(secret),row['space_id'],row['device_id'],lambda:self.journal.get('sync',uid,'anchor:'+row['space_id']),lambda value:self.journal.put('sync',uid,'anchor:'+row['space_id'],value))

    def configure(self,uid,value,*,create=False):
        with self.store.user_lock(uid):
            old=self.binding(uid)
            if old:raise SyncError('CONFLICT','请先暂停并通过迁移预览切换同步后端，不能覆盖现有绑定')
            backend=value.get('backend')
            if backend not in ('hub','webdav'):raise SyncError('INCOMPATIBLE')
            material=value.get('recovery')
            if not create and not isinstance(material,dict):raise SyncError('KEY','加入已有空间需要本人恢复材料和当前确认锚点')
            space=uuid.uuid4().hex if create else material.get('space_id','')
            if not ID.fullmatch(space):raise SyncError('CORRUPT')
            device=uuid.uuid4().hex;key=os.urandom(32) if create else parse_key(material.get('key',''))
            config={'url':str(value.get('url','')).rstrip('/'),'allow_private':value.get('allow_private') is True,'device_name':str(value.get('device_name','本机设备'))[:80],'space_id':space,'backend':backend,'first_merge':True}
            config['creating']=bool(create)
            # 可信 CA 仅由运维配置，不接受普通用户指定服务器本地文件路径。
            if value.get('ca_file'):raise SyncError('INCOMPATIBLE','自定义证书需由实际运行端的运维配置')
            secret={'key':key_text(key),'keys':material.get('keys',{'1':key_text(key)}) if material else {'1':key_text(key)},'generation':material.get('key_version',1) if material else 1,'username':value.get('username',''),'password':value.get('password',''),'token':value.get('token','')}
            if backend=='hub' and not self.custom_factory:
                from .transports import provision
                provisioned=provision(self.store,config,value,material,create);space=provisioned['space_id'];device=provisioned['device_id'];secret['token']=provisioned['token'];secret['password']='';config['space_id']=space
            remote=self.factory(config,secret);capabilities=remote.probe()
            sealed=self.keys.seal(json_text(secret),site=self.store.site_id,user=uid,resource=space,purpose='sync-credentials')
            self.store.execute('INSERT INTO sync_bindings VALUES(?,?,?,?,1,?,?,?,0,?)',(uid,space,device,backend,json_text(config),sealed,'configured',stamp()))
            row=self.binding(uid)
            self.persist_binding(row)
            try:
                if create:self.heads(row,remote).create(config['device_name'])
                else:
                    # 根密钥证明解密能力；Hub 还必须先完成一次性设备绑定授权。
                    anchor=material.get('anchor')
                    if not anchor:raise SyncError('KEY','恢复材料缺少可信同步锚点，请从已有设备重新导出')
                    self.journal.put('sync',uid,'anchor:'+space,anchor)
                    self.join_device(row,remote)
                    joined,_=self.heads(row,remote).read();self.receive_lineage(row,remote,joined,material)
                self.restore_outbox(row);self.mark_all(uid);self.set_status(uid,'configured',capabilities=capabilities)
                from .schema import protect
                with self.store.tx(immediate=True) as c:protect(c)
            except SyncError as exc:
                if exc.code in ('OFFLINE','CONFLICT'):
                    self.store.execute("UPDATE sync_bindings SET status='binding_pending' WHERE user_id=?",(uid,));self.set_status(uid,'binding_pending',error=exc.code)
                    self.persist_binding(self.binding(uid))
                    return self.details(uid)
                self.store.execute('DELETE FROM sync_bindings WHERE user_id=?',(uid,));self.journal.put('sync',uid,'binding-current',None);raise
            except BaseException:
                self.store.execute('DELETE FROM sync_bindings WHERE user_id=?',(uid,));self.journal.put('sync',uid,'binding-current',None);raise
            return self.details(uid)

    def join_device(self,row,remote):
        secret=self.secrets(row);raw,tag=remote.read('head.bin');head=open_sealed(keyring(secret),row['space_id'],raw,'head')
        anchor=self.journal.get('sync',row['user_id'],'anchor:'+row['space_id'])
        from .heads import validate
        validate(head,row['space_id'])
        if head['sequence']<anchor['sequence']:raise SyncError('ROLLBACK')
        current=head
        while current['sequence']>anchor['sequence']:
            raw,_=remote.read('heads/'+current['previous']+'.bin');parent=open_sealed(keyring(secret),row['space_id'],raw,'head');validate(current,row['space_id'],parent);current=parent
        if digest(current)!=anchor['hash']:raise SyncError('ROLLBACK')
        if head['deleted']:raise SyncError('REVOKED')
        if row['device_id'] in head['devices']:
            if head['devices'][row['device_id']]['revoked']:raise SyncError('REVOKED')
            self.journal.put('sync',row['user_id'],'anchor:'+row['space_id'],{'sequence':head['sequence'],'hash':digest(head)});return
        new=copy.deepcopy(head);new['devices'][row['device_id']]={'name':json.loads(row['payload_json'])['device_name'],'generation':1,'revoked':False,'ack':0};new['sequence']+=1;new['previous']=digest(head)
        validate(new,row['space_id'],head);cipher=seal(keyring(secret),row['space_id'],new,'head',new['key_version'])
        remote.put('heads/'+digest(new)+'.bin',cipher,create=True);remote.put('head.bin',cipher,etag=tag)
        self.journal.put('sync',row['user_id'],'anchor:'+row['space_id'],{'sequence':new['sequence'],'hash':digest(new)})

    def recovery(self,uid):
        row=self.binding(uid)
        if not row:raise SyncError('MISSING')
        try:head,_=self.heads(row).read()
        except SyncError as exc:
            if exc.code not in ('AUTH','OFFLINE'):raise
            anchor=self.journal.get('sync',uid,'anchor:'+row['space_id'])
            if not anchor:raise
            secret=self.secrets(row)
            cached=self.journal.get('sync',uid,'latest-head:'+row['space_id'])
            previous=open_sealed(keyring(secret),row['space_id'],base64.b64decode(cached),'head') if cached else {}
            return {'format':'stockai-sync-recovery/1','space_id':row['space_id'],'key':secret['key'],'keys':secret.get('keys',{'1':secret['key']}),'key_version':secret.get('generation',1),'anchor':anchor,'status':'last_confirmed_anchor','lineage_keys':self.lineage_material(uid,previous)}
        secret=self.secrets(row)
        return {'format':'stockai-sync-recovery/1','space_id':row['space_id'],'key':secret['key'],'keys':secret.get('keys',{'1':secret['key']}),'key_version':head['key_version'],'anchor':{'sequence':head['sequence'],'hash':digest(head)},'lineage_keys':self.lineage_material(uid,head)}

    def mark_all(self,uid):
        from .schema import ROOTS
        with self.store.tx(immediate=True) as c:
            for table,(kind,column) in ROOTS.items():
                if table in ('model_profile_versions','alert_delivery_events'):continue
                c.execute(f"INSERT OR IGNORE INTO sync_dirty(user_id,kind,local_id) SELECT user_id,'{kind}',CAST({column} AS TEXT) FROM {table} WHERE user_id=?",(uid,))

    def set_status(self,uid,status,**values):
        old=self.journal.get('sync',uid,'status',{});old.update(status=status,**values);self.journal.put('sync',uid,'status',old)

    def capture(self,uid,limit=1000):
        row=self.binding(uid)
        if not row:return 0
        adapter=Adapter(self.store,uid,row['space_id'],row['device_id']);secret=self.secrets(row);key=keyring(secret);captured=0
        with self.store.user_lock(uid):
            dirty=self.store.query('SELECT * FROM sync_dirty WHERE user_id=? LIMIT ?',(uid,limit))
            for change in dirty:
                adapter.created_maps=[]
                with self.store.tx(immediate=True) as c:
                    kind,local=change['kind'],change['local_id'];table=KINDS.get(kind) or ('user_watchlist' if kind=='watch' else 'user_settings')
                    gid=adapter.gid(table,local,c);payload=adapter.export(kind,local,c)
                    entity=c.execute('SELECT * FROM sync_entities WHERE user_id=? AND kind=? AND object_id=?',(uid,kind,gid)).fetchone()
                    fingerprint=digest(payload)
                    if entity and entity['projection_hash']==fingerprint:
                        c.execute('DELETE FROM sync_dirty WHERE user_id=? AND kind=? AND local_id=?',(uid,kind,local));continue
                    if payload is None and not entity:
                        c.execute('DELETE FROM sync_dirty WHERE user_id=? AND kind=? AND local_id=?',(uid,kind,local));continue
                    if entity and entity['deleted'] and payload is not None:continue
                    parents=json.loads(entity['heads_json']) if entity else []
                    if not entity:
                        history=c.execute('SELECT hash,payload_json FROM sync_operations WHERE user_id=? AND kind=? AND object_id=?',(uid,kind,gid)).fetchall()
                        consumed={h for version in history for h in json.loads(version['payload_json'])['parents']}
                        parents=[version['hash'] for version in history if version['hash'] not in consumed]
                    operation={'id':uuid.uuid4().hex,'space':row['space_id'],'device':row['device_id'],'kind':kind,'object':gid,'parents':parents,'deleted':payload is None,'immutable':kind in ('report','backtest'),'payload':payload,'schema':1,'created_at':timestamp(),'restores':None}
                    if kind=='account' and payload is not None:
                        operation['payload']={**payload,'writer':self.journal.get('sync',uid,'account:'+gid,{'device':row['device_id'],'generation':1})}
                    cipher=seal(key,row['space_id'],operation,generation=secret.get('generation',1));op_hash=digest(cipher)
                    # 独立签名账本先保留密文；业务回滚后可以恢复唯一待发副本。
                    self.journal.put('sync',uid,'operation:'+row['space_id']+':'+op_hash,self.operation_record(row,adapter,cipher,'ready'))
                    if payload is None:self.remember_deletion(row,kind,gid,local,op_hash)
                    self.save_op(c,uid,op_hash,operation,cipher,'ready')
                    c.execute('INSERT INTO sync_entities VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,kind,object_id) DO UPDATE SET heads_json=excluded.heads_json,payload_json=excluded.payload_json,projection_hash=excluded.projection_hash,deleted=excluded.deleted',(uid,kind,gid,local,json_text([op_hash]),json_text(payload),fingerprint,int(payload is None)))
                    c.execute('DELETE FROM sync_dirty WHERE user_id=? AND kind=? AND local_id=?',(uid,kind,local));captured+=1
        return captured

    @staticmethod
    def save_op(c,uid,op_hash,operation,cipher,state):
        old=c.execute('SELECT * FROM sync_operations WHERE user_id=? AND op_id=?',(uid,operation['id'])).fetchone()
        if old and (old['hash']!=op_hash or json.loads(old['payload_json'])!=operation):raise SyncError('CORRUPT','同一操作标识对应不同内容，已隔离')
        c.execute('INSERT OR IGNORE INTO sync_operations VALUES(?,?,?,?,?,?,?,?,?)',(uid,op_hash,operation['id'],operation['object'],operation['kind'],json_text(operation),cipher,state,operation['created_at']))

    def remember_deletion(self,row,kind,gid,local,op_hash):
        if kind!='object':return
        uid=row['user_id']
        with self.journal.control.tx() as c:
            deleted=c.execute('SELECT kind FROM deletions WHERE user_id=? AND id=?',(uid,str(local))).fetchone()
        if not deleted:return
        intent=self.journal.control.deletion(uid,deleted['kind'],str(local))
        if intent:self.journal.put('sync',uid,'deletion:'+row['space_id']+':'+op_hash,{'object':gid,'local_id':str(local),'kind':deleted['kind'],'signature':intent['signature']})

    def copy_deletion(self,uid,old_space,old_hash,new_space,new_hash):
        intent=self.journal.get('sync',uid,'deletion:'+old_space+':'+old_hash)
        if intent:self.journal.put('sync',uid,'deletion:'+new_space+':'+new_hash,intent)

    def restore_outbox(self,row):
        uid=row['user_id'];key=keyring(self.secrets(row))
        for slot,value in self.journal.all('sync',uid,'operation:'+row['space_id']+':').items():
            self.restore_maps(row,value)
            op_hash=slot.rsplit(':',1)[1]
            if self.store.one('SELECT hash FROM sync_operations WHERE user_id=? AND hash=?',(uid,op_hash)):continue
            cipher=base64.b64decode(value['cipher']);operation=validate_operation(open_sealed(key,row['space_id'],cipher),row['space_id'])
            if digest(cipher)!=op_hash:raise SyncError('CORRUPT')
            with self.store.tx(immediate=True) as c:self.save_op(c,uid,op_hash,operation,cipher,value['state'])

    def recover_local(self):
        self.recover_bindings()
        # 业务备份不会覆盖此账本。先恢复唯一待发分支，再施加最新墓碑。
        for row in self.store.query('SELECT * FROM sync_bindings'):
            uid=row['user_id'];self.restore_outbox(row);pending=self.store.query("SELECT hash,kind,object_id,payload_json FROM sync_operations WHERE user_id=? AND state='ready'",(uid,))
            groups={}
            for op in pending:groups.setdefault(op['kind']+':'+op['object_id'],[]).append(op)
            objects={}
            for name,ops in groups.items():
                parents=set(h for op in ops for h in json.loads(op['payload_json'])['parents']);heads=[op for op in ops if op['hash'] not in parents]
                if not heads:raise SyncError('CORRUPT')
                value=json.loads(heads[-1]['payload_json']);objects[name]={'heads':[op['hash'] for op in heads],'deleted':value['deleted'],'immutable':value['immutable']}
            remote=self.remote(row)
            if objects:self.pull(row,{'objects':objects},remote,100000)
            cipher=self.journal.get('sync',uid,'latest-head:'+row['space_id'])
            if cipher:
                head=open_sealed(keyring(self.secrets(row)),row['space_id'],base64.b64decode(cipher),'head');self.remember_control(row,head);self.pull(row,head,remote,100000)
                device=head['devices'].get(row['device_id'])
                if head['deleted'] or not device or device['revoked']:self.store.execute("UPDATE sync_bindings SET status='paused' WHERE user_id=?",(uid,));self.set_status(uid,'revoked')

    def preview(self,uid):
        row=self.binding(uid)
        if not row:raise SyncError('MISSING')
        if row['status']=='binding_pending':
            config=json.loads(row['payload_json']);remote=self.remote(row)
            if config.get('creating'):
                try:self.heads(row,remote).read()
                except SyncError as exc:
                    if exc.code!='MISSING':raise
                    self.heads(row,remote).create(config['device_name'])
            else:self.join_device(row,remote)
            self.store.execute("UPDATE sync_bindings SET status='configured' WHERE user_id=?",(uid,));self.mark_all(uid);self.set_status(uid,'configured',error=None)
            self.persist_binding(self.binding(uid))
            from .schema import protect
            with self.store.tx(immediate=True) as c:protect(c)
        self.capture(uid,100000);self.restore_outbox(row);head,_=self.heads(row).read()
        pending=self.store.query("SELECT hash,kind,object_id FROM sync_operations WHERE user_id=? AND state='ready'",(uid,))
        value={'remote_hash':digest(head),'local_hash':digest(pending),'local_pending':len(pending),'remote_objects':len(head['objects']),'remote_tombstones':len(head['tombstones']),'mode':'merge','space_id':row['space_id']}
        value['id']=digest(value);self.journal.put('sync',uid,'preview',value);return value

    def synchronize(self,uid,preview_id=None,limit=50):
        started=time.monotonic()
        with self.store.user_lock(uid):
            row=self.binding(uid)
            if not row:raise SyncError('MISSING')
            if row['status']=='paused':raise SyncError('MAINTENANCE','同步已由本人暂停')
            self.capture(uid,1000);self.restore_outbox(row);config=json.loads(row['payload_json'])
            if config.get('first_merge'):
                preview=self.preview(uid)
                if preview_id!=preview['id']:raise SyncError('CONFLICT','请先检查最新的首次合并预览，再明确确认')
                self.checkpoint(uid,'首次合并')
            try:
                remote=self.remote(row);heads=self.heads(row,remote);head,_=heads.read()
                page=None
                if hasattr(remote,'changes'):
                    page=remote.changes(self.journal.get('sync',uid,'hub-cursor:'+row['space_id'],0),max(1,min(limit,200)))
                    self.journal.put('sync',uid,'hub-page:'+row['space_id'],page)
                self.remember_control(row,head)
                # 优先接收最新删除与控制，再处理本地待发记录。
                self.pull(row,head,remote,limit)
                pending=self.store.query("SELECT * FROM sync_operations WHERE user_id=? AND state='ready' ORDER BY created_at LIMIT ?",(uid,limit))
                for op in pending:
                    path='objects/'+op['hash']+'.bin'
                    try:remote.put(path,op['cipher'],create=True)
                    except SyncError as exc:
                        if exc.code!='CONFLICT':raise
                        existing,_=remote.read(path)
                        if existing!=op['cipher']:raise SyncError('CORRUPT')
                def publish(current):
                    self.publish_usage(uid,current)
                    self.ack_limits(row,current)
                    for op in pending:
                        value=json.loads(op['payload_json']);name=value['kind']+':'+value['object'];before=current['objects'].get(name)
                        if before and before.get('deleted') and not value['deleted'] and value['restores'] not in before['heads']:continue
                        tombstone=current['tombstones'].get(name)
                        if tombstone and not value['deleted'] and value['restores']!=tombstone['hash'] and tombstone['hash'] not in self.ancestors(uid,value['parents']):continue
                        old_heads=before['heads'] if before else []
                        if before and before.get('immutable') and not value['deleted']:
                            for previous in old_heads:
                                prior=self.load_op(row,previous,remote)
                                if not prior['deleted'] and prior['payload']!=value['payload']:raise SyncError('CORRUPT','不可变归档同标识内容不同，已隔离')
                        if value['kind']=='account' and not value['deleted'] and before:
                            lease=current['accounts'].get(name);writer=value['payload'].get('writer',{})
                            if lease and (lease['device']!=writer.get('device') or lease['generation']!=writer.get('generation')):
                                lease['frozen']=True;continue
                        if before and before.get('immutable') and old_heads and any(h not in value['parents'] and h!=op['hash'] for h in old_heads):raise SyncError('CORRUPT','不可变归档存在同标识不同内容')
                        keep=[h for h in old_heads if h not in value['parents'] and h!=op['hash']]
                        if value['deleted']:
                            current['tombstones'][name]={'hash':op['hash'],'at':value['created_at']};keep=[]
                        current['objects'][name]={'heads':sorted(keep+[op['hash']]),'immutable':value['immutable'],'deleted':value['deleted']}
                        if value['kind']=='account' and name not in current['accounts']:current['accounts'][name]={'device':value['device'],'generation':1,'frozen':False}
                    current['devices'][row['device_id']]['ack']=current['sequence']
                _,head=heads.mutate(publish)
                self.remember_control(row,head)
                for op in pending:
                    self.store.execute("UPDATE sync_operations SET state='confirmed' WHERE user_id=? AND hash=?",(uid,op['hash']))
                    saved=self.journal.get('sync',uid,'operation:'+row['space_id']+':'+op['hash']);saved['state']='confirmed';self.journal.put('sync',uid,'operation:'+row['space_id']+':'+op['hash'],saved)
                remote_pending=self.pull(row,head,remote,limit)
                config['first_merge']=False;self.store.execute('UPDATE sync_bindings SET payload_json=? WHERE user_id=?',(json_text(config),uid))
                self.persist_binding(self.binding(uid))
                remaining=self.store.one("SELECT count(*) n FROM sync_operations WHERE user_id=? AND state='ready'",(uid,))['n'];conflicts=self.store.one("SELECT count(*) n FROM sync_conflicts WHERE user_id=? AND state='open'",(uid,))['n']
                stats=getattr(getattr(remote,'http',None),'stats',{})
                dependencies=self.store.one('SELECT count(*) n FROM sync_pending WHERE user_id=?',(uid,))['n']
                if page and not remote_pending and not dependencies:
                    remote.acknowledge(page['next_cursor']);self.journal.put('sync',uid,'hub-cursor:'+row['space_id'],page['next_cursor'])
                self.set_status(uid,'conflicts' if conflicts else 'syncing' if remaining or remote_pending or page and page['has_more'] else 'waiting_dependencies' if dependencies else 'synced',last_success=stamp(),error=None,retries=0,next_retry=None,remote_pending=bool(remote_pending),metrics={**stats,'elapsed_seconds':round(time.monotonic()-started,3),'uploaded_operations':len(pending),'page_items':len(page['items']) if page else 0})
                return self.details(uid)
            except SyncError as exc:
                previous=self.journal.get('sync',uid,'status',{});retry=int(previous.get('retries',0))+1
                next_retry=(datetime.now(timezone.utc)+timedelta(seconds=min(3600,5*2**min(retry,10)))).isoformat() if exc.code in ('OFFLINE','RATE_LIMIT','CONFLICT') else None
                self.set_status(uid,{'OFFLINE':'offline','AUTH':'auth_failed','REVOKED':'revoked','STORAGE_FULL':'storage_full','ROLLBACK':'paused','INCOMPATIBLE':'incompatible'}.get(exc.code,'error'),error=exc.code,retries=retry,next_retry=next_retry);raise

    def load_op(self,row,op_hash,remote,depth=0):
        if depth>10000:raise SyncError('CORRUPT')
        cached=self.store.one('SELECT payload_json FROM sync_operations WHERE user_id=? AND hash=?',(row['user_id'],op_hash))
        if cached:return json.loads(cached['payload_json'])
        cipher,_=remote.read('objects/'+op_hash+'.bin')
        if digest(cipher)!=op_hash:raise SyncError('CORRUPT')
        operation=validate_operation(open_sealed(keyring(self.secrets(row)),row['space_id'],cipher),row['space_id'])
        for parent in operation['parents']:self.load_op(row,parent,remote,depth+1)
        self.journal.put('sync',row['user_id'],'operation:'+row['space_id']+':'+op_hash,{'cipher':base64.b64encode(cipher).decode(),'state':'received'})
        with self.store.tx(immediate=True) as c:self.save_op(c,row['user_id'],op_hash,operation,cipher,'received')
        return operation

    def ancestors(self,uid,heads):
        seen=set();pending=list(heads)
        while pending:
            value=pending.pop()
            if value in seen:continue
            if len(seen)>100000:raise SyncError('CORRUPT')
            seen.add(value);row=self.store.one('SELECT payload_json FROM sync_operations WHERE user_id=? AND hash=?',(uid,value))
            if row:pending.extend(json.loads(row['payload_json'])['parents'])
        return seen

    def pull(self,row,head,remote,limit):
        uid=row['user_id'];adapter=Adapter(self.store,uid,row['space_id'],row['device_id']);processed=0;more=False
        with self.journal.control.tx() as c:
            local_deletions={v['id']:v['kind'] for v in c.execute('SELECT id,kind FROM deletions WHERE user_id=?',(uid,))}
        # 删除先于普通对象；单次有界，后续从已保存实体和操作记录继续。
        ordered=sorted(head['objects'].items(),key=lambda pair:(not pair[1]['deleted'],{'watch':0,'preferences':1,'profile':2,'report':3,'account':4,'backtest':5,'object':6,'experiment':7}.get(pair[0].split(':')[0],9)))
        for name,description in ordered:
            adapter.created_maps=[]
            kind,gid=name.split(':',1);entity=self.store.one('SELECT * FROM sync_entities WHERE user_id=? AND kind=? AND object_id=?',(uid,kind,gid))
            mapping=self.store.one("SELECT local_id FROM sync_rows WHERE user_id=? AND table_name='research_objects' AND global_id=?",(uid,gid)) if kind=='object' and local_deletions else None
            locally_deleted=kind=='object' and (entity['local_id'] if entity else mapping['local_id'] if mapping else None) in local_deletions
            if entity and sorted(json.loads(entity['heads_json']))==sorted(description['heads']) and (not locally_deleted or description['deleted']):continue
            if processed>=limit:more=True;break
            values=[(h,self.load_op(row,h,remote)) for h in description['heads']]
            if any(v['object']!=gid or v['kind']!=kind for _,v in values):raise SyncError('CORRUPT')
            restore_intent=None
            if locally_deleted and not description['deleted']:
                local_heads=json.loads(entity['heads_json']) if entity else []
                if entity and entity['deleted'] and set(description['heads'])<=self.ancestors(uid,local_heads):continue
                restores=values[0][1]['restores']
                bound=self.journal.get('sync',uid,'deletion:'+row['space_id']+':'+restores) if restores else None
                deletion=self.journal.control.deletion(uid,local_deletions[entity['local_id']],entity['local_id']) if entity and entity['local_id'] in local_deletions else None
                if len(values)==1 and restores and entity and entity['deleted'] and bound and deletion and bound=={'object':gid,'local_id':entity['local_id'],'kind':local_deletions[entity['local_id']],'signature':deletion['signature']} and head.get('tombstones',{}).get(name,{}).get('hash')==restores and set(local_heads)<=self.ancestors(uid,[restores]) and restores in self.ancestors(uid,description['heads']):
                    restore_intent=(bound['kind'],entity['local_id'],restores,deletion['signature'])
                else:
                    # A restorable business snapshot cannot override a newer signed local delete.
                    self.emit_version(uid,kind,gid,None,sorted(set(local_heads+description['heads'])))
                    processed+=1;continue
            same=all(v['payload']==values[0][1]['payload'] and v['deleted']==values[0][1]['deleted'] for _,v in values)
            if len(values)>1 and not same:
                self.conflict(uid,kind,gid,entity,values);processed+=1;continue
            op_hash,operation=values[0]
            if kind in ('report','backtest') and entity and not entity['deleted'] and not operation['deleted'] and json.loads(entity['payload_json'])!=operation['payload']:
                self.conflict(uid,kind,gid,entity,values);processed+=1;continue
            # 本地尚未上传的改动不能被线性远端版本覆盖。
            local_heads=json.loads(entity['heads_json']) if entity else []
            if entity and set(description['heads'])<=self.ancestors(uid,local_heads):
                # 远端仍是本机待发版本的祖先，不把正常顺序编辑误判为并发。
                continue
            if entity and local_heads and not set(local_heads)<=self.ancestors(uid,description['heads']) and not operation['deleted'] and json.loads(entity['payload_json'])!=operation['payload']:
                self.conflict(uid,kind,gid,entity,values);processed+=1;continue
            try:
                with self.store.tx(immediate=True) as c:
                    c.execute('PRAGMA defer_foreign_keys=ON')
                    if restore_intent:
                        if not self.journal.control.restore(uid,*restore_intent):raise SyncError('CONFLICT','本机删除意图已变化，请重新核对')
                        local_deletions.pop(restore_intent[1],None)
                    local=entity['local_id'] if entity else uid if kind=='preferences' else operation['payload']['tables']['user_watchlist'][0]['instrument_id'] if kind=='watch' and not operation['deleted'] else adapter.local_id(KINDS.get(kind,'research_objects'),gid,c)
                    if operation['deleted']:adapter.remove(kind,local,c);projection=None
                    else:
                        local=adapter.project(kind,gid,operation['payload'],c);projection=adapter.export(kind,local,c)
                    c.execute('INSERT INTO sync_entities VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,kind,object_id) DO UPDATE SET local_id=excluded.local_id,heads_json=excluded.heads_json,payload_json=excluded.payload_json,projection_hash=excluded.projection_hash,deleted=excluded.deleted',(uid,kind,gid,str(local),json_text(description['heads']),json_text(operation['payload']),digest(projection),int(operation['deleted'])))
                    c.execute('DELETE FROM sync_dirty WHERE user_id=? AND kind=? AND local_id=?',(uid,kind,str(local)))
                    c.execute('DELETE FROM sync_pending WHERE user_id=? AND hash=?',(uid,op_hash))
                    if adapter.created_maps:
                        record=self.journal.get('sync',uid,'operation:'+row['space_id']+':'+op_hash)
                        if record:self.journal.put('sync',uid,'operation:'+row['space_id']+':'+op_hash,{**record,**self.operation_record(row,adapter,base64.b64decode(record['cipher']),record['state'])})
                    adapter.created_maps=[]
            except (sqlite3.IntegrityError,SyncError) as exc:
                if isinstance(exc,SyncError) and exc.code not in ('DEPENDENCY',):raise
                self.store.execute('INSERT OR REPLACE INTO sync_pending VALUES(?,?,?,?)',(uid,op_hash,json_text(operation),'DEPENDENCY'))
            processed+=1
        return more

    def conflict(self,uid,kind,gid,entity,values):
        heads=sorted(set((json.loads(entity['heads_json']) if entity else [])+[h for h,_ in values]));ident=digest([uid,kind,gid,heads])
        if self.store.one('SELECT id FROM sync_conflicts WHERE id=? AND user_id=?',(ident,uid)):return
        groups=[self.ancestors(uid,[h]) for h in heads];common=set.intersection(*groups) if groups else set();baseline=None
        if common:
            # 选所有共同祖先中不再被另一共同祖先继承的节点，不用设备时钟排序。
            older=set().union(*(self.ancestors(uid,json.loads(self.store.one('SELECT payload_json FROM sync_operations WHERE user_id=? AND hash=?',(uid,h))['payload_json'])['parents']) for h in common))
            nearest=sorted(common-older)
            if len(nearest)==1:baseline=json.loads(self.store.one('SELECT payload_json FROM sync_operations WHERE user_id=? AND hash=?',(uid,nearest[0]))['payload_json'])['payload']
        self.store.execute('INSERT INTO sync_conflicts VALUES(?,?,?,?,?,?,?,?,?,?,NULL)',(ident,uid,kind,gid,json_text(baseline) if baseline is not None else None,entity['payload_json'] if entity else None,json_text(values),json_text(heads),'open',stamp()))

    def resolve(self,uid,ident,choice,payload=None):
        row=self.binding(uid);conflict=self.store.one("SELECT * FROM sync_conflicts WHERE id=? AND user_id=? AND state='open'",(ident,uid))
        if not conflict:raise SyncError('MISSING')
        if conflict['kind'] in ('account','report','backtest'):raise SyncError('CONFLICT','账本分支和不可变归档需要保留分支处理，不能直接覆盖')
        values=json.loads(conflict['remote_json']);local=json.loads(conflict['local_json']) if conflict['local_json'] else None
        selected=local if choice=='local' else values[-1][1]['payload'] if choice=='remote' else payload if choice=='manual' else None
        if selected is None:raise SyncError('CONFLICT','请明确选择保留内容')
        operation={'id':uuid.uuid4().hex,'space':row['space_id'],'device':row['device_id'],'kind':conflict['kind'],'object':conflict['object_id'],'parents':json.loads(conflict['heads_json']),'deleted':False,'immutable':False,'payload':selected,'schema':1,'created_at':timestamp(),'restores':None}
        secret=self.secrets(row);cipher=seal(keyring(secret),row['space_id'],operation,generation=secret.get('generation',1));op_hash=digest(cipher);adapter=Adapter(self.store,uid,row['space_id'],row['device_id'])
        with self.store.tx(immediate=True) as c:
            c.execute('PRAGMA defer_foreign_keys=ON');local_id=adapter.project(conflict['kind'],conflict['object_id'],selected,c);self.save_op(c,uid,op_hash,operation,cipher,'ready')
            self.journal.put('sync',uid,'operation:'+row['space_id']+':'+op_hash,self.operation_record(row,adapter,cipher,'ready'))
            c.execute('UPDATE sync_entities SET heads_json=?,payload_json=?,projection_hash=? WHERE user_id=? AND kind=? AND object_id=?',(json_text([op_hash]),json_text(selected),digest(adapter.export(conflict['kind'],local_id,c)),uid,conflict['kind'],conflict['object_id']))
            c.execute("UPDATE sync_conflicts SET state='resolved',resolution_json=? WHERE id=? AND user_id=?",(json_text({'choice':choice,'head':op_hash}),ident,uid))
        return {'resolved':True,'operation':op_hash}

    def emit_version(self,uid,kind,gid,payload,parents,restores=None):
        row=self.binding(uid);operation={'id':uuid.uuid4().hex,'space':row['space_id'],'device':row['device_id'],'kind':kind,'object':gid,'parents':parents,'deleted':payload is None,'immutable':kind in ('report','backtest'),'payload':payload,'schema':1,'created_at':timestamp(),'restores':restores}
        secret=self.secrets(row);cipher=seal(keyring(secret),row['space_id'],operation,generation=secret.get('generation',1));op_hash=digest(cipher);adapter=Adapter(self.store,uid,row['space_id'],row['device_id'])
        with self.store.user_lock(uid),self.store.tx(immediate=True) as c:
            c.execute('PRAGMA defer_foreign_keys=ON')
            if payload is None:
                local=adapter.local_id(KINDS[kind],gid,c);adapter.remove(kind,local,c);projection=None
                self.remember_deletion(row,kind,gid,local,op_hash)
            else:
                if kind=='object' and restores:
                    local=adapter.local_id('research_objects',gid,c)
                    # Save the complete authorized restore before lifting its deletion marker.
                    self.journal.put('sync',uid,'operation:'+row['space_id']+':'+op_hash,self.operation_record(row,adapter,cipher,'ready'))
                    subtype=payload['tables']['research_objects'][0]['kind'];deletion=self.journal.control.deletion(uid,subtype,local)
                    if deletion and not self.journal.control.restore(uid,subtype,local,restores,deletion['signature']):raise SyncError('CONFLICT','本机删除意图已变化，请重新核对')
                local=adapter.project(kind,gid,payload,c);projection=adapter.export(kind,local,c)
            self.save_op(c,uid,op_hash,operation,cipher,'ready')
            self.journal.put('sync',uid,'operation:'+row['space_id']+':'+op_hash,self.operation_record(row,adapter,cipher,'ready'))
            c.execute('INSERT INTO sync_entities VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,kind,object_id) DO UPDATE SET local_id=excluded.local_id,heads_json=excluded.heads_json,payload_json=excluded.payload_json,projection_hash=excluded.projection_hash,deleted=excluded.deleted',(uid,kind,gid,str(local),json_text([op_hash]),json_text(payload),digest(projection),int(payload is None)))
        return {'operation':op_hash,'local_id':local,'object_id':gid}
