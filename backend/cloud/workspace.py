# 组合个人研究工作区事实，统一校验报告、实验与模拟资源的用户归属。
from __future__ import annotations

import hashlib
import json
import uuid

from backend import analytics,backtest
from backend.budget import budget_check
from backend.collectors import fresh_reasons
from backend.config import Fees,STRATEGY_VERSION,now,timestamp
from . import VERSION
from backend.research import attach_full_evidence,resample_weekly
from backend.report_facts import attach_metals,closing_followup,rank_candidates
from . import market
from .auth import AuthError
from .preferences import get_preferences,Preferences
from .store import json_text,stamp


# 仅从允许的表中按资源及用户共同查询，拒绝访问其他用户对象。
def owned(store,table,uid,ident):
    if table not in {"personal_reports","personal_jobs","personal_backtests","sim_accounts","sim_orders","sim_price_rules"}:
        raise ValueError("不支持的个人资源")
    row=store.one(f"SELECT * FROM {table} WHERE id=? AND user_id=?",(ident,uid))
    if not row:
        raise AuthError("NOT_FOUND","记录不存在",404)
    return row


# 列出本人报告的索引、版本、摘要和生成状态。
def report_list(store,uid,mode="",q=""):
    rows=store.query("SELECT id,series_id,version,parent_id,mode,report_date,generated_at,payload_json FROM personal_reports WHERE user_id=? ORDER BY generated_at DESC",(uid,))
    out=[]
    for row in rows:
        payload=json.loads(row.pop("payload_json"))
        if (mode and row["mode"]!=mode) or (q and q.casefold() not in json_text(payload).casefold()):
            continue
        out.append({**row,"summary":payload.get("summary","研究归档"),"ai_status":payload.get("ai_status","fallback"),"ai_error":payload.get("ai_error"),"candidate_count":len(payload.get("candidates",[]))})
    return out


# 读取归属已核对的报告，并解析完整事实与版本信息。
def report_detail(store,uid,ident):
    row=owned(store,"personal_reports",uid,ident)
    payload=json.loads(row.pop("payload_json"));payload.pop("source_facts",None)
    row.pop("markdown_path");row.pop("html_path")
    versions=store.query("SELECT id,version,parent_id,generated_at FROM personal_reports WHERE user_id=? AND series_id=? ORDER BY version",(uid,row["series_id"]))
    return {**row,"payload":payload,"versions":versions}


