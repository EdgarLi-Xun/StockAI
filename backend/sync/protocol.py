from __future__ import annotations
import base64
from datetime import datetime,timezone
import hashlib
import json
import os
import re
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

MAX_OBJECT=32*1024*1024
MAX_HEAD=8*1024*1024
ID=re.compile(r'^[a-zA-Z0-9_-]{8,100}$')
HASH=re.compile(r'^[0-9a-f]{64}$')


class SyncError(ValueError):
    def __init__(self,code,message=None):
        self.code=code;super().__init__(message or {'OFFLINE':'同步服务暂不可达，本地资料保留','AUTH':'同步凭据失效，请重新连接','CONFLICT':'远端已变化，请重新读取合并','STORAGE_FULL':'同步存储空间不足','INCOMPATIBLE':'服务不支持安全条件写入','CORRUPT':'同步内容或完整性验证失败','ROLLBACK':'远端状态回退或不连续，已暂停写入','KEY':'同步解密密钥不匹配','REVOKED':'设备已撤销，需重新授权并完整校准','MISSING':'指定远端空间不存在，未清空本地资料','DEPENDENCY':'依赖尚未完整接收','LEASE':'执行归属无法核验，动作已暂停','QUOTA':'跨端调用上限已用完','MAINTENANCE':'当前正在维护，已暂停同步写入'}.get(code,'同步操作未完成'))


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode()


def digest(value):return hashlib.sha256(value if isinstance(value,bytes) else canonical(value)).hexdigest()
def timestamp():return datetime.now(timezone.utc).isoformat(timespec='microseconds')


def key_text(key):return base64.urlsafe_b64encode(key).decode()


def parse_key(value):
    try:key=base64.b64decode(value.encode(),altchars=b'-_',validate=True)
    except Exception:raise SyncError('KEY') from None
    if len(key)!=32:raise SyncError('KEY')
    return key


def keyring(secret):
    return {int(k):parse_key(v) for k,v in secret.get('keys',{'1':secret['key']}).items()}


def seal(key,space,value,kind='object',generation=1):
    if not ID.fullmatch(space):raise SyncError('CORRUPT')
    raw=canonical(value)
    if len(raw)>MAX_OBJECT:raise SyncError('CORRUPT','单个同步对象超过大小限制，请减少附件或拆分历史')
    nonce=os.urandom(12);header={'protocol':1,'space':space,'kind':kind,'key_version':generation}
    cipher=AESGCM(key[generation] if isinstance(key,dict) else key).encrypt(nonce,raw,canonical(header))
    return canonical({**header,'nonce':base64.b64encode(nonce).decode(),'cipher':base64.b64encode(cipher).decode()})


def open_sealed(key,space,raw,kind='object',generation=None):
    if len(raw)>MAX_OBJECT*2:raise SyncError('CORRUPT')
    try:
        value=json.loads(raw);header={k:value[k] for k in ('protocol','space','kind','key_version')}
        if header['protocol']!=1:raise SyncError('INCOMPATIBLE')
        if header['space']!=space or header['kind']!=kind or (generation is not None and header['key_version']!=generation):raise SyncError('CORRUPT')
        plain=AESGCM(key[header['key_version']] if isinstance(key,dict) else key).decrypt(base64.b64decode(value['nonce'],validate=True),base64.b64decode(value['cipher'],validate=True),canonical(header))
        if len(plain)>MAX_OBJECT:raise SyncError('CORRUPT')
        return json.loads(plain)
    except SyncError:raise
    except Exception:raise SyncError('KEY') from None


def validate_operation(value,space):
    required={'id','space','device','kind','object','parents','deleted','immutable','payload','schema','created_at','restores'}
    if not isinstance(value,dict) or set(value)!=required or value['space']!=space or value['schema']!=1:raise SyncError('INCOMPATIBLE')
    if not all(isinstance(value[k],str) and ID.fullmatch(value[k]) for k in ('id','device','object')):raise SyncError('CORRUPT')
    if not isinstance(value['parents'],list) or len(value['parents'])>1000 or any(not isinstance(v,str) or not HASH.fullmatch(v) for v in value['parents']):raise SyncError('CORRUPT')
    if type(value['deleted']) is not bool or type(value['immutable']) is not bool:raise SyncError('CORRUPT')
    if len(canonical(value))>MAX_OBJECT:raise SyncError('CORRUPT')
    return value
