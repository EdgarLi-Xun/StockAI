"""Fixed local executor. Installs inactive code, then probes in a separate process."""
from __future__ import annotations
from dataclasses import asdict
import json
import os
from pathlib import Path,PurePosixPath
import shutil
import subprocess
import sys
import zipfile
from backend.cloud.crypto import atomic_private
from backend.processes import hidden_process
from backend.sync.protocol import canonical
from .checkpoint import file_hash
from .trust import UpgradeError


class Runner:
    def __init__(self,manager):self.manager=manager;self.root=manager.folder/'installations';self.root.mkdir(parents=True,exist_ok=True)

    def prepare(self,tx,package):
        folder=self.root/tx['id'];folder.mkdir(parents=True,exist_ok=True)
        current=self.manager.state.get('active-installation')
        self.manager.state.put('previous-installation:'+tx['id'],current)
        if self.manager.form=='exe':
            target=folder/'StockAI.exe';shutil.copy2(package,target)
            if file_hash(target)!=tx['release']['package']['sha256']:raise UpgradeError('PACKAGE')
            return {'path':str(target),'sha256':file_hash(target),'version':tx['target_version'],'form':'exe','transaction':tx['id']}
        stage=folder/'code';stage.mkdir(exist_ok=True)
        with zipfile.ZipFile(package) as source:
            from .archive import validate_names
            validate_names(source.infolist())
            names=source.namelist()
            if len(names)!=len(set(names)) or len(names)>60000:raise UpgradeError('PACKAGE')
            total=0
            forbidden={'data','cloud-data','cloud-secrets','cloud-erasures','hub-data','hub-control','.git','.venv','.aws','.codex','.agents','node_modules'}
            for item in source.infolist():
                path=PurePosixPath(item.filename);total+=item.file_size
                if path.is_absolute() or '..' in path.parts or '\\' in item.filename or ':' in item.filename or not path.parts or set(path.parts)&forbidden or item.file_size>1024*1024*1024 or ((item.external_attr>>16)&0o170000)==0o120000 or total>6*1024*1024*1024:raise UpgradeError('PACKAGE','升级包包含不安全路径或超量解包内容')
            if not {'backend/upgrades/probe.py','backend/cloud/__init__.py'}<=set(names):raise UpgradeError('PACKAGE','源码包缺少组件探针或版本声明')
            source.extractall(stage)
        # 保存整个代码树的摘要；后续启动不会信任已被替换的缓存文件。
        files={p.relative_to(stage).as_posix():file_hash(p) for p in stage.rglob('*') if p.is_file()}
        return {'path':str(stage),'sha256':tx['release']['package']['sha256'],'files':files,'version':tx['target_version'],'form':'source','transaction':tx['id']}

    def probe(self,tx,installation):
        folder=self.root/tx['id'];config=folder/'probe-config.json';result=folder/'probe-result.json'
        cfg=self.manager.cfg
        value={'component':self.manager.component,'data':str(cfg.data),'erasures':str(cfg.erasures),'key_file':str(cfg.key_file),'public_url':cfg.public_url,'dev':cfg.dev,'expected_version':tx['target_version'],'baseline':tx.get('checkpoint',{}).get('counts',{}),'client_only':getattr(self.manager,'client_only',False)}
        atomic_private(config,canonical(value));result.unlink(missing_ok=True)
        if installation['form']=='exe':command=[installation['path'],'--upgrade-probe',str(config),'--result',str(result)];cwd=folder
        else:
            command=[sys.executable,'-m','backend.upgrades.probe',str(config),'--result',str(result)];cwd=Path(installation['path'])
        environment={k:v for k,v in os.environ.items() if k not in ('PYTHONPATH','PYTHONHOME')};environment.update(PYTHONUTF8='1',PYTHONDONTWRITEBYTECODE='1',STOCKAI_CLOUD_NO_BACKGROUND='1')
        completed=subprocess.run(command,cwd=cwd,env=environment,capture_output=True,timeout=180,**hidden_process())
        if completed.returncode or not result.is_file():raise UpgradeError('HEALTH','新程序的独立健康检查没有通过')
        return json.loads(result.read_bytes())

    def activate(self,tx,installation):
        self.verify(installation);self.manager.state.put('active-installation',installation)

    def stop_new(self,tx):pass  # 探针进程同步退出，正式业务后台从未在预检阶段启动。

    def rollback(self,tx):
        previous=self.manager.state.get('previous-installation:'+tx['id'])
        if previous:self.verify(previous)
        self.manager.state.put('active-installation',previous)

    def verify(self,value):
        path=Path(value['path']).resolve()
        if not path.is_relative_to(self.root.resolve()) or path.is_symlink():raise UpgradeError('PACKAGE')
        if value['form']=='exe':
            if not path.is_file() or file_hash(path)!=value['sha256']:raise UpgradeError('PACKAGE')
        else:
            for name,expected in value['files'].items():
                file=(path/name).resolve()
                if not file.is_relative_to(path) or file.is_symlink() or not file.is_file() or file_hash(file)!=expected:raise UpgradeError('PACKAGE')
        return path
