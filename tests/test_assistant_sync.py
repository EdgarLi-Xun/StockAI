"""Sync retains automatic lookup ownership and consent markers without model profiles."""
import json
import uuid

from backend.cloud import assistant, resources
from backend.cloud.store import json_text, stamp
from test_cloud import cloud
from test_assistant_lookup import archive
from test_sync_protocol import peers, sync


def test_synced_automatic_sources_keep_local_ids_and_sensitive_history_guard(peers):
    first, uid, second, other_uid, _ = peers
    store = first.store; report = archive(store, uid)
    sid = resources.save(store, uid, 'chat_session', {'title': '自动查询'})['id']
    ident = uuid.uuid4().hex
    payload = {'contexts': [], 'profile': {'credential_id': 'NEVER-SYNC'},
               'retrieved_contexts': [{'kind': 'report', 'id': report}],
               'lookup_contexts': [{'kind': 'report', 'id': report}], 'lookup_sensitivity': ['summary']}
    reply = {'answer': '归档的模拟汇总应结合原始来源理解。', 'citations': ['report:' + report]}
    store.execute("INSERT INTO assistant_requests VALUES(?,?,?,?,?,NULL,?,?,'success',?,?,?,NULL,1,?,NULL)",
                  (ident, uid, sid, uuid.uuid4().hex, 'fingerprint', '查询我的报告', json_text(payload), stamp(), stamp(), json_text(reply), '["summary"]'))
    sync(first, uid); sync(second, other_uid)
    row = second.store.one('SELECT * FROM assistant_requests WHERE user_id=?', (other_uid,))
    assert row and json.loads(row['sensitivity_json']) == ['summary']
    received = json.loads(row['payload_json'])
    assert 'profile' not in received and 'NEVER-SYNC' not in row['payload_json']
    local = received['retrieved_contexts'][0]['id']
    assert local != report and second.store.one('SELECT id FROM personal_reports WHERE id=? AND user_id=?', (local, other_uid))
    assert received['lookup_contexts'][0]['id'] == local and received['lookup_sensitivity'] == ['summary']
    assert assistant.history(second.store, other_uid, row['session_id'], {}) == ([], 1)
