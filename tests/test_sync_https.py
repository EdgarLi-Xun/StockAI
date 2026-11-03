"""Real local TLS sockets and WebDAV semantics, separate from NAS deployment acceptance."""
from datetime import datetime,timedelta,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
import ipaddress
from pathlib import Path
import ssl
import threading
import uuid
import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes,serialization
from cryptography.x509.oid import NameOID
from backend.sync.network import HTTPS
from backend.sync.transports import DAV
from backend.sync.heads import Heads
from backend.sync.protocol import SyncError,digest


@pytest.fixture
def dav(tmp_path):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048);name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'StockAI isolated TLS fixture')]);now=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1)).add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),critical=False).add_extension(x509.BasicConstraints(ca=True,path_length=None),critical=True).sign(key,hashes.SHA256())
    ca=tmp_path/'ca.pem';ca.write_bytes(cert.public_bytes(serialization.Encoding.PEM));private=tmp_path/'key.pem';private.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption()))
    files={'/keep-existing.bin':b'existing private document'};folders={'/'};lock=threading.RLock();behavior={'conditional':True,'weak':False}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def respond(self,status,body=b'',etag=None):
            self.send_response(status);self.send_header('Content-Length',str(len(body)))
            if etag:self.send_header('ETag',etag)
            self.end_headers();self.wfile.write(body)
        def do_MKCOL(self):
            with lock:
                status=405 if self.path in folders else 201;folders.add(self.path);self.respond(status)
        def do_PROPFIND(self):
            from xml.sax.saxutils import escape
            base=self.path.rstrip('/') or '/';depth=self.headers.get('Depth','0')
            paths=[p for p in folders|set(files) if p==base or depth=='infinity' and p.startswith(base+'/')]
            content='<d:multistatus xmlns:d="DAV:">'+''.join('<d:response><d:href>'+escape(p+('/' if p in folders and p!='/' else ''))+'</d:href><d:propstat><d:prop><d:resourcetype>'+('<d:collection/>' if p in folders else '')+'</d:resourcetype></d:prop></d:propstat></d:response>' for p in paths)+'</d:multistatus>'
            self.respond(207,content.encode())
        def do_GET(self):
            with lock:
                data=files.get(self.path)
                if data is None:self.respond(404);return
                self.respond(200,data,('W/' if behavior['weak'] else '')+'"'+digest(data)+'"')
        def do_PUT(self):
            data=self.rfile.read(int(self.headers.get('Content-Length','0')))
            with lock:
                old=files.get(self.path)
                if behavior['conditional'] and ((self.headers.get('If-None-Match')=='*' and old is not None) or (self.headers.get('If-Match') and (old is None or self.headers['If-Match']!='"'+digest(old)+'"'))):self.respond(412);return
                files[self.path]=data;self.respond(201 if old is None else 200,etag='"'+digest(data)+'"')
        def do_DELETE(self):
            with lock:files.pop(self.path,None);folders.discard(self.path);self.respond(204)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler);context=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);context.load_cert_chain(ca,private);server.socket=context.wrap_socket(server.socket,server_side=True)
    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
    client=HTTPS('https://127.0.0.1:'+str(server.server_port),allow_private=True,desktop=True,ca_file=str(ca));remote=DAV(client,uuid.uuid4().hex,'local-fixture','fixture-only-password')
    try:yield remote,files,folders,behavior,ca
    finally:server.shutdown();server.server_close();thread.join(3)


def test_real_tls_webdav_probe_cas_and_no_user_file_changes(dav):
    remote,files,folders,behavior,ca=dav
    result=remote.probe();assert result['conditional_write'] and result['available_bytes'] is None
    assert files=={'/keep-existing.bin':b'existing private document'} and folders=={'/'}
    key=b'k'*32;device=uuid.uuid4().hex;anchor={}
    heads=Heads(remote,key,remote.space,device,lambda:anchor.get('value'),lambda v:anchor.update(value=v));heads.create('real-tls')
    heads.reserve_usage('request-one','2026-11-02','2026-11',{'daily':1,'monthly':2},'assistant')
    with pytest.raises(SyncError,match='上限'):heads.reserve_usage('request-two','2026-11-02','2026-11',{'daily':1,'monthly':2},'assistant')
    assert remote.server_time.tzinfo is not None
    files[remote.prefix+'/head.bin']=b'corrupt'
    with pytest.raises(SyncError):heads.read()


@pytest.mark.parametrize('field',['conditional','weak'])
def test_real_tls_unsafe_dav_stops_writes_and_only_cleans_own_probe(dav,field):
    remote,files,folders,behavior,_=dav;behavior[field]=False if field=='conditional' else True
    with pytest.raises(SyncError):remote.probe()
    assert files=={'/keep-existing.bin':b'existing private document'} and folders=={'/'}


def test_private_network_requires_explicit_owner_and_operator_allowlist(dav):
    remote,*_=dav;url=remote.http.base
    with pytest.raises(SyncError):HTTPS(url,allow_private=False,desktop=True).addresses()
    with pytest.raises(SyncError):HTTPS(url,allow_private=True,desktop=False).addresses()
    assert HTTPS(url,allow_private=True,private_networks=('127.0.0.0/8',)).addresses()==['127.0.0.1']


def test_webdav_erase_verifies_own_ciphertext_disappeared(dav):
    remote,files,*_=dav;remote.initialize();body=b'owned ciphertext fixture';ident=digest(body);remote.put('objects/'+ident+'.bin',body,create=True)
    assert remote.erase_objects([ident])['deleted']
    assert remote.prefix+'/objects/'+ident+'.bin' not in files
    assert files['/keep-existing.bin']==b'existing private document'
