from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .store import json_text


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def atomic_private(path:Path,value:bytes):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temp=path.with_name(path.name+".tmp")
    fd=os.open(temp,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,"wb") as f:
        f.write(value); f.flush(); os.fsync(f.fileno())
    temp.replace(path)


class Keyring:
    def __init__(self,path:Path):
        self.path=path
        if not path.is_file():
            raise RuntimeError("尚未初始化凭据主密钥，请使用 cloud CLI init")
        raw=json.loads(path.read_text(encoding="utf-8"))
        self.active=int(raw["active"])
        self.keys={int(k):base64.b64decode(v,validate=True) for k,v in raw["keys"].items()}
        if self.active not in self.keys or any(len(v)!=32 for v in self.keys.values()):
            raise RuntimeError("主密钥文件无效")

    @classmethod
    def create(cls,path):
        if path.exists():
            raise ValueError("主密钥已存在，不覆盖")
        atomic_private(path,json_text({"active":1,"keys":{"1":base64.b64encode(os.urandom(32)).decode()}}).encode())
        return cls(path)

    def seal(self,value:str,*,site:str,user:str,resource:str,purpose:str):
        version=self.active
        aad=json_text([site,user,resource,purpose,version]).encode()
        nonce=os.urandom(12)
        cipher=AESGCM(self.keys[version]).encrypt(nonce,value.encode(),aad)
        return json_text({"version":version,"nonce":base64.b64encode(nonce).decode(),"cipher":base64.b64encode(cipher).decode()})

    def open(self,envelope:str,*,site:str,user:str,resource:str,purpose:str):
        data=json.loads(envelope)
        version=int(data["version"])
        aad=json_text([site,user,resource,purpose,version]).encode()
        return AESGCM(self.keys[version]).decrypt(base64.b64decode(data["nonce"]),base64.b64decode(data["cipher"]),aad).decode()

    def sign(self,value,version=None):
        version=version or self.active
        return hmac.new(self.keys[version],b"stockai-control\0"+value.encode(),hashlib.sha256).hexdigest()

    def rotate(self):
        version=max(self.keys)+1
        self.keys[version]=os.urandom(32)
        self.active=version
        atomic_private(self.path,json_text({"active":version,"keys":{str(k):base64.b64encode(v).decode() for k,v in self.keys.items()}}).encode())
        return version
