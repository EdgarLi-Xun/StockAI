# 控制本机引导登录、局域网访问、来源校验和管理员与只读权限。
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import secrets
import threading
import time
from urllib.parse import urlsplit

from . import db
from .config import DATA

TOKENS={}
LOGIN_ATTEMPTS={}
TOKEN_LOCK=threading.Lock()


# 读取或生成本地管理员令牌，供可信本机客户端引导会话。
def local_token():
    path=DATA/".local-session"
    DATA.mkdir(parents=True,exist_ok=True)
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48),encoding="utf-8")
    return path.read_text(encoding="utf-8").strip()


# 判断地址是否为本机回环地址，非法地址按不可信处理。
def loopback(host):
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host=="testclient"  # 测试客户端的专用来源只用于测试，不能作为生产网络地址处理。


# 检测代理转发头，区分直连本机请求与转发请求。
def forwarded(request):
    return any(request.headers.get(k) for k in ("x-forwarded-for","forwarded","x-real-ip"))


# 只将没有转发头的可信回环连接认定为本机请求。
def local_request(request):
    return loopback(request.client.host) and not forwarded(request)


# 提取访问控制所需的实际来源地址。
def effective_peer(request):
    if loopback(request.client.host) and forwarded(request):
        return (request.headers.get("x-forwarded-for") or request.headers.get("x-real-ip") or "invalid").split(",")[0].strip()
    return request.client.host


# 核对请求来源是否属于允许的服务地址，阻止跨站访问。
def origin_allowed(request):
    origin=request.headers.get("origin")
    if not origin:
        return request.headers.get("sec-fetch-site") not in {"cross-site"}
    p=urlsplit(origin)
    host=request.headers.get("host","")
    if p.scheme in {"http","https"} and p.netloc==host:
        return True
    return loopback(request.client.host) and origin in {"http://127.0.0.1:5173","http://localhost:5173"} and host in {"127.0.0.1:8765","localhost:8765","testserver"}


# 校验 Host 为本机、已允许的内网地址或已配置的代理主机。
def valid_host(request):
    host=urlsplit("//"+request.headers.get("host","")).hostname
    if host in {"localhost","127.0.0.1","::1","testserver"}:
        return True
    cfg=db.settings()
    if cfg.lan_enabled and cfg.proxy_hostname and host==cfg.proxy_hostname and loopback(request.client.host):
        return True
    try:
        ip=ipaddress.ip_address(host)
        return db.settings().lan_enabled and ip.is_private and not ip.is_unspecified
    except (ValueError,TypeError):
        return False


# 按回环地址、局域网开关与 CIDR 白名单判断来源是否允许。
def peer_allowed(host):
    if loopback(host):
        return True
    cfg=db.settings()
    if not cfg.lan_enabled:
        return False
    try:
        ip=ipaddress.ip_address(host)
        return any(ip in ipaddress.ip_network(net) for net in cfg.allowed_networks)
    except ValueError:
        return False


# 校验角色和密码要求后保存密码验证信息。
def configure_password(password,role="admin"):
    if role not in {"admin","reader"} or len(password)<12 or len(password)>200:
        raise ValueError("访问密码至少12位，角色仅支持管理员或只读")
    salt=secrets.token_hex(24)
    hashed=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(salt),310000).hex()
    db.execute("INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",("auth_"+role,db.dumps({"salt":salt,"hash":hashed})))
    with TOKEN_LOCK:
        TOKENS.clear()


# 检查局域网访问所需的管理员密码是否已配置。
def lan_ready():
    return bool(db.one("SELECT key FROM settings WHERE key='auth_admin'"))


# 限制登录尝试并校验密码，签发与角色绑定的访问令牌。
def login(password,role,host):
    with TOKEN_LOCK:
        attempts=[t for t in LOGIN_ATTEMPTS.get(host,[]) if time.monotonic()-t<300]
        if len(attempts)>=8:
            raise ValueError("登录失败次数过多，请5分钟后再试")
        attempts.append(time.monotonic())
        LOGIN_ATTEMPTS[host]=attempts
    row=db.one("SELECT value FROM settings WHERE key=?",("auth_"+role,)) if role in {"admin","reader"} else None
    if not row:
        raise ValueError("密码或角色错误")
    auth=json.loads(row["value"])
    actual=hashlib.pbkdf2_hmac("sha256",password.encode(),bytes.fromhex(auth["salt"]),310000).hex()
    if not hmac.compare_digest(actual,auth["hash"]):
        raise ValueError("密码或角色错误")
    token=secrets.token_urlsafe(48)
    with TOKEN_LOCK:
        TOKENS[token]=(role,time.monotonic()+8*3600)
        LOGIN_ATTEMPTS.pop(host,None)
    return {"token":token,"role":role,"expires_in":8*3600}


# 验证访问令牌并返回角色，拒绝过期或无效会话。
def authenticate(request):
    value=request.headers.get("authorization","")
    token=value[7:] if value.startswith("Bearer ") else ""
    if local_request(request) and token and hmac.compare_digest(token,local_token()):
        return "admin"
    with TOKEN_LOCK:
        record=TOKENS.get(token)
        if record and record[1]>time.monotonic():
            return record[0]
        TOKENS.pop(token,None)
    return None
