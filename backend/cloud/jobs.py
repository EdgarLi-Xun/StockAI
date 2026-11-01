from __future__ import annotations

import copy
import html
import json
import threading
import time
import uuid
from datetime import datetime,timedelta,timezone

from backend.config import now,timestamp
from backend.research import markdown_report
from . import models,workspace
from .auth import AuthError
from .crypto import atomic_private
from .preferences import get_preferences,Preferences
from .store import stamp,json_text

KINDS={"standard","closing","full","supplement","profile_test"}


def enqueue(store,uid,kind="standard",*,automatic=False,at=None,parent_id=None,profile_id=None,ai=True):
    if kind not in KINDS:
        raise ValueError("不支持的个人任务")
    at=at or now();target=at.date().isoformat();ident=uuid.uuid4().hex
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("VERIFICATION_REQUIRED","请先完成邮箱验证",403)
        prefs=get_preferences(store,uid)
        parent=None
        if kind=="supplement":
            parent=workspace.owned(store,"personal_reports",uid,parent_id)
            target=parent["report_date"]
        logical=f"{kind}:{target}" if kind not in {"supplement","profile_test"} else f"{kind}:{ident}"
        old=c.execute("SELECT id,status FROM personal_jobs WHERE user_id=? AND logical_key=?",(uid,logical)).fetchone()
        if old:
            return {**dict(old),"reused":True}
        if c.execute("SELECT COUNT(*) FROM personal_jobs WHERE user_id=? AND status='queued'",(uid,)).fetchone()[0]>=1:
            raise AuthError("QUEUE_FULL","每人最多一项运行任务和一项等待任务，请等待当前队列",409)
        fixed=models.snapshot(store,uid,profile_id)
        snap={"preferences":prefs.model_dump(),"profile":fixed,"ai":bool(ai and (not automatic or prefs.automatic_ai)),"parent_id":parent_id}
        if parent:
            value=json.loads(parent["payload_json"])
            if "source_facts" not in value:
                raise ValueError("此历史报告缺少原始研究事实，不能补充AI；请生成新的研究报告")
            snap["facts"]=value["source_facts"]
        c.execute("INSERT INTO personal_jobs(id,user_id,logical_key,kind,target_date,status,automatic,snapshot_json,created_at) VALUES(?,?,?,?,?,'queued',?,?,?)",(ident,uid,logical,kind,target,int(automatic),json_text(snap),stamp()))
    return {"id":ident,"status":"queued","reused":False}


def claim(store,worker):
    with store.tx(immediate=True) as c:
        if c.execute("SELECT value FROM cloud_meta WHERE key='maintenance'").fetchone()[0]!="0":
            return None
        c.execute("UPDATE personal_jobs SET status='cancelled',error_code='NO_HISTORICAL_CATCHUP',finished_at=? WHERE status='queued' AND kind!='supplement' AND target_date<?",(stamp(),now().date().isoformat()))
        if c.execute("SELECT COUNT(*) FROM personal_jobs WHERE status='running'").fetchone()[0]>=2:
            return None
        row=c.execute("""SELECT j.* FROM personal_jobs j JOIN users u ON u.id=j.user_id
            WHERE j.status='queued' AND u.status='active' AND u.verified_at IS NOT NULL
            AND NOT EXISTS(SELECT 1 FROM personal_jobs r WHERE r.user_id=j.user_id AND r.status='running')
            ORDER BY COALESCE(u.last_dispatch_at,''),j.created_at,j.id LIMIT 1""").fetchone()
        if not row:
            return None
        ts=stamp();until=(datetime.now(timezone.utc)+timedelta(seconds=45)).isoformat(timespec="microseconds")
        c.execute("UPDATE personal_jobs SET status='running',lease_owner=?,lease_until=?,heartbeat_at=?,started_at=COALESCE(started_at,?) WHERE id=? AND status='queued'",(worker,until,ts,ts,row["id"]))
        c.execute("UPDATE users SET last_dispatch_at=? WHERE id=?",(ts,row["user_id"]))
        return {**dict(row),"status":"running","lease_owner":worker}


