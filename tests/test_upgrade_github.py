"""GitHub discovery stays separate from signed acceptance. Desktop EXE may install a checksum-matched asset."""
from datetime import datetime,timedelta,timezone
from pathlib import Path
import hashlib
import threading
import time
import pytest
from backend.upgrades.github import ASSET_HOSTS,DEFAULT_SOURCE,asset_url,parse_checksum,repository,latest
from backend.upgrades.manager import Manager
from backend.upgrades.trust import UpgradeError
from test_cloud import cloud
from test_upgrade_protocol import manager


def target(component='desktop',form='exe'):
    return {'component':component,'form':form,'architecture':'x64'}


def release(version='3.3.0-preview.1',*,suffix='Setup.exe',size=1234,**changes):
    value={'tag_name':'v'+version,'draft':False,'prerelease':'-' in version,'published_at':'2026-11-04T00:00:00Z','assets':[{'name':f'StockAI-{version}-{suffix}','state':'uploaded','size':size,'browser_download_url':'https://unrelated.example.test/do-not-use.exe'}]}
    value.update(changes);return value


class Pages:
    def __init__(self,*pages):self.pages=list(pages);self.requests=[]
    def metadata(self,url,headers=None):
        self.requests.append((url,headers));return self.pages.pop(0)


@pytest.mark.parametrize('url',[DEFAULT_SOURCE,'https://github.com/EdgarLi-Xun/StockAI','https://github.com/EdgarLi-Xun/StockAI.git','https://github.com/EdgarLi-Xun/StockAI/releases/'])
def test_project_and_api_addresses_resolve_to_the_same_repository(url):
    assert repository(url)=='EdgarLi-Xun/StockAI'


@pytest.mark.parametrize('url',['http://github.com/owner/repo','https://github.com.evil.test/owner/repo','https://github.com/owner/../repo','https://github.com/owner/repo?token=private','https://name:password@github.com/owner/repo','https://github.com:invalid/owner/repo','https://api.github.com/repos/owner/repo/releases/latest'])
def test_other_or_credentialed_addresses_do_not_become_github_repositories(url):
    assert repository(url) is None


@pytest.mark.parametrize('channel,expected',[('stable','3.3.0'),('preview','3.3.0-preview.10')])
def test_channels_and_semver_choose_the_highest_matching_public_release(channel,expected):
    fetcher=Pages([release('3.3.0-preview.9'),release('3.3.0'),release('9.0.0',draft=True),release('3.3.0-preview.10')])
    result=latest(fetcher,'EdgarLi-Xun/StockAI',target(),channel)
    assert result['version']==expected and result['signature_verified'] is False
    assert result['download_url'].startswith('https://github.com/EdgarLi-Xun/StockAI/releases/download/v'+expected+'/')
    assert 'unrelated.example.test' not in str(result) and 'package' not in result
    assert fetcher.requests[0][1]['User-Agent'].startswith('StockAI/')
    assert 'Authorization' not in fetcher.requests[0][1]


@pytest.mark.parametrize('component,form,suffix',[('desktop','exe','Setup.exe'),('desktop','source','Windows.zip'),('web','source','Public.zip'),('hub','source','Hub.zip')])
def test_only_the_matching_component_and_installation_form_is_offered(component,form,suffix):
    data=release();data['assets']=[release(suffix=value)['assets'][0] for value in ('Setup.exe','Windows.zip','Public.zip','Hub.zip')]
    result=latest(Pages([data]),'EdgarLi-Xun/StockAI',target(component,form),'preview')
    assert result['asset_name'].endswith(suffix) and result['component']==component


def test_missing_packages_and_prereleases_do_not_appear_on_the_stable_channel():
    assert latest(Pages([release(),release('4.0.0',prerelease=True),release('3.3.0',suffix='Public.zip')]),'owner/repo',target(),'stable') is None
    assert latest(Pages([release(assets=[{'name':'StockAI-3.3.0-preview.1-Setup.exe','size':1234,'state':'new'}])]),'owner/repo',target(),'preview') is None
    assert latest(Pages([release(suffix='Windows.exe')]),'owner/repo',target(),'preview') is None


