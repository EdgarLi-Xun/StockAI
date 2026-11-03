"""Explicit backend handover with a recovery point and preserved control history."""
import base64
import copy
import json
import os
import uuid
from backend.cloud.store import json_text,stamp
from .protocol import SyncError,digest,seal,keyring,key_text


class Migration:
    def candidate_secret(self,uid,candidate):
        value=self.read_private(uid,'candidate-'+candidate['config']['space_id'])
        if value is None:raise SyncError('KEY')
        return value

    def candidate_envelope(self,uid,candidate):
        return self.keys.seal(json_text(self.candidate_secret(uid,candidate)),site=self.store.site_id,user=uid,resource=candidate['config']['space_id'],purpose='sync-credentials')

    def migration_preview(self,uid,value):
        old=self.binding(uid)
        if not old or old['status']!='paused':raise SyncError('CONFLICT','更换后端前请先暂停同步')
        self.capture(uid);head,_=self.heads(old).read()
        if any(not v['revoked'] and k!=old['device_id'] for k,v in head['devices'].items()):raise SyncError('LEASE','请先在所有其他设备完成同步并撤销旧绑定，再迁移到新后端')
        if self.store.one("SELECT id FROM sync_conflicts WHERE user_id=? AND state='open'",(uid,)):raise SyncError('CONFLICT','请先解决现有同步冲突')
        if self.store.one("SELECT hash FROM sync_operations WHERE user_id=? AND state='ready'",(uid,)):raise SyncError('CONFLICT','仍有待发送记录，请恢复旧后端并同步完成后再迁移')
        if any(r['state'] in ('reserved','sending','uncertain') for r in head['receipts'].values()) or any(r['state'] in ('reserved','sent','uncertain') for r in head['usage'].values()):raise SyncError('LEASE','仍有在途或结果不确定的执行记录，请先核对')
        backend=value.get('backend')
        if backend not in ('hub','webdav'):raise SyncError('INCOMPATIBLE')
        space=uuid.uuid4().hex;device=uuid.uuid4().hex;key=key_text(os.urandom(32))
        config={'url':str(value.get('url','')).rstrip('/'),'backend':backend,'space_id':space,'allow_private':value.get('allow_private') is True,'device_name':str(value.get('device_name','迁移设备'))[:80],'first_merge':False}
        secret={'key':key,'keys':{'1':key},'generation':1,'username':value.get('username',''),'password':value.get('password',''),'token':value.get('token','')}
        if backend=='hub' and not self.custom_factory:
            from .transports import provision
            result=provision(self.store,config,value,None,True);space=result['space_id'];device=result['device_id'];config['space_id']=space;secret['token']=result['token'];secret['password']=''
        remote=self.factory(config,secret);capabilities=remote.probe()
        try:remote.read('head.bin')
        except SyncError as exc:
            if exc.code!='MISSING':raise
        else:raise SyncError('CONFLICT','目标已有同步清单，不能覆盖')
        self.write_private(uid,'candidate-'+space,secret)
        candidate={'config':config,'secret_ref':space,'device':device,'source':old['space_id'],'head':digest(head)}
        candidate['id']=digest(candidate);self.journal.put('sync',uid,'migration-candidate',candidate)
        return {'id':candidate['id'],'source':old['backend'],'target':backend,'url':config['url'],'objects':len(head['objects']),'tombstones':len(head['tombstones']),'pending':0,'limits':head['limits'],'devices_require_rebinding':True,'capabilities':capabilities}

    def migrate(self,uid,ident):
        with self.store.user_lock(uid):
            candidate=self.journal.get('sync',uid,'migration-candidate');old=self.binding(uid)
            saved=self.journal.get('sync',uid,'migration',{})
            if saved.get('id')==ident and saved.get('status') in ('staged','source_closed'):
                candidate=saved['candidate'];config=candidate['config'];space=config['space_id'];secret=self.candidate_secret(uid,candidate);remote=self.factory(config,secret)
                if old['space_id']==space:
                    self.journal.put('sync',uid,'migration',{'id':ident,'status':'completed','from':candidate['source'],'to':space});return self.details(uid)
                if old['space_id']!=candidate['source']:raise SyncError('CONFLICT')
                source=self.remote(old);raw,_=source.read('head.bin')
                from .protocol import open_sealed
                current=open_sealed(keyring(self.secrets(old)),old['space_id'],raw,'head')
                if not current['deleted']:
                    if digest(current)!=candidate['head']:raise SyncError('CONFLICT')
                    self.heads(old,source).mutate(lambda head:head.update(deleted=True))
                elif current['previous']!=candidate['head']:raise SyncError('ROLLBACK')
                encrypted=base64.b64decode(saved['head_cipher']);new=open_sealed(keyring(secret),space,encrypted,'head')
                operations=[]
                for op_hash in saved['mapping'].values():
                    cipher,_=remote.read('objects/'+op_hash+'.bin');value=open_sealed(keyring(secret),space,cipher);operations.append((op_hash,value,cipher))
                return self.finish_migration(uid,candidate,old,remote,new,encrypted,saved['mapping'],operations)
            if not candidate or candidate['id']!=ident or old['space_id']!=candidate['source'] or old['status']!='paused':raise SyncError('CONFLICT')
            source=self.remote(old);heads=self.heads(old,source);head,_=heads.read()
            if digest(head)!=candidate['head']:raise SyncError('CONFLICT','旧后端状态已变化，请重新预览')
            self.checkpoint(uid,'更换后端前');config=candidate['config'];space=config['space_id']
            self.retire_binding(old)
            secret=self.candidate_secret(uid,candidate);remote=self.factory(config,secret);remote.initialize()
            mapping={};operations=[]
            def convert(op_hash):
                if op_hash in mapping:return mapping[op_hash]
                op=self.load_op(old,op_hash,source);parents=[convert(h) for h in op['parents']];value={**op,'space':space,'parents':parents,'restores':convert(op['restores']) if op['restores'] else None}
                cipher=seal(keyring(secret),space,value);new_hash=digest(cipher);remote.put('objects/'+new_hash+'.bin',cipher,create=True);mapping[op_hash]=new_hash;operations.append((new_hash,value,cipher))
                if op['deleted']:self.copy_deletion(uid,old['space_id'],op_hash,space,new_hash)
                return new_hash
            new=copy.deepcopy(head)
            for desc in new['objects'].values():desc['heads']=[convert(h) for h in desc['heads']]
            for tombstone in new['tombstones'].values():tombstone['hash']=convert(tombstone['hash'])
            new.update(space=space,sequence=0,previous=None,key_version=1,devices={candidate['device']:{'name':config['device_name'],'generation':1,'revoked':False,'ack':0}},executor={'device':candidate['device'],'generation':head['executor']['generation']+1},limits_proposal=None)
            for lease in new['accounts'].values():lease.update(device=candidate['device'],generation=lease['generation']+1)
            self.add_lineages(old,head,new,mapping,source,remote,secret)
            cipher=seal(keyring(secret),space,new,'head');remote.put('heads/'+digest(new)+'.bin',cipher,create=True)
            # 目标尚未成为可用头时先封存旧空间，失败则保持暂停，不自动切回双写。
            intent={'id':ident,'status':'staged','candidate':candidate,'mapping':mapping,'head_cipher':base64.b64encode(cipher).decode()}
            self.journal.put('sync',uid,'migration',intent)
            def close(current):
                if digest(current)!=candidate['head']:raise SyncError('CONFLICT')
                current['deleted']=True
            heads.mutate(close);self.journal.put('sync',uid,'migration',{**intent,'status':'source_closed'})
            return self.finish_migration(uid,candidate,old,remote,new,cipher,mapping,operations)

    def finish_migration(self,uid,candidate,old,remote,new,cipher,mapping,operations):
        config=candidate['config'];space=config['space_id']
        try:remote.put('head.bin',cipher,create=True)
        except SyncError as exc:
            if exc.code!='CONFLICT':raise
            existing,_=remote.read('head.bin')
            if existing!=cipher:raise SyncError('CONFLICT')
        mapping_rows=self.store.query('SELECT table_name,local_id,global_id FROM sync_rows WHERE user_id=?',(uid,))
        for index,(op_hash,value,encrypted) in enumerate(operations):
            record={'cipher':base64.b64encode(encrypted).decode(),'state':'confirmed'}
            if index==0:
                secret=self.candidate_secret(uid,candidate)
                record['local_map']=base64.b64encode(seal(keyring(secret),space,{'site':self.store.site_id,'owner':uid,'maps':[{'table':v['table_name'],'local':v['local_id'],'global':v['global_id']} for v in mapping_rows]},'local-map')).decode()
            self.journal.put('sync',uid,'operation:'+space+':'+op_hash,record)
        envelope=self.candidate_envelope(uid,candidate)
        next_binding={**dict(old),'space_id':space,'device_id':candidate['device'],'backend':config['backend'],'payload_json':json_text(config),'secret_json':envelope,'status':'configured','automatic':0,'updated_at':stamp()}
        self.persist_binding(next_binding)
        with self.store.tx(immediate=True) as c:
            c.execute('DELETE FROM sync_operations WHERE user_id=?',(uid,))
            for op_hash,value,encrypted in operations:self.save_op(c,uid,op_hash,value,encrypted,'confirmed')
            for entity in c.execute('SELECT * FROM sync_entities WHERE user_id=?',(uid,)).fetchall():
                c.execute('UPDATE sync_entities SET heads_json=? WHERE user_id=? AND kind=? AND object_id=?',(json_text([mapping[h] for h in json.loads(entity['heads_json'])]),uid,entity['kind'],entity['object_id']))
            c.execute('UPDATE sync_bindings SET space_id=?,device_id=?,backend=?,payload_json=?,secret_json=?,status=?,automatic=0,updated_at=? WHERE user_id=?',(space,candidate['device'],config['backend'],json_text(config),envelope,'configured',stamp(),uid))
        self.journal.put('sync',uid,'anchor:'+space,{'sequence':0,'hash':digest(new)});self.remember_control(self.binding(uid),new);self.set_status(uid,'synced',remote_cleanup='old_space_sealed');self.journal.put('sync',uid,'migration',{'id':candidate['id'],'status':'completed','from':old['space_id'],'to':space})
        self.persist_binding(self.binding(uid))
        return self.details(uid)
