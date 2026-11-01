from __future__ import annotations

import http.client
import ipaddress
import json
import re
import socket
import ssl
import threading
import uuid
from urllib.parse import urlsplit

from pydantic import BaseModel,ConfigDict,Field,model_validator

from backend.ai import ResearchText,validate_output
from backend.config import ROOT
from .auth import AuthError
from .store import stamp,json_text

PRESETS={
    "cn":{"base_url":"https://api.minimax.cn/v1","model":"MiniMax-M3.1-Flash","label":"MiniMax 国内站 · 订阅 Key"},
    "global":{"base_url":"https://api.minimax.io/v1","model":"MiniMax-M3.1-Flash","label":"MiniMax 国际站 · M Plan Key"},
}
OLLAMA_LOCK=threading.Lock()


class ModelError(Exception):
    def __init__(self,code,retryable=False,uncertain=False):
        self.code,self.retryable,self.uncertain=code,retryable,uncertain
        super().__init__(code)


MESSAGES={
    "NO_PROFILE":"未选择默认模型配置，已生成规则报告",
    "AUTO_AI_DISABLED":"自动 AI 未开启，本次仅生成规则报告",
    "PROFILE_DISABLED":"所选模型配置已停用或删除",
    "RECONNECT_REQUIRED":"恢复后的模型配置需要本人重新连接",
    "KEY_REVOKED":"任务固定的凭据已删除或替换，请重新提交任务",
    "PERMISSION_REVOKED":"任务所需的数据授权已撤销，未发送模型请求",
    "INVALID_KEY":"模型凭据无效或没有访问权限",
    "QUOTA_EXCEEDED":"订阅或供应商额度不足",
    "RATE_LIMITED":"供应商限流，允许的重试后仍不可用",
    "TEMPORARY_PROVIDER_ERROR":"模型服务暂时不可用",
    "UNCERTAIN_TIMEOUT":"请求已发出但结果不确定，未自动重试，供应商可能已计入额度",
    "CONNECT_FAILED":"连接未建立，模型服务暂时不可达",
    "ADDRESS_REJECTED":"模型地址不符合公网 HTTPS 出站规则",
    "INVALID_OUTPUT":"模型输出未通过结构或事实校验，已生成规则报告",
    "INCOMPATIBLE_PROVIDER":"模型接口或请求协议不兼容",
    "OLLAMA_BUSY":"服务器 Ollama 已有推理任务，未并行启动本地模型",
    "OLLAMA_DISABLED":"站点尚未启用服务器 Ollama",
    "ACCOUNT_DISABLED":"账号状态已改变，已停止后续模型调用",
    "INTERNAL_MODEL_ERROR":"模型调用未完成，错误信息已脱敏",
}


def public_ips(host,port=443):
    try:
        raw=socket.getaddrinfo(host,port,type=socket.SOCK_STREAM)
        ips=list(dict.fromkeys(row[4][0] for row in raw))
        if not ips:
            raise ValueError()
        for value in ips:
            ip=ipaddress.ip_address(value)
            if ip.version==6 and ip.ipv4_mapped:
                ip=ip.ipv4_mapped
            if not ip.is_global or ip.is_multicast or ip.is_reserved or ip.is_loopback or "%" in value:
                raise ValueError()
        return ips
    except (OSError,ValueError):
        raise ModelError("ADDRESS_REJECTED") from None


def cloud_address(value,resolve=True):
    try:
        p=urlsplit(value)
        if p.scheme!="https" or not p.hostname or p.username or p.password or p.query or p.fragment or any(c in value for c in ("\r","\n","\\")):
            raise ValueError()
        p.hostname.encode("idna")
        port=p.port or 443
        if not 1<=port<=65535:
            raise ValueError()
        # Reject literal special-use IPs before resolution as well.
        try:
            ip=ipaddress.ip_address(p.hostname)
        except ValueError:
            ip=None
        if ip is not None and not ip.is_global:
            raise ValueError()
        if resolve:
            public_ips(p.hostname,port)
        return value.rstrip("/")
    except (ValueError,UnicodeError):
        raise ModelError("ADDRESS_REJECTED") from None


