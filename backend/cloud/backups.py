from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from dataclasses import replace
from datetime import datetime,timedelta,timezone
from pathlib import Path,PurePosixPath

from .crypto import atomic_private
from .lifecycle import purge_user,directory_swap
from .store import Store,json_text,stamp


def file_hash(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f,"sha256").hexdigest()


def counts(connection):
    names=[r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    if any(not name.replace("_","").isalnum() for name in names):
        raise ValueError("数据库表名不合法")
    return {name:connection.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0] for name in sorted(names)}


def safe_file(root,name):
    part=PurePosixPath(name)
    if part.is_absolute() or ".." in part.parts or "\\" in name or ":" in name or not part.parts:
        raise ValueError("备份清单含有非法路径")
    path=(root/str(part)).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("备份路径越界")
    return path


def prune(store,journal):
    cutoff=datetime.now(timezone.utc)-timedelta(days=30)
    for row in journal.backups():
        path=store.cfg.data/"backups"/(row["id"]+".age")
        if datetime.fromisoformat(row["created_at"])<cutoff:
            path.unlink(missing_ok=True)
    # A process interrupted during encryption never leaves a downloadable plaintext file.


def create(store,keys,journal):
    if not store.cfg.age_recipient.startswith("age1"):
        raise ValueError("尚未配置 age 备份接收公钥")
    journal.verify();ident=uuid.uuid4().hex
    staging=store.cfg.data/".staging";staging.mkdir(exist_ok=True,mode=0o700)
    output=store.cfg.data/"backups";output.mkdir(exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix="backup-",dir=staging) as raw:
        root=Path(raw);snapshot=root/"stockai.sqlite3"
        with store.publish_lock:
            source=sqlite3.connect(store.path,timeout=15);dest=sqlite3.connect(snapshot)
            try:
                source.backup(dest);dest.execute("PRAGMA journal_mode=DELETE")
                tables=counts(dest);site=dest.execute("SELECT value FROM cloud_meta WHERE key='site_id'").fetchone()[0]
                files=dest.execute("SELECT markdown_path,html_path FROM personal_reports").fetchall()
            finally:
                dest.close();source.close()
            for pair in files:
                for name in pair:
                    normalized=Path(name).as_posix();src=safe_file(store.cfg.data,normalized);target=safe_file(root,normalized)
                    if src.is_symlink() or not src.is_file():
                        raise ValueError("归档报告文件缺失或不是普通文件")
                    target.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
                    try:
                        os.link(src,target)
                    except OSError:
                        shutil.copyfile(src,target)
        listed={p.relative_to(root).as_posix():file_hash(p) for p in root.rglob("*") if p.is_file()}
        manifest={"format":"stockai-cloud/1","id":ident,"site_id":site,"created_at":stamp(),"counts":tables,"files":listed,"erasure_seq_at_snapshot":len(journal.verify())}
        atomic_private(root/"manifest.json",json_text(manifest).encode())
        archive=root/"archive.zip"
        with zipfile.ZipFile(archive,"w",zipfile.ZIP_DEFLATED) as z:
            for name in [*listed,"manifest.json"]:
                z.write(safe_file(root,name),name)
        pending=output/(ident+".pending")
        try:
            r=subprocess.run([store.cfg.age_bin,"--encrypt","--recipient",store.cfg.age_recipient,"--output",str(pending),str(archive)],capture_output=True,timeout=300)
            if r.returncode:
                raise ValueError("age 加密失败；未发布备份")
            final=output/(ident+".age");pending.replace(final)
            record={"created_at":manifest["created_at"],"site_id":site,"sha256":file_hash(final),"manifest_sha256":file_hash(root/"manifest.json"),"size":final.stat().st_size,"key_version":keys.active}
            journal.register_backup(ident,record)
        finally:
            pending.unlink(missing_ok=True)
    prune(store,journal)
    return {"id":ident,**record}


def list_backups(store,journal):
    return [{**r,"available":(store.cfg.data/"backups"/(r["id"]+".age")).is_file()} for r in journal.backups()]


def registered(store,journal,ident):
    row=next((r for r in journal.backups() if r["id"]==ident),None)
    path=store.cfg.data/"backups"/(ident+".age")
    if not row or not path.is_file() or file_hash(path)!=row["sha256"]:
        raise ValueError("备份不存在或密文摘要不匹配")
    return path,row


