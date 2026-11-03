# 本地 FastAPI 入口，封装鉴权、研究数据、模拟交易与维护接口。
from __future__ import annotations

import csv
import io
import json
import os
import sqlite3
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from . import ai, analytics, backtest, collectors, db, instruments, jobs, maintenance, research, security, simulation
from .budget import budget_check
from .config import DATA, ROOT, Settings, VERSION, now, timestamp


# 启动数据库、访问令牌和调度器，并在应用退出时收尾。
@asynccontextmanager
async def lifespan(app):
    db.initialize()
    security.local_token()
    if os.environ.get("STOCKAI_DISABLE_SCHEDULER")!="1":
        jobs.start_scheduler()
    yield
    jobs.STOP.set()


app=FastAPI(title="StockAI Local API",version=VERSION,lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)


# 将业务参数错误转换为受长度限制的客户端错误响应。
@app.exception_handler(ValueError)
async def value_error(request,exc):
    return JSONResponse({"detail":str(exc)[:2000]},status_code=400)


# 将重复记录与关联约束失败转换为可理解的冲突提示。
@app.exception_handler(sqlite3.IntegrityError)
async def integrity_error(request,exc):
    return JSONResponse({"detail":"记录重复或关联校验失败，请刷新后重试"},status_code=409)


# 将缺少必要字段的错误转换为请求校验提示。
@app.exception_handler(KeyError)
async def missing_field(request,exc):
    return JSONResponse({"detail":"缺少必填字段："+str(exc)},status_code=422)


# 统一检查主机、来源、访问令牌及写入权限，再交给业务接口。
@app.middleware("http")
async def guard(request: Request,call_next):
    if not security.valid_host(request) or not security.peer_allowed(security.effective_peer(request)) or (security.forwarded(request) and not db.settings().lan_enabled):
        return JSONResponse({"detail":"此来源不允许访问本地服务"},status_code=403)
    if not security.origin_allowed(request):
        return JSONResponse({"detail":"拒绝跨站请求"},status_code=403)
    path=request.url.path
    public={"/api/health","/api/session","/api/login"}
    if path.startswith("/api/") and path not in public:
        role=security.authenticate(request)
        if not role:
            return JSONResponse({"detail":"需要登录"},status_code=401)
        request.state.role=role
        if role=="reader" and request.method not in {"GET","HEAD"}:
            return JSONResponse({"detail":"只读用户不能修改数据或触发任务"},status_code=403)
        if role=="reader" and path.startswith("/api/backups"):
            return JSONResponse({"detail":"数据备份仅限管理员访问"},status_code=403)
    if request.headers.get("content-length") and int(request.headers["content-length"])>10*1024*1024:
        return JSONResponse({"detail":"请求体超过10MB"},status_code=413)
    response=await call_next(request)
    response.headers["X-Content-Type-Options"]="nosniff"
    response.headers["X-Frame-Options"]="DENY"
    response.headers["Referrer-Policy"]="same-origin"
    response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'"
    if path.startswith("/api/"):
        response.headers["Cache-Control"]="no-store"
    return response


# 返回服务版本、上海时间与仅支持模拟交易的运行模式。
@app.get("/api/health")
def health():
    return {"status":"ok","version":VERSION,"time":timestamp(),"timezone":"Asia/Shanghai","mode":"simulation-only"}


# 只允许可信本机请求引导管理员会话，其他来源必须登录。
@app.get("/api/session")
def session(request: Request):
    if not security.local_request(request):
        return {"requires_login":True}
    return {"token":security.local_token(),"role":"admin","requires_login":False}


# 校验登录角色与密码，并返回带权限的会话令牌。
@app.post("/api/login")
def login(payload:dict,request:Request):
    return security.login(str(payload.get("password","")),str(payload.get("role","admin")),request.client.host)


# 返回本机 AI 服务的可用性检查结果。
@app.get("/api/model/health")
def model_health():
    return ai.health()


