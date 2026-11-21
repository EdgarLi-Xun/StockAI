# 构建可在两个研究入口复用的报告事实。
from __future__ import annotations

import json
import math
import statistics
from datetime import date, datetime

from . import analytics, trading_status
from .collectors import SGE_HISTORY, SGE_HISTORY_URL, fresh_reasons, sge_last_closed_day
from .config import TZ
from .instruments import GOLD_REFERENCE_INSTRUMENTS, METAL_LINKED_SECURITIES, official_url

METAL_NAMES = {
    "FX:XAUUSD": "伦敦金现货",
    "FX:XAGUSD": "伦敦银现货",
    "SHFE:AU0": "沪金主力连续",
    "SHFE:AG0": "沪银主力连续",
    **{f"{market}:{symbol}": name for market, symbol, name, _ in GOLD_REFERENCE_INSTRUMENTS},
}
BENCHMARKS = {"SH": "SH:000001", "SZ": "SZ:399001"}
CLOSING_STATUS = {"retained": "继续入选", "removed": "收盘未入选", "added": "收盘新增"}
SIGNAL_RESULTS = {"created": "已生成模拟委托", "skipped": "跳过", "no_trigger": "未触发",
                  "rejected": "未通过模拟规则"}
METAL_RETURN_WINDOW = 20
MIN_METAL_SAMPLES = 10


def add_source(sources, instrument_id, url, source, quote_time, collected_at):
    source_id = f"s{len(sources) + 1}"
    sources.append(dict(id=source_id, instrument_id=instrument_id, url=url, source=source,
                        quote_time=quote_time, collected_at=collected_at))
    return source_id


def excluded_security(item, reasons, last, sources, at):
    row = {"instrument_id": item["id"], "name": item["name"], "reasons": list(dict.fromkeys(reasons)),
           "price": last["close"] if last else None}
    planned = trading_status.scheduled_halt(item, at) if item["status"] == "active" else None
    if event := planned or item["evidence"].get("exchange_trading_status"):
        label = "官方停复牌证据"
        collected = event.get("fetched_at")
        source = add_source(sources, item["id"], event["url"], label, event.get("published_at") or collected, collected)
        row["trading_status_evidence"] = {"url": event["url"], "effective_at": event["effective_at"], "source_id": source}
    return row


def gold_silver_ratio(quotes, at):
    by_id = {q["id"]: q for q in quotes}
    gold, silver = (by_id.get(key, {}) for key in ("FX:XAUUSD", "FX:XAGUSD"))
    if not all(q.get("price") and q.get("quote_time") and q.get("source_url")
               and q.get("kind") == "spot" and q.get("currency") == "USD"
               and q.get("unit") == "美元/金衡盎司" for q in (gold, silver)):
        return None
    try:
        gold_time, silver_time = (datetime.fromisoformat(q["quote_time"]) for q in (gold, silver))
        gold_price, silver_price = (float(q["price"]) for q in (gold, silver))
        if (not all(math.isfinite(p) and p > 0 for p in (gold_price, silver_price))
                or max(gold_time, silver_time) > at or abs((gold_time - silver_time).total_seconds()) > 60):
            return None
    except (TypeError, ValueError):
        return None
    return {
        "value": round(gold_price / silver_price, 2),
        "gold_quote_time": gold["quote_time"],
        "silver_quote_time": silver["quote_time"],
        "stale": bool(gold.get("stale") or silver.get("stale")),
        "source_ids": gold.get("source_ids", []) + silver.get("source_ids", []),
    }


def quote_time(quote):
    raw = quote["quote_time"]
    if quote.get("kind") == "sge_daily" and len(raw) == 10:
        return datetime.combine(date.fromisoformat(raw), datetime.min.time(), TZ)
    return datetime.fromisoformat(raw)


def quote_stale(quote, at, *, spot_hours=1):
    try:
        seconds = (at - quote_time(quote)).total_seconds()
    except (KeyError, ValueError, TypeError):
        return True
    cutoff = 24 if quote.get("kind") in {"sge_daily", "etf_close"} else spot_hours
    return seconds < 0 or seconds > cutoff * 3600


