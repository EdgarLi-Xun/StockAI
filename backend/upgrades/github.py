"""Public GitHub release discovery.

Desktop EXE installs may use the official asset plus its published SHA-256.
That checksum is not a publisher signature and does not authorize Web or Hub replacement.
"""
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
            candidates.append((version,{'component':target['component'],'version':value,'channel':release_channel,'published':item.get('published_at'),'release_url':base+'/tag/'+quote(tag,safe=''),'download_url':asset_url(repo,value,name),'asset_name':name,'size':asset['size'],'signature_verified':False}))
        if len(entries)<100:return max(candidates,key=lambda item:item[0])[1] if candidates else None
    raise UpgradeError('GITHUB_INCOMPLETE','发行记录分页尚未读取完整，未判断为最新')


ASSET_HOSTS=frozenset({'github.com','release-assets.githubusercontent.com','objects.githubusercontent.com','github-releases.githubusercontent.com'})


def asset_url(repo,version,name):
    return f'https://github.com/{repo}/releases/download/{quote("v"+version,safe="")}/{quote(name,safe="")}'


def github_asset(parsed):
    """Official release asset or GitHub's own download redirect. No other github.com path."""
    host=parsed.hostname
    if parsed.port not in (None,443) or parsed.username or parsed.password or parsed.fragment:return False
    if host=='github.com':return not parsed.query and re.fullmatch(r'/[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9_][A-Za-z0-9_.-]*/releases/download/[^/]+/StockAI-[A-Za-z0-9._-]+',parsed.path) is not None
    if host in ASSET_HOSTS:
        path=parsed.path or ''
        return path.startswith('/') and '..' not in path and '\\' not in path and len(path)<2048
    return False


def parse_checksum(raw,name):
    """GNU checksum line published beside the asset: lowercase SHA-256, two spaces, exact file name."""
    if not isinstance(raw,(bytes,bytearray)) or not 0<len(raw)<=256:raise UpgradeError('PACKAGE')
    try:text=bytes(raw).decode('ascii')
    except UnicodeError:raise UpgradeError('PACKAGE') from None
    if '\r' in text or text.count('\n')>1:raise UpgradeError('PACKAGE')
    line=text[:-1] if text.endswith('\n') else text
    match=re.fullmatch(r'([0-9a-f]{64})  ([A-Za-z0-9][A-Za-z0-9._-]{0,180})',line)
    if not match or match.group(2)!=name:raise UpgradeError('PACKAGE','校验文件与发行包名称不一致')
    return match.group(1)
