"""Durable erasure authority, deliberately stored outside the backup/restore tree."""
from __future__ import annotations

import hmac
import json
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from contextlib import contextmanager

from .auth import AuthError,password_matches
from .crypto import atomic_private
from .store import json_text,stamp


class ProcessLock:
    def __init__(self,path):
        self.path=path;self.file=None

    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open("a+b")
        self.file.seek(0);self.file.write(b"0");self.file.flush();self.file.seek(0)
        try:
            if __import__("os").name=="nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            self.file.close();self.file=None
            raise RuntimeError("另一个服务或离线维护进程仍在使用此数据目录") from None
        return self

    def __exit__(self,*args):
        if self.file:
            self.file.seek(0)
            if __import__("os").name=="nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            self.file.close();self.file=None


class ErasureJournal:
    def __init__(self,store,keys):
        self.store,self.keys=store,keys
        self.path=store.cfg.erasures/"authority.sqlite3"
        self.head=store.cfg.erasures/"erasure-head.json"
        self.lock=threading.RLock()

    @contextmanager
    def tx(self):
        c=sqlite3.connect(self.path,timeout=15);c.row_factory=sqlite3.Row
        c.execute("PRAGMA synchronous=FULL");c.execute("BEGIN IMMEDIATE")
        try:
            yield c;c.commit()
        except BaseException:
            c.rollback();raise
        finally:
            c.close()

    def create(self):
        if self.path.exists() or self.head.exists():
            raise RuntimeError("注销权威记录已存在，不能重建或覆盖")
        self.path.parent.mkdir(parents=True,mode=0o700,exist_ok=True)
        with self.tx() as c:
            c.execute("CREATE TABLE events(seq INTEGER PRIMARY KEY,user_id TEXT NOT NULL UNIQUE,ts TEXT NOT NULL,previous TEXT NOT NULL,signature TEXT NOT NULL,key_version INTEGER NOT NULL)")
            c.execute("CREATE TABLE registry(id TEXT PRIMARY KEY,payload_json TEXT NOT NULL,signature TEXT NOT NULL,key_version INTEGER NOT NULL)")
        self._head(0,"")

    def _head(self,seq,chain):
        value={"site_id":self.store.site_id,"seq":seq,"chain":chain,"key_version":self.keys.active}
        value["signature"]=self.keys.sign(json_text(value))
        atomic_private(self.head,json_text(value).encode())

    def verify(self):
        if not self.path.is_file() or not self.head.is_file():
            raise RuntimeError("独立注销权威记录缺失；拒绝启动或恢复，不能从旧备份重建")
        head=json.loads(self.head.read_text(encoding="utf-8"));signature=head.pop("signature")
        if head["site_id"]!=self.store.site_id or not hmac.compare_digest(signature,self.keys.sign(json_text(head),head["key_version"])):
            raise RuntimeError("注销记录的站点或签名校验失败")
        previous="";seq=0;anchor=head["seq"]==0 and head["chain"]==""
        with self.tx() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM events ORDER BY seq")]
        for r in rows:
            seq+=1
            payload=[self.store.site_id,r["seq"],r["user_id"],r["ts"],r["previous"]]
            if r["seq"]!=seq or r["previous"]!=previous or not hmac.compare_digest(r["signature"],self.keys.sign(json_text(payload),r["key_version"])):
                raise RuntimeError("注销记录不连续或签名无效")
            previous=r["signature"]
            if seq==head["seq"] and previous==head["chain"]:
                anchor=True
        if seq<head["seq"] or not anchor:
            raise RuntimeError("注销记录被回退或缺失，拒绝恢复")
        if seq>head["seq"]:
            self._head(seq,previous)  # Recovery after fsync journal / before atomic head publication.
        return rows

    def append(self,uid):
        with self.lock:
            rows=self.verify()
            if any(r["user_id"]==uid for r in rows):
                return
            seq=len(rows)+1;previous=rows[-1]["signature"] if rows else "";ts=stamp()
            signature=self.keys.sign(json_text([self.store.site_id,seq,uid,ts,previous]))
            with self.tx() as c:
                c.execute("INSERT INTO events VALUES(?,?,?,?,?,?)",(seq,uid,ts,previous,signature,self.keys.active))
            self._head(seq,signature)

    def register_backup(self,ident,payload):
        encoded=json_text(payload)
        with self.lock,self.tx() as c:
            c.execute("INSERT INTO registry VALUES(?,?,?,?)",(ident,encoded,self.keys.sign(encoded),self.keys.active))

    def backups(self):
        with self.tx() as c:
            rows=[dict(r) for r in c.execute("SELECT * FROM registry")]
        out=[]
        for row in rows:
            if not hmac.compare_digest(row["signature"],self.keys.sign(row["payload_json"],row["key_version"])):
                raise RuntimeError("备份注册信息签名无效")
            out.append({"id":row["id"],**json.loads(row["payload_json"])})
        return sorted(out,key=lambda r:r["created_at"],reverse=True)


