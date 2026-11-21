"""Task-center retries preserve history, ownership, and truthful collection status."""
import json
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from test_cloud import cloud, login, run_job
from backend.cloud import collector, jobs, workspace
from backend.cloud.auth import AuthError
from backend.cloud.store import stamp
from backend.config import now
from backend.public_app import create_app


def test_task_help_describes_available_entries_without_model_calls():
    from backend.cloud.assistant import help_document, relevant_help
    entry = help_document('jobs')
    assert entry['version'] == 'research-workbench/4'
    assert '任务中心' in entry['text'] and '不确定发送不直接重发' in entry['text']
    assert relevant_help('任务中心怎么看失败原因')[0]['id'] == 'jobs'


def test_failed_report_can_be_retried_once_without_replacing_history(cloud, monkeypatch):
    store, keys, _, _, _, uid = cloud
    with monkeypatch.context() as patch:
        patch.setattr(workspace, 'facts', lambda *a: (_ for _ in ()).throw(ValueError('fixture data unavailable')))
        result = run_job(cloud, ai=False)
    assert result['error_code'] == 'REPORT_GENERATION_FAILED'
    original = store.one('SELECT * FROM personal_jobs WHERE user_id=?', (uid,))
    retry = jobs.retry(store, uid, original['id'])
    assert retry['id'] != original['id']
    assert jobs.retry(store, uid, original['id'])['id'] == retry['id']
    current = jobs.claim(store, 'retry-fixture')
    assert current['id'] == retry['id']
    assert jobs.run(store, keys, current)['report_id']
    assert store.one('SELECT status FROM personal_jobs WHERE id=?', (original['id'],))['status'] == 'failed'
    assert store.one('SELECT COUNT(*) n FROM personal_jobs')['n'] == 2
    value = jobs.list_jobs(store, uid)
    assert any(row.get('result_route', '').startswith('reports?report=') for row in value['jobs'])
    assert {row['message'] for row in value['logs']} >= {'任务已加入队列', '任务开始执行'}


def test_retry_rejects_running_foreign_old_and_uncertain_jobs(cloud):
    store, _, _, _, admin, uid = cloud
    original = jobs.enqueue(store, uid, ai=False)
    with pytest.raises(AuthError) as blocked:
        jobs.retry(store, uid, original['id'])
    assert blocked.value.code == 'JOB_NOT_RETRYABLE'
    with pytest.raises(AuthError) as foreign:
        jobs.retry(store, admin, original['id'])
    assert foreign.value.code == 'NOT_FOUND'
    store.execute("UPDATE personal_jobs SET status='failed',target_date=? WHERE id=?", ((now()-timedelta(days=1)).date().isoformat(), original['id']))
    assert not jobs.list_jobs(store, uid)['jobs'][0]['can_retry']
    with pytest.raises(AuthError):
        jobs.retry(store, uid, original['id'])
    store.execute('UPDATE personal_jobs SET target_date=? WHERE id=?', (now().date().isoformat(), original['id']))
    store.execute("INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,created_at) VALUES(?,?,?,'fixture',1,1,'uncertain',?)", ('uncertain-fixture', uid, original['id'], stamp()))
    with pytest.raises(AuthError) as uncertain:
        jobs.retry(store, uid, original['id'])
    assert '不确定' in str(uncertain.value)
    assert store.one('SELECT COUNT(*) n FROM personal_jobs')['n'] == 1


def test_rule_report_retry_is_an_idempotent_new_version(cloud):
    store, keys, _, _, _, uid = cloud
    report = run_job(cloud, ai=False)
    original = store.one('SELECT * FROM personal_jobs WHERE user_id=?', (uid,))
    retry = jobs.retry(store, uid, original['id'])
    assert jobs.retry(store, uid, original['id'])['id'] == retry['id']
    result = jobs.run(store, keys, jobs.claim(store, 'supplement-fixture'))
    assert result['version'] == 2 and result['report_id'] != report['report_id']
    row = store.one('SELECT parent_id FROM personal_reports WHERE id=?', (result['report_id'],))
    assert row['parent_id'] == report['report_id']


@pytest.mark.parametrize('value,expected', [
    ({'status': 'success'}, 'success'),
    ({'status': 'partial'}, 'partial'),
    ({'status': 'unavailable'}, 'failed'),
    ({'result': [{'status': 'unavailable'}]}, 'failed'),
    ({'result': [{'status': 'success', 'cache_only': True}, {'status': 'unavailable'}]}, 'failed'),
    ({'result': [{'status': 'success'}, {'status': 'unavailable'}]}, 'partial'),
    ({'result': [{'status': 'success'}]}, 'success'),
    ({'result': []}, 'failed'),
    ({'announcements': []}, 'success'),
])
def test_market_collection_result_is_not_misreported_as_success(value, expected):
    assert collector.result_status(value) == expected


def test_shared_partial_collection_preserves_request_and_retry_is_idempotent(cloud, monkeypatch):
    store, *_ = cloud
    original = collector.enqueue(store, 'instrument', 'SH:603126', '15m')
    monkeypatch.setattr(collector.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps({'status': 'partial'})))
    assert collector.run_one(store)
    row = collector.list_jobs(store)[0]
    assert row['status'] == 'partial' and row['can_retry']
    assert row['result']['_request'] == {'instrument_id': 'SH:603126', 'timeframe': '15m'}
    retry = collector.retry(store, original['id'])
    assert collector.retry(store, original['id'])['id'] == retry['id']
    args = store.one('SELECT result_json FROM site_jobs WHERE id=?', (retry['id'],))
    assert json.loads(args['result_json']) == {'instrument_id': 'SH:603126', 'timeframe': '15m'}