def test_pagination_does_not_hide_a_newer_version_from_later_pages():
    first=[release(draft=True) for _ in range(99)]+[release('3.3.0-preview.9')]
    fetcher=Pages(first,[release('3.3.0-preview.10')])
    assert latest(fetcher,'owner/repo',target(),'preview')['version']=='3.3.0-preview.10'
    assert len(fetcher.requests)==2 and fetcher.requests[-1][0].endswith('page=2')


@pytest.mark.parametrize('data',[{},[None],[{'tag_name':'v3.3.0'}]])
def test_invalid_github_responses_fail_instead_of_claiming_the_program_is_current(data):
    with pytest.raises(UpgradeError) as error:latest(Pages(data),'owner/repo',target(),'preview')
    assert error.value.code=='GITHUB_METADATA'


def test_incomplete_pagination_is_an_error_instead_of_a_current_version_result():
    fetcher=Pages(*[[release(draft=True) for _ in range(100)] for _ in range(5)])
    with pytest.raises(UpgradeError) as error:latest(fetcher,'owner/repo',target(),'preview')
    assert error.value.code=='GITHUB_INCOMPLETE' and len(fetcher.requests)==5


def test_default_source_checks_github_but_cannot_prepare_unsigned_installation(manager):
    m,key=manager
    m=Manager(m.cfg,m.keys,component='desktop',form='exe',version='3.2.0-preview.3',fetcher=Pages([release()]))
    m.state.put('settings',{**m.state.get('settings'),'channel':'preview'})
    assert m.source==DEFAULT_SOURCE and m.status()['source_kind']=='github'
    checked=m.check()
    assert checked['status']=='download_available' and checked['release']['signature_verified'] is False
    assert m.state.get('selected-envelope') is None and m.current() is None
    with pytest.raises(UpgradeError) as error:m.prepare()
    assert error.value.code=='UNTRUSTED'
    assert m.check(automatic=True)==checked and len(m.fetcher.requests)==1


def test_changing_source_or_channel_discards_old_failure_backoff_and_display(manager):
    m,key=manager;m.source=DEFAULT_SOURCE;m.target.update(component='desktop',form='exe')
    now=datetime.now(timezone.utc)
    m.state.put('check',{'status':'check_failed','error':'SOURCE_UNCONFIGURED','last_attempt':now.isoformat(),'next_retry':(now+timedelta(minutes=30)).isoformat()})
    m.state.put('settings',{**m.state.get('settings'),'channel':'preview'})
    m.fetcher=Pages([release()],[release('4.0.0')])
    assert m.status()['check']['status']=='never_checked'
    assert m.check(automatic=True)['status']=='download_available'
    settings=m.state.get('settings');m.state.put('settings',{**settings,'channel':'stable'})
    assert m.status()['check']['status']=='never_checked'
    assert m.check(automatic=True)['release']['version']=='4.0.0'


def test_disabled_automatic_checks_do_not_return_available_results_from_an_old_source(manager):
    m,key=manager;m.source=DEFAULT_SOURCE;m.fetcher=Pages()
    settings=m.state.get('settings');m.state.put('settings',{**settings,'automatic_check':False})
    m.state.put('check',{'status':'available','source_id':'old-source','release':{'version':'9.0.0'}})
    assert m.check(automatic=True)=={} and m.status()['check']['status']=='never_checked'
    assert not m.fetcher.requests and m.current() is None


