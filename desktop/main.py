# 使用 Qt WebEngine 打开共享研究界面，管理单实例、导航、下载和窗口生命周期。
from __future__ import annotations

import hashlib
import sys

from backend.config import DATA, ROOT


# 确保共享后端就绪后启动桌面窗口，并处理单实例与冒烟检查。
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
    # 限制内嵌页面导航范围，把外部链接交给系统浏览器。
    from desktop.links import open_blank_target
    class Page(QWebEnginePage):
        # 仅在嵌入窗口中接受允许的本地导航，其他链接按外部地址处理。
        def acceptNavigationRequest(self,url,kind,main_frame):
            if url.host() in {"127.0.0.1","localhost"} and url.port()==8765:
                return True
            if kind==QWebEnginePage.NavigationType.NavigationTypeLinkClicked and url.scheme() in {"https","http"}:
                QDesktopServices.openUrl(url)
            return False
        # target=_blank 的公告和来源链接走新窗口，转给系统浏览器。
        def createWindow(self, kind):
            return open_blank_target(self, ("http", "https"), QDesktopServices.openUrl)
    window=QMainWindow()
    window.setWindowTitle("StockAI · 个人研究终端")
    window.resize(1440,940)
    window.setMinimumSize(960,650)
    from desktop.host import prepare_page_cache
    profile=QWebEngineProfile("StockAI",window)
    profile.setPersistentStoragePath(str(DATA/"desktop-profile"))
    prepare_page_cache(profile, DATA/"desktop-cache")
    view=QWebEngineView(window)
    page=Page(profile,view)
    view.setPage(page)
    window.setCentralWidget(view)
    window.statusBar().showMessage("本地服务 · 127.0.0.1:8765 · 仅研究与模拟 · 关闭窗口后共享服务继续运行")
    # 通过系统保存对话框确定下载位置后接受或取消下载。
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
    # 收到另一个桌面实例的激活请求时恢复并前置当前窗口。
    def activate():
        pending=server.nextPendingConnection()
        if pending:
            pending.deleteLater()
        window.showNormal()
        window.raise_()
        window.activateWindow()
    server.newConnection.connect(activate)
    outcome={"loaded":False}
    # 记录主页面加载结果，供冒烟检查判断界面是否成功打开。
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
