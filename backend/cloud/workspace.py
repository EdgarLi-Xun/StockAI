from __future__ import annotations

import hashlib
import json
import uuid

from backend import analytics,backtest
from backend.budget import budget_check
from backend.collectors import fresh_reasons
from backend.config import Fees,STRATEGY_VERSION,timestamp
from . import VERSION
from backend.research import resample_weekly
from . import market
from .auth import AuthError
from .preferences import get_preferences,Preferences
from .store import json_text,stamp


def owned(store,table,uid,ident):
    if table not in {"personal_reports","personal_jobs","personal_backtests","sim_accounts","sim_orders"}:
        raise ValueError("不支持的个人资源")
    row=store.one(f"SELECT * FROM {table} WHERE id=? AND user_id=?",(ident,uid))
    if not row:
        raise AuthError("NOT_FOUND","记录不存在",404)
    return row


def report_list(store,uid,mode="",q=""):
    rows=store.query("SELECT id,series_id,version,parent_id,mode,report_date,generated_at,payload_json FROM personal_reports WHERE user_id=? ORDER BY generated_at DESC",(uid,))
    out=[]
    for row in rows:
        payload=json.loads(row.pop("payload_json"))
        if (mode and row["mode"]!=mode) or (q and q.casefold() not in json_text(payload).casefold()):
            continue
        out.append({**row,"summary":payload.get("summary","研究归档"),"ai_status":payload.get("ai_status","fallback"),"ai_error":payload.get("ai_error"),"candidate_count":len(payload.get("candidates",[]))})
    return out


def report_detail(store,uid,ident):
    row=owned(store,"personal_reports",uid,ident)
    payload=json.loads(row.pop("payload_json"));payload.pop("source_facts",None)
    row.pop("markdown_path");row.pop("html_path")
    versions=store.query("SELECT id,version,parent_id,generated_at FROM personal_reports WHERE user_id=? AND series_id=? ORDER BY version",(uid,row["series_id"]))
    return {**row,"payload":payload,"versions":versions}


def facts(store,uid,mode,prefs=None):
    from .simulation import portfolio
    prefs=prefs or get_preferences(store,uid);ctx=market.calendar(store)
    candidates=[];excluded=[];sources=[];watch=[];aux=[]
    for r in store.query("SELECT instrument_id FROM user_watchlist WHERE user_id=? ORDER BY instrument_id",(uid,)):
        item=market.instrument(store,r["instrument_id"])
        watch.append({"instrument_id":item["id"],"symbol":item["symbol"],"market":item["market"],"asset_class":item["asset_class"],"official_name":item["name"]})
        bars=market.bars_for(store,item["id"]);last=bars[-1] if bars else None
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
            excluded.append({"instrument_id":item["id"],"name":item["name"],"reasons":list(dict.fromkeys(reasons)),"price":last["close"] if last else None});continue
        c={"instrument_id":item["id"],"symbol":item["symbol"],"name":item["name"],"price":last["close"],"quote_time":last["ts"],"source_url":last["source_url"],"source_ids":[sid],"quantity":estimate["max_quantity"],"budget":estimate,"score":points,"indicators":stats["latest"],"status":"verified","rules":item["rules"],"evidence":item["evidence"],"verified_at":item["verified_at"],"rationale":"趋势、成交量与预算规则同时通过。","counterargument":"历史形态不代表未来表现，费用和跳空会改变可执行性。","invalidation":"风险、行情或预算条件变化时失效。"}
        if mode=="full":
            c["multi_timeframe"]={tf:analytics.indicators(market.bars_for(store,item["id"],tf))["latest"] for tf in ("15m","60m")};c["weekly"]=analytics.indicators(resample_weekly(bars))["latest"]
        candidates.append(c)
    candidates.sort(key=lambda c:(-c["score"],c["instrument_id"]))
    for c in candidates[3:]:
        excluded.append({"instrument_id":c["instrument_id"],"name":c["name"],"reasons":["本次排名未进入最多3只的研究名额"]})
    metals=[q for q in market.quotes(store) if q.get("id","").startswith(("FX:","SHFE:"))]
    for q in metals:
        sources.append({"id":"s"+str(len(sources)+1),"instrument_id":q["id"],"url":q["source_url"],"source":q["source"],"quote_time":q["quote_time"],"collected_at":q["fetched_at"]})
    value={"mode":mode,"report_date":ctx["date"],"generated_at":timestamp(),"calendar":ctx,"budget_limit":prefs.budget,"candidate_limit":3,"candidates":candidates[:3],"excluded":excluded,"metals":metals,"auxiliary_markets":aux,"watchlist":watch,"sources":sources,"fees":prefs.fees.model_dump(),"strategy_version":STRATEGY_VERSION,"app_version":VERSION,"missing_data":["无结构化权威证据时，不推断宏观或基本面结论。"],"simulation":portfolio(store,uid),"backtests":[{"id":r["id"],"created_at":r["created_at"],"result":json.loads(r["result_json"])["summary"]} for r in store.query("SELECT * FROM personal_backtests WHERE user_id=? ORDER BY created_at DESC LIMIT 5",(uid,))]}
    value["simulation"]["recent_fills"]=[o for o in value["simulation"]["orders"] if o["status"]=="filled"][:30]
    if not ctx["calendar_verified"]:
        value["missing_data"].append("交易日历未覆盖报告日期")
    if mode=="full":
        value["deep_context"]=[{**{k:r[k] for k in ("id","category","instrument_id","source","source_url","as_of","fetched_at")},"data":json.loads(r["payload_json"])} for r in store.query("SELECT * FROM research_context ORDER BY category,instrument_id")]
        value["announcements"]=store.query("SELECT instrument_id,title,source_url,verified_at,severity FROM risk_events WHERE event_type='announcement' ORDER BY verified_at DESC LIMIT 40")
    value["snapshot_id"]=hashlib.sha256(json_text(value).encode()).hexdigest()
    return value


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


def reproduce(store,uid,ident):
    row=owned(store,"personal_backtests",uid,ident);saved=json.loads(row["result_json"])
    if row["strategy_version"]!=STRATEGY_VERSION or hashlib.sha256(json_text(saved["data_snapshot"]).encode()).hexdigest()!=row["data_hash"]:
        raise ValueError("回测版本或快照校验失败")
    result=backtest.evaluate(saved["data_snapshot"],backtest.Parameters.model_validate_json(row["params_json"]),saved["instrument_snapshot"],Fees.model_validate(saved["fee_snapshot"]),saved["budget_snapshot"])
    return {"identical":all(result[k]==saved[k] for k in ("full","out_of_sample","cost_sensitivity")),"data_hash":row["data_hash"],"summary":result["summary"]}
