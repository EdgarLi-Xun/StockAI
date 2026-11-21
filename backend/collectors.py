# 规范化外部行情、交易日历与公告数据，保存来源和时效性证据。
from __future__ import annotations

import json
import math
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time, timedelta
from urllib.parse import urlsplit

from . import db, instruments, trading_status
from .config import ROOT, TZ, now, timestamp
from .processes import module_command,hidden_process

DISCLOSURE_ERRORS = (ValueError, TypeError, RuntimeError, OSError, subprocess.TimeoutExpired)
SGE_HISTORY_SOURCE = "上海黄金交易所 · 日行情"
SGE_HISTORY_URL = "https://www.sge.com.cn/sjzx/quotation_daily_new"
SGE_HISTORY_CLOSE = time(15, 30)
SGE_HISTORY = {
    "SGE:AU99.99": ("Au99.99", "元/克"),
    "SGE:AGTD": ("Ag(T+D)", "元/千克"),
}


# 通过有超时限制的子进程执行外部采集，失败时不伪造数据。
def call(action: str, args=None, timeout=25) -> list[dict]:
    result = subprocess.run(module_command("backend.collector_worker",action,db.dumps(args or {})),cwd=ROOT,capture_output=True,text=True,encoding="utf-8",timeout=timeout,**hidden_process())
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError(f"{action} 未返回标准数据（采集进程退出 {result.returncode}）") from None
    if isinstance(value, dict) and "error" in value:
        raise ValueError(value["error"])
    if result.returncode or not isinstance(value, list) or (not value and action not in {"announcements", "sse_suspensions"}):
        raise ValueError(f"{action} 返回空数据或运行失败")
    return value


# 按市场收盘时间及当地时区解析时间，再统一转换为上海时区。
def parse_ts(value, daily=False, market="CN") -> str:
    dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if daily:
        if market=="US":
            from zoneinfo import ZoneInfo
            dt=datetime.combine(dt.date(),datetime.min.time()).replace(hour=16,tzinfo=ZoneInfo("America/New_York"))
        else:
            dt = datetime.combine(dt.date(), datetime.min.time()).replace(hour=16 if market=="HK" else 15, tzinfo=TZ)
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ).isoformat(timespec="seconds")


# 校验并规范化 OHLC、成交量和来源信息，剔除尚未完成的日线。
def normalize_bars(rows, source: str, url: str, timeframe="1d", volume_unit="lot", market="CN") -> list[dict]:
    result = []
    for row in rows:
        # 从可选的行情字段名读取数值，并统一处理缺失或非法输入。
        def val(en, cn):
            return row[en] if en in row else row.get(cn)
        ts = parse_ts(val("date", "日期") or row.get("时间") or row.get("day"), daily=timeframe == "1d",market=market)
        if datetime.fromisoformat(ts) > now():
            continue  # 尚未完成的日线不能作为已收盘行情持久化。
        bar = {k: float(val(k, cn)) for k, cn in (("open", "开盘"), ("high", "最高"), ("low", "最低"), ("close", "收盘"), ("volume", "成交量"))}
        if not all(math.isfinite(v) for v in bar.values()) or min(bar[k] for k in ("open", "high", "low", "close")) <= 0 or bar["volume"] < 0 or bar["low"] > min(bar["open"], bar["close"]) or bar["high"] < max(bar["open"], bar["close"]) or bar["low"] > bar["high"]:
            raise ValueError(f"行情数据校验失败 {ts}：零值、缺失或 OHLC 冲突")
        if volume_unit == "lot":
            bar["volume"] *= 100
        amount = val("amount", "成交额")
        amount = float(amount) if amount is not None else None
        if amount is not None and (not math.isfinite(amount) or amount < 0):
            raise ValueError("成交额无效")
        result.append({**bar, "ts": ts, "amount": amount, "adjustment": "none", "source": source, "source_url": url, "fetched_at": timestamp(), "quality": "valid", "metadata": {"volume_unit": "share", "price_unit": "CNY/share", "historical_status_verified": False, "corporate_actions_verified": False,"timestamp_basis":"交易日及该市场常规收盘时点，非逐笔时间" if timeframe=="1d" else "提供方分钟K线时点"}})
    if len({b["ts"] for b in result}) != len(result):
        raise ValueError("行情含有重复时间戳")
    return sorted(result, key=lambda b: b["ts"])


# 先校验整批行情，再在同一事务中保存，防止失败后留下部分数据。
def save_bars(ident: str, bars: list[dict], timeframe="1d"):
    from urllib.parse import urlsplit
    with db.connection() as c:
        for b in bars:
            dt = datetime.fromisoformat(b["ts"])
            if dt.tzinfo is None or dt > now() or b["adjustment"] not in {"none", "qfq", "hfq"} or not b.get("source") or urlsplit(b.get("source_url", "")).scheme not in {"http", "https"}:
                raise ValueError("缺少复权口径、来源或有效报价时间")
            values = [float(b[k]) for k in ("open", "high", "low", "close", "volume")]
            if not all(math.isfinite(x) for x in values) or min(values[:4]) <= 0 or values[4] < 0 or b["low"] > min(b["open"],b["close"]) or b["high"] < max(b["open"],b["close"]) or b["high"] < b["low"]:
                raise ValueError("OHLCV 数据不合法")
            c.execute("""INSERT INTO bars(instrument_id,timeframe,ts,open,high,low,close,volume,amount,adjustment,source,source_url,fetched_at,quality,metadata_json)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(instrument_id,timeframe,ts) DO UPDATE SET
                open=excluded.open,high=excluded.high,low=excluded.low,close=excluded.close,volume=excluded.volume,
                amount=excluded.amount,adjustment=excluded.adjustment,source=excluded.source,source_url=excluded.source_url,
                fetched_at=excluded.fetched_at,quality=excluded.quality,metadata_json=excluded.metadata_json""",
                (ident,timeframe,b["ts"],b["open"],b["high"],b["low"],b["close"],b["volume"],b.get("amount"),b["adjustment"],b["source"],b["source_url"],b.get("fetched_at",timestamp()),b.get("quality","valid"),db.dumps(b.get("metadata",{}))))