def attach_metals(facts, quotes, at):
    by_id = {q.get("id"): q for q in quotes}
    metals, missing = [], []
    for ident, name in METAL_NAMES.items():
        q = by_id.get(ident, {})
        try:
            quoted = quote_time(q)
            price = float(q["price"])
            if not (math.isfinite(price) and price > 0 and quoted <= at
                    and all(q.get(k) for k in ("source", "source_url", "fetched_at", "currency", "unit"))
                    and q.get("status", "success") == "success"):
                raise ValueError("报价未核验")
        except (TypeError, ValueError, KeyError):
            missing.append(name)
            continue
        source_id = add_source(facts["sources"], ident, q["source_url"], q["source"], q["quote_time"], q["fetched_at"])
        metals.append({**q, "name": name, "price": price, "source_ids": [source_id],
                       "stale": quote_stale(q, at)})
    facts["metals"] = metals
    facts["gold_silver_ratio"] = gold_silver_ratio(metals, at)
    if missing:
        facts["missing_data"].append("未取得可核验的贵金属报价：" + "、".join(missing))


def _session_returns(sessions, closes):
    return {end: math.log(closes[end]) - math.log(closes[start])
            for start, end in zip(sessions, sessions[1:]) if start in closes and end in closes}


def _correlation(returns, equity, pairs):
    matched = [(equity_day, returns[metal_day], equity[equity_day])
               for metal_day, equity_day in pairs if metal_day in returns and equity_day in equity]
    coefficient = None
    if len(matched) >= MIN_METAL_SAMPLES:
        _, x, y = zip(*matched)
        try:
            coefficient = round(statistics.correlation(x, y), 4)
        except statistics.StatisticsError:
            pass
    return {"coefficient": coefficient, "sample_count": len(matched),
            "first_session": matched[0][0] if matched else None, "last_session": matched[-1][0] if matched else None}


