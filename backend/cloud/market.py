# 读取共享市场资料，并将个人自选操作限制在所属用户范围内。
"""Shared market cache with explicit, separate personal watchlist membership."""
from __future__ import annotations

import json
import re
import math
from datetime import datetime,timedelta
from decimal import Decimal,ROUND_HALF_UP

from backend import analytics
from backend.budget import budget_check,dec,money
from backend.config import now,timestamp
from backend.collectors import fresh_reasons
from backend.instruments import InstrumentInput,Rules,official_url
from .auth import AuthError
from .collector import queue_names
from .preferences import get_preferences
from .store import json_text,stamp


# 读取共享证券并解析产品规则、核验证据等 JSON 字段。
def instrument(store,ident):
    row=store.one("SELECT * FROM instruments WHERE id=?",(ident,))
    if not row:
        raise AuthError("NOT_FOUND","证券不存在",404)
    row["rules"]=json.loads(row.pop("rules_json"));row["evidence"]=json.loads(row.pop("evidence_json"))
    for k in ("group_name","pinned","watched"):
        row.pop(k,None)
    return row


# 读取指定证券的有效行情，按时间升序返回并解析元数据。
def bars_for(store,ident,timeframe="1d",limit=2000):
    if timeframe not in {"1d","15m","60m"}:
        raise ValueError("不支持的K线周期")
    rows=store.query("SELECT * FROM (SELECT * FROM bars WHERE instrument_id=? AND timeframe=? ORDER BY ts DESC LIMIT ?) ORDER BY ts",(ident,timeframe,min(limit,5000)))
    for b in rows:
        b["metadata"]=json.loads(b.pop("metadata_json"))
    return rows


# 根据共享交易日历构建带核验状态的市场时间上下文。
def calendar(store,at=None):
    at=at or now();day=at.date().isoformat()
    bounds=store.one("SELECT MIN(day) first,MAX(day) last FROM calendar")
    known=bool(bounds and bounds["first"] and bounds["first"]<=day<=bounds["last"])
    trading=bool(store.one("SELECT day FROM calendar WHERE day=?",(day,))) if known else None
    latest=store.one("SELECT MAX(day) day FROM calendar WHERE day<? OR(day=? AND ?>='15:00')",(day,day,at.strftime("%H:%M")))
    return {"date":day,"timezone":"Asia/Shanghai","is_trading_day":trading,"calendar_verified":known,"last_completed_session":latest["day"] if latest else None,"note":"日线为已完成交易日收盘数据" if known else "交易日历未覆盖当前日期，禁止按工作日推算交易日"}


# 结合证券证据与个人偏好，计算候选准入的风险障碍。
def risk_reasons(item,prefs,at=None):
    at=at or now();errors=[]
    if item["market"] not in {"SH","SZ"} or item["asset_class"] not in {"stock","etf"}:
        errors.append("仅供观察，不支持普通模拟买入")
    if item["status"]!="active":
        errors.append({"unknown":"交易状态未核验","suspended":"停牌","delisted":"已退市"}.get(item["status"],"交易状态不允许买入"))
    if item["risk_flag"]!="clear" or "ST" in item["name"].upper() or "退" in item["name"]:
        errors.append("风险警示或公告尚未核验")
    try:
        verified=datetime.fromisoformat(item["verified_at"])
        if verified.tzinfo is None or verified>at or at-verified>timedelta(hours=prefs.max_risk_age_hours):
            errors.append("风险核验已过期或时间无效")
    except (ValueError,TypeError):
        errors.append("缺少有效风险核验时间")
    if not all(official_url(item["evidence"].get(k,"")) for k in ("listing_url","status_url","announcement_url")):
        errors.append("缺少上市、停复牌或公告的官方核验证据")
    if not item["rules"].get("verified") or not official_url(item["rules"].get("source_url","")):
        errors.append("产品交易规则未核验")
    return list(dict.fromkeys(errors))