# 按证券、周期和条数读取行情，并恢复为时间升序及结构化元数据。
def bars_for(ident: str, timeframe="1d", limit=2000) -> list[dict]:
    rows = db.query("SELECT * FROM (SELECT * FROM bars WHERE instrument_id=? AND timeframe=? ORDER BY ts DESC LIMIT ?) ORDER BY ts", (ident, timeframe, limit))
    for b in rows:
        b["metadata"] = json.loads(b.pop("metadata_json"))
    return rows


# 从已采集的交易日历判断交易日与最近完成场次，缺失时返回未知。
def calendar_context(at=None) -> dict:
    at = at or now()
    day = at.date().isoformat()
    bounds = db.one("SELECT MIN(day) AS first, MAX(day) AS last FROM calendar")
    known = bool(bounds and bounds["first"] and bounds["first"] <= day <= bounds["last"])
    trading = bool(db.one("SELECT day FROM calendar WHERE day=?", (day,))) if known else None
    latest = db.one("SELECT MAX(day) AS day FROM calendar WHERE day<? OR (day=? AND ? >= '15:00')", (day, day, at.strftime("%H:%M")))
    return {"date": day, "timezone": "Asia/Shanghai", "is_trading_day": trading, "calendar_verified": known, "last_completed_session": latest["day"] if latest else None, "note": "日线为已完成交易日收盘数据" if known else "交易日历未覆盖当前日期，禁止按工作日推算交易日"}


# 对照已核验日历检查报价时效，返回过期或无法确认的原因。
def fresh_reasons(bar: dict | None, context: dict | None = None) -> list[str]:
    if not bar:
        return ["没有已确认行情"]
    context = context or calendar_context()
    errors=[]
    if not context["calendar_verified"] or not context["last_completed_session"]:
        errors.append("交易日历未核验")
    elif bar["ts"][:10] != context["last_completed_session"]:
        errors.append("行情不是最近已完成交易日，已过期")
    if bar.get("quality") != "valid" or bar.get("adjustment") != "none":
        errors.append("行情质量或成交价格复权口径不合格")
    if not bar.get("source_url") or not bar.get("source"):
        errors.append("缺少价格来源")
    if datetime.fromisoformat(bar["ts"]) > now():
        errors.append("行情时间在未来")
    return errors


# 按资产和市场选择行情源，主源失败时尝试允许的备用源。
def sync_instrument(item: dict, timeframe="1d") -> dict:
    if item["asset_class"]=="fund":
        try:
            rows=call("fund_nav",{"symbol":item["symbol"]},timeout=20)
            row=sorted(rows,key=lambda r:str(r.get("净值日期","")))[-1]
            day=str(row["净值日期"])[:10]
            value=float(row["单位净值"])
            if not math.isfinite(value) or value<=0 or day>now().date().isoformat():
                raise ValueError("单位净值或估值日期无效")
            payload={"id":item["id"],"price":value,"quote_time":day+"T00:00:00+08:00","valuation_date":day,"timestamp_basis":"净值估值日期，具体披露时刻未知","fetched_at":timestamp(),"source":"AKShare / 东方财富基金净值","source_url":f"https://fund.eastmoney.com/{item['symbol']}.html","currency":"CNY","unit":"元/基金份额净值","kind":"nav","status":"success"}
            db.execute("INSERT INTO market_quotes VALUES(?,?) ON CONFLICT(instrument_id) DO UPDATE SET payload_json=excluded.payload_json",(item["id"],db.dumps(payload)))
            return {"id":item["id"],"status":"success","valuation_date":day,"message":"场外净值独立观察，不可按场内股票规则模拟买卖"}
        except Exception as exc:
            return {"id":item["id"],"status":"unavailable","message":str(exc)[:400]}
    if item["asset_class"] in {"metal", "future"}:
        return {"id": item["id"], "status": "observed", "message": "独立观察渠道"}
    existing = bars_for(item["id"], timeframe, 1)
    start = (datetime.fromisoformat(existing[-1]["ts"]) - timedelta(days=3)).strftime("%Y%m%d") if existing else (now()-timedelta(days=800)).strftime("%Y%m%d")
    args = {**{k:item[k] for k in ("symbol", "market", "asset_class")}, "start": start, "end": now().strftime("%Y%m%d")}
    source = "AKShare / 新浪财经" if item["market"]=="US" else "AKShare / 东方财富"
    url = f"https://finance.sina.com.cn/stock/usstock/cn/{item['symbol'].lower()}.shtml" if item["market"]=="US" else f"https://quote.eastmoney.com/{item['market'].lower()}{item['symbol']}.html"
    try:
        if timeframe in {"15m", "60m"}:
            args["period"] = timeframe[:-1]
            raw = call("minute", args, 20)
        else:
            try:
                raw = call("daily", args, 20)
            except (ValueError, subprocess.TimeoutExpired) as first_error:
                try:
                    raw = call("daily_backup", args, 20)
                except (ValueError,subprocess.TimeoutExpired) as backup_error:
                    raise ValueError(f"主源：{first_error}；备用源：{backup_error}") from None
                source = "AKShare / 新浪财经（备用）"
                url = f"https://finance.sina.com.cn/realstock/company/{item['market'].lower()}{item['symbol']}/nc.shtml"
        if timeframe=="1d":
            first_day=args["start"][:4]+"-"+args["start"][4:6]+"-"+args["start"][6:]
            raw=[r for r in raw if str(r.get("date",r.get("日期",r.get("day",""))))[:10]>=first_day]
        bars = normalize_bars(raw, source, url, timeframe, "share" if "备用" in source or item["market"]=="US" else "lot",item["market"])
        for b in bars:
            b["metadata"]["currency"]="HKD" if item["market"]=="HK" else "USD" if item["market"]=="US" else "CNY"
            b["metadata"]["price_unit"]="index_points" if item["asset_class"]=="index" else b["metadata"]["currency"]+"/share"
        if not bars:
            raise ValueError("没有已完成的有效K线")
        save_bars(item["id"], bars, timeframe)
        return {"id": item["id"], "status": "success", "bars": len(bars), "last_quote_time": bars[-1]["ts"], "source": source}
    except (ValueError, subprocess.TimeoutExpired, OSError, TypeError, KeyError, OverflowError) as exc:
        return {"id": item["id"], "status": "unavailable", "message": str(exc)[:400]}


