# 在研究备份外维护带签名的删除与模型用量权威记录，恢复后仍可核对。
"""Independent signed resource-deletion and model-use authority, outside research backups."""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from backend.config import TZ,now
from .auth import AuthError
from .crypto import atomic_private
from .store import json_text,stamp


def known_tokens(usage):
    if not isinstance(usage,dict):return None
    raw=usage.get('total_tokens')
    if raw is None:
        parts=[usage.get(k) for k in ('eval_count','prompt_eval_count')]
        if not all(isinstance(p,int) and not isinstance(p,bool) and p>=0 for p in parts):return None
        raw=sum(parts)
    if isinstance(raw,bool) or not isinstance(raw,(int,float)) or raw<0 or raw>9007199254740991 or not math.isfinite(raw) or int(raw)!=raw:return None
    return int(raw)


# 为权威账本方法添加可重入锁，串行化记录追加和锚点更新。
def locked(method):
    # 在同一实例锁内调用原方法，覆盖整个权威状态变更过程。
    def call(self,*args,**kwargs):
        with self.lock:return method(self,*args,**kwargs)
    return call


# 协调独立账本、签名锚点、用量预留和研究资源删除记录。
class Control:
    # 建立权威存储并核验签名，已有站点丢失账本时拒绝重新初始化。
    def __init__(self,store,keys):
        self.store,self.keys=store,keys
        self.path=store.cfg.erasures/'research-authority.sqlite3';self.head=store.cfg.erasures/'research-head.json'
        self.lock=threading.RLock()
        self.path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        initialized=store.one("SELECT value FROM cloud_meta WHERE key='research_authority_initialized'")
        if not self.path.exists() or not self.head.exists():
            if initialized:
                raise RuntimeError('最新研究删除与用量权威记录缺失，拒绝恢复或调用')
            with self.tx() as c:
                c.executescript("CREATE TABLE IF NOT EXISTS ledger(seq INTEGER PRIMARY KEY,previous TEXT NOT NULL,payload TEXT NOT NULL,signature TEXT NOT NULL,version INTEGER NOT NULL); CREATE TABLE IF NOT EXISTS usage(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,day TEXT NOT NULL,month TEXT NOT NULL,state TEXT NOT NULL,payload TEXT NOT NULL); CREATE INDEX IF NOT EXISTS ix_usage_owner ON usage(user_id,day,month,state); CREATE TABLE IF NOT EXISTS deletions(user_id TEXT NOT NULL,kind TEXT NOT NULL,id TEXT NOT NULL,ts TEXT NOT NULL,PRIMARY KEY(user_id,kind,id));")
            self.write_head(0,'')
        self.verify()
        if not initialized:
            self.import_legacy_usage()
        store.execute("INSERT OR IGNORE INTO cloud_meta VALUES('research_authority_initialized','1')")

    # 把已有模型调用幂等导入独立用量账本，保留已发送和不确定调用的计数。
    def import_legacy_usage(self):
        """Preserve known sends on an additive upgrade; unknown usage stays unknown."""
        rows=self.store.query('SELECT m.*,j.kind,j.snapshot_json FROM model_calls m JOIN personal_jobs j ON j.id=m.job_id')
        head=None
        with self.lock,self.tx() as c:
            existing={r[0] for r in c.execute('SELECT id FROM usage')}
            for row in rows:
                if row['phase']=='prepared' or (row['phase']=='failed' and row.get('error_code')=='CONNECT_FAILED'):continue
                ident=row['job_id']+':'+str(row['attempt'])
                if ident in existing:continue
                when=datetime.fromisoformat(row['created_at'])
                if when.tzinfo is None:raise RuntimeError('旧模型调用时间缺少时区，无法核对用量窗口')
                when=when.astimezone(TZ);profile=json.loads(row['snapshot_json'] or '{}').get('profile') or {}
                try:usage=json.loads(row['usage_json'] or '{}')
                except ValueError:usage={}
                day=when.date().isoformat();state=row['phase'] if row['phase'] in {'received','failed'} else 'uncertain'
                value={'type':'usage','id':ident,'user_id':row['user_id'],'profile_id':profile.get('profile_id'),'purpose':row['kind'],'day':day,'month':day[:7],'state':state,'ts':row['created_at'],'tokens':known_tokens(usage),'origin':'legacy_model_calls'}
                head=self.append(c,value)
                c.execute('INSERT INTO usage VALUES(?,?,?,?,?,?)',(ident,row['user_id'],day,day[:7],state,json_text(value)));existing.add(ident)
        if head:self.write_head(*head)

    # 以立即事务和完整同步持久化独立账本，失败时回滚。
    @contextmanager
    def tx(self):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row;c.execute('PRAGMA synchronous=FULL');c.execute('BEGIN IMMEDIATE')
        try:
            yield c;c.commit()
        except BaseException:
            c.rollback();raise
        finally:
            c.close()

    # 将站点、最新序号及链尾签名写入经过签名的独立锚点文件。
    def write_head(self,seq,signature):
        row={'site':self.store.site_id,'seq':seq,'chain':signature,'key_version':self.keys.active}
        row['signature']=self.keys.sign(json_text(row));atomic_private(self.head,json_text(row).encode())

    # 将带前序签名的事件追加到账本，返回新序号和链尾签名。
    def append(self,c,value):
        row=c.execute('SELECT seq,signature FROM ledger ORDER BY seq DESC LIMIT 1').fetchone();seq=row['seq']+1 if row else 1;prev=row['signature'] if row else ''
        encoded=json_text(value);signature=self.keys.sign(json_text([self.store.site_id,seq,prev,encoded]))
        c.execute('INSERT INTO ledger VALUES(?,?,?,?,?)',(seq,prev,encoded,signature,self.keys.active))
        return seq,signature

    # 核对站点、签名链和锚点，重建用量及删除投影并检测账本回退。
    def verify(self):
        with self.lock:
            if not self.head.exists() or not self.path.exists():
                raise RuntimeError('研究删除与用量权威记录缺失')
            head=json.loads(self.head.read_text(encoding='utf-8'));signature=head.pop('signature')
            if head['site']!=self.store.site_id or not hmac.compare_digest(signature,self.keys.sign(json_text(head),head['key_version'])):
                raise RuntimeError('研究权威记录签名或站点无效')
            seq=0;prev='';anchor=head['seq']==0 and not head['chain'];usages={};deleted={};delete_signatures={}
            with self.tx() as c:
                for r in c.execute('SELECT * FROM ledger ORDER BY seq'):
                    seq+=1
                    if r['seq']!=seq or r['previous']!=prev or not hmac.compare_digest(r['signature'],self.keys.sign(json_text([self.store.site_id,seq,prev,r['payload']]),r['version'])):
                        raise RuntimeError('研究权威记录不连续或已损坏')
                    prev=r['signature'];value=json.loads(r['payload'])
                    if value['type']=='usage': usages[value['id']]=value
                    elif value['type']=='delete':
                        key=(value['user_id'],value['kind'],value['id']);deleted[key]=value;delete_signatures[key]=r['signature']
                    elif value['type']=='restore':
                        key=(value['user_id'],value['kind'],value['id']);previous=deleted.get(key)
                        if not previous or previous['ts']!=value['deleted_at'] or delete_signatures[key]!=value['deletion_signature']:
                            raise RuntimeError('研究恢复意图与删除记录不匹配')
                        del deleted[key];del delete_signatures[key]
                    if seq==head['seq'] and prev==head['chain']: anchor=True
                if seq<head['seq'] or not anchor: raise RuntimeError('研究权威记录被回退')
                c.execute('DELETE FROM usage');c.execute('DELETE FROM deletions')
                for v in usages.values(): c.execute('INSERT INTO usage VALUES(?,?,?,?,?,?)',(v['id'],v['user_id'],v['day'],v['month'],v['state'],json_text(v)))
                for v in deleted.values(): c.execute('INSERT INTO deletions VALUES(?,?,?,?)',(v['user_id'],v['kind'],v['id'],v['ts']))
            if seq>head['seq']: self.write_head(seq,prev)
        return {'usage':len(usages),'deletions':len(deleted)}

    # 在同一事务中核对日月上限并预留一次调用，避免并发超额。
    @locked
    def reserve(self,uid,ident,profile,purpose,limits,at=None):
        at=(at or now()).astimezone(TZ);day=at.date().isoformat();month=day[:7]
        with self.lock,self.tx() as c:
            old=c.execute('SELECT payload FROM usage WHERE id=?',(ident,)).fetchone()
            if old:
                old=json.loads(old[0])
                if old['state'] not in {'reserved','released'}: return old
            for window,column,value in (('daily','day',day),('monthly','month',month)):
                cap=limits.get(window)
                if cap is None:continue
                count=c.execute(f"SELECT COUNT(*) FROM usage WHERE user_id=? AND {column}=? AND state!='released' AND id!=?",(uid,value,ident)).fetchone()[0]
                if cap is not None and count>=cap:
                    raise AuthError('USAGE_LIMIT',('日' if window=='daily' else '月')+'调用上限已用完，未发送模型请求',409)
            value={'type':'usage','id':ident,'user_id':uid,'profile_id':profile,'purpose':purpose,'day':day,'month':month,'state':'reserved','ts':stamp(),'tokens':None}
            seq,sig=self.append(c,value)
            c.execute('INSERT INTO usage VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET day=excluded.day,month=excluded.month,state=excluded.state,payload=excluded.payload',(ident,uid,day,month,'reserved',json_text(value)))
        self.write_head(seq,sig);return value

    # 记录调用从预留到发送或释放等状态变化，并保存可核验的令牌用量。
    @locked
    def transition(self,ident,state,usage=None):
        with self.lock,self.tx() as c:
            row=c.execute('SELECT payload FROM usage WHERE id=?',(ident,)).fetchone()
            if not row:return
            value=json.loads(row[0])
            if state=='released' and value['state'] not in {'reserved','released'}:return
            value['state']=state;value['updated_at']=stamp()
            if usage is not None:
                value['tokens']=known_tokens(usage)
            seq,sig=self.append(c,value);c.execute('UPDATE usage SET state=?,payload=? WHERE id=?',(state,json_text(value),ident))
        self.write_head(seq,sig)

    # 幂等持久化研究资源删除意图，使旧备份恢复后仍会删除该对象。
    @locked
    def delete(self,uid,kind,ident):
        with self.lock,self.tx() as c:
            if c.execute('SELECT 1 FROM deletions WHERE user_id=? AND kind=? AND id=?',(uid,kind,ident)).fetchone():return
            value={'type':'delete','user_id':uid,'kind':kind,'id':ident,'ts':stamp()};seq,sig=self.append(c,value)
            c.execute('INSERT INTO deletions VALUES(?,?,?,?)',(uid,kind,ident,value['ts']))
        self.write_head(seq,sig)

    # 检查指定用户和类型的资源是否已有权威删除记录。
    def deleted(self,uid,kind,ident):
        with self.tx() as c:return bool(c.execute('SELECT 1 FROM deletions WHERE user_id=? AND kind=? AND id=?',(uid,kind,ident)).fetchone())

    def deletion(self,uid,kind,ident,c=None):
        """Return the signed deletion identity, independently of restorable business rows."""
        if c is None:
            with self.tx() as connection:return self.deletion(uid,kind,ident,connection)
        row=c.execute('SELECT ts FROM deletions WHERE user_id=? AND kind=? AND id=?',(uid,kind,ident)).fetchone()
        if not row:return None
        event=c.execute("SELECT signature FROM ledger WHERE json_extract(payload,'$.type')='delete' AND json_extract(payload,'$.user_id')=? AND json_extract(payload,'$.kind')=? AND json_extract(payload,'$.id')=? ORDER BY seq DESC LIMIT 1",(uid,kind,ident)).fetchone()
        if not event:raise RuntimeError('研究删除权威记录缺少签名事件')
        return {'ts':row['ts'],'signature':event['signature']}

    @locked
    def restore(self,uid,kind,ident,tombstone,expected_deletion):
        """A deliberate sync restore supersedes exactly the current local deletion."""
        if not isinstance(tombstone,str) or len(tombstone)!=64 or any(v not in '0123456789abcdef' for v in tombstone):raise ValueError('恢复墓碑标识无效')
        with self.tx() as c:
            previous=self.deletion(uid,kind,ident,c)
            if not previous or previous['signature']!=expected_deletion:return False
            value={'type':'restore','user_id':uid,'kind':kind,'id':ident,'deleted_at':previous['ts'],'deletion_signature':expected_deletion,'tombstone':tombstone,'ts':stamp()}
            seq,sig=self.append(c,value)
            c.execute('DELETE FROM deletions WHERE user_id=? AND kind=? AND id=?',(uid,kind,ident))
        self.write_head(seq,sig)
        return True

    # 读取全部或指定用户的用量投影，按日期和标识稳定排序。
    def rows(self,uid=None):
        with self.tx() as c:
            return [json.loads(r[0]) for r in c.execute('SELECT payload FROM usage'+(' WHERE user_id=?' if uid else '')+' ORDER BY day DESC,id',(uid,) if uid else ())]

    # 校验独立账本后重新应用删除，并处理被中断的提醒投递状态。
    def reconcile(self,store=None):
        target=store or self.store;self.verify()
        with self.tx() as c: rows=[dict(r) for r in c.execute('SELECT * FROM deletions')]
        with target.publish_lock,target.tx(immediate=True) as c:
            for r in rows:
                if r['kind']=='chat_session':
                    for req in c.execute('SELECT job_id FROM assistant_requests WHERE user_id=? AND session_id=?',(r['user_id'],r['id'])).fetchall():
                        if req['job_id']:c.execute("UPDATE personal_jobs SET snapshot_json='{}',status='cancelled',error_code='SOURCE_DELETED' WHERE id=? AND user_id=?",(req['job_id'],r['user_id']))
                c.execute('DELETE FROM research_objects WHERE id=? AND user_id=? AND kind=?',(r['id'],r['user_id'],r['kind']))
            c.execute("UPDATE alert_deliveries SET state='uncertain',error_code='DELIVERY_UNCERTAIN' WHERE state='sending'")
        return len(rows)


