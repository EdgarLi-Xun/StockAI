"""本环境的数据来源选择；不随个人业务同步，也不改写已有研究资料。"""
import json

from .preferences import Preferences
from .store import stamp
from backend.sync.protocol import SyncError


SOURCES = frozenset(('independent', 'desktop', 'web', 'publish', 'legacy'))
PERSONAL_TABLES = (
    'user_watchlist', 'model_profiles', 'personal_reports', 'sim_accounts',
    'personal_backtests', 'personal_jobs', 'research_objects', 'research_experiments',
)


def status(store, uid, connection=None):
    if connection is None:
        with store.tx() as connection:
            return status(store, uid, connection)
    c = connection
    account = store.active(uid, c=c)
    if not account:
        raise ValueError('账号不可用')
    saved = c.execute('SELECT source FROM workspace_setup WHERE user_id=?', (uid,)).fetchone()
    binding = c.execute('SELECT payload_json FROM sync_bindings WHERE user_id=?', (uid,)).fetchone()
    has_data = any(c.execute(f'SELECT 1 FROM {table} WHERE user_id=? LIMIT 1', (uid,)).fetchone() for table in PERSONAL_TABLES)
    prefs = c.execute('SELECT payload_json FROM user_settings WHERE user_id=?', (uid,)).fetchone()
    has_data = has_data or bool(prefs and Preferences.model_validate_json(prefs[0]) != Preferences())
    if account['role'] != 'admin':
        legacy_reason = '旧版本地数据迁移需由站点管理员停服执行。'
    elif store.cfg.desktop_mode:
        legacy_reason = '旧版本地数据需先迁入独立 Web 站点，再同步到桌面。'
    elif c.execute("SELECT 1 FROM cloud_meta WHERE key='legacy_migration'").fetchone():
        legacy_reason = '本站已经完成旧版迁移，请继续使用现有资料。'
    elif c.execute('SELECT COUNT(*) FROM users').fetchone()[0] != 1 or has_data or binding:
        legacy_reason = '旧版迁移要求本站只有初始化管理员，且尚未创建个人研究资料或同步绑定。'
    else:
        legacy_reason = ''
    source = saved['source'] if saved else None
    return {
        'source': source,
        'required': not has_data and not binding and source != 'independent',
        'has_data': bool(has_data),
        'configured': bool(binding),
        'first_merge': bool(binding and json.loads(binding[0]).get('first_merge')),
        'legacy_available': not legacy_reason,
        'legacy_reason': legacy_reason,
    }


def select(store, uid, value):
    if set(value) != {'source'} or not isinstance(value['source'], str) or value['source'] not in SOURCES:
        raise ValueError('请选择有效的数据来源')
    with store.user_lock(uid), store.tx(immediate=True) as c:
        current = status(store, uid, c)
        if current['configured']:
            raise SyncError('CONFLICT', '已有同步绑定，请先在同步设置中处理，不能通过初始化选择替换绑定')
        if value['source'] == 'legacy' and not current['legacy_available']:
            raise ValueError(current['legacy_reason'])
        c.execute(
            'INSERT INTO workspace_setup VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET source=excluded.source,updated_at=excluded.updated_at',
            (uid, value['source'], stamp()),
        )
        return status(store, uid, c)