def attach_metal_study(facts, at, query, read_bars):
    cutoff = sge_last_closed_day(at)
    sessions = [row["day"] for row in reversed(query(
        "SELECT day FROM calendar WHERE day<=? ORDER BY day DESC LIMIT ?", (cutoff, METAL_RETURN_WINDOW + 2)))]
    session_set = set(sessions)
    names = {r["id"]: r["name"] for r in query(
        "SELECT id,name FROM instruments WHERE id IN (" + ",".join("?" for _ in METAL_LINKED_SECURITIES) + ")",
        tuple(METAL_LINKED_SECURITIES))}
    history = {row["instrument_id"]: row for row in query(
        "SELECT * FROM research_context WHERE category='metal_closes' AND instrument_id IN (?,?)", tuple(SGE_HISTORY))
        if row["source_url"] == SGE_HISTORY_URL}
    sources = facts["sources"]
    equities = {}
    for ident in METAL_LINKED_SECURITIES:
        bars = [b for b in read_bars(ident, 90) if b["ts"][:10] in session_set and b["quality"] == "valid"
                and b["adjustment"] == "none" and b["source_url"] and b["source"]
                and b["volume"] > 0 and math.isfinite(b["close"]) and b["close"] > 0]
        if not bars:
            continue
        last = bars[-1]
        closes = {b["ts"][:10]: b["close"] for b in bars
                  if (b["source"], b["source_url"]) == (last["source"], last["source_url"])}
        returns = _session_returns(sessions, closes)
        if returns:
            equities[ident] = (returns, add_source(sources, ident, last["source_url"], last["source"], last["ts"], last["fetched_at"]))

    prior_metal_day = list(zip(sessions, sessions[1:]))[-METAL_RETURN_WINDOW:]
    recent = [end for _, end in prior_metal_day]
    same_day = [(day, day) for day in recent]
    facts["metal_study"] = {
        "as_of": sessions[-1] if sessions else None, "window_sessions": METAL_RETURN_WINDOW,
        "minimum_samples": MIN_METAL_SAMPLES, "annual_sessions": 252, "series": [],
        "method": f"用最近 {METAL_RETURN_WINDOW} 个已完成交易日的相邻日对数收益计算，年化按 252 个交易日。"
                  "同期按同一收盘日期配对。上金所日盘收于 15:30，A 股日盘收于 15:00。"
                  "滞后按金银前一交易日收益与股票下一交易日收益配对。"
                  "股票和 ETF 使用未复权收盘价，除权除息会影响结果。相关系数描述历史联动。",
    }
    for ident, (contract, unit) in SGE_HISTORY.items():
        row = history.get(ident)
        points = {point["date"]: point for point in json.loads(row["payload_json"])
                  if point["date"] in session_set and math.isfinite(point["close"]) and point["close"] > 0} if row else {}
        closes = {day: point["close"] for day, point in points.items()}
        returns = _session_returns(sessions, closes)
        samples = {day: returns[day] for day in recent if day in returns}
        metal_sources = []
        if closes:
            latest = max(closes)
            metal_sources.append(add_source(sources, ident, row["source_url"], row["source"], latest, points[latest].get("fetched_at") or row["fetched_at"]))
        volatility = round(statistics.stdev(samples.values()) * math.sqrt(252) * 100, 4) if len(samples) >= MIN_METAL_SAMPLES else None
        linked = []
        for code, name in METAL_LINKED_SECURITIES.items():
            security_returns, source = equities.get(code, ({}, None))
            official_name = names.get(code, "")
            linked.append({"instrument_id": code, "name": official_name if official_name and "待核验" not in official_name else name,
                           "same_session": _correlation(returns, security_returns, same_day),
                           "next_session": _correlation(returns, security_returns, prior_metal_day),
                           "source_ids": metal_sources + ([source] if source else [])})
        facts["metal_study"]["series"].append({
            "instrument_id": ident, "contract": contract, "unit": unit, "source_ids": metal_sources,
            "annualized_volatility_pct": volatility, "sample_count": len(samples),
            "first_session": next(iter(samples), None), "last_session": next(reversed(samples), None), "linked": linked,
        })
        if volatility is None:
            facts["missing_data"].append(f"上金所 {contract} 年化波动率缺少足够的已完成交易日收盘样本（{len(samples)}/{MIN_METAL_SAMPLES}）。")
        if not any(r[k]["coefficient"] is not None for r in linked for k in ("same_session", "next_session")):
            facts["missing_data"].append(f"上金所 {contract} 与矿业股及黄金 ETF 缺少可计算的配对收益。")


def _profit(row, report_date):
    if row and row.get("source_url"):
        from .context import model_data
        periods = []
        for item in model_data("financials", json.loads(row["payload_json"])):
            try:
                day = date.fromisoformat(str(item.get("报告期"))[:10])
                reported = item.get("归属于上市公司股东的净利润")
                value = str(item.get("净利润") if reported in (None, "") else reported).strip()
                number = value.replace(",", "").removesuffix("元").strip()
                unit = next((u for u in ("万亿", "亿", "万") if number.endswith(u)), "")
                profit = float(number.removesuffix(unit)) * {"万亿": 1e12, "亿": 1e8, "万": 1e4, "": 1}[unit]
            except (TypeError, ValueError):
                continue
            if day.isoformat() <= report_date and math.isfinite(profit):
                periods.append((day.isoformat(), profit, value))
        if periods:
            period, profit, reported = max(periods, key=lambda entry: entry[0])
            return {"period": period, "profit": profit, "reported_profit": reported, "source_url": row["source_url"],
                    "source": row["source"], "fetched_at": row["fetched_at"]}
    return None


