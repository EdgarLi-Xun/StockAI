"""Assistant lookups, ordinary chat and manually applied, project-only proposals."""
import json
import uuid

import pytest
from pydantic import ValidationError

from backend.cloud import assistant, assistant_actions, jobs, models, resources, simulation
from backend.cloud.assistant_lookup import DataQuery, Lookup, search
from backend.cloud.auth import AuthError
from backend.cloud.preferences import get_preferences, save_preferences
from backend.cloud.research_control import summary
from backend.cloud.store import json_text, stamp
from test_cloud import cloud, profile
from test_assistant_conversation import session, submit


def archive(store, uid, *, version=1, date='2026-11-03', facts=None):
    ident, series = uuid.uuid4().hex, uuid.uuid4().hex
    folder = store.cfg.data / 'reports' / uid
    folder.mkdir(parents=True, exist_ok=True)
    markdown, html = folder / (ident + '.md'), folder / (ident + '.html')
    markdown.write_text('# Archived test report\n', encoding='utf-8')
    html.write_text('<p>Archived test report</p>', encoding='utf-8')
    source = {'report_date': date, 'budget_limit': 1000, 'candidates': [], 'excluded': [], **(facts or {})}
    payload = {**source, 'source_facts': source, 'snapshot_id': 'snapshot-' + ident}
    store.execute("INSERT INTO report_series VALUES(?,?,?,?)", (series, uid, series, date))
    store.execute("INSERT INTO personal_reports VALUES(?,?,?,?,?,NULL,'standard',?,?,?,?,?)",
                  (ident, uid, series, version, None, date, stamp(), json_text(payload), str(markdown), str(html)))
    return ident


def run(cloud, message, wire):
    store, keys, *_, uid = cloud
    job = jobs.claim(store, 'lookup-regression')
    assert job and job['id'] == message['job_id']
    jobs.run(store, keys, job, wire)
    return assistant.request_detail(store, uid, message['id'])


def response(answer, citations=None, **extra):
    return json_text({'answer': answer, 'citations': citations or [], **extra}), {'total_tokens': 7}, 'fixture'


def lookup_response(*queries):
    return json_text({'queries': list(queries)}), {'total_tokens': 3}, 'fixture'


def test_latest_report_without_attachments_is_queried_and_cited(cloud):
    store, keys, *_, uid = cloud
    ident = archive(store, uid); profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '帮我分析最近的研究报告')
    sent = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); sent.append(current)
        if len(sent) == 1:
            assert not any(s['kind'] == 'report' for s in current['sources'])
            return lookup_response({'action': 'latest', 'kind': 'report'})
        source = next(s for s in current['sources'] if s['kind'] == 'report')
        assert source['object_id'] == ident and source['facts']['budget_limit'] == 1000
        return response('归档没有合格候选，可结合排除原因和数据时间核对。', ['report:' + ident])
    result = run(cloud, message, wire)
    assert result['status'] == 'success' and result['context']['contexts'] == []
    assert result['context']['retrieved_contexts'] == [{'kind': 'report', 'id': ident}]
    assert result['reply']['lookup_rounds'] == 1 and len(sent) == 2
    assert result['reply']['references'][0]['version'] == 1
    assert summary(store, uid)['daily']['sent'] == 2
    assert len(assistant.request_detail(store, uid, message['id'])['chunks']) == 1
    assert summary(store, uid)['daily']['sent'] == 2  # Re-reading never invokes inference.


def test_search_metadata_then_read_is_scoped_to_owner(cloud):
    store, keys, _, _, admin, uid = cloud
    own = archive(store, uid); other = archive(store, admin); profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '解释我在二零二六年十一月三日的报告')
    sent = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); sent.append(current)
        assert other not in json_text(messages)
        if len(sent) == 1:
            return lookup_response({'action': 'search', 'kind': 'report', 'query': '2026-11-03', 'limit': 5})
        if len(sent) == 2:
            items = current['lookup_results'][0]['items']
            assert [i['id'] for i in items] == [own] and 'payload_json' not in json_text(items)
            assert not any(s['kind'] == 'report' for s in current['sources'])
            return lookup_response({'action': 'read', 'kind': 'report', 'id': own})
        return response('该归档记录没有合格候选，请查看保存的事实。', ['report:' + own])
    assert run(cloud, message, wire)['status'] == 'success'
    assert len(sent) == 3 and summary(store, uid)['daily']['sent'] == 3


def test_foreign_read_and_missing_id_are_indistinguishable(cloud):
    store, keys, _, _, admin, uid = cloud
    other = archive(store, admin); profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '查询我的报告')
    calls = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); calls.append(current)
        if len(calls) == 1:
            return lookup_response({'action': 'read', 'kind': 'report', 'id': other},
                                   {'action': 'read', 'kind': 'report', 'id': 'missing-report'})
        assert [r['error_code'] for r in current['lookup_results']] == ['NOT_FOUND', 'NOT_FOUND']
        assert not any(s['kind'] == 'report' for s in current['sources'])
        return response('没有找到可访问的归档报告，请核对资料。', ['help:reports'])
    result = run(cloud, message, wire)
    assert result['status'] == 'success' and result['context']['retrieved_contexts'] == []


def test_latest_pair_compares_both_versions_without_manual_selection(cloud):
    store, keys, *_, uid = cloud
    left = archive(store, uid, version=1); right = archive(store, uid, version=2); profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '比较最近两份报告')
    calls = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); calls.append(current)
        if len(calls) == 1:
            return lookup_response({'action': 'latest', 'kind': 'report', 'limit': 2})
        assert len([s for s in current['sources'] if s['kind'] == 'report']) == 2
        assert any(s['kind'] == 'comparison' for s in current['sources'])
        return response('报告均没有候选，差异应以归档事实为准。', ['report:' + left, 'report:' + right])
    result = run(cloud, message, wire)
    assert result['status'] == 'success' and len(result['reply']['references']) == 3


