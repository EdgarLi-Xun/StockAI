import copy
from dataclasses import replace
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import sqlite3
import subprocess
import zipfile
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from backend.sync.protocol import canonical,digest
from backend.upgrades.trust import Trust,UpgradeError,public_key,sign
from backend.upgrades.state import State
from backend.upgrades.versions import Version,upgrade_order
from backend.upgrades.manager import Manager
from backend.upgrades.checkpoint import Checkpoints
from backend.upgrades.runner import Runner
from test_cloud import cloud


def root(private,version=1):
    entry=public_key(private);ident=digest(entry)
    return {'_type':'stockai-root/1','version':version,'expires':(datetime.now(timezone.utc)+timedelta(days=30)).isoformat(),'keys':{ident:entry},'roles':{r:{'keyids':[ident],'threshold':1} for r in ('root','releases')}}


def release(private,**changes):
    value={'_type':'stockai-release/1','component':'web','version':'3.3.0-preview.1','channel':'preview','platform':'linux','architecture':'x64','form':'source','sequence':1,'published':datetime.now(timezone.utc).isoformat(),'expires':(datetime.now(timezone.utc)+timedelta(days=3)).isoformat(),'minimum_source':'3.0.0','accepts':{k:{'min':1,'max':3} for k in ('schema','api','sync')},'provides':{'schema':3,'api':1,'sync':1},'migration':'stockai-ordered/1','rollback':{'prewrite':True,'postwrite':False},'package':{'size':10,'sha256':'a'*64,'url':'https://releases.example.test/payload.zip'}}
    value.update(changes);return sign(value,[private])


@pytest.fixture
def manager(cloud,tmp_path):
    store,keys,*_=cloud;private=Ed25519PrivateKey.generate();path=tmp_path/'root.json';path.write_bytes(canonical(sign(root(private),[private])))
    m=Manager(store.cfg,keys,store=store,root_file=path);return m,private


def test_semver_and_dynamic_order():
    assert Version('1.0.0-preview.9')<Version('1.0.0-preview.10')<Version('1.0.0')
    assert Version('2.0.0')>Version('1.99.99')
    with pytest.raises(ValueError):Version('1.0.0-01')
    current={'web':{'api':1},'hub':{'api':1}}
    result=upgrade_order({'web':{'provides':{'api':2},'peers':{'hub':{'api':{'min':1,'max':2}}}},'hub':{'provides':{'api':2},'peers':{'web':{'api':{'min':2,'max':2}}}}},current)
    assert result['order']==['web','hub'] and not result['maintenance_all']


def test_signatures_expiry_replay_and_rotating_root(manager):
    m,key=manager;trust=m.trust();envelope=release(key);trust.release(envelope,m.target)
    bad=copy.deepcopy(envelope);bad['signed']['package']['size']=1
    with pytest.raises(UpgradeError,match='签名'):trust.release(bad,m.target)
    with pytest.raises(UpgradeError):trust.release(release(key,sequence=0),m.target)
    future=release(key,sequence=2);trust.release(future,m.target)
    with pytest.raises(UpgradeError):trust.release(envelope,m.target)
    with pytest.raises(UpgradeError):trust.release(release(key,sequence=3,expires='2020-01-01T00:00:00+00:00'),m.target)
    key2=Ed25519PrivateKey.generate();next_root=root(key2,2)
    with pytest.raises(UpgradeError):trust.rotate([sign(next_root,[key2])])
    trust.rotate([sign(next_root,[key,key2])]);trust.release(release(key2,sequence=3),m.target)
    with pytest.raises(UpgradeError):trust.release(release(key,sequence=4),m.target)


@pytest.mark.parametrize('order',[(2,1,0),(0,1,2),(1,2,0)])
def test_release_lists_are_order_independent_and_repeatable(manager,monkeypatch,order):
    m,key=manager
    candidates=[release(key,version=f'3.3.0-preview.{i}',sequence=i) for i in (1,2,3)]
    feed={'releases':[candidates[i] for i in order]}
    m.source='https://releases.example.test/index.json'
    monkeypatch.setattr(m.fetcher,'metadata',lambda source:feed)
    assert m.check()['release']['version']=='3.3.0-preview.3'
    assert m.state.get('release-floor:web:preview')=={'sequence':3,'hash':digest(candidates[2]['signed'])}
    feed['releases'].reverse()
    assert m.check()['status']=='available'
    assert m.prepare(feed)['target_version']=='3.3.0-preview.3'


