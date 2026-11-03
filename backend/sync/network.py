"""Pinned HTTPS requests with explicit NAS policy and no redirect/proxy credential forwarding."""
from __future__ import annotations
from dataclasses import dataclass
import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urlsplit,urlunsplit,unquote,quote
from .protocol import SyncError,MAX_OBJECT


@dataclass
class Reply:
    status:int
    headers:dict
    body:bytes


class HTTPS:
    def __init__(self,url,*,allow_private=False,private_networks=(),desktop=False,ca_file=None,timeout=15):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:raise SyncError('INCOMPATIBLE','同步地址必须是无凭据的 HTTPS 地址')
        decoded=unquote(parsed.path)
        if any(part in ('.','..') for part in decoded.split('/')) or '\\' in decoded or any(ord(v)<32 or ord(v)==127 for v in decoded):raise SyncError('CORRUPT','同步目录路径不安全')
        if parsed.port is not None and not 1<=parsed.port<=65535:raise SyncError('INCOMPATIBLE')
        self.base=urlunsplit((parsed.scheme,parsed.netloc,quote(decoded.rstrip('/'),safe='/@-._~'),'',''));self.host=parsed.hostname;self.port=parsed.port or 443;self.path=quote(decoded.rstrip('/'),safe='/@-._~')
        self.allow_private=allow_private;self.networks=[ipaddress.ip_network(n) for n in private_networks];self.desktop=desktop
        self.context=ssl.create_default_context(cafile=ca_file);self.timeout=min(max(timeout,1),60)
        self.stats={'uploaded_bytes':0,'downloaded_bytes':0,'requests':0}

    def addresses(self):
        try:addresses=list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(self.host,self.port,type=socket.SOCK_STREAM)))
        except OSError:raise SyncError('OFFLINE') from None
        if not addresses:raise SyncError('OFFLINE')
        for address in addresses:
            ip=ipaddress.ip_address(address)
            if isinstance(ip,ipaddress.IPv6Address) and ip.ipv4_mapped:ip=ip.ipv4_mapped
            if ip.is_unspecified or ip.is_multicast or ip.is_link_local or str(ip) in ('169.254.169.254','100.100.100.200'):raise SyncError('INCOMPATIBLE','同步目标地址被拒绝')
            if not ip.is_global and not (self.allow_private and (self.desktop or any(ip in net for net in self.networks))):raise SyncError('INCOMPATIBLE','内网同步目标尚未明确受信任')
        return addresses

    def request(self,method,path='',body=None,headers=None,limit=MAX_OBJECT*2):
        if not path.startswith('/') or '..' in path.split('/') or '\\' in path or '%' in path:raise SyncError('CORRUPT')
        addresses=self.addresses();connection=http.client.HTTPSConnection(self.host,self.port,context=self.context,timeout=self.timeout)
        # 连接已验证的 IP，TLS SNI 与证书仍使用原主机名。
        def connect():
            sock=socket.create_connection((addresses[0],self.port),self.timeout)
            try:connection.sock=self.context.wrap_socket(sock,server_hostname=self.host)
            except BaseException:sock.close();raise
        connection.connect=connect
        try:
            connection.request(method,self.path+path,body=body,headers=headers or {})
            response=connection.getresponse();data=response.read(limit+1)
            self.stats['uploaded_bytes']+=len(body or b'');self.stats['downloaded_bytes']+=len(data);self.stats['requests']+=1
            if len(data)>limit:raise SyncError('CORRUPT','远端响应超过大小限制')
            if 300<=response.status<400:raise SyncError('INCOMPATIBLE','同步服务返回重定向，未转发凭据')
            return Reply(response.status,{k.lower():v for k,v in response.getheaders()},data)
        except SyncError:raise
        except ssl.SSLCertVerificationError:raise SyncError('INCOMPATIBLE','同步服务证书未受信任，请由运行端管理员配置可信 CA') from None
        except (OSError,ssl.SSLError,http.client.HTTPException):raise SyncError('OFFLINE') from None
        finally:connection.close()