class ProfileInput(BaseModel):
    model_config=ConfigDict(extra="forbid")
    name:str=Field(min_length=1,max_length=80)
    provider:str="minimax"
    region:str="cn"
    base_url:str=""
    model:str=Field("MiniMax-M3.1-Flash",min_length=1,max_length=150)
    api_key:str|None=Field(None,max_length=1024)
    remove_key:bool=False
    active:bool=True
    allow_simulation_summary:bool=False
    allow_trade_details:bool=False

    @model_validator(mode="after")
    def valid_provider(self):
        if self.provider not in {"minimax","openai_compatible","ollama"}:
            raise ValueError("不支持的模型服务")
        if self.provider=="minimax":
            if self.region not in PRESETS:
                raise ValueError("请选择国内站或国际站")
            self.base_url=PRESETS[self.region]["base_url"]
        elif self.provider=="openai_compatible":
            cloud_address(self.base_url,resolve=False)
            self.region="custom"
        else:
            self.base_url=""; self.region="server"
            if self.api_key:
                raise ValueError("服务器 Ollama 不使用个人 API Key")
        if self.api_key is not None and self.api_key!="" and (len(self.api_key)<8 or re.search(r"[\r\n\s]",self.api_key)):
            raise ValueError("Key 格式无效")
        if self.api_key and any(self.api_key in value for value in (self.name,self.base_url,self.model)):
            raise ValueError("配置名称、地址或模型名称中不能填写 Key")
        return self


def profile_rows(store,uid):
    if not uid:
        raise ValueError("必须指定用户")
    rows=store.query("""SELECT p.*,v.provider,v.region,v.base_url,v.model,v.credential_id,v.allow_summary,v.allow_trades,
        c.key_hint,c.revoked_at,c.envelope_json IS NOT NULL AS key_present
        FROM model_profiles p JOIN model_profile_versions v ON v.profile_id=p.id AND v.version=p.current_version
        LEFT JOIN model_credentials c ON c.id=v.credential_id WHERE p.user_id=? ORDER BY p.created_at""",(uid,))
    default=store.one("SELECT default_profile_id FROM user_settings WHERE user_id=?",(uid,))
    for r in rows:
        r["is_default"]=bool(default and default["default_profile_id"]==r["id"])
        r["allow_simulation_summary"]=bool(r.pop("allow_summary")); r["allow_trade_details"]=bool(r.pop("allow_trades"))
        r["credential_status"]="not_required" if r["provider"]=="ollama" else "configured" if r["key_present"] and not r["revoked_at"] else "missing"
        r.pop("credential_id",None); r.pop("key_present",None); r.pop("revoked_at",None)
    return rows


def save_profile(store,keys,uid,value:ProfileInput,ident=None):
    if value.provider!="ollama":
        cloud_address(value.base_url)
    updating=ident is not None
    ident=ident or uuid.uuid4().hex
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("ACCOUNT_DISABLED","账号不可用",403)
        old=c.execute("SELECT * FROM model_profiles WHERE id=? AND user_id=?",(ident,uid)).fetchone()
        if updating and not old:
            raise AuthError("NOT_FOUND","模型配置不存在",404)
        if old:
            version=old["current_version"]+1
            previous=c.execute("SELECT * FROM model_profile_versions WHERE profile_id=? AND version=?",(ident,old["current_version"])).fetchone()
            credential=previous["credential_id"]
        else:
            if c.execute("SELECT COUNT(*) FROM model_profiles WHERE user_id=?",(uid,)).fetchone()[0]>=20:
                raise AuthError("PROFILE_LIMIT","最多保存20套模型配置")
            version=1; credential=None
            c.execute("INSERT INTO model_profiles(id,user_id,name,current_version,active,created_at) VALUES(?,?,?,?,?,?)",(ident,uid,value.name,version,int(value.active),stamp()))
        if value.remove_key or value.api_key or value.provider=="ollama" or (old and (previous["provider"]!=value.provider or previous["base_url"]!=value.base_url)):
            c.execute("UPDATE model_credentials SET revoked_at=?,envelope_json=NULL WHERE profile_id=? AND revoked_at IS NULL",(stamp(),ident))
            credential=None
        if value.api_key:
            credential=uuid.uuid4().hex
            sealed=keys.seal(value.api_key,site=store.site_id,user=uid,resource=credential,purpose="model-key")
            c.execute("INSERT INTO model_credentials VALUES(?,?,?,?,?,NULL,?)",(credential,uid,ident,sealed,"••••"+value.api_key[-4:],stamp()))
        c.execute("INSERT INTO model_profile_versions VALUES(?,?,?,?,?,?,?,?,?,?)",(ident,version,value.provider,value.region,value.base_url,value.model,credential,int(value.allow_simulation_summary),int(value.allow_trade_details),stamp()))
        reconnect=0 if value.api_key or value.provider=="ollama" else old["needs_reconnect"] if old else 0
        c.execute("UPDATE model_profiles SET name=?,current_version=?,active=?,needs_reconnect=? WHERE id=? AND user_id=?",(value.name,version,int(value.active),reconnect,ident,uid))
    return next(r for r in profile_rows(store,uid) if r["id"]==ident)


