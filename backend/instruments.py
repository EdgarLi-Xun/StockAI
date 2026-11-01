from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db
from .config import now, timestamp


class Rules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lot_size: int = Field(100, ge=1, le=100000)
    quantity_step: int = Field(100, ge=1, le=100000)
    price_tick: str = "0.01"
    settlement_days: int = Field(1, ge=0, le=1)
    limit_pct: float = Field(0.1, gt=0, le=0.5)
    verified: bool = False
    source_url: str = ""

    @field_validator("price_tick")
    @classmethod
    def tick_valid(cls, v):
        from decimal import Decimal
        d = Decimal(v)
        if not d.is_finite() or d <= 0 or d > 1:
            raise ValueError("价格步长不合法")
        return str(d)


class InstrumentInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    symbol: str = Field(min_length=1, max_length=24)
    market: str = "SZ"
    name: str = Field("待核验", min_length=1, max_length=80)
    asset_class: str = "stock"
    group_name: str = Field("我的自选", max_length=40)

    @field_validator("symbol")
    @classmethod
    def symbol_valid(cls, value):
        if not re.fullmatch(r"[A-Za-z0-9._/-]+", value):
            raise ValueError("证券代码格式错误")
        return value.upper()

    @field_validator("market")
    @classmethod
    def market_valid(cls, value):
        if value not in {"SH", "SZ", "HK", "US", "FX", "SHFE", "FUND"}:
            raise ValueError("不支持的市场")
        return value

    @field_validator("asset_class")
    @classmethod
    def asset_valid(cls, value):
        if value not in {"stock", "etf", "index", "fund", "metal", "future"}:
            raise ValueError("不支持的资产类别")
        return value


def official_url(url: str) -> bool:
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    return p.scheme == "https" and not p.username and any(host == h or host.endswith("." + h) for h in ("sse.com.cn", "szse.cn", "cninfo.com.cn", "sge.com.cn", "shfe.com.cn"))


def add(item: InstrumentInput):
    ident = f"{item.market}:{item.symbol}"
    if item.market in {"SH", "SZ", "FUND"} and not re.fullmatch(r"\d{6}", item.symbol):
        raise ValueError("境内证券代码须为六位数字")
    rules = Rules(price_tick="0.001" if item.asset_class == "etf" else "0.01")
    db.execute("""INSERT INTO instruments(id,symbol,market,name,asset_class,group_name,rules_json)
        VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET watched=1,group_name=excluded.group_name""",
        (ident, item.symbol, item.market, item.name, item.asset_class, item.group_name, rules.model_dump_json()))
    return get(ident)


def get(ident: str) -> dict:
    row = db.one("SELECT * FROM instruments WHERE id=?", (ident,))
    if not row:
        raise ValueError("证券不存在")
    row["rules"] = json.loads(row.pop("rules_json"))
    row["evidence"] = json.loads(row.pop("evidence_json"))
    return row


def risk_reasons(item: dict, at: datetime | None = None) -> list[str]:
    at = at or now()
    errors = []
    if item["market"] not in {"SH", "SZ"} or item["asset_class"] not in {"stock", "etf"}:
        errors.append("仅供观察，不支持普通模拟买入")
    if item["status"] != "active":
        errors.append({"unknown": "交易状态未核验", "suspended": "停牌", "delisted": "已退市"}.get(item["status"], "交易状态不允许买入"))
    if item["risk_flag"] != "clear" or "ST" in item["name"].upper() or "退" in item["name"]:
        errors.append("风险警示或公告尚未核验")
    if not item["verified_at"]:
        errors.append("缺少风险核验时间")
    else:
        verified = datetime.fromisoformat(item["verified_at"])
        if verified.tzinfo is None or verified > at or at - verified > timedelta(hours=db.settings().max_risk_age_hours):
            errors.append("风险核验已过期或时间无效")
    evidence = item.get("evidence", {})
    if not all(official_url(evidence.get(k, "")) for k in ("listing_url", "status_url", "announcement_url")):
        errors.append("缺少上市、停复牌或公告的官方核验证据")
    if not item["rules"].get("verified") or not official_url(item["rules"].get("source_url", "")):
        errors.append("产品交易规则未核验")
    return list(dict.fromkeys(errors))


