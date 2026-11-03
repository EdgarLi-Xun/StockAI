# 评估个人证券提醒，保存边沿触发事件并控制免打扰、冷却和通知投递。
from __future__ import annotations

import json
import smtplib
import ssl
import uuid
from datetime import datetime,timedelta
from email.message import EmailMessage
from typing import Literal

from pydantic import BaseModel,ConfigDict,Field,model_validator
from backend.config import now,TZ
from backend.collectors import fresh_reasons
from backend.budget import budget_check
from . import market,resources,workspace
from .auth import AuthError
from .preferences import get_preferences
from .research import fingerprint,RULE_VERSION
from .store import json_text,stamp


# 约束提醒条件、阈值、渠道、免打扰时段和冷却时间。
class AlertInput(BaseModel):
    model_config=ConfigDict(extra='forbid',allow_inf_nan=False)
    name:str=Field(min_length=1,max_length=100)
    instrument_id:str
    condition:Literal['price_above','price_below','change_above','change_below','risk_changed','candidate_invalid']
    threshold:float|None=None
    report_id:str|None=None
    cooldown_minutes:int=Field(60,ge=0,le=525600,strict=True)
    expires_at:datetime|None=Field(default_factory=lambda:now()+timedelta(days=30))
    dnd_start:str|None=None
    dnd_end:str|None=None
    email:bool=False
    enabled:bool=True

    # 按提醒类型核验阈值、时段及其他互相关联的参数。
    @model_validator(mode='after')
    def valid(self):
        if self.condition.startswith(('price_','change_')) and self.threshold is None:raise ValueError('需要条件阈值')
        if self.condition.startswith('price_') and self.threshold<=0:raise ValueError('价格阈值必须为正')
        if self.condition.startswith('change_') and self.threshold< -100:raise ValueError('涨跌幅不能低于负百分之百')
        if bool(self.dnd_start)!=bool(self.dnd_end):raise ValueError('请同时设置免打扰起止时间')
        if self.dnd_start:
            from backend.config import Settings
            Settings.check_time(self.dnd_start);Settings.check_time(self.dnd_end)
            if self.dnd_start==self.dnd_end:raise ValueError('免打扰起止时间不能相同')
        if self.expires_at and (self.expires_at.tzinfo is None or (self.enabled and self.expires_at<=now())):raise ValueError('有效期必须是有时区的未来时刻')
        if self.condition=='candidate_invalid' and not self.report_id:raise ValueError('候选失效提醒需要指定本人报告')
        return self


# 核验证券在本人自选中，并补充经验证的提醒条件与依赖信息。
def prepare(store,uid,value,previous=None):
    item=market.instrument(store,value.instrument_id)
    if not store.one('SELECT 1 FROM user_watchlist WHERE user_id=? AND instrument_id=?',(uid,item['id'])) and not (previous and not value.enabled):raise ValueError('请先将证券加入个人自选')
    data=value.model_dump(mode='json');data['input_fingerprint']=fingerprint(data);data['currency']='USD' if item['market'] in {'US','FX'} else 'HKD' if item['market']=='HK' else 'CNY';data['unit']='价格点' if item['asset_class']=='index' else '元/股或份' if data['currency']=='CNY' else '报价单位';data['rule_version']=RULE_VERSION
    if value.report_id:
        report=workspace.owned(store,'personal_reports',uid,value.report_id);payload=json.loads(report['payload_json'])
        if not any(c['instrument_id']==item['id'] for c in payload.get('candidates',[])):raise ValueError('所选报告没有该证券候选')
        data['report_version']=report['version']
    bars=market.bars_for(store,item['id'],limit=1);last=bars[-1] if bars else None
    if value.condition.startswith('change_'):
        if previous and previous.get('instrument_id')==item['id'] and previous.get('condition')==value.condition and previous.get('reference'):
            data['reference']=previous['reference']
        else:
            if not last or fresh_reasons(last,market.calendar(store)):raise ValueError('涨跌幅参考价需要有效的已确认交易日收盘价')
            data['reference']={k:last[k] for k in ('close','ts','source','source_url','adjustment','fetched_at')}
    return data