def set_default(store,uid,ident):
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if ident is not None and not c.execute("SELECT id FROM model_profiles WHERE id=? AND user_id=? AND active=1",(ident,uid)).fetchone():
            raise AuthError("NOT_FOUND","模型配置不存在",404)
        c.execute("UPDATE user_settings SET default_profile_id=? WHERE user_id=?",(ident,uid))


def remove_profile(store,uid,ident):
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not c.execute("SELECT id FROM model_profiles WHERE id=? AND user_id=?",(ident,uid)).fetchone():
            raise AuthError("NOT_FOUND","模型配置不存在",404)
        c.execute("UPDATE user_settings SET default_profile_id=NULL WHERE user_id=? AND default_profile_id=?",(uid,ident))
        c.execute("UPDATE model_profile_versions SET credential_id=NULL WHERE profile_id=?",(ident,))
        c.execute("DELETE FROM model_profiles WHERE id=? AND user_id=?",(ident,uid))


def snapshot(store,uid,ident=None):
    if ident is None:
        row=store.one("SELECT default_profile_id FROM user_settings WHERE user_id=?",(uid,))
        ident=row["default_profile_id"] if row else None
    if not ident:
        return None
    row=store.one("SELECT p.*,v.* FROM model_profiles p JOIN model_profile_versions v ON v.profile_id=p.id AND v.version=p.current_version WHERE p.id=? AND p.user_id=?",(ident,uid))
    if not row:
        raise AuthError("NOT_FOUND","模型配置不存在",404)
    return {k:row[k] for k in ("profile_id","version","provider","region","base_url","model","credential_id","allow_summary","allow_trades")}


def check_live(store,uid,fixed):
    if not store.active(uid):
        raise ModelError("ACCOUNT_DISABLED")
    if not fixed:
        raise ModelError("NO_PROFILE")
    row=store.one("SELECT p.*,v.allow_summary,v.allow_trades FROM model_profiles p JOIN model_profile_versions v ON v.profile_id=p.id AND v.version=p.current_version WHERE p.id=? AND p.user_id=?",(fixed["profile_id"],uid))
    if not row or not row["active"]:
        raise ModelError("PROFILE_DISABLED")
    if row["needs_reconnect"]:
        raise ModelError("RECONNECT_REQUIRED")
    if any(fixed[k] and not row[k] for k in ("allow_summary","allow_trades")):
        raise ModelError("PERMISSION_REVOKED")
    if fixed["provider"]=="ollama":
        if not store.cfg.ollama_enabled:
            raise ModelError("OLLAMA_DISABLED")
        return None
    cred=store.one("SELECT * FROM model_credentials WHERE id=? AND user_id=? AND profile_id=? AND revoked_at IS NULL AND envelope_json IS NOT NULL",(fixed["credential_id"],uid,fixed["profile_id"]))
    if not cred:
        raise ModelError("KEY_REVOKED")
    return cred


def project_input(facts,fixed):
    """Build a new allowlisted value; never serialize the original private facts object."""
    def select(row,fields):
        return {k:row[k] for k in fields if k in row}
    result={k:facts[k] for k in ("mode","report_date","calendar","budget_limit","candidate_limit","strategy_version") if k in facts}
    result["watchlist"]=[select(r,("instrument_id","symbol","market","asset_class","official_name")) for r in facts.get("watchlist",[])]
    result["candidates"]=[select(r,("instrument_id","symbol","name","price","quantity","score","quote_time","source_ids","indicators")) for r in facts.get("candidates",[])]
    result["excluded"]=[select(r,("instrument_id","name","reasons","price")) for r in facts.get("excluded",[])]
    result["sources"]=[select(r,("id","instrument_id","source","url","quote_time","collected_at")) for r in facts.get("sources",[])]
    result["market_summary"]=[select(r,("instrument_id","symbol","market","name","price","quote_time","source_url","currency","unit")) for r in facts.get("auxiliary_markets",[])+facts.get("metals",[])]
    result["research_status"]="仅使用程序提供的研究事实；缺失风险证据不等于安全；费用为用户的模拟假设。"
    sim=facts.get("simulation",{})
    if fixed.get("allow_summary"):
        result["simulation_summary"]=[select(r,("initial_cash","cash","equity","pnl","positions")) for r in sim.get("accounts",[])]
        for a in result["simulation_summary"]:
            if "positions" in a:
                a["positions"]=[select(p,("instrument_id","quantity","cost","market_value","unrealized_pnl")) for p in a["positions"]]
    if fixed.get("allow_trades"):
        result["trade_details"]=[select(r,("instrument_id","side","quantity","fill_price","fees","filled_at")) for r in sim.get("recent_fills",[])]
    return result


class PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self,host,port,ip,timeout):
        super().__init__(host,port,timeout=timeout,context=ssl.create_default_context())
        self.checked_ip=ip

    def connect(self):
        ip=ipaddress.ip_address(self.checked_ip)
        sock=socket.socket(socket.AF_INET6 if ip.version==6 else socket.AF_INET,socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        try:
            sock.connect((str(ip),self.port))
            self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def request_ollama(store,uid,payload,before_send):
    p=urlsplit(store.cfg.ollama_url);sent=False
    cls=http.client.HTTPSConnection if p.scheme=="https" else http.client.HTTPConnection
    conn=cls(p.hostname,p.port,timeout=min(20,store.cfg.model_timeout));deadline=None
    try:
        conn.connect();conn.sock.settimeout(5)
        deadline=request_deadline(conn.sock,store.cfg.model_timeout)
        with store.user_lock(uid):
            before_send();sent=True
            conn.request("POST","/api/chat",body=json_text(payload).encode(),headers={"Content-Type":"application/json"})
        conn.sock.settimeout(store.cfg.model_timeout)
        response=conn.getresponse();raw=response.read(2*1024*1024+1)
        if response.status!=200:
            raise ModelError("INCOMPATIBLE_PROVIDER")
        if len(raw)>2*1024*1024:
            raise ModelError("INVALID_OUTPUT")
        data=json.loads(raw)
        return data["message"]["content"],{k:data[k] for k in ("prompt_eval_count","eval_count") if isinstance(data.get(k),int)},""
    except (socket.timeout,TimeoutError,OSError,http.client.HTTPException):
        raise ModelError("UNCERTAIN_TIMEOUT" if sent else "CONNECT_FAILED",uncertain=sent,retryable=not sent) from None
    finally:
        if deadline:
            deadline.cancel()
        conn.close()


def request_deadline(sock,seconds):
    def abort():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer=threading.Timer(seconds,abort);timer.daemon=True;timer.start()
    return timer


def request_cloud(store,uid,fixed,body,before_send,key_loader):
    url=cloud_address(fixed["base_url"],resolve=False)
    p=urlsplit(url); port=p.port or 443
    ips=public_ips(p.hostname,port)
    conn=PinnedHTTPS(p.hostname,port,ips[0],min(store.cfg.model_timeout,30))
    sent=False;deadline=None
    try:
        conn.connect()  # TLS is verified for the original hostname against a pinned address.
        conn.sock.settimeout(5)
        deadline=request_deadline(conn.sock,store.cfg.model_timeout)
        with store.user_lock(uid):
            cred=check_live(store,uid,fixed)
            key=key_loader(cred)
            before_send()
            sent=True
            conn.request("POST",p.path.rstrip("/")+"/chat/completions",body=json_text(body).encode(),headers={"Authorization":"Bearer "+key,"Content-Type":"application/json"})
        conn.sock.settimeout(store.cfg.model_timeout)
        response=conn.getresponse()
        raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024:
            raise ModelError("INVALID_OUTPUT")
        status=response.status
        if status in {401,403}:
            raise ModelError("INVALID_KEY")
        if status==402:
            raise ModelError("QUOTA_EXCEEDED")
        if status==429:
            raise ModelError("RATE_LIMITED",retryable=True)
        if status in {502,503}:
            raise ModelError("TEMPORARY_PROVIDER_ERROR",retryable=True)
        if status!=200:
            raise ModelError("INCOMPATIBLE_PROVIDER")
        try:
            result=json.loads(raw)
            base=result.get("base_resp",{})
            if base.get("status_code"):
                code=base["status_code"]
                raise ModelError("INVALID_KEY" if code==1004 else "QUOTA_EXCEEDED" if code==1008 else "RATE_LIMITED" if code in {1002,1302} else "INCOMPATIBLE_PROVIDER",retryable=code in {1002,1302})
            content=result["choices"][0]["message"]["content"]
            usage={k:v for k,v in result.get("usage",{}).items() if k in {"prompt_tokens","completion_tokens","total_tokens"} and isinstance(v,(int,float))}
            request_id=response.getheader("X-Request-ID","")[:150]
            return content,usage,request_id if re.fullmatch(r"[A-Za-z0-9._:-]*",request_id) else ""
        except (ValueError,KeyError,TypeError,IndexError):
            raise ModelError("INVALID_OUTPUT") from None
    except (socket.timeout,TimeoutError):
        raise ModelError("UNCERTAIN_TIMEOUT" if sent else "CONNECT_FAILED",retryable=not sent,uncertain=sent) from None
    except ssl.SSLError:
        raise ModelError("ADDRESS_REJECTED") from None
    except (OSError,http.client.HTTPException):
        raise ModelError("UNCERTAIN_TIMEOUT" if sent else "CONNECT_FAILED",retryable=not sent,uncertain=sent) from None
    finally:
        if deadline:
            deadline.cancel()
        conn.close()


def call_model(store,keys,job,facts,attempt=1,transport=None):
    uid=job["user_id"]; fixed=json.loads(job["snapshot_json"]).get("profile")
    check_live(store,uid,fixed)
    projected=project_input(facts,fixed)
    ident=uuid.uuid4().hex
    store.execute("INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,created_at) VALUES(?,?,?,?,?,?,?,?)",(ident,uid,job["id"],fixed["profile_id"],fixed["version"],attempt,"prepared",stamp()))
    def before_send():
        check_live(store,uid,fixed)
        if job.get("automatic"):
            from .preferences import get_preferences
            if not get_preferences(store,uid).automatic_ai:
                raise ModelError("AUTO_AI_DISABLED")
        if not store.one("SELECT id FROM personal_jobs WHERE id=? AND user_id=? AND status='running'",(job["id"],uid)):
            raise ModelError("ACCOUNT_DISABLED")
        store.execute("UPDATE model_calls SET phase='sent' WHERE id=?",(ident,))
    secrets_seen=set()
    def decrypt(credential):
        try:
            value=keys.open(credential["envelope_json"],site=store.site_id,user=uid,resource=credential["id"],purpose="model-key")
            secrets_seen.add(value)
            return value
        except Exception:
            raise ModelError("KEY_REVOKED") from None
    prompt=(ROOT/"config"/("prompt-full.md" if facts["mode"]=="full" else "prompt-standard.md")).read_text(encoding="utf-8")
    prompt+="\n仅返回给定结构JSON；结构："+json_text(ResearchText.model_json_schema())
    messages=[{"role":"system","content":prompt},{"role":"user","content":json_text(projected)}]
    try:
        if transport:
            with store.user_lock(uid):
                cred=check_live(store,uid,fixed); before_send()
                key=decrypt(cred) if cred else None
            content,usage,request_id=transport(fixed,messages,key)
        elif fixed["provider"]=="ollama":
            if not OLLAMA_LOCK.acquire(blocking=False):
                raise ModelError("OLLAMA_BUSY")
            try:
                payload={"model":fixed["model"],"stream":False,"think":False,"format":ResearchText.model_json_schema(),"messages":messages,"options":{"temperature":0.1,"num_ctx":16384,"num_predict":3000}}
                content,usage,request_id=request_ollama(store,uid,payload,before_send)
            finally:
                OLLAMA_LOCK.release()
        else:
            body={"model":fixed["model"],"messages":messages,"stream":False}
            if fixed["provider"]=="minimax":
                body.update(max_completion_tokens=8192,reasoning_split=True,reasoning_effort="low")
            else:
                body.update(max_tokens=4096,temperature=0.1)
            content,usage,request_id=request_cloud(store,uid,fixed,body,before_send,decrypt)
        # Ignore separately returned reasoning; never archive it as research output.
        content=re.sub(r"^\s*<think>.*?</think>\s*","",str(content),flags=re.S)
        try:
            text=ResearchText.model_validate_json(content)
            validate_output(text,projected)
            if any(secret in json_text(text.model_dump()) for secret in secrets_seen):
                raise ValueError("凭据回显被拒绝")
        except (ValueError,TypeError):
            raise ModelError("INVALID_OUTPUT") from None
        if any(secret in str(request_id) for secret in secrets_seen):
            request_id=""
        store.execute("UPDATE model_calls SET phase='received',usage_json=?,provider_request_id=?,finished_at=? WHERE id=?",(json_text(usage),request_id,stamp(),ident))
        return text.model_dump(),{"provider":fixed["provider"],"model":fixed["model"],"profile_id":fixed["profile_id"],"profile_version":fixed["version"],"usage":usage}
    except ModelError as exc:
        store.execute("UPDATE model_calls SET phase=?,error_code=?,finished_at=? WHERE id=?",("uncertain" if exc.uncertain else "failed",exc.code,stamp(),ident))
        raise
    except Exception:
        store.execute("UPDATE model_calls SET phase='failed',error_code='INVALID_OUTPUT',finished_at=? WHERE id=?",(stamp(),ident))
        raise ModelError("INVALID_OUTPUT") from None
