# 实现研究就绪检查、版本化判断日志、后续观察和个人组合风险度量。
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime,timedelta,time
from decimal import Decimal

from pydantic import BaseModel,ConfigDict,Field,model_validator
from backend.config import TZ,now
from backend.budget import budget_check,dec,money
from backend.instruments import official_url
from backend.collectors import fresh_reasons
from . import market,resources,workspace
from .auth import AuthError
from .preferences import get_preferences
from .store import stamp,json_text

RULE_VERSION='research-workbench/1'


class ReadView:
    def __init__(self,connection):self.connection=connection
    def one(self,sql,args=()):
        row=self.connection.execute(sql,args).fetchone()
        return dict(row) if row else None


# 对稳定 JSON 计算 SHA-256，标识事实快照与计算结果。
def fingerprint(value):return hashlib.sha256(json_text(value).encode()).hexdigest()


# 优先读取市场专属交易场次，境内市场缺失时使用已采集交易日历。
def sessions(store,market_name):
    rows=store.query('SELECT * FROM market_sessions WHERE market=? ORDER BY day',(market_name,))
    if rows:return rows
    if market_name not in {'SH','SZ'}:return []
    return [{'day':r['day'],'open_at':r['day']+'T09:30:00+08:00','close_at':r['day']+'T15:00:00+08:00','source_url':r['source'],'fetched_at':r['fetched_at']} for r in store.query('SELECT * FROM calendar ORDER BY day')]