# 合并自选、最新行情、风险障碍和预算估算，生成列表视图。
def watch_rows():
    cfg=db.settings()
    context=collectors.calendar_context()
    result=[]
    for r in db.query("SELECT id FROM instruments WHERE watched=1 ORDER BY pinned DESC,group_name,symbol"):
        item=instruments.get(r["id"])
        bars=collectors.bars_for(item["id"],limit=2)
        last=bars[-1] if bars else None
        observation=db.one("SELECT payload_json FROM market_quotes WHERE instrument_id=?",(item["id"],))
        if not last and observation:
            q=json.loads(observation["payload_json"])
            last={"price":q["price"],"ts":q["quote_time"],"source":q["source"],"source_url":q["source_url"],"fetched_at":q["fetched_at"],"unit":q["unit"],"currency":q["currency"],"kind":q["kind"],"timestamp_basis":q.get("timestamp_basis",q.get("timezone",""))}
        estimate=None
        if last and item["asset_class"] in {"stock","etf"} and item["market"] in {"SH","SZ"}:
            try:
                estimate=budget_check(last["close"],item["rules"],item["asset_class"],cfg.fees,cfg.budget)
            except ValueError:
                pass
        change=(last["close"]/bars[-2]["close"]-1)*100 if len(bars)>1 else None
        ordinary=item["market"] in {"SH","SZ"} and item["asset_class"] in {"stock","etf"}
        result.append({**item,"quote":last,"change_pct":round(change,3) if change is not None else None,"budget":estimate,"observation_only":not ordinary,"exclusion_reasons":instruments.risk_reasons(item)+collectors.fresh_reasons(last,context) if ordinary else ["仅供独立观察，不参与普通模拟候选"]})
    return result


# 汇总自选数量、报价覆盖、最近报告和通知等总览数据。
@app.get("/api/dashboard")
def dashboard():
    rows=watch_rows()
    recent=db.query("SELECT id,mode,report_date,generated_at,payload_json FROM reports ORDER BY generated_at DESC LIMIT 5")
    reports=[]
    for r in recent:
        p=json.loads(r.pop("payload_json"))
        reports.append({**r,"summary":p["summary"],"candidate_count":len(p["candidates"]),"ai_status":p["ai_status"]})
    candidates=json.loads(db.one("SELECT payload_json FROM reports ORDER BY generated_at DESC LIMIT 1")["payload_json"])["candidates"] if reports else []
    return {"version":VERSION,"time":timestamp(),"calendar":collectors.calendar_context(),"stats":{"watch_count":len(rows),"quote_count":sum(r["quote"] is not None for r in rows),"verified_count":sum(not instruments.risk_reasons(r) for r in rows),"candidate_count":len(candidates)},"watchlist":rows,"metals":metals(),"reports":reports,"candidates":candidates,"jobs":db.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 6"),"notifications":db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 10"),"settings_summary":{"budget":db.settings().budget,"fee_confirmed":db.settings().fees.confirmed,"lan_enabled":db.settings().lan_enabled}}


# 返回包含行情与准入状态的自选证券列表。
@app.get("/api/watchlist")
def watchlist():
    return watch_rows()


# 校验输入并将证券加入共享自选清单。
@app.post("/api/watchlist")
def add_watch(item:instruments.InstrumentInput):
    return instruments.add(item)


# 仅允许更新证券分组与置顶标记，保持身份和规则字段受控。
@app.patch("/api/instruments/{ident}")
def patch_instrument(ident:str,payload:dict):
    instruments.get(ident)
    if not payload or set(payload)-{"group_name","pinned"}:
        raise ValueError("仅允许更新分组和置顶；状态须通过独立核验")
    for k,v in payload.items():
        if k=="group_name" and (not isinstance(v,str) or not v.strip() or len(v)>40):
            raise ValueError("分组名称不合法")
        if k=="pinned" and not isinstance(v,bool):
            raise ValueError("置顶值须为布尔类型")
        db.execute(f"UPDATE instruments SET {k}=? WHERE id=?",(v,ident))
    return instruments.get(ident)


# 取消证券的自选状态，保留已有研究与交易关联。
@app.delete("/api/watchlist/{ident}")
def remove_watch(ident:str):
    db.execute("UPDATE instruments SET watched=0 WHERE id=?",(ident,))
    return {"removed":True}


# 按指定格式导出自选证券的基础身份信息。
@app.get("/api/watchlist/export")
def export_watch(format:str="json"):
    rows=[{k:r[k] for k in ("symbol","market","name","asset_class","group_name")} for r in watch_rows()]
    if format=="csv":
        buf=io.StringIO()
        writer=csv.DictWriter(buf,fieldnames=["symbol","market","name","asset_class","group_name"])
        writer.writeheader()
        writer.writerows(rows)
        return download("\ufeff"+buf.getvalue(),"watchlist.csv","text/csv; charset=utf-8")
    return download(db.dumps(rows),"watchlist.json","application/json")