# 同步证券名称和交易日历，独立于K线采集及交易资格核验。
def _sync_reference_data(actions,log):
    result = {"sources": [], "errors": []}
    for action in actions:
        try:
            if action == "sse_suspensions":
                end = now().date()
                raw = call(action, {"start": (end-timedelta(days=30)).strftime("%Y%m%d"), "end": end.strftime("%Y%m%d")}, timeout=25)
            else:
                raw = call(action, timeout=25)
            if action == "calendar":
                days = sorted({str(r["trade_date"])[:10] for r in raw})
                for d in days:
                    datetime.strptime(d, "%Y-%m-%d")
                with db.connection() as c:
                    c.executemany("INSERT INTO calendar(day,source,fetched_at) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET fetched_at=excluded.fetched_at", [(d,"新浪财经交易日历",timestamp()) for d in days])
            elif action in {"delist_sh","delist_sz"}:
                _store_delisting(action,raw)
            elif action=="sse_suspensions":
                _store_sse_status(raw)
            elif action=="etf_names":
                with db.connection() as c:
                    for row in raw:
                        sym=str(row.get("代码","")).zfill(6)
                        name=str(row.get("名称","")).strip()
                        if name:
                            c.execute("UPDATE instruments SET name=? WHERE symbol=? AND asset_class='etf' AND verified_at IS NULL",(name,sym))
            else:
                market = "SH" if action == "master_sh" else "SZ"
                url = "https://www.sse.com.cn/assortment/stock/list/share/" if market == "SH" else "https://www.szse.cn/market/product/stock/list/index.html"
                with db.connection() as c:
                    c.execute("BEGIN IMMEDIATE")
                    for row in raw:
                        sym = str(row.get("证券代码", row.get("A股代码", ""))).zfill(6)
                        name = str(row.get("证券简称", row.get("A股简称", ""))).strip()
                        if not name or name == "None":
                            continue
                        _store_listing(c,market,sym,name,url,row)
                # 仅出现在上市证券清单中，不能证明公告风险或停牌状态已经核验。
            result["sources"].append({"name": action, "status": "success", "count": len(raw)})
            log(f"{action} 已同步 {len(raw)} 条记录")
        except Exception as exc:
            result["errors"].append({"source": action, "error": str(exc)[:300]})
            log(f"{action} 不可用：{str(exc)[:250]}；保留缓存并标记缺失")
    result["status"]="success" if not result["errors"] else "partial" if result["sources"] else "unavailable"
    return result


def _store_delisting(action,rows):
    market="SH" if action=="delist_sh" else "SZ"
    url="https://www.sse.com.cn/assortment/stock/list/delisting/" if market=="SH" else "https://www.szse.cn/market/stock/suspend/index.html"
    at=timestamp()
    today=at[:10]
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        for row in rows:
            symbol=str(row.get("公司代码") if market=="SH" else row.get("证券代码"))
            effective=str(row.get("暂停上市日期") if market=="SH" else row.get("终止上市日期"))[:10]
            if not re.fullmatch(r"\d{1,6}",symbol) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",effective) or effective>today:
                continue
            ident=f"{market}:{symbol.zfill(6)}"
            item=c.execute("SELECT name,status,evidence_json FROM instruments WHERE id=? AND asset_class='stock'",(ident,)).fetchone()
            if not item:
                continue
            evidence=json.loads(item["evidence_json"])
            if evidence.get("exchange_listing_date","")>effective:
                continue
            prior=evidence.get("exchange_delisting")
            if prior and prior.get("effective_date")==effective and item["status"]=="delisted":
                continue
            evidence["exchange_delisting"]={"url":url,"effective_date":effective,"fetched_at":at}
            name=row.get("公司简称") if market=="SH" else row.get("证券简称")
            name=name.strip() if isinstance(name,str) else item["name"]
            c.execute("UPDATE instruments SET name=?,status='delisted',evidence_json=? WHERE id=?",(name or item["name"],db.dumps(evidence),ident))
            c.execute("INSERT INTO risk_events(instrument_id,event_type,severity,title,source_url,verified_at,payload_json) VALUES(?,?,?,?,?,?,?)",
                      (ident,"exchange_delisting","warning",f"交易所终止上市名单 · {effective}",url,at,db.dumps(evidence["exchange_delisting"])))


def _store_listing(c,market,symbol,name,url,row):
    item=c.execute("SELECT * FROM instruments WHERE market=? AND symbol=? AND asset_class='stock'",(market,symbol)).fetchone()
    if not item:
        return
    evidence=json.loads(item["evidence_json"])
    listed_on=str(row.get("上市日期") or row.get("A股上市日期") or "")[:10]
    status=item["status"]
    verified=item["verified_at"]
    risk=instruments.name_risk(name) or item["risk_flag"]
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}",listed_on) and listed_on<=now().date().isoformat():
        evidence["exchange_listing_date"]=listed_on
        delisting=evidence.get("exchange_delisting")
        if delisting and listed_on>delisting["effective_date"]:
            evidence.pop("exchange_delisting")
            evidence.pop("trading_status_events", None)
            evidence.pop("manual_trading_status_at", None)
            status,verified,risk="unknown",None,instruments.name_risk(name) or "unknown"
    c.execute("UPDATE instruments SET name=?,source_url=?,status=?,risk_flag=?,verified_at=?,evidence_json=? WHERE id=?",
              (name,url,status,risk,verified,db.dumps(evidence),item["id"]))


