"""首次数据来源选择的身份隔离、非破坏性和未完成导入状态。"""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from backend.cloud import resources
from backend.cloud.preferences import get_preferences, save_preferences
from backend.cloud.store import Store
from backend.cloud.workspace_setup import select, status
from backend.public_app import create_app
from backend.sync.engine import Sync
from backend.sync.protocol import SyncError
from test_cloud import cloud, login
from test_sync_protocol import Memory


def test_empty_workspace_requires_explicit_choice_and_remembers_it_per_user(cloud):
    store, keys, auth, _, admin, uid = cloud
    assert status(store, uid)['required']  # 站点预装的公共证券不算个人资料。
    chosen = select(store, uid, {'source': 'independent'})
    assert not chosen['required'] and not chosen['has_data']
    assert status(Store(store.cfg).initialize(), uid)['source'] == 'independent'
    assert status(store, admin)['required']
    # 用户注销时，当前环境的来源选择同样被外键清理。
    store.execute('DELETE FROM users WHERE id=?', (uid,))
    assert not store.one('SELECT * FROM workspace_setup WHERE user_id=?', (uid,))


def test_existing_data_or_custom_preferences_do_not_trigger_initial_setup(cloud):
    store, _, _, _, admin, uid = cloud
    row = resources.save(store, uid, 'topic', {'name': '保留已有课题'})
    assert status(store, uid)['has_data'] and not status(store, uid)['required']
    select(store, uid, {'source': 'independent'})
    assert resources.get(store, uid, 'topic', row['id'])['data']['name'] == '保留已有课题'
    assert status(store, admin)['required']
    prefs = get_preferences(store, admin).model_dump()
    prefs['budget'] = 500
    save_preferences(store, admin, prefs)
    assert status(store, admin)['has_data'] and not status(store, admin)['required']


def test_selecting_remote_source_does_not_claim_data_was_imported(cloud):
    store, keys, _, _, admin, uid = cloud
    for source in ('desktop', 'web', 'publish'):
        chosen = select(store, uid, {'source': source})
        assert chosen['source'] == source and chosen['required']
        assert not chosen['configured'] and not chosen['has_data']
    remote = Memory()
    service = Sync(store, keys, lambda *args: remote)
    resources.save(store, admin, 'topic', {'name': '来自已有环境的课题'})
    service.configure(admin, {'backend': 'webdav', 'url': 'https://storage.example.test'}, create=True)
    service.synchronize(admin, service.preview(admin)['id'])
    service.configure(uid, {'backend': 'webdav', 'url': 'https://storage.example.test', 'recovery': service.recovery(admin)})
    current = status(store, uid)
    assert current['configured'] and current['first_merge']
    assert not resources.listing(store, uid, 'topic')['items']
    with pytest.raises(SyncError, match='已有同步绑定'):
        select(store, uid, {'source': 'independent'})
    service.synchronize(uid, service.preview(uid)['id'])
    assert not status(store, uid)['first_merge']
    assert resources.listing(store, uid, 'topic')['items'][0]['data']['name'] == '来自已有环境的课题'


def test_legacy_choice_keeps_offline_migration_requirements(cloud):
    store, _, _, _, admin, uid = cloud
    with pytest.raises(ValueError, match='管理员'):
        select(store, uid, {'source': 'legacy'})
    assert not status(store, admin)['legacy_available']  # 已有第二个用户。
    store.execute('DELETE FROM users WHERE id=?', (uid,))
    assert status(store, admin)['legacy_available']
    chosen = select(store, admin, {'source': 'legacy'})
    assert chosen['required'] and not chosen['has_data']
    desktop = Store(replace(store.cfg, desktop_mode=True))
    assert not status(desktop, admin)['legacy_available']


def test_workspace_setup_api_requires_verified_user_and_cannot_target_someone_else(cloud):
    store, _, auth, _, admin, uid = cloud
    auth.new_user('pending@example.test', 'test-cloud-password-123')
    with TestClient(create_app(store.cfg)) as client:
        assert client.get('/api/me/workspace-setup').status_code == 401
        login(client, 'pending@example.test')
        assert client.get('/api/me/workspace-setup').status_code == 403
        login(client)
        assert client.get('/api/me/workspace-setup').json()['required']
        for payload in ({'source': 'unknown'}, {'source': []}, {}, {'source': 'independent', 'user_id': admin}):
            assert client.post('/api/me/workspace-setup', json=payload).status_code == 400
        result = client.post('/api/me/workspace-setup', json={'source': 'independent'})
        assert result.status_code == 200 and not result.json()['required']
        assert status(store, admin)['required']
        assert store.one('SELECT source FROM workspace_setup WHERE user_id=?', (uid,))['source'] == 'independent'
