# 将研究主题关联到可复现策略实验，并提供买入持有基准与口径差异比较。
from __future__ import annotations

import json
import uuid
from decimal import Decimal

from pydantic import BaseModel,ConfigDict,Field
from backend.backtest import Parameters,executable_price
from backend.budget import budget_check,dec,money,transaction_fees
from backend.config import STRATEGY_VERSION,Fees
from . import market,resources,workspace
from .auth import AuthError
from .preferences import get_preferences
from .research import fingerprint,RULE_VERSION
from .store import json_text,stamp


# 约束实验幂等键、所属主题、策略参数及可选基准与父实验。
class ExperimentInput(BaseModel):
    model_config=ConfigDict(extra='forbid')
    client_key:str=Field(min_length=8,max_length=100)
    topic_id:str
    parameters:Parameters
    index_id:str|None=None
    parent_id:str|None=None


# 在区间首个可执行开盘模拟买入，计入费用、预算、公司行动及未投入现金。
def buy_hold(bars,params,item,fees,budget,start_index=0):
    cash=money(params.initial_cash);quantity=0;paid=Decimal(0);fills=[];curve=[];rejected=[];peak=cash;max_dd=Decimal(0)
    for i,b in enumerate(bars):
        if i<start_index:continue
        m=b.get('metadata',{});rules=item['rules'];tick=dec(rules['price_tick'])
        if quantity:
            split=dec(m.get('split_ratio',1));dividend=dec(m.get('cash_dividend',0))
            if split<=0 or dividend<0 or dec(quantity)*split%1:raise ValueError('基准公司行动无法可靠处理')
            cash=money(cash+quantity*dividend);quantity=int(quantity*split)
        if not quantity:
            previous=dec(m.get('reference_price',bars[i-1]['close'] if i else b['open']))
            upper=dec(m.get('upper_limit',(previous*(1+dec(rules['limit_pct']))/tick).quantize(Decimal(1),rounding='ROUND_HALF_UP')*tick));lower=dec(m.get('lower_limit',(previous*(1-dec(rules['limit_pct']))/tick).quantize(Decimal(1),rounding='ROUND_HALF_UP')*tick))
            price=executable_price(b['open'],'buy',rules['price_tick'],fees.slippage_bps)
            valid=b['volume']>0 and b.get('quality')=='valid' and m.get('trading_status','active')=='active' and m.get('risk_flag','clear')=='clear' and dec(b['open'])<upper and lower<=price<=upper and dec(b['low'])<=price<=dec(b['high'])
            if valid:
                estimate=budget_check(price,rules,item['asset_class'],fees,min(budget,1000));q=estimate['max_quantity']
                while q>=rules['lot_size'] and dec(price)*q+dec(transaction_fees(price,q,item['asset_class'],'buy',fees)['total'])>cash:q-=rules['quantity_step']
                if q>=rules['lot_size']:
                    paid=dec(transaction_fees(price,q,item['asset_class'],'buy',fees)['total']);cash=money(cash-price*q-paid);quantity=q
                    fills.append({'ts':b['ts'],'side':'buy','price':float(price),'quantity':q,'fees':float(paid),'remaining_cash':float(cash)})
                else:rejected.append({'ts':b['ts'],'reason':'预算、最小申报数量或费用后现金不足'})
            else:rejected.append({'ts':b['ts'],'reason':'停牌、交易状态、成交量、限价或滑点阻断'})
        equity=money(cash+dec(b['close'])*quantity);peak=max(peak,equity);max_dd=max(max_dd,(peak-equity)/peak if peak>0 else Decimal(0))
        curve.append({'ts':b['ts'],'equity':float(equity),'normalized':float(equity/dec(params.initial_cash)*100)})
    final=curve[-1]['equity'] if curve else params.initial_cash
    return {'metrics':{'initial_cash':params.initial_cash,'final_equity':final,'return_pct':float((dec(final)/dec(params.initial_cash)-1)*100),'max_drawdown_pct':float(max_dd*100),'fill_count':len(fills),'fees':float(paid),'open_quantity':quantity,'remaining_cash':float(cash)},'trades':fills,'equity':curve,'rejected':rejected,'execution':'区间首个可执行开盘买入，遵守单只含费预算、产品规则及历史可成交性；保留未投入现金；期末仍持有，不虚构卖出','version':'buy-hold-open/1'}


