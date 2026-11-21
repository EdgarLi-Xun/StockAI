"""Normalize dated official suspension and resumption evidence."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime, time, timedelta

from .config import TZ

SSE_STATUS_URL = "https://www.sse.com.cn/disclosure/dealinstruc/suspension/stock/"
CNINFO_NOTICE_URL = "https://www.cninfo.com.cn/new/disclosure"

NOTICE_KEYWORD = re.compile(r"(停复牌|停牌|复牌|暂停交易|恢复交易)")
_DATE_CN = r"(?P<day>\d{4}年\d{1,2}月\d{1,2}日)"
_DECLARATION = re.compile(
    r"(?P<subject>(?:本?公司(?:的)?(?:A股股票|股票|证券|基金|A股)(?:交易)?|本基金)"
    r"(?:停复牌(?:起始日|安排):)?|(?:并|,))"
    r"(?:将)?(?:自|于|从)?" + _DATE_CN +
    r"(?:\([^)]{1,12}\))?(?P<period>上午|下午)?"
    r"(?:(?P<clock>\d{1,2}:\d{2})|(?:开市|开盘)(?:时)?)?(?:起)?"
    r"(?P<verb>(?:开始|再次|连续|继续|临时)?停牌|复牌|暂停交易|恢复交易)"
    r"(?P<one_day>(?:一|1)(?:个交易)?天)?"
)
_SAME_DAY_NOTICE = re.compile(r"(?:关于)?(?:本公司|公司)?(?:股票|证券|A股|基金)(?:当日|今日)临时停牌(?:的)?(?:公告|通知)")


def parse_local(value):
    try:
        parsed = datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None
    return parsed.astimezone(TZ) if parsed.tzinfo else parsed.replace(tzinfo=TZ)


def _at(day, clock=time(9, 30)):
    return datetime.combine(day, clock, TZ).isoformat(timespec="seconds")


def document_events(symbol, body):
    body = re.sub(r"\s+", "", unicodedata.normalize("NFKC", body))
    if not re.search(r"(?:证券|股票|基金)代码[:：]?" + re.escape(symbol) + r"(?![0-9])", body):
        return []
    events = {}
    for sentence in re.split(r"[。；;\n]", body):
        matched_subject = False
        for match in _DECLARATION.finditer(sentence):
            if not match.group("subject").startswith(("公司", "本公司", "本基金")) and not matched_subject:
                continue
            try:
                day = datetime.strptime(match.group("day"), "%Y年%m月%d日").date()
                pm = match.group("period") == "下午"
                clock = time.fromisoformat(match.group("clock").zfill(5)) if match.group("clock") else time(13) if pm else time(9, 30)
                if pm and clock.hour < 12:
                    clock = clock.replace(hour=clock.hour + 12)
            except ValueError:
                continue
            matched_subject = True
            status = "active" if match.group("verb") in {"复牌", "恢复交易"} else "suspended"
            effective = _at(day, clock)
            event = {"status": status, "effective_at": effective}
            if status == "suspended" and (match.group("one_day") or "临时" in match.group("verb")):
                event["expires_at"] = _at(day + timedelta(days=1), time.min)
            events[(effective, status)] = event
    return list(events.values())


def notice_events(row, symbol, title, url, at):
    if str(row.get("代码") or row.get("证券代码") or row.get("secCode") or "").zfill(6) != symbol:
        return []
    published = parse_local(row.get("公告时间") or row.get("公告日期"))
    if not published or published > at or published < at - timedelta(days=31):
        return []
    source = row.get("附件链接") or url
    parsed = row.get("trading_events") or []
    if not parsed and _SAME_DAY_NOTICE.fullmatch(re.sub(r"\s+", "", title)) and published.time() < time(15):
        parsed = [{"status": "suspended", "effective_at": max(_at(published.date()), published.isoformat(timespec="seconds")),
                   "expires_at": _at(published.date() + timedelta(days=1), time.min)}]
    return [{**event, "notice_key": url, "url": source, "title": title, "published_at": published.isoformat(timespec="seconds")}
            for event in parsed]


def sse_events(row, next_session):
    start = row.get("startStopDate")
    end = row.get("endStopDate")
    code = row.get("productCode")
    mode = row.get("type")
    if not re.fullmatch(r"\d{6}", code or "") or mode not in {"LXTP", "LSTP"} or row.get("controlType") != "TR":
        return []
    try:
        first = date.fromisoformat(start)
        last = date.fromisoformat(end) if end else None
    except (TypeError, ValueError):
        return []
    if last and last < first:
        return []
    base = {"notice_key": f"sse:{code}:{start}:{mode}", "url": SSE_STATUS_URL, "source": "sse",
            "title": "上海证券交易所停复牌记录"}
    start_clock = time(13) if row.get("stopTime") == "PM" else time(9, 30)
    halt = {**base, "status": "suspended", "effective_at": _at(first, start_clock)}
    events = [halt]
    reopen = None
    if mode == "LSTP":
        same_day = {"930": time(10, 30), "AM": time(13)}.get(row.get("stopTime"))
        if same_day:
            reopen = _at(first, same_day)
        elif row.get("stopTime") == "WH":
            after = next_session((last or first).isoformat())
            reopen = _at(date.fromisoformat(after)) if after else None
        halt["expires_at"] = reopen or _at((last or first) + timedelta(days=1), time.min)
    elif last:
        after = next_session(last.isoformat())
        reopen = _at(date.fromisoformat(after)) if after else None
        halt["expires_at"] = reopen or _at(last + timedelta(days=1), time.min)
    reason = row.get("endStopReason") or ""
    if reopen and not any(word in reason for word in ("摘牌", "终止上市", "退市")):
        events.append({**base, "status": "active", "effective_at": reopen})
    return events


def selected_event(evidence, at):
    point = at.astimezone(TZ).isoformat(timespec="seconds")
    reviewed = evidence.get("manual_trading_status_at", "")
    due = [row for row in evidence.get("trading_status_events", []) if row["effective_at"] <= point
           and row.get("published_at", "") <= point and max(row["effective_at"], row.get("published_at", "")) > reviewed]
    return max(due, key=lambda row: (row["effective_at"], row["status"] == "suspended", row.get("source") == "sse", row.get("published_at", "")), default=None)


def current_status(status, evidence, at):
    if status != "delisted" and (event := selected_event(evidence, at)):
        expires = event.get("expires_at")
        if expires and expires <= at.astimezone(TZ).isoformat(timespec="seconds"):
            return "unknown", event
        return event["status"], event
    return status, None


def apply_status_view(item, at):
    item["status"], event = current_status(item["status"], item["evidence"], at)
    if event:
        item["evidence"]["exchange_trading_status"] = {**event, "status": item["status"]}


def scheduled_halt(item, at):
    point = at.astimezone(TZ).isoformat(timespec="seconds")
    return any(row["status"] == "suspended" and row.get("published_at", "") <= point
               and point < row["effective_at"] and row["effective_at"][:10] == point[:10]
               for row in item.get("evidence", {}).get("trading_status_events", []))
