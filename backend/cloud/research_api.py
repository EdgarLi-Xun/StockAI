# 注册个人研究就绪、提醒、判断日志、实验、用量及助手接口，并隔离共享资料维护权限。
from __future__ import annotations

import json
import uuid
from datetime import datetime

from fastapi import Depends,Request
from pydantic import BaseModel,ConfigDict,Field,StrictInt
from backend.instruments import official_url
from backend.config import now
from . import alerts,assistant,experiments,market,research,resources
from . import assistant_actions
from .auth import Actor,AuthError
from .smtp_settings import configured as mail_configured
from .research_control import summary,limits,control
from .store import json_text,stamp


# 约束日月调用上限为非负整数，并携带乐观并发版本。
class LimitsInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    daily:StrictInt|None=Field(None,ge=0)
    monthly:StrictInt|None=Field(None,ge=0)
    version:StrictInt=Field(0,ge=0)


def expected_version(payload):
    value=payload.get('version')
    if type(value) is not int or value<1:raise ValueError('更新需要当前记录的整数版本号')
    return value


def client_key(payload):
    value=payload.get('client_key')
    if value is None:return uuid.uuid4().hex
    if not isinstance(value,str) or not 8<=len(value)<=100:raise ValueError('请求标识须为8至100个字符')
    return value