# 在用户锁内保存提醒版本，并同步处理对应的评估状态。
def save(store,uid,value,ident=None,version=None,client_key=None,request_fingerprint=None):
    request_fingerprint=request_fingerprint or fingerprint(value.model_dump(mode='json',exclude_unset=True))
    with store.user_lock(uid):
        if not ident:
            previous=resources.replay(store,uid,'alert',client_key,request_fingerprint)
            if previous:return previous
        previous=resources.get(store,uid,'alert',ident)['data'] if ident else None
        data=prepare(store,uid,value,previous)
        if not ident:data['creation_request_fingerprint']=request_fingerprint
        result=resources.save(store,uid,'alert',data,ident=ident,version=version,status='active' if value.enabled else 'disabled',client_key=client_key)
        if ident:
            with store.tx(immediate=True) as c:
                c.execute("UPDATE alert_events SET state='cancelled' WHERE user_id=? AND rule_id=? AND rule_version!=? AND state='pending_dnd'",(uid,ident,result['version']))
        return result


# 按提醒设置的起止时间判断免打扰窗口，支持跨午夜时段。
def in_dnd(rule,at):
    start,end=rule.get('dnd_start'),rule.get('dnd_end')
    if not start or not end:return False
    t=at.astimezone(TZ).strftime('%H:%M')
    return start<=t<end if start<end else t>=start or t<end


def expire_rules(store,uid,at=None):
    at=(at or now()).isoformat()
    rows=store.query("SELECT id,version FROM research_objects WHERE user_id=? AND kind='alert' AND status='active' AND julianday(json_extract(payload_json,'$.expires_at'))<=julianday(?)",(uid,at))
    if not rows:return 0
    changed=0
    with store.user_lock(uid),store.tx(immediate=True) as c:
        for row in rows:
            count=c.execute("UPDATE research_objects SET status='expired',updated_at=? WHERE id=? AND user_id=? AND version=? AND status='active' AND julianday(json_extract(payload_json,'$.expires_at'))<=julianday(?)",(stamp(),row['id'],uid,row['version'],at)).rowcount
            if count:
                c.execute("UPDATE alert_events SET state='cancelled' WHERE rule_id=? AND user_id=? AND state IN('pending','pending_dnd')",(row['id'],uid));changed+=count
    return changed


