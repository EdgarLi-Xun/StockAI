"""Recoverable local binding material, separate from business backups and remote objects."""
import base64
import json
from pathlib import Path
import re
from backend.cloud.crypto import atomic_private
from backend.cloud.store import json_text
from .protocol import SyncError,digest,seal,open_sealed,keyring


class LocalState:
    def private_folder(self,uid):return self.store.cfg.erasures/'sync-private'/digest(uid.encode())

    def read_private(self,uid,name):
        if not re.fullmatch(r'[a-z0-9_-]{8,150}',name):raise SyncError('CORRUPT')
        ref=self.journal.get('sync',uid,'private:'+name)
        if ref is None:return None
        path=self.private_folder(uid)/(name+'-'+ref['sha256']+'.json')
        try:raw=path.read_bytes()
        except OSError:raise SyncError('KEY','本机同步恢复材料缺失，请恢复独立控制目录') from None
        if digest(raw)!=ref['sha256']:raise SyncError('ROLLBACK')
        try:return json.loads(self.keys.open(raw.decode(),site=self.store.site_id,user=uid,resource=name,purpose='sync-local-state'))
        except Exception:raise SyncError('KEY') from None

    def write_private(self,uid,name,value):
        if self.read_private(uid,name)==value:return
        raw=self.keys.seal(json_text(value),site=self.store.site_id,user=uid,resource=name,purpose='sync-local-state').encode()
        atomic_private(self.private_folder(uid)/(name+'-'+digest(raw)+'.json'),raw)
        self.journal.put('sync',uid,'private:'+name,{'sha256':digest(raw)})

    def persist_binding(self,row):
        self.write_private(row['user_id'],'binding-'+row['space_id'],dict(row));self.journal.put('sync',row['user_id'],'binding-current',row['space_id'])

    def retire_binding(self,row):
        self.write_private(row['user_id'],'binding-'+row['space_id'],dict(row));self.journal.put('sync',row['user_id'],'retired:'+row['space_id'],True)
        return row['space_id']

    def operation_record(self,row,adapter,cipher,state):
        record={'cipher':base64.b64encode(cipher).decode(),'state':state}
        if adapter.created_maps:
            secret=self.secrets(row);record['local_map']=base64.b64encode(seal(keyring(secret),row['space_id'],{'site':self.store.site_id,'owner':row['user_id'],'maps':adapter.created_maps},'local-map',secret.get('generation',1))).decode()
        return record

    def restore_maps(self,row,record):
        if not record.get('local_map'):return
        value=open_sealed(keyring(self.secrets(row)),row['space_id'],base64.b64decode(record['local_map']),'local-map')
        if value['site']!=self.store.site_id or value['owner']!=row['user_id']:raise SyncError('CORRUPT')
        with self.store.tx(immediate=True) as c:
            for item in value['maps']:
                before=c.execute('SELECT global_id FROM sync_rows WHERE user_id=? AND table_name=? AND local_id=?',(row['user_id'],item['table'],item['local'])).fetchone()
                if before and before[0]!=item['global']:raise SyncError('CORRUPT')
                c.execute('INSERT OR IGNORE INTO sync_rows VALUES(?,?,?,?)',(row['user_id'],item['table'],item['local'],item['global']))

    def recover_bindings(self):
        for user in self.store.query("SELECT id FROM users WHERE status='active'"):
            uid=user['id'];space=self.journal.get('sync',uid,'binding-current','unseen')
            if space=='unseen':continue
            old=self.store.one('SELECT * FROM sync_bindings WHERE user_id=?',(uid,))
            if space is None:
                if old:
                    with self.store.tx(immediate=True) as c:
                        for table in ('sync_operations','sync_entities','sync_rows','sync_conflicts','sync_pending','sync_dirty','sync_bindings'):c.execute('DELETE FROM '+table+' WHERE user_id=?',(uid,))
                continue
            row=self.read_private(uid,'binding-'+space)
            if not row or row['user_id']!=uid or row['space_id']!=space:raise SyncError('CORRUPT')
            if old and old['space_id']!=space:
                with self.store.tx(immediate=True) as c:
                    for table in ('sync_operations','sync_entities','sync_rows','sync_conflicts','sync_pending'):c.execute('DELETE FROM '+table+' WHERE user_id=?',(uid,))
            self.store.execute('INSERT OR REPLACE INTO sync_bindings VALUES(?,?,?,?,?,?,?,?,?,?)',tuple(row[k] for k in ('user_id','space_id','device_id','backend','version','payload_json','secret_json','status','automatic','updated_at')))

    def forget_private(self,uid):
        if self.store.active(uid):return
        if any(v.get('status')!='completed' for v in self.journal.all('sync-cleanup',uid,'intent:').values()):return
        folder=self.private_folder(uid)
        if folder.exists():
            if not folder.resolve().is_relative_to((self.store.cfg.erasures/'sync-private').resolve()):raise SyncError('CORRUPT')
            for path in folder.iterdir():
                if path.is_file() and not path.is_symlink():path.unlink()
            folder.rmdir()
