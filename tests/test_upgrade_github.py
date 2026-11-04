"""GitHub discovery stays separate from signed acceptance. Desktop EXE may install a checksum-matched asset."""
from datetime import datetime,timedelta,timezone
import hashlib
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