# 生成全样本与子区间买入持有基准，并按共同有效日期归一化指数。
def benchmarks(store,bars,params,item,fees,budget,result,index_id=None,index_snapshot=None):
    split=int(len(bars)*params.train_ratio)
    output={'buy_hold':{'full':buy_hold(bars,params,item,fees,budget),'in_sample':buy_hold(bars[:split],params,item,fees,budget),'out_of_sample':buy_hold(bars,params,item,fees,budget,split),'validated':result['validated'],'warnings':result['warnings'],'subperiod_basis':'样本内和样本外分别使用新的初始资金，不能拼接为连续实盘曲线'},'index':{'status':'not_selected'},'strategy_normalized':[{'ts':p['ts'],'value':p['equity']/params.initial_cash*100} for p in result['full']['equity']]}
    if index_id:
        index=index_snapshot['instrument'] if index_snapshot else market.instrument(store,index_id)
        if index['asset_class']!='index':raise ValueError('归一化观察基准必须为指数')
        index_bars=index_snapshot['bars'] if index_snapshot else market.bars_for(store,index_id,limit=5000)
        output['index_snapshot']={'instrument':index,'bars':index_bars}
        observed={b['ts'][:10]:b for b in index_bars if b['quality']=='valid' and b['adjustment']=='none'}
        points=[(p,observed[p['ts'][:10]]) for p in result['full']['equity'] if p['ts'][:10] in observed]
        if not points:output['index']={'status':'missing','instrument_id':index_id,'reason':'没有共同的有效数据区间'}
        else:
            first=points[0][1]['close'];strategy_start=points[0][0]['equity']
            # 按策略与指数的共同有效日期配对，将各自起点归一化为 100。
            def interval(curve):
                paired=[(p,observed[p['ts'][:10]]) for p in curve if p['ts'][:10] in observed]
                if not paired:return {'status':'missing','points':[]}
                first_index=paired[0][1]['close'];first_strategy=paired[0][0]['equity']
                return {'status':'available','common_range':[paired[0][1]['ts'],paired[-1][1]['ts']],'points':[{'day':i['ts'][:10],'index':i['close']/first_index*100,'strategy':p['equity']/first_strategy*100,'source_url':i['source_url']} for p,i in paired]}
            output['index']={'instrument_id':index_id,'name':index['name'],'kind':'price_index',**interval(result['full']['equity']),'subperiods':{k:interval(result[k]['equity']) for k in ('in_sample','out_of_sample')},'basis':'共同有效起点归一化为100的价格指数观察，不代表可无费买卖或总收益；费用及风险敞口与策略不同'}
    return output


# 核验主题和证券归属，按请求指纹幂等创建实验与对应后台任务。
def enqueue(store,uid,value:ExperimentInput):
    from .jobs import enqueue_task
    resources.get(store,uid,'topic',value.topic_id);item=market.instrument(store,value.parameters.instrument_id)
    if item['market'] not in {'SH','SZ'} or item['asset_class'] not in {'stock','etf'}:raise ValueError('实验仅支持已接入的境内股票及ETF')
    if value.parent_id:owned(store,uid,value.parent_id)
    if value.index_id and market.instrument(store,value.index_id)['asset_class']!='index':raise ValueError('观察基准必须是指数')
    encoded=value.model_dump(mode='json');encoded.pop('client_key');hashed=fingerprint(encoded)
    with store.user_lock(uid),store.tx(immediate=True) as c:
        previous=c.execute('SELECT * FROM research_experiments WHERE user_id=? AND client_key=?',(uid,value.client_key)).fetchone()
        if previous:
            if previous['fingerprint']!=hashed:raise AuthError('REQUEST_CONFLICT','同一请求标识被不同实验使用',409)
            return dict(previous)
        ident=uuid.uuid4().hex
        # 队列容量检查与实验记录写入使用同一事务，防止并发超额或孤立任务。
        job=enqueue_task(store,uid,'experiment',{'experiment_id':ident,'preferences':get_preferences(store,uid).model_dump()},value.client_key,c=c)
        c.execute('INSERT INTO research_experiments(id,user_id,topic_id,client_key,fingerprint,status,job_id,params_json,benchmark_json,created_at,parent_id) VALUES(?,?,?,?,?,\'queued\',?,?,?,?,?)',(ident,uid,value.topic_id,value.client_key,hashed,job['id'],value.parameters.model_dump_json(),json_text({'index_id':value.index_id}),stamp(),value.parent_id))
    return owned(store,uid,ident)