# 只请求尚有占位名称的市场，避免新增自选触发整站历史行情采集。
def sync_names(log=lambda m: None) -> dict:
    pending={(r["market"],r["asset_class"]) for r in db.query("""SELECT DISTINCT market,asset_class FROM instruments
        WHERE market IN('SH','SZ') AND asset_class IN('stock','etf')
        AND (name LIKE '%待核验%' OR trim(name)='')""")}
    actions=[]
    if ("SH","stock") in pending:
        actions.append("master_sh")
    if ("SZ","stock") in pending:
        actions.append("master_sz")
    if any(asset=="etf" for _,asset in pending):
        actions.append("etf_names")
    result=_sync_reference_data(actions,log)
    result["unresolved"]=[r["id"] for r in db.query("""SELECT id FROM instruments
        WHERE market IN('SH','SZ') AND asset_class IN('stock','etf')
        AND (name LIKE '%待核验%' OR trim(name)='') ORDER BY id""")]
    if result["unresolved"]:
        result["status"]="partial" if result["sources"] else "unavailable"
    return result


# 逐项同步自选证券并汇总成功、失败与缺失信息。
def sync_all(log=lambda m: None) -> dict:
    result=_sync_reference_data(("calendar","master_sh","master_sz","etf_names","delist_sh","delist_sz"),log)
    result["instruments"]=[]
    items = [instruments.get(r["id"]) for r in db.query("SELECT id FROM instruments WHERE watched=1")]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(sync_instrument, item) for item in items]
        for future in as_completed(futures):
            item = future.result()
            result["instruments"].append(item)
            log(f"{item['id']} {item['status']}"+("："+item["message"] if item.get("message") else ""))
    result["metals"] = sync_metals(log)
    trading = sync_trading_status(items, log)
    result["sources"].extend(trading["sources"])
    result["errors"].extend(trading["errors"])
    result["disclosure_errors"] = trading["disclosure_errors"]
    succeeded = sum(x["status"] == "success" and not x.get("cache_only") for x in result["instruments"] + result["metals"])
    complete = all(x["status"] != "unavailable" for x in result["instruments"] + result["metals"])
    result["status"] = "success" if succeeded and not result["errors"] and not result["disclosure_errors"] and complete else "partial" if succeeded else "unavailable"
    return result


# 同步贵金属观察行情，保留现货和期货各自的数据口径。
def sync_metals(log=lambda m: None):
    results=[]
    for action in ("international_metals", "domestic_metals"):
        try:
            rows = call(action, timeout=20)
            for row in rows:
                name = str(row.get("名称", row.get("symbol", row.get("name", ""))))
                ident = "FX:XAUUSD" if "伦敦金" in name or name.upper() == "XAU" else "FX:XAGUSD" if "伦敦银" in name or name.upper() == "XAG" else "SHFE:AU0" if "黄金" in name or "AU" in name.upper() else "SHFE:AG0" if "白银" in name or "AG" in name.upper() else None
                if not ident:
                    continue
                price = float(row.get("最新价", row.get("current_price", row.get("price", 0))))
                date = row.get("日期", row.get("date"))
                time = row.get("行情时间", row.get("时间", row.get("time")))
                if not date or not time:
                    raise ValueError("提供方缺少实际报价日期或时间，拒绝用采集时间替代报价时间")
                ts = parse_ts(str(date)[:10] + "T" + str(time))
                if not math.isfinite(price) or price <= 0 or datetime.fromisoformat(ts) > now():
                    raise ValueError("贵金属价格或时间戳不合法")
                payload = {"id": ident, "price": price, "quote_time": ts, "fetched_at": timestamp(), "source": "AKShare / 新浪财经", "source_url": "https://finance.sina.com.cn/money/future/hf.html" if action == "international_metals" else "https://finance.sina.com.cn/futures/", "currency": "USD" if ident.startswith("FX") else "CNY", "unit": "美元/金衡盎司" if ident.startswith("FX") else "元/克" if "AU" in ident else "元/千克", "timezone": "Asia/Shanghai（提供方显示时间）", "delay_type": "提供方未声明延迟，非交易所直连", "kind": "spot" if ident.startswith("FX") else "continuous_future", "status": "success"}
                db.execute("INSERT INTO market_quotes VALUES(?,?) ON CONFLICT(instrument_id) DO UPDATE SET payload_json=excluded.payload_json", (ident,db.dumps(payload)))
                results.append(payload)
        except Exception as exc:
            results.append({"source":action,"status":"unavailable","error":str(exc)[:300]})
            log(f"{action} 不可用：缺失数据不填造")
    results.extend(_sync_gold_references(log))
    return results


def sge_last_closed_day(at):
    return (at.date() if at.time() >= SGE_HISTORY_CLOSE else at.date() - timedelta(days=1)).isoformat()


def _fetch_sge_history(ident):
    symbol, unit = SGE_HISTORY[ident]
    cutoff = sge_last_closed_day(now())
    series = {}
    for row in call("sge", {"symbol": symbol}, timeout=20):
        try:
            day = datetime.fromisoformat(row["date"]).date().isoformat()
            close = float(row["close"])
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        if day > cutoff or not math.isfinite(close) or close <= 0:
            continue
        if day in series and series[day] != close:
            raise ValueError("上金所同一交易日返回了不同收盘价")
        series[day] = close
    if not series:
        raise ValueError("无有效的上金所已发布日收盘价")

    key = f"metal_closes:{ident}"
    fetched_at = timestamp()
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        old = c.execute("SELECT payload_json FROM research_context WHERE id=?", (key,)).fetchone()
        cached = {row["date"]: row for row in json.loads(old["payload_json"])} if old else {}
        cached.update({day: {"date": day, "close": close, "fetched_at": fetched_at} for day, close in series.items()})
        days = sorted(cached)[-90:]
        history = [cached[day] for day in days]
        c.execute("""INSERT INTO research_context VALUES(?, 'metal_closes', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,as_of=excluded.as_of,
            fetched_at=excluded.fetched_at""",
            (key, ident, db.dumps(history), SGE_HISTORY_SOURCE, SGE_HISTORY_URL, days[-1], fetched_at))

    day = max(series)
    return dict(id=ident, price=series[day], quote_time=day, fetched_at=fetched_at,
                source=SGE_HISTORY_SOURCE, source_url=SGE_HISTORY_URL, unit=unit,
                kind="sge_daily", delay_type="日收盘，仅提供交易日期")