# 确保证券身份合法后加入当前用户自选，避免改动其他用户清单。
def add_watch(store,uid,value:InstrumentInput):
    if value.market in {"SH","SZ","FUND"} and not re.fullmatch(r"\d{6}",value.symbol):
        raise ValueError("境内证券代码须为六位数字")
    ident=f"{value.market}:{value.symbol}"
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("ACCOUNT_DISABLED","账号不可用",403)
        if c.execute("SELECT COUNT(*) FROM user_watchlist WHERE user_id=?",(uid,)).fetchone()[0]>=300 and not c.execute("SELECT 1 FROM user_watchlist WHERE user_id=? AND instrument_id=?",(uid,ident)).fetchone():
            raise ValueError("个人自选最多300只")
        # 用户导入的名称只用于个人输入，不能覆盖共享证券的权威名称。
        rules=Rules(price_tick="0.001" if value.asset_class=="etf" else "0.01")
        c.execute("INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,'',0,?)",(ident,value.symbol,value.market,"名称待核验",value.asset_class,rules.model_dump_json()))
        c.execute("INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,0,?) ON CONFLICT(user_id,instrument_id) DO UPDATE SET group_name=excluded.group_name",(uid,ident,value.group_name,stamp()))
        queue_names(store,c=c)
    return {**instrument(store,ident),"group_name":value.group_name,"pinned":False}


# 整批校验并原子导入个人自选，保留已有置顶等个人状态。
def import_watch(store,uid,rows):
    validated={}
    for row in rows:
        value=InstrumentInput.model_validate({k:v for k,v in row.items() if k in InstrumentInput.model_fields})
        if value.market in {"SH","SZ","FUND"} and not re.fullmatch(r"\d{6}",value.symbol):
            raise ValueError("境内证券代码须为六位数字")
        pinned=row.get("pinned",False)
        if pinned not in (True,False,0,1,"0","1","true","false","True","False"):
            raise ValueError("置顶字段格式无效")
        validated[f"{value.market}:{value.symbol}"]=(value,pinned in (True,1,"1","true","True"))
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("ACCOUNT_DISABLED","账号不可用",403)
        existing={r[0] for r in c.execute("SELECT instrument_id FROM user_watchlist WHERE user_id=?",(uid,))}
        if len(existing|set(validated))>300:
            raise ValueError("个人自选最多300只；本次未导入任何证券")
        for ident,(v,pinned) in validated.items():
            rules=Rules(price_tick="0.001" if v.asset_class=="etf" else "0.01")
            c.execute("INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,'',0,?)",(ident,v.symbol,v.market,"名称待核验",v.asset_class,rules.model_dump_json()))
            c.execute("INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,?,?) ON CONFLICT(user_id,instrument_id) DO UPDATE SET group_name=excluded.group_name,pinned=excluded.pinned",(uid,ident,v.group_name,int(pinned),stamp()))
        queue_names(store,c=c)
    return {"imported":len(validated),"duplicates":len(rows)-len(validated)}


# 只更新本人自选的分组和置顶信息。
def patch_watch(store,uid,ident,payload):
    if set(payload)-{"group_name","pinned"}:
        raise ValueError("这里只能修改个人分组和置顶")
    if "pinned" in payload and not isinstance(payload["pinned"],bool):
        raise ValueError("置顶值必须为布尔值")
    with store.user_lock(uid),store.tx(immediate=True) as c:
        row=c.execute("SELECT * FROM user_watchlist WHERE user_id=? AND instrument_id=?",(uid,ident)).fetchone()
        if not row:
            raise AuthError("NOT_FOUND","记录不存在",404)
        group=str(payload.get("group_name",row["group_name"])).strip()
        if not group or len(group)>40:
            raise ValueError("分组名称须为1至40字")
        c.execute("UPDATE user_watchlist SET group_name=?,pinned=? WHERE user_id=? AND instrument_id=?",(group,int(bool(payload.get("pinned",row["pinned"]))),uid,ident))
    return {"success":True}


