# 根据已完成日线判断价格规则，只给出委托意图，不访问数据库。
from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP

from .backtest import executable_price
from .budget import budget_check, cost, dec
from .config import TZ, Fees

KINDS = {"close_at_or_below", "close_at_or_above", "close_stop", "ma_forward"}
SOURCES = {"manual", "candidate", "idea", "backtest"}
PRICE_KINDS = {"close_at_or_below", "close_at_or_above", "close_stop"}
CALENDAR_BLOCK_REASONS = ("交易日历未核验", "没有最近已完成交易日")


# 判断启用日是否允许使用该交易日的收盘。15:00 及之后启用的，当天收盘不采用。
def session_usable(armed_at: str, bar_day: str) -> bool:
    armed = datetime.fromisoformat(armed_at)
    if armed.tzinfo is None:
        armed = armed.replace(tzinfo=TZ)
    armed = armed.astimezone(TZ)
    if bar_day < armed.date().isoformat():
        return False
    if bar_day > armed.date().isoformat():
        return True
    return (armed.hour, armed.minute, armed.second) < (15, 0, 0)


# 交易日历未核验时返回原因，调用方记一条事件且不发信号。
def calendar_block(calendar: dict) -> str | None:
    if not calendar.get("calendar_verified"):
        return "交易日历未核验"
    if not calendar.get("last_completed_session"):
        return "没有最近已完成交易日"
    return None


def evaluated_days(events: list[dict]) -> set[str]:
    """日历暂不可用的记录不是已完成判断，恢复后仍须处理对应交易日。"""
    return {row["bar_date"] for row in events
            if not (row["result"] == "skipped" and row["reason"] in CALENDAR_BLOCK_REASONS)}


# 幂等键只含规则、交易日和方向，同一根 K 线重复判断不会生成第二笔。
def client_key(rule_id: str, bar_day: str, side: str) -> str:
    return f"rule:{rule_id}:{bar_day}:{side}"


# 从委托幂等键还原规则编号。同步前缀不改变规则编号的位置。
def rule_id_from_client_key(client_key: str) -> str | None:
    text = client_key
    if text.startswith("sync:"):
        parts = text.split(":", 2)
        text = parts[2] if len(parts) == 3 else text
    if not text.startswith("rule:"):
        return None
    bits = text.split(":")
    if len(bits) < 4 or not bits[1]:
        return None
    return bits[1]


# 收集当前 K 线之前、可用于均线的有效收盘价。
def prior_closes(bars: list[dict], index: int) -> list[float]:
    closes = []
    for bar in bars[:index]:
        if bar.get("quality") == "valid" and bar.get("adjustment") == "none" and float(bar.get("volume") or 0) > 0:
            closes.append(float(bar["close"]))
    return closes


# 找出下一次还能用来判断的交易日。已判断过的已完成交易日不再返回。
def next_usable_day(armed_at: str, last_completed: str | None, seen: set[str], calendar_days: list[str]) -> str | None:
    if not last_completed:
        return None
    for day in sorted(set(calendar_days)):
        if day <= last_completed and day not in seen and session_usable(armed_at, day):
            return day
    for day in sorted(set(calendar_days)):
        if day > last_completed and session_usable(armed_at, day):
            return day
    return None


# 校验并冻结规则参数。来源摘录只保存原文，不从句子里提取价格。
def prepare(payload: dict, instrument: dict, strategy_version: str) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("价格规则不合法")
    kind = str(payload.get("kind") or "")
    source = str(payload.get("source_kind") or "manual")
    if kind not in KINDS:
        raise ValueError("价格规则种类不合法")
    if source not in SOURCES:
        raise ValueError("价格规则来源不合法")
    if source == "backtest" and kind != "ma_forward":
        raise ValueError("回测向前跟随只能使用均线规则")
    if instrument.get("market") not in {"SH", "SZ"} or instrument.get("asset_class") not in {"stock", "etf"}:
        raise ValueError("价格规则只用于沪深股票或场内 ETF")
    rules = instrument["rules"]
    if kind != "close_at_or_below" and not rules.get("verified"):
        raise ValueError("卖出前需核验产品规则")
    note = str(payload.get("source_note") or "").strip()
    if len(note) > 800:
        raise ValueError("来源摘录不超过800字")
    source_id = str(payload.get("source_id") or "").strip() or None
    if source_id and len(source_id) > 80:
        raise ValueError("来源编号过长")
    expires = payload.get("expires_on") or None
    if expires:
        expires = str(expires)
        datetime.strptime(expires, "%Y-%m-%d")
    default_max = 100 if kind == "ma_forward" else 1
    raw_max = payload.get("max_orders")
    maximum = default_max if raw_max in (None, "") else _integer(raw_max, "成功次数上限")
    if not 1 <= maximum <= 100:
        raise ValueError("成功次数上限为1至100")
    if kind == "ma_forward":
        params = _ma_params(payload, strategy_version)
    else:
        params = _price_params(payload, kind, rules)
    return {"kind": kind, "params": params, "expires_on": expires, "max_orders": maximum, "source_kind": source, "source_id": source_id, "source_note": note}


