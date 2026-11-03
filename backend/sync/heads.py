"""Authenticated manifest compare-and-swap and monotonic shared control rules."""
import copy
from datetime import datetime,timezone
import uuid
from .protocol import SyncError,canonical,digest,seal,open_sealed,HASH


def empty(space,device,name):
    return {'protocol':1,'schema':1,'space':space,'sequence':0,'previous':None,'devices':{device:{'name':name,'generation':1,'revoked':False,'ack':0}},'objects':{},'tombstones':{},'usage':{},'limits':{'daily':None,'monthly':None},'accounts':{},'executor':{'device':device,'generation':1},'receipts':{},'deleted':False,'key_version':1}


def validate(head,space,previous=None):
    if head.get('protocol')!=1 or head.get('schema')!=1:raise SyncError('INCOMPATIBLE')
    if head.get('space')!=space or type(head.get('sequence')) is not int or head['sequence']<0:raise SyncError('CORRUPT')
    for field in ('objects','tombstones','usage','devices','accounts','receipts'):
        if not isinstance(head.get(field),dict):raise SyncError('CORRUPT')
    for field in ('daily','monthly'):
        cap=head['limits'].get(field)
        if cap is not None and (type(cap) is not int or cap<0):raise SyncError('CORRUPT')
    if previous:
        if head['sequence']!=previous['sequence']+1 or head['previous']!=digest(previous):raise SyncError('ROLLBACK')
        for field in ('tombstones','usage','receipts'):
            if not set(previous[field])<=set(head[field]):raise SyncError('ROLLBACK','共享删除、用量或执行记录被回退')
        for ident,before in previous['usage'].items():
            after=head['usage'][ident]
            if before['state'] not in ('reserved','released') and after['state'] in ('reserved','released'):raise SyncError('ROLLBACK')
            if before['state'] not in ('reserved','released') and any(before.get(k)!=after.get(k) for k in ('day','month')):raise SyncError('ROLLBACK')
        for ident,before in previous['devices'].items():
            after=head['devices'].get(ident)
            if not after or after['generation']<before['generation'] or (before['revoked'] and not after['revoked'] and after['generation']==before['generation']):raise SyncError('REVOKED')
        if previous['deleted'] and not head['deleted']:raise SyncError('ROLLBACK')
    if len(canonical(head))>8*1024*1024:raise SyncError('CORRUPT','同步清单过大，需要受控归档')
    return head


