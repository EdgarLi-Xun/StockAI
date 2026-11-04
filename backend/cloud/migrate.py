# 从本地数据库复制研究档案到明确用户，保留来源而不修改原数据。
"""Offline, copy-only migration. Publish the prepared directory only after full checks."""
from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

from backend.config import Settings
from .backups import create,file_hash,safe_file
from .crypto import atomic_private
from .preferences import Preferences
from .store import Store,json_text,stamp
from .lifecycle import directory_swap

SHARED=("instruments","bars","market_quotes","calendar","research_context","risk_events")


# 将本地历史报告转换为公网归档结构，并标明旧版事实的边界。
def legacy_facts(payload):
    """Recover only a byte-verifiable legacy fact snapshot; never invent missing history."""
    value=json.loads(json_text(payload));expected=value.get("snapshot_id")
    for k in ("snapshot_id","summary","market_context","risk_notes","ai_status","ai_error","model","model_parameters"):
        value.pop(k,None)
    for c in value.get("candidates",[]):
        c.update(rationale="趋势、成交量与预算规则同时通过；评分仅表示规则匹配程度。",counterargument="历史技术形态不代表未来表现，费用和跳空会改变可执行性。",invalidation="风险核验过期、出现风险公告、行情过期或成交总成本超过预算时失效。")
    if expected and hashlib.sha256(json_text(value).encode()).hexdigest()==expected:
        value["snapshot_id"]=expected
        return value
    return None