def test_large_report_can_read_one_instrument_and_exclude_private_notes(cloud):
    store, keys, *_, uid = cloud
    excluded = [{'instrument_id': 'SH:' + str(600000 + i), 'reasons': ['证据不足']} for i in range(300)]
    target = excluded[-1]['instrument_id']
    ident = archive(store, uid, facts={'excluded': excluded, 'note': 'PRIVATE-NOTE',
                                    'fees': {'note': 'PRIVATE-FEE-NOTE', 'stock_minimum': 5}})
    profile(cloud); message = submit(store, keys, uid, session(store, uid), '查询我的报告中该股票的排除原因')
    calls = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); calls.append(current)
        assert 'PRIVATE-' not in json_text(messages)
        assert len(json_text(messages).encode()) <= 8000
        if len(calls) == 1:
            return lookup_response({'action': 'read', 'kind': 'report', 'id': ident, 'instrument_id': target})
        source = next(s for s in current['sources'] if s['kind'] == 'report')
        assert source['facts']['excluded'] == [excluded[-1]]
        return response('排除记录显示证据不足，请先核对历史资料。', ['report:' + ident])
    assert run(cloud, message, wire)['status'] == 'success'


def test_backtest_and_failed_experiment_are_read_from_saved_results(cloud):
    store, keys, *_, uid = cloud
    instrument = store.one('SELECT id FROM instruments LIMIT 1')['id']
    backtest, exp = uuid.uuid4().hex, uuid.uuid4().hex
    params = json_text({'instrument_id': instrument, 'fast': 5})
    store.execute('INSERT INTO personal_backtests VALUES(?,?,?,?,?,?,?)',
                  (backtest, uid, stamp(), params, json_text({'summary': {'return_pct': -5}, 'validated': False}), 'data-hash', 'strategy'))
    topic = resources.save(store, uid, 'topic', {'name': '失败实验'})['id']
    store.execute("INSERT INTO research_experiments(id,user_id,topic_id,client_key,fingerprint,status,params_json,benchmark_json,created_at,finished_at,error_code) VALUES(?,?,?,?,?,'failed',?,'{}',?,?,?)",
                  (exp, uid, topic, uuid.uuid4().hex, 'fingerprint', params, stamp(), stamp(), 'EVIDENCE_OR_DATA_INSUFFICIENT'))
    profile(cloud); message = submit(store, keys, uid, session(store, uid), '查看我的回测和实验结果')
    calls = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); calls.append(current)
        if len(calls) == 1:
            return lookup_response({'action': 'latest', 'kind': 'backtest'}, {'action': 'latest', 'kind': 'experiment'})
        sources = {s['kind']: s for s in current['sources']}
        assert sources['backtest']['facts']['summary']['return_pct'] == -5
        assert sources['experiment']['experiment_status'] == 'failed'
        assert sources['experiment']['experiment_error_code'] == 'EVIDENCE_OR_DATA_INSUFFICIENT'
        return response('回测尚未验证，实验没有形成结果，需核对证据缺口。', ['backtest:' + backtest, 'experiment:' + exp])
    assert run(cloud, message, wire)['status'] == 'success'


def test_account_lookup_still_requires_model_data_permission(cloud):
    store, keys, *_, uid = cloud; profile(cloud)
    simulation.create_account(store, uid, 'PRIVATE-ACCOUNT', 10000)
    message = submit(store, keys, uid, session(store, uid), '查询我的账户持仓')
    calls = []
    def wire(fixed, messages, key):
        calls.append(messages); assert 'PRIVATE-ACCOUNT' not in json_text(messages)
        return lookup_response({'action': 'search', 'kind': 'account'})
    result = run(cloud, message, wire)
    assert result['status'] == 'permission_required' and result['error_code'] == 'SUMMARY_PERMISSION_REQUIRED'
    assert len(calls) == 1 and result['reply']['source'] == 'permission_notice'
    trades = submit(store, keys, uid, session(store, uid), '查询我的逐笔成交明细')
    assert trades['status'] == 'permission_required' and not trades['job_id']


def test_retrieved_account_history_respects_revocation(cloud):
    store, keys, *_, uid = cloud; p = profile(cloud, allow_simulation_summary=True)
    account = simulation.create_account(store, uid, 'PRIVATE-ACCOUNT', 10000)
    sid = session(store, uid); message = submit(store, keys, uid, sid, '查询我的账户资金')
    calls = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); calls.append(current)
        assert 'PRIVATE-ACCOUNT' not in json_text(messages)
        if len(calls) == 1:
            return lookup_response({'action': 'read', 'kind': 'account', 'id': account['id']})
        return response('账户目前仅有虚拟现金，请结合估值时间理解。', ['account:' + account['id']])
    assert run(cloud, message, wire)['status'] == 'success'
    assert assistant.history(store, uid, sid, models.snapshot(store, uid))[0]
    assert not store.one('SELECT id FROM portfolio_valuation_history WHERE user_id=?', (uid,))
    models.save_profile(store, keys, uid, models.ProfileInput(name='撤回授权', provider='minimax'), p['id'])
    assert assistant.history(store, uid, sid, models.snapshot(store, uid)) == ([], 1)


def test_automatic_research_followup_requeries_and_deletion_omits_history(cloud):
    store, keys, *_, uid = cloud; ident = archive(store, uid); profile(cloud); sid = session(store, uid)
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content'])
        if not any(s['kind'] == 'report' for s in current['sources']):
            if current['question'] == '继续':
                assert current['recent_sources'] == [{'kind': 'report', 'id': ident}]
            return lookup_response({'action': 'read', 'kind': 'report', 'id': ident})
        return response('归档没有候选，需要核对证据状态。', ['report:' + ident])
    first = submit(store, keys, uid, sid, '分析最近的报告')
    assert run(cloud, first, wire)['status'] == 'success'
    follow = submit(store, keys, uid, sid, '继续')
    assert run(cloud, follow, wire)['status'] == 'success'
    store.execute('DELETE FROM personal_reports WHERE id=?', (ident,))
    assert assistant.history(store, uid, sid, models.snapshot(store, uid)) == ([], 2)
    assert assistant.request_detail(store, uid, first['id'])['reply']['references'][0]['source_deleted']


