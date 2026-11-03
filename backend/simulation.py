# 维护虚拟账户、委托、持仓和资金流水，仅按后续有效行情模拟成交。
from __future__ import annotations

import json
import uuid
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field

from . import collectors, db, instruments
from .backtest import executable_price
from .budget import budget_check, dec, money, transaction_fees
from .config import Fees, now, timestamp


# 约束委托幂等键、账户、证券、方向、数量和限价。
class OrderInput(BaseModel):
    client_key: str = Field(min_length=8,max_length=100)
    account_id: str
    instrument_id: str
    side: str
    quantity: int = Field(gt=0,le=1000000)
    limit_price: float = Field(gt=0,le=100000)
    stop_loss: float | None = Field(None,gt=0)


# 要求用户明确提供正数虚拟本金，并记录账户初始资金流水。
def create_account(name,initial_cash):
    if not str(name).strip() or len(name)>80:
        raise ValueError("请输入账户名称（不超过80字）")
    cash=money(initial_cash)
    if cash<=0 or cash>100000000:
        raise ValueError("请显式设置正数虚拟初始资金")
    ident=uuid.uuid4().hex
    with db.connection() as c:
        c.execute("INSERT INTO accounts VALUES(?,?,?,?,?)",(ident,name,str(cash),str(cash),timestamp()))
        c.execute("INSERT INTO ledger(account_id,ts,delta,balance,description) VALUES(?,?,?,?,?)",(ident,timestamp(),str(cash),str(cash),"设立虚拟账户，非真实资金"))
    return db.one("SELECT * FROM accounts WHERE id=?",(ident,))


# 校验证券风险、数量报价规则、可用现金和持仓后保存待成交委托。
def order(payload: OrderInput):
    existing=db.one("SELECT * FROM orders WHERE client_key=?",(payload.client_key,))
    if existing:
        if any(str(existing[k])!=str(getattr(payload,k)) for k in ("account_id","instrument_id","side","quantity")) or dec(existing["limit_price"])!=dec(payload.limit_price):
            raise ValueError("幂等键已被不同委托使用")
        return existing
    item=instruments.get(payload.instrument_id)
    if payload.side not in {"buy","sell"}:
        raise ValueError("委托方向不合法")
    if payload.side=="buy":
        recent=collectors.bars_for(item["id"],limit=1)
        reasons=instruments.risk_reasons(item)+collectors.fresh_reasons(recent[-1] if recent else None)
        if reasons:
            raise ValueError("；".join(reasons))
    elif not item["rules"].get("verified"):
        raise ValueError("卖出前需核验产品规则")
    cfg=db.settings()
    estimate=budget_check(payload.limit_price,item["rules"],item["asset_class"],cfg.fees,cfg.budget,payload.quantity)
    if payload.side=="buy" and not estimate["within_budget"]:
        raise ValueError("模拟买入总成本超过1000元预算")
    if payload.stop_loss is not None and (payload.side!="buy" or dec(payload.stop_loss)>=dec(payload.limit_price)):
        raise ValueError("止损价须低于模拟买入限价")
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        account=c.execute("SELECT * FROM accounts WHERE id=?",(payload.account_id,)).fetchone()
        if not account:
            raise ValueError("虚拟账户不存在")
        if payload.side=="buy":
            # 可用现金扣除所有待买委托预占；同证券预算还需计入已有持仓与待买成本。
            reserved=sum((dec(r["cost"]) for r in c.execute("SELECT cost FROM orders WHERE account_id=? AND side='buy' AND status='pending'",(payload.account_id,))),Decimal(0))
            if dec(account["cash"])-reserved<dec(estimate["total"]):
                raise ValueError("扣除其他待成交委托后虚拟现金不足")
            exposure=sum((dec(r["cost"]) for r in c.execute("SELECT cost FROM lots WHERE account_id=? AND instrument_id=? AND quantity>0",(payload.account_id,payload.instrument_id))),Decimal(0))
            reserved_symbol=sum((dec(r["cost"]) for r in c.execute("SELECT cost FROM orders WHERE account_id=? AND instrument_id=? AND side='buy' AND status='pending'",(payload.account_id,payload.instrument_id))),Decimal(0))
            if exposure+reserved_symbol+dec(estimate["total"])>dec(cfg.budget):
                raise ValueError("同一证券持仓成本与待买委托合计超过单只含费预算")
        else:
            qty=c.execute("SELECT COALESCE(SUM(quantity),0) q FROM lots WHERE account_id=? AND instrument_id=?",(payload.account_id,payload.instrument_id)).fetchone()["q"]
            reserved=c.execute("SELECT COALESCE(SUM(quantity),0) q FROM orders WHERE account_id=? AND instrument_id=? AND side='sell' AND status='pending'",(payload.account_id,payload.instrument_id)).fetchone()["q"]
            if payload.quantity>qty-reserved:
                raise ValueError("可用持仓不足（含其他待卖委托）")
        ident=uuid.uuid4().hex
        c.execute("INSERT INTO orders(id,client_key,account_id,instrument_id,side,quantity,limit_price,stop_loss,status,created_at,cost,rules_json,fees_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",(ident,payload.client_key,payload.account_id,payload.instrument_id,payload.side,payload.quantity,str(dec(payload.limit_price)),str(payload.stop_loss) if payload.stop_loss else None,"pending",timestamp(),str(estimate["total"]),db.dumps(item["rules"]),cfg.fees.model_dump_json()))
    return db.one("SELECT * FROM orders WHERE id=?",(ident,))