# 按本人自选检查风险、行情、预算和历史证据，并生成可分页的问题清单。
def _collect_readiness(store,uid,prefs,ctx):
    issues=[];objects=[]
    items=store.query('SELECT i.* FROM instruments i JOIN user_watchlist w ON w.instrument_id=i.id WHERE w.user_id=? ORDER BY i.id',(uid,))
    latest=store.query("SELECT b.* FROM bars b JOIN (SELECT b.instrument_id,MAX(b.ts) ts FROM bars b JOIN user_watchlist w ON w.instrument_id=b.instrument_id WHERE w.user_id=? AND b.timeframe='1d' GROUP BY b.instrument_id) last ON last.instrument_id=b.instrument_id AND last.ts=b.ts WHERE b.timeframe='1d'",(uid,))
    last_by_id={b['instrument_id']:{**b,'metadata':json.loads(b['metadata_json'])} for b in latest}
    evidence={}
    for field,url in (('historical_status_verified','status_source_url'),('corporate_actions_verified','corporate_actions_source_url')):
        rows=store.query("SELECT b.instrument_id,COUNT(*) n,SUM(CASE WHEN json_extract(metadata_json,?)=1 AND (json_extract(metadata_json,?) LIKE 'https://www.sse.com.cn/%' OR json_extract(metadata_json,?) LIKE 'https://www.szse.cn/%' OR json_extract(metadata_json,?) LIKE 'https://www.cninfo.com.cn/%') THEN 0 ELSE 1 END) missing FROM bars b JOIN user_watchlist w ON w.instrument_id=b.instrument_id WHERE w.user_id=? AND b.timeframe='1d' GROUP BY b.instrument_id",('$.'+field,'$.'+url,'$.'+url,'$.'+url,uid))
        evidence[field]={r['instrument_id']:r for r in rows}
    for item in items:
        issue_start=len(issues)
        item['rules']=json.loads(item.pop('rules_json'));item['evidence']=json.loads(item.pop('evidence_json'));last=last_by_id.get(item['id'])
        # 为当前证券追加带来源、时点、任务范围和修复入口的就绪问题。
        def add(code,message,scope,severity='blocked',admin=True):
            issues.append({'instrument_id':item['id'],'name':item['name'],'code':code,'message':message,'task':scope,'status':severity,'source_url':last.get('source_url') if last else item.get('source_url'),'quote_time':last.get('ts') if last else None,'collected_at':last.get('fetched_at') if last else None,'verified_at':item.get('verified_at'),'rule_version':RULE_VERSION,'action':{'page':'watchlist','instrument_id':item['id'],'admin_required':admin}})
        if item['market'] in {'SH','SZ'} and item['asset_class'] in {'stock','etf'}:
            for n,msg in enumerate(market.risk_reasons(item,prefs)):add('RISK_'+fingerprint(msg)[:10],msg,'candidate')
            for msg in fresh_reasons(last,ctx):
                add('QUOTE_'+fingerprint(msg)[:10],msg,'candidate','unknown' if not last else 'blocked');issues[-1]['affects']=['candidate','market']
            if last:
                try:
                    estimate=budget_check(last['close'],item['rules'],item['asset_class'],prefs.fees,prefs.budget)
                    if not estimate['within_budget']:add('BUDGET_EXCEEDED','最低合法数量含费超过个人预算','candidate',admin=False)
                except ValueError:add('PRICE_RULE_UNKNOWN','价格或产品步长口径不一致','candidate')
            history=evidence['historical_status_verified'].get(item['id']);actions=evidence['corporate_actions_verified'].get(item['id'])
            if not history or history['n']<40:add('HISTORY_SHORT','历史长度不足以进行可靠的严格回测','strict_backtest')
            if not history or history['missing']:add('HISTORICAL_STATUS_MISSING','缺少逐日历史交易状态证据','strict_backtest')
            if not actions or actions['missing']:add('CORPORATE_ACTIONS_MISSING','缺少逐日公司行动证据','strict_backtest')
            if not item['rules'].get('verified') or not official_url(item['rules'].get('source_url','')):add('PRODUCT_RULES_UNVERIFIED','产品交易规则未核验','strict_backtest')
        else:
            add('OBSERVATION_ONLY','此类资料用于行情观察，不适用境内普通买入候选','candidate','not_applicable',False)
            add('BACKTEST_NOT_APPLICABLE','此类资料不适用当前境内股票与ETF撮合回测','strict_backtest','not_applicable',False)
            if not last and not store.one('SELECT instrument_id FROM market_quotes WHERE instrument_id=?',(item['id'],)):add('OBSERVATION_QUOTE_UNKNOWN','尚无有效观察报价','market','unknown')
        own=issues[issue_start:]
        state='blocked' if any(r['status']=='blocked' for r in own) else 'unknown' if any(r['status']=='unknown' for r in own) else 'ready'
        abilities={}
        for ability in ('candidate','strict_backtest','market'):
            subset=[r for r in own if r['task']==ability or ability in r.get('affects',[])]
            abilities[ability]=next((s for s in ('blocked','unknown','hint','not_applicable') if any(r['status']==s for r in subset)),'ready')
        objects.append({'instrument_id':item['id'],'name':item['name'],'status':state,'abilities':abilities,'issues':len(own)})
    counts={}
    for r in issues:counts[r['code']]=counts.get(r['code'],0)+1
    value={'status':'empty' if not objects else 'blocked' if any(r['status']=='blocked' for r in objects) else 'unknown' if any(r['status']=='unknown' for r in objects) else 'ready','object_count':len(objects),'affected_objects':len({r['instrument_id'] for r in issues if r['status']!='not_applicable'}),'issue_count':len(issues),'reason_counts':counts,'objects':objects,'issues':issues,'rule_version':RULE_VERSION,'calendar':ctx}
    return value