def test_stop_after_lookup_decision_prevents_another_call(cloud):
    store, keys, *_, uid = cloud; profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '查询最近的报告')
    calls = []
    def wire(*args):
        calls.append(True); assistant.stop(store, uid, message['id'])
        return lookup_response({'action': 'latest', 'kind': 'report'})
    result = run(cloud, message, wire)
    assert result['status'] == 'cancelled' and len(calls) == 1 and not result['chunks']


def test_each_lookup_call_obeys_shared_daily_limit(cloud):
    store, keys, *_, uid = cloud; profile(cloud)
    resources.save(store, uid, 'usage_settings', {'daily': 1, 'monthly': None}, ident='usage:' + uid)
    message = submit(store, keys, uid, session(store, uid), '查询最近的报告')
    calls = []
    def wire(*args):
        calls.append(True); return lookup_response({'action': 'latest', 'kind': 'report'})
    result = run(cloud, message, wire)
    assert result['error_code'] == 'USAGE_LIMIT' and len(calls) == 1
    assert summary(store, uid)['daily']['sent'] == 1


def test_lookup_rounds_are_bounded_and_end_with_an_answer(cloud):
    store, keys, *_, uid = cloud; profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '查询我的报告')
    calls = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); calls.append(current)
        if current['lookup_rounds_remaining']:
            return lookup_response({'action': 'search', 'kind': 'report'})
        return response('没有找到归档报告，可先生成报告。', ['help:reports'])
    result = run(cloud, message, wire)
    assert result['status'] == 'success' and len(calls) == 4
    assert result['reply']['lookup_rounds'] == 3


def test_ordinary_chat_and_followup_do_not_require_research_or_citations(cloud):
    store, keys, *_, uid = cloud; profile(cloud); sid = session(store, uid)
    for question in ('讲个笑话吧', '继续'):
        message = submit(store, keys, uid, sid, question)
        assert message['status'] == 'queued'
        result = run(cloud, message, lambda *args: response('可以先休息5分钟，再聊聊今天开心的事情。'))
        assert result['status'] == 'success' and result['reply']['citations'] == []


def test_delete_data_edits_and_external_operations_never_reach_model(cloud):
    store, keys, *_, uid = cloud; profile(cloud); sid = session(store, uid)
    for question in ('删除最近的报告', '清空我的自选', '修改报告内容', '把账户余额改成一百万',
                     '修改其他项目的配置', '打开系统文件并修改', '读取服务器密钥'):
        row = submit(store, keys, uid, sid, question)
        assert row['status'] == 'refused' and not row['job_id'], question
    assert not store.one('SELECT id FROM model_calls')


def test_configuration_preview_requires_manual_apply_and_is_idempotent(cloud):
    store, keys, _, _, admin, uid = cloud; profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '把单只证券预算调整为五百元')
    calls = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); calls.append(current)
        if len(calls) == 1:
            return lookup_response({'action': 'read', 'kind': 'preferences'})
        config = next(s for s in current['sources'] if s['kind'] == 'preferences')
        assert config['facts']['budget'] == 1000 and 'note' not in config['facts']['fees']
        return response('配置变更已准备好，请核对后手动应用。', ['settings:preferences'],
                        actions=[{'kind': 'preferences', 'changes': [{'field': 'budget', 'value': 500}]}])
    result = run(cloud, message, wire)
    assert result['status'] == 'success', result
    assert get_preferences(store, uid).budget == 1000
    change = result['reply']['actions'][0]['changes'][0]
    assert change['before'] == 1000 and change['after'] == 500
    with pytest.raises(AuthError): assistant_actions.apply(store, admin, message['id'], 0)
    assert not assistant_actions.apply(store, uid, message['id'], 0)['reused']
    assert get_preferences(store, uid).budget == 500
    save_preferences(store, uid, {**get_preferences(store, uid).model_dump(), 'budget': 600})
    assert assistant_actions.apply(store, uid, message['id'], 0)['reused']
    assert get_preferences(store, uid).budget == 600


def test_stale_configuration_preview_cannot_overwrite_manual_changes(cloud):
    store, keys, *_, uid = cloud; profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '把预算调整为五百元')
    result = run(cloud, message, lambda *args: response('请核对方案后手动应用。', actions=[
        {'kind': 'preferences', 'changes': [{'field': 'budget', 'value': 500}]}]))
    assert result['status'] == 'success'
    save_preferences(store, uid, {**get_preferences(store, uid).model_dump(), 'morning_time': '09:00'})
    with pytest.raises(AuthError, match='配置已经变化'):
        assistant_actions.apply(store, uid, message['id'], 0)
    assert get_preferences(store, uid).budget == 1000


def test_add_stock_preview_only_adds_membership_and_preserves_existing_data(cloud):
    store, keys, _, _, admin, uid = cloud; profile(cloud)
    sid = session(store, uid)
    def proposal(group):
        message = submit(store, keys, uid, sid, '添加股票到自选')
        result = run(cloud, message, lambda *args: response('请核对股票代码后添加到自选。', actions=[
            {'kind': 'add_stock', 'market': 'SH', 'symbol': '600519', 'group_name': group}]))
        assert result['status'] == 'success', result
        return message, result
    message, result = proposal('长期观察')
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (uid,))
    assistant_actions.apply(store, uid, message['id'], 0)
    stock = store.one("SELECT * FROM user_watchlist WHERE user_id=? AND instrument_id='SH:600519'", (uid,))
    assert stock['group_name'] == '长期观察'
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (admin,))
    again, result = proposal('不应覆盖原分组')
    assert result['reply']['actions'][0]['already_exists']
    assistant_actions.apply(store, uid, again['id'], 0)
    assert store.one("SELECT * FROM user_watchlist WHERE user_id=? AND instrument_id='SH:600519'", (uid,)) == stock


