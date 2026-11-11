"""桌面新窗口外链交给系统浏览器，不在应用窗口内打开。"""
import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from desktop.links import external_target


def test_external_target_keeps_official_https_and_drops_other_schemes():
    assert external_target("https://static.cninfo.com.cn/finalpage/a.pdf") == "https://static.cninfo.com.cn/finalpage/a.pdf"
    assert external_target("http://quote.eastmoney.com/sh603126.html", ("https",)) is None
    assert external_target("about:blank") is None
    assert external_target("javascript:alert(1)") is None
    assert external_target("https:///missing-host") is None
    assert external_target("") is None


def test_desktop_pages_delegate_blank_targets():
    application = Path("desktop/application.py").read_text(encoding="utf-8")
    legacy = Path("desktop/main.py").read_text(encoding="utf-8")
    remote = Path("desktop/remote.py").read_text(encoding="utf-8")
    assert "return open_blank_target(self,('http','https'),QDesktopServices.openUrl)" in application
    assert 'return open_blank_target(self, ("http", "https"), QDesktopServices.openUrl)' in legacy
    assert "return open_blank_target(self,('https',),QDesktopServices.openUrl)" in remote


def test_blank_popup_opens_allowed_scheme_once():
    page, kind = _page()
    opened = []
    from desktop.links import open_blank_target
    from PySide6.QtCore import QUrl

    popup = open_blank_target(page, ("https",), opened.append)
    assert popup.acceptNavigationRequest(QUrl("http://static.cninfo.com.cn/a.pdf"), kind, True) is False
    assert opened == []
    target = QUrl("https://static.cninfo.com.cn/finalpage/a.pdf")
    assert popup.acceptNavigationRequest(target, kind, True) is False
    assert popup.acceptNavigationRequest(target, kind, True) is False
    assert [item.toString() for item in opened] == [target.toString()]
    another = open_blank_target(page, ("https",), opened.append)
    assert another.acceptNavigationRequest(QUrl("about:blank"), kind, True) is False
    assert another.acceptNavigationRequest(QUrl("javascript:alert(1)"), kind, True) is False
    assert len(opened) == 1


def test_clicking_blank_announcement_asks_browser_not_the_app():
    import pytest
    pytest.importorskip("PySide6.QtWebEngineWidgets")
    from PySide6.QtCore import QEventLoop, QTimer
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile
    from PySide6.QtWebEngineWidgets import QWebEngineView
    from desktop.links import open_blank_target

    app = QApplication.instance() or QApplication([])
    opened = []

    class Page(QWebEnginePage):
        def acceptNavigationRequest(self, url, kind, main_frame):
            return True

        def createWindow(self, kind):
            return open_blank_target(self, ("http", "https"), opened.append)

    profile = QWebEngineProfile()
    page = Page(profile)
    page._profile = profile
    page._app = app
    view = QWebEngineView()
    view.setPage(page)
    html = "<a id='a' href='https://static.cninfo.com.cn/finalpage/a.pdf' target='_blank' rel='noopener'>公告</a>"
    loop = QEventLoop()
    state = {"loaded": False}

    def click():
        page.runJavaScript("document.getElementById('a').click()")
        QTimer.singleShot(700, loop.quit)

    def loaded(ok):
        state["loaded"] = ok
        QTimer.singleShot(200, click) if ok else loop.quit()

    view.loadFinished.connect(loaded)
    QTimer.singleShot(8000, loop.quit)
    view.setHtml(html)
    loop.exec()
    assert state["loaded"]
    assert [item.toString() for item in opened] == ["https://static.cninfo.com.cn/finalpage/a.pdf"]
    assert page.url().scheme() != "https" or "cninfo" not in page.url().host()
    view.setPage(None)


def _page():
    import pytest
    pytest.importorskip("PySide6.QtWebEngineCore")
    from PySide6.QtWidgets import QApplication
    from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

    app = QApplication.instance() or QApplication([])
    profile = QWebEngineProfile()
    page = QWebEnginePage(profile)
    page._profile = profile
    page._app = app
    return page, QWebEnginePage.NavigationType.NavigationTypeLinkClicked