# 整批校验导入内容后写入，避免非法记录导致部分导入。
@app.post("/api/watchlist/import")
def import_watch(payload:dict):
    raw=payload.get("content","")
    if not isinstance(raw,str) or len(raw)>2*1024*1024:
        raise ValueError("导入内容须为不超过2MB的文本")
    try:
        rows=json.loads(raw) if payload.get("format")=="json" else list(csv.DictReader(io.StringIO(raw.lstrip("\ufeff"))))
        if not isinstance(rows,list) or len(rows)>2000:
            raise ValueError("最多导入2000条证券")
        items=[instruments.InstrumentInput.model_validate(r) for r in rows]
        for item in items:
            if item.market in {"SH","SZ","FUND"} and (not item.symbol.isdigit() or len(item.symbol)!=6):
                raise ValueError("境内代码须为六位数字")
    except (ValueError,TypeError) as exc:
        raise ValueError("导入格式错误："+str(exc)[:400]) from None
    unique={(i.market,i.symbol):i for i in items}
    for item in unique.values():
        instruments.add(item)
    return {"imported":len(unique),"duplicates":len(items)-len(unique)}


# 校验图表周期并返回证券规则、行情、技术指标与相关资料。
@app.get("/api/instruments/{ident}")
def detail(ident:str,timeframe:str="1d"):
    if timeframe not in {"1d","1w","15m","60m"}:
        raise ValueError("不支持的K线周期")
    item=instruments.get(ident)
    bars=collectors.bars_for(ident,"1d" if timeframe=="1w" else timeframe)
    if timeframe=="1w":
        bars=research.resample_weekly(bars)
    observed=db.one("SELECT payload_json FROM market_quotes WHERE instrument_id=?",(ident,))
    return {"instrument":item,"bars":bars,"observation":json.loads(observed["payload_json"]) if observed else None,"indicators":analytics.indicators(bars),"risk_reasons":instruments.risk_reasons(item),"events":db.query("SELECT * FROM risk_events WHERE instrument_id=? ORDER BY verified_at DESC LIMIT 100",(ident,))}


# 更新证券官方核验证据和产品规则。
@app.post("/api/instruments/{ident}/verify")
def verify(ident:str,payload:dict):
    return instruments.verify(ident,payload)


# 校验周期后同步指定证券行情。
@app.post("/api/instruments/{ident}/sync")
def sync_single(ident:str,payload:dict):
    tf=payload.get("timeframe","1d")
    if tf not in {"1d","15m","60m"}:
        raise ValueError("无效周期")
    return collectors.sync_instrument(instruments.get(ident),tf)


# 触发指定证券的公告采集并返回结果。
@app.post("/api/instruments/{ident}/announcements")
def fetch_announcements(ident:str):
    return collectors.announcements(ident)


# 校验批量行情输入后调用原子保存逻辑。
@app.post("/api/instruments/{ident}/bars/import")
def import_bars(ident:str,payload:dict):
    instruments.get(ident)
    bars=payload.get("bars")
    if not isinstance(bars,list) or not bars or len(bars)>10000:
        raise ValueError("须提供1至10000根带复权口径与来源的K线")
    tf=payload.get("timeframe","1d")
    if tf not in {"1d","15m","60m"}:
        raise ValueError("周期不合法")
    collectors.save_bars(ident,bars,tf)
    return {"imported":len(bars)}


# 按证券规则、用户费用与预算计算合法数量及含费成本。
@app.post("/api/budget")
def estimate_budget(payload:dict):
    item=instruments.get(str(payload["instrument_id"]))
    result=budget_check(payload["price"],item["rules"],item["asset_class"],db.settings().fees,db.settings().budget,payload.get("quantity"))
    result["risk_reasons"]=instruments.risk_reasons(item)
    result["eligible"]=result["within_budget"] and not result["risk_reasons"]
    return result


# 返回现货金银及期货观察行情，并标明数据状态与来源。
@app.get("/api/metals")
def metals():
    result=[]
    for ident in ("FX:XAUUSD","FX:XAGUSD","SHFE:AU0","SHFE:AG0"):
        row=db.one("SELECT payload_json FROM market_quotes WHERE instrument_id=?",(ident,))
        if row:
            item=json.loads(row["payload_json"])
            age=(now()-datetime.fromisoformat(item["quote_time"])).total_seconds()/3600
            item["stale"]=age>24
        else:
            item={"id":ident,"price":None,"status":"unavailable","stale":True,"name":instruments.get(ident)["name"],"message":"未取得具有报价时间的行情"}
        result.append(item)
    return result