@pytest.mark.parametrize('data,status,error',[([release('3.2.0-preview.3')],'download_current',None),([],'download_unavailable',None),({},'check_failed','GITHUB_METADATA')])
def test_github_current_missing_and_failed_results_are_distinct(manager,data,status,error):
    m,key=manager;m.source=DEFAULT_SOURCE;m.version='3.2.0-preview.3';m.target.update(component='desktop',form='exe');m.state.put('settings',{**m.state.get('settings'),'channel':'preview'});m.fetcher=Pages(data)
    check=m.check()
    assert check['status']==status and check['error']==error
    if error:
        assert check['next_retry'] and m.check(automatic=True)==check
    assert m.current() is None


class Checksum(Pages):
    def __init__(self,*pages,checksum=b''):
        super().__init__(*pages);self.checksum=checksum;self.reads=[]
    def fetch_bytes(self,url,limit,hosts):
        self.reads.append((url,limit,hosts));return self.checksum


def desktop(manager,fetcher):
    current,key=manager
    return Manager(current.cfg,current.keys,component='desktop',form='exe',version='3.2.1',fetcher=fetcher,store=current.store)


def published(payload,version='3.3.0'):
    name=f'StockAI-{version}-Setup.exe'
    return name,payload,(hashlib.sha256(payload).hexdigest()+'  '+name+'\n').encode()


def test_checksum_line_must_name_the_same_asset_in_lowercase():
    name='StockAI-3.3.0-Setup.exe';digest='ab'*32
    assert parse_checksum((digest+'  '+name+'\n').encode(),name)==digest
    for raw in ((digest+'  other.exe\n').encode(),(digest+' *'+name+'\n').encode(),(digest.upper()+'  '+name+'\n').encode(),(digest+'  '+name+'\n\n').encode(),'not-ascii-\xff'.encode()):
        with pytest.raises(UpgradeError) as error:parse_checksum(raw,name)
        assert error.value.code=='PACKAGE'


def test_checked_github_release_still_cannot_prepare_a_signed_plan(manager):
    name,payload,checksum=published(b'local-desktop-exe')
    m=desktop(manager,Checksum([release('3.3.0',size=len(payload))],checksum=checksum))
    assert m.check()['status']=='download_available'
    with pytest.raises(UpgradeError) as error:m.prepare()
    assert error.value.code=='UNTRUSTED' and m.current() is None


def test_desktop_downloads_the_official_asset(manager):
    from test_upgrade_download import Stream
    payload=b'local-desktop-exe';name,_,checksum=published(payload)
    class Feed(Stream):
        def __init__(self):
            super().__init__((200,payload,{'ETag':'"pkg"'}));self.page=[release('3.3.0',size=len(payload))];self.checksum=checksum;self.reads=[]
        def metadata(self,url,headers=None):
            self.requests.append((url,headers));return self.page
        def fetch_bytes(self,url,limit,hosts):
            self.reads.append((url,limit,hosts));return self.checksum
    m=desktop(manager,Feed());m.check();tx=m.prepare_local()
    assert tx['trust']=='github-sha256' and tx['status']=='planned'
    assert tx['release']['package']['url']==asset_url('EdgarLi-Xun/StockAI','3.3.0',name)
    assert m.fetcher.reads[0][0].endswith(name+'.sha256') and m.fetcher.reads[0][2]==ASSET_HOSTS
    assert m.download(tx['id'])['status']=='verified' and m.package_path(m.current()).read_bytes()==payload
    from backend.upgrades.runner import Runner
    from backend.upgrades.install_path import install_exe
    staged=Runner(m).prepare(m.current(),m.package_path(m.current()))
    assert staged['layout']=='setup' and staged['path'].endswith('StockAI-Setup.exe') and staged['program']==str(install_exe())
    assert Runner(m).verify(staged).name=='StockAI-Setup.exe'
    staged['program']=str(install_exe().parent/'elsewhere.exe')
    with pytest.raises(UpgradeError):Runner(m).verify(staged)
    assert m.fetcher.requests[-1][0].endswith('/'+name) and m.fetcher.requests[-1][2]==ASSET_HOSTS