# 只读取本人的实验，解析参数和基准并附加已保存的结果。
def owned(store,uid,ident,*,summary=False):
    row=store.one('SELECT * FROM research_experiments WHERE id=? AND user_id=?',(ident,uid))
    if not row:raise AuthError('NOT_FOUND','实验不存在',404)
    row['parameters']=json.loads(row.pop('params_json'));row['benchmark']=json.loads(row.pop('benchmark_json'));row.pop('snapshot_json',None)
    if row['result_id']:
        saved=workspace.owned(store,'personal_backtests',uid,row['result_id']);row['result']={**json.loads(saved['result_json']),'data_hash':saved['data_hash'],'strategy_version':saved['strategy_version']}
        if summary:
            row['result']={k:v for k,v in row['result'].items() if k in {'summary','validated','warnings','data_range','data_hash','strategy_version','fee_snapshot','budget_snapshot'}}
    return row


# 取消本人未完成实验及其任务，并区分尚未开始与已中断的执行。
def stop(store,uid,ident):
    row=owned(store,uid,ident,summary=True)
    with store.tx(immediate=True) as c:
        c.execute("UPDATE research_experiments SET status=CASE WHEN started_at IS NULL THEN 'cancelled' ELSE 'interrupted' END,finished_at=?,error_code='CANCELLED' WHERE id=? AND user_id=? AND status IN('queued','running')",(stamp(),ident,uid))
        c.execute("UPDATE personal_jobs SET status='cancelled',error_code='CANCELLED',finished_at=? WHERE id=? AND user_id=? AND status IN('queued','running')",(stamp(),row['job_id'],uid))
    return owned(store,uid,ident,summary=True)


