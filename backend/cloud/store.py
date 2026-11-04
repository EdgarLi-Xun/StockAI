# 管理公网 SQLite 存储、用户锁、事务、连接取消及多用户表结构。
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone

from backend.db import SCHEMA as LEGACY_SCHEMA
from .config import CloudConfig

# 共享行情继续复用基础表；个人资源通过显式 user_id 与复合外键限制归属。
# 模型配置按版本存档、凭据独立加密；任务和报告分别保留逻辑幂等键与版本。
SCHEMA="""
CREATE TABLE IF NOT EXISTS cloud_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS users(
 id TEXT PRIMARY KEY,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('admin','user')),
 status TEXT NOT NULL CHECK(status IN ('pending','active','disabled')),verified_at TEXT,created_at TEXT NOT NULL,last_dispatch_at TEXT);
CREATE TABLE IF NOT EXISTS sessions(
 token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,csrf_hash TEXT NOT NULL,
 expires_at TEXT NOT NULL,revoked_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS invitations(
 id TEXT PRIMARY KEY,code_hash TEXT NOT NULL UNIQUE,created_by TEXT NOT NULL REFERENCES users(id),created_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,revoked_at TEXT,used_by TEXT,used_at TEXT);
CREATE TABLE IF NOT EXISTS auth_tokens(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,purpose TEXT NOT NULL,
 token_hash TEXT NOT NULL UNIQUE,expires_at TEXT NOT NULL,consumed_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rate_limits(key TEXT PRIMARY KEY,window_start REAL NOT NULL,hits INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS mail_outbox(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,purpose TEXT NOT NULL,
 payload_json TEXT,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_at TEXT NOT NULL,
 expires_at TEXT NOT NULL,error_code TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_settings(
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,payload_json TEXT NOT NULL,default_profile_id TEXT);
CREATE TABLE IF NOT EXISTS workspace_setup(
 user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,source TEXT NOT NULL,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS user_watchlist(
 user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,instrument_id TEXT NOT NULL REFERENCES instruments(id),
 group_name TEXT NOT NULL,pinned INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL,
 cost_price TEXT,quantity INTEGER,PRIMARY KEY(user_id,instrument_id));
CREATE TABLE IF NOT EXISTS model_profiles(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,name TEXT NOT NULL,
 current_version INTEGER NOT NULL,active INTEGER NOT NULL DEFAULT 1,needs_reconnect INTEGER NOT NULL DEFAULT 0,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_credentials(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,
 envelope_json TEXT,key_hint TEXT NOT NULL,revoked_at TEXT,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_profile_versions(
 profile_id TEXT NOT NULL REFERENCES model_profiles(id) ON DELETE CASCADE,version INTEGER NOT NULL,
 provider TEXT NOT NULL,region TEXT NOT NULL,base_url TEXT NOT NULL,model TEXT NOT NULL,
 credential_id TEXT REFERENCES model_credentials(id),allow_summary INTEGER NOT NULL,allow_trades INTEGER NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(profile_id,version));
CREATE TABLE IF NOT EXISTS personal_jobs(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,logical_key TEXT NOT NULL,
 kind TEXT NOT NULL,target_date TEXT NOT NULL,status TEXT NOT NULL,automatic INTEGER NOT NULL DEFAULT 0,
 snapshot_json TEXT NOT NULL,lease_owner TEXT,lease_until TEXT,heartbeat_at TEXT,started_at TEXT,finished_at TEXT,
 error_code TEXT,result_json TEXT,created_at TEXT NOT NULL,UNIQUE(user_id,logical_key));
CREATE TABLE IF NOT EXISTS personal_job_logs(
 id INTEGER PRIMARY KEY,job_id TEXT NOT NULL REFERENCES personal_jobs(id) ON DELETE CASCADE,ts TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS model_calls(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,job_id TEXT NOT NULL REFERENCES personal_jobs(id) ON DELETE CASCADE,
 profile_id TEXT NOT NULL,profile_version INTEGER NOT NULL,attempt INTEGER NOT NULL,phase TEXT NOT NULL,
 error_code TEXT,usage_json TEXT,provider_request_id TEXT,created_at TEXT NOT NULL,finished_at TEXT,
 UNIQUE(job_id,attempt));
CREATE TABLE IF NOT EXISTS report_series(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,target_date TEXT NOT NULL,
 UNIQUE(user_id,kind,target_date));
CREATE TABLE IF NOT EXISTS personal_reports(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,series_id TEXT NOT NULL REFERENCES report_series(id) ON DELETE CASCADE,
 version INTEGER NOT NULL,parent_id TEXT,job_id TEXT REFERENCES personal_jobs(id) ON DELETE SET NULL,mode TEXT NOT NULL,
 report_date TEXT NOT NULL,generated_at TEXT NOT NULL,payload_json TEXT NOT NULL,markdown_path TEXT NOT NULL,html_path TEXT NOT NULL,
 UNIQUE(series_id,version),UNIQUE(id,user_id));
CREATE TABLE IF NOT EXISTS personal_candidates(
 report_id TEXT NOT NULL,user_id TEXT NOT NULL,instrument_id TEXT NOT NULL REFERENCES instruments(id),payload_json TEXT NOT NULL,
 PRIMARY KEY(report_id,instrument_id),FOREIGN KEY(report_id,user_id) REFERENCES personal_reports(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS sim_accounts(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,name TEXT NOT NULL,initial_cash TEXT NOT NULL,cash TEXT NOT NULL,
 created_at TEXT NOT NULL,UNIQUE(id,user_id));
CREATE TABLE IF NOT EXISTS sim_orders(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,client_key TEXT NOT NULL,account_id TEXT NOT NULL,instrument_id TEXT NOT NULL REFERENCES instruments(id),
 side TEXT NOT NULL,quantity INTEGER NOT NULL,limit_price TEXT NOT NULL,stop_loss TEXT,status TEXT NOT NULL,created_at TEXT NOT NULL,
 filled_at TEXT,fill_price TEXT,fees TEXT,cost TEXT,source_ts TEXT,reason TEXT,rules_json TEXT NOT NULL,fees_json TEXT NOT NULL,
 UNIQUE(user_id,client_key),UNIQUE(id,user_id),FOREIGN KEY(account_id,user_id) REFERENCES sim_accounts(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS sim_lots(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,account_id TEXT NOT NULL,instrument_id TEXT NOT NULL REFERENCES instruments(id),quantity INTEGER NOT NULL,
 cost TEXT NOT NULL,acquired_day TEXT NOT NULL,settlement_days INTEGER NOT NULL,
 FOREIGN KEY(account_id,user_id) REFERENCES sim_accounts(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS sim_ledger(
 id INTEGER PRIMARY KEY,user_id TEXT NOT NULL,account_id TEXT NOT NULL,order_id TEXT,ts TEXT NOT NULL,delta TEXT NOT NULL,balance TEXT NOT NULL,description TEXT NOT NULL,
 UNIQUE(order_id),FOREIGN KEY(account_id,user_id) REFERENCES sim_accounts(id,user_id) ON DELETE CASCADE,
 FOREIGN KEY(order_id,user_id) REFERENCES sim_orders(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS sim_equity(
 account_id TEXT NOT NULL,user_id TEXT NOT NULL,ts TEXT NOT NULL,equity TEXT NOT NULL,PRIMARY KEY(account_id,ts),
 FOREIGN KEY(account_id,user_id) REFERENCES sim_accounts(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS personal_backtests(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,created_at TEXT NOT NULL,params_json TEXT NOT NULL,
 result_json TEXT NOT NULL,data_hash TEXT NOT NULL,strategy_version TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS personal_notifications(
 id INTEGER PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,ts TEXT NOT NULL,level TEXT NOT NULL,message TEXT NOT NULL,read INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS reauth_grants(
 token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,session_hash TEXT NOT NULL,
 purpose TEXT NOT NULL,resource_id TEXT NOT NULL,expires_at TEXT NOT NULL,consumed_at TEXT);
CREATE TABLE IF NOT EXISTS admin_actions(
 id INTEGER PRIMARY KEY,actor_id TEXT NOT NULL,action TEXT NOT NULL,target_id TEXT,ts TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS site_jobs(
 id TEXT PRIMARY KEY,logical_key TEXT NOT NULL UNIQUE,kind TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,
 started_at TEXT,finished_at TEXT,error_code TEXT,result_json TEXT);
CREATE INDEX IF NOT EXISTS ix_owned_reports ON personal_reports(user_id,generated_at DESC);
CREATE INDEX IF NOT EXISTS ix_owned_jobs ON personal_jobs(user_id,status,created_at);
CREATE INDEX IF NOT EXISTS ix_dispatch ON personal_jobs(status,created_at);
CREATE INDEX IF NOT EXISTS ix_owned_orders ON sim_orders(user_id,status);
"""


