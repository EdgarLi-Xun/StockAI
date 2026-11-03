"""One persistent workspace and one loopback service for desktop and browser clients."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.request


def workspace_path(explicit=None):
    if explicit:return Path(explicit).resolve()
    base=Path(os.environ.get("LOCALAPPDATA",Path.home()/"AppData/Local"))
    return base/"StockAI"/"workspace"


def configure_home(home):
    home.mkdir(parents=True,exist_ok=True)
    os.environ["STOCKAI_DATA_DIR"]=str(home/"data")
    os.environ["PYTHONUTF8"]="1"


def instance_id(home):
    return hashlib.sha256(os.path.normcase(str(home.resolve())).encode()).hexdigest()[:24]


def health(port):
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f"http://127.0.0.1:{port}/api/health",timeout=.5) as response:return json.load(response)
    except Exception:return None


def choose_port(preferred=8765):
    for port in range(preferred,min(preferred+30,65536)):
        try:
            with socket.socket() as s:
                if sys.platform=="win32":s.setsockopt(socket.SOL_SOCKET,socket.SO_EXCLUSIVEADDRUSE,1)
                s.bind(("127.0.0.1",port))
            return port
        except OSError:continue
    raise RuntimeError("本机服务端口均被占用，请关闭不使用的 StockAI 实例后重试")


def local_config(home,port,background=True):
    from backend.config import ROOT
    from backend.cloud.config import CloudConfig
    tools=ROOT/"tools"
    if not tools.exists():tools=ROOT/".tmp/tools/age"
    identity=home/"secrets/backup-identity.txt"
    recipient=""
    if identity.is_file():
        recipient=next((line.split(":",1)[1].strip() for line in identity.read_text().splitlines() if line.startswith("# public key:")),"")
    return CloudConfig(data=home/"data",erasures=home/"control",key_file=home/"secrets/keys.json",public_url=f"http://127.0.0.1:{port}",dev=True,desktop_mode=True,desktop_instance=instance_id(home),background=background,ollama_enabled=True,age_bin=str(tools/"age.exe"),age_recipient=recipient)


def prepare(home):
    """Never reconstruct missing authority for an already prepared workspace."""
    from backend.cloud.crypto import Keyring,atomic_private
    from backend.cloud.lifecycle import ErasureJournal,ProcessLock
    from backend.cloud.store import Store
    cfg=local_config(home,8765,False)
    with ProcessLock(home/"control/service.lock"):
        database=cfg.data/"stockai.sqlite3"
        if not cfg.key_file.exists():
            if database.exists():raise RuntimeError("本机主密钥缺失，请恢复完整工作区，不能重建密钥")
            Keyring.create(cfg.key_file)
        keys=Keyring(cfg.key_file);store=Store(cfg).initialize();journal=ErasureJournal(store,keys)
        if (home/'control/upgrades/history.jsonl').exists():
            from backend.upgrades.manager import Manager
            from backend.upgrades.runner import Runner
            from backend.cloud import VERSION
            manager=Manager(cfg,keys,component='desktop',version=VERSION,form='exe' if getattr(sys,'frozen',False) else 'source',store=store)
            manager.recover(Runner(manager))
        if not journal.path.exists() and not journal.head.exists():
            if store.one("SELECT value FROM cloud_meta WHERE key='desktop_prepared'") or store.one("SELECT id FROM users LIMIT 1"):
                raise RuntimeError("本机删除权威记录缺失，请恢复完整工作区")
            journal.create()
        journal.verify()
        store.execute("INSERT OR IGNORE INTO cloud_meta VALUES('desktop_prepared','1')")
        identity=home/"secrets/backup-identity.txt"
        keygen=Path(cfg.age_bin).with_name("age-keygen.exe")
        # 第一次生成本机离线备份身份；旧身份缺失时不伪造为同一份密钥。
        marker=home/"control/backup-identity-created"
        if not identity.exists() and not marker.exists() and keygen.is_file():
            flags={"creationflags":subprocess.CREATE_NO_WINDOW} if sys.platform=="win32" else {}
            result=subprocess.run([str(keygen)],capture_output=True,timeout=15,**flags)
            if result.returncode:raise RuntimeError("本机备份身份生成失败")
            atomic_private(identity,result.stdout);atomic_private(marker,b"1")


class Host:
    def __init__(self,home,preferred=8765,background=True):
        self.home=home;self.port=choose_port(preferred);self.url=f"http://127.0.0.1:{self.port}"
        self.background=background;self.server=None;self.thread=None;self.error=None

    def start(self):
        import uvicorn
        from backend.public_app import create_app
        prepare(self.home)
        cfg=local_config(self.home,self.port,self.background)
        self.server=uvicorn.Server(uvicorn.Config(create_app(cfg),host="127.0.0.1",port=self.port,workers=1,proxy_headers=False,loop="asyncio",http="h11",ws="none",lifespan="on",access_log=False,log_level="warning",log_config=None))
        def run():
            try:self.server.run()
            except BaseException as exc:self.error=exc
        self.thread=threading.Thread(target=run,name="stockai-local-service",daemon=True);self.thread.start()
        return self

    def ready(self):
        value=health(self.port)
        return bool(value and value.get("edition")=="desktop-local" and value.get("instance")==instance_id(self.home))

    def wait(self,seconds=45,process_events=None):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            if self.error:raise RuntimeError("本机服务启动失败，请查看工作区的 desktop.log") from self.error
            if self.ready():return
            if process_events:process_events()
            time.sleep(.05)
        raise RuntimeError("本机服务启动超时，请查看工作区的 desktop.log")

    def close(self):
        if self.server:self.server.should_exit=True
        if self.thread:self.thread.join(timeout=8)
