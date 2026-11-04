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