def test_manual_apply_api_requires_confirmation_and_owned_request(cloud):
    from fastapi.testclient import TestClient
    from backend.public_app import create_app
    from test_cloud import login
    store, keys, *_, uid = cloud; profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '把预算调整为五百元')
    result = run(cloud, message, lambda *args: response('请核对配置变更后手动应用。', actions=[
        {'kind': 'preferences', 'changes': [{'field': 'budget', 'value': 500}]}]))
    assert result['status'] == 'success'
    path = '/api/me/assistant/requests/' + message['id'] + '/actions/0/apply'
    with TestClient(create_app(store.cfg)) as client:
        login(client)
        assert client.post(path, json={}).status_code == 422
        assert client.post(path, json={'confirm': False}).status_code == 422
        assert get_preferences(store, uid).budget == 1000
        assert client.post(path, json={'confirm': True}).status_code == 200
        assert get_preferences(store, uid).budget == 500
        assert client.post(path, json={'confirm': True}).json()['reused']
        login(client, 'admin@example.test')
        assert client.post(path, json={'confirm': True}).status_code == 404


@pytest.mark.parametrize('raw', [
    {'queries': [{'action': 'delete', 'kind': 'report', 'id': 'a'}]},
    {'queries': [{'action': 'read', 'kind': 'file', 'id': 'C:/secret'}]},
    {'queries': [{'action': 'search', 'kind': 'report', 'user_id': 'other'}]},
    {'queries': [{'action': 'latest', 'kind': 'report', 'limit': 1000}]},
    {'queries': [{'action': 'read', 'kind': 'preferences', 'id': 'another-user'}]},
    {'queries': [{'action': 'read', 'kind': 'watchlist', 'id': 'another-user'}]},
    {'queries': [{'action': 'search', 'kind': 'watchlist'}]},
])
def test_model_lookup_schema_has_no_external_write_or_cross_user_controls(raw):
    with pytest.raises(ValidationError): Lookup.model_validate(raw)


@pytest.mark.parametrize('changes', [
    [{'field': 'data_path', 'value': 'C:/elsewhere'}],
    [{'field': 'budget', 'value': 2000}],
    [{'field': 'morning_time', 'value': '25:00'}],
    [{'field': 'automatic_ai', 'value': 'true'}],
    [{'field': 'fees.note', 'value': 'arbitrary text'}],
])
def test_configuration_whitelist_and_validation_reject_unsafe_changes(changes):
    from backend.cloud.preferences import Preferences
    with pytest.raises(ValueError):
        assistant_actions.changed_preferences(Preferences().model_dump(), changes)


def listed_stock(store, symbol='600519', name='贵州茅台'):
    from backend.instruments import Rules
    ident = 'SH:' + symbol
    store.execute("INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,'',0,?)",
                  (ident, symbol, 'SH', name, 'stock', Rules().model_dump_json()))
    return ident


def daily_bar(store, ident, ts, close):
    store.execute("INSERT INTO bars(instrument_id,timeframe,ts,open,high,low,close,volume,amount,adjustment,source,source_url,fetched_at,quality,metadata_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                  (ident, '1d', ts, close, close, close, close, 100, close * 100, 'none', 'test', 'https://www.sse.com.cn/test', stamp(), 'valid', '{}'))


def test_declared_holding_uses_saved_quotes_and_does_not_rewrite_group(cloud):
    from backend.cloud import market
    store, keys, _, _, admin, uid = cloud
    profile(cloud); ident = listed_stock(store)
    daily_bar(store, ident, '2026-11-02T15:00:00+08:00', 10)
    daily_bar(store, ident, '2026-11-03T15:00:00+08:00', 11)
    sid = session(store, uid)
    message = submit(store, keys, uid, sid, '添加600519，持仓价8，持仓数量100')
    result = run(cloud, message, lambda *args: response('请核对持仓后添加到自选。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600519', 'group_name': '长期观察', 'cost_price': 8, 'quantity': 100}]))
    assert result['status'] == 'success', result
    position = result['reply']['actions'][0]['position']
    assert position['change_pct'] == 10 and position['market_value'] == 1100
    assert position['daily_pnl'] == 100 and position['daily_return_pct'] == 10
    assert position['total_pnl'] == 300 and position['total_return_pct'] == 37.5
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (uid,))
    assistant_actions.apply(store, uid, message['id'], 0)
    saved = store.one("SELECT * FROM user_watchlist WHERE user_id=? AND instrument_id=?", (uid, ident))
    assert saved['group_name'] == '长期观察' and saved['pinned'] == 0
    assert saved['cost_price'] == '8' and saved['quantity'] == 100
    assert market.watch_rows(store, uid)[0]['position']['market_value'] == 1100
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (admin,))
    again = submit(store, keys, uid, sid, '把600519持仓价改为9，持仓数量改为200')
    updated = run(cloud, again, lambda *args: response('请核对持仓变更。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600519', 'group_name': '不应覆盖原分组', 'cost_price': 9, 'quantity': 200}]))
    assert updated['reply']['actions'][0]['already_exists']
    assert updated['reply']['actions'][0]['previous_cost_price'] == '8'
    assistant_actions.apply(store, uid, again['id'], 0)
    row = store.one("SELECT * FROM user_watchlist WHERE user_id=? AND instrument_id=?", (uid, ident))
    assert row['group_name'] == '长期观察' and row['pinned'] == 0
    assert row['cost_price'] == '9' and row['quantity'] == 200
    stale = submit(store, keys, uid, sid, '把600519持仓价设为10，持仓数量设为100')
    prepared = run(cloud, stale, lambda *args: response('请再次核对持仓。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600519', 'cost_price': 10, 'quantity': 100}]))
    assert prepared['status'] == 'success'
    store.execute('UPDATE user_watchlist SET cost_price=? WHERE user_id=? AND instrument_id=?', ('7', uid, ident))
    with pytest.raises(AuthError, match='持仓记录已经变化'):
        assistant_actions.apply(store, uid, stale['id'], 0)
    assert store.one("SELECT cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id=?", (uid, ident))['cost_price'] == '7'


