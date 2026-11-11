"""Native desktop window and tray; both clients use the same local HTTP service."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import runpy
import sys
import time
import traceback


def worker_stdio():
    """A windowed frozen child still needs its parent-provided JSON pipe handles."""
    if os.name=="nt":
        import ctypes,msvcrt
        kernel=ctypes.WinDLL('kernel32',use_last_error=True)
        kernel.GetStdHandle.argtypes=[ctypes.c_int];kernel.GetStdHandle.restype=ctypes.c_void_p
        for name,code,flag in [('stdout',-11,os.O_WRONLY),('stderr',-12,os.O_WRONLY)]:
            if getattr(sys,name) is None:
                handle=kernel.GetStdHandle(code)
                if handle and handle!=ctypes.c_void_p(-1).value:
                    try:setattr(sys,name,os.fdopen(msvcrt.open_osfhandle(handle,flag|os.O_BINARY),'w',encoding='utf-8',buffering=1))
                    except OSError:pass
    for name in ('stdout','stderr'):
        if getattr(sys,name) is None:setattr(sys,name,open(os.devnull,'w',encoding='utf-8'))
        # Windows 为继承的管道创建的流可能仍是本机代码页，冻结模式不读取 -X utf8。
        stream=getattr(sys,name)
        if hasattr(stream,'reconfigure'):stream.reconfigure(encoding='utf-8',errors='backslashreplace',newline='\n',line_buffering=True)


def write_result(path,value):
    if path:
        p=Path(path);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(value,ensure_ascii=False,indent=2),encoding='utf-8')


def keep_source_process(no_forward=False):
    """Stay in this interpreter instead of handing off to an installed build."""
    if no_forward:return True
    if sys.gettrace() is not None:return True
    return 'pydevd' in sys.modules or 'debugpy' in sys.modules


def main():
    if len(sys.argv)>2 and sys.argv[1]=='--upgrade-probe':
        worker_stdio()
        from backend.upgrades.probe import main as probe
        probe(sys.argv[2:]);return
    if len(sys.argv)>2 and sys.argv[1]=='--upgrade-helper':
        worker_stdio()
        from desktop.upgrade import helper
        helper(sys.argv[2:]);return
    if len(sys.argv)>2 and sys.argv[1]=='--module':
        worker_stdio()
        from backend.processes import MODULES
        module=sys.argv[2]
        if module not in MODULES:raise SystemExit('不支持的子进程')
        sys.argv=[module,*sys.argv[3:]];runpy.run_module(module,run_name='__main__');return
    parser=argparse.ArgumentParser(description='StockAI 桌面与本机 Web 工作台')
    parser.add_argument('--web',action='store_true',help='打开浏览器，后台驻留系统托盘')
    parser.add_argument('--data-dir',help='指定独立的工作区目录')
    parser.add_argument('--local',action='store_true',help='使用本机独立模式')
    parser.add_argument('--remote-url',help='仅连接指定 HTTPS 远程 Web，不启动本机后台')
    parser.add_argument('--remote-account',help='该站点的独立账号会话配置名称')
    parser.add_argument('--port',type=int,default=8765,help='首选本机端口，被占用时顺延')
    parser.add_argument('--no-forward',action='store_true',help='留在当前源码进程，不转交已安装版本')
    parser.add_argument('--self-test',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--smoke-test',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--replace-instance',action='store_true',help=argparse.SUPPRESS)
    parser.add_argument('--result',help=argparse.SUPPRESS)
    args=parser.parse_args()
    if not 1024<=args.port<=65505:parser.error('端口须为1024至65505')
    from desktop.host import workspace_path,configure_home
    home=workspace_path(args.data_dir);home.mkdir(parents=True,exist_ok=True)
    if not args.local and not args.remote_url and not args.self_test and not args.smoke_test:
        mode_file=home/'desktop-mode.json'
        if mode_file.exists():
            saved=json.loads(mode_file.read_text(encoding='utf-8'))
            if saved.get('mode')=='remote':args.remote_url=saved['url'];args.remote_account=saved.get('account','')
    if not args.remote_url:configure_home(home)
    if not args.self_test and not args.smoke_test and not keep_source_process(args.no_forward):
        from desktop.upgrade import forward
        if forward(home,sys.argv[1:],client_only=bool(args.remote_url)):return
    log=(home/'desktop.log').open('a',encoding='utf-8',buffering=1)
    if sys.stdout is None:sys.stdout=log
    if sys.stderr is None:sys.stderr=log
    try:
        if args.self_test:
            from desktop.check import self_test
            result=self_test(home,args.port);write_result(args.result,result)
            if not result.get('passed'):raise SystemExit(1)
        elif args.remote_url:
            from desktop.remote import launch as launch_remote
            launch_remote(home,args)
        else:launch(home,args)
    except Exception as exc:
        traceback.print_exc(file=log);write_result(args.result,{'passed':False,'error':type(exc).__name__})
        if args.self_test or args.smoke_test:raise SystemExit(1)
        message=str(exc)+'\n\n工作区：'+str(home)
        try:
            from PySide6.QtWidgets import QApplication,QMessageBox
            app=QApplication.instance() or QApplication([])
            QMessageBox.critical(None,'StockAI 启动失败',message)
        except ImportError:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None,message,'StockAI 启动失败',0x10)
        raise SystemExit(1)


def running_version_is_older(current,target):
    if not target or target==current:return False
    try:
        from backend.upgrades.versions import Version
        return Version(current)<Version(target)
    except ValueError:return False


def launch(home,args):
    from PySide6.QtCore import QTimer,QUrl
    from PySide6.QtGui import QAction,QDesktopServices,QIcon
    from PySide6.QtNetwork import QLocalServer,QLocalSocket
    from PySide6.QtWidgets import QApplication,QMainWindow,QMessageBox,QSystemTrayIcon,QMenu,QFileDialog,QInputDialog
    from PySide6.QtWebEngineCore import QWebEnginePage,QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from backend.config import ROOT
    from backend.cloud import VERSION
    from desktop.host import Host,instance_id,prepare_page_cache
    app=QApplication(sys.argv[:1]);app.setApplicationName('StockAI');app.setOrganizationName('StockAI')
    app.setQuitOnLastWindowClosed(False)
    icon=QIcon(str(ROOT/'frontend/dist-public/favicon.svg'));app.setWindowIcon(icon)
    name='StockAI-Desktop-'+instance_id(home);probe=QLocalSocket();probe.connectToServer(name)
    if probe.waitForConnected(600):
        if args.replace_instance:
            probe.write(b'replace');probe.flush();probe.waitForBytesWritten(600);probe.disconnectFromServer()
            released=False
            for _ in range(80):
                time.sleep(0.25);again=QLocalSocket();again.connectToServer(name);held=again.waitForConnected(200);again.disconnectFromServer()
                if not held:released=True;break
            if not released:write_result(args.result,{'passed':True,'existing_instance':True});return
        else:
            probe.write(b'activate');probe.flush();probe.waitForBytesWritten(600)
            write_result(args.result,{'passed':True,'existing_instance':True});return
    QLocalServer.removeServer(name);ipc=QLocalServer()
    if not ipc.listen(name):raise RuntimeError('无法建立本机单实例通信')
    window=QMainWindow();window.setWindowTitle('StockAI '+VERSION+' · 个人研究终端');window.resize(1440,940);window.setMinimumSize(960,650)
    view=QWebEngineView(window);window.setCentralWidget(view)
    profile=QWebEngineProfile('StockAI-Desktop',window)
    profile.setPersistentStoragePath(str(home/'desktop-profile'));prepare_page_cache(profile,home/'desktop-cache')
    host=Host(home,args.port,background=not args.smoke_test)
    from desktop.links import open_blank_target
    class Page(QWebEnginePage):
        def acceptNavigationRequest(self,url,kind,main_frame):
            if url.scheme()=='http' and url.host()=='127.0.0.1' and url.port()==host.port:return True
            if kind==QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in ('https','http'):QDesktopServices.openUrl(url)
            return False
        # target=_blank 会先要新窗口，不会走进当前页的外链判断。
        def createWindow(self,kind):
            return open_blank_target(self,('http','https'),QDesktopServices.openUrl)
    page=Page(profile,view);view.setPage(page)
    from desktop.credentials_bridge import attach_login_credentials
    attach_login_credentials(page,os.path.normcase(str(home.resolve()))+'|local',host.url)
    tray=QSystemTrayIcon(icon,app);tray.setToolTip('StockAI · 桌面与 Web 共用本机工作区')
    menu=QMenu();tray.setContextMenu(menu)
    def show():window.showNormal();window.raise_();window.activateWindow()
    def browser():QDesktopServices.openUrl(QUrl(host.url))
    def data_folder():QDesktopServices.openUrl(QUrl.fromLocalFile(str(home)))
    def remote_mode():
        url,ok=QInputDialog.getText(window,'连接远程 Web','远程 HTTPS 根地址')
        if not ok:return
        from desktop.remote import origin,restart
        try:url=origin(url.strip())
        except ValueError as exc:QMessageBox.warning(window,'地址无效',str(exc));return
        account,ok=QInputDialog.getText(window,'隔离远程账号会话','请输入该站点的账号邮箱（用于隔离本机缓存）')
        if not ok or not account.strip():return
        restart(home,'--remote-url',url,'--remote-account',account.strip());quit_all()
    quitting={'value':False}
    def quit_all():
        if quitting['value']:return
        quitting['value']=True;tray.hide();window.hide();host.close();app.quit()
    def close_event(event):
        if args.smoke_test or quitting['value']:event.accept();return
        if QSystemTrayIcon.isSystemTrayAvailable():window.hide();event.ignore()
        else:quit_all();event.accept()
    window.closeEvent=close_event
    for title,callback in [('打开桌面',show),('打开 Web 版',browser),('连接远程 Web…',remote_mode),('打开数据目录',data_folder),('退出 StockAI（停止本机服务）',quit_all)]:
        action=QAction(title,menu);action.triggered.connect(callback);menu.addAction(action)
    native_menu=window.menuBar().addMenu('StockAI')
    for action in menu.actions():native_menu.addAction(action)
    tray.activated.connect(lambda reason:show() if reason in (QSystemTrayIcon.ActivationReason.DoubleClick,QSystemTrayIcon.ActivationReason.Trigger) else None)
    tray.show()
    def on_instance():
        sock=ipc.nextPendingConnection()
        if not sock:return
        def deliver():
            message=bytes(sock.readAll())
            if not message:return
            sock.deleteLater()
            if message.startswith(b'replace'):quit_all()
            else:show()
        sock.readyRead.connect(deliver)
        if sock.bytesAvailable():deliver()
    ipc.newConnection.connect(on_instance)
    def download(item):
        name,_=QFileDialog.getSaveFileName(window,'保存导出文件',item.downloadFileName())
        if name:
            path=Path(name);item.setDownloadDirectory(str(path.parent));item.setDownloadFileName(path.name);item.accept()
        else:item.cancel()
    profile.downloadRequested.connect(download)
    window.statusBar().showMessage('正在启动本机研究空间…');window.show();app.processEvents()
    host.start();host.wait(process_events=app.processEvents)
    from backend.upgrades.cli import manager as upgrade_manager
    upgrade_manager_instance=upgrade_manager('desktop',str(home))
    def upgrade_handoff():
        try:tx=upgrade_manager_instance.current()
        except Exception:return
        if not tx or quitting['value']:return
        if tx['status']=='exit_requested':
            from desktop.upgrade import handoff
            handoff(home,tx);quit_all();return
        target=tx.get('actual_version') or tx.get('target_version')
        if tx['status']=='succeeded' and not keep_source_process(args.no_forward) and running_version_is_older(VERSION,target):quit_all()
    upgrade_timer=QTimer(window);upgrade_timer.timeout.connect(upgrade_handoff);upgrade_timer.start(1500)
    window.statusBar().showMessage('Web 版：'+host.url+' · 桌面和浏览器共享数据 · 关闭窗口后驻留托盘')
    (home/'running.json').write_text(json.dumps({'url':host.url,'pid':os.getpid(),'version':VERSION,'instance':instance_id(home)}),encoding='utf-8')
    outcome={'passed':False,'desktop_loaded':False,'web_url':host.url,'version':VERSION}
    def loaded(ok):
        if not args.smoke_test:return
        outcome['desktop_loaded']=ok;outcome['passed']=bool(ok and host.ready())
        QTimer.singleShot(2500,quit_all)
    view.loadFinished.connect(loaded);view.setUrl(QUrl(host.url))
    if args.web:browser();window.hide()
    if args.smoke_test:QTimer.singleShot(45000,quit_all)
    try:app.exec()
    finally:
        host.close();tray.hide();ipc.close();page.deleteLater();view.setPage(None)
        write_result(args.result,outcome)
    if args.smoke_test and not outcome['passed']:raise RuntimeError('桌面页面加载未通过')
