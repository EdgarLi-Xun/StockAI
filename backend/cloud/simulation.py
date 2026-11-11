# 在明确用户归属下管理虚拟资金、委托、撮合和持仓快照。
"""Owned virtual ledgers. All account/order IDs are resolved together with the user."""
from __future__ import annotations

import json
import math
import uuid
from datetime import datetime
from decimal import Decimal

from backend.backtest import executable_price
from backend.budget import budget_check,dec,money,transaction_fees
from backend.collectors import fresh_reasons
from backend.config import Fees,now,timestamp
from backend.simulation import OrderInput
from . import market
from .auth import AuthError
from .preferences import get_preferences
from .store import json_text
from .workspace import owned


# 为本人创建具有明确初始虚拟本金的模拟账户及首笔流水。
def create_account(store,uid,name,initial_cash):
    name=str(name).strip();cash=money(initial_cash)
    if not name or len(name)>80 or not 0<cash<=100000000:
        raise ValueError("需要1至80字的账户名称及有效虚拟初始资金")
    ident=uuid.uuid4().hex
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("ACCOUNT_DISABLED","账号不可用",403)
        c.execute("INSERT INTO sim_accounts VALUES(?,?,?,?,?,?)",(ident,uid,name,str(cash),str(cash),timestamp()))
        c.execute("INSERT INTO sim_ledger(user_id,account_id,ts,delta,balance,description) VALUES(?,?,?,?,?,?)",(uid,ident,timestamp(),str(cash),str(cash),"设立虚拟账户，非真实资金"))
    return owned(store,"sim_accounts",uid,ident)


# 在用户锁内校验所有权、预算与可用资源，并按客户端键幂等提交委托。
def order(store,uid,p:OrderInput):
    owned(store,"sim_accounts",uid,p.account_id)
    if getattr(store,'sync_service',None):store.sync_service.account_guard(uid,p.account_id)
    existing=store.one("SELECT * FROM sim_orders WHERE user_id=? AND client_key=?",(uid,p.client_key))
    if existing:
        _same_order(existing,p)
        return existing
    item=market.instrument(store,p.instrument_id);prefs=get_preferences(store,uid)
    if p.side not in {"buy","sell"}:
        raise ValueError("委托方向不合法")
    if p.side=="buy":
        recent=market.bars_for(store,item["id"],limit=1)
        reasons=market.risk_reasons(item,prefs)+fresh_reasons(recent[-1] if recent else None,market.calendar(store))
        if reasons:
            raise ValueError("；".join(reasons))
    elif not item["rules"].get("verified"):
        raise ValueError("卖出前需核验产品规则")
    estimate=budget_check(p.limit_price,item["rules"],item["asset_class"],prefs.fees,prefs.budget,p.quantity)
    if p.side=="buy" and not estimate["within_budget"]:
        raise ValueError("模拟买入总成本超过单只含费预算")
    if p.stop_loss is not None and (p.side!="buy" or dec(p.stop_loss)>=dec(p.limit_price)):
        raise ValueError("止损价须低于买入限价")
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("ACCOUNT_DISABLED","账号不可用",403)
        existing=c.execute("SELECT * FROM sim_orders WHERE user_id=? AND client_key=?",(uid,p.client_key)).fetchone()
        if existing:
            _same_order(existing,p)
            return dict(existing)
        a=c.execute("SELECT * FROM sim_accounts WHERE id=? AND user_id=?",(p.account_id,uid)).fetchone()
        if not a:
            raise AuthError("NOT_FOUND","记录不存在",404)
        if p.side=="buy":
            reserved=sum((dec(r[0]) for r in c.execute("SELECT cost FROM sim_orders WHERE user_id=? AND account_id=? AND side='buy' AND status='pending'",(uid,p.account_id))),Decimal(0))
            exposure=sum((dec(r[0]) for r in c.execute("SELECT cost FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0",(uid,p.account_id,p.instrument_id))),Decimal(0))
            waiting=sum((dec(r[0]) for r in c.execute("SELECT cost FROM sim_orders WHERE user_id=? AND account_id=? AND instrument_id=? AND side='buy' AND status='pending'",(uid,p.account_id,p.instrument_id))),Decimal(0))
            if dec(a["cash"])-reserved<dec(estimate["total"]) or exposure+waiting+dec(estimate["total"])>dec(prefs.budget):
                raise ValueError("虚拟现金不足或同一证券持仓与待买合计超过含费预算")
        else:
            qty=c.execute("SELECT COALESCE(SUM(quantity),0) FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=?",(uid,p.account_id,p.instrument_id)).fetchone()[0]
            reserved=c.execute("SELECT COALESCE(SUM(quantity),0) FROM sim_orders WHERE user_id=? AND account_id=? AND instrument_id=? AND side='sell' AND status='pending'",(uid,p.account_id,p.instrument_id)).fetchone()[0]
            if p.quantity>qty-reserved:
                raise ValueError("可用持仓不足（含其他待卖委托）")
        ident=uuid.uuid4().hex
        c.execute("INSERT INTO sim_orders(id,user_id,client_key,account_id,instrument_id,side,quantity,limit_price,stop_loss,status,created_at,cost,rules_json,fees_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",(ident,uid,p.client_key,p.account_id,p.instrument_id,p.side,p.quantity,str(dec(p.limit_price)),str(p.stop_loss) if p.stop_loss else None,"pending",timestamp(),str(estimate["total"]),json_text(item["rules"]),prefs.fees.model_dump_json()))
    return owned(store,"sim_orders",uid,ident)


