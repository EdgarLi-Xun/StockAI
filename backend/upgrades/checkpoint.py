"""Consistent, age-encrypted business checkpoints; live control and keys are never rolled back."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
import zipfile
from contextlib import closing
from backend.cloud.crypto import atomic_private
from backend.processes import hidden_process
from backend.sync.protocol import canonical,digest
from .trust import UpgradeError


def file_hash(path):
    h=hashlib.sha256()
    with Path(path).open('rb') as source:
        for chunk in iter(lambda:source.read(1024*1024),b''):h.update(chunk)
    return h.hexdigest()


def database_health(path):
    path=Path(path)
    if not path.is_file() or path.stat().st_size==0:raise UpgradeError('RECOVERY','工作区数据库缺失，拒绝创建空白恢复点')
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as c:
        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok' or c.execute('PRAGMA foreign_key_check').fetchall():raise UpgradeError('RECOVERY','数据库完整性或关联检查失败')
        tables=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        return {name:c.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0] for name in tables}


class Checkpoints:
    def __init__(self,cfg,component='web',identity=None):
        self.cfg,self.component=cfg,component;self.root=cfg.erasures/'upgrades/checkpoints';self.root.mkdir(parents=True,exist_ok=True)
        self.identity=Path(identity) if identity else cfg.key_file.parent/'backup-identity.txt'
        self.db_name='hub.sqlite3' if component=='hub' else 'stockai.sqlite3'

    def materials(self):
        if not self.cfg.age_recipient or not self.identity.is_file() or not Path(self.cfg.age_bin).is_file() and not shutil.which(self.cfg.age_bin):raise UpgradeError('RECOVERY','服务器执行时需要可用的 age 工具、接收公钥及离线解密身份')
        if not self.cfg.key_file.is_file():raise UpgradeError('RECOVERY','主密钥缺失')

    def make(self,ident):
        self.materials();target=self.root/(ident+'.age')
        source_path=self.cfg.data/self.db_name
        database_health(source_path)
        with tempfile.TemporaryDirectory(prefix='checkpoint-',dir=self.root) as temporary:
            stage=Path(temporary);db=stage/self.db_name
            with closing(sqlite3.connect(source_path.as_uri()+'?mode=ro',uri=True)) as source,closing(sqlite3.connect(db)) as snapshot:source.backup(snapshot);snapshot.commit()
            counts=database_health(db);files={self.db_name:file_hash(db)}
            for folder in ('reports','exports'):
                base=self.cfg.data/folder
                if not base.exists():continue
                for path in base.rglob('*'):
                    if path.is_symlink():raise UpgradeError('RECOVERY','检查点不接受符号链接归档')
                    if not path.is_file():continue
                    resolved=path.resolve()
                    if not resolved.is_relative_to(self.cfg.data.resolve()):raise UpgradeError('RECOVERY')
                    relative=path.relative_to(self.cfg.data).as_posix();copy=stage/relative;copy.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,copy);files[relative]=file_hash(copy)
            manifest={'format':'stockai-upgrade-checkpoint/1','component':self.component,'database':self.db_name,'files':files,'counts':counts}
            (stage/'manifest.json').write_bytes(canonical(manifest));archive=stage/'checkpoint.zip'
            with zipfile.ZipFile(archive,'w',zipfile.ZIP_DEFLATED) as output:
                for name in ['manifest.json',*files]:output.write(stage/name,name)
            result=subprocess.run([self.cfg.age_bin,'--encrypt','--recipient',self.cfg.age_recipient,'--output',str(target),str(archive)],capture_output=True,timeout=300,**hidden_process())
            if result.returncode:raise UpgradeError('RECOVERY','加密检查点创建失败')
            # 真实解密与逐文件核验后才允许迁移，不仅检查备份文件存在。
            self.extract(target,stage/'verified')
        return {'id':ident,'sha256':file_hash(target),'counts':counts,'available':True}

    def extract(self,path,destination):
        self.materials();destination=Path(destination);destination.mkdir(parents=True,exist_ok=True)
        archive=destination/'decrypted.zip';result=subprocess.run([self.cfg.age_bin,'--decrypt','--identity',str(self.identity),'--output',str(archive),str(path)],capture_output=True,timeout=300,**hidden_process())
        if result.returncode:raise UpgradeError('RECOVERY','检查点无法用当前恢复材料解密')
        with zipfile.ZipFile(archive) as source:
            from .archive import validate_names
            validate_names(source.infolist())
            names=source.namelist()
            if len(names)!=len(set(names)) or len(names)>100000:raise UpgradeError('RECOVERY')
            for item in source.infolist():
                name=PurePosixPath(item.filename)
                if name.is_absolute() or '..' in name.parts or '\\' in item.filename or ':' in item.filename or item.file_size>2*1024*1024*1024 or ((item.external_attr>>16)&0o170000)==0o120000:raise UpgradeError('RECOVERY','检查点包含不安全路径')
            manifest=json.loads(source.read('manifest.json'))
            if manifest.get('format')!='stockai-upgrade-checkpoint/1' or manifest.get('component')!=self.component or set(names)!=set(manifest['files'])|{'manifest.json'}:raise UpgradeError('RECOVERY')
            if sum(v.file_size for v in source.infolist())>20*1024*1024*1024:raise UpgradeError('RECOVERY')
            source.extractall(destination)
        archive.unlink()
        for name,wanted in manifest['files'].items():
            if file_hash(destination/name)!=wanted:raise UpgradeError('RECOVERY','检查点清单摘要不匹配')
        if database_health(destination/self.db_name)!=manifest['counts']:raise UpgradeError('RECOVERY')
        return manifest

    def restore(self,checkpoint,after_restore=None):
        if not checkpoint.get('available'):raise UpgradeError('RECOVERY','检查点已过期或不可用，不能按此记录恢复')
        ident=checkpoint['id']
        if not ident.isalnum():raise UpgradeError('RECOVERY')
        encrypted=self.root/(ident+'.age')
        if file_hash(encrypted)!=checkpoint['sha256']:raise UpgradeError('RECOVERY')
        # 交换由既有签名切换记录保护。控制目录和主密钥不在待替换目录中。
        from backend.cloud.lifecycle import directory_swap
        from backend.cloud.crypto import Keyring
        keys=Keyring(self.cfg.key_file)
        if self.component=='hub':
            from types import SimpleNamespace
            target_store=SimpleNamespace(cfg=self.cfg,site_id=digest(str(self.cfg.data.resolve()).encode())[:24])
        else:
            from backend.cloud.store import Store
            target_store=Store(self.cfg)
        stage=self.cfg.data.parent/('.stockai-restore-'+target_store.site_id+'-'+uuid.uuid4().hex)/'prepared'
        manifest=self.extract(encrypted,stage);(stage/'manifest.json').unlink()
        if after_restore:after_restore(stage)
        database_health(stage/self.db_name)
        directory_swap(target_store,keys,stage)
        return manifest
