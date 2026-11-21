# 先通过规则构建研究事实，再合并经校验的 AI 解释并生成可追溯报告。
from __future__ import annotations

import hashlib
import html
import json
import os
import uuid
from datetime import datetime

from . import ai, analytics, backtest, collectors, db, instruments
from .budget import budget_check
from .config import DATA, TZ, VERSION, STRATEGY_VERSION, now, timestamp
from .report_facts import METAL_NAMES, attach_metals, closing_followup, rank_candidates


# 汇总行情、风险、预算和技术指标，筛选最多三只合格模拟候选。
def build_facts(mode="standard") -> dict:
    cfg = db.settings()
    context = collectors.calendar_context()
    candidates, excluded, sources = [], [], []
    histories, latest_bars = {}, {}
    for row in db.query("SELECT id FROM instruments WHERE watched=1 AND market IN ('SH','SZ') AND asset_class IN ('stock','etf') ORDER BY id"):
        item = instruments.get(row["id"])
        bars = collectors.bars_for(item["id"])
        last = bars[-1] if bars else None
        latest_bars[item["id"]] = last
        reasons = instruments.risk_reasons(item) + collectors.fresh_reasons(last,context)
        stats = analytics.indicators(bars)
        reasons += stats["missing"]
        estimate = None
        if last:
            source_id = "s" + str(len(sources)+1)
            sources.append({"id":source_id,"instrument_id":item["id"],"url":last["source_url"],"source":last["source"],"quote_time":last["ts"],"collected_at":last["fetched_at"],"adjustment":last["adjustment"]})
            try:
                estimate = budget_check(last["close"], item["rules"], item["asset_class"], cfg.fees, cfg.budget)
                if not estimate["within_budget"]:
                    reasons.append("最低合法数量及预计买入费用超过单只预算")
            except ValueError as exc:
                reasons.append(str(exc))
            if last.get("amount") is None or last["amount"] < cfg.min_daily_amount:
                reasons.append("成交额不足或流动性字段缺失")
        points = analytics.score(bars, stats["latest"]) if bars else 0
        if points < 60:
            reasons.append("趋势与成交量评分未达到60分（非上涨概率）")
        # 任一证据、行情、流动性或预算障碍都进入排除清单，不靠模型绕过规则。
        if reasons:
            excluded.append({"instrument_id":item["id"],"name":item["name"],"reasons":list(dict.fromkeys(reasons)),"price":last["close"] if last else None})
            continue
        candidate = {"instrument_id":item["id"],"symbol":item["symbol"],"name":item["name"],"asset_class":item["asset_class"],"price":last["close"],"quote_time":last["ts"],"source_url":last["source_url"],"source_ids":[source_id],"quantity":estimate["max_quantity"],"budget":estimate,"score":points,"indicators":stats["latest"],"status":"verified","rules":item["rules"],"evidence":item["evidence"],"verified_at":item["verified_at"],"rationale":"趋势、成交量与预算规则同时通过；评分仅表示规则匹配程度。","counterargument":"历史技术形态不代表未来表现，费用和跳空会改变可执行性。","invalidation":"风险核验过期、出现风险公告、行情过期或成交总成本超过预算时失效。"}
        if mode == "full":
            candidate["multi_timeframe"] = {tf:analytics.indicators(collectors.bars_for(item["id"],tf))["latest"] for tf in ("15m","60m")}
            candidate["weekly"] = analytics.indicators(resample_weekly(bars))["latest"]
        candidates.append(candidate)
        histories[item["id"]] = bars[-21:]
    financial_rows = db.query("SELECT * FROM research_context WHERE category='financials'")
    rank_candidates(candidates, histories, collectors.bars_for, financial_rows, sources, context, cfg.fees)
    for c in candidates[3:]:
        excluded.append({"instrument_id":c["instrument_id"],"name":c["name"],"reasons":["本次排名未进入最多3只的研究名额"]})
    facts={"mode":mode,"report_date":context["date"],"generated_at":timestamp(),"calendar":context,"budget_limit":cfg.budget,"candidate_limit":3,"candidates":candidates[:3],"excluded":excluded,"sources":sources,"fees":cfg.fees.model_dump(),"strategy_version":STRATEGY_VERSION,"app_version":VERSION,"missing_data":["宏观和基本面资料仅在有归档证据时解读。","历史名称不等于最新交易状态。"]}
    placeholders = ",".join("?" for _ in METAL_NAMES)
    quotes = [json.loads(row["payload_json"]) for row in db.query(f"SELECT payload_json FROM market_quotes WHERE instrument_id IN ({placeholders})", tuple(METAL_NAMES))]
    attach_metals(facts, quotes, now())
    auxiliary=[]
    for row in db.query("SELECT id,name,market,asset_class FROM instruments WHERE watched=1 AND (asset_class='index' OR market IN ('US','HK','FUND'))"):
        bars=collectors.bars_for(row["id"],limit=1)
        observed=db.one("SELECT payload_json FROM market_quotes WHERE instrument_id=?",(row["id"],))
        quote=bars[-1] if bars else json.loads(observed["payload_json"]) if observed else None
        if quote:
            q={**row,"price":quote.get("close",quote.get("price")),"quote_time":quote.get("ts",quote.get("quote_time")),"source_url":quote["source_url"],"source":quote["source"],"fetched_at":quote["fetched_at"],"observation_only":True}
            auxiliary.append(q)
            sources.append({"id":"s"+str(len(sources)+1),"instrument_id":row["id"],"url":q["source_url"],"source":q["source"],"quote_time":q["quote_time"],"collected_at":q["fetched_at"]})
        else:
            auxiliary.append({**row,"price":None,"status":"unavailable"})
    facts["auxiliary_markets"]=auxiliary
    if not context["calendar_verified"]:
        facts["missing_data"].append("交易日历未覆盖报告日期")
    if mode=="closing":
        morning = db.one("SELECT id,generated_at,payload_json FROM reports WHERE mode='standard' AND report_date=? ORDER BY generated_at LIMIT 1", (context["date"],))
        signals = db.query("""SELECT r.id price_rule_id,r.instrument_id,e.result,e.reason,o.status order_status
            FROM price_rule_events e JOIN price_rules r ON r.id=e.price_rule_id
            LEFT JOIN orders o ON o.id=e.order_id WHERE e.bar_date=? ORDER BY r.instrument_id,r.id""", (context["date"],))
        facts["closing_followup"] = closing_followup(facts, morning, latest_bars, collectors.bars_for, signals)
    if mode=="full":
        facts["backtests"]=[backtest.report_summary(r) for r in db.query("SELECT * FROM backtest_runs ORDER BY created_at DESC LIMIT 5")]
        attach_full_evidence(facts,db.query("SELECT * FROM research_context"),db.query("SELECT * FROM risk_events WHERE event_type='announcement' ORDER BY verified_at DESC,id DESC LIMIT 40"))
        facts["simulation"]={"accounts":db.query("SELECT id,name,initial_cash,cash FROM accounts"),"recent_fills":db.query("SELECT instrument_id,side,quantity,fill_price,fees,filled_at FROM orders WHERE status='filled' ORDER BY filled_at DESC LIMIT 30")}
    from .idea_flow import attach_local
    attach_local(facts)
    return facts


