# 管理用户自有模型配置、凭据授权、受限出站请求与模型调用状态。
from __future__ import annotations

import http.client
import ipaddress
import json
import sqlite3
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


# 表达稳定错误码，并区分可重试故障与已发送但结果不确定的故障。
class ModelError(Exception):
    # 保存重试和结果不确定标记，供任务层决定是否再次调用。
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
    "USAGE_LIMIT":"个人日/月调用上限不足，未发送模型请求",
    "USAGE_UNAVAILABLE":"调用用量权威记录不可核对，已暂停模型调用",
    "CONTEXT_TOO_LARGE":"当前问题或所选事实超出上下文预算，请缩小对象",
    "CANCELLED":"请求已停止；已发送部分的供应商用量不能撤回",
}


# 解析域名的全部地址，仅接受公网单播地址以限制服务端请求范围。
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


# 校验 HTTPS 地址、端口与主机，拒绝凭据、片段和非公网目标。
def cloud_address(value,resolve=True):
    try:
        p=urlsplit(value)
        if p.scheme!="https" or not p.hostname or p.username or p.password or p.query or p.fragment or any(c in value for c in ("\r","\n","\\")):
            raise ValueError()
        p.hostname.encode("idna")
        port=p.port or 443
        if not 1<=port<=65535:
            raise ValueError()
        # 除域名解析结果外，也先拒绝地址中直接填写的非公网 IP。
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


# 定义模型配置与数据授权输入，API Key 只用于加密存储。
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
    context_window:int=Field(16384,ge=1024,le=1000000,strict=True)
    output_limit:int=Field(4096,ge=128,le=4096,strict=True)

    # 按供应商应用预设或校验自定义地址，同时检查 Key 格式与泄漏风险。
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


# 仅列出本人的模型配置，返回凭据提示与状态并移除内部凭据字段。
def profile_rows(store,uid):
    if not uid:
        raise ValueError("必须指定用户")
    rows=store.query("""SELECT p.*,v.provider,v.region,v.base_url,v.model,v.credential_id,v.allow_summary,v.allow_trades,
        c.key_hint,c.revoked_at,c.envelope_json IS NOT NULL AS key_present
        FROM model_profiles p JOIN model_profile_versions v ON v.profile_id=p.id AND v.version=p.current_version
        LEFT JOIN model_credentials c ON c.id=v.credential_id WHERE p.user_id=? ORDER BY p.created_at""",(uid,))
    default=store.one("SELECT default_profile_id FROM user_settings WHERE user_id=?",(uid,))
    for r in rows:
        cap=store.one('SELECT context_window,output_limit FROM model_context_limits WHERE profile_id=? AND version=?',(r['id'],r['current_version']))
        r.update(cap or {'context_window':16384,'output_limit':4096})
        r["is_default"]=bool(default and default["default_profile_id"]==r["id"])
        r["allow_simulation_summary"]=bool(r.pop("allow_summary")); r["allow_trade_details"]=bool(r.pop("allow_trades"))
        r["credential_status"]="not_required" if r["provider"]=="ollama" else "configured" if r["key_present"] and not r["revoked_at"] else "missing"
        r.pop("credential_id",None); r.pop("key_present",None); r.pop("revoked_at",None)
    return rows


