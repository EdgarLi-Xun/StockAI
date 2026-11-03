"""Remote coordination never holds the business write lock or replays an uncertain send."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import smtplib
import sqlite3

import pytest

from test_cloud import cloud
from test_sync_protocol import Memory
from test_workbench import research_data
from backend.cloud import alerts,jobs,resources
from backend.cloud.auth import AuthError
from backend.cloud.runtime import Runtime
from backend.sync.engine import Sync
from backend.sync.protocol import digest


@pytest.fixture
def bound_sync(cloud):
    store,keys,_,_,_,uid=cloud
    remote=Memory();service=Sync(store,keys,lambda *args:remote)
    service.configure(uid,{'backend':'webdav','url':'https://storage.example/scheduling'},create=True)
    store.sync_service=service
    return store,keys,uid,service,remote


def assert_other_writer_available(store):
    connection=sqlite3.connect(store.path,timeout=.1)
    try:
        connection.execute('BEGIN IMMEDIATE')
        connection.execute("UPDATE cloud_meta SET value=value WHERE key='maintenance'")
        connection.commit()
    finally:connection.close()


def observe_remote_writes(monkeypatch,store,remote,callback=None,callback_read=1):
    original=remote.read;seen=[]
    def read(path):
        if path=='head.bin':
            assert_other_writer_available(store)
            seen.append(path)
            if callback and len(seen)==callback_read:callback()
        return original(path)
    monkeypatch.setattr(remote,'read',read)
    return seen


def test_automatic_report_coordinates_without_write_lock_and_is_idempotent(bound_sync,monkeypatch):
    store,_,uid,_,remote=bound_sync
    seen=observe_remote_writes(monkeypatch,store,remote)
    with ThreadPoolExecutor(2) as pool:
        results=list(pool.map(lambda _:jobs.enqueue(store,uid,automatic=True,ai=False),range(2)))
    assert seen and len({row['id'] for row in results})==1
    assert store.one('SELECT COUNT(*) n FROM personal_jobs')['n']==1


def test_automatic_report_rechecks_account_after_remote_coordination(bound_sync,monkeypatch):
    store,_,uid,_,remote=bound_sync
    observe_remote_writes(monkeypatch,store,remote,lambda:store.execute("UPDATE users SET status='disabled' WHERE id=?",(uid,)))
    with pytest.raises(AuthError) as blocked:jobs.enqueue(store,uid,automatic=True,ai=False)
    assert blocked.value.code=='VERIFICATION_REQUIRED'
    assert not store.one('SELECT id FROM personal_jobs')


def test_alert_coordinates_without_write_lock_and_records_one_event(research_data,bound_sync,monkeypatch):
    store,uid,ident,_,at=research_data;remote=bound_sync[-1]
    alerts.save(store,uid,alerts.AlertInput(name='价格',instrument_id=ident,condition='price_above',threshold=5,expires_at=None))
    seen=observe_remote_writes(monkeypatch,store,remote)
    assert alerts.evaluate(store,uid,at)==1
    assert alerts.evaluate(store,uid,at)==0
    assert seen and store.one('SELECT COUNT(*) n FROM alert_events')['n']==1


def test_alert_rechecks_rule_after_remote_coordination(research_data,bound_sync,monkeypatch):
    store,uid,ident,_,at=research_data;remote=bound_sync[-1]
    rule=alerts.save(store,uid,alerts.AlertInput(name='价格',instrument_id=ident,condition='price_above',threshold=5,expires_at=None))
    observe_remote_writes(monkeypatch,store,remote,lambda:store.execute("UPDATE research_objects SET status='disabled' WHERE id=?",(rule['id'],)),callback_read=2)
    assert alerts.evaluate(store,uid,at)==0
    assert not store.one('SELECT id FROM alert_events')


@pytest.fixture
def pending_mail(research_data,bound_sync):
    store,uid,ident,_,at=research_data
    store.cfg=replace(store.cfg,smtp_host='smtp.invalid',smtp_from='stockai@example.test')
    resources.save(store,uid,'notification_settings',{'email_enabled':True})
    alerts.save(store,uid,alerts.AlertInput(name='邮件',instrument_id=ident,condition='price_above',threshold=5,expires_at=None,email=True))
    assert alerts.evaluate(store,uid,at)==1
    return store,uid,bound_sync[3],store.one("SELECT * FROM alert_deliveries WHERE channel='email'")


def retry_now(store,ident):
    store.execute("UPDATE alert_deliveries SET next_at='2000-01-01T00:00:00+00:00' WHERE id=?",(ident,))


def mail_receipt(store,service,uid):
    events=store.query('SELECT event_key FROM alert_events WHERE user_id=?',(uid,))
    key=digest(sorted(row['event_key'] for row in events))
    head,_=service.heads(service.binding(uid)).read()
    return head['receipts'][digest(['mail',key])]


def test_mail_explicit_451_retries_same_delivery_once(pending_mail):
    store,uid,service,delivery=pending_mail;sent=[]
    def sender(message):
        sent.append(message)
        if len(sent)==1:raise smtplib.SMTPDataError(451,b'Temporarily rejected')
    assert alerts.send_mail(store,None,sender)
    assert store.one('SELECT state FROM alert_deliveries WHERE id=?',(delivery['id'],))['state']=='retry'
    retry_now(store,delivery['id'])
    assert alerts.send_mail(store,None,sender)
    assert not alerts.send_mail(store,None,sender)
    row=store.one('SELECT state,attempts FROM alert_deliveries WHERE id=?',(delivery['id'],))
    assert row=={'state':'delivered','attempts':2} and len(sent)==2
    receipt=mail_receipt(store,service,uid)
    assert receipt['state']=='delivered' and receipt['delivery']==delivery['id'] and receipt['attempt']==2


def test_mail_uncertain_retry_is_not_replayed_from_old_local_state(pending_mail):
    store,uid,service,delivery=pending_mail;sent=[]
    def sender(message):
        sent.append(message)
        if len(sent)==1:raise smtplib.SMTPDataError(451,b'Temporarily rejected')
        raise TimeoutError('Submission result unknown')
    alerts.send_mail(store,None,sender);retry_now(store,delivery['id'])
    alerts.send_mail(store,None,sender)
    assert store.one('SELECT state FROM alert_deliveries WHERE id=?',(delivery['id'],))['state']=='uncertain'
    assert not alerts.send_mail(store,None,sender)
    # 模拟恢复到上一轮明确拒绝后的业务记录；独立远端尝试次数仍然是最新的。
    store.execute("UPDATE alert_deliveries SET state='retry',attempts=1,error_code='SMTP_REJECTED' WHERE id=?",(delivery['id'],))
    retry_now(store,delivery['id']);alerts.send_mail(store,None,sender)
    assert len(sent)==2 and mail_receipt(store,service,uid)['attempt']==2


def test_mail_permanent_rejection_does_not_retry(pending_mail):
    store,_,_,delivery=pending_mail;sent=[]
    def sender(message):
        sent.append(message);raise smtplib.SMTPDataError(550,b'Rejected')
    alerts.send_mail(store,None,sender)
    assert store.one('SELECT state FROM alert_deliveries WHERE id=?',(delivery['id'],))['state']=='failed'
    assert not alerts.send_mail(store,None,sender) and len(sent)==1


def test_mail_accepted_before_quit_451_is_delivered_without_retry(pending_mail,monkeypatch):
    store,uid,service,delivery=pending_mail;sent=[]
    class SMTP:
        def __init__(self,*args,**kwargs):pass
        def __enter__(self):return self
        def __exit__(self,*args):raise smtplib.SMTPResponseException(451,b'QUIT failed after acceptance')
        def starttls(self,**kwargs):pass
        def login(self,*args):pass
        def send_message(self,message):sent.append(message);return {}
    monkeypatch.setattr(alerts.smtplib,'SMTP',SMTP);monkeypatch.setattr(alerts.smtplib,'SMTP_SSL',SMTP)
    assert alerts.send_mail(store,None)
    assert store.one('SELECT state FROM alert_deliveries WHERE id=?',(delivery['id'],))['state']=='delivered'
    assert mail_receipt(store,service,uid)['state']=='delivered'
    assert not alerts.send_mail(store,None) and len(sent)==1


def test_mail_retry_is_bound_to_delivery_device_generation_and_attempt(bound_sync):
    _,_,uid,service,_=bound_sync
    assert service.automatic(uid,'mail','guarded',delivery='delivery')
    assert not service.automatic(uid,'mail','guarded',delivery='other',retry=1)
    assert service.automatic(uid,'mail','guarded',delivery='delivery',retry=1)
    assert not service.automatic(uid,'mail','guarded',delivery='delivery',retry=1)
    heads=service.heads(service.binding(uid))
    heads.mutate(lambda head:head['executor'].update(generation=head['executor']['generation']+1))
    assert not service.automatic(uid,'mail','guarded',delivery='delivery',retry=2)
    receipt_id=digest(['mail','guarded'])
    heads.mutate(lambda head:head['receipts'][receipt_id].update(generation=head['executor']['generation'],device='another-device'))
    assert not service.automatic(uid,'mail','guarded',delivery='delivery',retry=2)


def test_worker_recovers_from_real_sqlite_busy_and_runs_next_claim(cloud,monkeypatch):
    store,keys,_,_,_,uid=cloud;job=jobs.enqueue(store,uid,ai=False)
    runtime=Runtime(store.cfg);runtime.store=store;runtime.keys=keys
    with store.tx(immediate=True):
        connection=sqlite3.connect(store.path,timeout=0)
        try:
            with pytest.raises(sqlite3.OperationalError) as busy:connection.execute('BEGIN IMMEDIATE')
        finally:connection.close()
    original_claim=jobs.claim;original_run=jobs.run;calls=[]
    def claim(*args):
        calls.append(1)
        if len(calls)==1:raise busy.value
        return original_claim(*args)
    def run(*args):
        try:return original_run(*args)
        finally:runtime.stop.set()
    monkeypatch.setattr(jobs,'claim',claim);monkeypatch.setattr(jobs,'run',run)
    runtime._worker()
    assert len(calls)==2 and store.one('SELECT status FROM personal_jobs WHERE id=?',(job['id'],))['status']=='success'


def test_worker_does_not_silence_unexpected_database_errors(cloud,monkeypatch):
    store,keys,*_=cloud;runtime=Runtime(store.cfg);runtime.store=store;runtime.keys=keys
    def invalid(*args):raise sqlite3.OperationalError('no such table: broken_queue')
    monkeypatch.setattr(jobs,'claim',invalid)
    with pytest.raises(sqlite3.OperationalError,match='broken_queue'):runtime._worker()