def heartbeat(store,ident,worker):
    until=(datetime.now(timezone.utc)+timedelta(seconds=45)).isoformat(timespec="microseconds")
    store.execute("UPDATE personal_jobs SET lease_until=?,heartbeat_at=? WHERE id=? AND lease_owner=? AND status='running'",(until,stamp(),ident,worker))


def recover(store):
    """Only invoked while the exclusive application process lock is held."""
    today=now().date().isoformat()
    with store.tx(immediate=True) as c:
        c.execute("UPDATE model_calls SET phase='uncertain',error_code='UNCERTAIN_TIMEOUT',finished_at=? WHERE phase='sent'",(stamp(),))
        for r in c.execute("SELECT * FROM personal_jobs WHERE status IN ('running','queued')").fetchall():
            if not store.active(r["user_id"],c=c):
                c.execute("UPDATE personal_jobs SET status='cancelled',error_code='ACCOUNT_DISABLED',finished_at=? WHERE id=?",(stamp(),r["id"]));continue
            if r["target_date"]!=today and r["kind"]!="supplement":
                c.execute("UPDATE personal_jobs SET status='cancelled',error_code='NO_HISTORICAL_CATCHUP',finished_at=? WHERE id=?",(stamp(),r["id"]));continue
            if r["status"]=="running":
                snap=json.loads(r["snapshot_json"])
                calls=c.execute("SELECT * FROM model_calls WHERE job_id=? ORDER BY attempt",(r["id"],)).fetchall()
                if any(call["phase"] in {"sent","uncertain","received"} for call in calls):
                    snap["recovery_error"]="UNCERTAIN_TIMEOUT"
                    c.execute("UPDATE model_calls SET phase='uncertain',error_code='UNCERTAIN_TIMEOUT',finished_at=? WHERE job_id=? AND phase='sent'",(stamp(),r["id"]))
                elif calls and (len(calls)>=2 or calls[-1]["error_code"] not in {None,"CONNECT_FAILED","RATE_LIMITED","TEMPORARY_PROVIDER_ERROR"}):
                    snap["recovery_error"]=calls[-1]["error_code"] or "INTERNAL_MODEL_ERROR"
                c.execute("UPDATE personal_jobs SET status='queued',lease_owner=NULL,lease_until=NULL,snapshot_json=? WHERE id=?",(json_text(snap),r["id"]))
        # A crashed delivery may be retried while its short-lived token is still valid.
        c.execute("UPDATE mail_outbox SET state='retry',next_at=? WHERE state='sending'",(stamp(),))
        c.execute("UPDATE site_jobs SET status='failed',finished_at=?,error_code='PROCESS_RESTARTED' WHERE status='running'",(stamp(),))


def _log(store,job,message,level="info"):
    with store.tx(immediate=True) as c:
        if c.execute("SELECT 1 FROM personal_jobs WHERE id=? AND user_id=?",(job["id"],job["user_id"])).fetchone():
            c.execute("INSERT INTO personal_job_logs(job_id,ts,level,message) VALUES(?,?,?,?)",(job["id"],stamp(),level,message[:500]))