# 在用户锁和事务中保存配置版本，按需撤销旧凭据并加密新 Key。
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
        c.execute('INSERT INTO model_context_limits VALUES(?,?,?,?)',(ident,version,value.context_window,min(value.output_limit,max(128,value.context_window//3))))
        reconnect=0 if value.api_key or value.provider=="ollama" else old["needs_reconnect"] if old else 0
        c.execute("UPDATE model_profiles SET name=?,current_version=?,active=?,needs_reconnect=? WHERE id=? AND user_id=?",(value.name,version,int(value.active),reconnect,ident,uid))
    return next(r for r in profile_rows(store,uid) if r["id"]==ident)


# 只允许将本人有效配置设为默认，也允许明确清空默认选择。
def set_default(store,uid,ident):
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if ident is not None and not c.execute("SELECT id FROM model_profiles WHERE id=? AND user_id=? AND active=1",(ident,uid)).fetchone():
            raise AuthError("NOT_FOUND","模型配置不存在",404)
        c.execute("UPDATE user_settings SET default_profile_id=? WHERE user_id=?",(ident,uid))


# 清除本人模型配置及可用凭据，并解除默认配置关联。
def remove_profile(store,uid,ident):
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not c.execute("SELECT id FROM model_profiles WHERE id=? AND user_id=?",(ident,uid)).fetchone():
            raise AuthError("NOT_FOUND","模型配置不存在",404)
        c.execute("UPDATE user_settings SET default_profile_id=NULL WHERE user_id=? AND default_profile_id=?",(uid,ident))
        c.execute("UPDATE model_profile_versions SET credential_id=NULL WHERE profile_id=?",(ident,))
        c.execute("DELETE FROM model_profiles WHERE id=? AND user_id=?",(ident,uid))


# 固定任务提交时的模型版本与授权范围，避免后续设置变化改写任务。
def snapshot(store,uid,ident=None):
    if ident is None:
        row=store.one("SELECT default_profile_id FROM user_settings WHERE user_id=?",(uid,))
        ident=row["default_profile_id"] if row else None
    if not ident:
        return None
    row=store.one("SELECT p.*,v.* FROM model_profiles p JOIN model_profile_versions v ON v.profile_id=p.id AND v.version=p.current_version WHERE p.id=? AND p.user_id=?",(ident,uid))
    if not row:
        raise AuthError("NOT_FOUND","模型配置不存在",404)
    cap=store.one('SELECT context_window,output_limit FROM model_context_limits WHERE profile_id=? AND version=?',(ident,row['version']))
    return {**{k:row[k] for k in ("profile_id","version","provider","region","base_url","model","credential_id","allow_summary","allow_trades")},**(cap or {'context_window':16384,'output_limit':4096})}


# 发送前重新核对账号、配置、授权和凭据，及时阻止已撤销权限。
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


# 按字段白名单构造新的模型输入，只包含已授权的研究和模拟事实。
def project_input(facts,fixed):
    """Build a new allowlisted value; never serialize the original private facts object."""
    # 从单条事实中复制允许的字段，避免私密信息随原始对象序列化。
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


# 固定经过校验的公网 IP 建立连接，同时保留原主机的 TLS 证书校验。
class PinnedHTTPS(http.client.HTTPSConnection):
    # 记录原始 HTTPS 主机与已核验 IP，供实际连接时使用。
    def __init__(self,host,port,ip,timeout):
        super().__init__(host,port,timeout=timeout,context=ssl.create_default_context())
        self.checked_ip=ip

    # 直接连接固定 IP，并以原主机名完成 TLS 握手。
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


# 仅调用站点配置的服务器 Ollama，串行推理并记录发送阶段。
def request_ollama(store,uid,payload,before_send):
    p=urlsplit(store.cfg.ollama_url);sent=False
    cls=http.client.HTTPSConnection if p.scheme=="https" else http.client.HTTPConnection
    conn=cls(p.hostname,p.port,timeout=min(20,store.cfg.model_timeout));deadline=None
    try:
        conn.connect();conn.sock.settimeout(5)
        store.connection_for_job(before_send.job_id,conn.sock)
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
        store.forget_connection(before_send.job_id)
        if deadline:
            deadline.cancel()
        conn.close()


# 为整次请求设置截止时间，到期主动关闭连接以终止阻塞。
def request_deadline(sock,seconds):
    # 截止时间到达时关闭底层套接字，促使读取或连接操作退出。
    def abort():
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
    timer=threading.Timer(seconds,abort);timer.daemon=True;timer.start()
    return timer


# 按供应商协议发送请求，限制响应并将连接、限流和超时归为稳定错误。
def request_cloud(store,uid,fixed,body,before_send,key_loader):
    url=cloud_address(fixed["base_url"],resolve=False)
    p=urlsplit(url); port=p.port or 443
    ips=public_ips(p.hostname,port)
    conn=PinnedHTTPS(p.hostname,port,ips[0],min(store.cfg.model_timeout,30))
    sent=False;deadline=None
    try:
        conn.connect()  # 连接固定 IP 的同时，仍按原主机名验证 TLS 证书。
        store.connection_for_job(before_send.job_id,conn.sock)
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
        if status in {400,413}:
            try:
                detail=json.loads(raw).get('error',{});code=str(detail.get('code') or detail.get('type') or '') if isinstance(detail,dict) else ''
            except (ValueError,AttributeError):code=''
            if code in {'context_length_exceeded','max_tokens_exceeded','input_too_long'} or status==413:raise ModelError('CONTEXT_TOO_LARGE')
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
        store.forget_connection(before_send.job_id)
        if deadline:
            deadline.cancel()
        conn.close()


# 核对授权、预留用量并记录调用阶段，验证输出后返回脱敏结果。
def call_model(store,keys,job,facts,attempt=1,transport=None,protocol=None):
    uid=job["user_id"]; fixed=json.loads(job["snapshot_json"]).get("profile")
    check_live(store,uid,fixed)
    projected=project_input(facts,fixed) if protocol is None else None
    from .research_control import control,limits
    usage_id=job['id']+':'+str(attempt)
    ident=uuid.uuid4().hex
    sync=getattr(store,'sync_service',None);shared=False
    store.execute("INSERT INTO model_calls(id,user_id,job_id,profile_id,profile_version,attempt,phase,created_at) VALUES(?,?,?,?,?,?,?,?)",(ident,uid,job["id"],fixed["profile_id"],fixed["version"],attempt,"prepared",stamp()))
    # 在真正发送前再次核对权限，并持久化已发送状态以防不确定重试。
    def before_send():
        nonlocal shared
        check_live(store,uid,fixed)
        if job.get("automatic"):
            from .preferences import get_preferences
            if not get_preferences(store,uid).automatic_ai:
                raise ModelError("AUTO_AI_DISABLED")
            if sync:
                from backend.sync.protocol import SyncError
                try:sync.ensure_executor(uid)
                except SyncError:raise ModelError('USAGE_UNAVAILABLE') from None
        if not store.one("SELECT id FROM personal_jobs WHERE id=? AND user_id=? AND status='running'",(job["id"],uid)):
            raise ModelError("CANCELLED")
        try:
            ctl=control(store,keys)
            if sync:
                from backend.sync.protocol import SyncError
                try:shared=sync.shared_reserve(uid,usage_id,limits(store,uid),job['kind'])
                except SyncError as exc:raise ModelError('USAGE_LIMIT' if exc.code=='QUOTA' else 'USAGE_UNAVAILABLE') from None
            ctl.reserve(uid,usage_id,fixed['profile_id'],job['kind'],limits(store,uid))
            ctl.transition(usage_id,'sent')
        except AuthError as exc:
            raise ModelError(exc.code) from None
        except (RuntimeError,OSError,sqlite3.Error):
            raise ModelError('USAGE_UNAVAILABLE') from None
        store.execute("UPDATE model_calls SET phase='sent' WHERE id=?",(ident,))
        if shared:sync.shared_result(uid,usage_id,'sent')
    before_send.job_id=job['id']
    secrets_seen=set()
    # 按站点、用户和凭据标识解密 Key，限制明文仅在调用阶段使用。
    def decrypt(credential):
        try:
            value=keys.open(credential["envelope_json"],site=store.site_id,user=uid,resource=credential["id"],purpose="model-key")
            secrets_seen.add(value)
            return value
        except Exception:
            raise ModelError("KEY_REVOKED") from None
    schema=protocol['schema'] if protocol else ResearchText.model_json_schema()
    if protocol:
        messages=protocol['messages']
    else:
        prompt=(ROOT/"config"/("prompt-full.md" if facts["mode"]=="full" else "prompt-standard.md")).read_text(encoding="utf-8")
        prompt+="\n仅返回给定结构JSON；结构："+json_text(schema)
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
                payload={"model":fixed["model"],"stream":False,"think":False,"format":schema,"messages":messages,"options":{"temperature":0.1,"num_ctx":fixed.get('context_window',16384),"num_predict":fixed.get('output_limit',4096) if protocol else 3000}}
                content,usage,request_id=request_ollama(store,uid,payload,before_send)
            finally:
                OLLAMA_LOCK.release()
        else:
            body={"model":fixed["model"],"messages":messages,"stream":False}
            if fixed["provider"]=="minimax":
                body.update(max_completion_tokens=fixed.get('output_limit',4096) if protocol else 8192,reasoning_split=True,reasoning_effort="low")
            else:
                body.update(max_tokens=fixed.get('output_limit',4096) if protocol else 4096,temperature=0.1)
            content,usage,request_id=request_cloud(store,uid,fixed,body,before_send,decrypt)
        # 只读取正式回答内容，单独返回的推理字段不进入研究归档。
        content=re.sub(r"^\s*<think>.*?</think>\s*","",str(content),flags=re.S)
        try:
            if protocol:
                result=protocol['validate'](content)
            else:
                text=ResearchText.model_validate_json(content);validate_output(text,projected);result=text.model_dump()
            if any(secret in json_text(result) for secret in secrets_seen):
                raise ValueError("凭据回显被拒绝")
        except (ValueError,TypeError):
            raise ModelError("INVALID_OUTPUT") from None
        if any(secret in str(request_id) for secret in secrets_seen):
            request_id=""
        store.execute("UPDATE model_calls SET phase='received',usage_json=?,provider_request_id=?,finished_at=? WHERE id=?",(json_text(usage),request_id,stamp(),ident))
        control(store,keys).transition(usage_id,'received',usage)
        if shared:sync.shared_result(uid,usage_id,'received',usage)
        return result,{"provider":fixed["provider"],"model":fixed["model"],"profile_id":fixed["profile_id"],"profile_version":fixed["version"],"usage":usage}
    except ModelError as exc:
        if shared:
            phase=store.one('SELECT phase FROM model_calls WHERE id=?',(ident,))['phase']
            sync.shared_result(uid,usage_id,'released' if phase=='prepared' else 'uncertain' if exc.uncertain else 'failed')
        try:control(store,keys).transition(usage_id,'uncertain' if exc.uncertain else 'failed')
        except (RuntimeError,OSError,__import__('sqlite3').Error):pass
        store.execute("UPDATE model_calls SET phase=?,error_code=?,finished_at=? WHERE id=?",("uncertain" if exc.uncertain else "failed",exc.code,stamp(),ident))
        raise
    except Exception:
        if shared:sync.shared_result(uid,usage_id,'uncertain')
        try:control(store,keys).transition(usage_id,'uncertain')
        except (RuntimeError,OSError,__import__('sqlite3').Error):pass
        store.execute("UPDATE model_calls SET phase='failed',error_code='INVALID_OUTPUT',finished_at=? WHERE id=?",(stamp(),ident))
        raise ModelError("INVALID_OUTPUT") from None
