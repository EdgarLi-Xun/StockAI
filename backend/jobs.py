# 管理本地后台任务、幂等归档及上海时区的内建任务调度。
from __future__ import annotations

import json
import smtplib
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.message import EmailMessage

from . import collectors, db, maintenance, research, simulation
from .config import now, timestamp

EXECUTOR=ThreadPoolExecutor(max_workers=1,thread_name_prefix="stockai-job")
STOP=threading.Event()
SCHEDULE_LOCK=threading.Lock()


# 记录任务时间、日志级别及消息，供任务中心查询。
def log(job_id,message,level="info"):
    db.execute("INSERT INTO job_logs(job_id,ts,level,message) VALUES(?,?,?,?)",(job_id,timestamp(),level,str(message)[:2000]))


# 按逻辑键复用已有任务或提交新任务，防止重复执行与归档。
def enqueue(kind: str, logical_key: str | None=None, retry=False):
    if kind not in {"sync","standard","full","closing","match","metals","backup"}:
        raise ValueError("未知任务类型")
    if not logical_key:
        logical_key=f"{kind}:{now().date().isoformat()}" if kind in {"standard","full","closing"} else f"{kind}:{uuid.uuid4().hex}"
    queued_id=None
    with SCHEDULE_LOCK,db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        previous=c.execute("SELECT * FROM jobs WHERE logical_key=?",(logical_key,)).fetchone()
        if previous:
            row=dict(previous)
            if retry and row["status"] in {"failed","interrupted","partial","missed"}:
                c.execute("UPDATE jobs SET status='queued',error=NULL,finished_at=NULL WHERE id=?",(row["id"],))
                queued_id=row["id"]
                row["status"]="queued"
        else:
            if c.execute("SELECT COUNT(*) FROM jobs WHERE status IN ('queued','running')").fetchone()[0]>=8:
                raise ValueError("任务队列已满，请稍后重试")
            ident=uuid.uuid4().hex
            c.execute("INSERT INTO jobs(id,logical_key,kind,status,created_at) VALUES(?,?,?,?,?)",(ident,logical_key,kind,"queued",timestamp()))
            row={"id":ident}
            queued_id=ident
    if queued_id:
        EXECUTOR.submit(run,queued_id)
    return db.one("SELECT * FROM jobs WHERE id=?",(row["id"],))


# 按已启用的 SMTP 配置发送通知，密码仅从进程环境读取。
def send_email(message):
    import os
    cfg=db.settings()
    password=os.environ.get("STOCKAI_SMTP_PASSWORD","")
    if not cfg.email_enabled:
        return
    if not all((cfg.smtp_host,cfg.smtp_user,cfg.email_to,password)):
        db.notify("邮件通知配置不完整，未发送；请配置 SMTP 参数及环境变量凭据。","warning")
        return
    email=EmailMessage()
    email["Subject"]="StockAI 任务通知"
    email["From"]=cfg.smtp_user
    email["To"]=cfg.email_to
    email.set_content(message)
    try:
        with smtplib.SMTP_SSL(cfg.smtp_host,cfg.smtp_port,timeout=15) as server:
            server.login(cfg.smtp_user,password)
            server.send_message(email)
    except Exception:
        db.notify("邮件发送失败，请检查配置和网络；凭据未写入日志。","warning")


# 执行指定任务并统一落库结果、错误、日志及用户通知。
def run(ident):
    with maintenance.LOCK:
        row=db.one("SELECT * FROM jobs WHERE id=?",(ident,))
        if not row or row["status"]!="queued":
            return
        fnlog=lambda m:log(ident,m)
        db.execute("UPDATE jobs SET status='running',started_at=?,attempts=attempts+1 WHERE id=?",(timestamp(),ident))
        fnlog(f"开始 {row['kind']}；幂等键 {row['logical_key']}")
        try:
            result={}
            if row["kind"]=="sync":
                for retry in range(3):
                    if retry:
                        fnlog(f"退避重试 {retry} / 2")
                        STOP.wait(2**retry)
                        db.execute("UPDATE jobs SET attempts=attempts+1 WHERE id=?",(ident,))
                    result=collectors.sync_all(fnlog)
                    if result["status"]!="unavailable":
                        break
                if result["status"]=="unavailable":
                    raise ValueError("全部行情源不可用；缓存未被覆盖，未生成候选")
            elif row["kind"] in {"standard","full","closing"}:
                # 每个逻辑报告任务只同步一次数据，已经归档的旧报告不随刷新改写。
                sync_result=collectors.sync_all(fnlog)
                if row["kind"]=="full":
                    from . import backtest, context, instruments
                    deep=context.sync(fnlog)
                    shortlisted=research.build_facts("full")["candidates"]
                    for item in shortlisted:
                        security=instruments.get(item["instrument_id"])
                        for tf in ("15m","60m"):
                            r=collectors.sync_instrument(security,tf)
                            fnlog(f"{item['instrument_id']} {tf}: {r['status']}")
                        try:
                            backtest.create(backtest.Parameters(instrument_id=item["instrument_id"],initial_cash=db.settings().budget))
                        except ValueError as exc:
                            fnlog(f"{item['instrument_id']} 周度历史验证未通过：{exc}")
                if row["kind"]=="closing":
                    simulation.evaluate_rules(fnlog)
                    simulation.match_orders(fnlog)
                result=research.generate(row["logical_key"],row["kind"],ident,fnlog)
                result["data_status"]=sync_result["status"]
                result["data_sync"]=sync_result
                if row["kind"]=="full":
                    result["deep_sources"]=deep
            elif row["kind"]=="metals":
                result={"metals":collectors.sync_metals(fnlog)}
                if not any(x.get("status")=="success" for x in result["metals"]):
                    raise ValueError("黄金白银数据源不可用，未填造价格")
            elif row["kind"]=="match":
                simulation.evaluate_rules(fnlog)
                result={"fills":simulation.match_orders(fnlog)}
            elif row["kind"]=="backup":
                result=maintenance.create_backup()
            state="partial" if result.get("status")=="partial" or result.get("data_status") in {"partial","unavailable"} else "success"
            db.execute("UPDATE jobs SET status=?,finished_at=?,result_json=? WHERE id=?",(state,timestamp(),db.dumps(result),ident))
            fnlog("任务完成" if state=="success" else "任务完成，部分数据不可用，详见来源状态")
            if state=="partial":
                db.notify("任务部分完成，存在数据源缺失。","warning")
            send_email(f"StockAI {row['kind']} {state}")
        except Exception as exc:
            message=f"{type(exc).__name__}: {str(exc)[:1000]}"
            db.execute("UPDATE jobs SET status='failed',finished_at=?,error=? WHERE id=?",(timestamp(),message,ident))
            fnlog(message,"error")
            db.notify(f"任务 {row['kind']} 失败：{message}","error")
            send_email(f"StockAI {row['kind']} 失败，请查看本机任务中心。")