def sync_sge_history(ident):
    try:
        quote = _fetch_sge_history(ident)
        return {"id": ident, "status": "success", "last_quote_time": quote["quote_time"]}
    except (ValueError, KeyError, TypeError, IndexError, OSError, subprocess.TimeoutExpired) as exc:
        return {"id": ident, "status": "unavailable", "error": str(exc)[:300]}


def _sync_gold_references(log):

    def etf_close(ident):
        rows = bars_for(ident, limit=1)
        cache_only = bool(rows and not fresh_reasons(rows[-1]))
        if not cache_only:
            updated = sync_instrument(instruments.get(ident))
            if updated["status"] != "success":
                raise ValueError(updated.get("message") or "ETF 日线刷新失败")
            rows = bars_for(ident, limit=1)
        row = rows[-1]
        if row["quality"] != "valid" or row["adjustment"] != "none" or not (row["source_url"] and row["source"]):
            raise ValueError("ETF 日线来源或口径无效")
        return dict(id=ident, price=row["close"], quote_time=row["ts"], fetched_at=row["fetched_at"],
                    source=row["source"], source_url=row["source_url"], unit="元/份",
                    kind="etf_close", delay_type="已完成的交易日收盘价", cache_only=cache_only)

    for ident, fetch in (("SGE:AU99.99", _fetch_sge_history), ("SH:518880", etf_close)):
        try:
            quote = fetch(ident)
            cache_only = quote.pop("cache_only", False)
            quote.update(currency="CNY", timezone="Asia/Shanghai", status="success")
            db.execute("""INSERT INTO market_quotes VALUES(?,?) ON CONFLICT(instrument_id) DO UPDATE
                SET payload_json=excluded.payload_json
                WHERE json_extract(excluded.payload_json,'$.quote_time')>=json_extract(market_quotes.payload_json,'$.quote_time')""",
                       (ident, db.dumps(quote)))
            yield {**quote, "cache_only": True} if cache_only else quote
        except (ValueError, KeyError, TypeError, IndexError, OSError, subprocess.TimeoutExpired) as exc:
            log(f"{ident} 行情未刷新，保留已有缓存")
            yield {"id":ident,"status":"unavailable","error":str(exc)[:300]}


def _store_trading_events(c, ident, updates, at, *, disclosure=None):
    item = c.execute("SELECT status,evidence_json FROM instruments WHERE id=?", (ident,)).fetchone()
    evidence = json.loads(item["evidence_json"])
    previous = evidence.get("trading_status_events", [])
    verified = {}
    for event in updates:
        effective = trading_status.parse_local(event.get("effective_at"))
        expiry = trading_status.parse_local(event.get("expires_at"))
        url = event.get("url")
        if not effective or event.get("status") not in {"active", "suspended"} or not instruments.official_status_url(url):
            continue
        day = effective.date().isoformat()
        if day < evidence.get("exchange_listing_date", "") or (expiry and expiry <= effective):
            continue
        normalized = {**event, "effective_at": effective.isoformat(timespec="seconds")}
        verified[(event["notice_key"], normalized["effective_at"], event["status"])] = normalized
    if verified:
        replacement = {r["notice_key"] for r in verified.values()}
        events = [r for r in previous if r["notice_key"] not in replacement]
        before = {(r["notice_key"], r["effective_at"], r["status"]): r for r in previous}
        for key, event in verified.items():
            payload = db.dumps(event)
            old = before.get(key, {})
            fetched = old.get("fetched_at") if {k:v for k,v in old.items() if k != "fetched_at"} == event else None
            events.append({**event, "fetched_at": fetched or timestamp()})
            c.execute("""INSERT INTO risk_events(instrument_id,event_type,severity,title,source_url,verified_at,payload_json)
                SELECT ?,'trading_status',?,?,?,?,? WHERE NOT EXISTS(
                    SELECT 1 FROM risk_events WHERE instrument_id=? AND event_type='trading_status' AND payload_json=?)""",
                (ident, "warning" if event["status"] == "suspended" else "info",
                 ("停牌 · " if event["status"] == "suspended" else "复牌 · ") + event["title"],
                 event["url"], fetched or timestamp(), payload, ident, payload))
        events.sort(key=lambda r: r["effective_at"])
        evidence["trading_status_events"] = events[-24:]
    if disclosure is not None:
        attempted = trading_status.parse_local(disclosure["started_at"])
        for key in ("disclosure_check", "disclosure_failure"):
            previous_check = evidence.get(key, {})
            began = trading_status.parse_local(previous_check.get("started_at") or previous_check.get("fetched_at"))
            if began and began > attempted:
                break
        else:
            if disclosure["status"] != "unavailable":
                evidence["disclosure_check"] = disclosure
            if disclosure["status"] == "success":
                evidence.pop("disclosure_failure", None)
            else:
                evidence["disclosure_failure"] = disclosure
    status, _ = trading_status.current_status(item["status"], evidence, at)
    if updates or disclosure is not None or status != item["status"]:
        c.execute("UPDATE instruments SET status=?,evidence_json=? WHERE id=?", (status, db.dumps(evidence), ident))


def _store_sse_status(rows):
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")

        def next_session(day):
            return c.execute("SELECT MIN(day) FROM calendar WHERE day>?", (day,)).fetchone()[0]

        grouped = {}
        for row in rows:
            events = trading_status.sse_events(row, next_session)
            if events:
                grouped.setdefault("SH:" + row["productCode"], []).extend(events)
        at = now()
        for ident, events in grouped.items():
            if c.execute("SELECT 1 FROM instruments WHERE id=? AND asset_class IN ('stock','etf')", (ident,)).fetchone():
                _store_trading_events(c, ident, events, at)


