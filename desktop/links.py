"""把桌面新窗口里的外链交给系统浏览器，应用窗口保持不动。"""
from __future__ import annotations

from urllib.parse import urlsplit

_POPUP = None


def external_target(value: str, schemes: tuple[str, ...] = ("http", "https")) -> str | None:
    """返回可以交给系统浏览器的地址。非允许协议或缺少主机名时返回空。"""
    part = urlsplit(value.strip())
    if part.scheme not in schemes or not part.hostname:
        return None
    return value.strip()


def open_blank_target(page, schemes, opener):
    """给 createWindow 返回临时页。外链只打开一次，临时页不加载该地址。"""
    popup = _popup_type()(page.profile(), page, tuple(schemes), opener)
    holding = getattr(page, "_blank_popups", None)
    if holding is None:
        holding = []
        page._blank_popups = holding
    holding.append(popup)

    def forget(*_args, item=popup, store=holding):
        if item in store:
            store.remove(item)

    popup.destroyed.connect(forget)
    return popup


def _popup_type():
    global _POPUP
    if _POPUP is None:
        from PySide6.QtCore import QTimer
        from PySide6.QtWebEngineCore import QWebEnginePage

        class ExternalPopup(QWebEnginePage):
            def __init__(self, profile, parent, schemes, opener):
                super().__init__(profile, parent)
                self._schemes = schemes
                self._opener = opener
                self._sent = False

            def acceptNavigationRequest(self, url, kind, main_frame):
                self._open(url)
                return False

            def _open(self, url):
                if self._sent or external_target(url.toString(), self._schemes) is None:
                    return
                self._sent = True
                self._opener(url)
                QTimer.singleShot(0, self.deleteLater)

        _POPUP = ExternalPopup
    return _POPUP
