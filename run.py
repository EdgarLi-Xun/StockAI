"""One shared server, two client entry points. Run with --help for options."""
from __future__ import annotations

import argparse
import atexit
import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser

from backend.config import DATA, ROOT

URL="http://127.0.0.1:8765"


def healthy():
    try:
        with urllib.request.urlopen(URL+"/api/health",timeout=1) as r:
            return json.load(r).get("mode")=="simulation-only"
    except Exception:
        return False


def ensure_server():
    if healthy():
        return
    DATA.mkdir(parents=True,exist_ok=True)
    log=(DATA/"server.log").open("a",encoding="utf-8")
    subprocess.Popen([sys.executable,str(ROOT/"run.py"),"--serve"],cwd=ROOT,stdout=log,stderr=log,stdin=subprocess.DEVNULL,creationflags=subprocess.CREATE_NO_WINDOW if sys.platform=="win32" else 0)
    log.close()
    for _ in range(60):
        if healthy():
            return
        time.sleep(0.5)
    raise RuntimeError("本地服务启动失败，请查看 data/server.log；8765端口可能被其他程序占用。")


def main():
    parser=argparse.ArgumentParser(description="StockAI 本地股票与贵金属研究终端")
    parser.add_argument("--serve",action="store_true",help="仅启动共享后端（前台）")
    parser.add_argument("--desktop",action="store_true",help="打开 Windows 桌面窗口")
    parser.add_argument("--smoke-test",action="store_true",help=argparse.SUPPRESS)
    args=parser.parse_args()
    if args.serve:
        from backend import db, security
        import uvicorn
        db.initialize()
        instance_lock=(DATA/"server.lock").open("a+b")
        instance_lock.seek(0)
        if not instance_lock.read(1):
            instance_lock.write(b"0")
            instance_lock.flush()
        instance_lock.seek(0)
        try:
            if sys.platform=="win32":
                import msvcrt
                msvcrt.locking(instance_lock.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(instance_lock.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            print("StockAI 共享服务已经运行。")
            instance_lock.close()
            return
        cfg=db.settings()
        if cfg.lan_enabled and not security.lan_ready():
            raise RuntimeError("局域网访问需要先配置管理员密码")
        host="0.0.0.0" if cfg.lan_enabled else "127.0.0.1"
        os.environ["STOCKAI_BIND_HOST"]=host
        pidfile=DATA/"server.pid"
        pidfile.write_text(str(os.getpid()),encoding="ascii")
        def cleanup():
            if pidfile.exists() and pidfile.read_text().strip()==str(os.getpid()):
                pidfile.unlink()
        atexit.register(cleanup)
        uvicorn.run("backend.app:app",host=host,port=8765,workers=1,proxy_headers=False,log_level="info")
    elif args.desktop:
        from desktop.main import launch
        launch(args.smoke_test)
    else:
        ensure_server()
        webbrowser.open(URL)
        print("StockAI 已启动："+URL)


if __name__=="__main__":
    main()
