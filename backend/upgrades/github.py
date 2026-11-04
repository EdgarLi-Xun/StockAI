"""Public GitHub release discovery. Results authorize manual downloads, never installation."""
import re
from urllib.parse import quote,urlsplit
from backend.cloud import VERSION
from .trust import UpgradeError
from .versions import Version

DEFAULT_SOURCE='https://api.github.com/repos/EdgarLi-Xun/StockAI/releases'


def repository(source):
    try:
        url=urlsplit(source)
        if url.scheme!='https' or url.username or url.password or url.port not in (None,443) or url.query or url.fragment:return None
    except ValueError:return None
    pattern=r'/repos/([^/]+)/([^/]+)/releases/?' if url.hostname=='api.github.com' else r'/([^/]+)/([^/]+?)(?:\.git)?(?:/releases)?/?' if url.hostname=='github.com' else ''
    match=re.fullmatch(pattern,url.path) if pattern else None
    if not match:return None
    owner,name=match.groups()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*',owner) or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*',name):return None
    return owner+'/'+name


def latest(fetcher,repo,target,channel):
    headers={'Accept':'application/vnd.github+json','User-Agent':'StockAI/'+VERSION,'X-GitHub-Api-Version':'2026-03-10'}
    suffix={'desktop':{'exe':'Windows.exe','source':'Windows.zip'},'web':{'source':'Public.zip'},'hub':{'source':'Hub.zip'}}.get(target['component'],{}).get(target['form'])
    if not suffix or target['architecture']!='x64':return None
    candidates=[]
    for page in range(1,6):
        entries=fetcher.metadata(f'https://api.github.com/repos/{repo}/releases?per_page=100&page={page}',headers)
        if not isinstance(entries,list) or len(entries)>100:raise UpgradeError('GITHUB_METADATA')
        for item in entries:
            if not isinstance(item,dict) or type(item.get('draft')) is not bool or type(item.get('prerelease')) is not bool:raise UpgradeError('GITHUB_METADATA')
            if item['draft']:continue
            tag=item.get('tag_name')
            if not isinstance(tag,str):raise UpgradeError('GITHUB_METADATA')
            value=tag[1:] if tag.startswith(('v','V')) else tag
            try:version=Version(value)
            except ValueError:continue
            release_channel='preview' if item['prerelease'] or version.pre else 'stable'
            if release_channel!=channel:continue
            assets=item.get('assets')
            if not isinstance(assets,list):raise UpgradeError('GITHUB_METADATA')
            name=f'StockAI-{value}-{suffix}'
            asset=next((a for a in assets if isinstance(a,dict) and a.get('name')==name and a.get('state')=='uploaded' and type(a.get('size')) is int and a['size']>0),None)
            if not asset:continue
            base=f'https://github.com/{repo}/releases'
            candidates.append((version,{'component':target['component'],'version':value,'channel':release_channel,'published':item.get('published_at'),'release_url':base+'/tag/'+quote(tag,safe=''),'download_url':base+'/download/'+quote(tag,safe='')+'/'+quote(name,safe=''),'asset_name':name,'size':asset['size'],'signature_verified':False}))
        if len(entries)<100:return max(candidates,key=lambda item:item[0])[1] if candidates else None
    raise UpgradeError('GITHUB_INCOMPLETE','发行记录分页尚未读取完整，未判断为最新')