# 在目标用户范围内复制自选、报告、模拟账户和实验，并校验关联。
def migrate(store,keys,journal,source,admin_id):
    if store.one("SELECT value FROM cloud_meta WHERE key='legacy_migration'"):
        return {"already_migrated":True}
    admin=store.one("SELECT id FROM users WHERE id=? AND role='admin' AND status='active'",(admin_id,))
    if not admin:
        raise ValueError("必须指定已初始化的有效管理员 UUID")
    if store.one("SELECT COUNT(*) n FROM users")["n"]!=1 or any(store.one(f"SELECT COUNT(*) n FROM {t}")["n"] for t in ("user_watchlist","personal_reports","sim_accounts","personal_backtests","personal_jobs")):
        raise ValueError("迁移只允许在注册开放前、只有初始化管理员的空白研究目录执行")
    source=Path(source).resolve();srcpath=source/"stockai.sqlite3"
    if source==store.cfg.data.resolve() or not srcpath.is_file():
        raise ValueError("需要独立的原本地版数据目录")
    restore_point=create(store,keys,journal)
    with tempfile.TemporaryDirectory(prefix=".stockai-migrate-"+store.site_id+"-",dir=store.cfg.data.parent) as raw:
        root=Path(raw);prepared=root/"prepared";shutil.copytree(store.cfg.data,prepared)
        trial=Store(replace(store.cfg,data=prepared))
        snapshot=root/"legacy.sqlite3";source_db=sqlite3.connect(srcpath);snap=sqlite3.connect(snapshot)
        try:
            source_db.backup(snap)
        finally:
            source_db.close();snap.close()
        old=sqlite3.connect(snapshot);old.row_factory=sqlite3.Row
        try:
            if old.execute("PRAGMA integrity_check").fetchone()[0]!="ok" or old.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("原数据库完整性或关联校验失败")
            source_counts={t:old.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in (*SHARED,"reports","accounts","orders","lots","ledger","equity_snapshots","backtest_runs","jobs")}
            with trial.tx(immediate=True) as c:
                for table in SHARED:
                    columns=[r[1] for r in old.execute(f"PRAGMA table_info({table})")]
                    rows=[dict(r) for r in old.execute(f"SELECT * FROM {table}")]
                    for row in rows:
                        if table=="instruments":
                            row.update(group_name="",pinned=0,watched=0)
                        c.execute(f"INSERT OR REPLACE INTO {table}({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",tuple(row[k] for k in columns))
                for r in old.execute("SELECT * FROM instruments WHERE watched=1"):
                    c.execute("INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,?,?)",(admin_id,r["id"],r["group_name"],r["pinned"],stamp()))
                original=old.execute("SELECT value FROM settings WHERE key='app'").fetchone()
                settings=Settings.model_validate_json(original[0]) if original else Settings()
                prefs=Preferences.model_validate({k:v for k,v in settings.model_dump().items() if k in Preferences.model_fields})
                prefs.morning_enabled=prefs.closing_enabled=prefs.weekly_enabled=prefs.automatic_ai=False
                c.execute("UPDATE user_settings SET payload_json=? WHERE user_id=?",(prefs.model_dump_json(),admin_id))
                profile=uuid.uuid4().hex
                c.execute("INSERT INTO model_profiles VALUES(?,?,?,1,1,1,?)",(profile,admin_id,"迁移的服务器 Ollama",stamp()))
                c.execute("INSERT INTO model_profile_versions VALUES(?,1,'ollama','server','',?,NULL,0,0,?)",(profile,settings.model,stamp()))
                c.execute("UPDATE user_settings SET default_profile_id=? WHERE user_id=?",(profile,admin_id))
                for r in old.execute("SELECT * FROM jobs ORDER BY created_at"):
                    finished=r["status"] not in {"queued","running"}
                    snapdata={"preferences":prefs.model_dump(),"profile":None,"ai":False,"legacy":True}
                    c.execute("INSERT INTO personal_jobs(id,user_id,logical_key,kind,target_date,status,snapshot_json,started_at,finished_at,error_code,result_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(r["id"],admin_id,"legacy:"+r["logical_key"],r["kind"],r["created_at"][:10],r["status"] if finished else "interrupted",json_text(snapdata),r["started_at"],r["finished_at"],None if finished else "MIGRATED_REVIEW_REQUIRED",r["result_json"],r["created_at"]))
                for r in old.execute("SELECT * FROM job_logs"):
                    if r["job_id"]:
                        c.execute("INSERT INTO personal_job_logs(job_id,ts,level,message) VALUES(?,?,?,?)",(r["job_id"],r["ts"],r["level"],r["message"]))
                for r in old.execute("SELECT * FROM reports ORDER BY generated_at,id"):
                    series=c.execute("SELECT id FROM report_series WHERE user_id=? AND kind=? AND target_date=?",(admin_id,r["mode"],r["report_date"])).fetchone()
                    sid=series[0] if series else uuid.uuid4().hex
                    if not series:
                        c.execute("INSERT INTO report_series VALUES(?,?,?,?)",(sid,admin_id,r["mode"],r["report_date"]))
                    version=c.execute("SELECT COALESCE(MAX(version),0)+1 FROM personal_reports WHERE series_id=?",(sid,)).fetchone()[0]
                    paths=[]
                    for key,ext in (("markdown_path","md"),("html_path","html")):
                        src=safe_file(source/"reports",Path(r[key]).as_posix());dest=prepared/"reports"/admin_id/(r["id"]+"."+ext)
                        if not src.is_file() or src.is_symlink():
                            raise ValueError("原报告文件缺失或路径无效，迁移已中止")
                        atomic_private(dest,src.read_bytes())
                        if file_hash(src)!=file_hash(dest):
                            raise ValueError("报告复制校验失败")
                        paths.append(dest.relative_to(prepared).as_posix())
                    payload=json.loads(r["payload_json"]);original_facts=legacy_facts(payload)
                    if original_facts:
                        payload["source_facts"]=original_facts
                    c.execute("INSERT INTO personal_reports VALUES(?,?,?,?,NULL,?,?,?,?,?,?,?)",(r["id"],admin_id,sid,version,r["job_id"],r["mode"],r["report_date"],r["generated_at"],json_text(payload),*paths))
                for r in old.execute("SELECT * FROM candidates"):
                    c.execute("INSERT INTO personal_candidates VALUES(?,?,?,?)",(r["report_id"],admin_id,r["instrument_id"],r["payload_json"]))
                mapping={"accounts":"sim_accounts","orders":"sim_orders","lots":"sim_lots","ledger":"sim_ledger","equity_snapshots":"sim_equity","backtest_runs":"personal_backtests"}
                for source_table,target_table in mapping.items():
                    cols=[r[1] for r in old.execute(f"PRAGMA table_info({source_table})")]
                    for r in old.execute(f"SELECT * FROM {source_table}"):
                        c.execute(f"INSERT INTO {target_table}({','.join(cols)},user_id) VALUES({','.join('?' for _ in range(len(cols)+1))})",(*[r[k] for k in cols],admin_id))
                for r in old.execute("SELECT * FROM notifications"):
                    c.execute("INSERT INTO personal_notifications(user_id,ts,level,message,read) VALUES(?,?,?,?,?)",(admin_id,r["ts"],r["level"],r["message"],r["read"]))
                for source_table,target_table in {"reports":"personal_reports","jobs":"personal_jobs",**mapping}.items():
                    if c.execute(f"SELECT COUNT(*) FROM {target_table} WHERE user_id=?",(admin_id,)).fetchone()[0]!=source_counts[source_table]:
                        raise ValueError("迁移前后个人记录数量不一致")
                if c.execute("PRAGMA foreign_key_check").fetchall():
                    raise ValueError("迁移后的外键校验失败")
                manifest={"source_sha256":file_hash(snapshot),"counts":source_counts,"owner_id":admin_id,"completed_at":stamp(),"restore_point":restore_point["id"]}
                c.execute("INSERT INTO cloud_meta VALUES('legacy_migration',?)",(json_text(manifest),))
        finally:
            old.close()
        directory_swap(store,keys,prepared)
    return manifest
