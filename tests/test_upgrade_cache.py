from datetime import datetime,timedelta,timezone
from pathlib import Path
import uuid
import pytest
from backend.upgrades.trust import UpgradeError
from test_cloud import cloud
from test_upgrade_protocol import manager


def transaction(m,days,status='succeeded'):
    ident=uuid.uuid4().hex;when=(datetime.now(timezone.utc)-timedelta(days=days)).isoformat()
    tx={'id':ident,'status':status,'created_at':when,'updated_at':when,'checkpoint':{'id':ident,'available':True}}
    for root,name in (('downloads','payload.zip'),('installations','old-code.py')):
        path=m.folder/root/ident/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_bytes(b'owned update file')
    point=m.folder/'checkpoints'/(ident+'.age');point.parent.mkdir(exist_ok=True);point.write_bytes(b'owned encrypted fixture')
    m.state.put('transaction:'+ident,tx);return tx


def test_retention_keeps_current_last_recovery_recent_and_foreign_data(manager):
    m,_=manager;expired=transaction(m,45);previous=transaction(m,40);recent=transaction(m,3);m.state.put('active',previous['id']);m.state.put('active-installation',{'transaction':previous['id']});m.state.put('history',[{'id':expired['id']}])
    unknown=m.folder/'downloads/not-a-transaction';unknown.mkdir();(unknown/'personal.txt').write_text('keep foreign folder')
    workspace=m.cfg.data/'preserve-user.txt';workspace.write_text('user data');key=m.cfg.key_file.read_bytes()
    plan=m.cleanup();assert plan['status']=='preview' and (m.folder/'checkpoints'/(expired['id']+'.age')).exists()
    result=m.cleanup(execute=True);assert result['status']=='completed'
    assert not (m.folder/'installations'/expired['id']).exists() and not (m.folder/'checkpoints'/(expired['id']+'.age')).exists()
    assert (m.folder/'installations'/previous['id']).is_dir() and not (m.folder/'checkpoints'/(previous['id']+'.age')).exists()
    assert not m.state.get('transaction:'+previous['id'])['checkpoint']['available']
    assert (m.folder/'downloads'/recent['id']).is_dir() and (unknown/'personal.txt').read_text()=='keep foreign folder'
    assert workspace.read_text()=='user data' and m.cfg.key_file.read_bytes()==key
    assert not m.state.get('transaction:'+expired['id'])['checkpoint']['available'] and not m.status()['history'][0]['recovery_available']
    assert m.cleanup(execute=True,automatic=True)==result


def test_unfinished_upgrade_blocks_cache_cleanup(manager):
    m,_=manager;pending=transaction(m,45,'migrating');m.state.put('active',pending['id'])
    assert m.cleanup(execute=True)['status']=='waiting' and (m.folder/'downloads'/pending['id']).exists()


def test_cache_link_cannot_escape_owned_root(manager,tmp_path):
    m,_=manager;expired=transaction(m,45);current=transaction(m,1);m.state.put('active',current['id']);outside=tmp_path/'outside.txt';outside.write_text('must survive');link=m.folder/'installations'/expired['id']/'escape.txt'
    try:link.symlink_to(outside)
    except OSError:pytest.skip('Windows symlink privilege unavailable')
    with pytest.raises(UpgradeError):m.cleanup(execute=True)
    assert outside.read_text()=='must survive'