# 固定历史与费用快照，执行回测和基准，在账号仍有效时原子保存结果。
def run(store,job):
    uid=job['user_id'];ident=json.loads(job['snapshot_json'])['experiment_id'];row=owned(store,uid,ident)
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c) or not c.execute("SELECT 1 FROM research_experiments WHERE id=? AND user_id=? AND status='queued'",(ident,uid)).fetchone():return {'error_code':'CANCELLED'}
        count=c.execute('SELECT COUNT(*) FROM research_experiments WHERE user_id=? AND topic_id=? AND started_at IS NOT NULL',(uid,row['topic_id'])).fetchone()[0]
        c.execute("UPDATE research_experiments SET status='running',started_at=?,attempt_no=? WHERE id=? AND user_id=? AND status='queued'",(stamp(),count+1,ident,uid))
    from .preferences import Preferences
    params=Parameters.model_validate(row['parameters']);prefs=Preferences.model_validate(json.loads(job['snapshot_json'])['preferences']);item=market.instrument(store,params.instrument_id)
    bars=[b for b in market.bars_for(store,item['id'],limit=5000) if (not params.start_date or b['ts'][:10]>=params.start_date) and (not params.end_date or b['ts'][:10]<=params.end_date)]
    snap={'bars':bars,'instrument':item,'preferences':prefs.model_dump(),'rule_version':RULE_VERSION,'strategy_version':STRATEGY_VERSION}
    store.execute('UPDATE research_experiments SET snapshot_json=? WHERE id=? AND user_id=?',(json_text(snap),ident,uid))
    try:
        from backend.backtest import evaluate
        result=evaluate(bars,params,item,prefs.fees,prefs.budget)
        result.update(data_snapshot=bars,instrument_snapshot=item,fee_snapshot=prefs.fees.model_dump(),budget_snapshot=prefs.budget,benchmarks=benchmarks(store,bars,params,item,prefs.fees,prefs.budget,result,row['benchmark'].get('index_id')))
        result_id=uuid.uuid4().hex;hashed=fingerprint(bars)
        with store.user_lock(uid),store.tx(immediate=True) as c:
            if not store.active(uid,c=c) or not c.execute("SELECT 1 FROM personal_jobs WHERE id=? AND user_id=? AND status='running'",(job['id'],uid)).fetchone():return {'error_code':'CANCELLED'}
            c.execute('INSERT INTO personal_backtests VALUES(?,?,?,?,?,?,?)',(result_id,uid,stamp(),params.model_dump_json(),json_text(result),hashed,STRATEGY_VERSION))
            c.execute("UPDATE research_experiments SET status='success',result_id=?,finished_at=? WHERE id=? AND user_id=?",(result_id,stamp(),ident,uid))
            c.execute("UPDATE personal_jobs SET status='success',finished_at=?,result_json=? WHERE id=? AND user_id=?",(stamp(),json_text({'experiment_id':ident,'result_id':result_id}),job['id'],uid))
        return {'experiment_id':ident,'result_id':result_id}
    except Exception as exc:
        code='EVIDENCE_OR_DATA_INSUFFICIENT' if isinstance(exc,ValueError) else 'EXPERIMENT_FAILED'
        with store.tx(immediate=True) as c:
            c.execute("UPDATE research_experiments SET status='failed',finished_at=?,error_code=? WHERE id=? AND user_id=? AND status='running'",(stamp(),code,ident,uid))
            c.execute("UPDATE personal_jobs SET status='failed',finished_at=?,error_code=? WHERE id=? AND user_id=? AND status='running'",(stamp(),code,job['id'],uid))
        return {'error_code':code}


# 比较本人多个实验的资金、区间和规则口径，标明差异并保留未完成实验。
def compare(store,uid,ids):
    if not isinstance(ids,list) or not 2<=len(set(ids))<=5:raise ValueError('请选择2至5个本人实验')
    rows=[owned(store,uid,ident) for ident in dict.fromkeys(ids)];notes=[]
    facets={}
    for r in rows:
        result=r.get('result',{});facets[r['id']]={'initial_cash':r['parameters']['initial_cash'],'range':result.get('data_range'),'strategy_version':result.get('strategy_version'),'fees':result.get('fee_snapshot'),'budget':result.get('budget_snapshot'),'strict':r['parameters']['strict'],'train_ratio':r['parameters']['train_ratio'],'validated':result.get('validated')}
    first=facets[rows[0]['id']]
    for r in rows[1:]:
        for key,value in facets[r['id']].items():
            if value!=first[key]:notes.append({'experiment_id':r['id'],'field':key,'reason':'比较口径与首个实验不同，不能直接据此宣称胜出'})
    if any(r['status']!='success' for r in rows):notes.append({'field':'status','reason':'包含未成功或尚未开始的实验，保留在比较表中'})
    for r in rows:
        if r.get('result'):
            result=r['result'];r['result']={k:v for k,v in result.items() if k in {'summary','data_hash','strategy_version','data_range','validated','warnings','fee_snapshot','budget_snapshot'}}
            for k in ('full','in_sample','out_of_sample'):r['result'][k]={'metrics':result[k]['metrics'],'range':result[k].get('range',result.get('data_range'))}
    return {'items':rows,'comparability':notes,'comparable':not notes,'ranking':None,'rule_version':RULE_VERSION}
