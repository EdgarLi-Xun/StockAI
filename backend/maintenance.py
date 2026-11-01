from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import threading
import uuid
from pathlib import Path
from zipfile import ZipFile, ZIP_DEFLATED

from . import db
from .config import DATA, VERSION, timestamp

LOCK = threading.RLock()


def inspect_database(path):
    c=sqlite3.connect(path)
    try:
        if c.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or c.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("备份数据库完整性或关联关系校验失败")
        return {table:c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("instruments","reports","candidates","orders","accounts","backtest_runs")}
    finally:
        c.close()


def create_backup():
    if not LOCK.acquire(blocking=False):
        raise ValueError("研究任务运行中，请待任务完成后备份")
    try:
        folder=DATA/"backups"
        folder.mkdir(parents=True,exist_ok=True)
        ident=uuid.uuid4().hex
        snapshot=folder/(ident+".sqlite3")
        with db.connection() as source:
            target=sqlite3.connect(snapshot)
            source.backup(target)
            target.close()
        counts=inspect_database(snapshot)
        files={"stockai.sqlite3":snapshot.read_bytes()}
        c=sqlite3.connect(snapshot)
        try:
            for md,ht in c.execute("SELECT markdown_path,html_path FROM reports"):
                for name in (md,ht):
                    path=DATA/"reports"/name
                    if not path.is_file() or path.resolve().parent!=(DATA/"reports").resolve():
                        raise ValueError("报告归档缺失，备份已中止")
                    files["reports/"+name]=path.read_bytes()
        finally:
            c.close()
        manifest={"version":VERSION,"created_at":timestamp(),"counts":counts,"sha256":{name:hashlib.sha256(raw).hexdigest() for name,raw in files.items()}}
        temp=folder/(ident+".zip.tmp")
        with ZipFile(temp,"w",ZIP_DEFLATED) as z:
            for name,raw in files.items():
                z.writestr(name,raw)
            z.writestr("manifest.json",db.dumps(manifest))
        temp.replace(folder/(ident+".zip"))
        snapshot.unlink()
        return {"id":ident,**manifest,"size":(folder/(ident+".zip")).stat().st_size}
    finally:
        LOCK.release()


def backup_path(ident):
    if len(ident)!=32 or any(c not in "0123456789abcdef" for c in ident):
        raise ValueError("备份标识不合法")
    path=DATA/"backups"/(ident+".zip")
    if not path.is_file():
        raise ValueError("备份不存在")
    return path


def list_backups():
    result=[]
    for path in sorted((DATA/"backups").glob("*.zip"),reverse=True):
        try:
            with ZipFile(path) as z:
                m=json.loads(z.read("manifest.json"))
            result.append({"id":path.stem,"size":path.stat().st_size,**m})
        except Exception:
            result.append({"id":path.stem,"error":"备份清单无法读取"})
    return sorted(result,key=lambda x:x.get("created_at",""),reverse=True)


def restore_backup(ident, confirmation):
    if confirmation!="恢复备份":
        raise ValueError("请键入“恢复备份”确认替换本地数据")
    if not LOCK.acquire(blocking=False):
        raise ValueError("任务正在运行，暂时不能恢复")
    try:
        archive=backup_path(ident)
        with ZipFile(archive) as z:
            if sum(i.file_size for i in z.infolist())>500*1024*1024:
                raise ValueError("备份超过安全解压大小")
            manifest=json.loads(z.read("manifest.json"))
            files={name:z.read(name) for name in manifest["sha256"]}
        for name,raw in files.items():
            p=Path(name)
            if p.is_absolute() or ".." in p.parts or (name!="stockai.sqlite3" and (len(p.parts)!=2 or p.parts[0]!="reports" or p.suffix not in {".md",".html"})):
                raise ValueError("备份文件路径不安全")
            if hashlib.sha256(raw).hexdigest()!=manifest["sha256"][name]:
                raise ValueError("备份校验和不匹配")
        candidate=DATA/("restore-"+uuid.uuid4().hex+".sqlite3")
        candidate.write_bytes(files["stockai.sqlite3"])
        counts=inspect_database(candidate)
        if counts!=manifest["counts"]:
            candidate.unlink()
            raise ValueError("备份记录数量不匹配")
        # Always make a recovery point of current data before replacing it.
        safety=create_backup()
        folder=DATA/"reports"
        folder.mkdir(parents=True,exist_ok=True)
        for name,raw in files.items():
            if name.startswith("reports/"):
                p=folder/Path(name).name
                temp=p.with_suffix(p.suffix+".restore")
                temp.write_bytes(raw)
                temp.replace(p)
        # SQLite backup API replaces pages transactionally and handles active WAL correctly.
        source=sqlite3.connect(candidate)
        target=sqlite3.connect(DATA/"stockai.sqlite3",timeout=30)
        try:
            source.backup(target)
        finally:
            source.close()
            target.close()
            candidate.unlink()
        db.notify("备份恢复完成，数量与关联校验通过；已保留恢复前的安全备份。")
        from . import security
        with security.TOKEN_LOCK:
            security.TOKENS.clear()
        return {"restored":True,"counts":counts,"safety_backup":safety["id"]}
    finally:
        LOCK.release()