def sync_trading_status(items, log=lambda m: None, *, refresh_identity=False):
    items = [item for item in items if item["market"] in {"SH", "SZ"} and item["asset_class"] in {"stock", "etf"}]
    actions = []
    if items and refresh_identity:
        if not db.one("SELECT day FROM calendar WHERE day>=? LIMIT 1", (now().date().isoformat(),)):
            actions.append("calendar")
        markets = {item["market"].lower() for item in items if item["asset_class"] == "stock"}
        for market in sorted(markets):
            actions.extend((f"master_{market}", f"delist_{market}"))
        if any(item["asset_class"] == "etf" for item in items):
            actions.append("etf_names")
    if any(r["market"] == "SH" for r in items):
        actions.append("sse_suspensions")
    result = _sync_reference_data(actions, log)
    tracked = [instruments.get(item["id"]) for item in items] if refresh_identity else items
    result["disclosure_errors"] = sync_announcements(tracked, log)
    if result["errors"] or result["disclosure_errors"]:
        result["status"] = "partial" if result["sources"] or len(result["disclosure_errors"]) < len(items) else "unavailable"
    else:
        result["status"] = "success"
    return result


# 采集指定证券的公告，并记录来源及抓取时间。
def announcements(ident: str):
    item = instruments.get(ident)
    if item["asset_class"] not in {"stock","etf"} or item["market"] not in {"SH", "SZ"}:
        raise ValueError("此公告适配器仅支持境内股票和场内 ETF")
    at=now()
    check = {"url": trading_status.CNINFO_NOTICE_URL, "started_at": at.isoformat(timespec="microseconds")}
    try:
        raw = call("announcements", {"symbol": item["symbol"], "asset_class":item["asset_class"],"start": (at-timedelta(days=30)).strftime("%Y%m%d"), "end": at.strftime("%Y%m%d")}, timeout=25)
    except DISCLOSURE_ERRORS:
        with db.connection() as c:
            c.execute("BEGIN IMMEDIATE")
            _store_trading_events(c, ident, [], now(), disclosure={**check, "status": "unavailable", "fetched_at": timestamp()})
        raise
    out=[]; updates=[]; incomplete=0
    with db.connection() as c:
        c.execute("BEGIN IMMEDIATE")
        for r in raw:
            title = str(r.get("公告标题", r.get("announcementTitle", "")))
            url = str(r.get("公告链接", r.get("adjunctUrl", "")))
            if url.startswith("/"):
                url = "https://static.cninfo.com.cn" + url
            if url.startswith("http://"):
                url = "https://" + url[7:]
            code=r.get("代码") or r.get("证券代码") or r.get("secCode")
            if not title or not instruments.official_url(url) or code is not None and str(code).zfill(6)!=item["symbol"]:
                continue
            status_events = trading_status.notice_events(r, item["symbol"], title, url, at) if instruments.official_status_url(url) else []
            updates.extend(status_events)
            incomplete += bool(r.get("status_read_error") and not status_events)
            if c.execute("SELECT 1 FROM risk_events WHERE instrument_id=? AND source_url=? AND event_type='announcement'", (ident,url)).fetchone():
                continue
            business_risk = any(k in title for k in ("风险警示", "终止上市", "退市", "立案", "处罚", "清算"))
            unresolved_halt = not status_events and any(k in title for k in ("停牌", "停复牌", "暂停交易"))
            severity = "warning" if business_risk or unresolved_halt or any(event["status"] == "suspended" for event in status_events) else "info"
            c.execute("INSERT INTO risk_events(instrument_id,event_type,severity,title,source_url,verified_at,payload_json) VALUES(?,?,?,?,?,?,?)", (ident,"announcement",severity,title,url,timestamp(),db.dumps(r)))
            if business_risk or unresolved_halt:
                c.execute("UPDATE instruments SET risk_flag='warning' WHERE id=? AND risk_flag<>'st'", (ident,))
            out.append({"title": title, "source_url": url, "severity": severity})
        _store_trading_events(c, ident, updates, now(), disclosure={
            **check, "fetched_at": timestamp(), "status": "partial" if incomplete else "success",
            "unread_status_notices": incomplete})
    return out


def sync_announcements(items,log=lambda m:None):
    def refresh(item):
        try:
            evidence = item.get("evidence", {})
            failure = evidence.get("disclosure_failure")
            disclosure = evidence.get("disclosure_check", {})
            previous = failure or disclosure
            checked = trading_status.parse_local(previous.get("fetched_at"))
            cached_for = timedelta(minutes=15) if failure or disclosure.get("unread_status_notices") else timedelta(hours=4)
            source_unavailable = failure and failure.get("status") == "unavailable"
            if source_unavailable or not checked or checked < now() - cached_for:
                announcements(item["id"])
                evidence = instruments.get(item["id"])["evidence"]
                failure = evidence.get("disclosure_failure")
                disclosure = evidence.get("disclosure_check", {})
            if failure and failure.get("status") == "unavailable":
                return {"instrument_id":item["id"],"error":"官方公告查询最近一次刷新失败"}
            if disclosure.get("unread_status_notices"):
                return {"instrument_id":item["id"],"error":"有停复牌公告附件暂未读取"}
        except DISCLOSURE_ERRORS as exc:
            return {"instrument_id":item["id"],"error":str(exc)[:250]}
        return None

    tracked=(item for item in items if item["market"] in {"SH","SZ"} and item["asset_class"] in {"stock","etf"} and item["status"]!="delisted")
    with ThreadPoolExecutor(max_workers=6) as pool:
        errors=[error for error in pool.map(refresh,tracked) if error]
    for error in errors:
        log(f"{error['instrument_id']} 官方公告未能刷新：{error['error']}")
    return errors


