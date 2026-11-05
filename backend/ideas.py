# 按固定规则评审想法：说清楚、有对手盘、分行情检验，并生成早报文本。
from __future__ import annotations

import re
from datetime import date, datetime, timedelta

COUNTERPARTIES = (
    "被动抛售或强平",
    "拥挤之后的回归",
    "流动性变薄造成的过度波动",
    "大事前后的情绪错杀",
)
SERIES = ("黄金", "白银", "观察指数", "新公告", "已过期大事")
OBSERVABLE = ("收盘", "开盘", "最高", "最低", "均线", "跌破", "上穿", "突破", "公告", "涨跌幅", "成交额", "前高", "前低")
VAGUE = ("感觉", "要涨", "要跌", "放量就进", "应该会", "可能要", "看看", "博一下")
PAYWALL = ("付费", "课程", "喊单", "会员", "私享", "买课")
_CRYPTO = re.compile(r"比特币|以太坊|以太币|(?<![A-Za-z])BTC(?![A-Za-z])|(?<![A-Za-z])ETH(?![A-Za-z])", re.I)

FEE = 0.001
SLIPPAGE = 0.0005
MIN_BARS = 400
HOLD_DAYS = 63
MIN_TRADES = 30
REGIME_WINDOW = 20
REGIME_MOVE = 0.08
TASK = "任务是评审想法池里已有的想法，并写明过关或死因；不下单、不给买卖数量、不编写自动交易、不保存行情或交易所密钥。"
DISCLAIMER = "历史表现，不代表未来"
ESTIMATE = "单边费用 0.1% 与滑点 0.05% 为估算"


# 判断文本是否点名比特币或以太坊。
def mentions_crypto(text: str) -> bool:
    return bool(_CRYPTO.search(text or ""))


# 把用户输入收成评审使用的字段，不读取其中的指令性文字。
def normalize(payload: dict, *, allow_incomplete: bool = False) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("想法不合法")
    sentence = str(payload.get("sentence") or "").strip()
    entry = str(payload.get("entry") or "").strip()
    exit_rule = str(payload.get("exit") or "").strip()
    wrong = str(payload.get("wrong") or "").strip()
    if not 1 <= len(sentence) <= 200:
        raise ValueError("想法需要一句话，且不超过200字")
    for name, text in (("进场", entry), ("离场", exit_rule), ("错在哪", wrong)):
        if len(text) > 200 or (not allow_incomplete and not text):
            raise ValueError(f"{name}需要写明，且不超过200字")
    counterparty = str(payload.get("counterparty") or "").strip()
    if len(counterparty) > 40:
        raise ValueError("对手盘说明过长")
    sensitive = str(payload.get("sensitive_to") or "").strip()
    if sensitive and sensitive not in SERIES:
        raise ValueError("对照项不在范围内")
    parameters = _parameters(payload.get("parameters") or [])
    risk = str(payload.get("max_risk") or "").strip()
    if len(risk) > 200:
        raise ValueError("最大风险不超过200字")
    idea = {
        "instrument_id": str(payload.get("instrument_id") or "").strip(),
        "sentence": sentence,
        "entry": entry,
        "exit": exit_rule,
        "wrong": wrong,
        "counterparty": counterparty,
        "parameters": parameters,
        "max_risk": risk or "未知",
        "sensitive_to": sensitive,
        "source_url": str(payload.get("source_url") or "").strip(),
        "source_time": str(payload.get("source_time") or "").strip(),
        "source_text": str(payload.get("source_text") or ""),
        "created_at": str(payload.get("created_at") or ""),
        "id": str(payload.get("id") or ""),
    }
    if not idea["instrument_id"]:
        raise ValueError("需要选择证券")
    if mentions_crypto(sentence + entry + exit_rule + wrong + idea["instrument_id"] + idea["source_text"]):
        raise ValueError("比特币和以太坊不在研究范围")
    return idea


