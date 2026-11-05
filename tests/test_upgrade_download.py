"""Exercise actual HTTP response parsing, on-disk partial data and signed-target verification."""
import http.client
import io
import json
import socket
import pytest
from backend.sync.protocol import digest
from backend.upgrades.download import Fetcher
from backend.upgrades.trust import UpgradeError


class Socket:
    def __init__(self,data):self.data=data
    def makefile(self,*args):return io.BytesIO(self.data)


class Stream(Fetcher):
    def __init__(self,*responses):self.responses=list(responses);self.requests=[];self.closed=0
    def response(self,url,headers=None,hosts=None,on_connection=None):
        self.requests.append((url,headers,hosts));status,body,values=self.responses.pop(0)
        if on_connection:on_connection(self)
        fields={'Content-Length':str(len(body)),**values}
        raw=('HTTP/1.1 '+str(status)+' OK\r\n'+''.join(k+': '+v+'\r\n' for k,v in fields.items())+'\r\n').encode()+body
        response=http.client.HTTPResponse(Socket(raw));response.begin();return self,response
    def close(self):self.closed+=1


def test_metadata_forwards_public_api_headers_without_credentials():
    fetch=Stream((200,b'[]',{}));headers={'Accept':'application/vnd.github+json','User-Agent':'StockAI/test'}
    assert fetch.metadata('https://api.github.com/repos/owner/repo/releases',headers)==[]
    assert fetch.requests[0][1]==headers and fetch.closed==1


def test_fixed_github_list_endpoint_keeps_tls_hostname_verification_with_fake_ip_dns(monkeypatch):
    calls=[]
    def connection(host,port,*,context,timeout):
        calls.append((host,port,context,timeout));return object()
    def no_dns_probe(*args,**kwargs):pytest.fail('固定 GitHub HTTPS 主机应由 TLS 连接器处理代理 DNS')
    monkeypatch.setattr(http.client,'HTTPSConnection',connection)
    monkeypatch.setattr(socket,'getaddrinfo',no_dns_probe)
    conn,path=Fetcher().connection('https://api.github.com/repos/owner/repo/releases?per_page=100&page=1')
    assert path=='/repos/owner/repo/releases?per_page=100&page=1'
    assert calls[0][0:2]==('api.github.com',443) and calls[0][2].check_hostname
    assert calls[0][2].verify_mode.name=='CERT_REQUIRED'


@pytest.mark.parametrize('url',['https://releases.example.test/index.json','https://api.github.com/internal','https://api.github.com:444/repos/owner/repo/releases','https://api.github.com.evil.test/repos/owner/repo/releases','https://github.com/EdgarLi-Xun/StockAI/releases/download/v3.3.0/StockAI-3.3.0-Windows.exe'])
def test_other_update_urls_still_reject_private_or_fake_ip_targets(monkeypatch,url):
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:[(socket.AF_INET,socket.SOCK_STREAM,6,'',('198.18.25.114',443))])
    with pytest.raises(UpgradeError) as error:Fetcher().connection(url)
    assert error.value.code=='TARGET'


@pytest.mark.parametrize('status,headers',[(429,{}),(403,{'X-RateLimit-Remaining':'0'})])
def test_rate_limits_are_reported_and_the_response_is_closed(status,headers):
    fetch=Stream((status,b'{}',headers))
    with pytest.raises(UpgradeError) as error:fetch.metadata('https://api.github.com/repos/owner/repo/releases')
    assert error.value.code=='RATE_LIMITED' and fetch.closed==1


def release(data,url='https://releases.example.test/fixture.exe'):
    return {'package':{'size':len(data),'sha256':digest(data),'url':url}}


def test_interrupted_stream_resumes_only_same_signed_target_and_strong_etag(tmp_path):
    data=b'a'*(1024*1024)+b'last block';path=tmp_path/'payload.exe'
    fetch=Stream((200,data[:1024*1024],{'ETag':'"stable"'}),(206,data[1024*1024:],{'ETag':'"stable"','Content-Range':f'bytes 1048576-{len(data)-1}/{len(data)}'}))
    with pytest.raises(UpgradeError) as error:fetch.download(release(data),path)
    assert error.value.code=='NETWORK' and not path.exists()
    fetch.download(release(data),path)
    assert path.read_bytes()==data and fetch.requests[1][1]['Range']=='bytes=1048576-' and fetch.requests[1][1]['If-Range']=='"stable"'
    assert fetch.closed==2


def test_closing_the_connection_pauses_a_blocked_read_without_waiting_for_the_chunk(tmp_path):
    import threading
    entered,closed=threading.Event(),threading.Event()
    class Conn:
        def close(self):closed.set()
    class Body:
        status=200
        def getheader(self,name,default=''):return {'etag':'"stall"','content-length':'10485760'}.get(name.lower(),default)
        def read(self,n):
            entered.set()
            if not closed.wait(2):raise TimeoutError('pause did not close the download connection')
            raise OSError('connection closed')
    fetch=Fetcher();held={}
    def response(url,headers=None,hosts=None,on_connection=None):
        conn=Conn();held['conn']=conn
        if on_connection:on_connection(conn)
        return conn,Body()
    fetch.response=response
    package={'size':10*1024*1024,'sha256':'ab'*32,'url':'https://releases.example.test/fixture.exe'}
    error={}
    def run():
        try:fetch.download({'package':package},tmp_path/'payload.exe',cancel=lambda:closed.is_set(),on_connection=lambda conn:held.__setitem__('bound',conn))
        except Exception as exc:error['exc']=exc
    thread=threading.Thread(target=run);thread.start()
    assert entered.wait(2) and held.get('bound') is held.get('conn')
    held['conn'].close()
    thread.join(2)
    assert not thread.is_alive()
    assert isinstance(error.get('exc'),UpgradeError) and error['exc'].code=='CANCELLED'
    assert not (tmp_path/'payload.exe').exists()