# 从共享行情和个人设置构建研究事实，补充本人模拟与实验摘要。
def facts(store,uid,mode,prefs=None,*,scheduled_for=None,context_update=None,market_at=None,market_update=None):
    from .simulation import portfolio
    prefs=prefs or get_preferences(store,uid)
    ctx=market.calendar(store,market_at) if market_at else market.calendar(store)
    candidates=[];excluded=[];sources=[];watch=[];aux=[];observations=[]
    histories, latest_bars = {}, {}
    for r in store.query("SELECT instrument_id FROM user_watchlist WHERE user_id=? ORDER BY instrument_id",(uid,)):
        item=market.instrument(store,r["instrument_id"])
        watch.append({"instrument_id":item["id"],"symbol":item["symbol"],"market":item["market"],"asset_class":item["asset_class"],"official_name":item["name"]})
        bars=market.bars_for(store,item["id"])
        if market_at and item["market"] in {"SH","SZ"}:
            bars=[b for b in bars if b["ts"][:10] < ctx["date"]]
        last=bars[-1] if bars else None
        latest_bars[item["id"]] = last
        if item["market"] not in {"SH","SZ"} or item["asset_class"] not in {"stock","etf"}:
            if last:
                aux.append({"instrument_id":item["id"],"name":item["name"],"symbol":item["symbol"],"market":item["market"],"price":last["close"],"quote_time":last["ts"],"source_url":last["source_url"]})
            continue
        reasons=market.risk_reasons(item,prefs)+fresh_reasons(last,ctx);stats=analytics.indicators(bars);reasons+=stats["missing"]
        estimate=None;sid=None
        if last:
            sid="s"+str(len(sources)+1)
            sources.append({"id":sid,"instrument_id":item["id"],"url":last["source_url"],"source":last["source"],"quote_time":last["ts"],"collected_at":last["fetched_at"],"adjustment":last["adjustment"]})
            try:
                estimate=budget_check(last["close"],item["rules"],item["asset_class"],prefs.fees,prefs.budget)
                if not estimate["within_budget"]:
                    reasons.append("最低合法数量及预计买入费用超过单只预算")
            except ValueError as exc:
                reasons.append(str(exc))
            if last.get("amount") is None or last["amount"]<prefs.min_daily_amount:
                reasons.append("成交额不足或流动性字段缺失")
        points=analytics.score(bars,stats["latest"]) if bars else 0
        if points<60:
            reasons.append("趋势与成交量评分未达到60分（非上涨概率）")
        if reasons:
            if mode=="full" and last and not fresh_reasons(last,ctx) and stats["latest"]:
                observations.append({"instrument_id":item["id"],"name":item["name"],"score":points,"source_ids":[sid],"indicators":stats["latest"]})
            excluded.append({"instrument_id":item["id"],"name":item["name"],"reasons":list(dict.fromkeys(reasons)),"price":last["close"] if last else None});continue
        c={"instrument_id":item["id"],"symbol":item["symbol"],"name":item["name"],"asset_class":item["asset_class"],"price":last["close"],"quote_time":last["ts"],"source_url":last["source_url"],"source_ids":[sid],"quantity":estimate["max_quantity"],"budget":estimate,"score":points,"indicators":stats["latest"],"status":"verified","rules":item["rules"],"evidence":item["evidence"],"verified_at":item["verified_at"],"rationale":"趋势、成交量与预算规则同时通过。","counterargument":"历史形态不代表未来表现，费用和跳空会改变可执行性。","invalidation":"风险、行情或预算条件变化时失效。"}
        if mode=="full":
            c["multi_timeframe"]={tf:analytics.indicators(market.bars_for(store,item["id"],tf))["latest"] for tf in ("15m","60m")};c["weekly"]=analytics.indicators(resample_weekly(bars))["latest"]
        candidates.append(c)
        histories[item["id"]] = bars[-21:]
    financial_rows = store.query("""SELECT * FROM research_context WHERE category='financials'
        AND instrument_id IN (SELECT instrument_id FROM user_watchlist WHERE user_id=?)""", (uid,))
    def read_bars(ident, limit):
        return market.bars_for(store, ident, limit=limit)
    rank_candidates(candidates, histories, read_bars, financial_rows, sources, ctx, prefs.fees)
    for c in candidates[3:]:
        excluded.append({"instrument_id":c["instrument_id"],"name":c["name"],"reasons":["本次排名未进入最多3只的研究名额"]})
    value={"mode":mode,"report_date":ctx["date"],"generated_at":timestamp(),"calendar":ctx,"budget_limit":prefs.budget,"candidate_limit":3,"candidates":candidates[:3],"excluded":excluded,"auxiliary_markets":aux,"watchlist":watch,"sources":sources,"fees":prefs.fees.model_dump(),"strategy_version":STRATEGY_VERSION,"app_version":VERSION,"missing_data":[],"simulation":portfolio(store,uid),"backtests":[backtest.report_summary(r) for r in store.query("SELECT * FROM personal_backtests WHERE user_id=? ORDER BY created_at DESC LIMIT 5",(uid,))]}
    attach_metals(value, market.quotes(store), now())
    value["simulation"]["recent_fills"]=[o for o in value["simulation"]["orders"] if o["status"]=="filled"][:30]
    if scheduled_for:
        value["scheduled_for"]=scheduled_for
        if scheduled_for!=ctx["date"]:
            value["missing_data"].append("此为恢复后补跑的周度复盘，研究日期、报价及采集时间均按实际记录展示。")
    if not ctx["calendar_verified"]:
        value["missing_data"].append("交易日历未覆盖报告日期")
    if market_update and market_update!="success":
        value["missing_data"].append("本时段行情刷新尚未完成或存在失败，报告仅引用已保存的可核验报价。")
    if mode=="closing":
        morning = store.one("""SELECT id,generated_at,payload_json FROM personal_reports
            WHERE user_id=? AND mode='standard' AND report_date=? ORDER BY version LIMIT 1""", (uid, ctx["date"]))
        signals = store.query("""SELECT r.id price_rule_id,r.instrument_id,e.result,e.reason,o.status order_status
            FROM sim_price_rule_events e JOIN sim_price_rules r ON r.id=e.price_rule_id AND r.user_id=e.user_id
            LEFT JOIN sim_orders o ON o.id=e.order_id AND o.user_id=e.user_id
            WHERE e.user_id=? AND e.bar_date=? ORDER BY r.instrument_id,r.id""", (uid, ctx["date"]))
        value["closing_followup"] = closing_followup(value, morning, latest_bars, read_bars, signals)
    if mode=="full":
        observations.sort(key=lambda item:(-item["score"],item["instrument_id"]))
        value["observations"]=observations[:max(0,3-len(value["candidates"]))]
        for item in value["observations"]:
            ident=item["instrument_id"]
            item["multi_timeframe"]={tf:analytics.indicators(market.bars_for(store,ident,tf))["latest"] for tf in ("15m","60m")}
            item["weekly"]=analytics.indicators(resample_weekly(market.bars_for(store,ident)))["latest"]
        context_rows=store.query("SELECT * FROM research_context WHERE instrument_id IS NULL OR instrument_id IN (SELECT instrument_id FROM user_watchlist WHERE user_id=?)",(uid,))
        announcements=store.query("""SELECT instrument_id,title,source_url,verified_at,severity,payload_json FROM risk_events
            WHERE event_type='announcement' AND instrument_id IN (SELECT instrument_id FROM user_watchlist WHERE user_id=?)
            ORDER BY verified_at DESC,id DESC LIMIT 40""",(uid,))
        attach_full_evidence(value,context_rows,announcements)
        if context_update and context_update!="success":
            value["missing_data"].append("本次深度资料未全部刷新，报告引用已归档证据及其采集时间。")
    from backend.idea_flow import attach_cloud
    attach_cloud(store,uid,value)
    value["snapshot_id"]=hashlib.sha256(json_text(value).encode()).hexdigest()
    from .research import readiness
    value['readiness']=readiness(store,uid,size=50,persist=False)
    value['snapshot_id']=hashlib.sha256(json_text({k:v for k,v in value.items() if k!='snapshot_id'}).encode()).hexdigest()
    return value


