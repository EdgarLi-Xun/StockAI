# 规范化外部行情、交易日历与公告数据，保存来源和时效性证据。
from __future__ import annotations

import json
import math
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from . import db, instruments
from .config import ROOT, TZ, now, timestamp
from .processes import module_command,hidden_process


# 通过有超时限制的子进程执行外部采集，失败时不伪造数据。
def call(action: str, args=None, timeout=25) -> list[dict]:
    result = subprocess.run(module_command("backend.collector_worker",action,db.dumps(args or {})),cwd=ROOT,capture_output=True,text=True,encoding="utf-8",timeout=timeout,**hidden_process())
    try:
        value = json.loads(result.stdout)
    except (ValueError, TypeError):
        raise ValueError(f"{action} 未返回标准数据（采集进程退出 {result.returncode}）") from None
    if isinstance(value, dict) and "error" in value:
        raise ValueError(value["error"])
    if result.returncode or not isinstance(value, list) or (not value and action!="announcements"):
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
            raw = call(action, timeout=25)
            if action == "calendar":
                days = sorted({str(r["trade_date"])[:10] for r in raw})
                for d in days:
                    datetime.strptime(d, "%Y-%m-%d")
                with db.connection() as c:
                    c.executemany("INSERT INTO calendar(day,source,fetched_at) VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET fetched_at=excluded.fetched_at", [(d,"新浪财经交易日历",timestamp()) for d in days])
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
                    for row in raw:
                        sym = str(row.get("证券代码", row.get("A股代码", ""))).zfill(6)
                        name = str(row.get("证券简称", row.get("A股简称", ""))).strip()
                        if not name or name == "None":
                            continue
                        c.execute("UPDATE instruments SET name=?,source_url=? WHERE market=? AND symbol=? AND asset_class='stock'", (name,url,market,sym))
                        if "ST" in name.upper() or "退" in name:
                            c.execute("UPDATE instruments SET risk_flag=? WHERE market=? AND symbol=?", ("st" if "ST" in name.upper() else "warning",market,sym))
                # 仅出现在上市证券清单中，不能证明公告风险或停牌状态已经核验。
            result["sources"].append({"name": action, "status": "success", "count": len(raw)})
            log(f"{action} 已同步 {len(raw)} 条记录")
        except Exception as exc:
            result["errors"].append({"source": action, "error": str(exc)[:300]})
            log(f"{action} 不可用：{str(exc)[:250]}；保留缓存并标记缺失")
    result["status"]="success" if not result["errors"] else "partial" if result["sources"] else "unavailable"
    return result


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
    result=_sync_reference_data(("calendar","master_sh","master_sz","etf_names"),log)
    result["instruments"]=[]
    items = [instruments.get(r["id"]) for r in db.query("SELECT id FROM instruments WHERE watched=1")]
    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(sync_instrument, item) for item in items]
        for future in as_completed(futures):
            item = future.result()
            result["instruments"].append(item)
            log(f"{item['id']} {item['status']}"+("："+item["message"] if item.get("message") else ""))
    result["metals"] = sync_metals(log)
    succeeded = sum(x["status"] == "success" for x in result["instruments"]) + sum(x.get("status") == "success" for x in result["metals"])
    result["status"] = "success" if succeeded and not result["errors"] and all(x["status"] != "unavailable" for x in result["instruments"]) else "partial" if succeeded else "unavailable"
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
    return results


# 采集指定证券的公告，并记录来源及抓取时间。
def announcements(ident: str):
    item = instruments.get(ident)
    if item["asset_class"] != "stock" or item["market"] not in {"SH", "SZ"}:
        raise ValueError("此公告适配器仅支持境内股票")
    raw = call("announcements", {"symbol": item["symbol"], "start": (now()-timedelta(days=30)).strftime("%Y%m%d"), "end": now().strftime("%Y%m%d")}, timeout=30)
    out=[]
    with db.connection() as c:
        for r in raw:
            title = str(r.get("公告标题", r.get("announcementTitle", "")))
            url = str(r.get("公告链接", r.get("adjunctUrl", "")))
            if url.startswith("/"):
                url = "https://static.cninfo.com.cn" + url
            if url.startswith("http://"):
                url = "https://" + url[7:]
            if not title or not instruments.official_url(url):
                continue
            if db.one("SELECT id FROM risk_events WHERE instrument_id=? AND source_url=?", (ident,url)):
                continue
            severity = "warning" if any(k in title for k in ("风险", "退市", "停牌", "立案", "处罚")) else "info"
            c.execute("INSERT INTO risk_events(instrument_id,event_type,severity,title,source_url,verified_at,payload_json) VALUES(?,?,?,?,?,?,?)", (ident,"announcement",severity,title,url,timestamp(),db.dumps(r)))
            if severity == "warning":
                c.execute("UPDATE instruments SET risk_flag='warning' WHERE id=?", (ident,))
            out.append({"title": title, "source_url": url, "severity": severity})
    return out