# 从带来源的行情与风险事实计算条件，证据不足时保留未知状态。
def sample(store,uid,data,at=None):
    at=at or now();item=market.instrument(store,data['instrument_id']);prefs=get_preferences(store,uid);kind=data['condition']
    result={'instrument_id':item['id'],'name':item['name'],'condition':kind,'threshold':data.get('threshold'),'currency':data['currency'],'unit':data['unit'],'sampled_at':at.isoformat(),'rule_version':RULE_VERSION,'satisfied':None,'quality':'unknown'}
    if kind=='candidate_invalid':result.update(report_id=data['report_id'],report_version=data.get('report_version'))
    if kind in {'risk_changed','candidate_invalid'}:
        reasons=market.risk_reasons(item,prefs,at)
        if kind=='risk_changed':
            result.update(quality='valid',satisfied=False,risk_fingerprint=fingerprint([item['status'],item['risk_flag'],reasons]),reasons=reasons,source_url=item.get('source_url'),verified_at=item['verified_at'])
            return result
        row=store.one('SELECT payload_json FROM personal_reports WHERE id=? AND user_id=?',(data.get('report_id'),uid))
        if not row:return {**result,'reason':'SOURCE_DELETED'}
        candidates=json.loads(row['payload_json']).get('candidates',[]);candidate=next((r for r in candidates if r['instrument_id']==item['id']),None)
        if not candidate:return {**result,'reason':'CANDIDATE_MISSING'}
        bars=market.bars_for(store,item['id'],limit=1);bar=bars[-1] if bars else None;reasons+=fresh_reasons(bar,market.calendar(store,at))
        if bar:
            try:
                if not budget_check(bar['close'],item['rules'],item['asset_class'],prefs.fees,prefs.budget,candidate['quantity'])['within_budget']:reasons.append('原候选数量按当前费用已超预算')
            except ValueError:reasons.append('当前产品规则或价格口径已变化')
        return {**result,'quality':'valid','satisfied':bool(reasons),'reasons':reasons,'report_id':data['report_id'],'report_version':data.get('report_version'),'quote_time':bar['ts'] if bar else None,'source_url':bar['source_url'] if bar else item.get('source_url')}
    bars=market.bars_for(store,item['id'],limit=1);bar=bars[-1] if bars else None
    if item['market'] not in {'SH','SZ'}:return {**result,'reason':'MARKET_CALENDAR_UNVERIFIED'}
    reasons=fresh_reasons(bar,market.calendar(store,at))
    if reasons:return {**result,'reasons':reasons,'reason':'QUOTE_UNVERIFIED'}
    number=bar['close']
    if kind.startswith('change_'):
        ref=data.get('reference')
        if not ref or ref['adjustment']!=bar['adjustment'] or not bar['metadata'].get('corporate_actions_verified'):
            return {**result,'reason':'REFERENCE_NOT_COMPARABLE'}
        number=(bar['close']/ref['close']-1)*100
    satisfied=number>=data['threshold'] if kind.endswith('above') else number<=data['threshold']
    return {**result,'quality':'valid','satisfied':satisfied,'value':number,'quote_time':bar['ts'],'collected_at':bar['fetched_at'],'source_url':bar['source_url'],'source':bar['source'],'reference':data.get('reference')}


# 复核提醒归属、版本、启用状态及账号状态，避免旧版本继续触发。
def live(store,uid,ident,version,at):
    row=store.one("SELECT * FROM research_objects WHERE id=? AND user_id=? AND kind='alert'",(ident,uid))
    if not row or row['version']!=version or row['status']!='active' or not store.active(uid):return None
    data=json.loads(row['payload_json'])
    if data.get('expires_at') and datetime.fromisoformat(data['expires_at'])<=at:return None
    return data


# 比较当前条件与前次有效状态，只在允许的变化及冷却窗口生成幂等事件。
def evaluate(store,uid,at=None):
    if getattr(store,'sync_service',None):store.sync_service.ensure_executor(uid)
    with store.user_lock(uid):return _evaluate(store,uid,at)


