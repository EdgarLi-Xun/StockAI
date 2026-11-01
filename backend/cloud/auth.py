from __future__ import annotations

import hmac
import json
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from datetime import datetime,timedelta,timezone

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError,VerificationError,InvalidHashError

from .crypto import digest
from .preferences import Preferences
from .store import stamp,json_text

HASHER=PasswordHasher(time_cost=2,memory_cost=65536,parallelism=2)


class AuthError(ValueError):
    def __init__(self,code,message,status=400):
        self.code,self.message,self.status=code,message,status
        super().__init__(message)


def later(seconds):
    return (datetime.now(timezone.utc)+timedelta(seconds=seconds)).isoformat(timespec="microseconds")


def email_value(value):
    value=str(value).strip().lower()
    if len(value)>254 or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,}",value):
        raise AuthError("INVALID_EMAIL","请输入有效邮箱")
    return value


def hash_password(value):
    if not isinstance(value,str) or not 12<=len(value)<=200:
        raise AuthError("WEAK_PASSWORD","密码须为12至200个字符")
    return HASHER.hash(value)


def password_matches(encoded,password):
    try:
        return HASHER.verify(encoded,password)
    except (VerifyMismatchError,VerificationError,InvalidHashError):
        return False


@dataclass(frozen=True)
class Actor:
    id:str
    email:str
    role:str
    status:str
    verified_at:str|None
    session_hash:str=""

    def public(self):
        return {"id":self.id,"email":self.email,"role":self.role,"status":self.status,"verified":bool(self.verified_at)}