def attach_full_evidence(facts,context_rows,announcement_rows):
    from .context import BLS_CPI_URL,model_data
    focused={c["instrument_id"] for c in facts["candidates"]+facts.get("observations",[])}
    sources=facts["sources"]
    context=[]
    for row in sorted(context_rows,key=lambda r:(r["instrument_id"] is not None,r["instrument_id"] not in focused,r["instrument_id"] or "",r["category"],r["id"])):
        data=model_data(row["category"],json.loads(row["payload_json"]))
        if not any(data):
            continue
        sid="s"+str(len(sources)+1)
        verification="官方月度统计，未经季节调整" if row["source_url"]==BLS_CPI_URL else "第三方资料，需结合原始来源核对"
        context.append({**{k:row[k] for k in ("id","category","instrument_id","source","source_url","as_of","fetched_at")},"data":data,"source_ids":[sid],"verification":verification})
        sources.append({"id":sid,"instrument_id":row["instrument_id"] or row["id"],"url":row["source_url"],"source":row["source"],"quote_time":row["as_of"],"collected_at":row["fetched_at"]})
    facts["deep_context"]=context
    announced=[]
    for row in announcement_rows:
        if not instruments.official_url(row["source_url"]):
            continue
        payload=json.loads(row.get("payload_json") or "{}")
        published=payload.get("公告时间") or payload.get("公告日期")
        try:
            published=datetime.fromisoformat(published)
            published=(published if published.tzinfo else published.replace(tzinfo=TZ)).isoformat()
        except (ValueError,TypeError):
            published=None
        sid="s"+str(len(sources)+1)
        announced.append({**{k:row[k] for k in ("instrument_id","title","source_url","verified_at","severity")},"published_at":published,"source_ids":[sid]})
        sources.append({"id":sid,"instrument_id":row["instrument_id"],"url":row["source_url"],"source":"官方公告归档","quote_time":published,"collected_at":row["verified_at"]})
    facts["announcements"]=announced
    categories={r["category"] for r in context}
    for category,description in (("macro","宏观指标"),("fundamentals","行业和证券概况"),("financials","财务摘要")):
        if category not in categories:facts["missing_data"].append(f"缺少已归档的{description}。")
    if not announced:facts["missing_data"].append("缺少可引用的官方公告归档。")
    if not facts["backtests"]:facts["missing_data"].append("未运行历史回测，策略表现未验证。")
    if not any(any(c.get("multi_timeframe",{}).values()) for c in facts["candidates"]+facts.get("observations",[])):
        facts["missing_data"].append("缺少可用的分钟线指标。")


