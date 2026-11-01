from __future__ import annotations

import hashlib
import sys

from backend.config import DATA, ROOT


def launch(smoke_test=False):
    try:
        from PySide6.QtCore import QTimer, QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtNetwork import QLocalServer, QLocalSocket
        from PySide6.QtWidgets import QApplication, QMainWindow, QMessageBox
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
        from PySide6.QtWebEngineWidgets import QWebEngineView
    except ImportError:
        raise SystemExit("缺少桌面组件。请运行 scripts/install.ps1 或 pip install -r requirements-desktop.txt")
    from run import URL, ensure_server
    app=QApplication(sys.argv[:1])
    app.setApplicationName("StockAI")
    app.setOrganizationName("StockAI Local")
    server_name="StockAI-"+hashlib.sha256(str(ROOT).encode()).hexdigest()[:16]
    socket=QLocalSocket()
    socket.connectToServer(server_name)
    if socket.waitForConnected(500):
        socket.write(b"activate")
        socket.flush()
        socket.waitForBytesWritten(500)
        return
    QLocalServer.removeServer(server_name)
    server=QLocalServer()
    if not server.listen(server_name):
        raise SystemExit("无法创建桌面单实例通信服务")
    try:
        ensure_server()
    except Exception as exc:
        QMessageBox.critical(None,"StockAI 启动失败",str(exc))
        raise SystemExit(1)
    class Page(QWebEnginePage):
        def acceptNavigationRequest(self,url,kind,main_frame):
            if url.host() in {"127.0.0.1","localhost"} and url.port()==8765:
                return True
            if kind==QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in {"https","http"}:
                QDesktopServices.openUrl(url)
            return False
    window=QMainWindow()
    window.setWindowTitle("StockAI · 个人研究终端")
    window.resize(1440,940)
    window.setMinimumSize(960,650)
    profile=QWebEngineProfile("StockAI",window)
    profile.setPersistentStoragePath(str(DATA/"desktop-profile"))
    profile.setCachePath(str(DATA/"desktop-cache"))
    view=QWebEngineView(window)
    page=Page(profile,view)
    view.setPage(page)
    window.setCentralWidget(view)
    window.statusBar().showMessage("本地服务 · 127.0.0.1:8765 · 仅研究与模拟 · 关闭窗口后共享服务继续运行")
    def download(item):
        from PySide6.QtWidgets import QFileDialog
        name,_=QFileDialog.getSaveFileName(window,"保存导出文件",item.downloadFileName())
        if name:
            from pathlib import Path
            p=Path(name)
            item.setDownloadDirectory(str(p.parent))
            item.setDownloadFileName(p.name)
            item.accept()
        else:
            item.cancel()
    profile.downloadRequested.connect(download)
    def activate():
        pending=server.nextPendingConnection()
        if pending:
            pending.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    server.newConnection.connect(activate)
    outcome={"loaded":False}
    def loaded(ok):
        outcome["loaded"]=ok
        if not ok:
            window.statusBar().showMessage("页面加载失败，请检查 data/server.log")
        if smoke_test:
            QTimer.singleShot(2500,app.quit)
    view.loadFinished.connect(loaded)
    view.setUrl(QUrl(URL))
    window.show()
    if smoke_test:
        QTimer.singleShot(30000,app.quit)
    app.exec()
    view.setPage(None)
    page.deleteLater()
    if smoke_test:
        print("DESKTOP_SMOKE="+("PASS" if outcome["loaded"] else "FAIL"))
        if not outcome["loaded"]:
            raise SystemExit(1)
