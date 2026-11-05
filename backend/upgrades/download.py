"""Bounded streamed HTTPS downloads; resume only an identical signed target and strong ETag."""
import hashlib
import http.client
import ipaddress
import json
import os
import re
from pathlib import Path
import socket
import ssl
from urllib.parse import urlsplit,urljoin
from backend.cloud.crypto import atomic_private
from backend.sync.protocol import canonical,digest
from .github import ASSET_HOSTS,github_asset
from .trust import UpgradeError


class Fetcher:
    def __init__(self,timeout=20):self.timeout=timeout
    def connection(self,url,hosts=None):
        parsed=urlsplit(url)
        if parsed.scheme!='https' or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:raise UpgradeError('TARGET','更新地址必须为可信 HTTPS')
        # Release assets stay on GitHub's certificate. Callers must pass ASSET_HOSTS;
        # every other package URL remains IP-pinned so a redirect cannot enter a private network.
        if hosts is not None:
            if hosts!=ASSET_HOSTS or not github_asset(parsed):raise UpgradeError('TARGET','安装包地址不在 GitHub 发行下载范围内')
            path=(parsed.path or '/')+('?'+parsed.query if parsed.query else '')
            return http.client.HTTPSConnection(parsed.hostname,443,context=ssl.create_default_context(),timeout=self.timeout),path
        # Only the fixed public GitHub list endpoint may use VPN/fake-IP DNS.
        # TLS still verifies api.github.com; arbitrary feeds and packages remain IP-pinned.
        if parsed.hostname=='api.github.com' and parsed.port in (None,443) and re.fullmatch(r'/repos/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*/releases/?',parsed.path):
            return http.client.HTTPSConnection('api.github.com',443,context=ssl.create_default_context(),timeout=self.timeout),parsed.path+('?' +parsed.query if parsed.query else '')
        try:addresses=list(dict.fromkeys(r[4][0] for r in socket.getaddrinfo(parsed.hostname,parsed.port or 443,type=socket.SOCK_STREAM)))
        except OSError:raise UpgradeError('NETWORK','更新源暂不可达') from None
        if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):raise UpgradeError('TARGET','更新源不允许指向内网或元数据服务')
        context=ssl.create_default_context();conn=http.client.HTTPSConnection(parsed.hostname,parsed.port or 443,context=context,timeout=self.timeout)
        def connect():
            raw=socket.create_connection((addresses[0],parsed.port or 443),self.timeout)
            try:conn.sock=context.wrap_socket(raw,server_hostname=parsed.hostname)
            except BaseException:raw.close();raise
        conn.connect=connect;return conn,(parsed.path or '/')+('?' +parsed.query if parsed.query else '')

    def response(self,url,headers=None,hosts=None,on_connection=None):
        # Each redirect is checked again. GitHub asset downloads cannot leave ASSET_HOSTS.
        for _ in range(4):
            if hosts is not None and urlsplit(url).hostname not in hosts:raise UpgradeError('TARGET','安装包地址不在 GitHub 发行下载范围内')
            conn,path=self.connection(url,hosts)
            if on_connection:on_connection(conn)
            try:conn.request('GET',path,headers=headers or {});response=conn.getresponse()
            except (OSError,http.client.HTTPException,ValueError):
                conn.close()
                raise UpgradeError('NETWORK','更新请求未完成') from None
            if response.status in (301,302,303,307,308):
                target=urljoin(url,response.getheader('Location',''));conn.close()
                if target==url:raise UpgradeError('NETWORK','更新源重定向无效')
                url=target;continue
            return conn,response
        raise UpgradeError('NETWORK','更新源重定向过多')

    def metadata(self,url,headers=None):
        conn,response=self.response(url,{'Accept':'application/json',**(headers or {})})
        try:
            if response.status==429 or response.status==403 and response.getheader('X-RateLimit-Remaining')=='0':raise UpgradeError('RATE_LIMITED','更新请求已被限流，请稍后再试')
            if response.status!=200:raise UpgradeError('NETWORK','无法读取发布清单，HTTP '+str(response.status))
            raw=response.read(1024*1024+1)
            if len(raw)>1024*1024:raise UpgradeError('UNTRUSTED')
            return json.loads(raw)
        except (OSError,http.client.HTTPException):raise UpgradeError('NETWORK','发布清单读取未完成') from None
        finally:conn.close()

    def fetch_bytes(self,url,limit,hosts):
        conn,response=self.response(url,{'Accept':'application/octet-stream','Accept-Encoding':'identity'},hosts)
        try:
            if response.status==429 or response.status==403 and response.getheader('X-RateLimit-Remaining')=='0':raise UpgradeError('RATE_LIMITED','更新请求已被限流，请稍后再试')
            if response.status!=200:raise UpgradeError('NETWORK','无法读取发行校验，HTTP '+str(response.status))
            raw=response.read(limit+1)
            if len(raw)>limit:raise UpgradeError('PACKAGE')
            return raw
        except (OSError,http.client.HTTPException):raise UpgradeError('NETWORK','发行校验读取未完成') from None
        finally:conn.close()

    def download(self,release,destination,progress=None,cancel=None,hosts=None,on_connection=None):
        package=release['package'];path=Path(destination);part=path.with_suffix('.part');sidecar=path.with_suffix('.resume.json');identity=digest(package)
        start=0;tag=None;chunk_size=64*1024;report_every=1024*1024
        if part.exists() and sidecar.exists():
            try:old=json.loads(sidecar.read_bytes())
            except (OSError,ValueError):old={}
            if not isinstance(old,dict):old={}
            if old.get('target')==identity and isinstance(old.get('etag'),str) and old['etag'].startswith('"') and not old['etag'].startswith('W/'):
                start=part.stat().st_size;tag=old['etag']
        def paused():
            return bool(cancel and cancel())
        def verified():
            h=hashlib.sha256()
            with part.open('rb') as source:
                for chunk in iter(lambda:source.read(1024*1024),b''):h.update(chunk)
            if h.hexdigest()!=package['sha256']:
                part.unlink(missing_ok=True);sidecar.unlink(missing_ok=True)
                raise UpgradeError('PACKAGE')
            part.replace(path)
            return path
        # 最后一个数据块后取消/崩溃，可能已收齐但尚未改名；本地验完即可完成，不能永久卡在 HTTP 416。
        if start==package['size']:
            result=verified()
            if progress:progress(start,package['size'])
            return result
        if start>package['size']:start=0
        if paused():raise UpgradeError('CANCELLED','下载已暂停')
        headers={'Accept-Encoding':'identity'}
        if start:headers.update(Range=f'bytes={start}-',**{'If-Range':tag})
        try:conn,response=self.response(package['url'],headers,hosts,on_connection)
        except UpgradeError as exc:
            if exc.code=='NETWORK' and paused():raise UpgradeError('CANCELLED','下载已暂停') from None
            raise
        try:
            current=response.getheader('ETag','')
            if response.status==200:start=0
            elif response.status==206:
                expected=f'bytes {start}-'
                if not start or current!=tag or not response.getheader('Content-Range','').startswith(expected) or not response.getheader('Content-Range','').endswith('/'+str(package['size'])):raise UpgradeError('PACKAGE','续传目标发生变化，保留旧版本')
            else:raise UpgradeError('NETWORK','下载请求未完成，HTTP '+str(response.status))
            path.parent.mkdir(parents=True,exist_ok=True);atomic_private(sidecar,canonical({'target':identity,'etag':current if current.startswith('"') and not current.startswith('W/') else None}))
            size=start;reported=start
            with part.open('ab' if start else 'wb') as output:
                while True:
                    if paused():raise UpgradeError('CANCELLED','下载已暂停')
                    try:chunk=response.read(chunk_size)
                    except (OSError,http.client.HTTPException,ValueError):
                        if paused():raise UpgradeError('CANCELLED','下载已暂停') from None
                        raise
                    if not chunk:break
                    size+=len(chunk)
                    if size>package['size']:raise UpgradeError('PACKAGE')
                    output.write(chunk)
                    if progress and size-reported>=report_every:progress(size,package['size']);reported=size
                output.flush();os.fsync(output.fileno())
            if progress and size!=reported:progress(size,package['size'])
            if size!=package['size']:raise UpgradeError('NETWORK','下载中断，可在目标标识一致时续传')
            if paused():raise UpgradeError('CANCELLED','下载已暂停')
            return verified()
        except UpgradeError:raise
        except (OSError,http.client.HTTPException,ValueError):
            if paused():raise UpgradeError('CANCELLED','下载已暂停') from None
            raise UpgradeError('NETWORK','下载未完成，当前程序未改变') from None
        finally:
            try:conn.close()
            except Exception:pass