@pytest.mark.parametrize('order',[(2,1,0),(0,1,2),(1,2,0)])
def test_prepare_release_lists_without_prior_check(manager,order):
    m,key=manager
    candidates=[release(key,version=f'3.3.0-preview.{i}',sequence=i) for i in (1,2,3)]
    assert m.prepare({'releases':[candidates[i] for i in order]})['target_version']=='3.3.0-preview.3'
    assert m.state.get('release-floor:web:preview')['sequence']==3


def test_candidate_validation_and_other_channels_do_not_advance_floor(manager,monkeypatch):
    m,key=manager;selected=release(key,sequence=2)
    assert m.trust().candidate(selected,m.target)==selected['signed']
    assert m.state.get('release-floor:web:preview') is None
    feed={'releases':[release(key,channel='stable',version='4.0.0',sequence=99),selected,release(key,version='3.3.0-preview.0')]}
    m.source='https://releases.example.test/index.json'
    monkeypatch.setattr(m.fetcher,'metadata',lambda source:feed)
    assert m.check()['release']==selected['signed']
    assert m.state.get('release-floor:web:preview')['sequence']==2
    assert m.state.get('release-floor:web:stable') is None


@pytest.mark.parametrize('action',['check','prepare','release'])
@pytest.mark.parametrize('changes',[{'sequence':2,'version':'3.3.0-preview.2'},{'sequence':3,'version':'3.3.0-preview.4'}])
def test_selected_release_still_rejects_replay(manager,monkeypatch,action,changes):
    m,key=manager;accepted=release(key,sequence=3,version='3.3.0-preview.3')
    m.trust().release(accepted,m.target);stale=release(key,**changes)
    if action=='check':
        m.source='https://releases.example.test/index.json'
        monkeypatch.setattr(m.fetcher,'metadata',lambda source:{'releases':[stale]})
        assert m.check()['error']=='REPLAY'
    else:
        with pytest.raises(UpgradeError) as error:
            if action=='prepare':m.prepare({'releases':[stale]})
            else:m.trust().release(stale,m.target)
        assert error.value.code=='REPLAY'
    assert m.state.get('release-floor:web:preview')=={'sequence':3,'hash':digest(accepted['signed'])}


@pytest.mark.parametrize('action',['check','prepare'])
@pytest.mark.parametrize('invalid',['expired','revoked_signer','equivocation'])
def test_rejected_candidate_list_never_advances_floor(manager,monkeypatch,action,invalid):
    m,key=manager;valid=release(key)
    if invalid=='expired':bad=release(key,sequence=2,expires='2020-01-01T00:00:00+00:00');code='EXPIRED'
    elif invalid=='revoked_signer':bad=release(Ed25519PrivateKey.generate(),sequence=2);code='UNTRUSTED'
    else:bad=release(key,version='3.3.0-preview.2');code='REPLAY'
    feed={'releases':[valid,bad]}
    if action=='check':
        m.source='https://releases.example.test/index.json'
        monkeypatch.setattr(m.fetcher,'metadata',lambda source:feed)
        assert m.check()['error']==code
    else:
        with pytest.raises(UpgradeError) as error:m.prepare(feed)
        assert error.value.code==code
    assert m.state.get('release-floor:web:preview') is None


def test_prepare_current_version_does_not_advance_floor(manager):
    m,key=manager
    with pytest.raises(UpgradeError) as error:m.prepare(release(key,version=m.version,sequence=99))
    assert error.value.code=='DOWNGRADE'
    assert m.state.get('release-floor:web:preview') is None


