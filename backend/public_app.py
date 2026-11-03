# 公网 FastAPI 入口，按登录身份隔离个人研究接口与站点管理员接口。
"""Public multiuser entry point. The original backend.app stays an independent local app."""
from __future__ import annotations

import csv
import hmac
import io
import json
import sqlite3
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends,FastAPI,Request
from fastapi.responses import FileResponse,JSONResponse,Response
from fastapi.exceptions import RequestValidationError
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend import analytics,backtest
from backend.budget import budget_check
from backend.config import ROOT,timestamp
from .cloud import VERSION as PUBLIC_VERSION
from backend.instruments import InstrumentInput
from backend.research import resample_weekly
from backend.simulation import OrderInput
from .cloud import backups,collector,jobs,market,models,simulation,workspace
from .cloud.auth import Actor,AuthError
from .cloud.config import CloudConfig
from .cloud.crypto import digest
from .cloud.lifecycle import erase
from .cloud.preferences import get_preferences,save_preferences
from .cloud.runtime import Runtime
from .cloud.store import json_text,stamp


# 绑定部署配置并创建应用，集中注册身份、个人工作区和管理员路由。
def create_app(cfg=None):
    cfg=cfg or CloudConfig.from_env()
    # 随应用启动和退出创建、恢复并关闭公网运行时资源。
    @asynccontextmanager
    async def lifespan(app):
        runtime=Runtime(cfg).start();app.state.runtime=runtime
        try:
            yield
        finally:
            runtime.close()
    app=FastAPI(title="StockAI 公网个人研究空间",version=PUBLIC_VERSION,lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    origins={cfg.public_url,*cfg.desktop_origins} if cfg.desktop_mode else {cfg.public_url}
    hosts=[urlsplit(origin).hostname for origin in origins]+(["127.0.0.1","localhost","testserver"] if cfg.dev else [])
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)

    # 从应用状态取得当前运行时，供路由与依赖共享。
    def rt(request:Request):
        return request.app.state.runtime
    # 从会话 Cookie 解析已认证身份。
    def actor(request:Request)->Actor:
        return rt(request).auth.actor(request.cookies.get(cfg.session_cookie))
    # 要求账号有效且已完成邮箱验证后进入个人研究接口。
    def user(a:Actor=Depends(actor)):
        if not a.verified_at or a.status!="active":
            raise AuthError("VERIFICATION_REQUIRED","请先完成邮箱验证",403)
        return a
    # 在用户身份验证基础上要求站点管理员角色。
    def admin(a:Actor=Depends(user)):
        if a.role!="admin":
            raise AuthError("ADMIN_REQUIRED","此操作需要站点管理员",403)
        return a
    # 根据部署模式设置会话与 CSRF Cookie 的安全属性。
    def cookie(response,name,value,httponly):
        response.set_cookie(name,value,max_age=8*3600,secure=not cfg.dev,httponly=httponly,samesite="lax",path="/")
    # 构造带正确内容类型和附件文件名的导出响应。
    def export(value,name,media="application/json"):
        return Response(value,media_type=media,headers={"Content-Disposition":f'attachment; filename="{name}"'})

    # 将身份与权限错误转换为稳定错误码及对应 HTTP 状态。
    @app.exception_handler(AuthError)
    async def auth_error(request,exc):
        return JSONResponse({"detail":exc.message,"code":exc.code},status_code=exc.status)
    # 将模型故障转换为脱敏的展示消息及错误码。
    @app.exception_handler(models.ModelError)
    async def model_error(request,exc):
        return JSONResponse({"detail":models.MESSAGES.get(exc.code,"模型操作失败"),"code":exc.code},status_code=400)
    # 将业务参数异常转换为客户端可理解的校验错误。
    @app.exception_handler(ValueError)
    async def input_error(request,exc):
        # 外部传输或密文异常可能包含私密信息，响应只返回受控错误码和提示。
        return JSONResponse({"detail":str(exc)[:400] if type(exc) is ValueError else "输入不符合字段要求","code":"INVALID_INPUT"},status_code=400)
    # 将数据库唯一性或关联约束失败转换为冲突响应。
    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request,exc):
        return JSONResponse({"detail":"记录状态已改变，请刷新后重试","code":"CONFLICT"},status_code=409)
    # 将缺少必要键的请求转换为参数错误响应。
    @app.exception_handler(KeyError)
    async def missing_field(request,exc):
        return JSONResponse({"detail":"缺少必要输入字段","code":"INVALID_INPUT"},status_code=400)
    # 清理校验异常中的原始输入，避免密码或凭据在错误响应中回显。
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        return JSONResponse({"detail":[{"loc":e["loc"],"msg":e["msg"]} for e in exc.errors()],"code":"INVALID_INPUT"},status_code=422)

    # 统一校验主机、来源、维护状态及写请求的 CSRF 绑定。
    @app.middleware("http")
    async def guard(request,call_next):
        if request.url.path.startswith('/api/'):
            runtime=rt(request)
            if runtime.paused() and not request.url.path.startswith(('/api/admin/upgrades','/api/health','/api/auth/')):
                return JSONResponse({'code':'MAINTENANCE','detail':'版本维护中，请稍后重试；草稿和资料已保留'},503)
            client_version=request.headers.get('x-stockai-client-version')
            if client_version and client_version!=PUBLIC_VERSION and request.method not in {'GET','HEAD','OPTIONS'}:
                return JSONResponse({'code':'CLIENT_VERSION','detail':'站点版本已更新，请保存草稿并刷新页面'},409)
        if request.url.path.startswith("/api/") and request.method not in {"GET","HEAD","OPTIONS"}:
            origin=request.headers.get("origin","").rstrip("/")
            request_origin=f'{request.url.scheme}://{request.headers.get("host","")}'
            if origin not in origins or (cfg.desktop_mode and origin!=request_origin):
                return JSONResponse({"detail":"请求来源验证失败","code":"ORIGIN_REJECTED"},403)
            csrf=request.cookies.get(cfg.csrf_cookie,"");header=request.headers.get("x-csrf-token","")
            if not csrf or not hmac.compare_digest(csrf,header):
                return JSONResponse({"detail":"请刷新页面后重试","code":"CSRF_REJECTED"},403)
            session=request.cookies.get(cfg.session_cookie)
            if session:
                s=rt(request).store.one("SELECT csrf_hash FROM sessions WHERE token_hash=? AND revoked_at IS NULL AND expires_at>?",(digest(session),stamp()))
                if not s or not hmac.compare_digest(s["csrf_hash"],digest(csrf)):
                    return JSONResponse({"detail":"登录状态已改变，请刷新页面","code":"CSRF_REJECTED"},403)
            if int(request.headers.get("content-length","0"))>10*1024*1024:
                return JSONResponse({"detail":"请求内容过大"},413)
        response=await call_next(request)
        response.headers["X-Content-Type-Options"]="nosniff"
        response.headers["Referrer-Policy"]="no-referrer"
        response.headers["X-Frame-Options"]="DENY"
        response.headers["Content-Security-Policy"]="default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"]="no-store"
        return response

    # 返回公网服务健康状态和运行版本。
    @app.get("/api/health")
    def health(request:Request):
        result={"status":"ok","edition":"public","version":PUBLIC_VERSION,"sqlite":sqlite3.sqlite_version,"development":cfg.dev}
        if cfg.desktop_mode:
            result.update(edition="desktop-local",desktop=True,instance=cfg.desktop_instance,initialized=bool(rt(request).store.one("SELECT id FROM users LIMIT 1")),csrf_cookie=cfg.csrf_cookie)
        return result

    if cfg.desktop_mode:
        @app.post("/api/desktop/setup")
        def desktop_setup(payload:dict,request:Request):
            # 仅首次在本机创建账号；仍受同源、CSRF 和事务竞争保护。
            import ipaddress
            if not ipaddress.ip_address(request.client.host).is_loopback or any(request.headers.get(h) for h in ("forwarded","x-forwarded-for","x-real-ip")):
                raise AuthError("LOCAL_ONLY","仅允许本机初始化",403)
            runtime=rt(request)
            with runtime.store.tx(immediate=True) as c:
                if c.execute("SELECT id FROM users LIMIT 1").fetchone():raise AuthError("ALREADY_INITIALIZED","本机研究空间已创建，请登录",409)
                runtime.auth.new_user(payload.get("email",""),payload.get("password",""),role="admin",verified=True,c=c)
            token,csrf,a=runtime.auth.login(payload.get("email",""),payload.get("password",""),request.client.host)
            response=JSONResponse({"user":a.public()});cookie(response,cfg.session_cookie,token,True);cookie(response,cfg.csrf_cookie,csrf,False)
            return response

    # 签发或更新当前会话绑定的 CSRF 令牌。
    @app.get("/api/auth/csrf")
    def csrf(request:Request):
        session=request.cookies.get(cfg.session_cookie)
        try:
            if session:
                rt(request).auth.actor(session)
        except AuthError:
            session=None
        value=rt(request).auth.csrf(session)
        response=JSONResponse({"csrf_token":value});cookie(response,cfg.csrf_cookie,value,False)
        if not session:
            response.delete_cookie(cfg.session_cookie,path="/",secure=not cfg.dev,httponly=True,samesite="lax")
        return response

    # 通过一次性邀请创建用户，并安排邮箱验证邮件。
    @app.post("/api/auth/register")
    def register(payload:dict,request:Request):
        return rt(request).auth.register(payload.get("email",""),payload.get("password",""),payload.get("invitation",""),request.client.host)
    # 验证邮箱与密码，设置会话 Cookie 并返回公开身份。
    @app.post("/api/auth/login")
    def login(payload:dict,request:Request):
        token,csrf,a=rt(request).auth.login(payload.get("email",""),payload.get("password",""),request.client.host)
        response=JSONResponse({"user":a.public()});cookie(response,cfg.session_cookie,token,True);cookie(response,cfg.csrf_cookie,csrf,False)
        return response
    # 返回当前登录用户允许公开的账号信息。
    @app.get("/api/auth/me")
    def me(a:Actor=Depends(actor)):
        return a.public()
    # 撤销当前会话并清除浏览器中的会话及 CSRF Cookie。
    @app.post("/api/auth/logout")
    def logout(request:Request,a:Actor=Depends(actor)):
        rt(request).store.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND user_id=?",(stamp(),a.session_hash,a.id))
        response=JSONResponse({"success":True});response.delete_cookie(cfg.session_cookie,path="/",secure=not cfg.dev,httponly=True,samesite="lax")
        return response
    # 为当前账号重新发送受限频率的邮箱验证邮件。
    @app.post("/api/auth/resend-verification")
    def resend(payload:dict,request:Request):
        return rt(request).auth.resend(payload.get("email",""),request.client.host)
    # 接受密码重置请求，使用统一提示避免暴露邮箱是否存在。
    @app.post("/api/auth/request-reset")
    def request_reset(payload:dict,request:Request):
        return rt(request).auth.resend(payload.get("email",""),request.client.host,"reset")
    # 消费有效的一次性验证令牌并更新邮箱验证状态。
    @app.post("/api/auth/verify-email")
    def verify_email(payload:dict,request:Request):
        return rt(request).auth.consume(payload.get("token",""),"verify")
    # 消费重置令牌，更新密码并使旧会话失效。
    @app.post("/api/auth/reset-password")
    def reset_password(payload:dict,request:Request):
        return rt(request).auth.consume(payload.get("token",""),"reset",payload.get("password",""))

    # 返回个人研究界面所需的登录及角色状态。
    @app.get("/api/me/session")
    def session(a:Actor=Depends(user)):
        return {"requires_login":False,"role":a.role,"user":a.public()}
    # 读取当前用户独立的研究偏好。
    @app.get("/api/me/preferences")
    def preferences(request:Request,a:Actor=Depends(user)):
        return get_preferences(rt(request).store,a.id).model_dump()
    # 校验并保存当前用户的预算、费用和自动任务设置。
    @app.put("/api/me/preferences")
    def update_preferences(payload:dict,request:Request,a:Actor=Depends(user)):
        return save_preferences(rt(request).store,a.id,payload).model_dump()
    # 将个人偏好转换为核心研究界面使用的配置结构。
    @app.get("/api/me/settings")
    def core_settings(request:Request,a:Actor=Depends(user)):
        p=get_preferences(rt(request).store,a.id)
        return {"settings":{**p.model_dump(),"timezone":"Asia/Shanghai","scheduler_enabled":any((p.morning_enabled,p.closing_enabled,p.weekly_enabled))},"schedule":{"plans":[]},"runtime_host":"public"}
    # 导出本人的偏好配置，便于保存和迁移。
    @app.get("/api/me/config/export")
    def export_preferences(request:Request,a:Actor=Depends(user)):
        return export(get_preferences(rt(request).store,a.id).model_dump_json(),"StockAI-preferences.json")
    # 返回站点支持的模型服务预设。
    @app.get("/api/me/model-presets")
    def presets(a:Actor=Depends(user)):
        return models.PRESETS
    # 只返回本人模型配置和脱敏凭据状态。
    @app.get("/api/me/model-profiles")
    def profiles(request:Request,a:Actor=Depends(user)):
        return models.profile_rows(rt(request).store,a.id)
    # 校验输入并为当前用户保存首个模型配置版本。
    @app.post("/api/me/model-profiles")
    def create_profile(payload:models.ProfileInput,request:Request,a:Actor=Depends(user)):
        r=rt(request);return models.save_profile(r.store,r.keys,a.id,payload)
    # 仅更新本人的模型配置，并追加版本或替换加密凭据。
    @app.put("/api/me/model-profiles/{ident}")
    def update_profile(ident:str,payload:models.ProfileInput,request:Request,a:Actor=Depends(user)):
        r=rt(request);return models.save_profile(r.store,r.keys,a.id,payload,ident)
    # 删除本人的模型配置及可用凭据。
    @app.delete("/api/me/model-profiles/{ident}")
    def delete_profile(ident:str,request:Request,a:Actor=Depends(user)):
        models.remove_profile(rt(request).store,a.id,ident);return {"deleted":True}
    # 校验所有权并设置或清空本人的默认模型配置。
    @app.post("/api/me/model-profiles/default")
    def default_profile(payload:dict,request:Request,a:Actor=Depends(user)):
        models.set_default(rt(request).store,a.id,payload.get("id"));return {"saved":True}
    # 对本人指定配置提交结构化连接测试任务。
    @app.post("/api/me/model-profiles/{ident}/test")
    def test_profile(ident:str,request:Request,a:Actor=Depends(user)):
        return jobs.enqueue(rt(request).store,a.id,"profile_test",profile_id=ident)
    # 根据本人默认配置和站点模型开关返回可用性提示。
    @app.get("/api/me/model/health")
    def model_health(request:Request,a:Actor=Depends(user)):
        fixed=models.snapshot(rt(request).store,a.id)
        return {"available":False,"configured":fixed is not None,"message":"已选择个人默认配置；连接结果请查看模型配置中的测试任务" if fixed else "尚未选择个人默认模型；可以先生成规则报告"}

    # 返回本人自选证券与共享行情、风险和预算状态。
    @app.get("/api/me/watchlist")
    def watchlist(request:Request,a:Actor=Depends(user)):
        return market.watch_rows(rt(request).store,a.id)
    # 把经过身份校验的证券加入本人自选清单。
    @app.post("/api/me/watchlist")
    def add_watch(payload:InstrumentInput,request:Request,a:Actor=Depends(user)):
        return market.add_watch(rt(request).store,a.id,payload)
    # 仅修改本人自选记录的分组与置顶状态。
    @app.patch("/api/me/instruments/{ident}")
    def patch_watch(ident:str,payload:dict,request:Request,a:Actor=Depends(user)):
        return market.patch_watch(rt(request).store,a.id,ident,payload)
    # 只删除本人自选关联，保留共享证券和既有研究档案。
    @app.delete("/api/me/watchlist/{ident}")
    def remove_watch(ident:str,request:Request,a:Actor=Depends(user)):
        store=rt(request).store
        with store.user_lock(a.id),store.tx(immediate=True) as c:
            if not c.execute('DELETE FROM user_watchlist WHERE user_id=? AND instrument_id=?',(a.id,ident)).rowcount:raise AuthError('NOT_FOUND','记录不存在',404)
            disabled=0
            for rule in c.execute("SELECT * FROM research_objects WHERE user_id=? AND kind='alert' AND status='active'",(a.id,)).fetchall():
                data=json.loads(rule['payload_json'])
                if data['instrument_id']!=ident:continue
                data['enabled']=False;version=rule['version']+1;ts=stamp()
                c.execute("UPDATE research_objects SET status='disabled',version=?,updated_at=?,payload_json=? WHERE id=? AND user_id=?",(version,ts,json_text(data),rule['id'],a.id))
                c.execute('INSERT INTO research_versions VALUES(?,?,?,?,?)',(rule['id'],a.id,version,ts,json_text(data)))
                c.execute("UPDATE alert_events SET state='cancelled' WHERE rule_id=? AND user_id=? AND state IN('pending','pending_dnd')",(rule['id'],a.id));disabled+=1
        return {'removed':True,'disabled_alerts':disabled}
    # 导出本人的自选基础资料。
    @app.get("/api/me/watchlist/export")
    def export_watchlist(request:Request,format:str="json",a:Actor=Depends(user)):
        rows=rt(request).store.query("SELECT i.symbol,i.market,i.asset_class,w.group_name,w.pinned FROM user_watchlist w JOIN instruments i ON i.id=w.instrument_id WHERE w.user_id=?",(a.id,))
        if format=="json":
            return export(json_text({"instruments":rows}),"StockAI-watchlist.json")
        if format!="csv":
            raise ValueError("导出格式不支持")
        buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=["symbol","market","asset_class","group_name","pinned"]);writer.writeheader();writer.writerows(rows)
        return export("\ufeff"+buffer.getvalue(),"StockAI-watchlist.csv","text/csv")
    # 整批校验并原子导入本人自选，避免部分写入。
    @app.post("/api/me/watchlist/import")
    def import_watchlist(payload:dict,request:Request,a:Actor=Depends(user)):
        data=payload.get("content",payload.get("data",payload.get("instruments",[])))
        if isinstance(data,str):
            data=list(csv.DictReader(io.StringIO(data.lstrip("\ufeff")))) if payload.get("format")=="csv" else json.loads(data)
        if isinstance(data,dict):
            data=data.get("instruments",[])
        if not isinstance(data,list) or not 1<=len(data)<=300:
            raise ValueError("须导入1至300只证券")
        if any(not isinstance(row,dict) for row in data):
            raise ValueError("每项证券必须为对象")
        return market.import_watch(rt(request).store,a.id,data)
    # 读取指定证券的共享资料及本人可访问的相关研究数据。
    @app.get("/api/me/instruments/{ident}")
    def detail(ident:str,request:Request,timeframe:str="1d",a:Actor=Depends(user)):
        store=rt(request).store;data=market.detail(store,a.id,ident,"1d" if timeframe=="1w" else timeframe)
        bars=resample_weekly(data["bars"]) if timeframe=="1w" else data["bars"]
        return {"instrument":{k:v for k,v in data.items() if k not in {"bars","indicators","quote","observation","budget","exclusion_reasons"}},"bars":bars,"indicators":analytics.indicators(bars),"observation":data["observation"],"risk_reasons":market.risk_reasons(data,get_preferences(store,a.id)),"events":store.query("SELECT id,event_type,severity,title,source_url,verified_at FROM risk_events WHERE instrument_id=? ORDER BY verified_at DESC LIMIT 100",(ident,))}
    # 结合产品规则与本人费用设置估算含费预算和合法数量。
    @app.post("/api/me/budget")
    def estimate_budget(payload:dict,request:Request,a:Actor=Depends(user)):
        store=rt(request).store;p=get_preferences(store,a.id);i=market.instrument(store,str(payload.get("instrument_id")))
        value=budget_check(payload["price"],i["rules"],i["asset_class"],p.fees,p.budget,payload.get("quantity"));value["risk_reasons"]=market.risk_reasons(i,p);value["eligible"]=value["within_budget"] and not value["risk_reasons"]
        return value
    # 返回共享贵金属观察报价及其时间与来源。
    @app.get("/api/me/metals")
    def metals(request:Request,a:Actor=Depends(user)):
        byid={q["id"]:q for q in market.quotes(rt(request).store)}
        return [byid.get(i,{"id":i,"price":None,"status":"unavailable","stale":True,"message":"没有已确认行情"}) for i in ("FX:XAUUSD","FX:XAGUSD","SHFE:AU0","SHFE:AG0")]
    # 组合本人自选、报告、任务及模拟概况，生成个人工作台总览。
    @app.get("/api/me/dashboard")
    def dashboard(request:Request,a:Actor=Depends(user)):
        store=rt(request).store;rows=market.watch_rows(store,a.id);reports=workspace.report_list(store,a.id)[:5]
        latest=workspace.report_detail(store,a.id,reports[0]["id"])["payload"] if reports else None;candidates=latest["candidates"] if latest else []
        indices=[market.detail(store,a.id,r["id"]) for r in store.query("SELECT id FROM instruments WHERE asset_class='index' ORDER BY id")]
        return {"version":PUBLIC_VERSION,"time":timestamp(),"calendar":market.calendar(store),"stats":{"watch_count":len(rows),"quote_count":sum(r["quote"] is not None for r in rows),"verified_count":sum(not market.risk_reasons(r,get_preferences(store,a.id)) for r in rows),"candidate_count":len(candidates)},"watchlist":rows,"indices":indices,"metals":metals(request,a),"reports":reports,"candidates":candidates,"jobs":jobs.list_jobs(store,a.id)["jobs"][:6],"notifications":store.query("SELECT * FROM personal_notifications WHERE user_id=? ORDER BY id DESC LIMIT 10",(a.id,)),"settings_summary":{"budget":get_preferences(store,a.id).budget,"fee_confirmed":get_preferences(store,a.id).fees.confirmed}}

    # 按查询条件列出本人的研究报告。
    @app.get("/api/me/reports")
    def reports(request:Request,mode:str="",q:str="",a:Actor=Depends(user)):
        return workspace.report_list(rt(request).store,a.id,mode,q)
    # 核对报告所有权后返回完整归档事实。
    @app.get("/api/me/reports/{ident}")
    def report_detail(ident:str,request:Request,a:Actor=Depends(user)):
        row=workspace.report_detail(rt(request).store,a.id,ident)
        return {**row["payload"],**{k:v for k,v in row.items() if k!="payload"}}
    # 对本人的既有事实快照提交 AI 补充解释，保留原报告版本。
    @app.post("/api/me/reports/{ident}/supplement")
    def supplement(ident:str,request:Request,a:Actor=Depends(user)):
        return jobs.enqueue(rt(request).store,a.id,"supplement",parent_id=ident)
    # 核对报告归属与格式后导出归档文件或结构化事实。
    @app.get("/api/me/reports/{ident}/export")
    def export_report(ident:str,request:Request,format:str="md",a:Actor=Depends(user)):
        store=rt(request).store;row=workspace.owned(store,"personal_reports",a.id,ident)
        if format=="json":
            return export(row["payload_json"],ident+".json")
        if format not in {"md","html"}:
            raise ValueError("不支持的格式")
        path=backups.safe_file(store.cfg.data,Path(row["markdown_path" if format=="md" else "html_path"]).as_posix())
        return FileResponse(path,filename=ident+"."+format,media_type="text/markdown" if format=="md" else "text/html")
    # 返回本人任务、调度设置与运行日志。
    @app.get("/api/me/jobs")
    def job_list(request:Request,a:Actor=Depends(user)):
        p=get_preferences(rt(request).store,a.id)
        plans=[{"kind":kind,"label":name,"time":getattr(p,time),"enabled":getattr(p,enabled),"timezone":"Asia/Shanghai","rule":rule+(" · 已开启" if getattr(p,enabled) else " · 未开启")} for kind,name,time,enabled,rule in (("standard","标准版早报","morning_time","morning_enabled","每天，仅补跑当天"),("closing","交易日收盘跟踪","closing_time","closing_enabled","仅已确认交易日"),("full","周日深度复盘","weekly_time","weekly_enabled","每周日，仅补跑当天"))]
        store=rt(request).store
        return {**jobs.list_jobs(store,a.id),"shared_jobs":collector.list_jobs(store) if a.role=='admin' else [],"schedule":{"plans":plans,"enabled":any(x["enabled"] for x in plans)}}
    # 按个人设置与模型快照提交研究任务。
    @app.post("/api/me/jobs")
    def run_job(payload:dict,request:Request,a:Actor=Depends(user)):
        kind=payload.get("kind","standard")
        if kind=="match":
            return {"status":"success","fills":simulation.match(rt(request).store,a.id)}
        if kind in {"sync","metals","context"}:
            if a.role!="admin":
                raise AuthError("ADMIN_REQUIRED","共享行情由站点管理员统一维护",403)
            return collector.enqueue(rt(request).store,kind)
        return jobs.enqueue(rt(request).store,a.id,kind,ai=payload.get("ai",True))
    # 核对任务归属和状态后执行允许的重新提交。
    @app.post("/api/me/jobs/{ident}/retry")
    def retry_job(ident:str,request:Request,a:Actor=Depends(user)):
        return jobs.retry(rt(request).store,a.id,ident)
    # 读取本人虚拟账户、持仓、委托及资金流水。
    @app.get("/api/me/simulation")
    def portfolio(request:Request,a:Actor=Depends(user)):
        return simulation.portfolio(rt(request).store,a.id)
    # 按明确的虚拟本金为本人创建模拟账户。
    @app.post("/api/me/accounts")
    def create_account(payload:dict,request:Request,a:Actor=Depends(user)):
        return simulation.create_account(rt(request).store,a.id,payload.get("name",""),payload.get("initial_cash",0))
    # 仅返回归属本人的模拟账户及相关估值数据。
    @app.get("/api/me/accounts/{ident}")
    def account_detail(ident:str,request:Request,a:Actor=Depends(user)):
        return workspace.owned(rt(request).store,"sim_accounts",a.id,ident)
    # 在本人账户中校验预算和产品规则后创建幂等委托。
    @app.post("/api/me/orders")
    def create_order(payload:OrderInput,request:Request,a:Actor=Depends(user)):
        return simulation.order(rt(request).store,a.id,payload)
    # 核对委托归属后返回完整模拟委托记录。
    @app.get("/api/me/orders/{ident}")
    def order_detail(ident:str,request:Request,a:Actor=Depends(user)):
        return workspace.owned(rt(request).store,"sim_orders",a.id,ident)
    # 仅撤销本人仍待成交的模拟委托。
    @app.post("/api/me/orders/{ident}/cancel")
    def cancel_order(ident:str,request:Request,a:Actor=Depends(user)):
        return simulation.cancel(rt(request).store,a.id,ident)
    # 列出本人的历史回测实验及结果摘要。
    @app.get("/api/me/backtests")
    def backtests(request:Request,a:Actor=Depends(user)):
        rows=rt(request).store.query("SELECT * FROM personal_backtests WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(a.id,))
        return [{"id":r["id"],"created_at":r["created_at"],"params":json.loads(r["params_json"]),"summary":json.loads(r["result_json"])["summary"],"validated":json.loads(r["result_json"])["validated"],"strategy_version":r["strategy_version"],"data_hash":r["data_hash"]} for r in rows]
    # 运行个人回测并保存可复现的行情和规则快照。
    @app.post("/api/me/backtests")
    def create_backtest(payload:backtest.Parameters,request:Request,a:Actor=Depends(user)):
        from .cloud import experiments,resources
        store=rt(request).store;topic=store.one("SELECT id FROM research_objects WHERE user_id=? AND kind='topic' AND client_key='legacy-backtest-entry'",(a.id,))
        if not topic:topic=resources.save(store,a.id,'topic',{'name':'历史撮合研究','question':'从原回测入口提交的独立实验'},client_key='legacy-backtest-entry')
        return experiments.enqueue(store,a.id,experiments.ExperimentInput(client_key=uuid.uuid4().hex,topic_id=topic['id'],parameters=payload))
    # 核对实验归属后返回完整结果。
    @app.get("/api/me/backtests/{ident}")
    def backtest_detail(ident:str,request:Request,a:Actor=Depends(user)):
        r=workspace.owned(rt(request).store,"personal_backtests",a.id,ident)
        return {"id":r["id"],"created_at":r["created_at"],"params":json.loads(r["params_json"]),"strategy_version":r["strategy_version"],"data_hash":r["data_hash"],**json.loads(r["result_json"])}
    # 仅使用本人的归档快照重演实验。
    @app.post("/api/me/backtests/{ident}/reproduce")
    def reproduce(ident:str,request:Request,a:Actor=Depends(user)):
        return workspace.reproduce(rt(request).store,a.id,ident)
    # 将本人回测的完整结果作为 JSON 导出。
    @app.get("/api/me/backtests/{ident}/export")
    def export_backtest(ident:str,request:Request,a:Actor=Depends(user)):
        return export(json_text(backtest_detail(ident,request,a)),ident+".json")
    # 读取本人的最近通知及未读状态。
    @app.get("/api/me/notifications")
    def notifications(request:Request,a:Actor=Depends(user)):
        return rt(request).store.query("SELECT * FROM personal_notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",(a.id,))
    # 只将当前用户的通知标记为已读。
    @app.post("/api/me/notifications/read")
    def read_notifications(request:Request,a:Actor=Depends(user)):
        rt(request).store.execute("UPDATE personal_notifications SET read=1 WHERE user_id=?",(a.id,));return {"success":True}
    # 重新验证密码与确认文字后，通过独立删除账本清除本人账号数据。
    @app.post("/api/me/erase")
    def erase_account(payload:dict,request:Request,a:Actor=Depends(actor)):
        r=rt(request);r.auth.limit("erase",a.id,5,3600)
        value=erase(r.store,r.journal,a,payload.get("password",""),payload.get("confirmation",""))
        response=JSONResponse(value);response.delete_cookie(cfg.session_cookie,path="/",secure=not cfg.dev,httponly=True,samesite="lax")
        return response

    # 仅向管理员返回用户列表与账号状态。
    @app.get("/api/admin/users")
    def users(request:Request,a:Actor=Depends(admin)):
        return rt(request).store.query("SELECT id,email,role,status,verified_at,created_at FROM users ORDER BY created_at")
    # 汇总站点共享数据、队列与维护状态供管理员查看。
    @app.get("/api/admin/status")
    def admin_status(request:Request,a:Actor=Depends(admin)):
        store=rt(request).store
        return {"smtp_configured":bool(cfg.smtp_host and cfg.smtp_from),"backup_configured":cfg.age_recipient.startswith("age1"),"ollama_enabled":cfg.ollama_enabled,"sqlite":sqlite3.sqlite_version,"mail_pending":store.one("SELECT COUNT(*) n FROM mail_outbox WHERE state IN('queued','retry','sending')")["n"],"mail_failed":store.one("SELECT COUNT(*) n FROM mail_outbox WHERE state='failed'")["n"]}
    # 允许管理员停用或恢复目标账号，同时处理相关会话和任务。
    @app.post("/api/admin/users/{ident}/status")
    def user_status(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        rt(request).auth.disable(a,ident,bool(payload.get("disabled",True)));return {"saved":True}
    # 向管理员列出邀请码状态与有效期。
    @app.get("/api/admin/invitations")
    def invitations(request:Request,a:Actor=Depends(admin)):
        return rt(request).store.query("SELECT id,created_at,expires_at,revoked_at,used_at FROM invitations ORDER BY created_at DESC LIMIT 100")
    # 为管理员生成新的一次性邀请。
    @app.post("/api/admin/invitations")
    def invitation(request:Request,a:Actor=Depends(admin)):
        return rt(request).auth.invitation(a.id)
    # 撤销尚未使用的邀请，阻止后续注册。
    @app.delete("/api/admin/invitations/{ident}")
    def revoke_invitation(ident:str,request:Request,a:Actor=Depends(admin)):
        rt(request).store.execute("UPDATE invitations SET revoked_at=? WHERE id=? AND used_at IS NULL",(stamp(),ident));rt(request).store.audit(a.id,"revoke_invitation",ident)
        return {"revoked":True}
    # 向管理员列出共享证券、行情与核验证据状态。
    @app.get("/api/admin/market")
    def shared_market(request:Request,a:Actor=Depends(admin)):
        return [market.instrument(rt(request).store,r["id"]) for r in rt(request).store.query("SELECT id FROM instruments ORDER BY market,symbol")]
    # 仅允许管理员更新共享证券的官方核验证据。
    @app.post("/api/admin/instruments/{ident}/verify")
    def verify_instrument(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        value=market.verify(rt(request).store,ident,payload);rt(request).store.audit(a.id,"verify_instrument",ident);return value
    # 仅允许管理员提交指定证券的共享行情采集任务。
    @app.post("/api/admin/instruments/{ident}/sync")
    def sync_instrument(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        market.instrument(rt(request).store,ident);tf=payload.get("timeframe","1d")
        if tf not in {"1d","15m","60m"}:
            raise ValueError("无效周期")
        return collector.enqueue(rt(request).store,"instrument",ident,tf)
    # 仅允许管理员触发共享公告采集。
    @app.post("/api/admin/instruments/{ident}/announcements")
    def announcements(ident:str,request:Request,a:Actor=Depends(admin)):
        market.instrument(rt(request).store,ident);return collector.enqueue(rt(request).store,"announcements",ident)
    # 仅允许管理员校验并导入共享历史行情。
    @app.post("/api/admin/instruments/{ident}/bars/import")
    def import_bars(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        return market.import_bars(rt(request).store,ident,payload)
    # 列出站点共享采集与维护任务。
    @app.get("/api/admin/jobs")
    def site_jobs(request:Request,a:Actor=Depends(admin)):
        return rt(request).store.query("SELECT * FROM site_jobs ORDER BY created_at DESC LIMIT 100")
    # 校验任务类型并向站点队列提交管理员任务。
    @app.post("/api/admin/jobs")
    def site_job(payload:dict,request:Request,a:Actor=Depends(admin)):
        return collector.enqueue(rt(request).store,payload.get("kind","sync"))
    @app.post("/api/admin/jobs/{ident}/retry")
    def retry_site_job(ident:str,request:Request,a:Actor=Depends(admin)):
        return collector.retry(rt(request).store,ident)
    # 向管理员返回已登记加密备份及可用性。
    @app.get("/api/admin/backups")
    def list_backups(request:Request,a:Actor=Depends(admin)):
        r=rt(request);return backups.list_backups(r.store,r.journal)
    # 由管理员触发加密备份创建。
    @app.post("/api/admin/backups")
    def backup(request:Request,a:Actor=Depends(admin)):
        ident=uuid.uuid4().hex
        rt(request).store.execute("INSERT INTO site_jobs(id,logical_key,kind,status,created_at) VALUES(?,?,'backup','queued',?)",(ident,"backup:"+ident,stamp()))
        rt(request).store.audit(a.id,"backup_create",ident);return {"id":ident,"status":"queued"}
    # 为指定备份重新验证管理员密码，生成单次下载授权。
    @app.post("/api/admin/backups/{ident}/reauth")
    def reauth_backup(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        r=rt(request);backups.registered(r.store,r.journal,ident)
        return {"grant":r.auth.reauth(a,payload.get("password",""),ident)}
    # 消费与会话和备份绑定的二次授权后下载加密文件。
    @app.post("/api/admin/backups/{ident}/download")
    def export_backup(ident:str,request:Request,a:Actor=Depends(admin)):
        r=rt(request);r.auth.consume_grant(a,request.headers.get("x-reauth-grant",""),ident);path,_=backups.registered(r.store,r.journal,ident)
        r.store.audit(a.id,"backup_export",ident);return FileResponse(path,filename="StockAI-"+ident+".age",media_type="application/octet-stream")

    from .cloud.research_api import install
    install(app,rt,user,admin)
    from .cloud.system_api import install as install_system
    install_system(app,rt,user,admin)
    static=ROOT/"frontend"/"dist-public"
    if static.is_dir():
        app.mount("/assets",StaticFiles(directory=static/"assets"),name="assets")
        # 返回应用图标静态资源。
        @app.get("/favicon.svg")
        def favicon():
            return FileResponse(static/"favicon.svg")
        # 返回公网前端入口，在未构建时提供明确的部署提示。
        @app.get("/")
        def index():
            return FileResponse(static/"index.html")
    return app


app=create_app()