# 按模式和搜索条件查询报告索引，限制返回条数。
@app.get("/api/reports")
def reports(mode:str="",q:str=""):
    rows=db.query("SELECT * FROM reports WHERE (?='' OR mode=?) AND (report_date LIKE ? OR payload_json LIKE ?) ORDER BY generated_at DESC LIMIT 200",(mode,mode,"%"+q+"%","%"+q+"%"))
    return [{"id":r["id"],"mode":r["mode"],"report_date":r["report_date"],"generated_at":r["generated_at"],"summary":json.loads(r["payload_json"])["summary"],"candidate_count":len(json.loads(r["payload_json"])["candidates"]),"ai_status":json.loads(r["payload_json"])["ai_status"]} for r in rows]


# 读取完整报告事实及对应归档信息。
@app.get("/api/reports/{ident}")
def report_detail(ident:str):
    row=db.one("SELECT * FROM reports WHERE id=?",(ident,))
    if not row:
        raise ValueError("报告不存在")
    return {"id":ident,**json.loads(row["payload_json"])}


# 统一设置下载内容类型与附件文件名。
def download(content,name,media="application/json"):
    return Response(content,media_type=media,headers={"Content-Disposition":f'attachment; filename="{name}"'})


# 校验导出格式并返回报告的 Markdown、HTML 或 JSON。
@app.get("/api/reports/{ident}/export")
def export_report(ident:str,format:str="md"):
    row=db.one("SELECT * FROM reports WHERE id=?",(ident,))
    if not row or format not in {"md","html","json"}:
        raise ValueError("报告或导出格式不存在")
    if format=="json":
        return download(row["payload_json"],ident+".json")
    path=DATA/"reports"/row["markdown_path" if format=="md" else "html_path"]
    return FileResponse(path,media_type="text/markdown" if format=="md" else "text/html",filename=ident+"."+format)


# 汇总任务计划、任务状态与运行日志。
@app.get("/api/jobs")
def job_list():
    return {"schedule":jobs.schedule_info(),"jobs":db.query("SELECT * FROM jobs ORDER BY created_at DESC LIMIT 200"),"logs":db.query("SELECT * FROM job_logs ORDER BY id DESC LIMIT 400")}


# 按请求类型提交后台任务并返回队列状态。
@app.post("/api/jobs")
def run_job(payload:dict):
    return jobs.enqueue(str(payload["kind"]))


# 读取原任务并通过任务队列执行允许的重试。
@app.post("/api/jobs/{ident}/retry")
def retry_job(ident:str):
    row=db.one("SELECT * FROM jobs WHERE id=?",(ident,))
    if not row:
        raise ValueError("任务不存在")
    return jobs.enqueue(row["kind"],row["logical_key"],retry=True)


# 返回虚拟账户、持仓、委托及资金流水总览。
@app.get("/api/simulation")
def portfolio():
    return simulation.portfolio()


# 根据用户明确输入的虚拟本金创建模拟账户。
@app.post("/api/accounts")
def account(payload:dict):
    return simulation.create_account(payload["name"],payload["initial_cash"])


# 提交经过预算、持仓与产品规则校验的模拟委托。
@app.post("/api/orders")
def order(payload:simulation.OrderInput):
    return simulation.order(payload)


# 仅撤销仍在等待成交的委托，其他状态返回明确错误。
@app.post("/api/orders/{ident}/cancel")
def cancel(ident:str):
    changed=db.execute("UPDATE orders SET status='cancelled',reason='用户撤销' WHERE id=? AND status='pending'",(ident,))
    if not changed:
        raise ValueError("只有待成交委托可以撤销")
    return {"cancelled":True}


# 返回历史实验参数、摘要及证据完整性状态。
@app.get("/api/backtests")
def backtest_list():
    rows=db.query("SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT 100")
    return [{"id":r["id"],"created_at":r["created_at"],"params":json.loads(r["params_json"]),"summary":json.loads(r["result_json"])["summary"],"validated":json.loads(r["result_json"])["validated"],"strategy_version":r["strategy_version"],"data_hash":r["data_hash"]} for r in rows]


# 执行历史回测并保存可复现快照。
@app.post("/api/backtests")
def create_backtest(payload:backtest.Parameters):
    return backtest.create(payload)


