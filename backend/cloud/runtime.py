# 协调公网服务启动恢复、双任务工作线程、站点采集及定期维护。
from __future__ import annotations

import shutil
import threading
import uuid
import os
import sqlite3
import sys

from backend.config import now
from . import backups,collector,jobs,mail
from .auth import Auth
from .crypto import Keyring
from .lifecycle import ErasureJournal,ProcessLock,reconcile,recover_swap,clean_staging
from .store import Store,stamp,json_text


def _database_busy(exc):
    code=getattr(exc,'sqlite_errorcode',None)
    if code is not None:return code&255 in (sqlite3.SQLITE_BUSY,sqlite3.SQLITE_LOCKED)
    return str(exc).lower() in ('database is locked','database is busy','database table is locked','database schema is locked')


# 持有服务资源与后台线程，保证启动恢复和退出收尾的统一顺序。
class Runtime:
    # 校验部署配置，并建立线程停止信号与资源容器。
    def __init__(self,cfg):
        self.cfg=cfg;cfg.validate()
        self.stop=threading.Event();self.threads=[];self.lock=None

    # 获取独占锁，恢复目录及权威记录，再初始化存储与后台线程。
    def start(self):
        self.lock=ProcessLock(self.cfg.erasures/"service.lock");self.lock.__enter__()
        try:
            self.keys=Keyring(self.cfg.key_file);recover_swap(self.cfg,self.keys)
            self.store=Store(self.cfg).initialize()
            self.auth=Auth(self.store,self.keys);self.journal=ErasureJournal(self.store,self.keys)
            reconcile(self.store,self.journal)
            from .research_control import control
            control(self.store,self.keys).reconcile()
            from backend.sync.engine import Sync
            self.store.sync_service=Sync(self.store,self.keys)
            self.store.sync_service.recover_local()
            from backend.upgrades.manager import Manager
            from . import VERSION
            from backend.config import ROOT
            self.upgrades=Manager(self.cfg,self.keys,component='desktop' if self.cfg.desktop_mode else 'web',version=VERSION,form='exe' if self.cfg.desktop_mode and getattr(sys,'frozen',False) else 'source',store=self.store,root_file=os.environ.get('STOCKAI_RELEASE_ROOT',str(ROOT/'config/release-root.json')),source=os.environ.get('STOCKAI_RELEASE_SOURCE',''))
            transaction=self.upgrades.current()
            if transaction and transaction['status'] in ('backup','preparing','migrating','switching','rolling_back','exit_requested','manual_recovery'):self.upgrades.maintenance(True)
            # 暂存目录可能含受限明文，启动恢复时清理被中断任务留下的内容。
            clean_staging(self.store)
            jobs.recover(self.store)
            if self.store.cfg.background:
                collector.queue_names(self.store)
                for i in range(2):
                    self._start(self._worker,f"report-{i}")
                self._start(self._control,"site-control")
                self._start(self._site,"site-collector")
                self._start(self._maintenance,"site-backups")
                self._start(self._research,"research-rules")
                self._start(self._alert_mail,"research-alert-mail")
                self._start(self._observations,"research-observations")
                self._start(self._sync,"personal-sync")
                self._start(self._upgrade_check,"release-check")
            return self
        except BaseException:
            self.lock.__exit__();self.lock=None;raise

    # 创建具名守护线程并登记，供统一停止时等待收尾。
    def _start(self,fn,name):
        thread=threading.Thread(target=fn,name=name,daemon=True);self.threads.append(thread);thread.start()

    # 通知后台线程停止，等待处理结束后才释放服务独占锁。
    def close(self):
        self.stop.set()
        # 请求设有超时；后台线程全部停止后才释放服务独占锁。
        for thread in self.threads:
            thread.join(timeout=1000)
        if self.lock:
            self.lock.__exit__();self.lock=None

    # 循环领取个人任务，并在执行期间保持租约心跳。
    def _worker(self):
        worker=uuid.uuid4().hex
        while not self.stop.is_set():
            try:
                if self.paused():self.stop.wait(1);continue
                job=jobs.claim(self.store,worker)
            except sqlite3.OperationalError as exc:
                if not _database_busy(exc):raise
                self.stop.wait(0.5);continue
            if not job:
                self.stop.wait(0.5);continue
            done=threading.Event()
            # 定期延长当前任务租约，任务结束或服务停止时退出。
            def beat():
                while not done.wait(10):
                    while not done.is_set():
                        try:
                            jobs.heartbeat(self.store,job["id"],worker);break
                        except sqlite3.OperationalError as exc:
                            if not _database_busy(exc):raise
                            if done.wait(0.5):return
            heart=threading.Thread(target=beat,daemon=True);heart.start()
            try:
                jobs.run(self.store,self.keys,job)
            finally:
                if job.get('automatic'):
                    try:
                        uncertain=self.store.one("SELECT id FROM model_calls WHERE job_id=? AND phase='uncertain'",(job['id'],))
                        self.store.sync_service.automatic(job['user_id'],'report',job['logical_key'],complete='uncertain' if uncertain else 'completed')
                    except Exception:pass
                done.set();heart.join()

    # 定期处理个人调度、邮件队列和控制面维护。
    def _control(self):
        last_minute=None
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            try:
                mail.send_one(self.store,self.keys)
                minute=now().isoformat()[:16]
                if minute!=last_minute:
                    at=now()
                    # 合并所有已开启个人研究计划的共享行情刷新，减少重复采集。
                    if self.store.one("SELECT user_id FROM user_settings s JOIN users u ON u.id=s.user_id WHERE u.status='active' AND (json_extract(s.payload_json,'$.morning_enabled')=1 OR json_extract(s.payload_json,'$.closing_enabled')=1 OR json_extract(s.payload_json,'$.weekly_enabled')=1) LIMIT 1"):
                        for stage,scheduled in (("morning","07:40"),("closing","15:40")):
                            key="shared-sync:"+stage+":"+at.date().isoformat()
                            if at.strftime("%H:%M")>=scheduled and not self.store.one("SELECT key FROM cloud_meta WHERE key=?",(key,)):
                                collector.enqueue(self.store,"sync")
                                self.store.execute("INSERT OR IGNORE INTO cloud_meta VALUES(?,?)",(key,stamp()))
                    jobs.schedule(self.store);last_minute=minute
            except Exception:
                # 邮件和模型错误可能含私密信息，此处不记录原始异常或载荷。
                pass
            self.stop.wait(2)

    # 串行消费共享市场数据采集任务。
    def _site(self):
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            try:
                collector.run_one(self.store)
            except Exception:
                pass
            self.stop.wait(2)

    # 按维护计划创建和清理加密备份。
    def _maintenance(self):
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            try:
                at=now();key="daily_backup:"+at.date().isoformat()
                row=self.store.one("SELECT * FROM site_jobs WHERE kind='backup' AND status='queued' ORDER BY created_at LIMIT 1")
                if not row and at.hour>=self.store.cfg.backup_hour and not self.store.one("SELECT id FROM site_jobs WHERE logical_key=?",(key,)):
                    ident=uuid.uuid4().hex
                    self.store.execute("INSERT INTO site_jobs(id,logical_key,kind,status,created_at) VALUES(?,?,'backup','queued',?)",(ident,key,stamp()))
                    row=self.store.one("SELECT * FROM site_jobs WHERE id=?",(ident,))
                if row:
                    self.store.execute("UPDATE site_jobs SET status='running',started_at=? WHERE id=?",(stamp(),row["id"]))
                    try:
                        result=backups.create(self.store,self.keys,self.journal)
                        self.store.execute("UPDATE site_jobs SET status='success',finished_at=?,result_json=? WHERE id=?",(stamp(),json_text(result),row["id"]))
                    except Exception:
                        self.store.execute("UPDATE site_jobs SET status='failed',finished_at=?,error_code='ENCRYPTED_BACKUP_FAILED' WHERE id=?",(stamp(),row["id"]))
                backups.prune(self.store,self.journal)
            except Exception:
                pass
            self.stop.wait(20)

    # 周期评估个人提醒并投递已生成的事件通知。
    def _research(self):
        from . import alerts,research
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            for user in self.store.query("SELECT id FROM users WHERE status='active' AND verified_at IS NOT NULL"):
                if self.stop.is_set():break
                try:
                    alerts.evaluate(self.store,user['id'])
                except Exception:
                    pass
            self.stop.wait(15)

    def _alert_mail(self):
        from . import alerts
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            try:alerts.send_mail(self.store,self.keys)
            except Exception:pass
            self.stop.wait(2)

    # 分批刷新有效判断的后续观察，限制每轮规模以免影响提醒评估。
    def _observations(self):
        """Bound observation batches so alert evaluation is independent of journal history."""
        from . import research
        cursor=''
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            rows=self.store.query("SELECT r.id,r.user_id FROM research_objects r JOIN users u ON u.id=r.user_id WHERE r.kind='journal' AND r.status='active' AND u.status='active' AND r.id>? ORDER BY r.id LIMIT 20",(cursor,))
            for row in rows:
                if self.stop.is_set():return
                try:research.journal_detail(self.store,row['user_id'],row['id'],size=20)
                except Exception:pass
            cursor=rows[-1]['id'] if rows else ''
            self.stop.wait(5)

    def paused(self):
        row=self.store.one("SELECT value FROM cloud_meta WHERE key='maintenance'")
        return bool(row and row['value']!='0')

    def _sync(self):
        from datetime import datetime,timezone
        from backend.sync.protocol import SyncError
        last={}
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            try:
                for erased in self.journal.verify():self.store.sync_service.cleanup(erased['user_id'])
            except Exception:pass
            if self.paused():self.stop.wait(1);continue
            for row in self.store.query("SELECT * FROM sync_bindings b JOIN users u ON u.id=b.user_id WHERE u.status='active'"):
                uid=row['user_id']
                try:
                    # 即使网络不可达，先把本机业务变化持久化为待发密文。
                    changed=self.store.sync_service.capture(uid)
                    if not row['automatic'] or row['status']=='paused':continue
                    details=self.store.sync_service.details(uid)
                    if details.get('first_merge'):continue
                    if details.get('status') in ('auth_failed','revoked','incompatible','paused'):continue
                    at=datetime.now(timezone.utc)
                    if details.get('next_retry') and at<datetime.fromisoformat(details['next_retry']):continue
                    if changed or at.timestamp()-last.get(uid,0)>=60:
                        self.store.sync_service.synchronize(uid);last[uid]=at.timestamp()
                except (SyncError,ValueError,OSError):pass
            self.stop.wait(5)

    def _upgrade_check(self):
        while not self.stop.is_set():
            if self.paused():self.stop.wait(1);continue
            if not self.paused():
                try:
                    self.upgrades.cleanup(execute=True,automatic=True)
                    checked=self.upgrades.check(automatic=True)
                    if checked.get('status')=='available' and self.upgrades.state.get('settings')['automatic_download']:
                        tx=self.upgrades.prepare()
                        from datetime import datetime,timezone
                        if tx['status'] in ('planned','download_failed') and (not tx.get('next_retry') or datetime.now(timezone.utc)>=datetime.fromisoformat(tx['next_retry'])):self.upgrades.download(tx['id'])
                except Exception:pass
            self.stop.wait(30)