def test_unexpected_download_failure_leaves_a_retryable_plan(manager):
    _,payload,checksum=published(b'boom-payload')
    class Boom(Checksum):
        def download(self,*args,**kwargs):
            raise RuntimeError('另一个服务或离线维护进程仍在使用此数据目录')
    m=desktop(manager,Boom([release('3.3.0',size=len(payload))],checksum=checksum));m.check();tx=m.prepare_local()
    with pytest.raises(UpgradeError) as error:m.download(tx['id'])
    assert error.value.code=='NETWORK'
    current=m.current()
    assert current['status']=='download_failed' and current['error']=='NETWORK'


def test_process_lock_without_wait_rejects_another_thread(tmp_path):
    from backend.cloud.lifecycle import ProcessLock
    path=tmp_path/'service.lock';held=threading.Event();done=threading.Event()
    def hold():
        with ProcessLock(path):
            held.set();done.wait(2)
    reader=threading.Thread(target=hold);reader.start()
    try:
        assert held.wait(1)
        with pytest.raises(RuntimeError):
            with ProcessLock(path):pass
    finally:
        done.set();reader.join(2)


def test_process_lock_wait_lets_another_thread_enter_after_release(tmp_path):
    from backend.cloud.lifecycle import ProcessLock
    path=tmp_path/'journal.lock';order=[];held=threading.Event()
    def hold():
        with ProcessLock(path,wait=True):
            order.append('a');held.set();time.sleep(0.05)
    reader=threading.Thread(target=hold);reader.start()
    assert held.wait(1)
    with ProcessLock(path,wait=True):order.append('b')
    reader.join(2);assert order==['a','b']


def test_second_manager_can_read_status_while_a_download_checks_cancel(manager):
    _,payload,checksum=published(b'x'*2048)
    class Busy(Checksum):
        def download(self,release,destination,progress=None,cancel=None,hosts=None,on_connection=None):
            for i in range(40):
                if cancel and cancel():raise UpgradeError('CANCELLED','下载已暂停')
                if progress:progress(min((i+1)*48,len(payload)),len(payload))
                time.sleep(0.005)
            path=Path(destination);path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(payload);return path
    m=desktop(manager,Busy([release('3.3.0',size=len(payload))],checksum=checksum));m.check();tx=m.prepare_local()
    other=Manager(m.cfg,m.keys,component='desktop',form='exe',version='3.2.1',store=m.store)
    done=threading.Event();errors=[]
    def hammer():
        while not done.is_set():
            try:other.current();other.status()
            except Exception as exc:errors.append(exc)
            time.sleep(0.002)
    reader=threading.Thread(target=hammer);reader.start()
    try:assert m.download(tx['id'])['status']=='verified'
    finally:
        done.set();reader.join(2)
    assert errors==[]


def test_request_cancel_stops_an_in_flight_download_without_waiting_for_the_next_chunk(manager):
    import threading,time
    started=threading.Event();name,payload,checksum=published(b'x'*1024)
    class Stall(Checksum):
        def download(self,*args,**kwargs):
            started.set();cancel=kwargs.get('cancel') if kwargs.get('cancel') is not None else (args[3] if len(args)>3 else None)
            deadline=time.monotonic()+3
            while time.monotonic()<deadline:
                if cancel and cancel():raise UpgradeError('CANCELLED','下载已暂停')
                time.sleep(0.02)
            raise TimeoutError('pause did not stop the download')
    m=desktop(manager,Stall([release('3.3.0',size=len(payload))],checksum=checksum));m.check();tx=m.prepare_local()
    error={}
    def run():
        try:m.download(tx['id'])
        except Exception as exc:error['exc']=exc
    thread=threading.Thread(target=run);thread.start()
    assert started.wait(2)
    m.request_cancel(tx['id']);thread.join(2)
    assert not thread.is_alive()
    assert isinstance(error.get('exc'),UpgradeError) and error['exc'].code=='CANCELLED'
    assert m.current()['status']=='download_paused' and m.current()['error']=='CANCELLED'