def readiness(store,uid,*,task='',q='',status='',page=1,size=20,persist=True):
    if size not in {20,50} or page<1:raise ValueError('分页参数不合法')
    with store.tx() as c:
        view=ReadView(c);prefs=get_preferences(view,uid);ctx=market.calendar(view)
        owned_ids=tuple(r[0] for r in c.execute('SELECT instrument_id FROM user_watchlist WHERE user_id=? ORDER BY instrument_id',(uid,)))
        revision=view.one("SELECT value FROM cloud_meta WHERE key='shared_research_revision'")['value']
        latest=view.one('SELECT id,version,payload_json FROM personal_reports WHERE user_id=? ORDER BY generated_at DESC LIMIT 1',(uid,))
        old=view.one("SELECT version,json_extract(payload_json,'$.snapshot_id') snapshot_id FROM research_objects WHERE id=? AND user_id=?",('readiness:'+uid,uid))
        active=store.active(uid,c=c)
    # Only shared evidence, the explicitly owned security set and numerical settings are cached.
    # Private archives are always resolved below using the requesting user's identity.
    fee_values={k:v for k,v in prefs.fees.model_dump().items() if k!='note'}
    cache_key=fingerprint([revision,owned_ids,prefs.budget,prefs.max_risk_age_hours,fee_values,ctx,int(now().timestamp())//15])
    with store.research_cache_lock:
        base=store.research_cache.get(cache_key)
        if base is None:
            base=_collect_readiness(store,uid,prefs,ctx)
            base['shared_snapshot_id']=fingerprint(base)
            if len(store.research_cache)>=64:store.research_cache.clear()
            store.research_cache[cache_key]=base
    issues=base['issues']
    report=None
    if latest:
        facts=json.loads(latest['payload_json']);report={'id':latest['id'],'version':latest['version'],'snapshot_id':facts.get('snapshot_id'),'report_date':facts.get('report_date'),'excluded':facts.get('excluded',[]),'candidate_count':len(facts.get('candidates',[]))}
    value={**base,'latest_report':report}
    value['snapshot_id']=fingerprint([base['shared_snapshot_id'],report])
    if persist and active:
        if not old or old['snapshot_id']!=value['snapshot_id']:
            stored={**value,'issues':issues[:50],'issues_truncated':len(issues)>50}
            try:resources.save(store,uid,'readiness',stored,ident='readiness:'+uid,version=old['version'] if old else None)
            except AuthError as exc:
                if exc.code!='VERSION_CONFLICT':raise
    filtered=[r for r in issues if (not task or r['task']==task or task in r.get('affects',[])) and (not status or r['status']==status) and (not q or q.lower() in (r['name']+r['instrument_id']+r['message']).lower())]
    visible_ids={r['instrument_id'] for r in filtered[(page-1)*size:page*size]}
    objects=[o for o in base['objects'] if o['instrument_id'] in visible_ids] if visible_ids else [o for o in base['objects'] if not q or q.lower() in (o['name']+o['instrument_id']).lower()]
    return {**value,'objects':objects[:size],'issues':filtered[(page-1)*size:page*size],'total':len(filtered),'page':page,'size':size,'calculated_at':stamp()}


# 约束研究判断、反方论据、失效条件和关联模拟委托等输入。
class JournalInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    instrument_id:str
    report_id:str|None=None
    rationale:str=Field(min_length=1,max_length=4000)
    counterargument:str=Field('',max_length=4000)
    invalidation:str=Field('',max_length=4000)
    note:str=Field('',max_length=4000)
    order_ids:list[str]=Field(default_factory=list,max_length=50)


# 核验报告及委托归属与证券一致性，再保存带版本的判断快照。
def save_journal(store,uid,value:JournalInput,ident=None,version=None,client_key=None,request_fingerprint=None):
    request_fingerprint=request_fingerprint or fingerprint(value.model_dump(mode='json',exclude_unset=True))
    if not ident:
        previous=resources.replay(store,uid,'journal',client_key,request_fingerprint)
        if previous:return previous
    item=market.instrument(store,value.instrument_id);data=value.model_dump();data['input_fingerprint']=fingerprint(data)
    if not ident:data['creation_request_fingerprint']=request_fingerprint
    if value.report_id:
        row=workspace.owned(store,'personal_reports',uid,value.report_id);report=json.loads(row['payload_json'])
        mentioned={r['instrument_id'] for r in report.get('candidates',[])+report.get('excluded',[])}
        if item['id'] not in mentioned:raise ValueError('该证券不在所选报告的研究范围内')
        data['report_snapshot']={'id':row['id'],'version':row['version'],'report_date':row['report_date'],'snapshot_id':report.get('snapshot_id'),'generated_at':row['generated_at']}
    for order_id in value.order_ids:
        row=workspace.owned(store,'sim_orders',uid,order_id)
        if row['instrument_id']!=item['id']:raise ValueError('关联成交必须属于同一研究证券')
    if ident:
        old=resources.get(store,uid,'journal',ident)
        if old['version']!=version:raise AuthError('VERSION_CONFLICT','判断已修订，请重新加载',409)
    data['judgement_at']=stamp();data['instrument']={k:item[k] for k in ('id','name','symbol','market','asset_class')};data['rule_version']=RULE_VERSION
    return resources.save(store,uid,'journal',data,ident=ident,version=version,client_key=client_key,status=old['status'] if ident else 'active')


# 对关联的本人模拟成交按先进先出配对，仅闭合数量计入已实现收益。
def simulation_return(store,uid,order_ids):
    if not order_ids:return {'status':'unlinked','realized_pnl':None,'closed_quantity':0}
    orders=[workspace.owned(store,'sim_orders',uid,ident) for ident in order_ids]
    if len({r['account_id'] for r in orders})>1:raise ValueError('关联成交必须来自同一模拟账户')
    lots=[];pnl=Decimal(0);closed=0;unmatched=0
    for o in sorted(orders,key=lambda r:(r['filled_at'] or r['created_at'],r['id'])):
        if o['status']!='filled':continue
        qty=o['quantity'];gross=dec(o['fill_price'])*qty;fee=dec(o['fees'])
        if o['side']=='buy':lots.append([qty,(gross+fee)/qty])
        else:
            proceeds=(gross-fee)/qty;remaining=qty
            for lot in lots:
                take=min(remaining,lot[0]);pnl+=take*(proceeds-lot[1]);remaining-=take;lot[0]-=take;closed+=take
                if not remaining:break
            unmatched+=remaining
    return {'status':'partial' if unmatched or any(l[0] for l in lots) else 'closed' if closed else 'no_round_trip','realized_pnl':float(money(pnl)) if closed else None,'closed_quantity':closed,'unmatched_sell_quantity':unmatched,'remaining_quantity':sum(l[0] for l in lots),'basis':'已保存模拟成交及费用，FIFO配对；仅闭合数量计入已实现收益'}


# 从判断后的首个交易场次起观察后续表现，明确区分未到期和缺失证据。
def observation(store,uid,journal,*,at=None):
    at=at or now();data=journal['data'];judgement=datetime.fromisoformat(data['judgement_at']);item=market.instrument(store,data['instrument_id']);days=sessions(store,item['market'])
    result={'judgement_at':data['judgement_at'],'rule_version':RULE_VERSION,'start':None,'targets':[],'simulation':simulation_return(store,uid,data.get('order_ids',[]))}
    if not days or not days[0]['day']<=judgement.astimezone(TZ).date().isoformat()<=days[-1]['day']:
        return {**result,'status':'calendar_unknown'}
    start=next((i for i,r in enumerate(days) if datetime.fromisoformat(r['open_at'])>judgement),None)
    if start is None:return {**result,'status':'calendar_unknown'}
    anchor=days[start];result['start']={'day':anchor['day'],'open_at':anchor['open_at'],'price':None,'source_url':None}
    bars={b['ts'][:10]:b for b in market.bars_for(store,item['id'],limit=5000)};base=bars.get(anchor['day'])
    if datetime.fromisoformat(anchor['open_at'])>at:base_status='pending_start'
    elif not base:base_status='baseline_missing'
    else:base_status='started';result['start'].update(price=base['open'],source_url=base['source_url'],quote_time=base['ts'])
    for horizon in (1,5,20):
        if start+horizon-1>=len(days):result['targets'].append({'horizon':horizon,'status':'calendar_unknown','return_pct':None});continue
        target=days[start+horizon-1];bar=bars.get(target['day']);entry={'horizon':horizon,'day':target['day'],'close_at':target['close_at'],'return_pct':None,'source_url':bar.get('source_url') if bar else None,'quote_time':bar.get('ts') if bar else None,'collected_at':bar.get('fetched_at') if bar else None}
        if datetime.fromisoformat(target['close_at'])>at:entry['status']='not_due'
        elif base_status!='started':entry['status']=base_status
        elif not bar:entry['status']='price_missing'
        elif bar['metadata'].get('trading_status')=='suspended':entry['status']='suspended'
        else:
            relevant=[bars.get(r['day']) for r in days[start:start+horizon]]
            comparable=all(b and b['quality']=='valid' and b['adjustment']=='none' and b['metadata'].get('corporate_actions_verified') and official_url(b['metadata'].get('corporate_actions_source_url','')) for b in relevant)
            if not comparable:entry['status']='corporate_actions_unverified'
            elif dec(base['open'])<=0:entry['status']='invalid_baseline'
            else:entry.update(status='observed',return_pct=float((dec(bar['close'])/dec(base['open'])-1)*100),close=bar['close'],basis='不复权可比价格变化，不含交易费用，不代表实际成交')
        result['targets'].append(entry)
    result['status']=base_status
    return result


# 返回各版判断及观察计算，并按结果指纹保留可追溯的计算历史。
def journal_detail(store,uid,ident,at=None,*,page=1,size=20):
    if page<1 or size not in {20,50}:raise ValueError('修订分页无效')
    row=resources.get(store,uid,'journal',ident);versions=store.query('SELECT * FROM research_versions WHERE object_id=? AND user_id=? ORDER BY version DESC LIMIT ? OFFSET ?',(ident,uid,size,(page-1)*size));outputs=[]
    for v in versions:
        judgement={**row,'version':v['version'],'data':json.loads(v['payload_json'])};value=observation(store,uid,judgement,at=at);hashed=fingerprint(value)
        with store.user_lock(uid),store.tx(immediate=True) as c:
            if store.active(uid,c=c) and c.execute('SELECT 1 FROM research_objects WHERE id=? AND user_id=?',(ident,uid)).fetchone():
                previous=c.execute('SELECT data_hash,calculation_version FROM journal_observation_history WHERE user_id=? AND object_id=? AND judgement_version=? ORDER BY calculation_version DESC LIMIT 1',(uid,ident,v['version'])).fetchone()
                if not previous or previous['data_hash']!=hashed:
                    number=previous['calculation_version']+1 if previous else 1
                    c.execute('INSERT INTO journal_observation_history VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,uid,ident,v['version'],number,stamp(),hashed,json_text(value)))
        outputs.append({'version':v['version'],'created_at':v['created_at'],'data':judgement['data'],'observation':value,'calculations':[{'version':r['calculation_version'],'created_at':r['created_at'],'data':json.loads(r['payload_json'])} for r in store.query('SELECT * FROM journal_observation_history WHERE user_id=? AND object_id=? AND judgement_version=? ORDER BY calculation_version',(uid,ident,v['version']))]})
    return {**row,'versions':outputs,'total_versions':row['version'],'version_page':page,'version_size':size}


# 按权益峰值计算当前及最大回撤；历史缺失时不输出伪精确结果。
def drawdowns(sequence):
    peak=None;current=None;largest=0
    for value in sequence:
        if value is None or dec(value)<=0:return {'current_drawdown_pct':None,'max_drawdown_pct':None,'status':'incomplete'}
        value=dec(value);peak=max(peak or value,value);current=(peak-value)/peak*100;largest=max(largest,current)
    return {'current_drawdown_pct':float(current) if current is not None else None,'max_drawdown_pct':float(largest) if current is not None else None,'status':'verified' if current is not None else 'missing_history'}


def valid_valuation_segments(curve):
    segments=[];current=[]
    def finish():
        if not current:return
        metrics=drawdowns([p['equity'] for p in current]) if len(current)>1 else {'current_drawdown_pct':None,'max_drawdown_pct':None}
        segments.append({'start':current[0]['ts'],'end':current[-1]['ts'],'points':len(current),**metrics,'basis':'相邻已留存的有效快照，不代表逐交易日完整覆盖'})
        current.clear()
    for point in curve:
        if point['quality']=='verified' and point['equity'] is not None and dec(point['equity'])>0:current.append(point)
        else:finish()
    finish();return segments


# 计算本人现金、持仓与行业集中度，保存估值证据并保留数据缺口。
def portfolio_risk(store,uid,account_id):
    account=workspace.owned(store,'sim_accounts',uid,account_id);cash=dec(account['cash']);positions=[];market_value=Decimal(0);known=Decimal(0);sectors={};gaps=[];ctx=market.calendar(store)
    for p in store.query('SELECT instrument_id,SUM(quantity) quantity FROM sim_lots WHERE user_id=? AND account_id=? AND quantity>0 GROUP BY instrument_id',(uid,account_id)):
        item=market.instrument(store,p['instrument_id']);bars=market.bars_for(store,item['id'],limit=1);bar=bars[-1] if bars else None;reasons=fresh_reasons(bar,ctx)
        value=money(dec(bar['close'])*p['quantity']) if bar else None
        if reasons:gaps.append({'instrument_id':item['id'],'reasons':reasons})
        if value is not None:market_value+=value
        industry=store.one('SELECT * FROM industry_classifications WHERE instrument_id=?',(item['id'],)) if item['asset_class']!='etf' else None
        group=industry['industry'] if industry else '未穿透 ETF' if item['asset_class']=='etf' else '未知行业'
        if value is not None:
            sectors[group]=sectors.get(group,Decimal(0))+value
            if industry:known+=value
        positions.append({**p,'name':item['name'],'market_value':float(value) if value is not None else None,'industry':group,'industry_source':industry,'quote_time':bar['ts'] if bar else None,'source_url':bar['source_url'] if bar else None,'quality':'unverified' if reasons else 'valid'})
    reserved=sum((dec(r['cost']) for r in store.query("SELECT cost FROM sim_orders WHERE user_id=? AND account_id=? AND status='pending' AND side='buy'",(uid,account_id))),Decimal(0));equity=cash+market_value;trusted=not gaps and equity>0
    known_sectors=[(k,v) for k,v in sectors.items() if k not in {'未知行业','未穿透 ETF'}];top=max(known_sectors,key=lambda r:r[1]) if known_sectors else None
    result={'account_id':account_id,'name':account['name'],'currency':'CNY','cash':float(cash),'reserved_cash':float(reserved),'available_cash':float(cash-reserved),'market_value':float(market_value) if not gaps else None,'equity':float(equity) if not gaps else None,'book_reference_equity':float(equity),'position_pct':float(market_value/equity*100) if trusted else None,'cash_pct':float(cash/equity*100) if trusted else None,'industry_concentration_pct':float(top[1]/market_value*100) if trusted and top and market_value else None,'industry_equity_pct':float(top[1]/equity*100) if trusted and top else None,'classification_coverage_pct':float(known/market_value*100) if trusted and market_value else None,'top_industry':top[0] if top else None,'industry_distribution':[{'industry':k,'value':float(v),'position_pct':float(v/market_value*100) if trusted and market_value else None} for k,v in sectors.items()],'positions':positions,'gaps':gaps,'quality':'verified' if trusted else 'incomplete','rule_version':RULE_VERSION}
    hashed=fingerprint(result)
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if store.active(uid,c=c) and c.execute('SELECT id FROM sim_accounts WHERE id=? AND user_id=?',(account_id,uid)).fetchone():
            previous=c.execute('SELECT revision,data_hash FROM portfolio_valuation_history WHERE user_id=? AND account_id=? ORDER BY revision DESC LIMIT 1',(uid,account_id)).fetchone()
            if not previous or previous['data_hash']!=hashed:
                c.execute('INSERT INTO portfolio_valuation_history VALUES(?,?,?,?,?,?,?,?)',(uuid.uuid4().hex,uid,account_id,previous['revision']+1 if previous else 1,stamp(),hashed,json_text(result),'live'))
    history=store.query('SELECT revision,created_at,payload_json,origin FROM portfolio_valuation_history WHERE user_id=? AND account_id=? ORDER BY revision',(uid,account_id));sequence=[dec(account['initial_cash'])]
    curve=[{'ts':account['created_at'],'equity':float(account['initial_cash']),'quality':'verified','source':'虚拟初始资金'}]
    for r in history:
        point=json.loads(r['payload_json']);sequence.append(point['equity'] if point['quality']=='verified' else None);curve.append({'ts':r['created_at'],'equity':point['equity'],'quality':point['quality'],'revision':r['revision'],'origin':r['origin']})
    segments=valid_valuation_segments(curve)
    return {**result,**drawdowns(sequence),'valuation_at':history[-1]['created_at'] if history else stamp(),'valuation_revision':history[-1]['revision'] if history else None,'curve':curve,'valid_segments':segments[-20:],'segment_count':len(segments),'drawdown_basis':'基于已留存的估值快照，不推断未观测时段；历史有缺口时不提供完整区间回撤','history_incomplete':any(v is None for v in sequence),'industry_note':'未知行业及未穿透ETF计入持仓分母；不以缺失分类推断分散风险'}