def test_old_version_feed_does_not_hide_known_replay(manager,monkeypatch):
    m,key=manager;old=release(key,version='2.0.0',sequence=1)
    m.source='https://releases.example.test/index.json'
    monkeypatch.setattr(m.fetcher,'metadata',lambda source:{'releases':[old]})
    assert m.check()['status']=='up_to_date'
    assert m.state.get('release-floor:web:preview') is None
    m.trust().release(release(key,sequence=3),m.target)
    assert m.check()['error']=='REPLAY'
    with pytest.raises(UpgradeError) as error:m.trust().release(old,m.target)
    assert error.value.code=='REPLAY'


@pytest.mark.parametrize('changes',[{'form':'exe'},{'component':'hub'},{'platform':'windows'},{'minimum_source':'9.0.0'},{'version':'2.0.0'},{'package':{'size':10,'sha256':'z'*64,'url':'https://x.test/p'}}])
def test_wrong_target_and_malformed_package(manager,changes):
    m,key=manager
    with pytest.raises(UpgradeError):m.prepare(release(key,**changes))
    assert not m.current()


def test_state_rollback_is_detected(tmp_path):
    s=State(tmp_path/'state',b'x'*32);s.put('slot',1);old=s.path.read_bytes();s.put('slot',2);s.path.write_bytes(old)
    with pytest.raises(RuntimeError,match='回退'):s.get('slot')


def test_no_executor_is_waiting_and_check_failure_not_latest(manager):
    m,key=manager;tx=m.prepare(release(key));assert m.prepare(release(key))['id']==tx['id']
    assert m.preflight(tx['id'])['status']=='awaiting_operator'
    assert m.check()['status']=='check_failed'
    assert m.check(automatic=True)['error']=='SOURCE_UNCONFIGURED'
    with pytest.raises(UpgradeError):m.execute(tx['id'],'wrong',None)


def test_package_zip_paths_are_rejected(manager,tmp_path):
    m,key=manager;tx=m.prepare(release(key));bad=tmp_path/'bad.zip'
    with zipfile.ZipFile(bad,'w') as z:z.writestr('../outside.py','bad')
    with pytest.raises(UpgradeError):Runner(m).prepare(tx,bad)
    assert not (m.folder/'outside.py').exists()


def test_real_encrypted_checkpoint_restores_business_and_keeps_control(cloud,tmp_path):
    store,keys,auth,journal,admin,uid=cloud
    tool=Path('.tmp/tools/age/age.exe').resolve();keygen=tool.with_name('age-keygen.exe')
    if not tool.is_file():pytest.skip('real age tool unavailable')
    identity=tmp_path/'identity.txt';raw=subprocess.run([str(keygen)],capture_output=True,check=True).stdout;identity.write_bytes(raw)
    recipient=next(line.split(':',1)[1].strip() for line in raw.decode().splitlines() if line.startswith('# public key:'))
    cfg=replace(store.cfg,age_bin=str(tool),age_recipient=recipient)
    cp=Checkpoints(cfg,identity=identity);point=cp.make('actualcheckpoint')
    store.execute("UPDATE users SET status='disabled' WHERE id=?",(uid,));marker=cfg.erasures/'latest-control-proof';marker.write_text('must survive')
    cp.restore(point)
    assert store.one('SELECT status FROM users WHERE id=?',(uid,))['status']=='active' and marker.read_text()=='must survive'
    assert b'test-cloud-password' not in (cp.root/'actualcheckpoint.age').read_bytes()


def test_incompatible_old_writer_rejected_after_adoption(cloud):
    store,*_=cloud
    from backend.sync.schema import protect
    with store.tx(immediate=True) as c:protect(c)
    with sqlite3.connect(store.path) as c:
        with pytest.raises(sqlite3.OperationalError,match='stockai_write_capability'):c.execute("UPDATE user_settings SET payload_json=payload_json")
    store.execute('UPDATE user_settings SET payload_json=payload_json')


@pytest.mark.parametrize('name',['../escape.py','.. /escape.py','backend/CON.py','backend/file.py.'])
def test_archive_rejects_windows_aliases(name):
    from backend.upgrades.archive import validate_names
    item=zipfile.ZipInfo(name)
    with pytest.raises(UpgradeError):validate_names([item])
