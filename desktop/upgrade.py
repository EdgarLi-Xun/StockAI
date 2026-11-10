"""Desktop handoff and early code selection, independent of Qt and the business server."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from backend.processes import hidden_process


def parent_released(pid,timeout_ms=120000):
    """True when the previous desktop process is gone. A live process we cannot wait on stays unreleased."""
    if not pid or os.name!='nt':return True
    import ctypes
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.OpenProcess.argtypes=[ctypes.c_ulong,ctypes.c_int,ctypes.c_ulong]
    kernel.WaitForSingleObject.argtypes=[ctypes.c_void_p,ctypes.c_ulong]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    parent=kernel.OpenProcess(0x100000,False,int(pid))
    if not parent:return ctypes.get_last_error()==87
    try:return kernel.WaitForSingleObject(parent,timeout_ms)==0
    finally:kernel.CloseHandle(parent)


def forward(home,arguments,client_only=False):
    if not (home/('client-state/secrets/keys.json' if client_only else 'secrets/keys.json')).is_file():return False
    from backend.upgrades.cli import manager
    from backend.upgrades.runner import Runner
    from backend.cloud import VERSION
    if client_only:
        from desktop.client_updates import manager as client_manager
        m=client_manager(home)
    else:m=manager('desktop',str(home))
    selected=m.state.get('active-installation')
    if not selected or selected['version']==VERSION:return False
    from backend.upgrades.install_path import program_path
    runner=Runner(m);runner.verify(selected);target=program_path(selected)
    same_exe=selected['form']=='exe' and getattr(sys,'frozen',False) and target.resolve()==Path(sys.executable).resolve()
    if same_exe and selected.get('layout')=='setup' and selected.get('transaction'):
        staging=runner.root/selected['transaction']/'app'
        if (staging/'StockAI.exe').is_file():
            from backend.upgrades.install_path import schedule_swap
            schedule_swap(os.getpid(),staging,arguments);return True
    if same_exe:return False
    command=[str(target)] if selected['form']=='exe' else [sys.executable,str(target/'stockai_desktop.py')]
    subprocess.Popen([*command,*arguments],**hidden_process());return True


def handoff(home,tx,client_only=False,remote_url='',remote_account=''):
    command=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve().parents[1]/'stockai_desktop.py')]
    extra=['--client-only','--remote-url',remote_url,'--remote-account',remote_account] if client_only else []
    subprocess.Popen([*command,'--upgrade-helper','--home',str(home),'--id',tx['id'],'--confirmation',tx['confirmation'],'--parent',str(os.getpid()),*extra],**hidden_process())


def helper(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--home',required=True);p.add_argument('--id',required=True);p.add_argument('--confirmation',required=True);p.add_argument('--parent',type=int)
    p.add_argument('--client-only',action='store_true');p.add_argument('--remote-url');p.add_argument('--remote-account');args=p.parse_args(argv)
    from backend.upgrades.cli import manager
    from backend.upgrades.runner import Runner
    from backend.cloud.lifecycle import ProcessLock,recover_swap
    if args.client_only:
        from desktop.client_updates import manager as client_manager
        m=client_manager(Path(args.home))
    else:m=manager('desktop',args.home)
    runner=Runner(m);lock=None
    if args.parent and not parent_released(args.parent):raise RuntimeError('旧客户端尚未退出，升级未开始')
    # 只等待当前工作区服务正常释放锁，不杀进程或替换被占用的 EXE。
    for _ in range(240):
        try:lock=ProcessLock(m.cfg.erasures/'service.lock');lock.__enter__();break
        except RuntimeError:time.sleep(.5)
    if not lock or lock.file is None:raise RuntimeError('本机服务尚未停止，升级没有开始')
    try:
        if not args.client_only:recover_swap(m.cfg,m.keys)
        if getattr(sys,'frozen',False) and not m.state.get('active-installation'):
            from backend.upgrades.install_path import install_exe
            source=Path(sys.executable).resolve()
            if source==install_exe().resolve() and source.is_file() and not source.is_symlink():
                m.state.put('active-installation',{'path':str(source),'version':m.version,'form':'exe','layout':'installed','transaction':'bootstrap'})
        try:tx=m.execute(args.id,args.confirmation,runner,actor='desktop-owner')
        except Exception as exc:
            tx=m.current()
            if not tx or tx.get('checkpoint') or tx.get('writes_opened'):raise
            tx=m.update(tx,'failed',error=getattr(exc,'code','PREFLIGHT_FAILED'),actual_version=m.version);m.record(tx);m.maintenance(False)
    finally:lock.__exit__()
    if tx['status']=='succeeded' or (tx['status'] in ('rolled_back','failed') and not tx.get('writes_opened',False)):
        selected=m.state.get('active-installation')
        mode=['--remote-url',args.remote_url,'--remote-account',args.remote_account or ''] if args.client_only else ['--local']
        if selected and selected.get('layout')=='setup' and tx['status']=='succeeded':
            from backend.upgrades.install_path import schedule_swap
            runner.verify(selected);schedule_swap(os.getpid(),runner.root/args.id/'app',['--data-dir',args.home,*mode])
        elif selected:
            from backend.upgrades.install_path import program_path
            runner.verify(selected);target=program_path(selected)
            command=[str(target)] if selected['form']=='exe' else [sys.executable,str(target/'stockai_desktop.py')]
            subprocess.Popen([*command,'--data-dir',args.home,*mode],**hidden_process())
        else:
            command=[sys.executable,str(Path(__file__).resolve().parents[1]/'stockai_desktop.py')]
            subprocess.Popen([*command,'--data-dir',args.home,*mode],**hidden_process())
