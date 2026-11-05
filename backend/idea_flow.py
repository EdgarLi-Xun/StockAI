# 把想法池、日线和非加密货币上下文接成早报与周复盘能用的评审。
from __future__ import annotations

import json
import uuid
from datetime import timedelta

from . import collectors, db, ideas
from .config import now, timestamp


# 读取本地想法池，按写入时间稳定排序。
def list_ideas() -> list[dict]:
    return [_row(row) for row in db.query("SELECT * FROM ideas ORDER BY created_at,id")]


# 校验证券与字段后保存一条本地想法。
def create_idea(payload: dict) -> dict:
    from . import instruments
    idea = ideas.normalize(payload)
    instruments.get(idea["instrument_id"])
    idea["id"] = uuid.uuid4().hex
    idea["created_at"] = timestamp()
    _insert(idea)
    return idea


# 删除一条本地想法。
def delete_idea(ident: str) -> dict:
    if not db.one("SELECT id FROM ideas WHERE id=?", (ident,)):
        raise ValueError("想法不存在")
    db.execute("DELETE FROM ideas WHERE id=?", (ident,))
    return {"deleted": True, "id": ident}


# 为本地早报附上想法评审；池中已有一条或两条时，用七日公开资料补到三条。
def attach_local(facts: dict) -> dict:
    pool = list_ideas()
    for addition in ideas.public_additions(_notes(db.query), pool, now().date()):
        addition["id"] = uuid.uuid4().hex
        addition["created_at"] = timestamp()
        _insert(addition)
    pool = list_ideas()
    today = _series_local()
    brief = ideas.review_pool(pool, _bars(pool, lambda ident: collectors.bars_for(ident, "1d", 800)), today, _previous_local())
    _save_local(today)
    if facts.get("mode") == "full":
        brief["weekly"] = ideas.rank_deaths(_stored_deaths(db.query, "") + _current_deaths(brief))
    facts["idea_review"] = brief
    return facts


# 为云端用户附上同一套评审，想法存在个人研究对象里。
def attach_cloud(store, uid: str, facts: dict) -> dict:
    from .cloud import market, resources
    pool = _cloud_ideas(store, uid)
    for addition in ideas.public_additions(_notes(store.query), pool, now().date()):
        addition["id"] = uuid.uuid4().hex
        addition["created_at"] = timestamp()
        resources.save(store, uid, "idea", addition, ident=addition["id"])
    pool = _cloud_ideas(store, uid)
    today = _series_cloud(store)
    brief = ideas.review_pool(pool, _bars(pool, lambda ident: market.bars_for(store, ident, "1d", 800)), today, _previous_cloud(store, uid))
    _save_cloud(store, uid, today)
    if facts.get("mode") == "full":
        brief["weekly"] = ideas.rank_deaths(_stored_deaths(lambda sql, args=(): store.query(sql, args), uid) + _current_deaths(brief))
    facts["idea_review"] = brief
    return facts


# 列出云端有效想法，并摊平存储载荷。
def cloud_list(store, uid: str) -> list[dict]:
    return _cloud_ideas(store, uid)


# 校验后写入云端想法池。
def cloud_create(store, uid: str, payload: dict, client_key: str | None = None) -> dict:
    from .cloud import market, resources
    idea = ideas.normalize(payload)
    market.instrument(store, idea["instrument_id"])
    idea["created_at"] = timestamp()
    saved = resources.save(store, uid, "idea", idea, client_key=client_key)
    return {"id": saved["id"], "version": saved["version"], **saved["data"]}


# 把数据库行还原成评审字段。
def _row(row: dict) -> dict:
    return {
        "id": row["id"], "instrument_id": row["instrument_id"], "sentence": row["sentence"], "entry": row["entry"],
        "exit": row["exit_rule"], "wrong": row["wrong"], "counterparty": row["counterparty"],
        "parameters": json.loads(row["parameters_json"]), "max_risk": row["max_risk"], "sensitive_to": row["sensitive_to"],
        "source_url": row["source_url"], "source_time": row["source_time"], "source_text": row["source_text"],
        "created_at": row["created_at"],
    }


# 插入一条已经规范化的想法。
def _insert(idea: dict):
    db.execute(
        "INSERT INTO ideas VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (idea["id"], idea["instrument_id"], idea["sentence"], idea["entry"], idea["exit"], idea["wrong"], idea["counterparty"],
         db.dumps(idea["parameters"]), idea["max_risk"], idea["sensitive_to"], idea["source_url"], idea["source_time"], idea.get("source_text") or "", idea["created_at"]),
    )


# 读取过去七天带来源和时间的公告。
def _notes(query) -> list[dict]:
    since = (now() - timedelta(days=7)).isoformat()
    rows = query("SELECT instrument_id,title,source_url,verified_at FROM risk_events WHERE event_type='announcement' AND verified_at>=?", (since,))
    return [{"instrument_id": row["instrument_id"], "title": row["title"], "url": row["source_url"], "published_at": row["verified_at"], "text": row["title"]} for row in rows]


# 读取云端想法并忽略已删除版本。
def _cloud_ideas(store, uid: str) -> list[dict]:
    rows = store.query("SELECT id,payload_json,created_at FROM research_objects WHERE user_id=? AND kind='idea' AND status='active' ORDER BY created_at,id", (uid,))
    ideas_out = []
    for row in rows:
        data = json.loads(row["payload_json"])
        data["id"] = row["id"]
        data.setdefault("created_at", row["created_at"])
        ideas_out.append(data)
    return ideas_out