class Heads:
    def __init__(self,remote,key,space,device,anchor_get,anchor_put):
        self.remote,self.key,self.space,self.device=remote,key,space,device
        self.anchor_get,self.anchor_put=anchor_get,anchor_put

    def read(self):
        raw,etag=self.remote.read('head.bin');head=validate(open_sealed(self.key,self.space,raw,'head'),self.space)
        anchor=self.anchor_get();current=head;chain=[]
        if anchor and head['sequence']<anchor['sequence']:raise SyncError('ROLLBACK')
        floor=anchor['sequence'] if anchor else 0
        while current['sequence']>floor:
            parent_hash=current['previous']
            if not isinstance(parent_hash,str) or not HASH.fullmatch(parent_hash):raise SyncError('ROLLBACK')
            old,_=self.remote.read('heads/'+parent_hash+'.bin');parent=validate(open_sealed(self.key,self.space,old,'head'),self.space)
            if digest(parent)!=parent_hash:raise SyncError('CORRUPT')
            validate(current,self.space,parent);chain.append(current);current=parent
            if len(chain)>100000:raise SyncError('CORRUPT','同步链需要完整校准')
        if anchor and digest(current)!=anchor['hash']:raise SyncError('ROLLBACK')
        device=head['devices'].get(self.device)
        if not device or device['revoked'] or head['deleted']:raise SyncError('REVOKED')
        self.anchor_put({'sequence':head['sequence'],'hash':digest(head)})
        return head,etag

    def create(self,name):
        self.remote.initialize();head=empty(self.space,self.device,name)
        body=seal(self.key,self.space,head,'head')
        try:self.remote.put('heads/'+digest(head)+'.bin',body,create=True)
        except SyncError as exc:
            if exc.code!='CONFLICT':raise
            existing,_=self.remote.read('heads/'+digest(head)+'.bin')
            if open_sealed(self.key,self.space,existing,'head')!=head:raise SyncError('CORRUPT')
            body=existing
        self.remote.put('head.bin',body,create=True);self.anchor_put({'sequence':0,'hash':digest(head)})
        return head

    def mutate(self,fn,retries=5):
        for _ in range(retries):
            head,etag=self.read();next_head=copy.deepcopy(head);result=fn(next_head)
            if next_head==head:return result,head
            next_head['sequence']=head['sequence']+1;next_head['previous']=digest(head);validate(next_head,self.space,head)
            encrypted=seal(self.key,self.space,next_head,'head',next_head['key_version'])
            path='heads/'+digest(next_head)+'.bin'
            try:self.remote.put(path,encrypted,create=True)
            except SyncError as exc:
                if exc.code!='CONFLICT':raise
                existing,_=self.remote.read(path)
                if open_sealed(self.key,self.space,existing,'head')!=next_head:raise SyncError('CORRUPT')
            try:self.remote.put('head.bin',encrypted,etag=etag)
            except SyncError as exc:
                if exc.code=='CONFLICT':continue
                raise
            self.anchor_put({'sequence':next_head['sequence'],'hash':digest(next_head)})
            return result,next_head
        raise SyncError('CONFLICT','同步清单持续竞争，请稍后重试')

    def reserve_usage(self,ident,day,month,limits,purpose):
        def change(head):
            old=head['usage'].get(ident)
            if old and old['state']!='released':raise SyncError('LEASE','此调用已被预留或记录，不能以相同标识再次发送')
            for field,column,value in (('daily','day',day),('monthly','month',month)):
                caps=[v for v in (limits.get(field),head['limits'].get(field)) if v is not None];cap=min(caps) if caps else None
                used=sum(1 for k,r in head['usage'].items() if k!=ident and r[column]==value and r['state']!='released')
                if cap is not None and used>=cap:raise SyncError('QUOTA')
            row={'device':self.device,'day':day,'month':month,'state':'reserved','purpose':purpose,'tokens':None};head['usage'][ident]=row;return row
        return self.mutate(change)[0]

    def usage_transition(self,ident,state,tokens=None):
        def change(head):
            old=head['usage'].get(ident)
            if not old:raise SyncError('ROLLBACK')
            if old['device']!=self.device:raise SyncError('LEASE')
            if state=='released' and old['state'] not in ('reserved','released'):return
            old['state']=state
            if tokens is not None:old['tokens']=tokens
        self.mutate(change)

    def receipt(self,kind,logical_key,*,delivery=None,retry=None):
        ident=digest([kind,logical_key])
        def change(head):
            lease=head['executor']
            if lease['device']!=self.device:raise SyncError('LEASE')
            previous=head['receipts'].get(ident)
            if previous:
                # 仅明确 SMTP 拒绝后的同一投递可消耗一次重试；旧备份不能重用已消耗的次数。
                if kind!='mail' or not delivery or type(retry) is not int or retry<1:return False
                if previous['device']!=self.device or previous['generation']!=lease['generation'] or previous.get('delivery')!=delivery or previous['state']!='reserved' or previous.get('attempt')!=retry:return False
                previous['attempt']=retry+1;return True
            receipt={'device':self.device,'generation':lease['generation'],'state':'reserved'}
            if kind=='mail' and delivery:receipt.update(delivery=delivery,attempt=retry+1 if type(retry) is int and retry>0 else 1)
            head['receipts'][ident]=receipt;return True
        return self.mutate(change)[0]
