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
from backend.config import ROOT,VERSION,timestamp
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


def create_app(cfg=None):
    cfg=cfg or CloudConfig.from_env()
    @asynccontextmanager
    async def lifespan(app):
        runtime=Runtime(cfg).start();app.state.runtime=runtime
        try:
            yield
        finally:
            runtime.close()
    app=FastAPI(title="StockAI 公网个人研究空间",version="3.0.0",lifespan=lifespan,docs_url=None,redoc_url=None,openapi_url=None)
    hosts=[urlsplit(cfg.public_url).hostname]+(["127.0.0.1","localhost","testserver"] if cfg.dev else [])
    app.add_middleware(TrustedHostMiddleware,allowed_hosts=hosts)

    def rt(request:Request):
        return request.app.state.runtime
    def actor(request:Request)->Actor:
        return rt(request).auth.actor(request.cookies.get(cfg.session_cookie))
    def user(a:Actor=Depends(actor)):
        if not a.verified_at or a.status!="active":
            raise AuthError("VERIFICATION_REQUIRED","请先完成邮箱验证",403)
        return a
    def admin(a:Actor=Depends(user)):
        if a.role!="admin":
            raise AuthError("ADMIN_REQUIRED","此操作需要站点管理员",403)
        return a
    def cookie(response,name,value,httponly):
        response.set_cookie(name,value,max_age=8*3600,secure=not cfg.dev,httponly=httponly,samesite="lax",path="/")
    def export(value,name,media="application/json"):
        return Response(value,media_type=media,headers={"Content-Disposition":f'attachment; filename="{name}"'})

    @app.exception_handler(AuthError)
    async def auth_error(request,exc):
        return JSONResponse({"detail":exc.message,"code":exc.code},status_code=exc.status)
    @app.exception_handler(models.ModelError)
    async def model_error(request,exc):
        return JSONResponse({"detail":models.MESSAGES.get(exc.code,"模型操作失败"),"code":exc.code},status_code=400)
    @app.exception_handler(ValueError)
    async def input_error(request,exc):
        # Never echo arbitrary exception strings from external transports or ciphertext.
        return JSONResponse({"detail":str(exc)[:400] if type(exc) is ValueError else "输入不符合字段要求","code":"INVALID_INPUT"},status_code=400)
    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request,exc):
        return JSONResponse({"detail":"记录状态已改变，请刷新后重试","code":"CONFLICT"},status_code=409)
    @app.exception_handler(KeyError)
    async def missing_field(request,exc):
        return JSONResponse({"detail":"缺少必要输入字段","code":"INVALID_INPUT"},status_code=400)
    @app.exception_handler(RequestValidationError)
    async def validation_error(request,exc):
        return JSONResponse({"detail":[{"loc":e["loc"],"msg":e["msg"]} for e in exc.errors()],"code":"INVALID_INPUT"},status_code=422)

    @app.middleware("http")
    async def guard(request,call_next):
        if request.url.path.startswith("/api/") and request.method not in {"GET","HEAD","OPTIONS"}:
            if request.headers.get("origin","").rstrip("/")!=cfg.public_url:
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

    @app.get("/api/health")
    def health(request:Request):
        return {"status":"ok","edition":"public","version":"3.0.0","sqlite":sqlite3.sqlite_version,"development":cfg.dev}

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

    @app.post("/api/auth/register")
    def register(payload:dict,request:Request):
        return rt(request).auth.register(payload.get("email",""),payload.get("password",""),payload.get("invitation",""),request.client.host)
    @app.post("/api/auth/login")
    def login(payload:dict,request:Request):
        token,csrf,a=rt(request).auth.login(payload.get("email",""),payload.get("password",""),request.client.host)
        response=JSONResponse({"user":a.public()});cookie(response,cfg.session_cookie,token,True);cookie(response,cfg.csrf_cookie,csrf,False)
        return response
    @app.get("/api/auth/me")
    def me(a:Actor=Depends(actor)):
        return a.public()
    @app.post("/api/auth/logout")
    def logout(request:Request,a:Actor=Depends(actor)):
        rt(request).store.execute("UPDATE sessions SET revoked_at=? WHERE token_hash=? AND user_id=?",(stamp(),a.session_hash,a.id))
        response=JSONResponse({"success":True});response.delete_cookie(cfg.session_cookie,path="/",secure=not cfg.dev,httponly=True,samesite="lax")
        return response
    @app.post("/api/auth/resend-verification")
    def resend(payload:dict,request:Request):
        return rt(request).auth.resend(payload.get("email",""),request.client.host)
    @app.post("/api/auth/request-reset")
    def request_reset(payload:dict,request:Request):
        return rt(request).auth.resend(payload.get("email",""),request.client.host,"reset")
    @app.post("/api/auth/verify-email")
    def verify_email(payload:dict,request:Request):
        return rt(request).auth.consume(payload.get("token",""),"verify")
    @app.post("/api/auth/reset-password")
    def reset_password(payload:dict,request:Request):
        return rt(request).auth.consume(payload.get("token",""),"reset",payload.get("password",""))

    @app.get("/api/me/session")
    def session(a:Actor=Depends(user)):
        return {"requires_login":False,"role":a.role,"user":a.public()}
    @app.get("/api/me/preferences")
    def preferences(request:Request,a:Actor=Depends(user)):
        return get_preferences(rt(request).store,a.id).model_dump()
    @app.put("/api/me/preferences")
    def update_preferences(payload:dict,request:Request,a:Actor=Depends(user)):
        return save_preferences(rt(request).store,a.id,payload).model_dump()
    @app.get("/api/me/settings")
    def core_settings(request:Request,a:Actor=Depends(user)):
        p=get_preferences(rt(request).store,a.id)
        return {"settings":{**p.model_dump(),"timezone":"Asia/Shanghai","scheduler_enabled":any((p.morning_enabled,p.closing_enabled,p.weekly_enabled))},"schedule":{"plans":[]},"runtime_host":"public"}
    @app.get("/api/me/config/export")
    def export_preferences(request:Request,a:Actor=Depends(user)):
        return export(get_preferences(rt(request).store,a.id).model_dump_json(),"StockAI-preferences.json")
    @app.get("/api/me/model-presets")
    def presets(a:Actor=Depends(user)):
        return models.PRESETS
    @app.get("/api/me/model-profiles")
    def profiles(request:Request,a:Actor=Depends(user)):
        return models.profile_rows(rt(request).store,a.id)
    @app.post("/api/me/model-profiles")
    def create_profile(payload:models.ProfileInput,request:Request,a:Actor=Depends(user)):
        r=rt(request);return models.save_profile(r.store,r.keys,a.id,payload)
    @app.put("/api/me/model-profiles/{ident}")
    def update_profile(ident:str,payload:models.ProfileInput,request:Request,a:Actor=Depends(user)):
        r=rt(request);return models.save_profile(r.store,r.keys,a.id,payload,ident)
    @app.delete("/api/me/model-profiles/{ident}")
    def delete_profile(ident:str,request:Request,a:Actor=Depends(user)):
        models.remove_profile(rt(request).store,a.id,ident);return {"deleted":True}
    @app.post("/api/me/model-profiles/default")
    def default_profile(payload:dict,request:Request,a:Actor=Depends(user)):
        models.set_default(rt(request).store,a.id,payload.get("id"));return {"saved":True}
    @app.post("/api/me/model-profiles/{ident}/test")
    def test_profile(ident:str,request:Request,a:Actor=Depends(user)):
        return jobs.enqueue(rt(request).store,a.id,"profile_test",profile_id=ident)
    @app.get("/api/me/model/health")
    def model_health(request:Request,a:Actor=Depends(user)):
        fixed=models.snapshot(rt(request).store,a.id)
        return {"available":False,"configured":fixed is not None,"message":"已选择个人默认配置；连接结果请查看模型配置中的测试任务" if fixed else "尚未选择个人默认模型；可以先生成规则报告"}

    @app.get("/api/me/watchlist")
    def watchlist(request:Request,a:Actor=Depends(user)):
        return market.watch_rows(rt(request).store,a.id)
    @app.post("/api/me/watchlist")
    def add_watch(payload:InstrumentInput,request:Request,a:Actor=Depends(user)):
        return market.add_watch(rt(request).store,a.id,payload)
    @app.patch("/api/me/instruments/{ident}")
    def patch_watch(ident:str,payload:dict,request:Request,a:Actor=Depends(user)):
        return market.patch_watch(rt(request).store,a.id,ident,payload)
    @app.delete("/api/me/watchlist/{ident}")
    def remove_watch(ident:str,request:Request,a:Actor=Depends(user)):
        n=rt(request).store.execute("DELETE FROM user_watchlist WHERE user_id=? AND instrument_id=?",(a.id,ident))
        if not n:
            raise AuthError("NOT_FOUND","记录不存在",404)
        return {"removed":True}
    @app.get("/api/me/watchlist/export")
    def export_watchlist(request:Request,format:str="json",a:Actor=Depends(user)):
        rows=rt(request).store.query("SELECT i.symbol,i.market,i.asset_class,w.group_name,w.pinned FROM user_watchlist w JOIN instruments i ON i.id=w.instrument_id WHERE w.user_id=?",(a.id,))
        if format=="json":
            return export(json_text({"instruments":rows}),"StockAI-watchlist.json")
        if format!="csv":
            raise ValueError("导出格式不支持")
        buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=["symbol","market","asset_class","group_name","pinned"]);writer.writeheader();writer.writerows(rows)
        return export("\ufeff"+buffer.getvalue(),"StockAI-watchlist.csv","text/csv")
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
    @app.get("/api/me/instruments/{ident}")
    def detail(ident:str,request:Request,timeframe:str="1d",a:Actor=Depends(user)):
        store=rt(request).store;data=market.detail(store,a.id,ident,"1d" if timeframe=="1w" else timeframe)
        bars=resample_weekly(data["bars"]) if timeframe=="1w" else data["bars"]
        return {"instrument":{k:v for k,v in data.items() if k not in {"bars","indicators","quote","observation","budget","exclusion_reasons"}},"bars":bars,"indicators":analytics.indicators(bars),"observation":data["observation"],"risk_reasons":market.risk_reasons(data,get_preferences(store,a.id)),"events":store.query("SELECT id,event_type,severity,title,source_url,verified_at FROM risk_events WHERE instrument_id=? ORDER BY verified_at DESC LIMIT 100",(ident,))}
    @app.post("/api/me/budget")
    def estimate_budget(payload:dict,request:Request,a:Actor=Depends(user)):
        store=rt(request).store;p=get_preferences(store,a.id);i=market.instrument(store,str(payload.get("instrument_id")))
        value=budget_check(payload["price"],i["rules"],i["asset_class"],p.fees,p.budget,payload.get("quantity"));value["risk_reasons"]=market.risk_reasons(i,p);value["eligible"]=value["within_budget"] and not value["risk_reasons"]
        return value
    @app.get("/api/me/metals")
    def metals(request:Request,a:Actor=Depends(user)):
        byid={q["id"]:q for q in market.quotes(rt(request).store)}
        return [byid.get(i,{"id":i,"price":None,"status":"unavailable","stale":True,"message":"没有已确认行情"}) for i in ("FX:XAUUSD","FX:XAGUSD","SHFE:AU0","SHFE:AG0")]
    @app.get("/api/me/dashboard")
    def dashboard(request:Request,a:Actor=Depends(user)):
        store=rt(request).store;rows=market.watch_rows(store,a.id);reports=workspace.report_list(store,a.id)[:5]
        latest=workspace.report_detail(store,a.id,reports[0]["id"])["payload"] if reports else None;candidates=latest["candidates"] if latest else []
        indices=[market.detail(store,a.id,r["id"]) for r in store.query("SELECT id FROM instruments WHERE asset_class='index' ORDER BY id")]
        return {"version":"3.0.0","time":timestamp(),"calendar":market.calendar(store),"stats":{"watch_count":len(rows),"quote_count":sum(r["quote"] is not None for r in rows),"verified_count":sum(not market.risk_reasons(r,get_preferences(store,a.id)) for r in rows),"candidate_count":len(candidates)},"watchlist":rows,"indices":indices,"metals":metals(request,a),"reports":reports,"candidates":candidates,"jobs":jobs.list_jobs(store,a.id)["jobs"][:6],"notifications":store.query("SELECT * FROM personal_notifications WHERE user_id=? ORDER BY id DESC LIMIT 10",(a.id,)),"settings_summary":{"budget":get_preferences(store,a.id).budget,"fee_confirmed":get_preferences(store,a.id).fees.confirmed}}

    @app.get("/api/me/reports")
    def reports(request:Request,mode:str="",q:str="",a:Actor=Depends(user)):
        return workspace.report_list(rt(request).store,a.id,mode,q)
    @app.get("/api/me/reports/{ident}")
    def report_detail(ident:str,request:Request,a:Actor=Depends(user)):
        row=workspace.report_detail(rt(request).store,a.id,ident)
        return {**row["payload"],**{k:v for k,v in row.items() if k!="payload"}}
    @app.post("/api/me/reports/{ident}/supplement")
    def supplement(ident:str,request:Request,a:Actor=Depends(user)):
        return jobs.enqueue(rt(request).store,a.id,"supplement",parent_id=ident)
    @app.get("/api/me/reports/{ident}/export")
    def export_report(ident:str,request:Request,format:str="md",a:Actor=Depends(user)):
        store=rt(request).store;row=workspace.owned(store,"personal_reports",a.id,ident)
        if format=="json":
            return export(row["payload_json"],ident+".json")
        if format not in {"md","html"}:
            raise ValueError("不支持的格式")
        path=backups.safe_file(store.cfg.data,Path(row["markdown_path" if format=="md" else "html_path"]).as_posix())
        return FileResponse(path,filename=ident+"."+format,media_type="text/markdown" if format=="md" else "text/html")
    @app.get("/api/me/jobs")
    def job_list(request:Request,a:Actor=Depends(user)):
        p=get_preferences(rt(request).store,a.id)
        plans=[{"kind":kind,"label":name,"time":getattr(p,time),"enabled":getattr(p,enabled),"timezone":"Asia/Shanghai","rule":rule+(" · 已开启" if getattr(p,enabled) else " · 未开启")} for kind,name,time,enabled,rule in (("standard","标准版早报","morning_time","morning_enabled","每天，仅补跑当天"),("closing","交易日收盘跟踪","closing_time","closing_enabled","仅已确认交易日"),("full","周日深度复盘","weekly_time","weekly_enabled","每周日，仅补跑当天"))]
        return {**jobs.list_jobs(rt(request).store,a.id),"schedule":{"plans":plans,"enabled":any(x["enabled"] for x in plans)}}
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
    @app.post("/api/me/jobs/{ident}/retry")
    def retry_job(ident:str,request:Request,a:Actor=Depends(user)):
        row=workspace.owned(rt(request).store,"personal_jobs",a.id,ident)
        # Never blindly repeat an uncertain provider call; a new report version is explicit.
        result=json.loads(row["result_json"] or "{}")
        if result.get("report_id"):
            return jobs.enqueue(rt(request).store,a.id,"supplement",parent_id=result["report_id"])
        raise ValueError("此任务不可自动重试；请检查原因后显式提交新的研究或连接测试")
    @app.get("/api/me/simulation")
    def portfolio(request:Request,a:Actor=Depends(user)):
        return simulation.portfolio(rt(request).store,a.id)
    @app.post("/api/me/accounts")
    def create_account(payload:dict,request:Request,a:Actor=Depends(user)):
        return simulation.create_account(rt(request).store,a.id,payload.get("name",""),payload.get("initial_cash",0))
    @app.get("/api/me/accounts/{ident}")
    def account_detail(ident:str,request:Request,a:Actor=Depends(user)):
        return workspace.owned(rt(request).store,"sim_accounts",a.id,ident)
    @app.post("/api/me/orders")
    def create_order(payload:OrderInput,request:Request,a:Actor=Depends(user)):
        return simulation.order(rt(request).store,a.id,payload)
    @app.get("/api/me/orders/{ident}")
    def order_detail(ident:str,request:Request,a:Actor=Depends(user)):
        return workspace.owned(rt(request).store,"sim_orders",a.id,ident)
    @app.post("/api/me/orders/{ident}/cancel")
    def cancel_order(ident:str,request:Request,a:Actor=Depends(user)):
        return simulation.cancel(rt(request).store,a.id,ident)
    @app.get("/api/me/backtests")
    def backtests(request:Request,a:Actor=Depends(user)):
        rows=rt(request).store.query("SELECT * FROM personal_backtests WHERE user_id=? ORDER BY created_at DESC LIMIT 100",(a.id,))
        return [{"id":r["id"],"created_at":r["created_at"],"params":json.loads(r["params_json"]),"summary":json.loads(r["result_json"])["summary"],"validated":json.loads(r["result_json"])["validated"],"strategy_version":r["strategy_version"],"data_hash":r["data_hash"]} for r in rows]
    @app.post("/api/me/backtests")
    def create_backtest(payload:backtest.Parameters,request:Request,a:Actor=Depends(user)):
        return workspace.create_backtest(rt(request).store,a.id,payload)
    @app.get("/api/me/backtests/{ident}")
    def backtest_detail(ident:str,request:Request,a:Actor=Depends(user)):
        r=workspace.owned(rt(request).store,"personal_backtests",a.id,ident)
        return {"id":r["id"],"created_at":r["created_at"],"params":json.loads(r["params_json"]),"strategy_version":r["strategy_version"],"data_hash":r["data_hash"],**json.loads(r["result_json"])}
    @app.post("/api/me/backtests/{ident}/reproduce")
    def reproduce(ident:str,request:Request,a:Actor=Depends(user)):
        return workspace.reproduce(rt(request).store,a.id,ident)
    @app.get("/api/me/backtests/{ident}/export")
    def export_backtest(ident:str,request:Request,a:Actor=Depends(user)):
        return export(json_text(backtest_detail(ident,request,a)),ident+".json")
    @app.get("/api/me/notifications")
    def notifications(request:Request,a:Actor=Depends(user)):
        return rt(request).store.query("SELECT * FROM personal_notifications WHERE user_id=? ORDER BY id DESC LIMIT 100",(a.id,))
    @app.post("/api/me/notifications/read")
    def read_notifications(request:Request,a:Actor=Depends(user)):
        rt(request).store.execute("UPDATE personal_notifications SET read=1 WHERE user_id=?",(a.id,));return {"success":True}
    @app.post("/api/me/erase")
    def erase_account(payload:dict,request:Request,a:Actor=Depends(actor)):
        r=rt(request);r.auth.limit("erase",a.id,5,3600)
        value=erase(r.store,r.journal,a,payload.get("password",""),payload.get("confirmation",""))
        response=JSONResponse(value);response.delete_cookie(cfg.session_cookie,path="/",secure=not cfg.dev,httponly=True,samesite="lax")
        return response

    @app.get("/api/admin/users")
    def users(request:Request,a:Actor=Depends(admin)):
        return rt(request).store.query("SELECT id,email,role,status,verified_at,created_at FROM users ORDER BY created_at")
    @app.get("/api/admin/status")
    def admin_status(request:Request,a:Actor=Depends(admin)):
        store=rt(request).store
        return {"smtp_configured":bool(cfg.smtp_host and cfg.smtp_from),"backup_configured":cfg.age_recipient.startswith("age1"),"ollama_enabled":cfg.ollama_enabled,"sqlite":sqlite3.sqlite_version,"mail_pending":store.one("SELECT COUNT(*) n FROM mail_outbox WHERE state IN('queued','retry','sending')")["n"],"mail_failed":store.one("SELECT COUNT(*) n FROM mail_outbox WHERE state='failed'")["n"]}
    @app.post("/api/admin/users/{ident}/status")
    def user_status(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        rt(request).auth.disable(a,ident,bool(payload.get("disabled",True)));return {"saved":True}
    @app.get("/api/admin/invitations")
    def invitations(request:Request,a:Actor=Depends(admin)):
        return rt(request).store.query("SELECT id,created_at,expires_at,revoked_at,used_at FROM invitations ORDER BY created_at DESC LIMIT 100")
    @app.post("/api/admin/invitations")
    def invitation(request:Request,a:Actor=Depends(admin)):
        return rt(request).auth.invitation(a.id)
    @app.delete("/api/admin/invitations/{ident}")
    def revoke_invitation(ident:str,request:Request,a:Actor=Depends(admin)):
        rt(request).store.execute("UPDATE invitations SET revoked_at=? WHERE id=? AND used_at IS NULL",(stamp(),ident));rt(request).store.audit(a.id,"revoke_invitation",ident)
        return {"revoked":True}
    @app.get("/api/admin/market")
    def shared_market(request:Request,a:Actor=Depends(admin)):
        return [market.instrument(rt(request).store,r["id"]) for r in rt(request).store.query("SELECT id FROM instruments ORDER BY market,symbol")]
    @app.post("/api/admin/instruments/{ident}/verify")
    def verify_instrument(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        value=market.verify(rt(request).store,ident,payload);rt(request).store.audit(a.id,"verify_instrument",ident);return value
    @app.post("/api/admin/instruments/{ident}/sync")
    def sync_instrument(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        market.instrument(rt(request).store,ident);tf=payload.get("timeframe","1d")
        if tf not in {"1d","15m","60m"}:
            raise ValueError("无效周期")
        return collector.enqueue(rt(request).store,"instrument",ident,tf)
    @app.post("/api/admin/instruments/{ident}/announcements")
    def announcements(ident:str,request:Request,a:Actor=Depends(admin)):
        market.instrument(rt(request).store,ident);return collector.enqueue(rt(request).store,"announcements",ident)
    @app.post("/api/admin/instruments/{ident}/bars/import")
    def import_bars(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        return market.import_bars(rt(request).store,ident,payload)
    @app.get("/api/admin/jobs")
    def site_jobs(request:Request,a:Actor=Depends(admin)):
        return rt(request).store.query("SELECT * FROM site_jobs ORDER BY created_at DESC LIMIT 100")
    @app.post("/api/admin/jobs")
    def site_job(payload:dict,request:Request,a:Actor=Depends(admin)):
        return collector.enqueue(rt(request).store,payload.get("kind","sync"))
    @app.get("/api/admin/backups")
    def list_backups(request:Request,a:Actor=Depends(admin)):
        r=rt(request);return backups.list_backups(r.store,r.journal)
    @app.post("/api/admin/backups")
    def backup(request:Request,a:Actor=Depends(admin)):
        ident=uuid.uuid4().hex
        rt(request).store.execute("INSERT INTO site_jobs(id,logical_key,kind,status,created_at) VALUES(?,?,'backup','queued',?)",(ident,"backup:"+ident,stamp()))
        rt(request).store.audit(a.id,"backup_create",ident);return {"id":ident,"status":"queued"}
    @app.post("/api/admin/backups/{ident}/reauth")
    def reauth_backup(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        r=rt(request);backups.registered(r.store,r.journal,ident)
        return {"grant":r.auth.reauth(a,payload.get("password",""),ident)}
    @app.post("/api/admin/backups/{ident}/download")
    def export_backup(ident:str,request:Request,a:Actor=Depends(admin)):
        r=rt(request);r.auth.consume_grant(a,request.headers.get("x-reauth-grant",""),ident);path,_=backups.registered(r.store,r.journal,ident)
        r.store.audit(a.id,"backup_export",ident);return FileResponse(path,filename="StockAI-"+ident+".age",media_type="application/octet-stream")

    static=ROOT/"frontend"/"dist-public"
    if static.is_dir():
        app.mount("/assets",StaticFiles(directory=static/"assets"),name="assets")
        @app.get("/favicon.svg")
        def favicon():
            return FileResponse(static/"favicon.svg")
        @app.get("/")
        def index():
            return FileResponse(static/"index.html")
    return app


app=create_app()