def test_holding_without_saved_quote_keeps_inputs_and_leaves_results_empty(cloud):
    store, keys, *_, uid = cloud
    profile(cloud); listed_stock(store, '600000', '浦发银行')
    message = submit(store, keys, uid, session(store, uid), '添加600000，持仓价4.85，持仓数量300')
    result = run(cloud, message, lambda *args: response('暂无行情，请先核对持仓。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600000', 'cost_price': '4.85', 'quantity': 300}]))
    position = result['reply']['actions'][0]['position']
    assert position['cost_price'] == '4.85' and position['quantity'] == 300
    assert position['market_price'] is None and position['market_value'] is None
    assert position['daily_pnl'] is None and position['daily_return_pct'] is None
    assert position['total_pnl'] is None and position['total_return_pct'] is None
    assistant_actions.apply(store, uid, message['id'], 0)
    saved = store.one("SELECT cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id='SH:600000'", (uid,))
    assert saved['cost_price'] == '4.85' and saved['quantity'] == 300


@pytest.mark.parametrize('proposal', [
    {'cost_price': 8},
    {'quantity': 100},
    {'cost_price': 0, 'quantity': 100},
    {'cost_price': -1, 'quantity': 100},
    {'cost_price': 1.23456, 'quantity': 100},
    {'cost_price': 8, 'quantity': 0},
])
def test_holding_inputs_must_be_a_valid_pair(cloud, proposal):
    store, *_, uid = cloud
    with pytest.raises(ValueError):
        assistant_actions.prepare(store, uid, [{'kind': 'add_stock', 'market': 'SH', 'symbol': '600519', 'group_name': '我的自选', **proposal}])


def test_price_without_quantity_asks_instead_of_failing(cloud):
    store, keys, *_, uid = cloud
    profile(cloud)
    question = '000911 持仓价 4.85 持仓价格 4.85 添加到自选，如果已经有了则更新一下'
    sid = session(store, uid)
    message = submit(store, keys, uid, sid, question)
    row = store.one('SELECT * FROM assistant_requests WHERE id=?', (message['id'],))
    prompt = assistant.protocol(store, uid, row, {})[0]['messages'][0]['content']
    assert '先追问' in prompt and '半套持仓' in prompt and '不要说无法添加' in prompt
    result = run(cloud, message, lambda *args: response('已准备把000911加入自选。', actions=[
        {'kind': 'add_stock', 'market': 'SZ', 'symbol': '000911', 'cost_price': 4.85, 'quantity': None}]))
    assert result['status'] == 'success', result
    assert result['reply']['actions'] == [] and result['reply']['citations'] == []
    assert '000911' in result['reply']['answer'] and '持仓数量' in result['reply']['answer']
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (uid,))
    floated = submit(store, keys, uid, sid, question)
    floated_result = run(cloud, floated, lambda *args: response('数量按4.85记下。', actions=[
        {'kind': 'add_stock', 'market': 'SZ', 'symbol': '000911', 'cost_price': 4.85, 'quantity': 4.85}]))
    assert floated_result['status'] == 'success', floated_result
    assert floated_result['reply']['actions'] == [] and '持仓数量' in floated_result['reply']['answer']
    whole = submit(store, keys, uid, sid, '添加600519，持仓价8，持仓数量100')
    listed_stock(store)
    whole_result = run(cloud, whole, lambda *args: response('请核对持仓。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600519', 'cost_price': 8, 'quantity': 100.0}]))
    assert whole_result['status'] == 'success', whole_result
    assert whole_result['reply']['actions'][0]['cost_price'] == '8'
    assert whole_result['reply']['actions'][0]['quantity'] == 100


@pytest.mark.parametrize('proposal', [
    {'cost_price': 8, 'quantity': 0},
    {'cost_price': -1, 'quantity': 100},
])
def test_invalid_holding_pair_still_fails_the_turn(cloud, proposal):
    store, keys, *_, uid = cloud
    profile(cloud)
    message = submit(store, keys, uid, session(store, uid), '添加600519，持仓价8，持仓数量100')
    result = run(cloud, message, lambda *args: response('请核对持仓。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600519', 'group_name': '我的自选', **proposal}]))
    assert result['status'] == 'failed' and result['error_code'] == 'INVALID_OUTPUT' and not result['chunks']


def test_missing_holding_reuses_the_earlier_proposal_instead_of_a_manual_guide(cloud):
    store, keys, *_, uid = cloud
    profile(cloud)
    sid = session(store, uid)
    first = submit(store, keys, uid, sid, '添加000911，持仓价4.85，持仓数量300')
    prepared = run(cloud, first, lambda *args: response('请核对持仓后添加到自选。', actions=[
        {'kind': 'add_stock', 'market': 'SZ', 'symbol': '000911', 'cost_price': '4.85', 'quantity': 300}]))
    assert prepared['reply']['actions'][0]['cost_price'] == '4.85'
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (uid,))
    follow = submit(store, keys, uid, sid, '持仓数据没有添加进去')
    row = store.one('SELECT * FROM assistant_requests WHERE id=?', (follow['id'],))
    proto, *_ = assistant.protocol(store, uid, row, {})
    current = json.loads(proto['messages'][-1]['content'])
    assert current['pending_holdings'][0]['symbol'] == '000911'
    assert current['pending_holdings'][0]['quantity'] == 300
    assert not any(item['kind'] == 'help' for item in current['sources'])
    assert not any(item['kind'] == 'watchlist' for item in current['sources'])
    result = run(cloud, follow, lambda *args: response(
        '您可以通过以下步骤将股票添加到自选的持仓中：登录您的投资平台，搜索ST广糖。', ['help:watchlist']))
    assert result['status'] == 'success', result
    action = result['reply']['actions'][0]
    assert action['instrument_id'] == 'SZ:000911' and action['cost_price'] == '4.85' and action['quantity'] == 300
    assert '投资平台' not in result['reply']['answer'] and result['reply']['citations'] == []
    assert not result['reply']['references']
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (uid,))
    assistant_actions.apply(store, uid, follow['id'], 0)
    saved = store.one("SELECT cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id='SZ:000911'", (uid,))
    assert saved['cost_price'] == '4.85' and saved['quantity'] == 300