# 用一句中文描述已冻结的参数，供模拟页展示。
def summarize(kind: str, params: dict) -> str:
    if kind == "close_at_or_below":
        stop = f"，止损 {params['stop_loss']}" if params.get("stop_loss") else ""
        return f"收盘 ≤ {params['trigger_price']} 时，以 {params['limit_price']} 买入 {params['quantity']}{stop}"
    if kind == "close_at_or_above":
        return f"收盘 ≥ {params['trigger_price']} 时，以 {params['limit_price']} 卖出 {params['quantity']}"
    if kind == "close_stop":
        qty = "全部可卖" if params.get("sell_all") else str(params.get("quantity"))
        return f"收盘 ≤ {params['stop_price']} 时，以 {params['limit_price']} 卖出 {qty}"
    return f"均线 {params['fast']}/{params['slow']}，收盘止损 {params['stop_loss_pct']}，策略 {params['strategy_version']}"


# 判断一根已完成日线。启用前的 K 线返回空，调用方不写事件。
def judge(rule: dict, bar: dict, closes_before: list[float], position: dict, pending_sides: set[str], cash, fees: Fees, budget, asset_class: str, instrument_rules: dict) -> dict | None:
    day = bar["ts"][:10]
    if not session_usable(rule["armed_at"], day):
        return None
    if bar.get("quality") != "valid" or bar.get("adjustment") != "none" or float(bar.get("volume") or 0) <= 0:
        return _decision(rule, day, "skipped", "行情质量、复权或成交量不符合")
    params = rule["params"] if isinstance(rule["params"], dict) else json.loads(rule["params"])
    kind = rule["kind"]
    close = dec(bar["close"])
    available = int(position.get("available") or 0)
    held = int(position.get("quantity") or 0)
    if kind == "ma_forward":
        return _judge_ma(rule, day, close, closes_before, held, available, position, pending_sides, cash, fees, budget, asset_class, instrument_rules, params)
    if kind == "close_at_or_below":
        if close > dec(params["trigger_price"]):
            return _decision(rule, day, "skipped", "条件未成立")
        return _ready(rule, day, "buy", int(params["quantity"]), params["limit_price"], params.get("stop_loss"), pending_sides)
    if kind == "close_at_or_above" and close < dec(params["trigger_price"]):
        return _decision(rule, day, "skipped", "条件未成立")
    if kind == "close_stop" and close > dec(params["stop_price"]):
        return _decision(rule, day, "skipped", "条件未成立")
    if held <= 0:
        return _decision(rule, day, "skipped", "没有持仓")
    if available <= 0:
        return _decision(rule, day, "skipped", "结算周期未到")
    quantity = available if kind == "close_stop" and params.get("sell_all") else int(params["quantity"])
    if available < quantity:
        return _decision(rule, day, "skipped", "可卖数量不足")
    return _ready(rule, day, "sell", quantity, params["limit_price"], None, pending_sides)


