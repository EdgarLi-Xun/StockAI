"""Remote desktop is an isolated HTTPS browser client; it never constructs a Host."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.parse import urlsplit


def origin(value):
    p=urlsplit(value)
    if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.path not in ('','/'):
        raise ValueError('远程站点需填写 HTTPS 根地址，例如 https://stockai.example.com')
    host=p.hostname.encode('idna').decode().lower()
    if ':' in host:host='['+host+']'
    return 'https://'+host+(':'+str(p.port) if p.port and p.port!=443 else '')


def profile_id(url,account):return hashlib.sha256((origin(url)+'\n'+str(account).strip().casefold()).encode()).hexdigest()[:32]


def restart(home,*arguments):
    from backend.processes import hidden_process
    command=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve().parents[1]/'stockai_desktop.py')]
    subprocess.Popen([*command,'--data-dir',str(home),*arguments],**hidden_process())


def launch(home,args):
    from PySide6.QtCore import QUrl,QTimer
    from PySide6.QtGui import QAction,QDesktopServices,QIcon
    from PySide6.QtNetwork import QLocalServer,QLocalSocket
    from PySide6.QtWidgets import QApplication,QMainWindow,QFileDialog
    from PySide6.QtWebEngineCore import QWebEnginePage,QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from backend.config import ROOT
    from backend.cloud import VERSION
    url=origin(args.remote_url);account=args.remote_account or '独立远程账号';ident=profile_id(url,account)
    app=QApplication(sys.argv[:1]);app.setApplicationName('StockAI');app.setOrganizationName('StockAI')
    name='StockAI-Remote-'+hashlib.sha256(str(home.resolve()).encode()).hexdigest()[:16]+'-'+ident;probe=QLocalSocket();probe.connectToServer(name)
    if probe.waitForConnected(500):probe.write(b'activate');probe.flush();probe.waitForBytesWritten(500);return
    QLocalServer.removeServer(name);ipc=QLocalServer()
    if not ipc.listen(name):raise RuntimeError('无法建立远程客户端单实例通信')
    window=QMainWindow();window.resize(1440,940);window.setWindowTitle('StockAI '+VERSION+' · 远程 '+url)
    icon=QIcon(str(ROOT/'frontend/dist-public/favicon.svg'));window.setWindowIcon(icon)
    view=QWebEngineView(window);window.setCentralWidget(view)
    profile=QWebEngineProfile('StockAI-Remote-'+ident,window)
    profile.setPersistentStoragePath(str(home/'remote-profiles'/ident/'storage'));profile.setCachePath(str(home/'remote-profiles'/ident/'cache'))
    class Page(QWebEnginePage):
        def acceptNavigationRequest(self,destination,kind,main_frame):
            try:
                if origin(destination.toString().split('#')[0].split('?')[0])==url:return True
            except ValueError:
                # 同站业务路由允许路径，顶层身份凭据仍由远程站点验证。
                p=urlsplit(destination.toString())
                if p.scheme=='https' and p.netloc==urlsplit(url).netloc:return True
            if kind==QWebEnginePage.NavigationType.NavigationTypeLinkClicked and destination.scheme()=='https':QDesktopServices.openUrl(destination)
            return False
    page=Page(profile,view);view.setPage(page)
    menu=window.menuBar().addMenu('StockAI')
    requested_close={};pending_close={}
    def local():
        requested_close['mode']='local';window.close()
    for label,fn in [('用浏览器打开远程 Web',lambda:QDesktopServices.openUrl(QUrl(url))),('切换为本机独立模式',local),('退出远程客户端',window.close)]:
        action=QAction(label,window);action.triggered.connect(fn);menu.addAction(action)
    action=QAction('关于与升级本机客户端…',window)
    closing={'allowed':False}
    def close_event(event):
        if closing['allowed']:event.accept()
        else:
            pending_close.clear();pending_close.update(requested_close);requested_close.clear()
            event.ignore();page.triggerAction(QWebEnginePage.WebAction.RequestClose)
    def accepted_close():
        closing['allowed']=True
        if pending_close.get('mode')=='local':
            (home/'desktop-mode.json').write_text(json.dumps({'mode':'local'}),encoding='utf-8');restart(home,'--local')
        if pending_close.get('transaction'):
            from desktop.upgrade import handoff
            from desktop.client_updates import manager as client_manager
            m=client_manager(home);tx=m.current()
            if tx and tx['id']==pending_close['transaction']['id']:
                tx=m.update(tx,'exit_requested',client_only=True)
                handoff(home,tx,client_only=True,remote_url=args.remote_url,remote_account=args.remote_account or '')
        window.close()
    window.closeEvent=close_event;page.windowCloseRequested.connect(accepted_close)
    def upgrade_exit(tx):requested_close['transaction']=tx;window.close()
    def upgrades():
        from desktop.client_updates import show
        show(window,home,args,upgrade_exit)
    action.triggered.connect(upgrades);menu.addAction(action)
    def download(item):
        name,_=QFileDialog.getSaveFileName(window,'保存远程导出文件',item.downloadFileName())
        if name:item.setDownloadDirectory(str(Path(name).parent));item.setDownloadFileName(Path(name).name);item.accept()
        else:item.cancel()
    profile.downloadRequested.connect(download)
    ipc.newConnection.connect(lambda:(window.showNormal(),window.raise_(),window.activateWindow()))
    window.statusBar().showMessage('远程客户端 · '+url+' · '+account+' · 研究数据与后台任务运行在远程站点')
    (home/'desktop-mode.json').write_text(json.dumps({'mode':'remote','url':url,'account':account}),encoding='utf-8')
    import threading
    from desktop.client_updates import background_checks
    stop_checks=threading.Event();background_checks(home,stop_checks)
    window.show();view.setUrl(QUrl(url))
    if args.smoke_test:QTimer.singleShot(10000,window.close)
    try:app.exec()
    finally:stop_checks.set();ipc.close();page.deleteLater();view.setPage(None)