# 读取完整实验结果及其行情和策略快照信息。
@app.get("/api/backtests/{ident}")
def get_backtest(ident:str):
    row=db.one("SELECT * FROM backtest_runs WHERE id=?",(ident,))
    if not row:
        raise ValueError("回测不存在")
    return {"id":ident,"created_at":row["created_at"],"params":json.loads(row["params_json"]),"data_hash":row["data_hash"],"strategy_version":row["strategy_version"],**json.loads(row["result_json"])}


# 调用已保存快照进行确定性重演。
@app.post("/api/backtests/{ident}/reproduce")
def reproduce(ident:str):
    return backtest.reproduce(ident)


# 将完整实验结果序列化为 JSON 附件。
@app.get("/api/backtests/{ident}/export")
def export_backtest(ident:str):
    return download(db.dumps(get_backtest(ident)),ident+".json")


# 返回当前配置、鉴权准备状态、调度信息和实际监听地址。
@app.get("/api/settings")
def settings():
    return {"settings":db.settings().model_dump(),"lan_auth_configured":security.lan_ready(),"schedule":jobs.schedule_info(),"runtime_host":os.environ.get("STOCKAI_BIND_HOST","127.0.0.1")}


# 校验完整配置及局域网启用前提后保存设置。
@app.put("/api/settings")
def save_settings(payload:dict,request:Request):
    cfg=Settings.model_validate(payload["settings"])
    old=db.settings()
    dangerous=cfg.lan_enabled!=old.lan_enabled or cfg.allowed_networks!=old.allowed_networks or cfg.proxy_hostname!=old.proxy_hostname or cfg.budget!=old.budget or cfg.fees!=old.fees
    if dangerous and payload.get("confirm") is not True:
        raise ValueError("修改安全、预算或费用配置须明确确认")
    if (cfg.lan_enabled!=old.lan_enabled or cfg.allowed_networks!=old.allowed_networks or cfg.proxy_hostname!=old.proxy_hostname) and not security.local_request(request):
        raise ValueError("局域网安全配置只能在本机修改")
    if cfg.lan_enabled and not security.lan_ready():
        raise ValueError("启用局域网前须在本机设置至少12位管理员密码")
    db.save_settings(cfg)
    return {"saved":True,"restart_required":cfg.lan_enabled!=old.lan_enabled}


# 仅允许可信本机请求设置管理员或只读访问密码。
@app.post("/api/security/password")
def set_password(payload:dict,request:Request):
    if not security.local_request(request):
        raise ValueError("访问密码只能在本机设置")
    security.configure_password(str(payload["password"]),str(payload.get("role","admin")))
    return {"saved":True}


# 导出当前经过校验的配置，不包含进程环境中的邮件密码。
@app.get("/api/config/export")
def export_config():
    return download(db.settings().model_dump_json(),"stockai-config.json")


# 返回已保存备份及其统计信息。
@app.get("/api/backups")
def backups():
    return maintenance.list_backups()


# 创建并校验数据库和报告档案备份。
@app.post("/api/backups")
def backup():
    return maintenance.create_backup()


# 解析受控备份路径并返回下载文件。
@app.get("/api/backups/{ident}/download")
def backup_download(ident:str):
    return FileResponse(maintenance.backup_path(ident),filename="stockai-backup-"+ident+".zip")


# 仅接受可信本机请求，并要求匹配的确认文字后恢复备份。
@app.post("/api/backups/{ident}/restore")
def restore(ident:str,payload:dict,request:Request):
    if not security.local_request(request):
        raise ValueError("恢复数据库只能在本机执行")
    return maintenance.restore_backup(ident,payload.get("confirmation"))


# 按时间倒序返回最近通知。
@app.get("/api/notifications")
def notifications():
    return db.query("SELECT * FROM notifications ORDER BY id DESC LIMIT 100")


# 将现有本地通知标记为已读。
@app.post("/api/notifications/read")
def read_notifications():
    db.execute("UPDATE notifications SET read=1")
    return {"read":True}


# 浏览器与桌面客户端从同一 API 来源加载相同的静态构建，保持界面与数据一致。
static=ROOT/"frontend"/"dist"
if static.exists():
    app.mount("/",StaticFiles(directory=static,html=True),name="web")
else:
    # 前端尚未构建时返回安装提示，便于定位缺失的静态资源。
    @app.get("/")
    def setup_page():
        return Response("StockAI 后端已启动。请在 frontend 目录运行 npm install 与 npm run build，然后重启服务。",media_type="text/plain; charset=utf-8")