# 只接受 2 到 250 的数值参数，最多 6 个，超限留给评审标成可疑。
def _parameters(value) -> list[int]:
    if not isinstance(value, list) or len(value) > 6:
        raise ValueError("参数不合法")
    numbers = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ValueError("参数须为数字")
        number = int(item)
        if number < 2 or number > 250:
            raise ValueError("参数须在 2 到 250 之间")
        numbers.append(number)
    return numbers


# 一句话是否包含可观察条件，而不是只有笼统判断。
def _clear(text: str) -> bool:
    if len(text.strip()) < 4:
        return False
    observable = any(word in text for word in OBSERVABLE) or any(ch.isdigit() for ch in text)
    vague = any(word in text for word in VAGUE)
    if vague and not any(word in text for word in OBSERVABLE):
        return False
    return observable


# 外部正文只保留为资料，不参与通过或淘汰。
def quarantine(text: str) -> str:
    return (text or "")[:2000]


# 由短周期和长周期确定均线，未填写时使用固定的 5 与 20。
def resolve_windows(parameters: list[int]) -> tuple[int, int]:
    if not parameters:
        return 5, 20
    if len(parameters) == 1:
        slow = parameters[0]
        return max(2, slow // 4), slow
    return parameters[0], parameters[1]


# 计算某一根 K 线所在的上涨、下跌或横盘段。
def regime_at(closes: list[float], index: int) -> str | None:
    if index < REGIME_WINDOW or closes[index - REGIME_WINDOW] <= 0:
        return None
    change = closes[index] / closes[index - REGIME_WINDOW] - 1
    if change >= REGIME_MOVE:
        return "up"
    if change <= -REGIME_MOVE:
        return "down"
    return "side"


# 用下一根开盘价、固定费用和滑点模拟往返，只在内部判断盈亏方向。
def simulate(bars: list[dict], fast: int, slow: int) -> list[dict]:
    closes = [float(b["close"]) for b in bars]
    opens = [float(b["open"]) for b in bars]
    trades = []
    position = None
    for i in range(slow, len(bars) - 1):
        fast_now = _mean(closes, i, fast)
        slow_now = _mean(closes, i, slow)
        fast_prev = _mean(closes, i - 1, fast)
        slow_prev = _mean(closes, i - 1, slow)
        if None in (fast_now, slow_now, fast_prev, slow_prev):
            continue
        if position is None and fast_prev <= slow_prev and fast_now > slow_now:
            fill = opens[i + 1] * (1 + SLIPPAGE)
            position = (fill * (1 + FEE), i + 1)
        elif position is not None and fast_prev >= slow_prev and fast_now < slow_now:
            fill = opens[i + 1] * (1 - SLIPPAGE)
            proceeds = fill * (1 - FEE)
            entry_cost, entry_i = position
            trades.append({"entry": entry_i, "win": proceeds > entry_cost})
            position = None
    return trades


# 读取包含当前点在内的均线，长度不足时返回空。
def _mean(values: list[float], end: int, window: int):
    if window < 2 or end < window - 1:
        return None
    return sum(values[end - window + 1:end + 1]) / window


# 按交易次数和三种行情的内部结果给出第三关结论。
def label_from_counts(trade_count: int, buckets: dict[str, list[bool]]) -> str:
    if trade_count < MIN_TRADES:
        return "样本太少"
    good = [name for name in ("up", "down", "side") if buckets.get(name) and sum(buckets[name]) > len(buckets[name]) / 2]
    if len(good) == 3:
        return "通过"
    if good:
        return "偏科"
    return "三种行情均未成立"


# 在给定区间上跑第三关，样本长度不足两年时直接记样本太少。
def history_label(bars: list[dict], parameters: list[int]) -> str:
    if len(parameters) > 3:
        return "可疑"
    fast, slow = resolve_windows(parameters)
    if fast >= slow or fast < 2 or slow > 250:
        return "可疑"
    if len(bars) < MIN_BARS:
        return "样本太少"
    if any(float(b["open"]) <= 0 or float(b["close"]) <= 0 for b in bars):
        return "样本太少"
    closes = [float(b["close"]) for b in bars]
    trades = simulate(bars, fast, slow)
    buckets = {"up": [], "down": [], "side": []}
    for trade in trades:
        name = regime_at(closes, trade["entry"])
        if name:
            buckets[name].append(trade["win"])
    return label_from_counts(len(trades), buckets)


# 留出段把通过和淘汰对调时，结论改为可疑。
def combine_holdout(train_label: str, full_label: str) -> str:
    if (train_label == "通过") != (full_label == "通过"):
        return "可疑"
    return full_label


# 每个数值按一成上下拨动，得到仍能比较的邻近参数。
def neighbors(parameters: list[int]) -> list[list[int]]:
    found = []
    for index, value in enumerate(parameters):
        step = max(1, round(abs(value) * 0.1))
        for delta in (-step, step):
            nxt = list(parameters)
            nxt[index] = max(2, int(round(value + delta)))
            if nxt != list(parameters) and resolve_windows(nxt)[0] < resolve_windows(nxt)[1]:
                found.append(nxt)
    return found


# 评审单条想法。来源正文只归档，不能改变关卡。
def review_one(idea: dict, bars: list[dict]) -> dict:
    quarantine(idea.get("source_text") or "")
    gates = 0
    if not (_clear(idea.get("entry") or "") and _clear(idea.get("exit") or "") and _clear(idea.get("wrong") or "")):
        reason = "说不清楚"
    else:
        gates = 1
        if idea.get("counterparty") not in COUNTERPARTIES:
            reason = "没有对手盘"
        else:
            gates = 2
            reason = _robust(idea.get("parameters") or [], bars)
            if reason == "通过":
                gates = 3
                reason = "三关都通过"
    survived = reason == "三关都通过"
    return _card(idea, gates, reason, survived, bars)


# 汇总参数数量、留出段和参数拨动后的第三关结论。
def _robust(parameters: list[int], bars: list[dict]) -> str:
    if len(parameters) > 3:
        return "可疑"
    fast, slow = resolve_windows(parameters)
    if fast >= slow:
        return "可疑"
    if len(bars) < MIN_BARS + HOLD_DAYS:
        return "样本太少"
    train = history_label(bars[:-HOLD_DAYS], parameters)
    full = history_label(bars, parameters)
    label = combine_holdout(train, full)
    if label != "通过" or not parameters:
        return label
    for candidate in neighbors(parameters):
        if history_label(bars, candidate) != "通过":
            return "可疑"
    return label


# 组装一张对外卡片，不放入胜率、收益或回撤。
def _card(idea: dict, gates: int, reason: str, survived: bool, bars: list[dict]) -> dict:
    sources = []
    if bars:
        last = bars[-1]
        sources.append({"source": last.get("source") or "未知", "url": last.get("source_url") or "", "fetched_at": last.get("fetched_at") or "未知"})
    if idea.get("source_url"):
        sources.append({"source": "公开资料", "url": idea["source_url"], "fetched_at": idea.get("source_time") or "未知"})
    if not sources:
        sources.append({"source": "未知", "url": "", "fetched_at": "未知"})
    return {
        "idea_id": idea.get("id") or "",
        "instrument_id": idea.get("instrument_id") or "",
        "sentence": idea.get("sentence") or "",
        "gates_passed": gates,
        "reason": reason,
        "survived": survived,
        "death_only": not survived,
        "max_risk": idea.get("max_risk") or "未知",
        "invalidation": idea.get("wrong") or "未知",
        "sources": sources,
        "sensitive_to": idea.get("sensitive_to") or "",
        "created_at": idea.get("created_at") or "",
        "parameter_count": len(idea.get("parameters") or []),
    }


# 通过全部关卡的想法最多留两条，其余只保留名额死因。
def assign_survivors(cards: list[dict]) -> list[dict]:
    passed = [card for card in cards if card["reason"] == "三关都通过"]
    passed.sort(key=lambda card: (card["parameter_count"], card["created_at"], card["idea_id"]))
    keep = {card["idea_id"] for card in passed[:2]}
    result = []
    for card in cards:
        if card["reason"] == "三关都通过" and card["idea_id"] not in keep:
            result.append({**card, "reason": "存活名额已满", "survived": False, "death_only": True})
        else:
            result.append(card)
    return result


# 选出相对前一快照变化最大的一项，并对应到想法。
def pick_comparison(today: dict, yesterday: dict, cards: list[dict]) -> dict:
    best = None
    unknown = []
    for name in SERIES:
        current = today.get(name) or {}
        previous = yesterday.get(name) or {}
        current_value = current.get("value")
        previous_value = previous.get("value")
        if current_value is None or previous_value is None or previous_value == 0:
            unknown.append(name)
            continue
        change = (current_value - previous_value) / abs(previous_value)
        if best is None or abs(change) > best[0]:
            best = (abs(change), name, change, current)
    if best is None:
        return {"status": "无法对照", "detail": "对照项缺少可复查的前值或当前值，记为未知。", "unknown": unknown}
    _magnitude, name, change, current = best
    direction = "上升" if change > 0 else "下降" if change < 0 else "持平"
    helps = [card["sentence"] for card in cards if card.get("sensitive_to") == name and card.get("survived")]
    hurts = [card["sentence"] for card in cards if card.get("sensitive_to") == name and not card.get("survived")]
    return {
        "status": "已对照",
        "series": name,
        "direction": direction,
        "change_note": f"{name}较前一快照{direction}，变化幅度为估算",
        "source": current.get("source") or "未知",
        "fetched_at": current.get("fetched_at") or "未知",
        "url": current.get("url") or "",
        "helps": helps or ["对现有想法无直接对照"],
        "hurts": hurts or ["对现有想法无直接对照"],
        "unknown": unknown,
    }


# 想法池为空时只复述任务，不启动三关。
def waiting_brief() -> dict:
    return {
        "waiting": True,
        "statement": TASK,
        "summary": [],
        "survivors": [],
        "deaths": [],
        "comparison": {"status": "无法对照", "detail": "想法池未填写，不做今日对照。"},
        "avoid": "今天最不该做的事：在想法池还是空的时候下结论。",
        "disclaimer": DISCLAIMER,
        "estimate_note": ESTIMATE,
        "weekly": None,
    }


# 评审整池想法，存活卡片保留全文，其余只留死因。
def review_pool(ideas: list[dict], bars_by_id: dict, today: dict, yesterday: dict) -> dict:
    if not ideas:
        return waiting_brief()
    cards = assign_survivors([review_one(idea, bars_by_id.get(idea["instrument_id"], [])) for idea in ideas])
    survivors = [card for card in cards if card["survived"]]
    deaths = [{"idea_id": card["idea_id"], "sentence": card["sentence"], "reason": card["reason"]} for card in cards if card["death_only"]]
    brief = {
        "waiting": False,
        "statement": TASK,
        "summary": [{"sentence": card["sentence"], "gates": card["gates_passed"], "conclusion": card["reason"]} for card in cards],
        "survivors": [{k: card[k] for k in ("idea_id", "instrument_id", "sentence", "gates_passed", "reason", "max_risk", "invalidation", "sources")} for card in survivors],
        "deaths": deaths,
        "comparison": pick_comparison(today, yesterday, cards),
        "avoid": "今天最不该做的事：在失效条件出现后仍沿用留下的想法。" if survivors else "今天最不该做的事：把已经淘汰的想法当成还能做的计划。",
        "disclaimer": DISCLAIMER,
        "estimate_note": ESTIMATE,
        "weekly": None,
    }
    return brief


# 公开资料不足三条时才补齐，付费内容和加密货币内容不进入。
def public_additions(notes: list[dict], existing: list[dict], today: date) -> list[dict]:
    if not existing or len(existing) >= 3:
        return []
    have_urls = {item.get("source_url") for item in existing}
    have_ids = {item.get("instrument_id") for item in existing}
    found = []
    for note in notes:
        if len(existing) + len(found) >= 3:
            break
        if not _acceptable_note(note, today) or note.get("url") in have_urls or note.get("instrument_id") in have_ids:
            continue
        if mentions_crypto((note.get("title") or "") + (note.get("instrument_id") or "")):
            continue
        found.append({
            "instrument_id": note["instrument_id"],
            "sentence": str(note.get("title") or "公开资料")[:200],
            "entry": "",
            "exit": "",
            "wrong": "",
            "counterparty": "",
            "parameters": [],
            "max_risk": "未知",
            "sensitive_to": "",
            "source_url": note["url"],
            "source_time": note["published_at"],
            "source_text": note.get("text") or "",
            "created_at": "",
            "id": "",
        })
    return found


# 公开补齐必须有链接和时间，并且落在过去七天。
def _acceptable_note(note: dict, today: date) -> bool:
    url = str(note.get("url") or "")
    if not url.startswith(("http://", "https://")):
        return False
    blob = (note.get("title") or "") + (note.get("text") or "")
    if any(word in blob for word in PAYWALL):
        return False
    try:
        published = datetime.fromisoformat(str(note.get("published_at"))).date()
    except ValueError:
        return False
    return today - timedelta(days=7) <= published <= today


# 按想法统计本周最新死因，并指出出现最多的一种。
def rank_deaths(entries: list[dict]) -> dict:
    latest = {}
    for entry in sorted(entries, key=lambda item: item.get("at") or ""):
        if entry.get("idea_id") and entry.get("reason"):
            latest[entry["idea_id"]] = entry["reason"]
    counts: dict[str, int] = {}
    for reason in latest.values():
        counts[reason] = counts.get(reason, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    if not ranked:
        return {"ranked": [], "most": "本周没有已记录的死因。"}
    reason, count = ranked[0]
    return {"ranked": [{"reason": name, "count": value} for name, value in ranked], "most": f"这周最常犯的错是{reason}，出现{count}次。"}


# 把评审收成早报里的四段文字。
def render_lines(review: dict) -> list[str]:
    lines = ["", "## 想法评审", review["statement"], ""]
    if review.get("waiting"):
        lines += ["想法池未填写，本日不评审。", review["avoid"], review["disclaimer"], review["estimate_note"]]
        return lines
    lines.append("## 总表")
    for row in review["summary"]:
        lines.append(f"- {row['sentence']} → {row['gates']}关 → {row['conclusion']}")
    lines += ["", "## 留下的想法"]
    if not review["survivors"]:
        lines.append("这次没有留下的想法。")
    for card in review["survivors"]:
        sources = "；".join(f"{item['source']} {item['fetched_at']}" for item in card["sources"])
        lines += [
            f"### {card['sentence']}",
            f"过了 {card['gates_passed']} 关。留下的原因：{card['reason']}",
            f"最大风险：{card['max_risk']}",
            f"失效条件：{card['invalidation']}",
            f"数据来源：{sources}",
        ]
    lines += ["", "## 今日对照"]
    comparison = review["comparison"]
    if comparison.get("status") != "已对照":
        lines.append(comparison.get("detail") or "无法对照")
    else:
        lines.append(f"{comparison['change_note']}。来源：{comparison['source']}，采集时间：{comparison['fetched_at']}。")
        lines.append("利好：" + "；".join(comparison["helps"]))
        lines.append("不利：" + "；".join(comparison["hurts"]))
    lines += ["", "## 今天最不该做的事", review["avoid"]]
    if review.get("weekly"):
        lines += ["", "## 本周死因", review["weekly"]["most"]]
        for item in review["weekly"]["ranked"]:
            lines.append(f"- {item['reason']}：{item['count']}次")
    lines += [review["disclaimer"], review["estimate_note"]]
    return lines
