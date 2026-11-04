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
    def response(self,url,headers=None):
        self.requests.append((url,headers));status,body,values=self.responses.pop(0)
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


@pytest.mark.parametrize('url',['https://releases.example.test/index.json','https://api.github.com/internal','https://api.github.com:444/repos/owner/repo/releases','https://api.github.com.evil.test/repos/owner/repo/releases'])
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


def test_corrupt_complete_package_can_be_downloaded_again(tmp_path):
    data=b'authentic';path=tmp_path/'payload.exe';fetch=Stream((200,b'corrupted',{'ETag':'"same"'}),(200,data,{'ETag':'"same"'}))
    with pytest.raises(UpgradeError) as error:fetch.download(release(data),path)
    assert error.value.code=='PACKAGE' and not path.exists() and not path.with_suffix('.part').exists()
    fetch.download(release(data),path);assert path.read_bytes()==data and 'Range' not in fetch.requests[-1][1]
