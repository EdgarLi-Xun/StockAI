"""Exercise actual HTTP response parsing, on-disk partial data and signed-target verification."""
import http.client
import io
import json
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