def test_missing_holding_can_be_recovered_from_the_user_words_when_the_action_omitted_it(cloud):
    store, keys, *_, uid = cloud
    profile(cloud); listed_stock(store)
    sid = session(store, uid)
    first = submit(store, keys, uid, sid, '添加600519，持仓价8，持仓数量100')
    run(cloud, first, lambda *args: response('请先核对代码。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600519'}]))
    assistant_actions.apply(store, uid, first['id'], 0)
    assert store.one("SELECT cost_price FROM user_watchlist WHERE user_id=? AND instrument_id='SH:600519'", (uid,))['cost_price'] is None
    follow = submit(store, keys, uid, sid, '还是没有添加进去')
    result = run(cloud, follow, lambda *args: response('请到自选页面手动填写。'))
    assert result['reply']['actions'][0]['cost_price'] == '8' and result['reply']['actions'][0]['quantity'] == 100
    assistant_actions.apply(store, uid, follow['id'], 0)
    assert store.one("SELECT cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id='SH:600519'", (uid,))['quantity'] == 100


def test_saved_holding_is_not_proposed_again(cloud):
    store, keys, *_, uid = cloud
    profile(cloud); listed_stock(store, '600000', '浦发银行')
    sid = session(store, uid)
    first = submit(store, keys, uid, sid, '添加600000，持仓价4.85，持仓数量300')
    run(cloud, first, lambda *args: response('请核对持仓。', actions=[
        {'kind': 'add_stock', 'market': 'SH', 'symbol': '600000', 'cost_price': '4.85', 'quantity': 300}]))
    assistant_actions.apply(store, uid, first['id'], 0)
    follow = submit(store, keys, uid, sid, '持仓数据没有添加进去')
    result = run(cloud, follow, lambda *args: response('登录您的投资平台后再添加。'))
    assert result['reply']['actions'] == []
    assert '已经在自选里' in result['reply']['answer'] and '投资平台' not in result['reply']['answer']
    assert not assistant_actions.holding_followup('我的持仓收益怎么算')


def test_colloquial_add_to_watchlist_enables_stock_action(cloud):
    store, keys, *_, uid = cloud
    profile(cloud)
    question = '帮我加到自选的持仓里面吧'
    assert assistant_actions.ACTION_INTENT.search(question)
    assert assistant.classify(question, []) == 'project'
    assert not assistant_actions.ACTION_INTENT.search('我的持仓收益怎么算')
    message = submit(store, keys, uid, session(store, uid), question)
    row = store.one('SELECT * FROM assistant_requests WHERE id=?', (message['id'],))
    proto, *_ = assistant.protocol(store, uid, row, {})
    assert 'add_stock' in proto['messages'][0]['content']
    assert '不要说无法添加' in proto['messages'][0]['content']
    prepared = assistant_actions.prepare(store, uid, [{'kind': 'add_stock', 'market': 'SH', 'symbol': '000911', 'group_name': '我的自选'}])
    assert prepared[0]['instrument_id'] == 'SZ:000911' and prepared[0]['market'] == 'SZ'


def watch(store, uid, ident):
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,0,?)',
                  (uid, ident, '我的自选', stamp()))


def test_alert_without_threshold_or_watchlist_is_not_created(cloud):
    store, keys, *_, uid = cloud
    profile(cloud)
    ident = listed_stock(store)
    watch(store, uid, ident)
    sid = session(store, uid)
    missing = submit(store, keys, uid, sid, '持仓跌了就提醒我')
    result = run(cloud, missing, lambda *args: response('已设好提醒。', actions=[
        {'kind': 'add_alert', 'instrument_id': ident, 'condition': 'price_below'}]))
    assert result['status'] == 'success', result
    assert result['reply']['actions'] == [] and '阈值' in result['reply']['answer']
    other = listed_stock(store, '600000', '浦发银行')
    blocked = submit(store, keys, uid, sid, '600000跌到8元提醒我')
    result = run(cloud, blocked, lambda *args: response('请核对提醒。', actions=[
        {'kind': 'add_alert', 'instrument_id': other, 'condition': 'price_below', 'threshold': 8}]))
    assert result['reply']['actions'] == [] and '自选' in result['reply']['answer']
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE kind='alert'")['n'] == 0


def test_alert_preview_applies_once_and_keeps_the_given_threshold(cloud):
    store, keys, _, _, admin, uid = cloud
    profile(cloud)
    ident = listed_stock(store)
    watch(store, uid, ident)
    sid = session(store, uid)
    message = submit(store, keys, uid, sid, '600519跌到1500元时邮件提醒我')
    row = store.one('SELECT * FROM assistant_requests WHERE id=?', (message['id'],))
    proto, *_ = assistant.protocol(store, uid, row, {})
    assert 'add_alert' in proto['messages'][0]['content'] and '不要编造' in proto['messages'][0]['content']
    result = run(cloud, message, lambda *args: response('请核对提醒后添加。', actions=[{
        'kind': 'add_alert', 'instrument_id': '600519', 'condition': 'price_below', 'threshold': 1500,
        'cooldown_minutes': 30, 'email': True}]))
    assert result['status'] == 'success', result
    action = result['reply']['actions'][0]
    assert action['instrument_id'] == ident and action['threshold'] == 1500 and action['email'] is True
    assert action['cooldown_minutes'] == 30 and action['dnd_start'] is None and action['expires_label'] != '持续有效'
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE user_id=? AND kind='alert'", (uid,))['n'] == 0
    with pytest.raises(AuthError):
        assistant_actions.apply(store, admin, message['id'], 0)
    assert not assistant_actions.apply(store, uid, message['id'], 0)['reused']
    payload = json.loads(store.one("SELECT payload_json FROM research_objects WHERE user_id=? AND kind='alert'", (uid,))['payload_json'])
    assert payload['threshold'] == 1500 and payload['email'] is True and payload['cooldown_minutes'] == 30
    assert assistant_actions.apply(store, uid, message['id'], 0)['reused']
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE user_id=? AND kind='alert'", (uid,))['n'] == 1
    continuous = assistant_actions.prepare(store, uid, [{'kind': 'add_alert', 'instrument_id': ident, 'condition': 'risk_changed', 'continuous': True}])
    assert continuous[0]['expires_label'] == '持续有效' and continuous[0]['threshold'] is None
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE user_id=? AND kind='alert'", (uid,))['n'] == 1


