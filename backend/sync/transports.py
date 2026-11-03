from __future__ import annotations
import base64
import json
import re
import uuid
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from .network import HTTPS
from .protocol import SyncError,canonical,digest,HASH,ID


def checked(reply,allowed=(200,201,204)):
    if reply.status in allowed:return reply
    code={401:'AUTH',403:'AUTH',404:'MISSING',409:'CONFLICT',412:'CONFLICT',413:'STORAGE_FULL',507:'STORAGE_FULL',429:'RATE_LIMIT'}.get(reply.status,'INCOMPATIBLE' if reply.status in (405,501) else 'OFFLINE')
    raise SyncError(code)


def strong_etag(reply):
    value=reply.headers.get('etag','')
    if not re.fullmatch(r'"[^"\r\n]{1,200}"',value):raise SyncError('INCOMPATIBLE','同步服务没有提供强 ETag，已停止双向写入')
    return value


def safe_path(path):
    if not re.fullmatch(r'(?:objects|heads)/[0-9a-f]{64}\.bin|head\.bin',path):raise SyncError('CORRUPT')
    return path


class DAV:
    def __init__(self,client,space,username='',password=''):
        if not ID.fullmatch(space):raise SyncError('CORRUPT')
        self.http=client;self.space=space
        self.headers={'Authorization':'Basic '+base64.b64encode((username+':'+password).encode()).decode()}
        self.prefix='/stockai-sync/'+space

    def read(self,path):
        reply=checked(self.http.request('GET',self.prefix+'/'+safe_path(path),headers=self.headers),allowed=(200,))
        self.server_time=parsedate_to_datetime(reply.headers['date']) if reply.headers.get('date') else None
        return reply.body,strong_etag(reply)

    def put(self,path,body,etag=None,create=False):
        headers={**self.headers,'Content-Type':'application/octet-stream'}
        if create:headers['If-None-Match']='*'
        elif etag:headers['If-Match']=etag
        else:raise SyncError('INCOMPATIBLE','拒绝无条件覆盖同步资源')
        reply=checked(self.http.request('PUT',self.prefix+'/'+safe_path(path),body,headers))
        # PUT 可能省略 ETag，重新读且确认本次内容，不把请求已接收当成确认。
        current,tag=self.read(path)
        if current!=body:raise SyncError('CONFLICT')
        return tag

    def initialize(self):
        for path in ('/stockai-sync',self.prefix,self.prefix+'/objects',self.prefix+'/heads'):
            checked(self.http.request('MKCOL',path,headers=self.headers),allowed=(201,405))

    def erase_objects(self,expected=()):
        from urllib.parse import urlsplit,unquote
        reply=checked(self.http.request('PROPFIND',self.prefix+'/',headers={**self.headers,'Depth':'infinity'},limit=8*1024*1024),(207,))
        if b'<!DOCTYPE' in reply.body.upper():raise SyncError('CORRUPT')
        try:root=ET.fromstring(reply.body)
        except ET.ParseError:raise SyncError('CORRUPT') from None
        prefix=unquote(self.http.path)+self.prefix+'/';seen_collection=False
        for node in root.iter('{DAV:}href'):
            value=unquote(urlsplit(node.text or '').path)
            if not value.startswith(prefix):continue
            relative=value[len(prefix):]
            if relative in ('objects','objects/'):seen_collection=True
            if relative.startswith('objects/') and not relative.endswith('/'):
                safe_path(relative);checked(self.http.request('DELETE',self.prefix+'/'+relative,headers=self.headers),(200,204,404))
                if self.http.request('GET',self.prefix+'/'+relative,headers=self.headers).status!=404:raise SyncError('OFFLINE','远端尚未确认业务密文删除')
        if not seen_collection:raise SyncError('INCOMPATIBLE','远端未提供可验证的对象目录，清理保持等待')
        for op_hash in expected:
            path='objects/'+op_hash+'.bin';safe_path(path)
            if self.http.request('GET',self.prefix+'/'+path,headers=self.headers).status!=404:raise SyncError('OFFLINE','仍有已知业务密文未删除')
        return {'deleted':True,'control_retained':True}

    def probe(self):
        probe='/stockai-probe-'+uuid.uuid4().hex;file=probe+'/condition.bin';created=False
        try:
            checked(self.http.request('MKCOL',probe,headers=self.headers),allowed=(201,));created=True
            data=uuid.uuid4().bytes
            checked(self.http.request('PUT',file,data,{**self.headers,'If-None-Match':'*'}))
            first=checked(self.http.request('GET',file,headers=self.headers),(200,));tag=strong_etag(first)
            if first.body!=data:raise SyncError('INCOMPATIBLE')
            if self.http.request('PUT',file,b'wrong',{**self.headers,'If-Match':'"deliberately-wrong"'}).status!=412:raise SyncError('INCOMPATIBLE','WebDAV 忽略条件更新，未启用双向写入')
            unchanged=checked(self.http.request('GET',file,headers=self.headers),(200,))
            if unchanged.body!=data or strong_etag(unchanged)!=tag:raise SyncError('INCOMPATIBLE')
            checked(self.http.request('PUT',file,b'confirmed',{**self.headers,'If-Match':tag}))
            last=checked(self.http.request('GET',file,headers=self.headers),(200,))
            if last.body!=b'confirmed' or strong_etag(last)==tag:raise SyncError('INCOMPATIBLE')
            properties=self.http.request('PROPFIND','/',body=b'<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/><d:quota-available-bytes/></d:prop></d:propfind>',headers={**self.headers,'Depth':'0','Content-Type':'application/xml'},limit=65536)
            checked(properties,(207,))
            available=None
            if b'<!DOCTYPE' in properties.body.upper():raise SyncError('CORRUPT')
            try:
                xml=ET.fromstring(properties.body)
                for node in xml.iter('{DAV:}quota-available-bytes'):
                    if node.text and node.text.isdigit():available=int(node.text)
            except ET.ParseError:raise SyncError('INCOMPATIBLE') from None
            return {'read':True,'write':True,'conditional_write':True,'directory':True,'lock_required':False,'available_bytes':available}
        finally:
            if created:
                # 删除范围严格限定为本次生成的探测目录，不触碰既有文件。
                try:self.http.request('DELETE',file,headers=self.headers);self.http.request('DELETE',probe,headers=self.headers)
                except SyncError:pass