# 自选里的股票、ETF、指数和基金，外加总览要展示的沪深指数。日线任务不走这里。
def _spot_targets() -> list[dict]:
    names = {row["name"] for row in db.query("SELECT name FROM sqlite_master WHERE type='table' AND name IN ('user_watchlist','users')")}
    if {"user_watchlist", "users"} <= names:
        return db.query("""SELECT DISTINCT id,symbol,market,asset_class FROM (
            SELECT i.id,i.symbol,i.market,i.asset_class FROM instruments i
            JOIN user_watchlist w ON w.instrument_id=i.id JOIN users u ON u.id=w.user_id
            WHERE u.status='active' AND i.market IN ('SH','SZ','HK','US','FUND') AND i.asset_class IN ('stock','etf','index','fund')
            UNION
            SELECT id,symbol,market,asset_class FROM instruments WHERE asset_class='index' AND market IN ('SH','SZ'))""")
    return db.query("""SELECT id,symbol,market,asset_class FROM instruments
        WHERE market IN ('SH','SZ','HK','US','FUND') AND asset_class IN ('stock','etf','index','fund')
        AND (watched=1 OR (asset_class='index' AND market IN ('SH','SZ')))""")


# 东方财富批量行情的证券编号。美股交易所不在代码里，三处都问，有价的写回。
def _secids(item: dict) -> list[str]:
    market, symbol = item["market"], item["symbol"]
    if market == "SH":
        return ["1." + symbol]
    if market == "SZ":
        return ["0." + symbol]
    if market == "HK":
        return ["116." + symbol.zfill(5)]
    if market == "US":
        return [prefix + "." + symbol for prefix in ("105", "106", "107")]
    return []


# 向东方财富批量要最新价。不导入 AKShare，避免每 15 秒拉起整套行情库。
def fetch_spots(secids: list[str]) -> list[dict]:
    import requests
    response = requests.get(
        "https://push2.eastmoney.com/api/qt/ulist.np/get",
        params={"fltt": "2", "invt": "2", "fields": "f12,f13,f14,f2,f3,f5,f6,f15,f16,f17,f18,f124", "secids": ",".join(secids)},
        headers={"Referer": "https://quote.eastmoney.com/", "User-Agent": "Mozilla/5.0"},
        timeout=12,
    )
    response.raise_for_status()
    diff = ((response.json() or {}).get("data") or {}).get("diff") or []
    if isinstance(diff, dict):
        diff = list(diff.values())
    return [row for row in diff if isinstance(row, dict)]


# 提供方时间转成上海时间。缺失或明显超前时不编造报价时点。
def _spot_time(raw) -> str | None:
    try:
        value = int(float(raw))
    except (TypeError, ValueError):
        return None
    if value > 10**12:
        value //= 1000
    if value <= 0:
        return None
    moment = datetime.fromtimestamp(value, TZ)
    if moment > now() + timedelta(minutes=5):
        return None
    return moment.isoformat(timespec="seconds")


# 最新价比已完成日线更晚时，列表才改用它。研究仍读日线。
def newer_spot(quote, spot) -> dict | None:
    if not isinstance(spot, dict) or spot.get("kind") != "spot" or not spot.get("quote_time"):
        return None
    try:
        price = float(spot["price"])
    except (TypeError, ValueError, KeyError):
        return None
    if not math.isfinite(price) or price <= 0:
        return None
    bar_ts = str((quote or {}).get("ts") or "")
    if bar_ts and str(spot["quote_time"]) <= bar_ts:
        return None
    return spot


# 把一批最新价写入观察报价，不改 K 线。
def _prefer_us(rows: list[dict], item: dict | None = None) -> dict | None:
    priced = []
    for row in rows:
        try:
            price = float(row.get("f2"))
        except (TypeError, ValueError):
            continue
        if math.isfinite(price) and price > 0:
            priced.append(row)
    if not priced:
        return None
    item = item or {}
    name = str(item.get("name") or "")
    upper = name.upper()
    prefer = "105" if any(word in upper or word in name for word in ("NASDAQ", "纳斯达克")) else "106" if any(word in upper or word in name for word in ("NYSE", "纽交所")) else "107" if any(word in upper or word in name for word in ("AMEX", "美交所")) else ""
    folded = "".join(ch.lower() for ch in name if ch.isalnum())
    def amount(row):
        try:
            value = float(row.get("f6") or 0)
        except (TypeError, ValueError):
            return 0
        return value if math.isfinite(value) and value > 0 else 0
    def score(row):
        quote = "".join(ch.lower() for ch in str(row.get("f14") or "") if ch.isalnum())
        named = 3 if folded and quote and (folded in quote or quote in folded) else 1 if folded[:4] and quote.startswith(folded[:4]) else 0
        return (named, 2 if prefer and str(row.get("f13")) == prefer else 0, amount(row))
    return max(priced, key=score)


def _match_spot(row: dict, by_key: dict) -> dict | None:
    code = str(row.get("f12") or "")
    if code.endswith(".0"):
        code = code[:-2]
    if code.isdigit():
        code = code.zfill(6)
    market = {"1": "SH", "0": "SZ", "116": "HK", "105": "US", "106": "US", "107": "US"}.get(str(row.get("f13")))
    item = by_key.get((market, code)) if market else None
    if item is None:
        matches = [value for (item_market, symbol), value in by_key.items() if symbol == code or symbol.lstrip("0") == code.lstrip("0")]
        item = matches[0] if len(matches) == 1 else None
    return item


def _store_spots(rows: list[dict], by_key: dict, fetched: str) -> int:
    updated = 0
    us_rows: dict[str, list[dict]] = {}
    for row in rows:
        item = _match_spot(row, by_key)
        if not item:
            continue
        if item["market"] == "US":
            us_rows.setdefault(item["id"], []).append(row)
            continue
        if _store_spot(item, row, fetched):
            updated += 1
    for ident, choices in us_rows.items():
        item = next(value for value in by_key.values() if value["id"] == ident)
        chosen = _prefer_us(choices, item)
        if chosen and _store_spot(item, chosen, fetched):
            updated += 1
    return updated


