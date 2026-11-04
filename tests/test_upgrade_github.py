"""GitHub discovery stays separate from signed package acceptance and execution."""
from datetime import datetime,timedelta,timezone
import pytest
from backend.upgrades.github import DEFAULT_SOURCE,repository,latest
from backend.upgrades.manager import Manager
from backend.upgrades.trust import UpgradeError
from test_cloud import cloud
from test_upgrade_protocol import manager


def target(component='desktop',form='exe'):
    return {'component':component,'form':form,'architecture':'x64'}


def release(version='3.3.0-preview.1',*,suffix='Windows.exe',**changes):
    value={'tag_name':'v'+version,'draft':False,'prerelease':'-' in version,'published_at':'2026-11-04T00:00:00Z','assets':[{'name':f'StockAI-{version}-{suffix}','state':'uploaded','size':1234,'browser_download_url':'https://unrelated.example.test/do-not-use.exe'}]}
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


@pytest.mark.parametrize('component,form,suffix',[('desktop','exe','Windows.exe'),('desktop','source','Windows.zip'),('web','source','Public.zip'),('hub','source','Hub.zip')])
def test_only_the_matching_component_and_installation_form_is_offered(component,form,suffix):
    data=release();data['assets']=[release(suffix=value)['assets'][0] for value in ('Windows.exe','Windows.zip','Public.zip','Hub.zip')]
    result=latest(Pages([data]),'EdgarLi-Xun/StockAI',target(component,form),'preview')
    assert result['asset_name'].endswith(suffix) and result['component']==component


def test_missing_packages_and_prereleases_do_not_appear_on_the_stable_channel():
    assert latest(Pages([release(),release('4.0.0',prerelease=True),release('3.3.0',suffix='Public.zip')]),'owner/repo',target(),'stable') is None
    assert latest(Pages([release(assets=[{'name':'StockAI-3.3.0-preview.1-Windows.exe','size':1234,'state':'new'}])]),'owner/repo',target(),'preview') is None


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
    m,key=manager;m.source=DEFAULT_SOURCE;m.version='3.2.0-preview.3';m.target.update(component='desktop',form='exe');m.fetcher=Pages(data)
    check=m.check()
    assert check['status']==status and check['error']==error
    if error:
        assert check['next_retry'] and m.check(automatic=True)==check
    assert m.current() is None
