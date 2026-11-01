from __future__ import annotations


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
        row = {"ts": bar["ts"], **{f"ma{n}": round(sum(close[i-n+1:i+1]) / n, 5) if i >= n-1 else None for n in (5,10,20,60)}, "rsi14": round(rsi, 2) if i >= 14 else None, "atr14": round(sum(tr[i-13:i+1]) / 14, 5) if i >= 13 else None, "dif": round(dif, 5) if i >= 25 else None, "dea": round(signal, 5) if i >= 33 else None, "macd": round((dif-signal)*2, 5) if i >= 33 else None}
        series.append(row)
    latest = dict(series[-1])
    latest["support20"] = min(b["low"] for b in bars[-20:])
    latest["resistance20"] = max(b["high"] for b in bars[-20:])
    latest["volume_ratio"] = round(bars[-1]["volume"] / (sum(b["volume"] for b in bars[-21:-1]) / 20), 2) if len(bars) > 20 and sum(b["volume"] for b in bars[-21:-1]) > 0 else None
    latest["return20"] = round((close[-1] / close[-21] - 1) * 100, 2) if len(close) > 20 else None
    returns = [(close[i]/close[i-1]-1) for i in range(max(1,len(close)-20),len(close))]
    mean = sum(returns)/len(returns) if returns else 0
    latest["volatility20"] = round((sum((x-mean)**2 for x in returns)/max(len(returns)-1,1))**0.5 * 252**0.5 * 100, 2) if len(returns)>=20 else None
    return {"series": series, "latest": latest, "missing": ["至少需要60根K线以完整计算指标"] if len(bars) < 60 else []}


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