# 说明待成交委托为何还没在最近一根已完成日线上成交。不写回委托。
def explain_pending(order: dict, bars: list[dict], current_rules: dict, available_quantity: int, calendar: dict) -> str | None:
    if order.get("status") != "pending":
        return None
    blocked = calendar_block(calendar)
    if blocked:
        return blocked
    saved = json.loads(order["rules_json"]) if isinstance(order["rules_json"], str) else order["rules_json"]
    if saved != current_rules:
        return "产品规则已变更"
    fees = Fees.model_validate_json(order["fees_json"]) if isinstance(order["fees_json"], str) else Fees.model_validate(order["fees_json"])
    created = datetime.fromisoformat(order["created_at"])
    last = calendar["last_completed_session"]
    chosen = []
    for index, bar in enumerate(bars):
        if bar["ts"][:10] > last:
            continue
        opened = datetime.fromisoformat(bar["ts"]).replace(hour=9, minute=30, second=0, microsecond=0)
        if opened.tzinfo is None and created.tzinfo is not None:
            opened = opened.replace(tzinfo=created.tzinfo)
        if opened <= created:
            continue
        chosen.append((index, bar))
    if not chosen:
        return "还没有委托创建之后的完整日线"
    reason = "限价未到"
    side = order["side"]
    limit = dec(order["limit_price"])
    tick = dec(saved["price_tick"])
    span = dec(saved["limit_pct"])
    for index, bar in chosen:
        if bar.get("quality") != "valid" or bar.get("adjustment") != "none" or float(bar.get("volume") or 0) <= 0:
            reason = "行情质量、复权或成交量不符合"
            continue
        if index == 0:
            reason = "缺少前收盘价"
            continue
        previous = dec((bar.get("metadata") or {}).get("reference_price", bars[index - 1]["close"]))
        upper = (previous * (1 + span) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
        lower = (previous * (1 - span) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
        raw = dec(bar["open"])
        if side == "buy" and raw >= upper:
            reason = "涨停"
            continue
        if side == "sell" and raw <= lower:
            reason = "跌停"
            continue
        price = executable_price(raw, side, tick, fees.slippage_bps)
        if (side == "buy" and price > limit) or (side == "sell" and price < limit):
            reason = "限价未到"
            continue
        if price < dec(bar["low"]) or price > dec(bar["high"]):
            reason = "滑点后超出当日高低价"
            continue
        if side == "sell" and int(saved.get("settlement_days") or 0) > 0 and available_quantity < int(order["quantity"]):
            reason = "结算周期未到"
            continue
        return "已满足后续开盘条件，尚未撮合"
    return reason


# 冻结均线跟随参数，并核对与回测相同的取值范围。
def _ma_params(payload: dict, strategy_version: str) -> dict:
    fast, slow = _integer(payload.get("fast"), "短均线周期"), _integer(payload.get("slow"), "长均线周期")
    try:
        stop = dec(payload.get("stop_loss_pct"))
    except (ArithmeticError, TypeError, ValueError) as exc:
        raise ValueError("请填写有效的收盘止损比例") from exc
    if not 2 <= fast <= 100 or not 3 <= slow <= 250 or fast >= slow:
        raise ValueError("短均线必须小于长均线，且处于回测允许的范围")
    if not Decimal("0") < stop <= Decimal("0.5"):
        raise ValueError("收盘止损比例须大于0且不超过0.5")
    return {"fast": fast, "slow": slow, "stop_loss_pct": format(stop, "f"), "strategy_version": strategy_version}


# 冻结价格规则的触发价、限价和数量，并按产品报价单位校验。
def _price_params(payload: dict, kind: str, rules: dict) -> dict:
    tick = dec(rules["price_tick"])
    limit = _tick_price(payload.get("limit_price"), tick, "限价")
    trigger_name = "stop_price" if kind == "close_stop" else "trigger_price"
    trigger = _tick_price(payload.get(trigger_name), tick, "止损价" if kind == "close_stop" else "触发价")
    sell_all = bool(payload.get("sell_all")) if kind == "close_stop" else False
    quantity = None
    if not sell_all:
        quantity = _quantity(payload.get("quantity"), rules)
    stop = None
    if kind == "close_at_or_below" and payload.get("stop_loss") not in (None, ""):
        stop = _tick_price(payload.get("stop_loss"), tick, "止损价")
        if stop >= limit:
            raise ValueError("止损价须低于买入限价")
    params = {"limit_price": format(limit, "f"), trigger_name: format(trigger, "f")}
    if sell_all:
        params["sell_all"] = True
    else:
        params["quantity"] = quantity
    if stop is not None:
        params["stop_loss"] = format(stop, "f")
    return params


# 要求价格为正，并且是最小变动价位的整数倍。
def _tick_price(value, tick: Decimal, label: str) -> Decimal:
    try:
        price = dec(value)
    except Exception as exc:
        raise ValueError(f"请填写{label}") from exc
    if price <= 0 or price > 100000 or price % tick != 0:
        raise ValueError(f"{label}不符合该产品最小变动单位")
    return price


# 对整数输入显式报错，不把小数数量或周期静默截断。
def _integer(value, label: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"请填写整数{label}") from exc
    if isinstance(value, bool) or isinstance(value, float) and value != number:
        raise ValueError(f"请填写整数{label}")
    return number


# 要求数量符合一手和申报阶梯。
def _quantity(value, rules: dict) -> int:
    quantity = _integer(value, "数量")
    lot, step = int(rules["lot_size"]), int(rules["quantity_step"])
    if quantity < lot or (quantity - lot) % step != 0 or quantity > 1000000:
        raise ValueError("数量不符合该产品最小申报数量和阶梯")
    return quantity


# 组装一条不带委托价格的判断结果。
def _decision(rule: dict, day: str, result: str, reason: str, side: str | None = None, quantity: int | None = None, limit: str | None = None, stop: str | None = None) -> dict:
    return {"bar_date": day, "result": result, "reason": reason, "side": side, "quantity": quantity, "limit_price": limit, "stop_loss": stop, "client_key": client_key(rule["id"], day, side) if side else None}


# 同方向已有待成交委托时不再发新单；否则给出可下单的意图。
def _ready(rule, day, side, quantity, limit, stop, pending_sides) -> dict:
    if side in pending_sides:
        return _decision(rule, day, "skipped", "已有同方向待成交委托")
    return _decision(rule, day, "signal", "条件成立", side, quantity, str(limit), str(stop) if stop else None)


# 按回测引擎的均线与止损口径决定买入或卖出，数量按当前现金和预算现算。
def _judge_ma(rule, day, close, closes_before, held, available, position, pending_sides, cash, fees, budget, asset_class, instrument_rules, params) -> dict:
    history = [*closes_before, float(close)]
    slow = int(params["slow"])
    if len(history) < slow:
        return _decision(rule, day, "skipped", "均线样本不足")
    fast = sum(history[-int(params["fast"]):]) / int(params["fast"])
    slow_value = sum(history[-slow:]) / slow
    upper, lower = _band(close, instrument_rules)
    if held <= 0 and fast > slow_value:
        if "buy" in pending_sides:
            return _decision(rule, day, "skipped", "已有同方向待成交委托")
        quantity = _affordable(upper, instrument_rules, asset_class, fees, budget, cash)
        if quantity <= 0:
            return _decision(rule, day, "rejected", "含费预算或现金不足")
        return _decision(rule, day, "signal", "短均线高于长均线", "buy", quantity, format(upper, "f"), None)
    if held > 0 and (fast < slow_value or close <= dec(position.get("cost") or 0) / held * (1 - dec(params["stop_loss_pct"]))):
        if available <= 0:
            return _decision(rule, day, "skipped", "结算周期未到")
        if "sell" in pending_sides:
            return _decision(rule, day, "skipped", "已有同方向待成交委托")
        return _decision(rule, day, "signal", "均线或收盘止损成立", "sell", available, format(lower, "f"), None)
    return _decision(rule, day, "skipped", "条件未成立")


# 用当日收盘估算下一交易日的涨跌停价，作为跟随委托的限价。
def _band(close: Decimal, rules: dict) -> tuple[Decimal, Decimal]:
    tick = dec(rules["price_tick"])
    span = dec(rules["limit_pct"])
    upper = (close * (1 + span) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    lower = (close * (1 - span) / tick).quantize(Decimal(1), rounding=ROUND_HALF_UP) * tick
    if lower <= 0:
        raise ValueError("卖出限价不是正数")
    return upper, lower


# 按含费预算和当前现金计算最大合法买入数量。
def _affordable(price: Decimal, rules: dict, asset: str, fees: Fees, budget, cash) -> int:
    estimate = budget_check(price, rules, asset, fees, budget)
    quantity = int(estimate["max_quantity"])
    cap = min(dec(cash), dec(budget), Decimal(1000))
    step = int(rules["quantity_step"])
    lot = int(rules["lot_size"])
    while quantity >= lot and cost(price, quantity, asset, fees) > cap:
        quantity -= step
    return quantity if quantity >= lot else 0