def _store_spot(item: dict, row: dict, fetched: str) -> bool:
    try:
        price = float(row.get("f2"))
    except (TypeError, ValueError):
        return False
    if not math.isfinite(price) or price <= 0 or str(row.get("f12") or "").lstrip("0") != item["symbol"].lstrip("0"):
        return False
    try:
        prev = float(row.get("f18"))
        if not math.isfinite(prev) or prev <= 0:
            prev = None
    except (TypeError, ValueError):
        prev = None
    try:
        change = float(row.get("f3"))
        if not math.isfinite(change):
            change = None
    except (TypeError, ValueError):
        change = None
    def optional(key):
        try:
            value = float(row.get(key))
        except (TypeError, ValueError):
            return None
        return value if math.isfinite(value) and value > 0 else None
    provider_time = _spot_time(row.get("f124"))
    if item["asset_class"] == "index":
        unit, url, currency = "点", f"https://quote.eastmoney.com/zs{item['symbol']}.html", "CNY"
    elif item["market"] == "HK":
        unit, url, currency = "港元/股", f"https://quote.eastmoney.com/hk/{item['symbol'].zfill(5)}.html", "HKD"
    elif item["market"] == "US":
        unit, url, currency = "美元/股", f"https://quote.eastmoney.com/us/{item['symbol']}.html", "USD"
    else:
        unit, url, currency = "元/股", f"https://quote.eastmoney.com/{item['market'].lower()}{item['symbol']}.html", "CNY"
    payload = {
        "id": item["id"], "price": price, "prev_close": prev, "change_pct": change,
        "open": optional("f17"), "high": optional("f15"), "low": optional("f16"), "volume": optional("f5"),
        "quote_time": provider_time or fetched, "fetched_at": fetched,
        "source": "东方财富最新价", "source_url": url,
        "currency": currency, "unit": unit, "kind": "spot", "status": "success",
        "timestamp_basis": "提供方最新价时点" if provider_time else "采集时刻，提供方未返回逐笔时间",
    }
    db.execute("INSERT INTO market_quotes VALUES(?,?) ON CONFLICT(instrument_id) DO UPDATE SET payload_json=excluded.payload_json", (item["id"], db.dumps(payload)))
    return True


# 场外基金用盘中估算，没有估算时用最近公布净值。不写日 K。
def _fetch_fund(item: dict) -> dict | str:
    import requests
    try:
        return _read_fund(item, requests)
    except Exception as exc:
        return str(exc)[:200]


def _read_fund(item: dict, requests) -> dict | str:
    response = requests.get(
        f"https://fundgz.1234567.com.cn/js/{item['symbol']}.js",
        headers={"Referer": "https://fund.eastmoney.com/"},
        timeout=8,
    )
    response.raise_for_status()
    text = response.text
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return "基金估值无数据"
    data = json.loads(text[start:end + 1])
    estimate, nav = data.get("gsz"), data.get("dwjz")
    try:
        price = float(estimate or nav)
    except (TypeError, ValueError):
        return "基金价格无效"
    if not math.isfinite(price) or price <= 0:
        return "基金价格无效"
    try:
        prev = float(nav) if estimate else None
        if prev is not None and (not math.isfinite(prev) or prev <= 0):
            prev = None
    except (TypeError, ValueError):
        prev = None
    raw_time = str(data.get("gztime") or "")
    quote_time = None
    if len(raw_time) >= 16 and raw_time[4] == "-" and raw_time[10] == " ":
        quote_time = raw_time[:10] + "T" + raw_time[11:16] + ":00+08:00"
    elif str(data.get("jzrq") or "")[:10]:
        quote_time = str(data["jzrq"])[:10] + "T00:00:00+08:00"
    try:
        change = float(data["gszzl"]) if data.get("gszzl") not in (None, "") else None
    except (TypeError, ValueError):
        change = None
    return {"price": price, "prev_close": prev, "change_pct": change, "quote_time": quote_time, "estimated": bool(estimate)}


def _save_fund(item: dict, value: dict, fetched: str) -> None:
    quote_time = value.get("quote_time") or fetched
    payload = {
        "id": item["id"], "price": value["price"], "prev_close": value.get("prev_close"), "change_pct": value.get("change_pct"),
        "quote_time": quote_time, "fetched_at": fetched, "source": "东方财富基金估值" if value.get("estimated") else "东方财富基金净值",
        "source_url": f"https://fund.eastmoney.com/{item['symbol']}.html", "currency": "CNY", "unit": "元/份",
        "kind": "spot", "status": "success",
        "timestamp_basis": "盘中估算时点" if value.get("estimated") else "最近公布净值日期",
    }
    db.execute("INSERT INTO market_quotes VALUES(?,?) ON CONFLICT(instrument_id) DO UPDATE SET payload_json=excluded.payload_json", (item["id"], db.dumps(payload)))


# 每轮刷新自选和总览指数的最新价。失败时保留上一笔，不改日 K。
def sync_spots() -> dict:
    targets = _spot_targets()
    if not targets:
        return {"status": "success", "updated": 0}
    quoted = [item for item in targets if item["asset_class"] != "fund"]
    funds = [item for item in targets if item["asset_class"] == "fund"]
    by_key = {(item["market"], item["symbol"]): item for item in quoted}
    secids, seen = [], set()
    for item in quoted:
        for secid in _secids(item):
            if secid not in seen:
                seen.add(secid)
                secids.append(secid)
    updated, errors, fetched = 0, [], timestamp()
    for start in range(0, len(secids), 40):
        chunk = secids[start:start + 40]
        try:
            updated += _store_spots(fetch_spots(chunk), by_key, fetched)
        except Exception as exc:
            errors.append(str(exc)[:200])
    if funds:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as pool:
            fetched_funds = list(pool.map(lambda item: (item, _fetch_fund(item)), funds))
        for item, value in fetched_funds:
            if isinstance(value, str):
                errors.append(value)
            else:
                _save_fund(item, value, fetched)
                updated += 1
    status = "success" if updated and not errors else "partial" if updated else "unavailable" if errors else "success"
    return {"status": status, "updated": updated, "errors": errors}