def publish(store,job,facts,text,meta,error):
    uid=job["user_id"];snap=json.loads(job["snapshot_json"]);value=copy.deepcopy(facts)
    value["source_facts"]=copy.deepcopy(facts)
    if text:
        byid={c["instrument_id"]:c for c in text["candidates"]}
        for c in value["candidates"]:
            c.update({k:byid[c["instrument_id"]][k] for k in ("rationale","counterargument","invalidation")})
    value.update(summary=text["summary"] if text else "今日无合格模拟候选" if not value["candidates"] else f"{len(value['candidates'])}只证券通过规则筛选",market_context=text["market_context"] if text else "使用已存档且带报价时间和来源的事实；风险或数据缺失时不入选。",risk_notes=(text["risk_notes"] if text else [])+[value["fees"]["note"],"主观评分不等于上涨概率；模拟成交前重新核验。"],ai_status="generated" if text else "fallback",ai_error=models.MESSAGES.get(error,error) if error else None,ai_error_code=error,model=meta.get("model","rules-only"),model_parameters=meta,generated_at=timestamp())
    if text:
        value["missing_data"]+=text["missing_data"]
    if len(value["candidates"])>3 or any(not c.get("source_url") or not c.get("quote_time") or c["budget"]["total"]>min(1000,value["budget_limit"]) for c in value["candidates"]):
        raise ValueError("报告事实或预算校验失败")
    ident=uuid.uuid4().hex;directory=store.cfg.data/"reports"/uid
    md=markdown_report(value);doc="<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>StockAI 研究报告</title><style>body{max-width:960px;margin:48px auto;padding:0 24px;font:16px/1.8 system-ui}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><body><pre>"+html.escape(md)+"</pre></body></html>"
    # The same short publication lock protects erasure and backup snapshots. No network here.
    with store.publish_lock,store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c) or not c.execute("SELECT 1 FROM personal_jobs WHERE id=? AND user_id=? AND status='running' AND lease_owner=?",(job["id"],uid,job["lease_owner"])).fetchone():
            raise AuthError("ACCOUNT_DISABLED","账号或任务状态已改变",403)
        mode=value["mode"];day=value["report_date"]
        series=c.execute("SELECT id FROM report_series WHERE user_id=? AND kind=? AND target_date=?",(uid,mode,day)).fetchone()
        sid=series[0] if series else uuid.uuid4().hex
        if not series:
            c.execute("INSERT INTO report_series VALUES(?,?,?,?)",(sid,uid,mode,day))
        version=c.execute("SELECT COALESCE(MAX(version),0)+1 FROM personal_reports WHERE user_id=? AND series_id=?",(uid,sid)).fetchone()[0]
        directory.mkdir(parents=True,exist_ok=True,mode=0o700)
        mdpath=directory/(ident+".md");htmlpath=directory/(ident+".html")
        atomic_private(mdpath,md.encode());atomic_private(htmlpath,doc.encode())
        c.execute("INSERT INTO personal_reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",(ident,uid,sid,version,snap.get("parent_id"),job["id"],mode,day,stamp(),json_text(value),mdpath.relative_to(store.cfg.data).as_posix(),htmlpath.relative_to(store.cfg.data).as_posix()))
        for candidate in value["candidates"]:
            c.execute("INSERT INTO personal_candidates VALUES(?,?,?,?)",(ident,uid,candidate["instrument_id"],json_text(candidate)))
        result={"report_id":ident,"candidate_count":len(value["candidates"]),"ai_status":value["ai_status"],"version":version}
        c.execute("UPDATE personal_jobs SET status='success',finished_at=?,result_json=?,error_code=?,lease_until=NULL WHERE id=? AND user_id=?",(stamp(),json_text(result),error,job["id"],uid))
        c.execute("INSERT INTO personal_notifications(user_id,ts,level,message) VALUES(?,?,?,?)",(uid,stamp(),"info" if text else "warning",f"研究报告第{version}版已归档"+("；"+models.MESSAGES.get(error,error) if error else "")))
    return result