def test_journal_without_rationale_is_not_created_and_a_complete_one_applies_once(cloud):
    store, keys, *_, uid = cloud
    profile(cloud)
    ident = listed_stock(store)
    watch(store, uid, ident)
    sid = session(store, uid)
    missing = submit(store, keys, uid, sid, '帮我记下这只股票的研究判断')
    result = run(cloud, missing, lambda *args: response('已记下。', actions=[{'kind': 'add_journal', 'instrument_id': ident}]))
    assert result['status'] == 'success' and result['reply']['actions'] == [] and '判断依据' in result['reply']['answer']
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE kind='journal'")['n'] == 0
    message = submit(store, keys, uid, sid, '记下600519的研究判断：需求仍在，失效条件是连续两个季度收入下滑')
    result = run(cloud, message, lambda *args: response('请核对判断后保存。', actions=[{
        'kind': 'add_journal', 'instrument_id': ident, 'rationale': '需求仍在', 'invalidation': '连续两个季度收入下滑', 'note': '只给自己看'}]))
    assert result['reply']['actions'][0]['rationale'] == '需求仍在'
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE user_id=? AND kind='journal'", (uid,))['n'] == 0
    assert not assistant_actions.apply(store, uid, message['id'], 0)['reused']
    saved = json.loads(store.one("SELECT payload_json FROM research_objects WHERE user_id=? AND kind='journal'", (uid,))['payload_json'])
    assert saved['rationale'] == '需求仍在' and saved['note'] == '只给自己看' and saved['instrument_id'] == ident
    assert assistant_actions.apply(store, uid, message['id'], 0)['reused']
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE user_id=? AND kind='journal'", (uid,))['n'] == 1


def test_topic_and_experiment_are_proposed_then_applied_once(cloud):
    store, keys, *_, uid = cloud
    profile(cloud)
    ident = listed_stock(store)
    watch(store, uid, ident)
    sid = session(store, uid)
    invented = submit(store, keys, uid, sid, '给600519做个实验')
    blocked = run(cloud, invented, lambda *args: response('实验已开始。', actions=[{
        'kind': 'add_experiment', 'topic_id': 'missing-topic', 'parameters': {'instrument_id': ident, 'initial_cash': 10000}}]))
    assert blocked['status'] == 'success' and blocked['reply']['actions'] == [] and '课题' in blocked['reply']['answer']
    assert store.one('SELECT COUNT(*) n FROM research_experiments')['n'] == 0
    topic_message = submit(store, keys, uid, sid, '创建一个均线回撤课题，研究快慢线对回撤的影响')
    topic_result = run(cloud, topic_message, lambda *args: response('请核对课题后创建。', actions=[{
        'kind': 'add_topic', 'name': '均线回撤', 'question': '快慢线对回撤的影响'}]))
    assert topic_result['reply']['actions'][0]['name'] == '均线回撤'
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE user_id=? AND kind='topic'", (uid,))['n'] == 0
    assert not assistant_actions.apply(store, uid, topic_message['id'], 0)['reused']
    topic_id = store.one("SELECT id FROM research_objects WHERE user_id=? AND kind='topic'", (uid,))['id']
    assert assistant_actions.apply(store, uid, topic_message['id'], 0)['reused']
    assert store.one("SELECT COUNT(*) n FROM research_objects WHERE user_id=? AND kind='topic'", (uid,))['n'] == 1
    experiment_message = submit(store, keys, uid, sid, '用这个课题给600519做实验')
    row = store.one('SELECT * FROM assistant_requests WHERE id=?', (experiment_message['id'],))
    proto, *_ = assistant.protocol(store, uid, row, {})
    assert proto['messages'][-1]['content'].find(topic_id) > 0
    result = run(cloud, experiment_message, lambda *args: response('请核对实验后加入队列。', actions=[{
        'kind': 'add_experiment', 'topic_id': topic_id, 'parameters': {'instrument_id': '600519', 'initial_cash': 10000, 'fast': 5, 'slow': 20}}]))
    assert result['status'] == 'success', result
    assert result['reply']['actions'][0]['parameters']['instrument_id'] == ident
    assert result['reply']['actions'][0]['topic_name'] == '均线回撤'
    assert store.one('SELECT COUNT(*) n FROM research_experiments')['n'] == 0
    assert not assistant_actions.apply(store, uid, experiment_message['id'], 0)['reused']
    assert store.one('SELECT COUNT(*) n FROM research_experiments WHERE user_id=?', (uid,))['n'] == 1
    assert assistant_actions.apply(store, uid, experiment_message['id'], 0)['reused']
    assert store.one('SELECT COUNT(*) n FROM research_experiments WHERE user_id=?', (uid,))['n'] == 1



def test_position_price_can_be_proposed_without_opening_trade_edits():
    assert assistant.classify('添加600519，持仓价4.85，持仓数量100', []) == 'project'
    assert assistant.classify('把持仓价改成9', []) == 'project'
    assert assistant.classify('修改持仓为100股', []) == 'manual'
    assert assistant.classify('把成交改成已撤销', []) == 'manual'