# 按交易周聚合日线的开高低收与成交量，供研究图表使用。
def resample_weekly(bars):
    groups={}
    for b in bars:
        key=datetime.fromisoformat(b["ts"]).isocalendar()[:2]
        if key not in groups:
            groups[key]=dict(b)
        else:
            g=groups[key]
            g.update(ts=b["ts"],high=max(g["high"],b["high"]),low=min(g["low"],b["low"]),close=b["close"],volume=g["volume"]+b["volume"])
    return list(groups.values())


# 在归档前复核候选数量、来源、报价时间及单只含费预算。
def validate_facts(facts):
    if len(facts["candidates"])>3:
        raise ValueError("候选数量超过上限")
    for c in facts["candidates"]:
        item = instruments.get(c["instrument_id"])
        if instruments.risk_reasons(item):
            raise ValueError("候选在发布前风险复核失败")
        check=budget_check(c["price"],item["rules"],item["asset_class"],db.settings().fees,db.settings().budget,c["quantity"])
        if not check["within_budget"] or c["budget"]["total"] != check["total"]:
            raise ValueError("候选在发布前预算复核失败")
        if not c["source_url"] or not c["quote_time"]:
            raise ValueError("候选缺少行情证据")


# 将结构化事实和解释渲染为包含来源及数据缺口的中文报告。
def markdown_report(facts):
    name={"standard":"标准版早报","full":"完整版深度复盘","closing":"收盘跟踪"}[facts["mode"]]
    lines=[f"# StockAI {name}",f"研究日期：{facts['report_date']} · 生成时间：{facts['generated_at']} · Asia/Shanghai",f"模型：{facts['model']} · 策略：{facts['strategy_version']} · 数据快照：{facts['snapshot_id']}","",f"## {facts['summary']}",facts["market_context"]]
    if facts.get("scheduled_for") and facts["scheduled_for"]!=facts["report_date"]:
        lines.insert(2,f"原定周度复盘日期：{facts['scheduled_for']} · 恢复后补跑")
    lines += ["", "## 黄金白银行情"]
    metal_quotes = {q["id"]: q for q in facts.get("metals", [])}
    for ident, name in METAL_NAMES.items():
        if ident not in metal_quotes:
            lines.append(f"- {name}：未取得带来源和报价时间的行情")
            continue
        q = metal_quotes[ident]
        quality = "缓存报价" if q.get("stale") else "来源报价"
        lines.append(f"- {name} · {q['price']} {q['unit']} ({q['currency']}) · {quality} · 报价 {q['quote_time']} · 采集 {q['fetched_at']} · {q.get('delay_type') or '延迟未声明'} · {q['source_url']}")
    ratio = facts.get("gold_silver_ratio")
    if ratio:
        cached = "（缓存报价）" if ratio["stale"] else ""
        lines.append(f"金银比 {ratio['value']:.2f}{cached} · 伦敦现货同口径 · 金 {ratio['gold_quote_time']} · 银 {ratio['silver_quote_time']}")
    else:
        lines.append("金银比暂无可比报价，需同币种、同单位且相隔不超过 1 分钟的伦敦金银现货。")
    followup = facts.get("closing_followup")
    if followup:
        lines += ["", "## 早报与收盘对照", followup["message"]]
        if morning := followup.get("morning_report"):
            lines.append(f"早报 {morning['report_id']} · 生成 {morning['generated_at']} · 候选 {followup['morning_candidate_count']} 只")
        for row in followup["rows"]:
            lines.append(f"### {row['name']} {row['symbol']} · {row['status_label']}")
            for label, q in (("早报参考价", row["morning"]), ("当日收盘价", row["closing"])):
                if q:
                    lines.append(f"- {label} {q['price']} 元 · {q['quote_time']} · {q['source_url']}")
                elif label == "当日收盘价":
                    lines.append("- 未取得可核验的当日收盘价")
            if row["change_pct"] is not None:
                lines.append(f"- 较早报参考价 {row['change_pct']:+.2f}%")
            lines.extend("- " + reason for reason in row["reasons"])
        lines += ["", "### 当日模拟信号"]
        for e in followup["signals"]:
            status = (" · 委托 " + e["order_status"]) if e["order_status"] else ""
            lines.append(f"- {e['instrument_id']} · {e['result_label']}{status} · {e['reason']}")
        if not followup["signals"]:
            lines.append("未留有当日模拟信号记录。")
    lines += ["", "## 模拟研究候选"]
    if not facts["candidates"]:
        lines.append("今日无合格模拟候选。")
    for c in facts["candidates"]:
        lines += [f"### {c['name']} {c['symbol']}",f"收盘价 {c['price']} 元；报价 {c['quote_time']}；模拟数量 {c['quantity']}；预计费用 {c['budget']['fees']['total']} 元；总成本 {c['budget']['total']} 元；评分 {c['score']}（非概率）。",f"研究理由：{c['rationale']}",f"反方观点：{c['counterargument']}",f"失效条件：{c['invalidation']}",f"来源：{c['source_url']}"]
        if score := c.get("score_details"):
            points = score["components"]
            lines.append(f"- 排序分 {score['version']} · 技术与成交量 {points['technical']}/65 · 相对强弱 {points['relative_strength']}/10 · 公告与基本面 {points['risk']}/10 · 成本 {points['cost']}/10 · 风险收益情景 {points['reward_risk']}/5")
            relative = score["relative_strength"]
            lines.append(f"- 相对 {relative['instrument_id']} 20 期超额 {relative['excess_pct']:+.2f} 个百分点 · {relative['start_date']} 至 {relative['end_date']}" if relative else "- 同期基准行情缺失，未计算相对强弱。")
            financials = score["risk"]["financials"]
            if financials:
                lines.append(f"- 财务报告期 {financials['period']} · 归档净利润 {'为负' if financials['profit'] < 0 else '非负'} · [{financials['source_id']}] {financials['source_url']}")
            elif score["risk"]["financial_status"] == "unavailable":
                lines.append("- 缺少可用财务摘要，基本面部分未加分。")
            scenario = score["scenario"]
            fee_basis = "假设费率" if score["fee_assumption"] else "已核对费率"
            lines.append(f"- 估算往返成本 {score['round_trip_cost_pct']:.4f}%（{fee_basis}，含滑点） · 20 日区间情景 {scenario['support']}–{scenario['resistance']} 元 · 含费上行 {scenario['net_upside_pct']:+.2f}% / 下行 {scenario['net_downside_pct']:.2f}%")
    if facts["mode"]=="full":
        lines += ["", "## 深度研究证据"]
        for item in facts.get("deep_context",[]):
            lines.append(f"- {item['source']} · {item.get('instrument_id') or '宏观'} · 资料期 {item['as_of']} · {item['source_url']}")
        for backtest in facts.get("backtests",[]):
            metrics=backtest.get("summary",backtest.get("result",{}))
            if metrics.get("return_pct") is not None:
                proof="历史证据完整" if backtest.get("validated") else "未验证实验"
                lines.append(f"- 回测 {backtest.get('instrument_id') or '归档证券'} · {proof} · 收益 {metrics['return_pct']}% · 最大回撤 {metrics.get('max_drawdown_pct','—')}%")
    lines += ["", "## 风险与数据缺口", *["- "+s for s in facts["risk_notes"]+facts["missing_data"]], "", "## 未入选记录"]
    for c in facts["excluded"]:
        lines.append(f"- {c['name']} {c['instrument_id']}：{'；'.join(c['reasons'])}")
    lines += ["", "## 原始证据"]
    for s in facts["sources"]:
        lines.append(f"- [{s['id']}] {s['source']} | 报价 {s['quote_time']} | 采集 {s['collected_at']} | {s['url']}")
    from .ideas import render_lines
    if facts.get("idea_review"):
        lines += render_lines(facts["idea_review"])
    lines += ["", "本系统仅供信息研究与模拟交易，不接入真实账户，不保证收益。"]
    return "\n\n".join(lines)


