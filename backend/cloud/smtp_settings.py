# 保存站点发信账号，并在未保存时回退到部署环境变量。
from __future__ import annotations

import json
import re
import smtplib
import ssl
from dataclasses import dataclass

from .store import json_text

SETTINGS_KEY = "smtp_settings"
SECRET_KEY = "smtp_secret"
# 站点级密文绑定，不对应某个用户账号。
SITE_BIND = "site"
RESOURCE = "smtp"
PURPOSE = "smtp-password"
ALLOWED = {"provider", "enabled", "host", "port", "starttls", "username", "sender", "password"}
HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,198}[A-Za-z0-9])?")
EMAIL = re.compile(r"[^@\s]{1,64}@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 预设只填服务器、端口和加密方式，账号与授权码仍由管理员填写。
PRESETS = {
    "qq": {"label": "QQ 邮箱", "host": "smtp.qq.com", "port": 465, "starttls": False, "note": "登录账号填完整 QQ 邮箱。授权码在 QQ 邮箱的设置里生成，不是 QQ 密码。"},
    "163": {"label": "网易 163", "host": "smtp.163.com", "port": 465, "starttls": False, "note": "登录账号填完整 163 邮箱。密码使用客户端授权码。"},
    "126": {"label": "126 邮箱", "host": "smtp.126.com", "port": 465, "starttls": False, "note": "登录账号填完整 126 邮箱。密码使用客户端授权码。"},
    "gmail": {"label": "Gmail", "host": "smtp.gmail.com", "port": 465, "starttls": False, "note": "使用 Gmail 地址和应用专用密码。"},
    "outlook": {"label": "Outlook", "host": "smtp.office365.com", "port": 587, "starttls": True, "note": "使用 Outlook 邮箱。加密方式为 STARTTLS。"},
    "custom": {"label": "自定义", "host": "", "port": 465, "starttls": False, "note": "按邮件服务商提供的 SMTP 服务器、端口和加密方式填写。"},
}


# 描述一次实际发信使用的账号，不写入接口响应。
@dataclass(frozen=True)
class MailAccount:
    host: str
    port: int
    username: str
    password: str
    sender: str
    starttls: bool


# 返回可选服务商，供邮件配置页展示。
def catalog():
    return [{"id": key, **value} for key, value in PRESETS.items()]


# 读取已保存的非密配置；没有保存记录时返回空。
def saved(store):
    row = store.one("SELECT value FROM cloud_meta WHERE key=?", (SETTINGS_KEY,))
    return json.loads(row["value"]) if row else None


# 判断授权码密文是否存在，不解密。
def secret_present(store):
    return store.one("SELECT 1 FROM cloud_meta WHERE key=?", (SECRET_KEY,)) is not None


# 页面已保存时以该记录为准；未保存时才看环境变量是否配齐主机和发件人。
def configured(store):
    row = saved(store)
    if row is not None:
        return bool(row.get("enabled") and row.get("host") and row.get("sender") and row.get("username") and secret_present(store))
    cfg = store.cfg
    return bool(cfg.smtp_host and cfg.smtp_from)


# 解密站点授权码。密钥不可用或密文损坏时按未配置处理。
def _password(store, keys):
    if keys is None:
        return ""
    row = store.one("SELECT value FROM cloud_meta WHERE key=?", (SECRET_KEY,))
    if not row:
        return ""
    try:
        return keys.open(row["value"], site=store.site_id, user=SITE_BIND, resource=RESOURCE, purpose=PURPOSE)
    except Exception:
        return ""


# 得到本次发信账号。已保存但停用时不回退到环境变量。
def effective(store, keys):
    row = saved(store)
    if row is not None:
        password = _password(store, keys)
        if not (row.get("enabled") and row.get("host") and row.get("sender") and row.get("username") and password):
            return None
        return MailAccount(row["host"], int(row["port"]), row["username"], password, row["sender"], bool(row["starttls"]))
    cfg = store.cfg
    if not (cfg.smtp_host and cfg.smtp_from):
        return None
    return MailAccount(cfg.smtp_host, int(cfg.smtp_port), cfg.smtp_user or "", cfg.smtp_password or "", cfg.smtp_from, bool(cfg.smtp_starttls))


# 组装管理员可见的配置。授权码只暴露是否已设置和末四位。
def view(store):
    row = saved(store)
    cfg = store.cfg
    environment = bool(cfg.smtp_host and cfg.smtp_from)
    if row is None:
        current = {"provider": "custom", "host": cfg.smtp_host or "", "port": int(cfg.smtp_port or 465), "starttls": bool(cfg.smtp_starttls), "username": cfg.smtp_user or "", "sender": cfg.smtp_from or "", "enabled": environment, "password_hint": ""}
        password_set = bool(cfg.smtp_password)
        source = "environment" if environment else "none"
    else:
        current = {key: row.get(key) for key in ("provider", "host", "port", "starttls", "username", "sender", "enabled", "password_hint")}
        password_set = secret_present(store)
        source = "saved"
    return {**current, "password_set": password_set, "saved": row is not None, "source": source, "configured": configured(store), "providers": catalog()}


# 校验主机名，拒绝地址、路径和空白。
def _host(value):
    if not isinstance(value, str) or not HOST.fullmatch(value) or ".." in value:
        raise ValueError("SMTP 服务器无效")
    return value


# 校验发件人和登录账号使用的邮箱格式。
def _email(value, label):
    if not isinstance(value, str) or len(value) > 200 or not EMAIL.fullmatch(value):
        raise ValueError(label + "无效")
    return value


# 把提交内容整理为可保存的非密记录，并决定是否更换授权码。
def _normalize(payload, existing, has_secret):
    if not isinstance(payload, dict) or set(payload) - ALLOWED:
        raise ValueError("邮件配置不合法")
    provider = payload.get("provider")
    if provider not in PRESETS:
        raise ValueError("不支持的邮箱服务商")
    enabled = payload.get("enabled")
    if not isinstance(enabled, bool):
        raise ValueError("邮件配置不合法")
    preset = PRESETS[provider]
    raw_host = payload.get("host", "")
    host = _host(raw_host.strip()) if isinstance(raw_host, str) and raw_host.strip() else preset["host"]
    port = payload.get("port", preset["port"])
    starttls = payload.get("starttls", preset["starttls"])
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(starttls, bool):
        raise ValueError("邮件配置不合法")
    username = payload.get("username", "")
    sender = payload.get("sender", "")
    if not isinstance(username, str) or not isinstance(sender, str):
        raise ValueError("邮件配置不合法")
    username = username.strip()
    sender = sender.strip()
    if enabled:
        if not host:
            raise ValueError("请填写 SMTP 服务器")
        host = _host(host)
        username = _email(username, "发信账号")
        sender = _email(sender, "发件人")
    elif host:
        host = _host(host)
        if username:
            username = _email(username, "发信账号")
        if sender:
            sender = _email(sender, "发件人")
    password = payload.get("password", "")
    if not isinstance(password, str) or len(password) > 200 or "\n" in password or "\r" in password:
        raise ValueError("授权码或密码无效")
    if password and not password.strip():
        raise ValueError("授权码或密码无效")
    if enabled and not password and not has_secret:
        raise ValueError("请填写授权码或密码")
    hint = password[-4:] if len(password) >= 4 else (existing or {}).get("password_hint", "") if not password else ""
    public = {"provider": provider, "host": host, "port": port, "starttls": starttls, "username": username, "sender": sender, "enabled": enabled, "password_hint": hint}
    return public, password or None


# 保存邮件配置。留空授权码表示沿用已加密的值。
def save(store, keys, actor, payload):
    with store.tx(immediate=True) as c:
        current = c.execute("SELECT value FROM cloud_meta WHERE key=?", (SETTINGS_KEY,)).fetchone()
        existing = json.loads(current[0]) if current else None
        has_secret = c.execute("SELECT 1 FROM cloud_meta WHERE key=?", (SECRET_KEY,)).fetchone() is not None
        public, password = _normalize(payload, existing, has_secret)
        c.execute("INSERT INTO cloud_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SETTINGS_KEY, json_text(public)))
        if password is not None:
            sealed = keys.seal(password, site=store.site_id, user=SITE_BIND, resource=RESOURCE, purpose=PURPOSE)
            c.execute("INSERT INTO cloud_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (SECRET_KEY, sealed))
    store.audit(actor, "save_mail_settings", public["provider"])
    return view(store)


# 按账号的加密方式连接 SMTP 并发送一封邮件。
def transmit(account, message):
    if account.starttls:
        with smtplib.SMTP(account.host, account.port, timeout=15) as server:
            server.starttls(context=ssl.create_default_context())
            if account.username:
                server.login(account.username, account.password)
            refused = server.send_message(message)
    else:
        with smtplib.SMTP_SSL(account.host, account.port, timeout=15, context=ssl.create_default_context()) as server:
            if account.username:
                server.login(account.username, account.password)
            refused = server.send_message(message)
    if refused:
        raise smtplib.SMTPRecipientsRefused(refused)