def test_request_cancel_pauses_a_stale_downloading_transaction_without_an_active_fetch(manager):
    _,payload,checksum=published(b'stale-partial')
    m=desktop(manager,Checksum([release('3.3.0',size=len(payload))],checksum=checksum));m.check();tx=m.prepare_local()
    m.update(tx,'downloading',downloaded=12,total=len(payload))
    paused=m.request_cancel(tx['id'])
    assert paused['status']=='download_paused' and paused['error']=='CANCELLED' and paused['downloaded']==12
    assert m.current()['id']==tx['id'] and m.state.get('cancel:'+tx['id']) is True


@pytest.mark.parametrize('interruption', ['CANCELLED', 'NETWORK'])
def test_desktop_resume_preserves_progress_and_clears_previous_download_error(manager, interruption):
    from test_upgrade_download import Stream
    chunk = 1024 * 1024
    payload = b'x' * chunk + b'final block'
    _, _, checksum = published(payload)
    class Feed(Stream):
        def __init__(self):
            super().__init__((200, payload[:chunk], {'ETag': '"resume"'}),
                             (206, payload[chunk:], {'ETag': '"resume"', 'Content-Range': f'bytes {chunk}-{len(payload)-1}/{len(payload)}'}))
        def metadata(self, *args):return [release('3.3.0', size=len(payload))]
        def fetch_bytes(self, *args):return checksum
    m = desktop(manager, Feed());m.check();tx = m.prepare_local()
    cancel = lambda: interruption == 'CANCELLED' and m.current().get('downloaded', 0) >= chunk
    with pytest.raises(UpgradeError) as error:m.download(tx['id'], cancel)
    assert error.value.code == interruption
    partial = m.current()
    assert partial['error'] == interruption and partial['downloaded'] == chunk
    assert partial['total'] == len(payload)
    complete = m.download(tx['id'])
    assert complete['status'] == 'verified' and complete['downloaded'] == len(payload)
    assert complete['error'] is None and complete['next_retry'] is None
    assert m.package_path(complete).read_bytes() == payload
    assert m.preflight(tx['id'])['error'] is None


def test_imported_local_exe_matches_the_published_checksum_without_a_release_root(manager,monkeypatch):
    payload=b'local-desktop-exe';_,_,checksum=published(payload)
    m=desktop(manager,Checksum([release('3.3.0',size=len(payload))],checksum=checksum));assert m.check()['status']=='download_available';imported=m.prepare_local()
    monkeypatch.setattr(m,'trust',lambda:(_ for _ in ()).throw(AssertionError('发布根不应参与 GitHub 安装')))
    assert m.upload(imported['id'],0,payload)['status']=='verified'
    checked=m.preflight(imported['id'])
    assert '升级包尚未完整验证' not in checked.get('blockers',[]) and checked['status'] in ('waiting_confirmation','preflight_blocked')


@pytest.mark.parametrize('change',['web','source','client'])
def test_github_install_stays_on_the_local_desktop_executable(manager,change):
    name,payload,checksum=published(b'local-desktop-exe')
    m=desktop(manager,Checksum([release('3.3.0',size=len(payload))],checksum=checksum));assert m.check()['status']=='download_available'
    if change=='web':m.component='web'
    elif change=='source':m.form='source'
    else:m.client_only=True
    with pytest.raises(UpgradeError) as error:m.prepare_local()
    assert error.value.code=='TARGET' and m.current() is None


def test_tampered_download_address_is_not_installed(manager):
    name,payload,checksum=published(b'local-desktop-exe')
    m=desktop(manager,Checksum([release('3.3.0',size=len(payload))],checksum=checksum));assert m.check()['status']=='download_available'
    record=m.state.get('check');record['release']['download_url']+='.evil';m.state.put('check',record)
    with pytest.raises(UpgradeError) as error:m.prepare_local()
    assert error.value.code=='TARGET' and m.current() is None and not m.fetcher.reads