def test_recorded_holdings_are_attached_for_sell_analysis(cloud):
    store, keys, _, _, admin, uid = cloud
    profile(cloud); ident = listed_stock(store)
    daily_bar(store, ident, '2026-11-02T15:00:00+08:00', 10)
    daily_bar(store, ident, '2026-11-03T15:00:00+08:00', 11)
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at,cost_price,quantity) VALUES(?,?,?,?,?,?,?)',
                  (uid, ident, 'SECRET-GROUP', 0, stamp(), '8', 100))
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at,cost_price,quantity) VALUES(?,?,?,?,?,?,?)',
                  (admin, ident, 'ADMIN-GROUP', 0, stamp(), '1', 1))
    other = listed_stock(store, '600000', '浦发银行')
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,0,?)',
                  (uid, other, '观察', stamp()))
    message = submit(store, keys, uid, session(store, uid), '现有持仓适合什么时候抛')
    sent = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); sent.append(current)
        source = next(item for item in current['sources'] if item['kind'] == 'watchlist')
        items = source['facts']['items']
        assert [row['instrument_id'] for row in items] == [ident]
        assert items[0]['position']['cost_price'] == '8' and items[0]['position']['quantity'] == 100
        assert items[0]['position']['market_value'] == 1100 and items[0]['position']['total_pnl'] == 300
        text = json_text(current)
        assert 'SECRET-GROUP' not in text and 'ADMIN-GROUP' not in text
        return response('持仓成本 8 元。若已存收盘明显高于成本且没有继续持有的证据，可以卖出。', ['watchlist:holdings'])
    result = run(cloud, message, wire)
    assert result['status'] == 'success' and len(sent) == 1
    assert result['reply']['citations'] == ['watchlist:holdings']
    assert result['reply']['references'][0]['id'] == 'watchlist:holdings'


def test_holding_question_without_records_does_not_invent_prices(cloud):
    store, keys, *_, uid = cloud; profile(cloud)
    listed_stock(store)
    message = submit(store, keys, uid, session(store, uid), '我的持仓收益怎么算')
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content'])
        source = next(item for item in current['sources'] if item['kind'] == 'watchlist')
        assert source['facts']['items'] == [] and source['facts']['recorded_count'] == 0
        assert 'cost_price' not in json_text(source['facts']['items'])
        return response('尚未记录持仓价和数量，无法按现有持仓计算收益。', ['watchlist:holdings'])
    result = run(cloud, message, wire)
    assert result['status'] == 'success'
    assert result['reply']['references'][0]['facts']['items'] == []


def test_model_can_read_watchlist_holdings_without_auto_attach(cloud):
    store, keys, *_, uid = cloud
    profile(cloud); ident = listed_stock(store)
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at,cost_price,quantity) VALUES(?,?,?,?,?,?,?)',
                  (uid, ident, '长期观察', 0, stamp(), '8', 100))
    message = submit(store, keys, uid, session(store, uid), '帮我分析最近的研究报告')
    sent = []
    def wire(fixed, messages, key):
        current = json.loads(messages[-1]['content']); sent.append(current)
        if len(sent) == 1:
            assert not any(item['kind'] == 'watchlist' for item in current['sources'])
            return lookup_response({'action': 'read', 'kind': 'watchlist'})
        source = next(item for item in current['sources'] if item['kind'] == 'watchlist')
        assert source['facts']['items'][0]['instrument_id'] == ident
        assert source['facts']['items'][0]['position']['quantity'] == 100
        return response('已记录持仓可按已存行情核对其相对成本的变化。', ['watchlist:holdings'])
    result = run(cloud, message, wire)
    assert result['status'] == 'success' and len(sent) == 2
    assert result['context']['read_watchlist'] is True


def test_instrument_read_includes_recorded_position_only_when_filled(cloud):
    store, keys, *_, uid = cloud
    ident = listed_stock(store)
    other = listed_stock(store, '600000', '浦发银行')
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at,cost_price,quantity) VALUES(?,?,?,?,?,?,?)',
                  (uid, ident, '长期观察', 0, stamp(), '8', 100))
    daily_bar(store, ident, '2026-11-03T15:00:00+08:00', 11)
    held, _ = assistant.context_refs(store, uid, [{'kind': 'instrument', 'id': ident}], {})
    assert held[0]['facts']['position']['cost_price'] == '8'
    assert held[0]['facts']['position']['quantity'] == 100
    assert held[0]['facts']['position']['market_value'] == 1100
    empty, _ = assistant.context_refs(store, uid, [{'kind': 'instrument', 'id': other}], {})
    assert 'position' not in empty[0]['facts']


def test_watch_holding_survives_sync_projection_and_rejects_invalid_quantity(cloud):
    from backend.sync.business import Adapter
    from backend.sync.protocol import SyncError
    store, _, _, _, admin, uid = cloud
    ident = listed_stock(store)
    store.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at,cost_price,quantity) VALUES(?,?,?,?,?,?,?)',
                  (uid, ident, '长期观察', 1, stamp(), '8.5', 100))
    with store.tx() as c:
        payload = Adapter(store, uid, 'holding-space', 'device-a').export('watch', ident, c)
    received = payload['tables']['user_watchlist'][0]
    assert received['cost_price'] == '8.5' and received['quantity'] == 100 and 'user_id' not in received
    invalid = json.loads(json_text(payload))
    invalid['tables']['user_watchlist'][0]['quantity'] = -1
    with pytest.raises(SyncError, match='持仓价或数量无效'):
        with store.tx(immediate=True) as c:
            Adapter(store, admin, 'holding-space', 'device-b').project('watch', 'ignored', invalid, c)
    assert not store.one('SELECT * FROM user_watchlist WHERE user_id=?', (admin,))
    with store.tx(immediate=True) as c:
        Adapter(store, admin, 'holding-space', 'device-b').project('watch', 'ignored', payload, c)
    copied = store.one("SELECT group_name,pinned,cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id=?", (admin, ident))
    assert copied == {'group_name': '长期观察', 'pinned': 1, 'cost_price': '8.5', 'quantity': 100}
    legacy = json.loads(json_text(payload))
    for row in legacy['tables']['user_watchlist']:
        row.pop('cost_price'); row.pop('quantity')
    with store.tx(immediate=True) as c:
        Adapter(store, uid, 'holding-space', 'device-a').project('watch', 'ignored', legacy, c)
    kept = store.one("SELECT cost_price,quantity,group_name FROM user_watchlist WHERE user_id=? AND instrument_id=?", (uid, ident))
    assert kept['cost_price'] == '8.5' and kept['quantity'] == 100 and kept['group_name'] == '长期观察'