def restore(store,keys,journal,ident,identity):
    """Offline only: caller must hold the exclusive service lock throughout."""
    erased=journal.verify();encrypted,record=registered(store,journal,ident)
    if not Path(identity).is_file():
        raise ValueError("需要本地 age 解密身份文件")
    restore_point=create(store,keys,journal)
    parent=store.cfg.data.parent
    with tempfile.TemporaryDirectory(prefix=".stockai-restore-"+store.site_id+"-",dir=parent) as raw:
        root=Path(raw);archive=root/"archive.zip";prepared=root/"prepared";prepared.mkdir(mode=0o700)
        r=subprocess.run([store.cfg.age_bin,"--decrypt","--identity",str(identity),"--output",str(archive),str(encrypted)],capture_output=True,timeout=300)
        if r.returncode:
            raise ValueError("age 解密失败，当前数据未被替换")
        with zipfile.ZipFile(archive) as z:
            info=z.infolist();names=[i.filename for i in info]
            if len(names)!=len(set(names)) or len(names)>100000 or sum(i.file_size for i in info)>2*1024**3:
                raise ValueError("备份清单重复或超出安全大小限制")
            for i in info:
                safe_file(prepared,i.filename)
                if i.is_dir() or (i.external_attr>>16)&0o170000==0o120000:
                    raise ValueError("备份只能包含清单内的普通文件")
            manifest_raw=z.read("manifest.json")
            if hashlib.sha256(manifest_raw).hexdigest()!=record["manifest_sha256"]:
                raise ValueError("清单摘要与受控登记不一致")
            manifest=json.loads(manifest_raw)
            if manifest.get("format")!="stockai-cloud/1" or manifest.get("site_id")!=store.site_id or manifest.get("id")!=ident:
                raise ValueError("备份格式、站点或标识不匹配")
            if set(names)!={"manifest.json",*manifest["files"]}:
                raise ValueError("备份文件与清单不一致")
            for name,expected in manifest["files"].items():
                if name!="stockai.sqlite3" and not name.startswith("reports/"):
                    raise ValueError("清单包含不允许恢复的文件")
                value=z.read(name)
                if hashlib.sha256(value).hexdigest()!=expected:
                    raise ValueError("备份文件校验和不匹配")
                atomic_private(safe_file(prepared,name),value)
        trial=Store(replace(store.cfg,data=prepared))
        with trial.tx() as c:
            if c.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or c.execute("PRAGMA foreign_key_check").fetchall() or counts(c)!=manifest["counts"]:
                raise ValueError("数据库完整性、外键或记录数量核验失败")
            if c.execute("SELECT value FROM cloud_meta WHERE key='site_id'").fetchone()[0]!=store.site_id:
                raise ValueError("数据库站点标识不匹配")
        # Never trust the old backup as the authority for erasures or live credentials.
        for row in erased:
            purge_user(trial,row["user_id"])
        with trial.tx(immediate=True) as c:
            for table in ("sessions","auth_tokens","mail_outbox","reauth_grants"):
                c.execute(f"DELETE FROM {table}")
            c.execute("UPDATE model_credentials SET envelope_json=NULL,key_hint='',revoked_at=?",(stamp(),))
            c.execute("UPDATE model_profiles SET needs_reconnect=1")
            c.execute("UPDATE personal_jobs SET status='cancelled',error_code='RESTORE_REQUIRES_REVIEW',finished_at=? WHERE status IN('queued','running')",(stamp(),))
            c.execute("UPDATE model_calls SET phase='uncertain',error_code='UNCERTAIN_TIMEOUT' WHERE phase IN('prepared','sent')")
            c.execute("UPDATE site_jobs SET status='failed',error_code='RESTORE_REQUIRES_REVIEW' WHERE status IN('queued','running')")
            for row in c.execute("SELECT * FROM user_settings").fetchall():
                p=json.loads(row["payload_json"]);p["automatic_ai"]=False
                c.execute("UPDATE user_settings SET payload_json=? WHERE user_id=?",(json_text(p),row["user_id"]))
            c.execute("UPDATE cloud_meta SET value='0' WHERE key='maintenance'")
            if c.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("注销清理后外键校验失败")
            referenced={r[k] for r in c.execute("SELECT markdown_path,html_path FROM personal_reports") for k in (0,1)}
        for p in (prepared/"reports").rglob("*") if (prepared/"reports").exists() else []:
            if p.is_file() and p.relative_to(prepared).as_posix() not in referenced:
                p.unlink()
        for name in referenced:
            if not safe_file(prepared,Path(name).as_posix()).is_file():
                raise ValueError("恢复后的报告文件关联不完整")
        # Retain only encrypted backups. The separate authority directory is never replaced.
        if (store.cfg.data/"backups").exists():
            shutil.copytree(store.cfg.data/"backups",prepared/"backups")
        directory_swap(store,keys,prepared)
    return {"restored":ident,"restore_point":restore_point["id"],"erased_users_applied":len(erased),"reconnect_required":True}