# 在本人范围执行回测，归档行情、产品规则、费用和结果快照。
def create_backtest(store,uid,params):
    item=market.instrument(store,params.instrument_id)
    if item["market"] not in {"SH","SZ"} or item["asset_class"] not in {"stock","etf"}:
        raise ValueError("仅支持境内股票和ETF的历史撮合实验")
    bars=[b for b in market.bars_for(store,item["id"],limit=5000) if (not params.start_date or b["ts"][:10]>=params.start_date) and (not params.end_date or b["ts"][:10]<=params.end_date)]
    prefs=get_preferences(store,uid);result=backtest.evaluate(bars,params,item,prefs.fees,prefs.budget)
    hashed=hashlib.sha256(json_text(bars).encode()).hexdigest()
    result.update(data_snapshot=bars,instrument_snapshot=item,fee_snapshot=prefs.fees.model_dump(),budget_snapshot=prefs.budget)
    ident=uuid.uuid4().hex
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("ACCOUNT_DISABLED","账号不可用",403)
        c.execute("INSERT INTO personal_backtests VALUES(?,?,?,?,?,?,?)",(ident,uid,stamp(),params.model_dump_json(),json_text(result),hashed,STRATEGY_VERSION))
    return {"id":ident,"data_hash":hashed,"strategy_version":STRATEGY_VERSION,**result}


# 用本人已保存快照重演实验，比较结果以检查可复现性。
def reproduce(store,uid,ident):
    row=owned(store,"personal_backtests",uid,ident);saved=json.loads(row["result_json"])
    if row["strategy_version"]!=STRATEGY_VERSION or hashlib.sha256(json_text(saved["data_snapshot"]).encode()).hexdigest()!=row["data_hash"]:
        raise ValueError("回测版本或快照校验失败")
    result=backtest.evaluate(saved["data_snapshot"],backtest.Parameters.model_validate_json(row["params_json"]),saved["instrument_snapshot"],Fees.model_validate(saved["fee_snapshot"]),saved["budget_snapshot"])
    identical=all(result[k]==saved[k] for k in ('full','out_of_sample','cost_sensitivity'))
    benchmark_identical=None
    if saved.get('benchmarks'):
        from .experiments import benchmarks
        index=saved['benchmarks'].get('index_snapshot')
        recomputed=benchmarks(store,saved['data_snapshot'],backtest.Parameters.model_validate_json(row['params_json']),saved['instrument_snapshot'],Fees.model_validate(saved['fee_snapshot']),saved['budget_snapshot'],result,index['instrument']['id'] if index else None,index)
        benchmark_identical=recomputed==saved['benchmarks'];identical=identical and benchmark_identical
    return {'identical':identical,'benchmark_identical':benchmark_identical,'data_hash':row['data_hash'],'summary':result['summary']}