# 比较已存在委托与本次输入，防止同一幂等键被不同内容复用。
def _same_order(existing,p):
    if any(str(existing[k])!=str(getattr(p,k)) for k in ("account_id","instrument_id","side","quantity")) or dec(existing["limit_price"])!=dec(p.limit_price) or (existing["stop_loss"] is not None)!=(p.stop_loss is not None) or (p.stop_loss is not None and dec(existing["stop_loss"])!=dec(p.stop_loss)):
        raise ValueError("幂等键已被不同委托使用")


# 仅撤销本人仍待成交的委托。
def cancel(store,uid,ident):
    row=owned(store,"sim_orders",uid,ident)
    if getattr(store,'sync_service',None):store.sync_service.account_guard(uid,row['account_id'])
    store.execute("UPDATE sim_orders SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",(ident,uid))
    return {"success":True}


# 按后续有效行情撮合本人委托，并事务性更新账户、持仓与唯一流水。
def match(store,uid):
    prefs=get_preferences(store,uid);results=[];ctx=market.calendar(store)
    for queued in store.query("SELECT * FROM sim_orders WHERE user_id=? AND status='pending' AND id NOT IN(SELECT order_id FROM sync_inert_orders WHERE user_id=?) ORDER BY created_at,id",(uid,uid)):
        if getattr(store,'sync_service',None):store.sync_service.account_guard(uid,queued['account_id'])
        item=market.instrument(store,queued["instrument_id"]);bars=market.bars_for(store,item["id"])
        for i,b in enumerate(bars):
            if datetime.fromisoformat(b["ts"]).replace(hour=9,minute=30,second=0)<=datetime.fromisoformat(queued["created_at"]) or datetime.fromisoformat(b["ts"])>now():
                continue
            if b["quality"]!="valid" or b["adjustment"]!="none" or b["volume"]<=0 or i==0:
                continue
            rules=json.loads(queued["rules_json"])
            if rules!=item["rules"] or (queued["side"]=="buy" and (market.risk_reasons(item,prefs) or fresh_reasons(bars[-1],ctx))):
                store.execute("UPDATE sim_orders SET status='rejected',reason='成交时规则或风险复核未通过' WHERE id=? AND user_id=? AND status='pending'",(queued["id"],uid));break
            fee_cfg=Fees.model_validate_json(queued["fees_json"]);raw=dec(b["open"]);limit=dec(queued["limit_price"])
            price=executable_price(raw,queued["side"],rules["price_tick"],fee_cfg.slippage_bps)
            prev=dec(b.get("metadata",{}).get("reference_price",bars[i-1]["close"]));tick=dec(rules["price_tick"])
            upper=(prev*(1+dec(rules["limit_pct"]))/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick
            lower=(prev*(1-dec(rules["limit_pct"]))/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick
            if (queued["side"]=="buy" and (price>limit or raw>=upper)) or (queued["side"]=="sell" and (price<limit or raw<=lower)) or not dec(b["low"])<=price<=dec(b["high"]):
                continue
            with store.user_lock(uid),store.tx(immediate=True) as c:
                if not store.active(uid,c=c):
                    return results
                current=c.execute("SELECT status FROM sim_orders WHERE id=? AND user_id=?",(queued["id"],uid)).fetchone()
                if not current or current[0]!="pending":
                    break
                a=c.execute("SELECT * FROM sim_accounts WHERE id=? AND user_id=?",(queued["account_id"],uid)).fetchone();q=queued["quantity"]
                fee=dec(transaction_fees(price,q,item["asset_class"],queued["side"],fee_cfg)["total"]);gross=money(price*q)
                if queued["side"]=="buy":
                    total=money(gross+fee)
                    exposure=sum((dec(r[0]) for r in c.execute("SELECT cost FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0",(uid,a["id"],item["id"]))),Decimal(0))
                    if exposure+total>dec(prefs.budget) or total>dec(a["cash"]):
                        c.execute("UPDATE sim_orders SET status='rejected',reason='成交预算或现金不足' WHERE id=? AND user_id=?",(queued["id"],uid));break
                    delta=-total
                    c.execute("INSERT INTO sim_lots VALUES(?,?,?,?,?,?,?,?)",(queued["id"],uid,a["id"],item["id"],q,str(total),b["ts"][:10],rules["settlement_days"]))
                else:
                    lots=[dict(r) for r in c.execute("SELECT * FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0 ORDER BY acquired_day,id",(uid,a["id"],item["id"])) if r["settlement_days"]==0 or r["acquired_day"]<b["ts"][:10]]
                    if sum(r["quantity"] for r in lots)<q:
                        continue
                    remain=q
                    for lot in lots:
                        take=min(remain,lot["quantity"]);left=lot["quantity"]-take
                        c.execute("UPDATE sim_lots SET quantity=?,cost=? WHERE id=? AND user_id=?",(left,str(money(dec(lot["cost"])*left/lot["quantity"])),lot["id"],uid));remain-=take
                        if not remain:
                            break
                    delta=money(gross-fee)
                balance=money(dec(a["cash"])+delta)
                c.execute("UPDATE sim_accounts SET cash=? WHERE id=? AND user_id=?",(str(balance),a["id"],uid))
                c.execute("INSERT INTO sim_ledger(user_id,account_id,order_id,ts,delta,balance,description) VALUES(?,?,?,?,?,?,?)",(uid,a["id"],queued["id"],b["ts"],str(delta),str(balance),"模拟"+queued["side"]))
                c.execute("UPDATE sim_orders SET status='filled',filled_at=?,fill_price=?,fees=?,source_ts=?,reason=NULL WHERE id=? AND user_id=?",(b["ts"],str(price),str(fee),b["ts"],queued["id"],uid))
                results.append({"id":queued["id"],"fill_price":float(price),"quantity":q})
            break
    _match_live(store,uid,prefs)
    _stops(store,uid)
    for a in portfolio(store,uid)["accounts"]:
        with store.user_lock(uid),store.tx(immediate=True) as c:
            if store.active(uid,c=c):
                c.execute("INSERT OR REPLACE INTO sim_equity VALUES(?,?,?,?)",(a["id"],uid,timestamp(),str(a["equity"])))
    return results


def _note_block(store,uid,queued,reason):
    if queued.get("reason")==reason:return
    queued["reason"]=reason
    store.execute("UPDATE sim_orders SET reason=? WHERE id=? AND user_id=? AND status='pending'",(reason,queued["id"],uid))
    store.notify(uid,f"{queued['instrument_id']} 最新价已到，未成交：{reason}","warning")


def _load_spot(store,ident):
    row=store.one("SELECT payload_json FROM market_quotes WHERE instrument_id=?",(ident,))
    if not row:return None
    try:payload=json.loads(row["payload_json"])
    except (TypeError,ValueError):return None
    if payload.get("kind")!="spot" or not payload.get("quote_time"):return None
    try:price=float(payload["price"])
    except (TypeError,ValueError):return None
    if not math.isfinite(price) or price<=0:return None
    return payload


def _live_bar(spot):
    def num(key):
        try:value=float(spot.get(key))
        except (TypeError,ValueError):return None
        return value if math.isfinite(value) and value>0 else None
    price=float(spot["price"]);open_=num("open") or num("prev_close") or price
    high=max(num("high") or price,price,open_);low=min(num("low") or price,price,open_)
    return {"ts":spot["quote_time"],"open":open_,"high":high,"low":low,"close":price,"volume":num("volume") or 1,"quality":"valid","adjustment":"none","metadata":{"reference_price":num("prev_close")}}


# 最新价穿过启用中的价格规则时，当天生成委托。未穿过不记账，收盘仍可再判断。
def evaluate_live(store,uid):
    from backend.price_rules import judge,prior_closes,session_usable,evaluated_days
    today=now().date().isoformat();created=[];prefs=get_preferences(store,uid)
    for rule in store.query("SELECT * FROM sim_price_rules WHERE user_id=? AND status='active' ORDER BY armed_at,id",(uid,)):
        try:_guard(store,uid,rule["account_id"])
        except Exception:continue
        if (rule["expires_on"] and rule["expires_on"]<today) or rule["success_count"]>=rule["max_orders"]:continue
        seen=evaluated_days(store.query("SELECT bar_date,result,reason FROM sim_price_rule_events WHERE user_id=? AND price_rule_id=?",(uid,rule["id"])))
        if today in seen or not session_usable(rule["armed_at"],today):continue
        spot=_load_spot(store,rule["instrument_id"])
        if not spot or str(spot["quote_time"])[:10]!=today:continue
        item=market.instrument(store,rule["instrument_id"])
        history=[b for b in market.bars_for(store,item["id"],"1d") if b["ts"][:10]<today]
        position,cash,pending=_rule_state(store,uid,rule,today)
        decision=judge({**rule,"params":json.loads(rule["params_json"])},_live_bar(spot),prior_closes(history,len(history)),position,pending,cash,prefs.fees,prefs.budget,item["asset_class"],item["rules"])
        if not decision or decision.get("result")!="signal":continue
        try:
            placed=order(store,uid,OrderInput(client_key=decision["client_key"],account_id=rule["account_id"],instrument_id=rule["instrument_id"],side=decision["side"],quantity=decision["quantity"],limit_price=float(decision["limit_price"]),stop_loss=float(decision["stop_loss"]) if decision["stop_loss"] else None))
        except Exception as exc:
            if _rule_event(store,uid,rule,today,"rejected",str(exc),None):store.notify(uid,f"{rule['instrument_id']} 条件已到，委托未生成：{exc}","warning")
            continue
        if _rule_event(store,uid,rule,today,"created","已按最新价生成委托",placed["id"]):
            store.execute("UPDATE sim_price_rules SET success_count=success_count+1 WHERE id=? AND user_id=?",(rule["id"],uid))
            created.append(placed["id"])
    return created


# 用最新价撮合已有委托。限价未到则留到后续日线。
def _match_live(store,uid,prefs):
    results=[]
    for queued in store.query("SELECT * FROM sim_orders WHERE user_id=? AND status='pending' AND id NOT IN(SELECT order_id FROM sync_inert_orders WHERE user_id=?) ORDER BY created_at,id",(uid,uid)):
        spot=_load_spot(store,queued["instrument_id"])
        if not spot or str(spot["quote_time"])<=str(queued["created_at"]):continue
        item=market.instrument(store,queued["instrument_id"]);rules=json.loads(queued["rules_json"])
        if rules!=item["rules"]:continue
        bar=_live_bar(spot);fee_cfg=Fees.model_validate_json(queued["fees_json"]);raw=dec(bar["close"]);limit=dec(queued["limit_price"])
        reached=(queued["side"]=="buy" and raw<=limit) or (queued["side"]=="sell" and raw>=limit)
        if not reached:continue
        price=executable_price(raw,queued["side"],rules["price_tick"],fee_cfg.slippage_bps)
        if queued["side"]=="buy" and price>limit:price=limit
        if queued["side"]=="sell" and price<limit:price=limit
        prev=bar["metadata"].get("reference_price")
        if prev:
            tick=dec(rules["price_tick"]);span=dec(rules["limit_pct"]);prev=dec(prev)
            upper=(prev*(1+span)/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick
            lower=(prev*(1-span)/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick
            if (queued["side"]=="buy" and raw>=upper) or (queued["side"]=="sell" and raw<=lower):
                _note_block(store,uid,queued,"涨跌停附近未成交");continue
        with store.user_lock(uid),store.tx(immediate=True) as c:
            if not store.active(uid,c=c):return results
            current=c.execute("SELECT status FROM sim_orders WHERE id=? AND user_id=?",(queued["id"],uid)).fetchone()
            if not current or current[0]!="pending":continue
            account=c.execute("SELECT * FROM sim_accounts WHERE id=? AND user_id=?",(queued["account_id"],uid)).fetchone();q=queued["quantity"]
            fee=dec(transaction_fees(price,q,item["asset_class"],queued["side"],fee_cfg)["total"]);gross=money(price*q)
            if queued["side"]=="buy":
                total=money(gross+fee)
                exposure=sum((dec(r[0]) for r in c.execute("SELECT cost FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0",(uid,account["id"],item["id"]))),Decimal(0))
                if exposure+total>dec(prefs.budget) or total>dec(account["cash"]):
                    _note_block(store,uid,queued,"现金或单只预算不足");continue
                delta=-total
                c.execute("INSERT INTO sim_lots VALUES(?,?,?,?,?,?,?,?)",(queued["id"],uid,account["id"],item["id"],q,str(total),bar["ts"][:10],rules["settlement_days"]))
            else:
                lots=[dict(r) for r in c.execute("SELECT * FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0 ORDER BY acquired_day,id",(uid,account["id"],item["id"])) if r["settlement_days"]==0 or r["acquired_day"]<bar["ts"][:10]]
                if sum(r["quantity"] for r in lots)<q:
                    _note_block(store,uid,queued,"可卖数量不足");continue
                remain=q
                for lot in lots:
                    take=min(remain,lot["quantity"]);left=lot["quantity"]-take
                    c.execute("UPDATE sim_lots SET quantity=?,cost=? WHERE id=? AND user_id=?",(left,str(money(dec(lot["cost"])*left/lot["quantity"])),lot["id"],uid));remain-=take
                    if not remain:break
                delta=money(gross-fee)
            balance=money(dec(account["cash"])+delta)
            c.execute("UPDATE sim_accounts SET cash=? WHERE id=? AND user_id=?",(str(balance),account["id"],uid))
            c.execute("INSERT INTO sim_ledger(user_id,account_id,order_id,ts,delta,balance,description) VALUES(?,?,?,?,?,?,?)",(uid,account["id"],queued["id"],bar["ts"],str(delta),str(balance),"模拟最新价"+queued["side"]))
            c.execute("UPDATE sim_orders SET status='filled',filled_at=?,fill_price=?,fees=?,source_ts=?,reason=NULL WHERE id=? AND user_id=?",(bar["ts"],str(price),str(fee),bar["ts"],queued["id"],uid))
            results.append({"id":queued["id"],"fill_price":float(price),"quantity":q})
    return results


# 根据已有止损条件生成后续卖出委托，遵守可卖数量与结算规则。
def _stops(store,uid):
    for lot in store.query("SELECT l.*,o.stop_loss FROM sim_lots l JOIN sim_orders o ON o.id=l.id AND o.user_id=l.user_id WHERE l.user_id=? AND l.quantity>0 AND o.stop_loss IS NOT NULL",(uid,)):
        bars=market.bars_for(store,lot["instrument_id"],limit=1)
        if not bars or dec(bars[-1]["close"])>dec(lot["stop_loss"]) or store.one("SELECT id FROM sim_orders WHERE user_id=? AND account_id=? AND instrument_id=? AND side='sell' AND status='pending'",(uid,lot["account_id"],lot["instrument_id"])):
            continue
        item=market.instrument(store,lot["instrument_id"])
        price=executable_price(dec(bars[-1]["close"])*(1-dec(item["rules"]["limit_pct"])),"sell",item["rules"]["price_tick"],0)
        try:
            order(store,uid,OrderInput(client_key="stop:"+lot["id"]+":"+bars[-1]["ts"][:10],account_id=lot["account_id"],instrument_id=lot["instrument_id"],side="sell",quantity=lot["quantity"],limit_price=float(price)))
            store.notify(uid,"模拟收盘止损已触发，等待后续K线撮合，不保证止损价成交。","warning")
        except ValueError:
            store.notify(uid,"模拟止损委托未生成，请检查可用持仓和规则。","warning")


# 没有写入权时不判断、不改规则。
def _guard(store,uid,account_id):
    service=getattr(store,"sync_service",None)
    if service:
        service.account_guard(uid,account_id)


# 校验本人账户与自选后启用价格规则。
def create_rule(store,uid,payload:dict):
    from backend.config import STRATEGY_VERSION
    from backend.price_rules import prepare
    owned(store,"sim_accounts",uid,payload.get("account_id"))
    _guard(store,uid,payload.get("account_id"))
    item=market.instrument(store,str(payload.get("instrument_id") or ""))
    if not store.one("SELECT 1 ok FROM user_watchlist WHERE user_id=? AND instrument_id=?",(uid,item["id"])):
        raise ValueError("只对自选里的证券启用价格规则")
    prepared=prepare(payload,item,STRATEGY_VERSION)
    if prepared["kind"]=="ma_forward" and store.one("SELECT id FROM sim_price_rules WHERE user_id=? AND account_id=? AND instrument_id=? AND kind='ma_forward' AND status IN ('active','paused')",(uid,payload["account_id"],item["id"])):
        raise ValueError("同一账户同一证券只能有一条均线跟随")
    ident=uuid.uuid4().hex
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):
            raise AuthError("ACCOUNT_DISABLED","账号不可用",403)
        c.execute("INSERT INTO sim_price_rules(id,user_id,account_id,instrument_id,kind,params_json,status,armed_at,expires_on,max_orders,success_count,source_kind,source_id,source_note,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,0,?,?,?,?)",(ident,uid,payload["account_id"],item["id"],prepared["kind"],json_text(prepared["params"]),"active",timestamp(),prepared["expires_on"],prepared["max_orders"],prepared["source_kind"],prepared["source_id"],prepared["source_note"],timestamp()))
    return _rule_view(store,uid,owned(store,"sim_price_rules",uid,ident),[])


# 暂停本人仍启用的价格规则。
def pause_rule(store,uid,ident):
    rule=owned(store,"sim_price_rules",uid,ident)
    _guard(store,uid,rule["account_id"])
    if not store.execute("UPDATE sim_price_rules SET status='paused' WHERE id=? AND user_id=? AND status='active'",(ident,uid)):
        raise ValueError("只有启用中的价格规则可以暂停")
    return {"success":True}


# 恢复本人已暂停的价格规则。
def resume_rule(store,uid,ident):
    rule=owned(store,"sim_price_rules",uid,ident)
    _guard(store,uid,rule["account_id"])
    if rule["status"]!="paused":
        raise ValueError("只有暂停中的价格规则可以恢复")
    if rule["expires_on"] and rule["expires_on"]<now().date().isoformat() or rule["success_count"]>=rule["max_orders"]:
        raise ValueError("规则已到期或达到次数上限")
    if rule["kind"]=="ma_forward" and store.one("SELECT id FROM sim_price_rules WHERE user_id=? AND account_id=? AND instrument_id=? AND kind='ma_forward' AND status='active'",(uid,rule["account_id"],rule["instrument_id"])):
        raise ValueError("同一账户同一证券只能有一条均线跟随")
    store.execute("UPDATE sim_price_rules SET status='active' WHERE id=? AND user_id=?",(ident,uid))
    return {"success":True}


# 停止本人的价格规则，并按要求撤销该规则的待成交委托。
def stop_rule(store,uid,ident,cancel_pending=False):
    rule=owned(store,"sim_price_rules",uid,ident)
    _guard(store,uid,rule["account_id"])
    if rule["status"]!="stopped":
        store.execute("UPDATE sim_price_rules SET status='stopped' WHERE id=? AND user_id=?",(ident,uid))
    if cancel_pending:
        for queued in store.query("SELECT id,account_id,client_key FROM sim_orders WHERE user_id=? AND account_id=? AND status='pending'",(uid,rule["account_id"])):
            if _order_rule_id(store,uid,queued)==ident:
                store.execute("UPDATE sim_orders SET status='cancelled',reason='规则已停止' WHERE id=? AND user_id=? AND status='pending'",(queued["id"],uid))
    return {"success":True}


# 判断本人启用中的价格规则。没有写入权的账户直接跳过。
def evaluate(store,uid):
    from backend.price_rules import calendar_block,judge,prior_closes,session_usable,evaluated_days
    from backend.sync.protocol import SyncError
    ctx=market.calendar(store);today=now().date().isoformat();created=[];prefs=get_preferences(store,uid)
    for rule in store.query("SELECT * FROM sim_price_rules WHERE user_id=? AND status='active' ORDER BY armed_at,id",(uid,)):
        try:
            _guard(store,uid,rule["account_id"])
        except SyncError:
            continue
        if (rule["expires_on"] and rule["expires_on"]<today) or rule["success_count"]>=rule["max_orders"]:
            store.execute("UPDATE sim_price_rules SET status='stopped' WHERE id=? AND user_id=? AND status='active'",(rule["id"],uid));continue
        if calendar_block(ctx):
            _rule_event(store,uid,rule,today,"skipped",calendar_block(ctx),None);continue
        item=market.instrument(store,rule["instrument_id"])
        bars=[b for b in market.bars_for(store,item["id"],"1d") if b["ts"][:10]<=ctx["last_completed_session"]]
        seen=evaluated_days(store.query("SELECT bar_date,result,reason FROM sim_price_rule_events WHERE user_id=? AND price_rule_id=?",(uid,rule["id"])))
        view={**rule,"params":json.loads(rule["params_json"])}
        for index,bar in enumerate(bars):
            day=bar["ts"][:10]
            if day in seen or not session_usable(rule["armed_at"],day):
                continue
            position,cash,pending=_rule_state(store,uid,rule,day)
            decision=judge(view,bar,prior_closes(bars,index),position,pending,cash,prefs.fees,prefs.budget,item["asset_class"],item["rules"])
            if not decision:
                continue
            order_id=None;result,reason=decision["result"],decision["reason"]
            if result=="signal":
                try:
                    placed=order(store,uid,OrderInput(client_key=decision["client_key"],account_id=rule["account_id"],instrument_id=rule["instrument_id"],side=decision["side"],quantity=decision["quantity"],limit_price=float(decision["limit_price"]),stop_loss=float(decision["stop_loss"]) if decision["stop_loss"] else None))
                    order_id,result,reason=placed["id"],"created","已生成限价委托";created.append(placed["id"])
                except SyncError:
                    break
                except ValueError as exc:
                    result,reason="rejected",str(exc)
            if _rule_event(store,uid,rule,day,result,reason,order_id) and result=="created":
                store.execute("UPDATE sim_price_rules SET success_count=success_count+1 WHERE id=? AND user_id=?",(rule["id"],uid))
                current=store.one("SELECT success_count,max_orders FROM sim_price_rules WHERE id=? AND user_id=?",(rule["id"],uid))
                if current["success_count"]>=current["max_orders"]:
                    store.execute("UPDATE sim_price_rules SET status='stopped' WHERE id=? AND user_id=?",(rule["id"],uid));break
            seen.add(day)
    return created


# 读取规则在该交易日可见的持仓、现金和本规则待成交方向。
def _rule_state(store,uid,rule,day):
    lots=store.query("SELECT * FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0",(uid,rule["account_id"],rule["instrument_id"]))
    available=sum(lot["quantity"] for lot in lots if lot["settlement_days"]==0 or lot["acquired_day"]<day)
    position={"quantity":sum(lot["quantity"] for lot in lots),"available":available,"cost":sum((dec(lot["cost"]) for lot in lots),Decimal(0))}
    cash=dec(store.one("SELECT cash FROM sim_accounts WHERE id=? AND user_id=?",(rule["account_id"],uid))["cash"])
    pending={queued["side"] for queued in store.query("SELECT id,account_id,side,client_key FROM sim_orders WHERE user_id=? AND account_id=? AND instrument_id=? AND status='pending'",(uid,rule["account_id"],rule["instrument_id"])) if _order_rule_id(store,uid,queued)==rule["id"]}
    return position,cash,pending


# 同一用户、同一规则、同一交易日只记录一次判断。
def _rule_event(store,uid,rule,day,result,reason,order_id):
    from backend.price_rules import CALENDAR_BLOCK_REASONS
    return bool(store.execute("""INSERT INTO sim_price_rule_events(id,user_id,account_id,price_rule_id,bar_date,result,reason,order_id)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(user_id,price_rule_id,bar_date) DO UPDATE SET
        result=excluded.result,reason=excluded.reason,order_id=excluded.order_id
        WHERE sim_price_rule_events.result='skipped' AND sim_price_rule_events.reason IN (?,?)""",
        (uuid.uuid4().hex,uid,rule["account_id"],rule["id"],day,result,reason[:500],order_id,*CALENDAR_BLOCK_REASONS)))


def _order_rule_id(store,uid,queued):
    """旧同步订单的 key 可能保留来源端 ID，优先使用已映射的事件关联。"""
    from backend.price_rules import rule_id_from_client_key
    event=store.one("SELECT price_rule_id FROM sim_price_rule_events WHERE user_id=? AND account_id=? AND order_id=? AND result='created'",(uid,queued["account_id"],queued["id"]))
    return event["price_rule_id"] if event else rule_id_from_client_key(queued["client_key"])


# 把规则行转成页面字段。
def _rule_view(store,uid,rule,calendar_days,last_completed=None):
    from backend.price_rules import next_usable_day,summarize,evaluated_days
    params=json.loads(rule["params_json"]) if isinstance(rule["params_json"],str) else rule["params_json"]
    events=store.query("SELECT * FROM sim_price_rule_events WHERE user_id=? AND price_rule_id=? ORDER BY bar_date DESC LIMIT 1",(uid,rule["id"]))
    seen=evaluated_days(store.query("SELECT bar_date,result,reason FROM sim_price_rule_events WHERE user_id=? AND price_rule_id=?",(uid,rule["id"])))
    return {**{k:rule[k] for k in ("id","account_id","instrument_id","kind","status","armed_at","expires_on","max_orders","success_count","source_kind","source_id","source_note","created_at")},"name":market.instrument(store,rule["instrument_id"])["name"],"params":params,"summary":summarize(rule["kind"],params),"last_event":events[0] if events else None,"next_day":next_usable_day(rule["armed_at"],last_completed,seen,calendar_days)}


# 返回本人虚拟账户、预留现金、价格规则、委托及现金流水。
def portfolio(store,uid):
    from backend.price_rules import explain_pending
    out=[];ctx=market.calendar(store);days=[row["day"] for row in store.query("SELECT day FROM calendar")]
    for a in store.query("SELECT * FROM sim_accounts WHERE user_id=? ORDER BY created_at",(uid,)):
        positions=[];equity=dec(a["cash"])
        for p in store.query("SELECT instrument_id,SUM(quantity) quantity FROM sim_lots WHERE user_id=? AND account_id=? AND quantity>0 GROUP BY instrument_id",(uid,a["id"])):
            lots=store.query("SELECT * FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0",(uid,a["id"],p["instrument_id"]))
            basis=sum((dec(l["cost"]) for l in lots),Decimal(0));bars=market.bars_for(store,p["instrument_id"],limit=1);last=bars[-1] if bars else None
            value=money(dec(last["close"])*p["quantity"]) if last else basis;equity+=value
            positions.append({**p,"name":market.instrument(store,p["instrument_id"])["name"],"cost":float(basis),"market_value":float(value) if last else None,"unrealized_pnl":float(value-basis) if last else None,"quote_time":last["ts"] if last else None,"stale":bool(fresh_reasons(last,ctx)),"available_quantity":sum(l["quantity"] for l in lots if l["settlement_days"]==0 or l["acquired_day"]<now().date().isoformat())})
        reserved=sum((dec(row["cost"]) for row in store.query("SELECT cost FROM sim_orders WHERE user_id=? AND account_id=? AND side='buy' AND status='pending'",(uid,a["id"]))),Decimal(0))
        out.append({**a,"positions":positions,"equity":float(money(equity)),"pnl":float(money(equity-dec(a["initial_cash"]))),"reserved_buy":float(money(reserved)),"available_cash":float(money(dec(a["cash"])-reserved)),"curve":store.query("SELECT ts,equity FROM sim_equity WHERE account_id=? AND user_id=? ORDER BY ts",(a["id"],uid))})
    orders=[]
    for queued in store.query("SELECT o.*,i.name FROM sim_orders o JOIN instruments i ON i.id=o.instrument_id WHERE o.user_id=? ORDER BY o.created_at DESC LIMIT 500",(uid,)):
        item=market.instrument(store,queued["instrument_id"])
        lots=store.query("SELECT * FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0",(uid,queued["account_id"],queued["instrument_id"]))
        available=sum(lot["quantity"] for lot in lots if lot["settlement_days"]==0 or lot["acquired_day"]<now().date().isoformat())
        queued["rule_id"]=_order_rule_id(store,uid,queued)
        queued["pending_note"]=explain_pending(queued,market.bars_for(store,queued["instrument_id"],"1d"),item["rules"],available,ctx) if queued["status"]=="pending" else None
        orders.append(queued)
    rules=[_rule_view(store,uid,rule,days,ctx.get("last_completed_session")) for rule in store.query("SELECT * FROM sim_price_rules WHERE user_id=? ORDER BY created_at DESC",(uid,))]
    return {"accounts":out,"orders":orders,"ledger":store.query("SELECT * FROM sim_ledger WHERE user_id=? ORDER BY id DESC LIMIT 500",(uid,)),"rules":rules}