def run(store,keys,job,transport=None):
    snap=json.loads(job["snapshot_json"]);prefs=Preferences.model_validate(snap["preferences"])
    try:
        if not store.active(job["user_id"]):
            raise models.ModelError("ACCOUNT_DISABLED")
        facts=snap.get("facts")
        if facts is None:
            facts=workspace.facts(store,job["user_id"],"standard" if job["kind"]=="profile_test" else job["kind"],prefs)
            if job["kind"]=="profile_test":
                facts.update(candidates=[],excluded=[],watchlist=[],simulation={},backtests=[],sources=[],metals=[],auxiliary_markets=[])
            snap["facts"]=facts
            job["snapshot_json"]=json_text(snap)
            store.execute("UPDATE personal_jobs SET snapshot_json=? WHERE id=? AND user_id=? AND status='running'",(job["snapshot_json"],job["id"],job["user_id"]))
        text=None;meta={};error=snap.get("recovery_error")
        if not error:
            error="AUTO_AI_DISABLED" if not snap["ai"] else "NO_PROFILE" if not snap.get("profile") else None
        if not error:
            previous=store.one("SELECT COALESCE(MAX(attempt),0) n FROM model_calls WHERE job_id=?",(job["id"],))["n"]
            for attempt in range(previous+1,3):
                try:
                    text,meta=models.call_model(store,keys,job,facts,attempt,transport);error=None;break
                except models.ModelError as exc:
                    error=exc.code
                    _log(store,job,models.MESSAGES.get(error,error),"warning")
                    if not exc.retryable or exc.uncertain or attempt>=2:
                        break
                    # A revoked key, account or consent is checked again inside the next send.
                    time.sleep(0.05 if transport else 1)
        if job["kind"]=="profile_test":
            result={"available":bool(text),"error_code":error,"message":models.MESSAGES.get(error,"模型连接及结构校验通过"),**meta}
            with store.user_lock(job["user_id"]),store.tx(immediate=True) as c:
                if store.active(job["user_id"],c=c):
                    c.execute("UPDATE personal_jobs SET status=?,finished_at=?,result_json=?,error_code=? WHERE id=? AND user_id=? AND status='running'",("success" if text else "failed",stamp(),json_text(result),error,job["id"],job["user_id"]))
            return result
        return publish(store,job,facts,text,meta,error)
    except (AuthError,models.ModelError) as exc:
        code=exc.code
    except Exception:
        code="REPORT_GENERATION_FAILED"
    store.execute("UPDATE personal_jobs SET status='failed',error_code=?,finished_at=? WHERE id=? AND user_id=? AND status='running'",(code,stamp(),job["id"],job["user_id"]))
    return {"error_code":code}


def list_jobs(store,uid):
    rows=store.query("SELECT id,logical_key,kind,target_date,status,automatic,created_at,started_at,finished_at,error_code,result_json FROM personal_jobs WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(uid,))
    for row in rows:
        row["result"]=json.loads(row.pop("result_json") or "null");row["error"]=models.MESSAGES.get(row["error_code"],row["error_code"])
        row["attempts"]=store.one("SELECT COUNT(*) n FROM model_calls WHERE job_id=? AND user_id=?",(row["id"],uid))["n"]
    logs=store.query("SELECT l.* FROM personal_job_logs l JOIN personal_jobs j ON j.id=l.job_id WHERE j.user_id=? ORDER BY l.id DESC LIMIT 200",(uid,))
    return {"jobs":rows,"logs":logs}


def schedule(store,at=None):
    at=at or now();ctx=workspace.market.calendar(store,at);hhmm=at.strftime("%H:%M");count=0
    for user in store.query("SELECT id FROM users WHERE status='active' AND verified_at IS NOT NULL"):
        uid=user["id"];prefs=get_preferences(store,uid)
        kinds=[]
        if prefs.morning_enabled and hhmm>=prefs.morning_time:
            kinds.append("standard")
        if prefs.closing_enabled and ctx["is_trading_day"] is True and hhmm>=prefs.closing_time:
            kinds.append("closing")
        if prefs.weekly_enabled and at.weekday()==6 and hhmm>=prefs.weekly_time:
            kinds.append("full")
        for kind in kinds:
            try:
                count+=not enqueue(store,uid,kind,automatic=True,at=at)["reused"]
            except AuthError:
                break
    return count