def test_cancel_after_last_block_can_finalize_without_another_request(tmp_path):
    data=b'complete but not finalized';path=tmp_path/'payload.exe';fetch=Stream((200,data,{'ETag':'"stable"'}));state={'cancel':False}
    with pytest.raises(UpgradeError) as error:fetch.download(release(data),path,lambda *args:state.update(cancel=True),lambda:state['cancel'])
    assert error.value.code=='CANCELLED' and not path.exists()
    fetch.download(release(data),path)
    assert path.read_bytes()==data and len(fetch.requests)==1


@pytest.mark.parametrize('change',['target','weak','corrupt-sidecar'])
def test_other_target_or_unsafe_resume_metadata_starts_from_zero(tmp_path,change):
    data=b'correct package';path=tmp_path/'payload.exe';path.with_suffix('.part').write_bytes(b'stale fragment')
    value={'target':digest(release(data)['package']),'etag':'"stable"'}
    if change=='target':value['target']='different target'
    if change=='weak':value['etag']='W/"stable"'
    path.with_suffix('.resume.json').write_text('broken json' if change=='corrupt-sidecar' else json.dumps(value))
    fetch=Stream((200,data,{'ETag':'"stable"'}));fetch.download(release(data),path)
    assert path.read_bytes()==data and 'Range' not in fetch.requests[0][1]


def test_server_changes_etag_during_resume_keeps_current_program(tmp_path):
    data=b'final package';path=tmp_path/'payload.exe';path.with_suffix('.part').write_bytes(data[:3]);path.with_suffix('.resume.json').write_text(json.dumps({'target':digest(release(data)['package']),'etag':'"old"'}))
    fetch=Stream((206,data[3:],{'ETag':'"new"','Content-Range':f'bytes 3-{len(data)-1}/{len(data)}'}))
    with pytest.raises(UpgradeError) as error:fetch.download(release(data),path)
    assert error.value.code=='PACKAGE' and not path.exists() and path.with_suffix('.part').read_bytes()==data[:3]


class Conn:
    def __init__(self,raw):self.raw=raw
    def request(self,method,path,body=None,headers=None):self.path=path
    def getresponse(self):
        response=http.client.HTTPResponse(Socket(self.raw));response.begin();return response
    def close(self):pass


def test_official_github_asset_uses_certificate_verification_without_ip_pinning(monkeypatch):
    from backend.upgrades.github import ASSET_HOSTS
    calls=[]
    def connection(host,port,*,context,timeout):
        calls.append((host,context.check_hostname,context.verify_mode.name));return object()
    monkeypatch.setattr(http.client,'HTTPSConnection',connection)
    monkeypatch.setattr(socket,'getaddrinfo',lambda *args,**kwargs:pytest.fail('发行包主机不应改走内网地址探测'))
    conn,path=Fetcher().connection('https://github.com/EdgarLi-Xun/StockAI/releases/download/v3.3.0/StockAI-3.3.0-Windows.exe.sha256',ASSET_HOSTS)
    assert calls==[('github.com',True,'CERT_REQUIRED')] and path.endswith('/StockAI-3.3.0-Windows.exe.sha256')
    with pytest.raises(UpgradeError) as error:Fetcher().connection('https://github.com/EdgarLi-Xun/StockAI/archive/refs/tags/v3.3.0.tar.gz',ASSET_HOSTS)
    assert error.value.code=='TARGET'


def test_github_asset_redirect_cannot_leave_release_hosts():
    from backend.upgrades.github import ASSET_HOSTS
    fetch=Fetcher();seen=[]
    def connection(url,hosts=None):
        seen.append(url)
        raw=b'HTTP/1.1 302 Found\r\nLocation: https://evil.example/payload.exe\r\nContent-Length: 0\r\n\r\n'
        return Conn(raw),'/'
    fetch.connection=connection
    with pytest.raises(UpgradeError) as error:fetch.response('https://github.com/EdgarLi-Xun/StockAI/releases/download/v3.3.0/StockAI-3.3.0-Windows.exe',hosts=ASSET_HOSTS)
    assert error.value.code=='TARGET' and seen==['https://github.com/EdgarLi-Xun/StockAI/releases/download/v3.3.0/StockAI-3.3.0-Windows.exe']


def test_github_asset_may_follow_its_own_download_cdn():
    from backend.upgrades.github import ASSET_HOSTS
    fetch=Fetcher();seen=[]
    def connection(url,hosts=None):
        seen.append(url)
        if len(seen)==1:raw=b'HTTP/1.1 302 Found\r\nLocation: https://objects.githubusercontent.com/releases/file?id=1\r\nContent-Length: 0\r\n\r\n'
        else:raw=b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nOK'
        return Conn(raw),'/'
    fetch.connection=connection
    conn,response=fetch.response('https://github.com/EdgarLi-Xun/StockAI/releases/download/v3.3.0/StockAI-3.3.0-Windows.exe',hosts=ASSET_HOSTS)
    assert response.status==200 and seen[1].startswith('https://objects.githubusercontent.com/releases/file?id=1')


def test_corrupt_complete_package_can_be_downloaded_again(tmp_path):
    data=b'authentic';path=tmp_path/'payload.exe';fetch=Stream((200,b'corrupted',{'ETag':'"same"'}),(200,data,{'ETag':'"same"'}))
    with pytest.raises(UpgradeError) as error:fetch.download(release(data),path)
    assert error.value.code=='PACKAGE' and not path.exists() and not path.with_suffix('.part').exists()
    fetch.download(release(data),path);assert path.read_bytes()==data and 'Range' not in fetch.requests[-1][1]