def rank_candidates(candidates, histories, bar_reader, financial_rows, sources, context, fees):
    financial_by_id = {r["instrument_id"]: r for r in sorted(financial_rows, key=lambda r: (r["as_of"], r["fetched_at"], r["id"]))}
    benchmark_bars, benchmark_sources = {}, {}
    for c in candidates:
        ident = c["instrument_id"]
        market = ident.split(":")[0]
        index = BENCHMARKS[market]
        if index not in benchmark_bars:
            benchmark_bars[index] = {b["ts"][:10]: b for b in bar_reader(index, limit=90)}
        bars = histories[ident]
        start, end = bars[-21], bars[-1]
        reference = [benchmark_bars[index].get(b["ts"][:10]) for b in (start, end)]
        comparison = None
        if all(b and b["quality"] == "valid" and b["adjustment"] == "none"
               and b["close"] > 0 and b["source_url"] and b["source"] for b in reference):
            first, last = reference
            benchmark_return = (last["close"] / first["close"] - 1) * 100
            excess = (end["close"] / start["close"] - 1) * 100 - benchmark_return
            comparison = {"instrument_id": index, "start_date": first["ts"][:10], "end_date": last["ts"][:10],
                          "benchmark_return_pct": round(benchmark_return, 2), "excess_pct": round(excess, 2)}
            for bar in reference:
                key = (index, bar["ts"])
                if key not in benchmark_sources:
                    benchmark_sources[key] = add_source(sources, index, bar["source_url"], bar["source"], bar["ts"], bar["fetched_at"])
                c["source_ids"].append(benchmark_sources[key])
        risk = {key: c["evidence"][key] for key in ("listing_url", "status_url", "announcement_url")
                if official_url(c["evidence"].get(key, ""))}
        profit = _profit(financial_by_id.get(ident), context["date"]) if c["asset_class"] == "stock" else None
        if profit:
            profit["source_id"] = add_source(sources, ident, profit["source_url"], profit["source"], profit["period"], profit["fetched_at"])
            c["source_ids"].append(profit["source_id"])
        c["score_details"] = analytics.research_score(c, comparison, risk, profit, fees)
        c["score"] = c["score_details"]["total"]
    candidates.sort(key=lambda c: (-c["score"], c["instrument_id"]))


def closing_followup(facts, morning, closing_bars, bar_reader, signals):
    ctx = facts["calendar"]
    if ctx["is_trading_day"] is not True or ctx["last_completed_session"] != ctx["date"]:
        return {"status": "close_unavailable", "message": "交易日历尚未确认当日已收盘。", "rows": [], "signals": []}
    signals = [{**row, "result_label": SIGNAL_RESULTS.get(row["result"], row["result"])} for row in signals]
    result = {"status": "no_morning_report", "message": "当天没有已发布的早报。", "rows": [], "signals": signals}
    if not morning:
        return result
    payload = json.loads(morning["payload_json"])
    if payload.get("calendar", {}).get("last_completed_session") == ctx["date"]:
        return {**result, "message": "当日第一份标准报告已使用当天收盘价，缺少盘前早报作为基准。"}
    previous = {c["instrument_id"]: c for c in payload["candidates"]}
    current = {c["instrument_id"]: c for c in facts["candidates"]}
    excluded = {c["instrument_id"]: c["reasons"] for c in facts["excluded"]}
    result.update(status="compared", message="按当日已归档早报与实际收盘数据对照。",
                  morning_report={"report_id": morning["id"], "generated_at": morning["generated_at"]},
                  morning_candidate_count=len(previous))
    for ident in dict.fromkeys([*previous, *current]):
        before, after = previous.get(ident), current.get(ident)
        row = before or after
        status = "added" if not before else "retained" if after else "removed"
        bar = closing_bars.get(ident)
        if ident not in closing_bars:
            bar = next(iter(bar_reader(ident, limit=1)), None)
        close = ({"price": bar["close"], "quote_time": bar["ts"], "source_url": bar["source_url"]}
                 if not fresh_reasons(bar, ctx) else None)
        baseline = {k: before[k] for k in ("price", "quote_time", "source_url")} if before else None
        reasons = excluded.get(ident, ["证券已不在本次研究范围"]) if status == "removed" else []
        result["rows"].append({
            "instrument_id": ident, "name": row["name"], "symbol": row["symbol"],
            "status": status, "status_label": CLOSING_STATUS[status],
            "morning": baseline, "closing": close, "reasons": reasons,
            "change_pct": round((close["price"] / baseline["price"] - 1) * 100, 2) if close and baseline else None,
        })
    return result
