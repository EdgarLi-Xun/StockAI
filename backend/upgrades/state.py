"""Append-only HMAC state and independent anchor, not part of a business checkpoint."""
import hashlib
import hmac
import json
import os
from pathlib import Path
import threading
from backend.cloud.crypto import atomic_private
from backend.cloud.lifecycle import ProcessLock
from backend.sync.protocol import canonical


class State:
    def __init__(self,folder,key):
        self.folder=Path(folder);self.folder.mkdir(parents=True,exist_ok=True);self.key=key;self.lock=threading.RLock()
        self.path=self.folder/'history.jsonl';self.head=self.folder/'anchor.json'
        with self._journal():
            if not self.path.exists():
                if self.head.exists():raise RuntimeError('升级状态记录缺失，拒绝重建')
                atomic_private(self.path,b'');self.anchor(0,'')
            self.reload()
    def _journal(self):return ProcessLock(self.folder/'state.lock',wait=True)
    def sign(self,value):return hmac.new(self.key,canonical(value),hashlib.sha256).hexdigest()
    def anchor(self,seq,chain):
        value={'seq':seq,'chain':chain};atomic_private(self.head,canonical({**value,'signature':self.sign(value)}))
    def reload(self):
        if not self.head.exists():raise RuntimeError('升级状态锚点缺失')
        anchor=json.loads(self.head.read_bytes());signature=anchor.pop('signature')
        if not hmac.compare_digest(signature,self.sign(anchor)):raise RuntimeError('升级锚点签名无效')
        seq=0;chain='';matched=anchor['seq']==0;values={}
        valid_bytes=0;incomplete=False
        with self.path.open('rb') as source:
            for line in source:
                if not line.endswith(b'\n'):incomplete=True;break
                row=json.loads(line);signature=row.pop('signature');seq+=1
                if row['seq']!=seq or row['previous']!=chain or not hmac.compare_digest(signature,self.sign(row)):raise RuntimeError('升级状态链损坏')
                chain=signature;values[row['slot']]=row['value']
                if seq==anchor['seq'] and chain==anchor['chain']:matched=True
                valid_bytes+=len(line)
        if seq<anchor['seq'] or not matched:raise RuntimeError('升级状态被回退')
        if incomplete:
            # 仅清理没有到达已签名锚点的未提交尾部；完整但无效的记录仍拒绝。
            with self.path.open('r+b') as target:target.truncate(valid_bytes);target.flush();os.fsync(target.fileno())
        if seq>anchor['seq']:self.anchor(seq,chain)
        self.seq,self.chain,self.values=seq,chain,values
    def get(self,slot,default=None):
        with self.lock,self._journal():self.reload();return self.values.get(slot,default)
    def prefixed(self,prefix):
        with self.lock,self._journal():
            self.reload();return {k:v for k,v in self.values.items() if k.startswith(prefix)}
    def put(self,slot,value):
        with self.lock,self._journal():
            self.reload()
            if self.values.get(slot)==value:return value
            row={'seq':self.seq+1,'previous':self.chain,'slot':slot,'value':value};signature=self.sign(row)
            with self.path.open('ab') as output:output.write(canonical({**row,'signature':signature})+b'\n');output.flush();os.fsync(output.fileno())
            self.anchor(row['seq'],signature);self.seq,self.chain=row['seq'],signature;self.values[slot]=value
        return value