class Auth:
    def __init__(self,store,keys):
        self.store,self.keys,self.cfg=store,keys,store.cfg
        self._dummy_hash=HASHER.hash(secrets.token_urlsafe(32))

    def limit(self,purpose,identity,maximum,seconds=300):
        key=digest(purpose+":"+identity)
        at=time.time()
        with self.store.tx(immediate=True) as c:
            row=c.execute("SELECT * FROM rate_limits WHERE key=?",(key,)).fetchone()
            if not row or at-row["window_start"]>=seconds:
                c.execute("INSERT INTO rate_limits VALUES(?,?,1) ON CONFLICT(key) DO UPDATE SET window_start=excluded.window_start,hits=1",(key,at))
            elif row["hits"]>=maximum:
                raise AuthError("RATE_LIMIT","请求过于频繁，请稍后再试",429)
            else:
                c.execute("UPDATE rate_limits SET hits=hits+1 WHERE key=?",(key,))

    def new_user(self,email,password,role="user",verified=False,c=None):
        uid=uuid.uuid4().hex
        ts=stamp()
        args=(uid,email_value(email),hash_password(password),role,"active" if verified else "pending",ts if verified else None,ts)
        def insert(conn):
            conn.execute("INSERT INTO users(id,email,password_hash,role,status,verified_at,created_at) VALUES(?,?,?,?,?,?,?)",args)
            conn.execute("INSERT INTO user_settings VALUES(?,?,NULL)",(uid,Preferences().model_dump_json()))
        if c is not None:
            insert(c)
        else:
            with self.store.tx(immediate=True) as conn:
                insert(conn)
        return uid

    def create_admin(self,email,password):
        encoded=hash_password(password)
        normalized=email_value(email)
        with self.store.tx(immediate=True) as c:
            if c.execute("SELECT id FROM users WHERE role='admin'").fetchone():
                raise AuthError("ALREADY_INITIALIZED","已存在管理员，不自动覆盖身份")
            uid=uuid.uuid4().hex; ts=stamp()
            c.execute("INSERT INTO users(id,email,password_hash,role,status,verified_at,created_at) VALUES(?,?,?,?,?,?,?)",(uid,normalized,encoded,"admin","active",ts,ts))
            c.execute("INSERT INTO user_settings VALUES(?,?,NULL)",(uid,Preferences().model_dump_json()))
        return uid

    def invitation(self,admin):
        code=secrets.token_urlsafe(24)
        ident=uuid.uuid4().hex
        self.store.execute("INSERT INTO invitations(id,code_hash,created_by,created_at,expires_at) VALUES(?,?,?,?,?)",(ident,digest(code),admin,stamp(),later(7*86400)))
        self.store.audit(admin,"invitation_create",ident)
        return {"id":ident,"code":code,"expires_at":self.store.one("SELECT expires_at FROM invitations WHERE id=?",(ident,))["expires_at"]}

    def _mail_token(self,c,uid,purpose):
        ts=stamp(); token=secrets.token_urlsafe(40); ident=uuid.uuid4().hex
        ttl=86400 if purpose=="verify" else 1800
        c.execute("UPDATE auth_tokens SET consumed_at=? WHERE user_id=? AND purpose=? AND consumed_at IS NULL",(ts,uid,purpose))
        c.execute("UPDATE mail_outbox SET state='cancelled',payload_json=NULL WHERE user_id=? AND purpose=? AND state!='sent'",(uid,purpose))
        c.execute("INSERT INTO auth_tokens VALUES(?,?,?,?,?,NULL,?)",(ident,uid,purpose,digest(token),later(ttl),ts))
        out_id=uuid.uuid4().hex
        # Raw tokens only exist in an authenticated encrypted outbox until delivery.
        envelope=self.keys.seal(json_text({"token":token,"purpose":purpose}),site=self.store.site_id,user=uid,resource=out_id,purpose="mail")
        c.execute("INSERT INTO mail_outbox(id,user_id,purpose,payload_json,state,next_at,expires_at,created_at) VALUES(?,?,?,?,?,?,?,?)",(out_id,uid,purpose,envelope,"queued",ts,later(ttl),ts))

    def register(self,email,password,code,peer):
        self.limit("register-ip",peer,12,3600)
        normalized=email_value(email); encoded=hash_password(password)
        with self.store.tx(immediate=True) as c:
            invite=c.execute("SELECT * FROM invitations WHERE code_hash=?",(digest(str(code)),)).fetchone()
            if not invite or invite["revoked_at"] or invite["expires_at"]<=stamp() or invite["used_by"]:
                raise AuthError("INVALID_INVITATION","邀请码无效、过期、已撤销或已使用")
            if c.execute("SELECT id FROM users WHERE email=?",(normalized,)).fetchone():
                raise AuthError("REGISTRATION_UNAVAILABLE","无法完成注册，请检查输入或使用找回密码")
            uid=uuid.uuid4().hex; ts=stamp()
            c.execute("INSERT INTO users(id,email,password_hash,role,status,created_at) VALUES(?,?,?,?,?,?)",(uid,normalized,encoded,"user","pending",ts))
            c.execute("INSERT INTO user_settings VALUES(?,?,NULL)",(uid,Preferences().model_dump_json()))
            changed=c.execute("UPDATE invitations SET used_by=?,used_at=? WHERE id=? AND used_by IS NULL AND revoked_at IS NULL AND expires_at>?",(uid,ts,invite["id"],ts)).rowcount
            if not changed:
                raise AuthError("INVALID_INVITATION","邀请码已被使用")
            self._mail_token(c,uid,"verify")
        return {"accepted":True,"message":"注册已提交，请检查邮箱完成验证；如未收到，可重发验证邮件。"}

    def resend(self,email,peer,purpose="verify"):
        self.limit("mail-ip",peer,10,3600)
        normalized=email_value(email)
        self.limit("mail-email",normalized,3,900)
        with self.store.tx(immediate=True) as c:
            u=c.execute("SELECT * FROM users WHERE email=?",(normalized,)).fetchone()
            eligible=u and u["status"]!="disabled" and (purpose=="reset" or not u["verified_at"])
            if eligible:
                self._mail_token(c,u["id"],purpose)
        return {"accepted":True,"message":"如账号符合条件，邮件将发送至对应邮箱。"}

    def consume(self,token,purpose,password=None):
        encoded=hash_password(password) if purpose=="reset" else None
        with self.store.tx(immediate=True) as c:
            r=c.execute("SELECT t.*,u.status FROM auth_tokens t JOIN users u ON u.id=t.user_id WHERE token_hash=? AND purpose=?",(digest(str(token)),purpose)).fetchone()
            if not r or r["consumed_at"] or r["expires_at"]<=stamp() or r["status"]=="disabled":
                raise AuthError("INVALID_TOKEN","链接无效、已使用或已过期")
            c.execute("UPDATE auth_tokens SET consumed_at=? WHERE id=? AND consumed_at IS NULL",(stamp(),r["id"]))
            if purpose=="verify":
                c.execute("UPDATE users SET verified_at=?,status='active' WHERE id=?",(stamp(),r["user_id"]))
            else:
                c.execute("UPDATE users SET password_hash=? WHERE id=?",(encoded,r["user_id"]))
                c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=?",(stamp(),r["user_id"]))
            c.execute("UPDATE mail_outbox SET payload_json=NULL,state='cancelled' WHERE user_id=? AND purpose=? AND state!='sent'",(r["user_id"],purpose))
        return {"success":True}

    def login(self,email,password,peer):
        self.limit("login-ip",peer,20,300)
        normalized=email_value(email)
        self.limit("login-email",normalized,10,300)
        if not isinstance(password,str) or not 12<=len(password)<=200:
            raise AuthError("INVALID_LOGIN","邮箱、密码或账号状态不正确",401)
        row=self.store.one("SELECT * FROM users WHERE email=?",(normalized,))
        valid=password_matches(row["password_hash"] if row else self._dummy_hash,str(password))
        if not row or not valid or row["status"]=="disabled":
            raise AuthError("INVALID_LOGIN","邮箱、密码或账号状态不正确",401)
        rehashed=HASHER.hash(str(password)) if HASHER.check_needs_rehash(row["password_hash"]) else None
        token=secrets.token_urlsafe(48); csrf=secrets.token_urlsafe(32)
        with self.store.user_lock(row["id"]),self.store.tx(immediate=True) as c:
            latest=c.execute("SELECT * FROM users WHERE id=? AND status!='disabled' AND password_hash=?",(row["id"],row["password_hash"])).fetchone()
            if not latest:
                raise AuthError("INVALID_LOGIN","账号状态已改变，请重新登录",401)
            c.execute("INSERT INTO sessions VALUES(?,?,?,?,NULL,?)",(digest(token),row["id"],digest(csrf),later(8*3600),stamp()))
            if rehashed:
                c.execute("UPDATE users SET password_hash=? WHERE id=? AND password_hash=?",(rehashed,row["id"],row["password_hash"]))
        return token,csrf,self.actor(token)

    def actor(self,token):
        if not token:
            raise AuthError("LOGIN_REQUIRED","请先登录",401)
        row=self.store.one("SELECT u.*,s.token_hash FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at>? AND u.status!='disabled'",(digest(token),stamp()))
        if not row:
            raise AuthError("LOGIN_REQUIRED","登录已失效，请重新登录",401)
        return Actor(row["id"],row["email"],row["role"],row["status"],row["verified_at"],row["token_hash"])

    def csrf(self,session=None):
        value=secrets.token_urlsafe(32)
        if session:
            self.store.execute("UPDATE sessions SET csrf_hash=? WHERE token_hash=? AND revoked_at IS NULL",(digest(value),digest(session)))
        return value

    def reauth(self,actor,password,resource,purpose="backup_export"):
        self.limit("reauth",actor.id,6,300)
        row=self.store.one("SELECT password_hash FROM users WHERE id=? AND status='active'",(actor.id,))
        if not row or not password_matches(row["password_hash"],str(password)):
            raise AuthError("REAUTH_FAILED","密码验证未通过",401)
        token=secrets.token_urlsafe(40)
        self.store.execute("INSERT INTO reauth_grants VALUES(?,?,?,?,?,?,NULL)",(digest(token),actor.id,actor.session_hash,purpose,resource,later(300)))
        return token

    def consume_grant(self,actor,token,resource):
        with self.store.tx(immediate=True) as c:
            n=c.execute("UPDATE reauth_grants SET consumed_at=? WHERE token_hash=? AND user_id=? AND session_hash=? AND purpose='backup_export' AND resource_id=? AND expires_at>? AND consumed_at IS NULL",(stamp(),digest(token),actor.id,actor.session_hash,resource,stamp())).rowcount
            if not n:
                raise AuthError("REAUTH_REQUIRED","请再次验证管理员密码后导出",403)

    def disable(self,actor,target,disabled):
        if target==actor.id:
            raise AuthError("SELF_DISABLE","不能在此停用当前管理员")
        with self.store.user_lock(target),self.store.tx(immediate=True) as c:
            row=c.execute("SELECT id FROM users WHERE id=?",(target,)).fetchone()
            if not row:
                raise AuthError("NOT_FOUND","记录不存在",404)
            c.execute("UPDATE users SET status=CASE WHEN ? THEN 'disabled' WHEN verified_at IS NULL THEN 'pending' ELSE 'active' END WHERE id=?",(disabled,target))
            if disabled:
                c.execute("UPDATE sessions SET revoked_at=? WHERE user_id=?",(stamp(),target))
                c.execute("UPDATE personal_jobs SET status='cancelled',error_code='ACCOUNT_DISABLED',finished_at=? WHERE user_id=? AND status IN ('queued','running')",(stamp(),target))
        self.store.audit(actor.id,"user_disable" if disabled else "user_enable",target)