# 在同一用户锁内逐条评估提醒边沿，保存状态并生成或归并待投递事件。
def _evaluate(store,uid,at=None):
    at=at or now();created=0
    if not store.active(uid):return 0
    expire_rules(store,uid,at)
    for row in store.query("SELECT * FROM research_objects WHERE user_id=? AND kind='alert' AND status='active'",(uid,)):
        data=json.loads(row['payload_json'])
        if data.get('expires_at') and datetime.fromisoformat(data['expires_at'])<=at:
            store.execute("UPDATE alert_events SET state='cancelled' WHERE user_id=? AND rule_id=? AND state='pending_dnd'",(uid,row['id']));continue
        value=sample(store,uid,data,at);old=store.one('SELECT * FROM alert_state WHERE user_id=? AND rule_id=?',(uid,row['id']))
        if old and old['rule_version']!=row['version']:old=None
        new_state=old['last_valid'] if old else None;new_fp=old['last_fingerprint'] if old else None;last_trigger=old['last_trigger'] if old else None;edge=False
        if value['quality']=='valid':
            if data['condition']=='risk_changed':
                edge=bool(new_fp and new_fp!=value['risk_fingerprint']);new_fp=value['risk_fingerprint'];new_state=int(edge)
            else:
                edge=bool(value['satisfied'] and new_state!=1);new_state=int(value['satisfied'])
        state='pending_dnd' if in_dnd(data,at) else 'pending'
        if edge and last_trigger and (at-datetime.fromisoformat(last_trigger)).total_seconds()<data['cooldown_minutes']*60:state='cooldown'
        event_key=fingerprint([row['id'],row['version'],old['last_evaluated'] if old else 'first',value.get('quote_time'),value.get('risk_fingerprint'),last_trigger]) if edge else None
        sync=getattr(store,'sync_service',None);receipt_key=None
        if edge and sync:
            binding=sync.binding(uid)
            if binding:
                from backend.sync.business import Adapter
                # 先保存稳定的同步标识，再在业务写事务外协调远端回执。
                with store.tx(immediate=True) as c:
                    if not store.active(uid,c=c) or not c.execute("SELECT 1 FROM research_objects WHERE id=? AND user_id=? AND version=? AND status='active'",(row['id'],uid,row['version'])).fetchone():continue
                    gid=Adapter(store,uid,binding['space_id'],binding['device_id']).gid('research_objects',row['id'],c)
                receipt_key=fingerprint([gid,row['version'],new_state,new_fp,value.get('quote_time'),value.get('risk_fingerprint')])
                if not sync.automatic(uid,'alert-event',receipt_key):continue
        with store.user_lock(uid),store.tx(immediate=True) as c:
            if not store.active(uid,c=c) or not c.execute("SELECT 1 FROM research_objects WHERE id=? AND user_id=? AND version=? AND status='active'",(row['id'],uid,row['version'])).fetchone():continue
            if edge:
                count=c.execute('INSERT OR IGNORE INTO alert_events VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,uid,row['id'],row['version'],event_key,at.isoformat(),state,json_text(value))).rowcount;created+=count
                if state!='cooldown' and count:last_trigger=at.isoformat()
            c.execute('INSERT INTO alert_state VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(rule_id) DO UPDATE SET rule_version=excluded.rule_version,last_valid=excluded.last_valid,last_fingerprint=excluded.last_fingerprint,last_trigger=excluded.last_trigger,last_evaluated=excluded.last_evaluated,quality=excluded.quality,details_json=excluded.details_json',(row['id'],uid,row['version'],new_state,new_fp,last_trigger,at.isoformat(),value['quality'],json_text(value)))
        if edge and receipt_key:sync.automatic(uid,'alert-event',receipt_key,complete='recorded')
    deliver_pending(store,uid,at)
    return created


# 把事件事实转换为含触发条件和来源信息的通知文本。
def event_text(event):
    v=json.loads(event['facts_json']);label={'price_above':'价格达到上阈值','price_below':'价格达到下阈值','change_above':'涨幅达到阈值','change_below':'跌幅达到阈值','risk_changed':'风险状态发生变化','candidate_invalid':'报告候选已失效'}[v['condition']]
    numeric=f"；当前 {v.get('value')} / 阈值 {v.get('threshold')} {v.get('currency','')} {v.get('unit','')}" if v.get('value') is not None else ''
    report=f"；来源报告 {v['report_id']} 版本 {v.get('report_version','未知')}" if v.get('report_id') else ''
    return f"{v['name']} {v['instrument_id']}：{label}{numeric}{report}；触发 {event['triggered_at']}；报价 {v.get('quote_time') or '不适用'}；来源 {v.get('source_url') or '资料缺失'}"+('；'+ '；'.join(v['reasons']) if v.get('reasons') else '')


# 筛选可投递事件并按渠道归并，记录投递状态以防重复通知。
def deliver_pending(store,uid,at=None):
    at=at or now();eligible=[]
    for event in store.query("SELECT * FROM alert_events WHERE user_id=? AND state IN('pending','pending_dnd') ORDER BY triggered_at",(uid,)):
        data=live(store,uid,event['rule_id'],event['rule_version'],at)
        if not data:
            store.execute("UPDATE alert_events SET state='cancelled' WHERE id=? AND user_id=?",(event['id'],uid));continue
        if in_dnd(data,at):continue
        if event['state']=='pending_dnd':
            current=sample(store,uid,data,at)
            previous=json.loads(event['facts_json'])
            if current['quality']!='valid' or (data['condition']!='risk_changed' and not current['satisfied']) or (data['condition']=='risk_changed' and current.get('risk_fingerprint')!=previous.get('risk_fingerprint')):
                store.execute("UPDATE alert_events SET state='resolved' WHERE id=? AND user_id=?",(event['id'],uid));continue
        eligible.append((event,data))
    if not eligible:return
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):return
        valid=[(e,d) for e,d in eligible if c.execute("SELECT 1 FROM research_objects r JOIN alert_events e ON e.rule_id=r.id AND e.user_id=r.user_id WHERE r.id=? AND r.user_id=? AND r.version=? AND r.status='active' AND e.id=? AND e.state IN('pending','pending_dnd')",(e['rule_id'],uid,e['rule_version'],e['id'])).fetchone()]
        if not valid:return
        events=[e for e,d in valid];ids=[e['id'] for e in events];ident=uuid.uuid4().hex;text='\n'.join(event_text(e) for e in events)
        c.execute('INSERT INTO personal_notifications(user_id,ts,level,message) VALUES(?,?,?,?)',(uid,stamp(),'warning',text))
        c.execute("INSERT INTO alert_deliveries(id,user_id,event_ids_json,channel,state,next_at,created_at,delivered_at) VALUES(?,?,?,'inapp','delivered',?,?,?)",(ident,uid,json_text(ids),stamp(),stamp(),stamp()))
        for e,d in valid:
            c.execute('INSERT OR IGNORE INTO alert_delivery_events VALUES(?,?,?)',(e['id'],'inapp',ident));c.execute("UPDATE alert_events SET state='delivered' WHERE id=? AND user_id=?",(e['id'],uid))
        settings=store.one("SELECT payload_json FROM research_objects WHERE user_id=? AND kind='notification_settings'",(uid,));email_enabled=bool(settings and json.loads(settings['payload_json']).get('email_enabled'))
        email_events=[e for e,d in valid if d['email']]
        if email_events:
            mid=uuid.uuid4().hex;mail_state='queued' if email_enabled and store.cfg.smtp_host and store.cfg.smtp_from else 'not_configured' if email_enabled else 'not_enabled'
            c.execute("INSERT INTO alert_deliveries(id,user_id,event_ids_json,channel,state,next_at,created_at) VALUES(?,?,?,'email',?,?,?)",(mid,uid,json_text([e['id'] for e in email_events]),mail_state,stamp(),stamp()))
            for e in email_events:c.execute('INSERT OR IGNORE INTO alert_delivery_events VALUES(?,?,?)',(e['id'],'email',mid))


