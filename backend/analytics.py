# 基于已有 K 线计算技术指标与规则评分，历史不足时保留缺失标记。
from __future__ import annotations

from .budget import transaction_fees

CANDIDATE_SCORE_VERSION = "candidate-ranking/1"

# 依次计算均线、MACD、RSI、ATR、量比及波动率，并汇总最新指标。
def indicators(bars: list[dict]) -> dict:
    if not bars:
        return {"series": [], "latest": {}, "missing": ["尚无行情"]}
    close = [float(b["close"]) for b in bars]
    ema12 = ema26 = signal = close[0]
    signal = 0.0
    gains, losses, tr = [], [], []
    series = []
    for i, bar in enumerate(bars):
        c = close[i]
        # 指数均线使用 2/(周期+1) 的平滑系数，逐根更新而不读取未来行情。
        ema12 = c * 2 / 13 + ema12 * 11 / 13
        ema26 = c * 2 / 27 + ema26 * 25 / 27
        dif = ema12 - ema26
        signal = dif * 2 / 10 + signal * 8 / 10
        delta = c - close[i - 1] if i else 0
        gains.append(max(delta, 0))
        losses.append(max(-delta, 0))
        tr.append(max(bar["high"] - bar["low"], abs(bar["high"] - close[i-1]) if i else 0, abs(bar["low"] - close[i-1]) if i else 0))
        avg_gain = sum(gains[max(1, i-13):i+1]) / 14
        avg_loss = sum(losses[max(1, i-13):i+1]) / 14
        rsi = (100 - 100 / (1 + avg_gain / avg_loss)) if avg_loss else (100 if avg_gain else 50)
        # 各指标达到预热长度前输出空值，供上层明确提示数据不足。
        row = {"ts": bar["ts"], **{f"ma{n}": round(sum(close[i-n+1:i+1]) / n, 5) if i >= n-1 else None for n in (5,10,20,60)}, "rsi14": round(rsi, 2) if i >= 14 else None, "atr14": round(sum(tr[i-13:i+1]) / 14, 5) if i >= 13 else None, "dif": round(dif, 5) if i >= 25 else None, "dea": round(signal, 5) if i >= 33 else None, "macd": round((dif-signal)*2, 5) if i >= 33 else None}
        series.append(row)
    latest = dict(series[-1])
    latest["support20"] = min(b["low"] for b in bars[-20:])
    latest["resistance20"] = max(b["high"] for b in bars[-20:])
    latest["volume_ratio"] = round(bars[-1]["volume"] / (sum(b["volume"] for b in bars[-21:-1]) / 20), 2) if len(bars) > 20 and sum(b["volume"] for b in bars[-21:-1]) > 0 else None
    latest["return20"] = round((close[-1] / close[-21] - 1) * 100, 2) if len(close) > 20 else None
    returns = [(close[i]/close[i-1]-1) for i in range(max(1,len(close)-20),len(close))]
    mean = sum(returns)/len(returns) if returns else 0
    # 按样本收益标准差乘以 252 的平方根年化，输出百分比口径。
    latest["volatility20"] = round((sum((x-mean)**2 for x in returns)/max(len(returns)-1,1))**0.5 * 252**0.5 * 100, 2) if len(returns)>=20 else None
    return {"series": series, "latest": latest, "missing": ["至少需要60根K线以完整计算指标"] if len(bars) < 60 else []}


# 按趋势、动量和成交量规则累加分数；评分不代表上涨概率。
def score(bars: list[dict], stats: dict) -> float:
    if len(bars) < 60:
        return 0
    c = bars[-1]["close"]
    result = 20 if c > stats["ma20"] else 0
    result += 20 if stats["ma5"] > stats["ma20"] > stats["ma60"] else 0
    result += 15 if stats["dif"] is not None and stats["dif"] > stats["dea"] else 0
    result += 15 if stats["rsi14"] is not None and 40 <= stats["rsi14"] <= 70 else 0
    result += 15 if stats["volume_ratio"] and 1 <= stats["volume_ratio"] <= 3 else 0
    result += 15 if stats["return20"] and stats["return20"] > 0 else 0
    return float(result)


# 技术分用于准入，含费情景与已取证的比较用于同一批合格证券的排序。
def research_score(candidate, relative_strength, official_evidence, financials, fees):
    technical = candidate["score"]
    price = candidate["price"]
    budget = candidate["budget"]
    asset = candidate["asset_class"]
    sell_fee = transaction_fees(price, candidate["quantity"], asset, "sell", fees)["total"]
    cost_pct = (budget["fees"]["total"] + sell_fee) / budget["gross"] * 100 + fees.slippage_bps * 2 / 100
    support = candidate["indicators"]["support20"]
    resistance = candidate["indicators"]["resistance20"]
    upside = (resistance / price - 1) * 100 - cost_pct
    downside = (1 - support / price) * 100 + cost_pct
    ratio = max(0.0, upside) / downside if downside > 0 else None
    financial_ok = asset == "etf" or bool(financials and financials["profit"] >= 0)
    components = {
        "technical": round(technical * 0.65, 2),
        "relative_strength": round(min(10, max(0, 5 + relative_strength["excess_pct"])), 2) if relative_strength else 0,
        "risk": 5 * (len(official_evidence) == 3) + 5 * financial_ok,
        "cost": round(max(0, 10 - 2 * cost_pct), 2),
        "reward_risk": round(min(5, 2.5 * (ratio or 0)), 2),
    }
    return {
        "version": CANDIDATE_SCORE_VERSION,
        "total": round(sum(components.values()), 2),
        "technical_score": technical,
        "components": components,
        "relative_strength": relative_strength,
        "risk": {"official_evidence": official_evidence, "financials": financials,
                 "financial_status": "not_applicable" if asset == "etf" else
                 "reported_loss" if financials and financials["profit"] < 0 else
                 "available" if financials else "unavailable"},
        "round_trip_cost_pct": round(cost_pct, 4),
        "fee_assumption": not fees.confirmed,
        "scenario": {"support": support, "resistance": resistance, "net_upside_pct": round(upside, 2),
                     "net_downside_pct": round(downside, 2),
                     "ratio": round(ratio, 2) if ratio is not None else None},
    }
