"""Owner-authorized controls; remote access never implies local automation consent."""
import base64
import copy
from datetime import datetime,timedelta,timezone
import json
import os
import uuid
from zoneinfo import ZoneInfo
from backend.cloud.crypto import atomic_private
from backend.cloud.store import json_text,stamp
from .protocol import SyncError,seal,open_sealed,digest,keyring,key_text


class Controls:
    def remember_control(self,row,head):
        uid=row['user_id']
        self.journal.put('sync',uid,'control',{'limits':head['limits'],'proposal':head.get('limits_proposal'),'executor':head['executor'],'devices':head['devices'],'receipts':head['receipts'],'usage':head['usage']})
        secret=self.secrets(row)
        self.journal.put('sync',uid,'latest-head:'+row['space_id'],base64.b64encode(seal(keyring(secret),row['space_id'],head,'head',head['key_version'])).decode())
        for name,value in head['accounts'].items():self.journal.put('sync',uid,'account:'+name.split(':',1)[1],value)

    def checkpoint(self,uid,reason):
        row=self.binding(uid);ident=uuid.uuid4().hex
        entities=self.store.query('SELECT kind,object_id,heads_json FROM sync_entities WHERE user_id=?',(uid,))
        value={'format':'stockai-personal-recovery/1','space':row['space_id'],'created_at':stamp(),'reason':str(reason)[:80],'entities':entities}
        secret=self.secrets(row);cipher=seal(keyring(secret),row['space_id'],value,'recovery',secret.get('generation',1))
        path=self.store.cfg.erasures/'sync-recovery'/digest(uid.encode())/(ident+'.bin');atomic_private(path,cipher)
        record={'id':ident,'sha256':digest(cipher),'created_at':value['created_at'],'reason':value['reason'],'objects':len(entities),'space_id':row['space_id']}
        self.journal.put('sync',uid,'checkpoint:'+ident,record);return record

    def checkpoints(self,uid):return list(self.journal.all('sync',uid,'checkpoint:').values())

    def point_preview(self,uid,ident):
        row=self.binding(uid);record=self.journal.get('sync',uid,'checkpoint:'+str(ident))
        if not record or record['space_id']!=row['space_id']:raise SyncError('MISSING')
        cipher=(self.store.cfg.erasures/'sync-recovery'/digest(uid.encode())/(record['id']+'.bin')).read_bytes()
        if digest(cipher)!=record['sha256']:raise SyncError('CORRUPT')
        point=open_sealed(keyring(self.secrets(row)),row['space_id'],cipher,'recovery');head,_=self.heads(row).read()
        current=self.store.query('SELECT kind,object_id,heads_json FROM sync_entities WHERE user_id=?',(uid,));allowed=[];skipped=[]
        for entity in point['entities']:
            name=entity['kind']+':'+entity['object_id']
            if name in head['tombstones'] or entity['kind'] in ('account','report','backtest'):
                skipped.append({'kind':entity['kind'],'object':entity['object_id'],'reason':'保留最新删除、账本和不可变归档'});continue
            heads=json.loads(entity['heads_json'])
            if len(heads)==1:allowed.append(entity)
        value={'point':record['id'],'head':digest(head),'local':digest(current),'allowed':allowed,'skipped':skipped};value['id']=digest(value);return value

    def restore_point(self,uid,ident,confirmation):
        preview=self.point_preview(uid,ident)
        if confirmation!=preview['id']:raise SyncError('CONFLICT','恢复预览已变化，请重新核对')
        row=self.binding(uid);head,_=self.heads(row).read();remote=self.remote(row);self.checkpoint(uid,'恢复操作前');count=0
        for entity in preview['allowed']:
            old=self.load_op(row,json.loads(entity['heads_json'])[0],remote)
            if old['deleted']:continue
            desc=head['objects'].get(entity['kind']+':'+entity['object_id'],{})
            self.emit_version(uid,entity['kind'],entity['object_id'],old['payload'],desc.get('heads',[]));count+=1
        return {'restored_as_new_versions':count,'skipped':preview['skipped']}

    def restore_deleted(self,uid,kind,gid,deleted_hash):
        with self.store.user_lock(uid):
            row=self.binding(uid);remote=self.remote(row);head,_=self.heads(row,remote).read();desc=head['objects'].get(kind+':'+gid)
            if not desc or not desc['deleted'] or desc['heads']!=[deleted_hash]:raise SyncError('CONFLICT')
            deleted=self.load_op(row,deleted_hash,remote);pending=list(deleted['parents']);selected=None
            while pending:
                candidate=self.load_op(row,pending.pop(),remote)
                if not candidate['deleted']:selected=candidate['payload'];break
                pending.extend(candidate['parents'])
            if selected is None or kind in ('account','report','backtest'):raise SyncError('CONFLICT','请保留归档或账本为独立分支，不能覆盖恢复')
            return self.emit_version(uid,kind,gid,selected,[deleted_hash],restores=deleted_hash)

    def fork(self,uid,ident,choice):
        from .business import Adapter,COMPOSITE
        row=self.binding(uid);conflict=self.store.one("SELECT * FROM sync_conflicts WHERE id=? AND user_id=? AND state='open'",(ident,uid))
        if not conflict or conflict['kind'] not in ('account','report','backtest'):raise SyncError('MISSING')
        values=json.loads(conflict['remote_json']);payload=json.loads(conflict['local_json']) if choice=='local' else values[-1][1]['payload'] if choice=='remote' else None
        if not payload:raise SyncError('CONFLICT')
        mapping={}
        for name,rows in payload['tables'].items():
            if name not in COMPOSITE and name!='instruments':
                for value in rows:
                    if 'id' in value:mapping[value['id']]=uuid.uuid4().hex
        def remap(value):
            if isinstance(value,str):return mapping.get(value,value)
            if isinstance(value,list):return [remap(v) for v in value]
            if isinstance(value,dict):return {k:remap(v) for k,v in value.items()}
            return value
        payload=remap(copy.deepcopy(payload));gid=mapping.get(conflict['object_id'],uuid.uuid4().hex)
        if conflict['kind']=='account':
            payload['writer']={'device':row['device_id'],'generation':1};payload['tables']['sim_accounts'][0]['name']=('分支副本 · '+payload['tables']['sim_accounts'][0]['name'])[:80]
        result=self.emit_version(uid,conflict['kind'],gid,payload,[])
        self.journal.put('sync',uid,'fork:'+gid,{'source':conflict['object_id'],'heads':json.loads(conflict['heads_json']),'choice':choice})
        self.store.execute("UPDATE sync_conflicts SET state='preserved_branch',resolution_json=? WHERE id=? AND user_id=?",(json_text(result),ident,uid));return result

    def pause(self,uid,paused=True,automatic=None):
        row=self.binding(uid)
        if not row:raise SyncError('MISSING')
        if automatic is not None:self.store.execute('UPDATE sync_bindings SET automatic=? WHERE user_id=?',(int(automatic is True),uid))
        self.store.execute('UPDATE sync_bindings SET status=? WHERE user_id=?',('paused' if paused else 'configured',uid))
        self.persist_binding(self.binding(uid))
        self.set_status(uid,'paused' if paused else 'configured');return self.details(uid)

    def unbind(self,uid,confirmation):
        row=self.binding(uid)
        if confirmation!='解除本机绑定并保留本地资料':raise SyncError('CONFLICT','请明确确认解除绑定')
        if row['status']!='paused':raise SyncError('CONFLICT','请先暂停同步，再解除绑定')
        self.capture(uid);point=self.checkpoint(uid,'解除绑定前')
        # 保留待发密文及旧空间恢复材料，不能把解除绑定等同于远端删除。
        self.retire_binding(row);self.journal.put('sync',uid,'unbound:'+row['space_id'],{'space':row['space_id'],'checkpoint':point,'remote_cleanup':'not_requested'})
        self.journal.put('sync',uid,'binding-current',None)
        with self.store.tx(immediate=True) as c:
            for table in ('sync_entities','sync_rows','sync_operations','sync_conflicts','sync_pending','sync_dirty'):c.execute('DELETE FROM '+table+' WHERE user_id=?',(uid,))
            c.execute('DELETE FROM sync_bindings WHERE user_id=?',(uid,))
        return {'status':'unbound','local_data_retained':True,'checkpoint':point}

    def devices(self,uid):
        row=self.binding(uid);heads=self.heads(row);head,_=heads.read();self.remember_control(row,head)
        accounts=copy.deepcopy(head['accounts'])
        for name,lease in accounts.items():
            mapping=self.store.one("SELECT local_id FROM sync_rows WHERE user_id=? AND table_name='sim_accounts' AND global_id=?",(uid,name.split(':',1)[1]))
            if mapping:
                local=self.store.one('SELECT id,name FROM sim_accounts WHERE user_id=? AND id=?',(uid,mapping['local_id']))
                if local:lease.update(local_id=local['id'],name=local['name'])
        at=(getattr(heads.remote,'server_time',None) or datetime.now(timezone.utc)).astimezone(ZoneInfo('Asia/Shanghai'))
        usage={window:sum(1 for v in head['usage'].values() if v['state']!='released' and v[column]==value) for window,column,value in (('daily','day',at.date().isoformat()),('monthly','month',at.strftime('%Y-%m')))}
        usage['unconfirmed_local']=sum(1 for slot in self.journal.all('sync',uid,'unconfirmed-usage:') if slot.split(':',1)[1] not in head['usage'])
        return {'current':row['device_id'],'devices':head['devices'],'executor':head['executor'],'accounts':accounts,'limits':head['limits'],'limits_proposal':head.get('limits_proposal'),'usage':usage,'receipts':head['receipts']}

    def request_delete(self,uid,confirmation):
        if confirmation!='删除我的远端同步资料':raise SyncError('CONFLICT')
        row=self.binding(uid)
        if row:self.capture(uid);self.checkpoint(uid,'远端删除前');self.retire_binding(row)
        if row:
            self.store.execute("UPDATE sync_bindings SET status='paused',automatic=0 WHERE user_id=?",(uid,));self.persist_binding(self.binding(uid))
        for slot in self.journal.all('sync',uid,'retired:'):
            space=slot.split(':',1)[1];name='intent:'+space
            if not self.journal.get('sync-cleanup',uid,name):self.journal.put('sync-cleanup',uid,name,{'space':space,'status':'pending','created_at':stamp()})
        return self.cleanup(uid,force=True)

    def cleanup(self,uid,force=False):
        records=self.journal.all('sync-cleanup',uid,'intent:');results=[]
        for slot,intent in records.items():
            if intent['status']!='completed':
                retry=intent.get('next_retry');manual=intent.get('error') in ('AUTH','KEY','INCOMPATIBLE','REVOKED')
                if force or (not manual and (not retry or datetime.now(timezone.utc)>=datetime.fromisoformat(retry))):
                    intent=self.cleanup_space(uid,intent);self.journal.put('sync-cleanup',uid,slot,intent)
            results.append(intent)
        self.forget_private(uid)
        if not results:return {'status':'unconfigured'}
        if all(v['status']=='completed' for v in results):self.set_status(uid,'revoked',remote_cleanup='completed')
        return {'status':'pending' if any(v['status']!='completed' for v in results) else 'completed','spaces':results}

    def cleanup_space(self,uid,intent):
        try:
            row=self.read_private(uid,'binding-'+intent['space'])
            if not row:raise SyncError('KEY')
            remote=self.remote(row)
            if row['backend']=='hub':
                result=remote.api('/v1/spaces/'+row['space_id'],method='DELETE')
                intent.update(status='completed',result=result,finished_at=stamp());return intent
            def change(head):
                head['deleted']=True
                for device in head['devices'].values():device.update(revoked=True,generation=device['generation']+1)
            try:_,closed=self.heads(row,remote).mutate(change)
            except SyncError as exc:
                if exc.code!='REVOKED':raise
                raw,_=remote.read('head.bin');existing=open_sealed(keyring(self.secrets(row)),row['space_id'],raw,'head')
                if not existing.get('deleted'):raise SyncError('REVOKED')
                closed=existing
            result=remote.erase_objects([h for v in closed['objects'].values() for h in v['heads']])
            intent.update(status='completed',result=result,finished_at=stamp())
            self.store.execute("UPDATE sync_bindings SET status='paused',automatic=0 WHERE user_id=?",(uid,));self.set_status(uid,'revoked',remote_cleanup='completed')
        except SyncError as exc:
            retries=int(intent.get('retries',0))+1
            intent.update(status='pending',error=exc.code,retries=retries,next_retry=(datetime.now(timezone.utc)+timedelta(seconds=min(3600,5*2**min(retries,10)))).isoformat())
        return intent

    def grant(self,uid):
        row=self.binding(uid)
        if row['backend']!='hub':raise SyncError('INCOMPATIBLE','WebDAV 加入设备需使用本人的恢复材料')
        secret=self.secrets(row);return self.remote(row).grant(keyring(secret),secret.get('generation',1))

    def revoke(self,uid,device):
        row=self.binding(uid)
        if device==row['device_id']:raise SyncError('CONFLICT','请在另一台已授权设备上撤销本机')
        def change(head):
            entry=head['devices'].get(device)
            if not entry:raise SyncError('MISSING')
            entry.update(revoked=True,generation=entry['generation']+1)
            if head['executor']['device']==device:head['executor']={'device':None,'generation':head['executor']['generation']+1}
            for lease in head['accounts'].values():
                if lease['device']==device:lease['frozen']=True
        _,head=self.heads(row).mutate(change);self.remember_control(row,head)
        if row['backend']=='hub':self.remote(row).api('/v1/spaces/'+row['space_id']+'/devices/'+device+'/revoke',{})
        return {'revoked':True,'rotation_required':True}

    def rotate(self,uid,confirmation):
        if confirmation!='轮换密钥并重新授权其他设备':raise SyncError('CONFLICT')
        with self.store.user_lock(uid):
            row=self.binding(uid);heads=self.heads(row);head,_=heads.read();secret=self.secrets(row);generation=head['key_version']+1
            secret['keys']={str(k):key_text(v) for k,v in keyring(secret).items()};secret['keys'][str(generation)]=key_text(os.urandom(32));secret['key']=secret['keys'][str(generation)];secret['generation']=generation
            envelope=self.keys.seal(json_text(secret),site=self.store.site_id,user=uid,resource=row['space_id'],purpose='sync-credentials')
            self.write_private(uid,'keys-'+row['space_id'],secret)
            heads.key=keyring(secret)
            def change(current):
                if current['key_version']!=generation-1:raise SyncError('CONFLICT')
                current['key_version']=generation
            heads.mutate(change);self.store.execute('UPDATE sync_bindings SET secret_json=? WHERE user_id=?',(envelope,uid))
            return {'rotated':True,'key_version':generation,'other_devices':'requires_new_recovery_material'}

    def request_limits(self,uid,caps):
        if set(caps)!={'daily','monthly'} or any(v is not None and (type(v) is not int or v<0 or v>1000000) for v in caps.values()):raise ValueError('调用上限无效')
        row=self.binding(uid)
        def change(head):
            proposal={'id':uuid.uuid4().hex,'limits':caps,'acks':[row['device_id']]}
            head['limits_proposal']=proposal;self.ack_limits(row,head)
        _,head=self.heads(row).mutate(change);self.remember_control(row,head);return {'limits':head['limits'],'proposal':head.get('limits_proposal')}

    @staticmethod
    def ack_limits(row,head):
        proposal=head.get('limits_proposal')
        if not proposal:return
        if row['device_id'] not in proposal['acks']:proposal['acks'].append(row['device_id'])
        active={k for k,v in head['devices'].items() if not v['revoked']}
        if active<=set(proposal['acks']):head['limits']=proposal['limits'];head['limits_proposal']=None

    def executor(self,uid,device):
        row=self.binding(uid)
        def change(head):
            if not head['devices'].get(device) or head['devices'][device]['revoked']:raise SyncError('REVOKED')
            if any(r['state'] in ('reserved','sending','uncertain') for r in head['receipts'].values()):raise SyncError('LEASE','存在在途或结果不确定的执行记录，请先核对')
            head['executor']={'device':device,'generation':head['executor']['generation']+1}
        _,head=self.heads(row).mutate(change);self.remember_control(row,head);return head['executor']

    def acknowledge_receipt(self,uid,ident,confirmation):
        if confirmation!='保留不确定记录且不再自动重发':raise SyncError('CONFLICT')
        row=self.binding(uid)
        def change(head):
            receipt=head['receipts'].get(ident)
            if not receipt or receipt['state'] not in ('reserved','sending','uncertain'):raise SyncError('MISSING')
            receipt.update(original_state=receipt['state'],state='acknowledged_unknown',acknowledged_by=row['device_id'],acknowledged_at=stamp())
        _,head=self.heads(row).mutate(change);self.remember_control(row,head);return {'acknowledged':True,'automatic_replay':False}

    def account_transfer(self,uid,local_id,device):
        row=self.binding(uid);self.capture(uid)
        mapping=self.store.one("SELECT global_id FROM sync_rows WHERE user_id=? AND table_name='sim_accounts' AND local_id=?",(uid,local_id))
        if not mapping:raise SyncError('DEPENDENCY')
        gid=mapping['global_id'];name='account:'+gid
        if self.store.one("SELECT hash FROM sync_operations WHERE user_id=? AND object_id=? AND state='ready'",(uid,gid)):raise SyncError('LEASE','请先同步完整账本与待发记录')
        def change(head):
            lease=head['accounts'].get(name)
            if not lease or lease['frozen'] or lease['device']!=row['device_id']:raise SyncError('LEASE')
            if not head['devices'].get(device) or head['devices'][device]['revoked']:raise SyncError('REVOKED')
            lease.update(device=device,generation=lease['generation']+1)
        _,head=self.heads(row).mutate(change);self.remember_control(row,head);return head['accounts'][name]

    def account_guard(self,uid,local_id):
        row=self.binding(uid)
        if not row:return
        mapping=self.store.one("SELECT global_id FROM sync_rows WHERE user_id=? AND table_name='sim_accounts' AND local_id=?",(uid,local_id))
        if not mapping:return
        lease=self.journal.get('sync',uid,'account:'+mapping['global_id'])
        try:
            head,_=self.heads(row).read();self.remember_control(row,head);lease=head['accounts'].get('account:'+mapping['global_id'],lease)
        except SyncError as exc:
            if exc.code!='OFFLINE':raise
        if lease and (lease.get('frozen') or lease['device']!=row['device_id']):raise SyncError('LEASE','该模拟账本由另一端持有写入权或存在分叉，当前只读')

    def automatic(self,uid,kind,key,*,complete=None,delivery=None,retry=None):
        row=self.binding(uid)
        if not row:return True
        if row['status']=='paused':raise SyncError('LEASE','同步已暂停，不能验证自动执行归属')
        heads=self.heads(row);ident=digest([kind,key])
        if complete is None:return heads.receipt(kind,key,delivery=delivery,retry=retry)
        def change(head):
            receipt=head['receipts'].get(ident)
            if receipt and receipt['device']==row['device_id']:
                if kind=='mail' and delivery and (receipt.get('delivery')!=delivery or receipt['generation']!=head['executor']['generation']):return
                receipt['state']=complete
        heads.mutate(change);return True

    def ensure_executor(self,uid):
        row=self.binding(uid)
        if not row:return
        if row['status']=='paused':raise SyncError('LEASE')
        head,_=self.heads(row).read();self.remember_control(row,head)
        if head['executor']['device']!=row['device_id']:raise SyncError('LEASE')

    def shared_reserve(self,uid,ident,caps,purpose):
        row=self.binding(uid)
        if not row:
            cached=self.journal.get('sync',uid,'control',{})
            if any(v is not None for v in cached.get('limits',{}).values()):raise SyncError('LEASE','已有共享有限额度，解除绑定不能绕过；请重新连接核验')
            return False
        cached=self.journal.get('sync',uid,'control',{});remote=self.remote(row);heads=self.heads(row,remote)
        try:head,_=heads.read();self.remember_control(row,head)
        except SyncError as exc:
            finite=any(v is not None for v in caps.values()) or any(v is not None for v in cached.get('limits',{}).values())
            if exc.code!='OFFLINE' or finite:raise
            self.journal.put('sync',uid,'unconfirmed-usage:'+ident,{'purpose':purpose,'state':'sent','at':stamp()});return False
        at=getattr(remote,'server_time',None)
        if at is None:raise SyncError('INCOMPATIBLE','同步服务缺少可验证的服务器时间，调用已暂停')
        at=at.astimezone(ZoneInfo('Asia/Shanghai'))
        heads.reserve_usage(ident,at.date().isoformat(),at.strftime('%Y-%m'),caps,purpose)
        return True

    def shared_result(self,uid,ident,state,tokens=None):
        row=self.binding(uid)
        if not row:return
        record={'state':state,'tokens':tokens};self.journal.put('sync',uid,'usage-result:'+ident,record)
        try:self.heads(row).usage_transition(ident,state,tokens)
        except SyncError:pass  # 已发送记录继续占额；离线不释放或自动重发。

    def publish_usage(self,uid,head):
        # 先补记离线无限额分支的已发事实，再确认新上限；不返还不确定调用。
        at=datetime.now(timezone.utc).astimezone(ZoneInfo('Asia/Shanghai'))
        for slot,record in self.journal.all('sync',uid,'unconfirmed-usage:').items():
            ident=slot.split(':',1)[1]
            head['usage'].setdefault(ident,{'device':self.binding(uid)['device_id'],'day':at.date().isoformat(),'month':at.strftime('%Y-%m'),'state':'sent','tokens':None,'purpose':record['purpose'],'unconfirmed_time':record['at']})
        for slot,record in self.journal.all('sync',uid,'usage-result:').items():
            ident=slot.split(':',1)[1]
            if ident in head['usage'] and head['usage'][ident]['device']==self.binding(uid)['device_id']:
                head['usage'][ident].update(record)
