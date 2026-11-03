"""Offline operator entry: fixed local plans and code, never browser-supplied commands."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from backend.cloud.config import CloudConfig
from backend.cloud.crypto import Keyring
from backend.cloud.lifecycle import ProcessLock,recover_swap
from backend.cloud.store import Store
from backend.cloud import VERSION
from .manager import Manager
from .runner import Runner


def manager(component='web',home=None,identity=None):
    from backend.config import ROOT
    if component=='desktop':
        from desktop.host import local_config,workspace_path
        cfg=local_config(workspace_path(home),8765,False)
    elif component=='hub':
        from backend.hub.app import Config
        h=Config.environment();cfg=CloudConfig(data=h.data,erasures=h.control,key_file=Path(os.environ.get('STOCKAI_HUB_UPGRADE_KEY',str(h.control/'upgrade-keys.json'))),public_url=os.environ.get('STOCKAI_HUB_PUBLIC_URL','https://hub.example.com'),age_bin=os.environ.get('STOCKAI_AGE_BIN','age'),age_recipient=os.environ.get('STOCKAI_BACKUP_RECIPIENT',''),dev=h.dev,background=False)
        if not cfg.key_file.exists():Keyring.create(cfg.key_file)
    else:cfg=CloudConfig.from_env()
    keys=Keyring(cfg.key_file)
    return Manager(cfg,keys,component=component,version=VERSION,form='exe' if component=='desktop' and getattr(sys,'frozen',False) else 'source',store=None if component=='hub' else Store(cfg),root_file=os.environ.get('STOCKAI_RELEASE_ROOT',str(ROOT/'config/release-root.json')),source=os.environ.get('STOCKAI_RELEASE_SOURCE',''),identity=identity)


def main(argv=None):
    p=argparse.ArgumentParser(description='StockAI 受控升级/恢复，执行前需停止对应服务')
    p.add_argument('action',choices=['status','plan','download','preflight','execute','recover','rollback','cleanup','serve']);p.add_argument('--component',choices=['desktop','web','hub'],default='web');p.add_argument('--home');p.add_argument('--id');p.add_argument('--manifest');p.add_argument('--confirmation');p.add_argument('--identity');p.add_argument('--port',type=int,default=8765);p.add_argument('--apply',action='store_true',help='实际清理已过期且归属确定的升级缓存；默认仅预览')
    args=p.parse_args(argv);m=manager(args.component,args.home,args.identity);runner=Runner(m)
    selected=m.state.get('active-installation')
    if args.action!='serve' and selected and selected['form']=='source' and selected['version']!=VERSION:
        cwd=runner.verify(selected)
        raise SystemExit(subprocess.call([sys.executable,'-m','backend.upgrades.cli',*(argv if argv is not None else sys.argv[1:])],cwd=cwd))
    if args.action=='serve':
        selected=m.state.get('active-installation');cwd=runner.verify(selected) if selected else Path(__file__).resolve().parents[2]
        if selected and selected['form']!='source':p.error('服务器需要源码形态')
        module='backend.hub.app:app' if args.component=='hub' else 'backend.public_app:app'
        raise SystemExit(subprocess.call([sys.executable,'-m','uvicorn',module,'--host','0.0.0.0','--port',str(args.port)],cwd=cwd))
    if args.action=='status':result=m.status()
    elif args.action=='cleanup':result=m.cleanup(execute=args.apply)
    elif args.action=='plan':result=m.prepare(json.loads(Path(args.manifest).read_bytes()))
    elif args.action=='download':result=m.download(args.id)
    elif args.action=='preflight':result=m.preflight(args.id,executor=True)
    else:
        with ProcessLock(m.cfg.erasures/'service.lock'):
            recover_swap(m.cfg,m.keys)
            if args.action=='execute':result=m.execute(args.id,args.confirmation,runner)
            elif args.action=='recover':result=m.recover(runner)
            else:result=m.rollback(args.id,runner,args.confirmation)
    print(json.dumps(result,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
