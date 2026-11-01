from __future__ import annotations

import shutil
import threading
import uuid

from backend.config import now
from . import backups,collector,jobs,mail
from .auth import Auth
from .crypto import Keyring
from .lifecycle import ErasureJournal,ProcessLock,reconcile,recover_swap,clean_staging
from .store import Store,stamp,json_text


class Runtime:
    def __init__(self,cfg):
        self.cfg=cfg;cfg.validate()
        self.stop=threading.Event();self.threads=[];self.lock=None

    def start(self):
        self.lock=ProcessLock(self.cfg.erasures/"service.lock");self.lock.__enter__()
        try:
            self.keys=Keyring(self.cfg.key_file);recover_swap(self.cfg,self.keys)
            self.store=Store(self.cfg).initialize()
            self.auth=Auth(self.store,self.keys);self.journal=ErasureJournal(self.store,self.keys)
            reconcile(self.store,self.journal)
            # Staging directories are restricted plaintext; always remove abandoned runs.
            clean_staging(self.store)
            jobs.recover(self.store)
            if self.store.cfg.background:
                for i in range(2):
                    self._start(self._worker,f"report-{i}")
                self._start(self._control,"site-control")
                self._start(self._site,"site-collector")
                self._start(self._maintenance,"site-backups")
            return self
        except BaseException:
            self.lock.__exit__();self.lock=None;raise

    def _start(self,fn,name):
        thread=threading.Thread(target=fn,name=name,daemon=True);self.threads.append(thread);thread.start()

    def close(self):
        self.stop.set()
        # Worker requests have bounded timeouts; keep the exclusive lock until all stop.
        for thread in self.threads:
            thread.join(timeout=1000)
        if self.lock:
            self.lock.__exit__();self.lock=None

    def _worker(self):
        worker=uuid.uuid4().hex
        while not self.stop.is_set():
            job=jobs.claim(self.store,worker)
            if not job:
                self.stop.wait(0.5);continue
            done=threading.Event()
            def beat():
                while not done.wait(10):
                    jobs.heartbeat(self.store,job["id"],worker)
            heart=threading.Thread(target=beat,daemon=True);heart.start()
            try:
                jobs.run(self.store,self.keys,job)
            finally:
                done.set();heart.join()

    def _control(self):
        last_minute=None
        while not self.stop.is_set():
            try:
                mail.send_one(self.store,self.keys)
                minute=now().isoformat()[:16]
                if minute!=last_minute:
                    at=now()
                    # The site refresh is merged for all opted-in research schedules.
                    if self.store.one("SELECT user_id FROM user_settings s JOIN users u ON u.id=s.user_id WHERE u.status='active' AND (json_extract(s.payload_json,'$.morning_enabled')=1 OR json_extract(s.payload_json,'$.closing_enabled')=1 OR json_extract(s.payload_json,'$.weekly_enabled')=1) LIMIT 1"):
                        for stage,scheduled in (("morning","07:40"),("closing","15:40")):
                            key="shared-sync:"+stage+":"+at.date().isoformat()
                            if at.strftime("%H:%M")>=scheduled and not self.store.one("SELECT key FROM cloud_meta WHERE key=?",(key,)):
                                collector.enqueue(self.store,"sync")
                                self.store.execute("INSERT OR IGNORE INTO cloud_meta VALUES(?,?)",(key,stamp()))
                    jobs.schedule(self.store);last_minute=minute
            except Exception:
                # No exception/payload logging: SMTP and provider errors may contain secrets.
                pass
            self.stop.wait(2)

    def _site(self):
        while not self.stop.is_set():
            try:
                collector.run_one(self.store)
            except Exception:
                pass
            self.stop.wait(2)

    def _maintenance(self):
        while not self.stop.is_set():
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
