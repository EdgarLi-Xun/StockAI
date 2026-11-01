"""Owned virtual ledgers. All account/order IDs are resolved together with the user."""
from __future__ import annotations

import json
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


def order(store,uid,p:OrderInput):
    owned(store,"sim_accounts",uid,p.account_id)
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


def _same_order(existing,p):
    if any(str(existing[k])!=str(getattr(p,k)) for k in ("account_id","instrument_id","side","quantity")) or dec(existing["limit_price"])!=dec(p.limit_price) or (existing["stop_loss"] is not None)!=(p.stop_loss is not None) or (p.stop_loss is not None and dec(existing["stop_loss"])!=dec(p.stop_loss)):
        raise ValueError("幂等键已被不同委托使用")


def cancel(store,uid,ident):
    owned(store,"sim_orders",uid,ident)
    store.execute("UPDATE sim_orders SET status='cancelled' WHERE id=? AND user_id=? AND status='pending'",(ident,uid))
    return {"success":True}


def match(store,uid):
    prefs=get_preferences(store,uid);results=[];ctx=market.calendar(store)
    for queued in store.query("SELECT * FROM sim_orders WHERE user_id=? AND status='pending' ORDER BY created_at,id",(uid,)):
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
    _stops(store,uid)
    for a in portfolio(store,uid)["accounts"]:
        with store.user_lock(uid),store.tx(immediate=True) as c:
            if store.active(uid,c=c):
                c.execute("INSERT OR REPLACE INTO sim_equity VALUES(?,?,?,?)",(a["id"],uid,timestamp(),str(a["equity"])))
    return results


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


def portfolio(store,uid):
    out=[];ctx=market.calendar(store)
    for a in store.query("SELECT * FROM sim_accounts WHERE user_id=? ORDER BY created_at",(uid,)):
        positions=[];equity=dec(a["cash"])
        for p in store.query("SELECT instrument_id,SUM(quantity) quantity FROM sim_lots WHERE user_id=? AND account_id=? AND quantity>0 GROUP BY instrument_id",(uid,a["id"])):
            lots=store.query("SELECT * FROM sim_lots WHERE user_id=? AND account_id=? AND instrument_id=? AND quantity>0",(uid,a["id"],p["instrument_id"]))
            basis=sum((dec(l["cost"]) for l in lots),Decimal(0));bars=market.bars_for(store,p["instrument_id"],limit=1);last=bars[-1] if bars else None
            value=money(dec(last["close"])*p["quantity"]) if last else basis;equity+=value
            positions.append({**p,"name":market.instrument(store,p["instrument_id"])["name"],"cost":float(basis),"market_value":float(value) if last else None,"unrealized_pnl":float(value-basis) if last else None,"quote_time":last["ts"] if last else None,"stale":bool(fresh_reasons(last,ctx)),"available_quantity":sum(l["quantity"] for l in lots if l["settlement_days"]==0 or l["acquired_day"]<now().date().isoformat())})
        out.append({**a,"positions":positions,"equity":float(money(equity)),"pnl":float(money(equity-dec(a["initial_cash"]))),"curve":store.query("SELECT ts,equity FROM sim_equity WHERE account_id=? AND user_id=? ORDER BY ts",(a["id"],uid))})
    return {"accounts":out,"orders":store.query("SELECT o.*,i.name FROM sim_orders o JOIN instruments i ON i.id=o.instrument_id WHERE o.user_id=? ORDER BY o.created_at DESC LIMIT 500",(uid,)),"ledger":store.query("SELECT * FROM sim_ledger WHERE user_id=? ORDER BY id DESC LIMIT 500",(uid,))}