# 先写同目录临时文件再替换，避免报告只写入一部分。
def atomic_write(path, content):
    temp=path.with_suffix(path.suffix+".tmp")
    with temp.open("w",encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    temp.replace(path)


# 按逻辑键幂等生成报告，保存事实、候选与 Markdown、HTML 档案。
def generate(logical_key, mode="standard", job_id=None, log=lambda m:None):
    previous=db.one("SELECT id,payload_json FROM reports WHERE logical_key=?",(logical_key,))
    if previous:
        return {"report_id":previous["id"],"reused":True}
    facts=build_facts(mode)
    planned=logical_key.rsplit(":",1)[-1]
    if len(planned)==10 and planned!=facts["report_date"]:
        facts["scheduled_for"]=planned
        facts["missing_data"].append("此报告为恢复后补跑，不能还原过去行情；研究日期、报价时间和采集时间均为实际记录。")
    digest=hashlib.sha256(db.dumps(facts).encode()).hexdigest()
    facts["snapshot_id"]=digest
    log(f"全池校验完成，合格候选 {len(facts['candidates'])} 只")
    text,error=ai.explain(facts,mode)
    if text and text["candidates"]:
        by_id={c["instrument_id"]:c for c in text["candidates"]}
        for c in facts["candidates"]:
            c.update({k:by_id[c["instrument_id"]][k] for k in ("rationale","counterargument","invalidation")})
    facts["summary"] = "今日无合格模拟候选" if not facts["candidates"] else f"{len(facts['candidates'])}只证券通过规则筛选"
    facts["market_context"] = text["market_context"] if text else "使用已持久保存、带来源和报价时间的事实。日线为最近已完成交易日收盘价；未知风险和缺失数据按不合格处理。"
    facts["risk_notes"] = (text["risk_notes"] if text else []) + [db.settings().fees.note,"主观评分不等于上涨概率；实际模拟成交前需要重新核验。"]
    if text:
        facts["missing_data"] += text["missing_data"]
    facts["ai_status"]="generated" if text else "fallback"
    facts["ai_error"]=error
    facts["model"]=db.settings().model if text else "rules-only"
    facts["model_parameters"]={"temperature":0.1,"num_ctx":16384} if text else {}
    validate_facts(facts)
    ident=uuid.uuid4().hex
    output=DATA/"reports"
    output.mkdir(parents=True,exist_ok=True)
    md=markdown_report(facts)
    document="<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>StockAI 研究报告</title><style>body{max-width:960px;margin:48px auto;padding:0 24px;font:16px/1.8 system-ui;color:#182228}pre{white-space:pre-wrap;overflow-wrap:anywhere}</style><body><pre>"+html.escape(md)+"</pre></body></html>"
    atomic_write(output/(ident+".md"),md)
    atomic_write(output/(ident+".html"),document)
    with db.connection() as c:
        c.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?)",(ident,logical_key,job_id,mode,facts["report_date"],timestamp(),db.dumps(facts),ident+".md",ident+".html"))
        for item in facts["candidates"]:
            c.execute("INSERT INTO candidates VALUES(?,?,?)",(ident,item["instrument_id"],db.dumps(item)))
    db.notify(f"{'完整版复盘' if mode=='full' else '研究报告'}已生成，候选{len(facts['candidates'])}只")
    return {"report_id":ident,"candidate_count":len(facts["candidates"]),"ai_status":facts["ai_status"]}
