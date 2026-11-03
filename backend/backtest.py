# 使用历史快照执行确定性回测，统一处理费用、交易约束和证据门槛。
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import collectors, db, instruments
from .budget import budget_check, dec, money, transaction_fees
from .config import Fees, STRATEGY_VERSION, timestamp


# 限定策略参数、初始虚拟资金、样本划分和严格证据模式。
class Parameters(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instrument_id: str
    initial_cash: float = Field(gt=0, le=100000000)
    fast: int = Field(5, ge=2, le=100)
    slow: int = Field(20, ge=3, le=250)
    train_ratio: float = Field(0.7, ge=0.5, le=0.9)
    stop_loss_pct: float = Field(0.05, gt=0, le=0.5)
    start_date: str | None = None
    end_date: str | None = None
    strict: bool = True

    # 校验均线长短关系、日期格式及回测区间的先后顺序。
    @model_validator(mode="after")
    def ordered(self):
        if self.fast >= self.slow:
            raise ValueError("短均线必须小于长均线")
        for value in (self.start_date,self.end_date):
            if value:
                datetime.strptime(value,"%Y-%m-%d")
        if self.start_date and self.end_date and self.start_date > self.end_date:
            raise ValueError("开始日期不能晚于结束日期")
        return self


# 加入方向相关的滑点，并按最小报价单位向不利方向取整。
def executable_price(raw, side, tick, slippage_bps):
    value = dec(raw)*(1+dec(slippage_bps)/10000 if side=="buy" else 1-dec(slippage_bps)/10000)
    return (value/dec(tick)).quantize(Decimal(1),rounding=ROUND_CEILING if side=="buy" else ROUND_FLOOR)*dec(tick)


# 按时间顺序生成收盘信号，在后续开盘按产品规则模拟成交。
def run_engine(bars: list[dict], params: Parameters, item: dict, fees: Fees, budget=1000, fee_multiplier=1, start_index=0):
    rules=item["rules"]
    cash=money(params.initial_cash)
    shares=0
    basis=Decimal(0)
    entry_day=""
    entry_price=Decimal(0)
    cycle_income=Decimal(0)
    pending=None
    history=[]
    trades=[]
    equity=[]
    rejected=[]
    completed=[]
    peak=cash
    max_dd=0.0
    total_fees=Decimal(0)
    for i,b in enumerate(bars):
        day=b["ts"][:10]
        meta=b.get("metadata",{})
        split=dec(meta.get("split_ratio",1))
        dividend=dec(meta.get("cash_dividend",0))
        if split<=0 or dividend<0:
            raise ValueError("公司行动数据不合法")
        # 公司行动先调整持仓和现金，并只重算指标历史口径，实际成交仍用不复权价格。
        if split!=1 or dividend:
            if shares:
                if dec(shares)*split % 1:
                    raise ValueError("涉及碎股的公司行动需要专项处理，停止回测")
                cash=money(cash+shares*dividend)
                cycle_income+=money(shares*dividend)
                shares=int(dec(shares)*split)
                entry_price=(entry_price-dividend)/split
            history=[float((dec(x)-dividend)/split) for x in history]
            if any(x<=0 for x in history):
                raise ValueError("公司行动导致非正调整价格")
        if i < start_index:
            history.append(float(b["close"]))
            continue
        previous = dec(meta.get("reference_price",history[-1] if history else b["open"]))
        # 涨跌停价按报价单位取整；特殊交易日优先采用当天已核验的限价。
        tick=dec(rules["price_tick"])
        upper=dec(meta.get("upper_limit",(previous*(1+dec(rules["limit_pct"]))/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick))
        lower=dec(meta.get("lower_limit",(previous*(1-dec(rules["limit_pct"]))/tick).quantize(Decimal(1),rounding="ROUND_HALF_UP")*tick))
        tradable=b["volume"]>0 and b.get("quality","valid")=="valid" and meta.get("trading_status","active")=="active" and meta.get("risk_flag","clear")=="clear"
        # 本根开盘只执行之前生成的信号，随后才利用本根收盘生成新信号。
        if pending and tradable:
            side=pending
            # 结算周期为零时可当日卖出，否则至少跨过买入交易日。
            settled=rules["settlement_days"]==0 or day>entry_day
            blocked=(side=="buy" and dec(b["open"])>=upper) or (side=="sell" and dec(b["open"])<=lower)
            if side=="sell" and not settled:
                blocked=True
            if not blocked:
                price=executable_price(b["open"],side,rules["price_tick"],fees.slippage_bps)
                if price<dec(b["low"]) or price>dec(b["high"]) or price>upper or price<lower:
                    rejected.append({"ts":b["ts"],"side":side,"reason":"滑点后价格超出OHLC或涨跌停范围"})
                    pending=None
                elif side=="buy" and not shares:
                    estimate=budget_check(price,rules,item["asset_class"],fees,min(budget,1000))
                    q=estimate["max_quantity"]
                    while q>=rules["lot_size"] and money(price*q+dec(transaction_fees(price,q,item["asset_class"],"buy",fees,fee_multiplier)["total"]))>min(cash,dec(budget),Decimal(1000)):
                        q-=rules["quantity_step"]
                    if q>=rules["lot_size"]:
                        f=dec(transaction_fees(price,q,item["asset_class"],"buy",fees,fee_multiplier)["total"])
                        basis=money(price*q+f)
                        cash=money(cash-basis)
                        shares=q
                        entry_day=day
                        entry_price=price
                        cycle_income=Decimal(0)
                        total_fees+=f
                        trades.append({"ts":b["ts"],"side":"buy","price":float(price),"quantity":q,"fees":float(f),"cash":float(cash),"signal_ts":bars[i-1]["ts"] if i else None})
                    else:
                        rejected.append({"ts":b["ts"],"side":side,"reason":"含费预算或现金不足"})
                    pending=None
                elif side=="sell" and shares:
                    f=dec(transaction_fees(price,shares,item["asset_class"],"sell",fees,fee_multiplier)["total"])
                    proceeds=money(price*shares-f)
                    pnl=money(proceeds+cycle_income-basis)
                    cash=money(cash+proceeds)
                    total_fees+=f
                    trades.append({"ts":b["ts"],"side":"sell","price":float(price),"quantity":shares,"fees":float(f),"pnl":float(pnl),"cash":float(cash),"signal_ts":bars[i-1]["ts"] if i else None})
                    completed.append(float(pnl))
                    shares=0
                    basis=Decimal(0)
                    cycle_income=Decimal(0)
                    pending=None
            else:
                rejected.append({"ts":b["ts"],"side":side,"reason":"涨跌停或T+1阻止成交"})
                # 受阻的卖出继续等待；买入信号只尝试下一根 K 线，受阻后失效。
                if side=="buy":
                    pending=None
        history.append(float(b["close"]))
        value=money(cash+dec(b["close"])*shares)
        peak=max(peak,value)
        dd=float((peak-value)/peak) if peak else 0
        max_dd=max(max_dd,dd)
        equity.append({"ts":b["ts"],"equity":float(value),"cash":float(cash),"quantity":shares,"drawdown":round(dd*100,4)})
        # 完成本根权益记账后再计算收盘信号，防止同根收盘信号提前在开盘成交。
        if len(history)>=params.slow:
            fast=sum(history[-params.fast:])/params.fast
            slow=sum(history[-params.slow:])/params.slow
            if shares and (fast<slow or dec(b["close"])<=entry_price*(1-dec(params.stop_loss_pct))):
                pending="sell"
            elif not shares and fast>slow:
                pending="buy"
    last=equity[-1]["equity"] if equity else float(cash)
    wins=sum(x>0 for x in completed)
    profit=sum(x for x in completed if x>0)
    loss=-sum(x for x in completed if x<0)
    return {"metrics":{"initial_cash":params.initial_cash,"final_equity":last,"return_pct":round((last/params.initial_cash-1)*100,4),"max_drawdown_pct":round(max_dd*100,4),"round_trips":len(completed),"fill_count":len(trades),"win_rate_pct":round(wins/len(completed)*100,2) if completed else None,"profit_factor":round(profit/loss,4) if loss else None,"fees":float(total_fees),"open_quantity":shares,"unrealized_pnl":float(money(dec(bars[-1]["close"])*shares-basis)) if bars else 0},"trades":trades,"equity":equity,"rejected":rejected,"execution":"收盘生成信号，下一根开盘撮合；止损按收盘触发并在下一根开盘执行，非盘中止损保证"}


# 校验证据与行情序列，分别运行全样本、样本内外及费用敏感性实验。
def evaluate(bars,params,item,fees,budget):
    if len(bars)<params.slow+20:
        raise ValueError(f"数据不足：至少需要{params.slow+20}根日线")
    if any(b.get("adjustment")!="none" for b in bars):
        raise ValueError("撮合必须使用明确标记的不复权成交价，不能用前复权价格买卖")
    ordered=sorted(bars,key=lambda b:b["ts"])
    if ordered!=bars or len({b["ts"] for b in bars})!=len(bars):
        raise ValueError("历史行情必须严格升序且不重复")
    if len({b["ts"][:10] for b in bars})!=len(bars):
        raise ValueError("日线回测每个交易日只允许一根K线")
    gaps=[]
    if not item["rules"].get("verified"):
        gaps.append("产品交易规则尚未核验")
    if not all(b.get("metadata",{}).get("historical_status_verified") and instruments.official_url(b.get("metadata",{}).get("status_source_url","")) and all(k in b.get("metadata",{}) for k in ("trading_status","risk_flag","upper_limit","lower_limit")) for b in bars):
        gaps.append("缺少逐日历史交易状态、风险警示与限价核验证据")
    if not all(b.get("metadata",{}).get("corporate_actions_verified") and instruments.official_url(b.get("metadata",{}).get("corporate_actions_source_url","")) for b in bars):
        gaps.append("缺少逐日公司行动核验，分红送转可能使结果失真")
    if params.strict and gaps:
        raise ValueError("可靠性门槛未通过："+"；".join(gaps)+"。可导入已核验的历史数据，或显式选择未验证实验模式。")
    # 样本内外独立评估并保留时间顺序，避免随机划分带来未来信息。
    split=int(len(bars)*params.train_ratio)
    if len(bars)-split<10:
        raise ValueError("样本外数据不足10根")
    full=run_engine(bars,params,item,fees,budget)
    train=run_engine(bars[:split],params,item,fees,budget)
    test=run_engine(bars,params,item,fees,budget,start_index=split)
    sensitivity=[{"fee_multiplier":mult,"metrics":run_engine(bars,params,item,fees,budget,fee_multiplier=mult)["metrics"]} for mult in (0.5,1,2)]
    warnings=gaps+["固定自选池存在幸存者偏差，单一证券实验不是全市场策略验证。","费用为配置假设，历史实际费率变化未自动还原。","不在样本外数据上调参；历史结果不代表未来。"]
    if len(bars)<400:
        warnings.append("数据不足约两年交易样本，长期结论未验证。")
    return {"summary":full["metrics"],"full":full,"in_sample":{"range":[bars[0]["ts"],bars[split-1]["ts"]],**train},"out_of_sample":{"range":[bars[split]["ts"],bars[-1]["ts"]],**test},"cost_sensitivity":sensitivity,"warnings":warnings,"validated":not gaps,"data_range":[bars[0]["ts"],bars[-1]["ts"]],"bars_count":len(bars)}


# 读取证券与历史数据，保存可复现的行情、规则、费用和策略快照。
def create(params: Parameters):
    item=instruments.get(params.instrument_id)
    if item["market"] not in {"SH","SZ"} or item["asset_class"] not in {"stock","etf"}:
        raise ValueError("仅支持境内股票和ETF的历史撮合实验")
    bars=collectors.bars_for(params.instrument_id,limit=5000)
    bars=[b for b in bars if (not params.start_date or b["ts"][:10]>=params.start_date) and (not params.end_date or b["ts"][:10]<=params.end_date)]
    cfg=db.settings()
    result=evaluate(bars,params,item,cfg.fees,cfg.budget)
    data_hash=hashlib.sha256(db.dumps(bars).encode()).hexdigest()
    result["data_snapshot"]=bars
    result["instrument_snapshot"]=item
    result["fee_snapshot"]=cfg.fees.model_dump()
    result["budget_snapshot"]=cfg.budget
    ident=uuid.uuid4().hex
    db.execute("INSERT INTO backtest_runs VALUES(?,?,?,?,?,?)",(ident,timestamp(),params.model_dump_json(),db.dumps(result),data_hash,STRATEGY_VERSION))
    return {"id":ident,"data_hash":data_hash,"strategy_version":STRATEGY_VERSION,**result}


# 使用已归档快照重演历史实验，并比较交易记录与结果是否一致。
def reproduce(ident):
    row=db.one("SELECT * FROM backtest_runs WHERE id=?",(ident,))
    if not row:
        raise ValueError("回测不存在")
    if row["strategy_version"]!=STRATEGY_VERSION:
        raise ValueError("策略版本不同，需要对应版本代码才能重演")
    saved=json.loads(row["result_json"])
    bars=saved["data_snapshot"]
    if hashlib.sha256(db.dumps(bars).encode()).hexdigest()!=row["data_hash"]:
        raise ValueError("历史快照哈希校验失败")
    result=evaluate(bars,Parameters.model_validate_json(row["params_json"]),saved["instrument_snapshot"],Fees.model_validate(saved["fee_snapshot"]),saved["budget_snapshot"])
    return {"identical":result["full"]==saved["full"] and result["out_of_sample"]==saved["out_of_sample"] and result["cost_sensitivity"]==saved["cost_sensitivity"],"data_hash":row["data_hash"],"summary":result["summary"]}