# 复用主应用的运行时和身份依赖，注册研究工作台扩展路由。
def install(app,rt,user,admin):
    # 返回本人证券、报告、模型和模拟账户的简要选项，供研究表单选择上下文。
    @app.get('/api/me/research/options')
    def research_options(request:Request,a:Actor=Depends(user)):
        store=rt(request).store
        from .models import profile_rows
        return {'watchlist':store.query('SELECT i.id,i.name,i.symbol,i.market,i.asset_class FROM instruments i JOIN user_watchlist w ON w.instrument_id=i.id WHERE w.user_id=? ORDER BY i.id',(a.id,)),
                'reports':store.query('SELECT id,version,report_date,mode FROM personal_reports WHERE user_id=? ORDER BY generated_at DESC LIMIT 200',(a.id,)),
                'profiles':profile_rows(store,a.id),
                'accounts':store.query('SELECT id,name FROM sim_accounts WHERE user_id=? ORDER BY created_at DESC',(a.id,)),
                'backtests':store.query('SELECT id,created_at FROM personal_backtests WHERE user_id=? ORDER BY created_at DESC LIMIT 200',(a.id,))}

    # 返回本人自选的数据和证据就绪问题，支持任务、状态和分页筛选。
    @app.get('/api/me/readiness')
    def readiness(request:Request,task:str='',q:str='',status:str='',page:int=1,size:int=20,a:Actor=Depends(user)):
        return research.readiness(rt(request).store,a.id,task=task,q=q,status=status,page=page,size=size)

    # 返回本人调用用量、日月上限和当前设置版本。
    @app.get('/api/me/usage')
    def usage(request:Request,a:Actor=Depends(user)):
        store=rt(request).store;row=store.one("SELECT id,version FROM research_objects WHERE user_id=? AND kind='usage_settings'",(a.id,))
        return {**summary(store,a.id),'settings':{**limits(store,a.id),'version':row['version'] if row else 0}}

    # 按版本保存个人调用上限，避免并发修改互相覆盖。
    @app.put('/api/me/usage/limits')
    def save_limits(value:LimitsInput,request:Request,a:Actor=Depends(user)):
        store=rt(request).store;data=value.model_dump(exclude={'version'});row=store.one("SELECT id,version FROM research_objects WHERE user_id=? AND kind='usage_settings'",(a.id,))
        if not row and value.version!=0:raise AuthError('VERSION_CONFLICT','用量上限版本不匹配，请刷新',409)
        resources.save(store,a.id,'usage_settings',data,ident='usage:'+a.id,version=value.version if row else None)
        return usage(request,a)

    # 分页读取本人提醒，并附加当前评估状态与证据详情。
    @app.get('/api/me/alerts')
    def list_alerts(request:Request,q:str='',status:str='',page:int=1,size:int=20,a:Actor=Depends(user)):
        alerts.expire_rules(rt(request).store,a.id)
        result=resources.listing(rt(request).store,a.id,'alert',q=q,status=status,page=page,size=size)
        for row in result['items']:
            state=rt(request).store.one('SELECT * FROM alert_state WHERE user_id=? AND rule_id=?',(a.id,row['id']))
            row['evaluation']={**state,'details':json.loads(state['details_json'] or '{}')} if state else None
        return result

    # 校验并评估提醒草稿，仅返回预览而不持久化。
    @app.post('/api/me/alerts/preview')
    def preview_alert(value:alerts.AlertInput,request:Request,rule_id:str='',version:int=0,a:Actor=Depends(user)):
        store=rt(request).store;previous=None
        if rule_id:
            row=resources.get(store,a.id,'alert',rule_id)
            if row['version']!=version:raise AuthError('VERSION_CONFLICT','提醒已有新修订，请重新加载',409)
            previous=row['data']
        data=alerts.prepare(store,a.id,value,previous)
        return {'condition':data,'current':alerts.sample(store,a.id,data),'saved':False}

    # 按客户端幂等键创建本人提醒规则。
    @app.post('/api/me/alerts')
    def create_alert(payload:dict,request:Request,a:Actor=Depends(user)):
        store=rt(request).store;key=client_key(payload);raw=payload.get('data',{});hashed=research.fingerprint(raw)
        previous=resources.replay(store,a.id,'alert',key,hashed)
        return previous or alerts.save(store,a.id,alerts.AlertInput.model_validate(raw),client_key=key,request_fingerprint=hashed)

    # 核对归属与版本后保存提醒的新修订。
    @app.put('/api/me/alerts/{ident}')
    def update_alert(ident:str,payload:dict,request:Request,a:Actor=Depends(user)):
        resources.get(rt(request).store,a.id,'alert',ident)
        return alerts.save(rt(request).store,a.id,alerts.AlertInput.model_validate(payload.get('data',{})),ident,expected_version(payload))

    # 通过独立删除记录删除本人提醒。
    @app.delete('/api/me/alerts/{ident}')
    def delete_alert(ident:str,request:Request,a:Actor=Depends(user)):
        return resources.delete(rt(request).store,a.id,'alert',ident)

    # 分页返回本人提醒事件及近期各渠道投递状态。
    @app.get('/api/me/alerts/events')
    def alert_events(request:Request,page:int=1,size:int=20,a:Actor=Depends(user)):
        if size not in {20,50} or page<1:raise ValueError('分页参数无效')
        store=rt(request).store;rows=store.query('SELECT * FROM alert_events WHERE user_id=? ORDER BY triggered_at DESC LIMIT ? OFFSET ?',(a.id,size,(page-1)*size))
        for row in rows:row['facts']=json.loads(row.pop('facts_json'))
        return {'items':rows,'total':store.one('SELECT COUNT(*) n FROM alert_events WHERE user_id=?',(a.id,))['n'],'page':page,'size':size,'deliveries':store.query('SELECT id,channel,state,attempts,created_at,delivered_at,error_code,event_ids_json FROM alert_deliveries WHERE user_id=? ORDER BY created_at DESC LIMIT 50',(a.id,))}

    # 读取本人邮件通知开关，并展示站点 SMTP 是否已配置。
    @app.get('/api/me/alerts/settings')
    def notification_settings(request:Request,a:Actor=Depends(user)):
        row=rt(request).store.one("SELECT * FROM research_objects WHERE user_id=? AND kind='notification_settings'",(a.id,))
        ready=mail_configured(rt(request).store)
        return {'email_enabled':False,'version':0,'smtp_configured':ready} if not row else {**json.loads(row['payload_json']),'version':row['version'],'smtp_configured':ready}

    # 校验布尔开关和设置版本后保存本人通知偏好。
    @app.put('/api/me/alerts/settings')
    def update_notification_settings(payload:dict,request:Request,a:Actor=Depends(user)):
        if set(payload)-{'email_enabled','version'} or not isinstance(payload.get('email_enabled'),bool):raise ValueError('通知设置不合法')
        store=rt(request).store;old=notification_settings(request,a)
        if payload.get('version')!=old['version']:raise AuthError('VERSION_CONFLICT','通知设置已改变，请刷新',409)
        resources.save(store,a.id,'notification_settings',{'email_enabled':payload['email_enabled']},ident='notifications:'+a.id,version=old['version'] if old['version'] else None)
        return notification_settings(request,a)

    # 列出本人想法池，字段与本地入口一致。
    @app.get('/api/me/ideas')
    def list_ideas(request:Request,a:Actor=Depends(user)):
        from backend.idea_flow import cloud_list
        return cloud_list(rt(request).store,a.id)

    # 校验证券和文字后写入本人想法。
    @app.post('/api/me/ideas')
    def create_idea(payload:dict,request:Request,a:Actor=Depends(user)):
        from backend.idea_flow import cloud_create
        return cloud_create(rt(request).store,a.id,payload,client_key(payload) if payload.get('client_key') else None)

    # 删除本人的一条想法。
    @app.delete('/api/me/ideas/{ident}')
    def delete_idea(ident:str,request:Request,a:Actor=Depends(user)):
        return resources.delete(rt(request).store,a.id,'idea',ident)

    # 按状态、搜索词和分页读取本人的研究判断。
    @app.get('/api/me/journals')
    def list_journals(request:Request,q:str='',status:str='active',page:int=1,size:int=20,a:Actor=Depends(user)):
        return resources.listing(rt(request).store,a.id,'journal',q=q,status=status,page=page,size=size)

    # 校验判断内容与关联对象，创建本人判断快照。
    @app.post('/api/me/journals')
    def create_journal(payload:dict,request:Request,a:Actor=Depends(user)):
        store=rt(request).store;key=client_key(payload);raw=payload.get('data',{});hashed=research.fingerprint(raw)
        previous=resources.replay(store,a.id,'journal',key,hashed)
        return previous or research.save_journal(store,a.id,research.JournalInput.model_validate(raw),client_key=key,request_fingerprint=hashed)

    # 读取本人判断的各版内容、后续观察和计算历史。
    @app.get('/api/me/journals/{ident}')
    def journal(ident:str,request:Request,page:int=1,size:int=20,a:Actor=Depends(user)):
        return research.journal_detail(rt(request).store,a.id,ident,page=page,size=size)

    # 核对归属及版本后保存新的研究判断修订。
    @app.put('/api/me/journals/{ident}')
    def update_journal(ident:str,payload:dict,request:Request,a:Actor=Depends(user)):
        resources.get(rt(request).store,a.id,'journal',ident)
        return research.save_journal(rt(request).store,a.id,research.JournalInput.model_validate(payload.get('data',{})),ident,expected_version(payload))

    # 按版本归档或恢复本人判断，保留原始内容和修订历史。
    @app.post('/api/me/journals/{ident}/archive')
    def archive_journal(ident:str,payload:dict,request:Request,a:Actor=Depends(user)):
        old=resources.get(rt(request).store,a.id,'journal',ident)
        return resources.save(rt(request).store,a.id,'journal',old['data'],ident=ident,version=expected_version(payload),status='archived' if payload.get('archived',True) else 'active')

    # 持久化独立删除意图后移除本人判断。
    @app.delete('/api/me/journals/{ident}')
    def delete_journal(ident:str,request:Request,a:Actor=Depends(user)):
        return resources.delete(rt(request).store,a.id,'journal',ident)

    # 读取本人模拟账户的现金、持仓、行业分布及证据缺口。
    @app.get('/api/me/portfolio/{ident}/risk')
    def portfolio(ident:str,request:Request,a:Actor=Depends(user)):
        return research.portfolio_risk(rt(request).store,a.id,ident)

    # 分页搜索本人的实验研究主题。
    @app.get('/api/me/experiment-topics')
    def topics(request:Request,q:str='',page:int=1,size:int=20,a:Actor=Depends(user)):
        return resources.listing(rt(request).store,a.id,'topic',q=q,page=page,size=size)

    # 校验研究主题和问题长度后幂等创建课题。
    @app.post('/api/me/experiment-topics')
    def topic(payload:dict,request:Request,a:Actor=Depends(user)):
        title=str(payload.get('name','')).strip();question=str(payload.get('question','')).strip()
        if not title or len(title)>100 or len(question)>2000:raise ValueError('课题名称或研究问题不合法')
        return resources.save(rt(request).store,a.id,'topic',{'name':title,'question':question},client_key=payload.get('client_key'))

    # 核对可选主题归属后分页读取本人的策略实验。
    @app.get('/api/me/experiments')
    def list_experiments(request:Request,topic_id:str='',page:int=1,size:int=20,a:Actor=Depends(user)):
        if size not in {20,50} or page<1:raise ValueError('分页参数不合法')
        store=rt(request).store;where='user_id=?';args=[a.id]
        if topic_id:resources.get(store,a.id,'topic',topic_id);where+=' AND topic_id=?';args.append(topic_id)
        rows=store.query('SELECT id FROM research_experiments WHERE '+where+' ORDER BY created_at DESC LIMIT ? OFFSET ?',[*args,size,(page-1)*size])
        return {'items':[experiments.owned(store,a.id,r['id'],summary=True) for r in rows],'total':store.one('SELECT COUNT(*) n FROM research_experiments WHERE '+where,args)['n'],'page':page,'size':size}

    # 校验实验输入并加入本人后台队列。
    @app.post('/api/me/experiments')
    def create_experiment(value:experiments.ExperimentInput,request:Request,a:Actor=Depends(user)):
        return experiments.enqueue(rt(request).store,a.id,value)

    # 仅比较本人的多个实验，并返回口径差异说明。
    @app.post('/api/me/experiments/compare')
    def compare_experiments(payload:dict,request:Request,a:Actor=Depends(user)):
        return experiments.compare(rt(request).store,a.id,payload.get('ids'))

    # 核对归属后返回指定实验及已保存的结果。
    @app.get('/api/me/experiments/{ident}')
    def experiment(ident:str,request:Request,a:Actor=Depends(user)):
        return experiments.owned(rt(request).store,a.id,ident)

    # 核对实验归属后停止本人尚未结束的实验。
    @app.post('/api/me/experiments/{ident}/stop')
    def stop_experiment(ident:str,request:Request,a:Actor=Depends(user)):
        return experiments.stop(rt(request).store,a.id,ident)

    # 返回当前版本中可用的项目帮助条目。
    @app.get('/api/me/assistant/help')
    def help_index(a:Actor=Depends(user)):
        return assistant.knowledge()

    # 返回指定帮助资料及版本信息。
    @app.get('/api/me/assistant/help/{ident}')
    def help_detail(ident:str,a:Actor=Depends(user)):
        return assistant.help_document(ident)

    # 分页搜索本人的助手会话。
    @app.get('/api/me/assistant/sessions')
    def sessions(request:Request,q:str='',page:int=1,size:int=20,a:Actor=Depends(user)):
        return resources.listing(rt(request).store,a.id,'chat_session',q=q,page=page,size=size)

    # 校验标题后创建具有客户端幂等键的个人会话。
    @app.post('/api/me/assistant/sessions')
    def new_session(payload:dict,request:Request,a:Actor=Depends(user)):
        title=str(payload.get('title','新对话')).strip()
        if not title or len(title)>100:raise ValueError('会话名称须为1至100字')
        return resources.save(rt(request).store,a.id,'chat_session',{'title':title},client_key=payload.get('client_key'))

    # 核对会话归属后分页返回消息，按时间顺序展示当前页。
    @app.get('/api/me/assistant/sessions/{ident}')
    def session(ident:str,request:Request,page:int=1,size:int=20,a:Actor=Depends(user)):
        store=rt(request).store;row=resources.get(store,a.id,'chat_session',ident)
        if size not in {20,50} or page<1:raise ValueError('分页参数无效')
        ids=store.query('SELECT id FROM assistant_requests WHERE user_id=? AND session_id=? ORDER BY created_at DESC LIMIT ? OFFSET ?',(a.id,ident,size,(page-1)*size))
        return {**row,'messages':[assistant.request_detail(store,a.id,r['id']) for r in ids[::-1]],'total':store.one('SELECT COUNT(*) n FROM assistant_requests WHERE user_id=? AND session_id=?',(a.id,ident))['n'],'page':page,'size':size}

    # 删除本人会话，并通过权威记录防止旧备份恢复内容。
    @app.delete('/api/me/assistant/sessions/{ident}')
    def delete_session(ident:str,request:Request,a:Actor=Depends(user)):
        return resources.delete(rt(request).store,a.id,'chat_session',ident)

    # 在本人会话中提交问题、授权上下文及模型选择。
    @app.post('/api/me/assistant/sessions/{ident}/messages')
    def message(ident:str,value:assistant.MessageInput,request:Request,a:Actor=Depends(user)):
        runtime=rt(request);return assistant.submit(runtime.store,runtime.keys,a.id,ident,value)

    # 校验片段游标，读取本人请求状态及已检查的输出片段。
    @app.get('/api/me/assistant/requests/{ident}')
    def request_state(ident:str,request:Request,after:int=0,a:Actor=Depends(user)):
        if after<0:raise ValueError('片段序号不合法')
        return assistant.request_detail(rt(request).store,a.id,ident,after)

    # 停止本人请求及对应任务，必要时关闭网络连接。
    @app.post('/api/me/assistant/requests/{ident}/stop')
    def stop_request(ident:str,request:Request,a:Actor=Depends(user)):
        return assistant.stop(rt(request).store,a.id,ident)

    # 仅由界面中的手动应用按钮调用，推理协议不暴露此写入口。
    @app.post('/api/me/assistant/requests/{ident}/actions/{index}/apply')
    def apply_assistant_action(ident:str,index:int,value:assistant_actions.ApplyInput,request:Request,a:Actor=Depends(user)):
        return assistant_actions.apply(rt(request).store,a.id,ident,index)

    # 仅允许管理员凭官方来源更新证券行业分类，并拒绝未来日期。
    @app.put('/api/admin/market/industry/{ident}')
    def industry(ident:str,payload:dict,request:Request,a:Actor=Depends(admin)):
        store=rt(request).store;market.instrument(store,ident);name=str(payload.get('industry','')).strip();source=str(payload.get('source_url',''))
        if not name or len(name)>100 or not official_url(source):raise ValueError('请提供行业分类名称和官方证据链接')
        as_of=str(payload.get('as_of',''));datetime.strptime(as_of,'%Y-%m-%d')
        if as_of>now().date().isoformat():raise ValueError('行业资料时间不能在未来')
        store.execute('INSERT INTO industry_classifications VALUES(?,?,?,?,?) ON CONFLICT(instrument_id) DO UPDATE SET industry=excluded.industry,source_url=excluded.source_url,as_of=excluded.as_of,verified_at=excluded.verified_at',(ident,name,source,as_of,stamp()))
        return {'saved':True}

    # 仅允许管理员整批导入带时区与官方来源的市场交易场次。
    @app.post('/api/admin/market/sessions')
    def import_sessions(payload:dict,request:Request,a:Actor=Depends(admin)):
        values=payload.get('sessions');market_name=payload.get('market')
        if market_name not in {'SH','SZ','HK','US','FUND','SHFE'} or not isinstance(values,list) or not 1<=len(values)<=2000:raise ValueError('请提供已接入市场及1至2000条已核验交易日')
        rows=[]
        for value in values:
            op=datetime.fromisoformat(value['open_at']);cl=datetime.fromisoformat(value['close_at'])
            if op.tzinfo is None or cl.tzinfo is None or op>=cl or op.date().isoformat()!=value['day'] or not official_url(value['source_url']):raise ValueError('交易日时间、时区或官方来源无效')
            rows.append((market_name,value['day'],op.isoformat(),cl.isoformat(),value['source_url'],stamp()))
        with rt(request).store.tx(immediate=True) as c:c.executemany('INSERT INTO market_sessions VALUES(?,?,?,?,?,?) ON CONFLICT(market,day) DO UPDATE SET open_at=excluded.open_at,close_at=excluded.close_at,source_url=excluded.source_url,fetched_at=excluded.fetched_at',rows)
        return {'imported':len(rows)}
