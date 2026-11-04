"""Expose local autofill only to the desktop's pinned top-level origin, never HTTP."""
from __future__ import annotations

import json
import os
from urllib.parse import urlsplit

from PySide6.QtCore import QFile,QIODevice,QObject,Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineCore import QWebEngineScript

from .credentials import LoginVault


def _origin(value):
    url=urlsplit(value)
    if url.scheme not in ('http','https') or not url.hostname or url.username or url.password:
        return None
    return url.scheme,url.hostname.lower(),url.port or (443 if url.scheme=='https' else 80)


class LoginBridge(QObject):
    def __init__(self,page,scope,origin,parent=None,vault=None):
        super().__init__(parent)
        self.page,self.origin,self.vault=page,_origin(origin),vault or LoginVault(scope)
        if self.origin is None:raise ValueError('登录记录必须绑定明确站点')

    def _call(self,operation):
        try:
            if _origin(self.page.url().toString())!=self.origin:return json.dumps({'ok':False,'error':'登录记录仅供当前站点使用'})
            return json.dumps({'ok':True,'login':operation()},ensure_ascii=False)
        except Exception:
            return json.dumps({'ok':False,'error':'无法访问本机保存的账号密码，请手动填写或稍后重试'},ensure_ascii=False)

    @Slot(result=str)
    def read(self):return self._call(self.vault.read)

    @Slot(str,str,result=str)
    def save(self,email,password):return self._call(lambda:self.vault.save(email,password))

    @Slot(result=str)
    def clear(self):return self._call(self.vault.clear)


def attach_login_credentials(page,scope,origin):
    if os.name!='nt':return None
    channel=QWebChannel(page);bridge=LoginBridge(page,scope,origin,parent=channel)
    channel.registerObject('stockaiLogin',bridge);page.setWebChannel(channel)
    resource=QFile(':/qtwebchannel/qwebchannel.js')
    if not resource.open(QIODevice.OpenModeFlag.ReadOnly):raise RuntimeError('缺少桌面登录自动填充组件')
    source=bytes(resource.readAll()).decode('utf-8');resource.close()
    source+='\n;new QWebChannel(qt.webChannelTransport,function(channel){window.stockaiLogin=channel.objects.stockaiLogin;window.dispatchEvent(new Event("stockai-login-ready"));});'
    script=QWebEngineScript();script.setName('StockAI login autofill')
    script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
    script.setWorldId(QWebEngineScript.ScriptWorldId.MainWorld);script.setRunsOnSubFrames(False)
    script.setSourceCode(source);page.scripts().insert(script)
    # Keep the transport and object alive for the page lifetime.
    page._stockai_login=(channel,bridge)
    return bridge
