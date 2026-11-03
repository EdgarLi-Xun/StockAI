"""Keep bounded collector processes working in source and single-file desktop builds."""
import sys

MODULES={"backend.collector_worker","backend.cloud.collector"}


def module_command(module,*args):
    if module not in MODULES:raise ValueError("不支持的采集子进程")
    if getattr(sys,"frozen",False):return [sys.executable,"--module",module,*args]
    return [sys.executable,"-X","utf8","-m",module,*args]


def hidden_process():
    import subprocess
    return {"creationflags":subprocess.CREATE_NO_WINDOW} if sys.platform=="win32" else {}