def test_shared_collection_persists_backoff_and_request_through_restart(cloud, monkeypatch):
    store, *_ = cloud
    at = datetime.now(timezone.utc)
    monkeypatch.setattr(collector, "stamp", lambda: at.isoformat(timespec="microseconds"))
    calls = []

    def offline(command, **kwargs):
        calls.append(json.loads(command[-1]))
        return SimpleNamespace(returncode=0, stdout=json.dumps({"status": "unavailable", "errors": ["fixture offline"]}))

    monkeypatch.setattr(collector.subprocess, "run", offline)
    args = {"instrument_id": "SH:603126", "timeframe": "15m"}
    original = collector.enqueue(store, "instrument", *args.values())
    for attempt, seconds in enumerate(collector.COLLECTION_RETRY_DELAYS, start=1):
        assert collector.run_one(store)
        queued = collector.list_jobs(store)[0]
        assert queued["status"] == "queued" and queued["id"] == original["id"]
        assert queued["run_attempts"] == attempt and not queued["can_retry"]
        assert datetime.fromisoformat(queued["next_attempt_at"]) == at + timedelta(seconds=seconds)
        assert queued["result"]["_request"] == args
        assert collector.enqueue(store, "instrument", *args.values())["id"] == original["id"]
        jobs.recover(store)
        assert not collector.run_one(store)
        at += timedelta(seconds=seconds)
    assert collector.run_one(store)
    result = collector.list_jobs(store)[0]
    assert result["status"] == "failed" and result["run_attempts"] == 3 and result["can_retry"]
    assert [row["attempt"] for row in result["result"]["retry_history"]] == [1, 2]
    assert result["next_attempt_at"] is None and calls == [args, args, args]
    assert store.one("SELECT COUNT(*) n FROM site_jobs")["n"] == 1


def test_interrupted_collection_can_be_retried_with_the_original_parameters(cloud):
    store, *_ = cloud
    original = collector.enqueue(store, "instrument", "SH:603126", "60m")
    store.execute("UPDATE site_jobs SET status='running' WHERE id=?", (original["id"],))
    jobs.recover(store)
    row = collector.list_jobs(store)[0]
    assert row["status"] == "failed" and row["can_retry"]
    again = collector.retry(store, original["id"])
    saved = store.one("SELECT result_json FROM site_jobs WHERE id=?", (again["id"],))
    assert json.loads(saved["result_json"]) == {"instrument_id": "SH:603126", "timeframe": "60m"}


def test_automatic_morning_uses_recovered_shared_refresh_and_archives_once(cloud, monkeypatch):
    store, keys, _, _, _, uid = cloud
    store.cfg = replace(store.cfg, background=True)
    at = datetime.now(timezone.utc)
    elapsed = 0
    provider_calls = []

    def collect(command, **kwargs):
        provider_calls.append(command[-2])
        value = {"status": "success" if len(provider_calls) > 1 else "unavailable"}
        return SimpleNamespace(returncode=0, stdout=json.dumps(value))

    def pump(seconds):
        nonlocal elapsed, at
        assert seconds == 1
        elapsed += 1
        at += timedelta(seconds=1)
        collector.run_one(store)

    monkeypatch.setattr(collector.subprocess, "run", collect)
    monkeypatch.setattr(collector, "stamp", lambda: at.isoformat(timespec="microseconds"))
    monkeypatch.setattr(collector, "time", SimpleNamespace(monotonic=lambda: elapsed, sleep=pump))
    original = jobs.enqueue(store, uid, automatic=True, ai=False)
    task = jobs.claim(store, "recovered-shared-market")
    report_id = jobs.run(store, keys, task)["report_id"]
    assert provider_calls == ["sync", "sync"]
    shared = collector.list_jobs(store)[0]
    assert shared["status"] == "success" and shared["run_attempts"] == 2
    assert shared["result"]["retry_history"][0]["delay_seconds"] == collector.COLLECTION_RETRY_DELAYS[0]
    assert store.one("SELECT COUNT(*) n FROM site_jobs")["n"] == 1
    assert jobs.enqueue(store, uid, automatic=True)["id"] == original["id"]
    assert store.query("SELECT id FROM personal_reports WHERE job_id=?", (original["id"],)) == [{"id": report_id}]


def test_shared_jobs_are_visible_only_to_admin_and_retry_checks_role(cloud):
    store, *_ = cloud
    original = collector.enqueue(store, 'sync')
    store.execute("UPDATE site_jobs SET status='failed',error_code='SHARED_COLLECTION_FAILED' WHERE id=?", (original['id'],))
    with TestClient(create_app(store.cfg)) as client:
        login(client)
        assert client.get('/api/me/jobs').json()['shared_jobs'] == []
        assert client.post('/api/admin/jobs/'+original['id']+'/retry').status_code == 403
        login(client, 'admin@example.test')
        value = client.get('/api/me/jobs').json()
        assert value['shared_jobs'][0]['id'] == original['id']
        first = client.post('/api/admin/jobs/'+original['id']+'/retry')
        assert first.status_code == 200
        assert client.post('/api/admin/jobs/'+original['id']+'/retry').json()['id'] == first.json()['id']