def purge_user(store,uid):
    """Caller holds publication + user locks; safe to repeat after a process crash."""
    with store.tx(immediate=True) as c:
        c.execute("DELETE FROM invitations WHERE created_by=?",(uid,))
        c.execute("UPDATE invitations SET used_by=NULL,revoked_at=COALESCE(revoked_at,?) WHERE used_by=?",(stamp(),uid))
        # Break the profile version/credential cycle before the user cascade.
        c.execute("UPDATE model_profile_versions SET credential_id=NULL WHERE profile_id IN(SELECT id FROM model_profiles WHERE user_id=?)",(uid,))
        c.execute("DELETE FROM users WHERE id=?",(uid,))
    for root in ("reports","exports"):
        path=(store.cfg.data/root/uid).resolve()
        if not path.is_relative_to((store.cfg.data/root).resolve()):
            raise RuntimeError("非法的个人目录")
        if path.exists():
            shutil.rmtree(path)


def reconcile(store,journal):
    rows=journal.verify()
    with store.publish_lock:
        for row in rows:
            with store.user_lock(row["user_id"]):
                purge_user(store,row["user_id"])
    return len(rows)


def erase(store,journal,actor,password,confirmation):
    if confirmation!="注销并删除我的全部个人数据":
        raise AuthError("CONFIRMATION_REQUIRED","请准确输入注销确认语句")
    row=store.one("SELECT password_hash FROM users WHERE id=?",(actor.id,))
    if not row or not password_matches(row["password_hash"],str(password)):
        raise AuthError("REAUTH_FAILED","密码验证未通过",401)
    # The authoritative tombstone is committed before sessions/private rows/files disappear.
    with store.publish_lock,store.user_lock(actor.id):
        journal.append(actor.id)
        purge_user(store,actor.id)
    return {"erased":True,"message":"个人数据及可用模型凭据已删除。历史加密备份最多保留30天；恢复时将先执行独立注销记录。"}


def recover_swap(cfg,keys):
    """Resolve an interrupted offline directory switch before opening/initializing the DB."""
    marker=cfg.erasures/"pending-swap.json"
    if not marker.exists():
        return
    value=json.loads(marker.read_text(encoding="utf-8"));signature=value.pop("signature")
    if not hmac.compare_digest(signature,keys.sign(json_text(value),value["key_version"])):
        raise RuntimeError("离线切换记录签名无效，拒绝启动")
    target=Path(value["target"]);old=Path(value["old"]);prepared=Path(value["prepared"])
    if target.resolve()!=cfg.data.resolve() or old.parent.resolve()!=target.parent.resolve() or not old.name.startswith(".stockai-replaced-"+value["site_id"]+"-") or prepared.name!="prepared" or prepared.parent.parent.resolve()!=target.parent.resolve() or not prepared.parent.name.startswith((".stockai-restore-"+value["site_id"]+"-",".stockai-migrate-"+value["site_id"]+"-")):
        raise RuntimeError("离线切换路径校验失败")
    if not target.exists():
        if old.exists():
            old.rename(target)  # First rename committed; restore the previous live generation.
        else:
            raise RuntimeError("当前和回滚目录均缺失，需要人工恢复")
    elif old.exists():
        if prepared.exists():
            raise RuntimeError("离线切换目录状态不明确，拒绝清理")
        shutil.rmtree(old)  # The verified new generation was already published.
    marker.unlink()


def directory_swap(store,keys,prepared):
    target=store.cfg.data;old=target.parent/(".stockai-replaced-"+store.site_id+"-"+uuid.uuid4().hex)
    value={"target":str(target.resolve()),"old":str(old.resolve()),"prepared":str(prepared.resolve()),"site_id":store.site_id,"key_version":keys.active}
    value["signature"]=keys.sign(json_text(value));marker=store.cfg.erasures/"pending-swap.json"
    atomic_private(marker,json_text(value).encode())
    target.rename(old)
    try:
        prepared.rename(target)
    except BaseException:
        old.rename(target);marker.unlink(missing_ok=True);raise
    shutil.rmtree(old);marker.unlink()


def clean_staging(store):
    targets=[store.cfg.data/".staging"]
    for prefix in (".stockai-restore-",".stockai-migrate-"):
        targets.extend(store.cfg.data.parent.glob(prefix+store.site_id+"-*"))
    for path in targets:
        resolved=path.resolve()
        if resolved!=path.absolute() or not resolved.is_relative_to(store.cfg.data.parent.resolve()) or not path.is_dir():
            continue
        shutil.rmtree(path)