# 生成 UTC 微秒时间戳，供跨线程排序、租约和归档使用。
def stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# 生成紧凑且保留中文的 JSON，拒绝 NaN 以维持稳定的签名输入。
def json_text(value):
    return json.dumps(value,ensure_ascii=False,allow_nan=False,separators=(",",":"),default=str)


# 检查 SQLite 版本是否包含受支持的 WAL 重置修复。
def wal_fixed(version):
    v=tuple(map(int,version.split(".")))
    return v>=(3,51,3) or (3,50,7)<=v<(3,51,0) or (3,44,6)<=v<(3,45,0)


# 封装共享数据库访问，并提供用户隔离和发布阶段的同步机制。
class Store:
    # 绑定数据目录，并创建发布锁、用户锁和进行中网络请求的索引。
    def __init__(self,cfg:CloudConfig):
        self.cfg=cfg
        self.path=cfg.data/"stockai.sqlite3"
        self.publish_lock=threading.RLock()
        self._locks={}
        self._locks_lock=threading.Lock()
        self._requests={}
        self._requests_lock=threading.Lock()
        self.research_cache_lock=threading.RLock()
        self.research_cache={}

    # 显式指定用户后复用可重入锁，串行化同一用户的敏感更新。
    def user_lock(self,uid):
        if not uid:
            raise ValueError("必须显式指定用户")
        with self._locks_lock:
            return self._locks.setdefault(uid,threading.RLock())

    # 启用外键和安全删除，统一执行提交、异常回滚及连接关闭。
    @contextmanager
    def tx(self,immediate=False):
        c=sqlite3.connect(self.path,timeout=15)
        c.row_factory=sqlite3.Row
        c.create_function('stockai_write_capability',0,lambda:3)
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA secure_delete=ON")
        c.execute("PRAGMA busy_timeout=15000")
        if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='cloud_meta'").fetchone():
            minimum=c.execute("SELECT value FROM cloud_meta WHERE key='minimum_write_capability'").fetchone()
            if minimum and int(minimum[0])>3:c.close();raise RuntimeError('此工作区需要更新版本，旧代码拒绝写入')
        try:
            if immediate:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    # 执行参数化查询并返回字典记录列表。
    def query(self,sql,args=()):
        with self.tx() as c:
            return [dict(r) for r in c.execute(sql,args)]

    # 返回首条查询记录，查无结果时返回空值。
    def one(self,sql,args=()):
        rows=self.query(sql,args)
        return rows[0] if rows else None

    # 执行事务内写操作并返回受影响行数。
    def execute(self,sql,args=()):
        with self.tx() as c:
            return c.execute(sql,args).rowcount

    # 校验 SQLite 与部署配置，增量建立表结构并初始化站点观察标的。
    def initialize(self):
        self.research_cache.clear()
        self.cfg.validate()
        self.cfg.data.mkdir(parents=True,exist_ok=True,mode=0o700)
        fixed=wal_fixed(sqlite3.sqlite_version)
        if not fixed and not self.cfg.dev:
            raise RuntimeError("SQLite 需要 WAL-reset 修复版本（3.51.3+、3.50.7 或3.44.6）；拒绝启动公网服务")
        with self.tx() as c:
            # 开发环境使用较旧 SQLite 时退回回滚日志模式，避免启用未修复的 WAL。
            c.executescript(LEGACY_SCHEMA.replace("PRAGMA journal_mode=WAL;","PRAGMA journal_mode="+("WAL" if fixed else "DELETE")+";"))
            c.executescript(SCHEMA)
            columns={r[1] for r in c.execute('PRAGMA table_info(user_watchlist)')}
            if 'cost_price' not in columns:c.execute('ALTER TABLE user_watchlist ADD COLUMN cost_price TEXT')
            if 'quantity' not in columns:c.execute('ALTER TABLE user_watchlist ADD COLUMN quantity INTEGER')
            from .research_schema import SCHEMA as RESEARCH_SCHEMA,upgrade
            c.executescript(RESEARCH_SCHEMA)
            upgrade(c)
            from backend.sync.schema import install as install_sync
            install_sync(c)
            c.execute("INSERT OR IGNORE INTO cloud_meta VALUES('site_id',?)",(uuid.uuid4().hex,))
            c.execute("INSERT OR IGNORE INTO cloud_meta VALUES('schema_version','1')")
            c.execute("INSERT OR IGNORE INTO cloud_meta VALUES('maintenance','0')")
            from backend.instruments import Rules
            for market,symbol,name,asset in (("SH","000001","上证指数","index"),("SZ","399001","深证成指","index"),("SZ","399006","创业板指","index"),("FX","XAUUSD","伦敦金现货","metal"),("FX","XAGUSD","伦敦银现货","metal"),("SHFE","AU0","沪金主力连续","future"),("SHFE","AG0","沪银主力连续","future")):
                c.execute("INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,'',0,?)",(market+":"+symbol,symbol,market,name,asset,Rules().model_dump_json()))
        return self

    # 登记任务当前网络连接，供取消操作中断请求。
    def connection_for_job(self,ident,sock):
        with self._requests_lock:
            self._requests[ident]=sock

    # 尝试关闭指定任务的套接字，使已停止任务尽快退出网络等待。
    def cancel_connection(self,ident):
        import socket
        with self._requests_lock:
            sock=self._requests.get(ident)
        if sock:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

    # 请求结束后移除连接索引，避免后续取消操作命中过期连接。
    def forget_connection(self,ident):
        with self._requests_lock:
            self._requests.pop(ident,None)

    # 读取持久化的站点标识，作为加密与权威签名的绑定范围。
    @property
    def site_id(self):
        return self.one("SELECT value FROM cloud_meta WHERE key='site_id'")["value"]

    # 按显式用户标识查询有效账号，可在调用方事务内复用连接。
    def active(self,uid,verified=True,c=None):
        if not uid:
            return None
        sql="SELECT * FROM users WHERE id=? AND status='active'"+(" AND verified_at IS NOT NULL" if verified else "")
        if c is None:
            return self.one(sql,(uid,))
        row=c.execute(sql,(uid,)).fetchone()
        return dict(row) if row else None

    # 记录管理员、动作和目标资源，保留站点维护审计线索。
    def audit(self,actor,action,target=None):
        self.execute("INSERT INTO admin_actions(actor_id,action,target_id,ts) VALUES(?,?,?,?)",(actor,action,target,stamp()))

    # 仅为仍有效的用户写入个人通知，避免删除或停用后的回写。
    def notify(self,uid,message,level="info"):
        with self.tx(immediate=True) as c:
            if self.active(uid,c=c):
                c.execute("INSERT INTO personal_notifications(user_id,ts,level,message) VALUES(?,?,?,?)",(uid,stamp(),level,message[:1000]))