class Hub:
    def __init__(self,client,space,token):
        if not ID.fullmatch(space):raise SyncError('CORRUPT')
        self.http=client;self.space=space;self.headers={'Authorization':'Bearer '+token};self.prefix='/v1/spaces/'+space

    def read(self,path):
        reply=checked(self.http.request('GET',self.prefix+'/files/'+safe_path(path),headers=self.headers),(200,))
        self.server_time=parsedate_to_datetime(reply.headers['date']) if reply.headers.get('date') else None
        return reply.body,strong_etag(reply)

    def put(self,path,body,etag=None,create=False):
        headers={**self.headers,'Content-Type':'application/octet-stream'}
        if create:headers['If-None-Match']='*'
        elif etag:headers['If-Match']=etag
        else:raise SyncError('INCOMPATIBLE')
        reply=checked(self.http.request('PUT',self.prefix+'/files/'+safe_path(path),body,headers),(200,201))
        return strong_etag(reply)

    def initialize(self):pass
    def probe(self):
        reply=checked(self.http.request('GET',self.prefix+'/status',headers=self.headers),(200,))
        value=json.loads(reply.body)
        if value.get('protocol')!=1 or not value.get('conditional_write'):raise SyncError('INCOMPATIBLE')
        return value

    def api(self,path,value=None,method='POST'):
        reply=checked(self.http.request(method,path,canonical(value) if value is not None else b'',{**self.headers,'Content-Type':'application/json'}),(200,201))
        return json.loads(reply.body)

    def changes(self,cursor,limit=50):
        value=self.api(self.prefix+'/changes?cursor='+str(cursor)+'&limit='+str(limit),method='GET')
        if value.get('protocol')!=1 or value.get('cursor')!=cursor or type(value.get('next_cursor')) is not int or value['next_cursor']<cursor or not isinstance(value.get('items'),list) or len(value['items'])>limit:raise SyncError('ROLLBACK')
        previous=cursor
        for item in value['items']:
            if type(item.get('seq')) is not int or not previous<item['seq']<=value['next_cursor']:raise SyncError('CORRUPT')
            safe_path(item['path']);previous=item['seq']
        return value

    def acknowledge(self,cursor):return self.api(self.prefix+'/changes/ack',{'cursor':cursor})

    def grant(self,key,generation=1):
        from .protocol import seal
        value=self.api(self.prefix+'/grants',{})
        challenge=seal(key,self.space,{'nonce':value['nonce']},'device-grant',generation).decode()
        self.api(self.prefix+'/grants/'+value['id'],{'challenge':challenge})
        return {k:v for k,v in value.items() if k!='nonce'}


def client_for(store,config):
    return HTTPS(config['url'],allow_private=config.get('allow_private',False),private_networks=getattr(store.cfg,'sync_private_networks',()),desktop=store.cfg.desktop_mode,ca_file=getattr(store.cfg,'sync_ca_file',None))


def transport(store,config,secrets):
    client=client_for(store,config)
    if config['backend']=='webdav':return DAV(client,config['space_id'],secrets.get('username',''),secrets.get('password',''))
    return Hub(client,config['space_id'],secrets['token'])


def provision(store,config,value,material,create):
    from .protocol import open_sealed,keyring
    hub=Hub(client_for(store,config),'temporary',str(value.get('token','')))
    if value.get('email'):
        hub.headers={'Authorization':'Bearer '+hub.api('/v1/auth/login',{'email':value['email'],'password':value.get('password','')})['token']}
    if create:return hub.api('/v1/spaces',{'name':config['device_name']})
    challenge=hub.api('/v1/bind/challenge',{'code':value.get('grant','')})
    if challenge['space_id']!=material['space_id']:raise SyncError('KEY')
    proof=open_sealed(keyring(material),material['space_id'],challenge['challenge'].encode(),'device-grant')['nonce']
    return hub.api('/v1/bind',{'code':value.get('grant',''),'proof':proof,'name':config['device_name']})