# 返回唯一内建调度源、时区和各任务的计划说明。
def schedule_info():
    cfg=db.settings()
    return {"source":"builtin","timezone":"Asia/Shanghai","enabled":cfg.scheduler_enabled and __import__("os").environ.get("STOCKAI_DISABLE_SCHEDULER")!="1","plans":[{"kind":"standard","label":"标准版早报","time":cfg.morning_time,"rule":"每天，A股使用最近已完成交易日数据"},{"kind":"closing","label":"收盘跟踪","time":cfg.closing_time,"rule":"仅经日历核验的A股交易日"},{"kind":"full","label":"完整版周度复盘","time":cfg.weekly_time,"rule":"每周日"}],"catch_up":"主机恢复后补跑当天已到期任务；更早缺报显示为缺失，可手动按原日期补跑，不伪造历史价格。","windows_task_scheduler":"仅负责登录后启动服务，不注册第二套08/16/20点研究任务。"}


# 按上海时间检查到期计划，并通过逻辑键确保同日任务幂等。
def tick(at=None):
    at=at or now()
    cfg=db.settings()
    if not cfg.scheduler_enabled:
        return
    today=at.date().isoformat()
    hm=at.strftime("%H:%M")
    context=collectors.calendar_context(at)
    plans=[("standard",cfg.morning_time,True),("closing",cfg.closing_time,context["is_trading_day"] is True),("full",cfg.weekly_time,at.weekday()==6)]
    for kind,when,eligible in plans:
        if eligible and hm>=when:
            key=f"{kind}:{today}"
            existing=db.one("SELECT status FROM jobs WHERE logical_key=?",(key,))
            enqueue(kind,key,retry=bool(existing and existing["status"] in {"interrupted","missed"}))
    if at.weekday()==6 and hm>=cfg.weekly_time:
        enqueue("backup",f"backup:{today}")
    db.execute("INSERT INTO settings VALUES('last_scheduler_tick',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(at.isoformat(),))


# 记录错过的历史计划，避免补造对应日期的行情与研究报告。
def record_missed(at=None):
    at=at or now()
    row=db.one("SELECT value FROM settings WHERE key='last_scheduler_tick'")
    if not row:
        return  # 只依据已保存的任务记录恢复状态，不补造从未持久化的执行历史。
    last=datetime.fromisoformat(row["value"])
    if last.date()>=at.date():
        return
    cfg=db.settings()
    day=max(last.date(),at.date()-timedelta(days=30))
    count=0
    while day<at.date():
        context=collectors.calendar_context(datetime.combine(day,datetime.min.time()).replace(hour=23,tzinfo=at.tzinfo))
        for kind,when,eligible in [("standard",cfg.morning_time,True),("closing",cfg.closing_time,context["is_trading_day"] is True),("full",cfg.weekly_time,day.weekday()==6)]:
            planned=datetime.fromisoformat(day.isoformat()+"T"+when+":00+08:00")
            if eligible and planned>last:
                count+=db.execute("INSERT OR IGNORE INTO jobs(id,logical_key,kind,status,finished_at,error,created_at) VALUES(?,?,?,?,?,?,?)",(uuid.uuid4().hex,f"{kind}:{day.isoformat()}",kind,"missed",timestamp(),"服务未运行，错过计划；可手动补跑，使用实际采集时点并保留原计划日期。",timestamp()))
        day+=timedelta(days=1)
    if count:
        db.notify(f"检测到{count}项历史计划缺报；已标记错过执行，可在任务中心补跑。","warning")


# 恢复任务状态并启动内建调度循环。
def start_scheduler():
    STOP.clear()
    record_missed()
    db.execute("UPDATE jobs SET status='interrupted',finished_at=?,error='服务退出导致任务中断，可按原幂等键补跑' WHERE status IN ('running','queued')",(timestamp(),))
    # 按固定间隔检查到期任务，记录异常后继续等待下一轮。
    def loop():
        while not STOP.wait(30):
            try:
                tick()
            except Exception:
                db.notify("调度检查失败，请查看任务中心。","error")
    thread=threading.Thread(target=loop,name="stockai-scheduler",daemon=True)
    thread.start()
    return thread
