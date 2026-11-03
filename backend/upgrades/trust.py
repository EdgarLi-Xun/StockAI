from __future__ import annotations
import base64
from datetime import datetime,timezone
import hashlib
import re
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey,Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding,PublicFormat
from backend.sync.protocol import canonical,digest
from .versions import Version,compatible


class UpgradeError(ValueError):
    def __init__(self,code,message=None):self.code=code;super().__init__(message or {'UNTRUSTED':'发布清单身份或签名验证失败','EXPIRED':'发布元数据已过期，未执行更新','REPLAY':'检测到旧发布元数据回放','TARGET':'升级包不适用于当前组件、平台或安装形态','DOWNGRADE':'普通更新不执行降级','COMPATIBILITY':'没有已验证的兼容升级路径','PACKAGE':'升级文件长度或摘要不匹配','MAINTENANCE':'服务正在维护','BUSY':'已有升级事务或在途操作','RECOVERY':'缺少可验证的恢复材料','WRITES':'新版本已开放写入，不能直接覆盖回升级前备份','CONFIRMATION':'请确认当前具体升级计划','EXECUTOR':'需服务器运维执行此计划'}.get(code,'升级操作未完成'))


def decode(value,length):
    try:result=base64.b64decode(value,validate=True)
    except Exception:raise UpgradeError('UNTRUSTED') from None
    if len(result)!=length:raise UpgradeError('UNTRUSTED')
    return result


def expires(value,now=None):
    try:when=datetime.fromisoformat(value.replace('Z','+00:00'))
    except Exception:raise UpgradeError('UNTRUSTED') from None
    if when.tzinfo is None:raise UpgradeError('UNTRUSTED')
    if when<=(now or datetime.now(timezone.utc)):raise UpgradeError('EXPIRED')


def public_key(private):
    raw=private.public_key().public_bytes(Encoding.Raw,PublicFormat.Raw);return {'keytype':'ed25519','public':base64.b64encode(raw).decode()}


def sign(signed,private_keys):
    return {'signed':signed,'signatures':[{'keyid':digest(public_key(key)),'sig':base64.b64encode(key.sign(canonical(signed))).decode()} for key in private_keys]}


def verify(envelope,root,role):
    if not isinstance(envelope,dict) or set(envelope)!={'signed','signatures'}:raise UpgradeError('UNTRUSTED')
    rule=root['roles'].get(role)
    if not rule or type(rule.get('threshold')) is not int or not 1<=rule['threshold']<=len(rule['keyids']):raise UpgradeError('UNTRUSTED')
    good=set()
    for signature in envelope['signatures']:
        ident=signature.get('keyid');entry=root['keys'].get(ident)
        if not entry or ident not in rule['keyids'] or entry.get('keytype')!='ed25519' or digest(entry)!=ident:continue
        try:Ed25519PublicKey.from_public_bytes(decode(entry['public'],32)).verify(decode(signature['sig'],64),canonical(envelope['signed']));good.add(ident)
        except Exception:continue
    if len(good)<rule['threshold']:raise UpgradeError('UNTRUSTED')
    return envelope['signed']


class Trust:
    def __init__(self,state,root_file):
        self.state=state;current=state.get('trusted-root')
        if current:self.root=current
        else:
            import json
            try:envelope=json.loads(Path(root_file).read_bytes())
            except Exception:raise UpgradeError('UNTRUSTED','尚未安装可信发布根，请由运维配置验证后的根文件') from None
            root=envelope.get('signed',{});verify(envelope,root,'root')
            if root.get('_type')!='stockai-root/1' or root.get('version')!=1:raise UpgradeError('UNTRUSTED')
            self.root=root;state.put('trusted-root',root)

    def rotate(self,envelopes):
        for envelope in envelopes:
            new=verify(envelope,self.root,'root')
            if new.get('_type')!='stockai-root/1' or new.get('version')!=self.root['version']+1:raise UpgradeError('REPLAY')
            verify(envelope,new,'root');expires(new['expires']);self.root=new;self.state.put('trusted-root',new)
        return self.root

    def candidate(self,envelope,target,now=None):
        """Validate selection metadata; acceptance checks replay and downgrade."""
        try:return self._release(envelope,target,now,accept=False)
        except (KeyError,TypeError,AttributeError):raise UpgradeError('UNTRUSTED') from None

    def release(self,envelope,target,now=None):
        try:return self._release(envelope,target,now)
        except (KeyError,TypeError,AttributeError):raise UpgradeError('UNTRUSTED') from None

    def _release(self,envelope,target,now=None,*,accept=True):
        expires(self.root['expires'],now);release=verify(envelope,self.root,'releases')
        if release.get('_type')!='stockai-release/1':raise UpgradeError('UNTRUSTED')
        expires(release['expires'],now)
        for field in ('component','platform','architecture','form'):
            if release.get(field)!=target[field]:raise UpgradeError('TARGET')
        sequence=release.get('sequence')
        if type(sequence) is not int or sequence<1:raise UpgradeError('UNTRUSTED')
        if accept:
            slot='release-floor:'+release['component']+':'+release['channel'];floor=self.state.get(slot,{'sequence':0,'hash':None})
            if sequence<floor['sequence'] or (sequence==floor['sequence'] and floor['hash']!=digest(release)):raise UpgradeError('REPLAY')
        version=Version(release['version']);current=Version(target['version'])
        if accept and version<current:raise UpgradeError('DOWNGRADE')
        if Version(release['minimum_source'])>current or not compatible(release['accepts'],target['capabilities']):raise UpgradeError('COMPATIBILITY')
        if release['channel'] not in ('stable','preview') or (release['channel']=='stable' and version.pre):raise UpgradeError('TARGET')
        package=release.get('package',{})
        if type(package.get('size')) is not int or not 0<package['size']<=2*1024*1024*1024 or not isinstance(package.get('sha256'),str) or not re.fullmatch('[0-9a-f]{64}',package['sha256']):raise UpgradeError('PACKAGE')
        from urllib.parse import urlsplit
        url=urlsplit(package['url'])
        if url.scheme!='https' or not url.hostname or url.username or url.password or url.fragment:raise UpgradeError('TARGET')
        for field in ('schema','api','sync'):
            if type(release['provides'].get(field)) is not int or release['provides'][field]<1:raise UpgradeError('COMPATIBILITY')
        if not isinstance(release.get('rollback'),dict) or not isinstance(release.get('migration'),str):raise UpgradeError('COMPATIBILITY')
        if accept:self.state.put(slot,{'sequence':sequence,'hash':digest(release)})
        return release