# 惰性建立并复用当前存储的研究权威控制器。
def control(store,keys=None):
    with store._requests_lock:
        if not hasattr(store,'research_control'):
            if keys is None:
                from .crypto import Keyring
                keys=Keyring(store.cfg.key_file)
            store.research_control=Control(store,keys)
    return store.research_control


# 读取个人日月调用上限，未设置的窗口保持无限制状态。
def limits(store,uid):
    row=store.one("SELECT payload_json FROM research_objects WHERE user_id=? AND kind='usage_settings'",(uid,))
    return json.loads(row['payload_json']) if row else {'daily':None,'monthly':None}


# 按上海日期汇总日月已发送、预留、不确定用量和配置用途分组。
def summary(store,uid,at=None):
    at=(at or now()).astimezone(TZ);rows=control(store).rows(uid);caps=limits(store,uid);result={}
    for label,key,scope in (('daily','day',at.date().isoformat()),('monthly','month',at.strftime('%Y-%m'))):
        group=[r for r in rows if r[key]==scope and r['state']!='released'];sent=[r for r in group if r['state']!='reserved'];reserved=len(group)-len(sent)
        result[label]={'window':scope,'limit':caps[label],'sent':len(sent),'reserved':reserved,'uncertain':sum(r['state']=='uncertain' for r in group),'known_tokens':sum(r['tokens'] for r in sent if r.get('tokens') is not None),'unknown_usage':sum(r.get('tokens') is None for r in sent),'remaining':max(caps[label]-len(group),0) if caps[label] is not None else None}
    groups={}
    for r in rows:
        if r['state']=='released' or r['month']!=at.strftime('%Y-%m'):continue
        key=(r['profile_id'],r['purpose']);groups.setdefault(key,{'profile_id':key[0],'purpose':key[1],'sent':0,'reserved':0,'known_tokens':0,'unknown_usage':0})
        g=groups[key];g['reserved' if r['state']=='reserved' else 'sent']+=1
        if r['state']!='reserved':
            if r.get('tokens') is None:g['unknown_usage']+=1
            else:g['known_tokens']+=r['tokens']
    return {**result,'groups':list(groups.values()),'supplier_balance':None,'billed_cost':None,'timezone':'Asia/Shanghai'}