def verify(ident: str, payload: dict):
    get(ident)
    if payload.get("status") not in {"active", "suspended", "delisted", "unknown"}:
        raise ValueError("交易状态不合法")
    if payload.get("risk_flag") not in {"clear", "st", "warning", "unknown"}:
        raise ValueError("风险标记不合法")
    evidence = payload.get("evidence", {})
    if not all(official_url(evidence.get(k, "")) for k in ("listing_url", "status_url", "announcement_url")):
        raise ValueError("请填写三项交易所或巨潮官方 HTTPS 证据链接")
    if not evidence.get("review_note", "").strip():
        raise ValueError("请填写已人工核对的公告与停复牌结论")
    rules = Rules.model_validate(payload["rules"])
    if not rules.verified or not official_url(rules.source_url):
        raise ValueError("需要核验后的产品交易规则及官方来源")
    name = str(payload.get("name", "")).strip()[:80]
    if not name:
        raise ValueError("需要最新官方名称")
    ts = timestamp()
    with db.connection() as c:
        c.execute("UPDATE instruments SET name=?,status=?,risk_flag=?,verified_at=?,source_url=?,rules_json=?,evidence_json=? WHERE id=?", (name, payload["status"], payload["risk_flag"], ts, evidence["listing_url"], rules.model_dump_json(), db.dumps(evidence), ident))
        c.execute("INSERT INTO risk_events(instrument_id,event_type,severity,title,source_url,verified_at,payload_json) VALUES(?,?,?,?,?,?,?)", (ident, "manual_official_review", "info" if payload["risk_flag"] == "clear" else "warning", evidence["review_note"][:500], evidence["announcement_url"], ts, db.dumps(payload)))
    return get(ident)


def seed():
    if db.one("SELECT id FROM instruments LIMIT 1"):
        return
    groups = {
        "原有自选": "603126:中材节能 601111:中国国航 002571:德力股份 000875:吉电股份 600115:中国东航 000911:南宁糖业 600121:郑州煤电 601939:建设银行 600804:鹏博士 600900:长江电力 600540:新赛股份 000425:徐工机械 601328:交通银行 000903:云内动力",
        "贵金属关联": "601899:紫金矿业 600547:山东黄金 600489:中金黄金 000603:盛达资源 002716:湖南白银 000426:兴业银锡",
        "其他观察": "601601:中国太保 600519:贵州茅台 600031:三一重工 002284:亚太股份 000702:正虹科技 002173:创新医疗",
        "场内ETF": "159330:ETF名称待核验 159337:ETF名称待核验 159692:ETF名称待核验 159992:ETF名称待核验 159599:ETF名称待核验",
    }
    for group, text in groups.items():
        for pair in text.split():
            sym, name = pair.split(":")
            add(InstrumentInput(symbol=sym, market="SH" if sym.startswith("6") else "SZ", name=name, group_name=group, asset_class="etf" if group == "场内ETF" else "stock"))
    extras = [("SH", "000001", "上证指数", "index", "市场指数"), ("SZ", "399001", "深证成指", "index", "市场指数"), ("SZ", "399006", "创业板指", "index", "市场指数"), ("HK", "00700", "腾讯控股（历史名称）", "stock", "海外观察"), ("US", "AAPL", "苹果", "stock", "海外观察"), ("FX", "XAUUSD", "伦敦金现货", "metal", "黄金白银"), ("FX", "XAGUSD", "伦敦银现货", "metal", "黄金白银"), ("SHFE", "AU0", "沪金主力连续", "future", "黄金白银"), ("SHFE", "AG0", "沪银主力连续", "future", "黄金白银")]
    extras += [("FUND", code, "场外基金名称待核验", "fund", "场外观察") for code in ("009047", "003591", "013965")]
    for market, sym, name, asset, group in extras:
        add(InstrumentInput(symbol=sym, market=market, name=name, asset_class=asset, group_name=group))