# 按证券读取日线，同一证券只查一次。
def _bars(pool: list[dict], loader) -> dict:
    return {item["instrument_id"]: loader(item["instrument_id"]) for item in pool}


# 组装本地对照序列：黄金、白银、上证、新公告和昨日已归档事件。
def _series_local() -> dict:
    def quote(ident):
        row = db.one("SELECT payload_json FROM market_quotes WHERE instrument_id=?", (ident,))
        return json.loads(row["payload_json"]) if row else None
    index = collectors.bars_for("SH:000001", "1d", 1)
    today = now().date().isoformat()
    yesterday = (now().date() - timedelta(days=1)).isoformat()
    return _pack(quote("FX:XAUUSD"), quote("FX:XAGUSD"), index[-1] if index else None,
                 db.one("SELECT COUNT(*) n FROM risk_events WHERE event_type='announcement' AND substr(verified_at,1,10)=?", (today,))["n"],
                 db.one("SELECT COUNT(*) n FROM risk_events WHERE substr(verified_at,1,10)=?", (yesterday,))["n"])


# 用云端共享行情组装同一组对照序列。
def _series_cloud(store) -> dict:
    from .cloud import market
    quotes = {item["id"]: item for item in market.quotes(store)}
    index = market.bars_for(store, "SH:000001", "1d", 1)
    today = now().date().isoformat()
    yesterday = (now().date() - timedelta(days=1)).isoformat()
    return _pack(quotes.get("FX:XAUUSD"), quotes.get("FX:XAGUSD"), index[-1] if index else None,
                 store.one("SELECT COUNT(*) n FROM risk_events WHERE event_type='announcement' AND substr(verified_at,1,10)=?", (today,))["n"],
                 store.one("SELECT COUNT(*) n FROM risk_events WHERE substr(verified_at,1,10)=?", (yesterday,))["n"])


# 把报价、指数和事件计数收成带值和来源的对照项。
def _pack(gold, silver, index, new_count, expired_count) -> dict:
    fetched = timestamp()

    def price(item, value_key="price"):
        if not item:
            return {"value": None}
        value = item.get(value_key, item.get("close"))
        return {"value": value, "source": item.get("source") or "未知", "url": item.get("source_url") or "", "fetched_at": item.get("fetched_at") or item.get("quote_time") or "未知"}

    return {
        "黄金": price(gold),
        "白银": price(silver),
        "观察指数": price(index, "close"),
        "新公告": {"value": new_count, "source": "已归档公告", "url": "", "fetched_at": fetched},
        "已过期大事": {"value": expired_count, "source": "已归档事件", "url": "", "fetched_at": fetched},
    }


# 读取今天之前最近一份本地对照快照。
def _previous_local() -> dict:
    row = db.one("SELECT payload_json FROM context_snapshots WHERE day<? ORDER BY day DESC LIMIT 1", (now().date().isoformat(),))
    return json.loads(row["payload_json"]) if row else {}


# 保存今天的本地对照快照，同一天重复运行时覆盖。
def _save_local(series: dict):
    db.execute(
        "INSERT INTO context_snapshots VALUES(?,?,?) ON CONFLICT(day) DO UPDATE SET payload_json=excluded.payload_json,fetched_at=excluded.fetched_at",
        (now().date().isoformat(), db.dumps(series), timestamp()),
    )


# 读取该用户今天之前最近一份对照快照。
def _previous_cloud(store, uid: str) -> dict:
    row = store.one("SELECT payload_json FROM research_objects WHERE user_id=? AND kind='context_snapshot' ORDER BY created_at DESC LIMIT 1", (uid,))
    if not row:
        return {}
    days = json.loads(row["payload_json"]).get("days") or {}
    previous = [day for day in days if day < now().date().isoformat()]
    return days[max(previous)] if previous else {}


# 把今天的对照序列并入用户快照，保留最近十四天。
def _save_cloud(store, uid: str, series: dict):
    from .cloud import resources
    from .cloud.auth import AuthError
    ident = "context-snapshot:" + uid
    row = store.one("SELECT id,version,payload_json FROM research_objects WHERE id=? AND user_id=? AND kind='context_snapshot'", (ident, uid))
    days = json.loads(row["payload_json"]).get("days") if row else {}
    days = days or {}
    days[now().date().isoformat()] = series
    keep = sorted(days)[-14:]
    payload = {"days": {day: days[day] for day in keep}}
    try:
        resources.save(store, uid, "context_snapshot", payload, ident=ident, version=row["version"] if row else None)
    except AuthError:
        return


# 从已归档报告收集本周死因。
def _stored_deaths(query, uid: str) -> list[dict]:
    start = (now().date() - timedelta(days=now().weekday())).isoformat()
    if uid:
        rows = query("SELECT generated_at,payload_json FROM personal_reports WHERE user_id=? AND report_date>=? AND mode IN ('standard','full')", (uid, start))
    else:
        rows = query("SELECT generated_at,payload_json FROM reports WHERE report_date>=? AND mode IN ('standard','full')", (start,))
    entries = []
    for row in rows:
        review = json.loads(row["payload_json"]).get("idea_review") or {}
        for death in review.get("deaths") or []:
            entries.append({"idea_id": death.get("idea_id"), "reason": death.get("reason"), "at": row["generated_at"]})
    return entries


# 把本次评审里的死因加入周排行。
def _current_deaths(brief: dict) -> list[dict]:
    stamp = timestamp()
    return [{"idea_id": item["idea_id"], "reason": item["reason"], "at": stamp} for item in brief.get("deaths") or []]