# 校验并更新共享证券的官方证据，供管理员接口调用。
def verify(store,ident,payload):
    instrument(store,ident)
    if payload.get("status") not in {"active","suspended","delisted","unknown"} or payload.get("risk_flag") not in {"clear","st","warning","unknown"}:
        raise ValueError("交易状态或风险标记不合法")
    evidence=payload.get("evidence",{})
    if not all(official_url(evidence.get(k,"")) for k in ("listing_url","status_url","announcement_url")) or not str(evidence.get("review_note","")).strip():
        raise ValueError("需要三项官方 HTTPS 证据及人工核验结论")
    rules=Rules.model_validate(payload.get("rules"))
    if not rules.verified or not official_url(rules.source_url):
        raise ValueError("需要已核验的交易规则及官方来源")
    name=str(payload.get("name","")).strip()
    if not name or len(name)>80:
        raise ValueError("需要最新官方名称")
    with store.tx(immediate=True) as c:
        c.execute("UPDATE instruments SET name=?,status=?,risk_flag=?,verified_at=?,source_url=?,rules_json=?,evidence_json=? WHERE id=?",(name,payload["status"],payload["risk_flag"],timestamp(),evidence["listing_url"],rules.model_dump_json(),json_text(evidence),ident))
        c.execute("INSERT INTO risk_events(instrument_id,event_type,severity,title,source_url,verified_at,payload_json) VALUES(?,?,?,?,?,?,?)",(ident,"manual_official_review","info" if payload["risk_flag"]=="clear" else "warning",str(evidence["review_note"])[:500],evidence["announcement_url"],timestamp(),json_text(payload)))
    return instrument(store,ident)


# 组合共享行情、技术指标、规则和本人研究相关信息。
def detail(store,uid,ident,timeframe="1d",prefs=None):
    prefs=prefs or get_preferences(store,uid);item=instrument(store,ident)
    bars=bars_for(store,ident,timeframe);last=bars[-1] if bars else None
    observed=store.one("SELECT payload_json FROM market_quotes WHERE instrument_id=?",(ident,))
    item.update(bars=bars,indicators=analytics.indicators(bars),quote=last,observation=json.loads(observed["payload_json"]) if observed else None,change_pct=(last["close"]/bars[-2]["close"]-1)*100 if len(bars)>1 else None)
    item["exclusion_reasons"]=risk_reasons(item,prefs)+fresh_reasons(last,calendar(store))
    item["budget"]=None
    if last and item["asset_class"] in {"stock","etf"}:
        item["budget"]=budget_check(last["close"],item["rules"],item["asset_class"],prefs.fees,prefs.budget)
    return item


# 校验本人声明的持仓价和数量。两者同时为空表示未记录持仓，只填一项则拒绝。
def normalize_holding(cost,quantity):
    if cost is None and quantity is None:return None,None
    if cost is None or quantity is None or isinstance(quantity,bool) or type(quantity) is not int:
        raise ValueError("持仓价和持仓数量须同时填写")
    price=dec(cost);scale=price.as_tuple().exponent
    if not price.is_finite() or price<=0 or price>Decimal("1000000") or scale<-4:
        raise ValueError("持仓价须大于0、不超过1000000，且最多四位小数")
    if not 1<=quantity<=100_000_000:raise ValueError("持仓数量须为1至100000000的整数")
    return format(price,"f"),quantity


def _points(current,base):
    if current is None or base is None or base==0:return None
    return float((((current/base)-1)*Decimal(100)).quantize(Decimal("0.0001"),rounding=ROUND_HALF_UP))


# 用已存日K现算持仓金额和收益，不写行情，也不把缺失价格补成零。
def holding_view(cost_price,quantity,bars):
    cost,qty=None,None
    if cost_price not in (None,"") and isinstance(quantity,int) and not isinstance(quantity,bool) and quantity>0:
        cost,qty=dec(cost_price),quantity
    last=dec(bars[-1]["close"]) if bars else None
    previous=dec(bars[-2]["close"]) if len(bars)>1 else None
    change=_points(last,previous)
    return {"cost_price":format(cost,"f") if cost is not None else None,"quantity":qty,
            "market_price":float(last) if last is not None else None,"prev_close":float(previous) if previous is not None else None,
            "change_pct":change,"market_value":float(money(last*qty)) if last is not None and qty else None,
            "daily_pnl":float(money((last-previous)*qty)) if last is not None and previous is not None and qty else None,
            "daily_return_pct":change if change is not None and qty else None,
            "total_pnl":float(money((last-cost)*qty)) if last is not None and cost is not None and qty else None,
            "total_return_pct":_points(last,cost) if cost is not None else None}