# 在记录发送意图后发送提醒邮件，区分可重试故障与结果不确定状态。
def send_mail(store,keys,sender=None):
    sync=getattr(store,'sync_service',None)
    with store.tx(immediate=True) as c:
        if c.execute("SELECT value FROM cloud_meta WHERE key='maintenance'").fetchone()[0]!='0':return False
        r=c.execute("SELECT * FROM alert_deliveries WHERE channel='email' AND state IN('queued','retry') AND next_at<=? ORDER BY created_at LIMIT 1",(stamp(),)).fetchone()
        if not r:return False
        r=dict(r);events=[];at=now()
        settings=c.execute("SELECT payload_json FROM research_objects WHERE user_id=? AND kind='notification_settings'",(r['user_id'],)).fetchone()
        if not store.active(r['user_id'],c=c) or not settings or not json.loads(settings[0]).get('email_enabled'):
            c.execute("UPDATE alert_deliveries SET state='cancelled' WHERE id=?",(r['id'],));return True
        for ident in json.loads(r['event_ids_json']):
            event=store.one('SELECT * FROM alert_events WHERE id=? AND user_id=?',(ident,r['user_id']))
            if event and live(store,r['user_id'],event['rule_id'],event['rule_version'],at):events.append(event)
        if not events:c.execute("UPDATE alert_deliveries SET state='cancelled' WHERE id=?",(r['id'],));return True
        user=c.execute('SELECT email FROM users WHERE id=?',(r['user_id'],)).fetchone()
        if sync:
            from backend.sync.protocol import digest
            payload=json.loads(r['payload_json'] or '{}')
            receipt_key=payload.setdefault('sync_receipt_key',digest(sorted(e['event_key'] for e in events)))
            r['payload_json']=json_text(payload)
        c.execute("UPDATE alert_deliveries SET state='sending',attempts=attempts+1,payload_json=? WHERE id=?",(r['payload_json'],r['id']))
    message=EmailMessage();message['Subject']='StockAI 个人条件提醒';message['To']=user['email'];message['From']=store.cfg.smtp_from or 'no-reply@stockai.test';message.set_content('\n\n'.join(event_text(e) for e in events))
    sent=False;accepted=False
    # 真正发送前复核账号、邮件开关和每个事件对应的提醒版本仍然有效。
    def still_enabled():
        settings=store.one("SELECT payload_json FROM research_objects WHERE user_id=? AND kind='notification_settings'",(r['user_id'],))
        if not (store.active(r['user_id']) and settings and json.loads(settings['payload_json']).get('email_enabled') and all(live(store,r['user_id'],e['rule_id'],e['rule_version'],now()) for e in events)):return False
        if sync:
            sync.ensure_executor(r['user_id'])
            retry=r['attempts'] if r['state']=='retry' and r['error_code']=='SMTP_REJECTED' else None
            if not sync.automatic(r['user_id'],'mail',receipt_key,delivery=r['id'],retry=retry):return False
        return True
    try:
        if sender:
            if not still_enabled():store.execute("UPDATE alert_deliveries SET state='cancelled' WHERE id=?",(r['id'],));return True
            sent=True;sender(message);accepted=True
        else:
            cls=smtplib.SMTP if store.cfg.smtp_starttls else smtplib.SMTP_SSL
            kwargs={'timeout':10}
            if not store.cfg.smtp_starttls:kwargs['context']=ssl.create_default_context()
            with cls(store.cfg.smtp_host,store.cfg.smtp_port,**kwargs) as server:
                if store.cfg.smtp_starttls:server.starttls(context=ssl.create_default_context())
                if store.cfg.smtp_user:server.login(store.cfg.smtp_user,store.cfg.smtp_password)
                if not still_enabled():store.execute("UPDATE alert_deliveries SET state='cancelled' WHERE id=?",(r['id'],));return True
                sent=True;refused=server.send_message(message)
                if refused:raise smtplib.SMTPRecipientsRefused(refused)
                accepted=True
    except (smtplib.SMTPResponseException,smtplib.SMTPRecipientsRefused) as exc:
        if not accepted:
            codes=[v[0] for v in exc.recipients.values()] if isinstance(exc,smtplib.SMTPRecipientsRefused) else [exc.smtp_code]
            state='retry' if codes and all(400<=code<500 for code in codes) and r['attempts']<1 else 'failed'
            store.execute("UPDATE alert_deliveries SET state=?,next_at=?,error_code='SMTP_REJECTED' WHERE id=? AND state='sending'",(state,(now()+timedelta(minutes=2)).isoformat(),r['id']))
            return True
    except Exception:
        if not accepted:
            state='uncertain' if sent else 'retry' if r['attempts']<1 else 'failed'
            store.execute("UPDATE alert_deliveries SET state=?,next_at=?,error_code=? WHERE id=? AND state='sending'",(state,(now()+timedelta(minutes=2)).isoformat(),'DELIVERY_UNCERTAIN' if sent else 'SMTP_CONNECT_FAILED',r['id']))
            return True
    # DATA 已明确接受后，QUIT 或连接退出失败不代表邮件被拒绝，不能开放重发。
    store.execute("UPDATE alert_deliveries SET state='delivered',delivered_at=?,error_code=NULL WHERE id=? AND state='sending'",(stamp(),r['id']))
    if sync:sync.automatic(r['user_id'],'mail',receipt_key,complete='delivered',delivery=r['id'])
    return True