# 使用委托创建后的完整 K 线撮合，原子更新现金、持仓与费用流水。
def match_orders(log=lambda m:None):
    results=[]
    for queued in db.query("SELECT * FROM orders WHERE status='pending' ORDER BY created_at,id"):
        item=instruments.get(queued["instrument_id"])
        bars=collectors.bars_for(item["id"])
        # 只使用开盘晚于委托创建时刻、且已完整收盘的后续 K 线进行撮合。
        choices=[(i,b) for i,b in enumerate(bars) if datetime.fromisoformat(b["ts"]).replace(hour=9,minute=30,second=0)>datetime.fromisoformat(queued["created_at"]) and datetime.fromisoformat(b["ts"])<=now()]
        for i,bar in choices:
            if bar["quality"]!="valid" or bar["adjustment"]!="none" or bar["volume"]<=0:
                continue
            if queued["side"]=="buy" and (instruments.risk_reasons(item) or collectors.fresh_reasons(bars[-1])):
                db.execute("UPDATE orders SET status='rejected',reason=? WHERE id=? AND status='pending'",("成交时风险复核未通过",queued["id"]))
                break
            rules=json.loads(queued["rules_json"])
            if rules!=item["rules"]:
                db.execute("UPDATE orders SET status='rejected',reason='产品规则已变更，请按新规则重新委托' WHERE id=? AND status='pending'",(queued["id"],))
                break
            fee_cfg=Fees.model_validate_json(queued["fees_json"])
            raw=dec(bar["open"])
            limit=dec(queued["limit_price"])
            if (queued["side"]=="buy" and raw>limit) or (queued["side"]=="sell" and raw<limit):
                continue
            price=executable_price(raw,queued["side"],rules["price_tick"],fee_cfg.slippage_bps)
            if (queued["side"]=="buy" and price>limit) or (queued["side"]=="sell" and price<limit) or price<dec(bar["low"]) or price>dec(bar["high"]):
                continue
            if i==0:
                continue  # 需要前一收盘价才能确定当天涨跌停价格，缺失时跳过撮合。
            previous=dec(bar.get("metadata",{}).get("reference_price",bars[i-1]["close"]))
            tick=dec(rules["price_tick"])
            upper=(previous*(1+dec(rules["limit_pct"]))/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick
            lower=(previous*(1-dec(rules["limit_pct"]))/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick
            if (queued["side"]=="buy" and raw>=upper) or (queued["side"]=="sell" and raw<=lower):
                continue
            with db.connection() as c:
                c.execute("BEGIN IMMEDIATE")
                # 进入写事务后再次核对待成交状态，避免取消或重复撮合产生额外流水。
                current=c.execute("SELECT status FROM orders WHERE id=?",(queued["id"],)).fetchone()
                if current["status"]!="pending":
                    break
                account=c.execute("SELECT * FROM accounts WHERE id=?",(queued["account_id"],)).fetchone()
                q=queued["quantity"]
                fee=dec(transaction_fees(price,q,item["asset_class"],queued["side"],fee_cfg)["total"])
                gross=money(price*q)
                if queued["side"]=="buy":
                    total=money(gross+fee)
                    exposure=sum((dec(l["cost"]) for l in c.execute("SELECT cost FROM lots WHERE account_id=? AND instrument_id=? AND quantity>0",(queued["account_id"],item["id"]))),Decimal(0))
                    if exposure+total>min(Decimal(1000),dec(db.settings().budget)) or total>dec(account["cash"]):
                        c.execute("UPDATE orders SET status='rejected',reason='成交预算或现金不足' WHERE id=?",(queued["id"],))
                        break
                    delta=-total
                    c.execute("INSERT INTO lots VALUES(?,?,?,?,?,?,?)",(queued["id"],queued["account_id"],item["id"],q,str(total),bar["ts"][:10],rules["settlement_days"]))
                else:
                    # 按买入批次先进先出扣减，只使用已经满足结算周期的可卖数量。
                    lots=[dict(r) for r in c.execute("SELECT * FROM lots WHERE account_id=? AND instrument_id=? AND quantity>0 ORDER BY acquired_day,id",(queued["account_id"],item["id"])) if r["settlement_days"]==0 or r["acquired_day"]<bar["ts"][:10]]
                    if sum(r["quantity"] for r in lots)<q:
                        continue
                    remaining=q
                    for lot in lots:
                        take=min(remaining,lot["quantity"])
                        remain=lot["quantity"]-take
                        c.execute("UPDATE lots SET quantity=?,cost=? WHERE id=?",(remain,str(money(dec(lot["cost"])*remain/lot["quantity"])),lot["id"]))
                        remaining-=take
                        if not remaining:
                            break
                    delta=money(gross-fee)
                balance=money(dec(account["cash"])+delta)
                c.execute("UPDATE accounts SET cash=? WHERE id=?",(str(balance),queued["account_id"]))
                c.execute("INSERT INTO ledger(account_id,order_id,ts,delta,balance,description) VALUES(?,?,?,?,?,?)",(queued["account_id"],queued["id"],bar["ts"],str(delta),str(balance),"模拟"+queued["side"]))
                c.execute("UPDATE orders SET status='filled',filled_at=?,fill_price=?,fees=?,source_ts=?,reason=NULL WHERE id=?",(bar["ts"],str(price),str(fee),bar["ts"],queued["id"]))
                results.append({"id":queued["id"],"fill_price":float(price),"quantity":q})
            log(f"模拟委托 {queued['id']} 已按后续行情成交")
            break
    snapshot_equity()
    create_stop_orders()
    return results


# 按已保存止损条件生成后续卖出委托，保留幂等与结算约束。
def create_stop_orders():
    for lot in db.query("SELECT l.*,o.stop_loss FROM lots l JOIN orders o ON o.id=l.id WHERE l.quantity>0 AND o.stop_loss IS NOT NULL"):
        bars=collectors.bars_for(lot["instrument_id"],limit=1)
        if not bars or dec(bars[-1]["close"])>dec(lot["stop_loss"]):
            continue
        if db.one("SELECT id FROM orders WHERE account_id=? AND instrument_id=? AND side='sell' AND status='pending'",(lot["account_id"],lot["instrument_id"])):
            continue
        item=instruments.get(lot["instrument_id"])
        limit=executable_price(dec(bars[-1]["close"])*(1-dec(item["rules"]["limit_pct"])),"sell",item["rules"]["price_tick"],0)
        try:
            order(OrderInput(client_key="stop:"+lot["id"]+":"+bars[-1]["ts"][:10],account_id=lot["account_id"],instrument_id=lot["instrument_id"],side="sell",quantity=lot["quantity"],limit_price=float(limit)))
            db.notify("模拟收盘止损已触发，将等待后续K线撮合，不保证止损价成交。","warning")
        except ValueError as exc:
            db.notify("模拟止损委托未生成："+str(exc),"warning")


# 以缓存行情估值账户权益，并保存行情时点与过期状态。
def snapshot_equity():
    for account in db.query("SELECT * FROM accounts"):
        value=dec(account["cash"])
        for lot in db.query("SELECT * FROM lots WHERE account_id=? AND quantity>0",(account["id"],)):
            last=collectors.bars_for(lot["instrument_id"],limit=1)
            value+=dec(last[-1]["close"])*lot["quantity"] if last else dec(lot["cost"])
        db.execute("INSERT OR REPLACE INTO equity_snapshots VALUES(?,?,?)",(account["id"],timestamp(),str(money(value))))


# 汇总账户估值、可卖数量、委托和流水，供模拟交易页面读取。
def portfolio():
    out=[]
    for a in db.query("SELECT * FROM accounts ORDER BY created_at"):
        positions=[]
        equity=dec(a["cash"])
        for p in db.query("SELECT instrument_id,SUM(quantity) quantity FROM lots WHERE account_id=? AND quantity>0 GROUP BY instrument_id",(a["id"],)):
            lots=db.query("SELECT * FROM lots WHERE account_id=? AND instrument_id=? AND quantity>0",(a["id"],p["instrument_id"]))
            basis=sum((dec(l["cost"]) for l in lots),Decimal(0))
            bars=collectors.bars_for(p["instrument_id"],limit=1)
            last=bars[-1] if bars else None
            value=money(dec(last["close"])*p["quantity"]) if last else basis
            equity+=value
            positions.append({**p,"name":instruments.get(p["instrument_id"])["name"],"cost":float(basis),"market_value":float(value) if last else None,"unrealized_pnl":float(value-basis) if last else None,"quote_time":last["ts"] if last else None,"stale":bool(collectors.fresh_reasons(last)),"available_quantity":sum(l["quantity"] for l in lots if l["settlement_days"]==0 or l["acquired_day"]<now().date().isoformat())})
        out.append({**a,"positions":positions,"equity":float(money(equity)),"pnl":float(money(equity-dec(a["initial_cash"]))),"curve":db.query("SELECT ts,equity FROM equity_snapshots WHERE account_id=? ORDER BY ts",(a["id"],))})
    return {"accounts":out,"orders":db.query("SELECT o.*,i.name FROM orders o JOIN instruments i ON i.id=o.instrument_id ORDER BY o.created_at DESC LIMIT 500"),"ledger":db.query("SELECT * FROM ledger ORDER BY id DESC LIMIT 500")}
