"""Signed source package + real child process + real age recovery, not a fake executor."""
from dataclasses import replace
from pathlib import Path
import json
import subprocess
import zipfile
import pytest
from backend.cloud import resources
from backend.cloud.research_control import control
from backend.sync.protocol import digest
from backend.upgrades.checkpoint import Checkpoints
from backend.upgrades.runner import Runner
from backend.upgrades.trust import UpgradeError
from test_cloud import cloud
from test_upgrade_protocol import manager,release


def test_built_frontend_identity_matches_real_output():
    root=Path('frontend/dist-public')
    if not (root/'version.json').is_file():pytest.skip('frontend build unavailable')
    build=json.loads((root/'version.json').read_bytes())
    from backend.cloud import VERSION
    assert build['version']==VERSION
    assert {'index.html','favicon.svg'}<=set(build['files'])
    assert build['files']=={p.relative_to(root).as_posix():digest(p.read_bytes()) for p in root.rglob('*') if p.is_file() and p.name!='version.json'}


def materials(m,tmp_path):
    age=Path('.tmp/tools/age/age.exe').resolve()
    if not age.is_file():pytest.skip('real age binary unavailable')
    raw=subprocess.run([str(age.with_name('age-keygen.exe'))],capture_output=True,check=True).stdout;identity=tmp_path/'recovery-identity.txt';identity.write_bytes(raw)
    recipient=next(v.split(':',1)[1].strip() for v in raw.decode().splitlines() if v.startswith('# public key:'))
    m.cfg=replace(m.cfg,age_bin=str(age),age_recipient=recipient);m.checkpoints=Checkpoints(m.cfg,identity=identity)


def package(tmp_path,version='3.3.0-preview.1'):
    from backend.cloud import VERSION
    target=tmp_path/'signed-source.zip';root=Path.cwd()
    files={}
    with zipfile.ZipFile(target,'w',zipfile.ZIP_DEFLATED) as output:
        for folder in ('backend','config','frontend/dist-public'):
            for path in (root/folder).rglob('*'):
                if path.is_file() and '__pycache__' not in path.parts and path.suffix not in ('.pyc','.sqlite3','.age'):
                    name=path.relative_to(root).as_posix();data=path.read_bytes()
                    if name=='backend/cloud/__init__.py' or name.startswith('frontend/dist-public/assets/') and name.endswith('.js'):data=data.replace(VERSION.encode(),version.encode())
                    if name!='frontend/dist-public/version.json':files[name]=data
        build={'version':version,'files':{name.removeprefix('frontend/dist-public/'):digest(data) for name,data in files.items() if name.startswith('frontend/dist-public/')}}
        files['frontend/dist-public/version.json']=json.dumps(build).encode()
        for name,data in files.items():output.writestr(name,data)
    return target


def ready(m,key,tmp_path):
    materials(m,tmp_path);archive=package(tmp_path);data=archive.read_bytes();tx=m.prepare(release(key,package={'size':len(data),'sha256':digest(data),'url':'https://releases.example.test/source.zip'}));m.upload(tx['id'],0,data);return tx


def test_real_signed_source_upgrade_checks_exact_version_and_does_no_work(manager,tmp_path):
    m,key=manager;uid=m.store.one("SELECT id FROM users WHERE role='user'")['id'];row=resources.save(m.store,uid,'topic',{'name':'existing evidence'})
    tx=ready(m,key,tmp_path);result=m.execute(tx['id'],tx['confirmation'],Runner(m))
    assert result['status']=='succeeded',result
    assert result['actual_version']=='3.3.0-preview.1' and result['health']['paid_calls']==0
    assert result['health']['orders_executed']==0 and result['health']['mail_sent']==0
    assert resources.get(m.store,uid,'topic',row['id'])['data']['name']=='existing evidence'
    assert m.preflight(tx['id'],executor=True)['status']=='succeeded'
    assert m.current()['writes_opened']
    # 已开放写入后首先保存当前状态，然后明确阻止旧检查点覆盖新资料。
    resources.save(m.store,uid,'topic',{'name':'new version record'})
    with pytest.raises(UpgradeError,match='不能直接'):m.rollback(tx['id'],Runner(m),tx['confirmation'])
    assert m.store.one("SELECT id FROM research_objects WHERE json_extract(payload_json,'$.name')='new version record'")


def test_health_failure_restores_real_checkpoint_and_reapplies_latest_delete(manager,tmp_path):
    m,key=manager;uid=m.store.one("SELECT id FROM users WHERE role='user'")['id'];row=resources.save(m.store,uid,'topic',{'name':'delete after backup'})
    tx=ready(m,key,tmp_path)
    class Fault(Runner):
        def probe(self,transaction,installation):
            result=super().probe(transaction,installation)
            resources.delete(m.store,uid,'topic',row['id'])
            ctl=control(m.store,m.keys);ctl.reserve(uid,'post-checkpoint-sent','profile','assistant',{'daily':10,'monthly':100});ctl.transition('post-checkpoint-sent','sent')
            result['archives']=False;return result
    result=m.execute(tx['id'],tx['confirmation'],Fault(m))
    assert result['status']=='rolled_back',result
    assert not m.store.one('SELECT id FROM research_objects WHERE id=?',(row['id'],))
    assert control(m.store,m.keys).rows(uid)[-1]['state']=='sent'
    assert m.store.one("SELECT value FROM cloud_meta WHERE key='maintenance'")['value']=='0'
    later=resources.save(m.store,uid,'topic',{'name':'new record after rollback'})
    assert m.rollback(tx['id'],Runner(m),tx['confirmation'])['status']=='rolled_back'
    assert resources.get(m.store,uid,'topic',later['id'])['data']['name']=='new record after rollback'