# 已记录持仓排在置顶收藏之前，同级保持分组和代码顺序。
def watch_priority(row):
    position=row.get("position") or {}
    quantity=position.get("quantity")
    held=position.get("cost_price") not in (None,"") and isinstance(quantity,int) and not isinstance(quantity,bool) and quantity>0
    if held and row.get("pinned"):return 0
    if held:return 1
    if row.get("pinned"):return 2
    return 3


# 将本人自选与共享报价、预算及风险状态组合为列表视图。
def watch_rows(store,uid):
    prefs=get_preferences(store,uid);out=[]
    for r in store.query("SELECT * FROM user_watchlist WHERE user_id=? ORDER BY pinned DESC,group_name,instrument_id",(uid,)):
        item=detail(store,uid,r["instrument_id"],prefs=prefs)
        bars=item.pop("bars");out.append({**item,"group_name":r["group_name"],"pinned":bool(r["pinned"]),"position":holding_view(r["cost_price"],r["quantity"],bars)})
    out.sort(key=watch_priority)
    return out


# 读取已保存的市场观察报价，并保留报价时间和来源信息。
def quotes(store):
    out=[]
    for row in store.query("SELECT payload_json FROM market_quotes"):
        value=json.loads(row["payload_json"])
        try:
            value["stale"]=(now()-datetime.fromisoformat(value["quote_time"])).total_seconds()>3600
        except (KeyError,ValueError,TypeError):
            value["stale"]=True
        out.append(value)
    return out


# 校验整批外部行情后写入共享行情表。
def import_bars(store,ident,payload):
    instrument(store,ident);bars=payload.get("bars");tf=payload.get("timeframe","1d")
    if tf not in {"1d","15m","60m"} or not isinstance(bars,list) or not 1<=len(bars)<=10000:
        raise ValueError("须提供合法周期及1至10000根K线")
    from urllib.parse import urlsplit
    normalized=[]
    for b in bars:
        ts=datetime.fromisoformat(b["ts"])
        v={k:float(b[k]) for k in ("open","high","low","close","volume")}
        if ts.tzinfo is None or ts>now() or not all(math.isfinite(x) for x in v.values()) or min(v[k] for k in ("open","high","low","close"))<=0 or v["volume"]<0 or v["low"]>min(v["open"],v["close"]) or v["high"]<max(v["open"],v["close"]):
            raise ValueError("K线价格、数量或时间不合法")
        if b.get("adjustment") not in {"none","qfq","hfq"} or not b.get("source") or urlsplit(b.get("source_url","")).scheme not in {"http","https"}:
            raise ValueError("缺少价格复权口径或来源")
        amount=b.get("amount")
        if amount is not None and (not math.isfinite(float(amount)) or float(amount)<0):
            raise ValueError("成交额无效")
        normalized.append((ident,tf,b["ts"],*[v[k] for k in ("open","high","low","close","volume")],amount,b["adjustment"],str(b["source"])[:150],b["source_url"],timestamp(),"valid",json_text(b.get("metadata",{}))))
    if len({r[2] for r in normalized})!=len(normalized):
        raise ValueError("K线时间重复")
    with store.tx(immediate=True) as c:
        c.executemany("INSERT INTO bars VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id,timeframe,ts) DO UPDATE SET open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,amount=excluded.amount,adjustment=excluded.adjustment,source=excluded.source,source_url=excluded.source_url,fetched_at=excluded.fetched_at,quality=excluded.quality,metadata_json=excluded.metadata_json",normalized)
    return {"imported":len(bars)}
