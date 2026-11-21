"""Verify the report and job behavior exposed by the shared desktop/Web backend."""
import json
import math
import statistics
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from backend import collectors, config, db, report_facts, research
from backend.cloud import jobs, mail, market, models, simulation, workspace
from backend.cloud.preferences import Preferences, save_preferences
from backend.cloud.store import json_text, stamp
from backend.instruments import InstrumentInput
from backend.public_app import create_app
from conftest import sample_bars
from test_cloud import GOOD, cloud, login, profile, run_job


def test_report_archives_metals_and_checks_ratio_comparability(cloud):
    store, _, _, _, _, uid = cloud
    at = config.now() - timedelta(minutes=5)
    prices = {"FX:XAUUSD": 2400, "FX:XAGUSD": 30, "SHFE:AU0": 566}
    for offset, (ident, price) in enumerate(prices.items()):
        spot = ident.startswith("FX")
        q = dict(id=ident, price=price, quote_time=(at - timedelta(minutes=offset)).isoformat(),
                 fetched_at=at.isoformat(), source="隔离验收行情", source_url="https://example.test/metals",
                 currency="USD" if spot else "CNY", unit="美元/金衡盎司" if spot else "元/克",
                 kind="spot" if spot else "continuous_future", status="success")
        store.execute("INSERT INTO market_quotes VALUES(?,?)", (ident, json_text(q)))
    result = run_job(cloud, ai=False)
    row = workspace.owned(store, "personal_reports", uid, result["report_id"])
    facts = json.loads(row["payload_json"])
    assert {q["id"]: q["price"] for q in facts["metals"]} == prices
    ratio = facts["gold_silver_ratio"]
    assert ratio["value"] == 80 and ratio["gold_quote_time"] != ratio["silver_quote_time"]
    assert set(ratio["source_ids"]) <= {source["id"] for source in facts["sources"]}
    invalid = [{**q, "quote_time": (at - timedelta(seconds=61)).isoformat()} if q["id"] == "FX:XAGUSD" else q
               for q in facts["metals"]]
    assert report_facts.gold_silver_ratio(invalid, at) is None
    assert report_facts.gold_silver_ratio(facts["metals"][1:], at) is None
    with TestClient(create_app(store.cfg)) as client:
        login(client)
        base = "/api/me/reports/" + row["id"]
        assert client.get(base).json()["gold_silver_ratio"]["value"] == 80
        assert client.get("/api/me/dashboard").json()["gold_silver_ratio"]["value"] == 80
        for fmt in ("md", "html"):
            body = client.get(base + "/export", params={"format": fmt}).text
            assert all(v in body for v in ("金银比 80.00", "2400.0", "30.0", "566.0", "沪银主力连续", "未取得"))


def test_sge_and_gold_etf_are_separate_observations_with_source_dates(monkeypatch):
    at = datetime(2025, 1, 3, 16, tzinfo=config.TZ)
    monkeypatch.setattr(config, "now", lambda: at)
    monkeypatch.setattr(collectors, "now", lambda: at)
    db.execute("INSERT INTO calendar VALUES(?,?,?)", (at.date().isoformat(), "验收日历", stamp()))
    etf_bar = sample_bars(2)[-1]
    collectors.save_bars("SH:518880", [etf_bar])

    def feed(action, *_args, **_options):
        if action == "sge":
            return [{"date": "2025-01-03T00:00:00", "close": 621.46},
                    {"date": "2025-01-06T00:00:00", "close": 9999}]
        return []

    monkeypatch.setattr(collectors, "call", feed)
    assert len([q for q in collectors.sync_metals() if q["status"] == "success"]) == 2
    facts = research.build_facts()
    quotes = {q["id"]: q for q in facts["metals"]}
    gold, etf = quotes["SGE:AU99.99"], quotes["SH:518880"]
    assert (gold["price"], gold["quote_time"], gold["unit"]) == (621.46, "2025-01-03", "元/克")
    assert gold["source_url"].startswith("https://www.sge.com.cn/")
    assert (etf["price"], etf["quote_time"], etf["unit"]) == (etf_bar["close"], etf_bar["ts"], "元/份")
    assert facts["gold_silver_ratio"] is None and not facts["candidates"]


def test_full_report_archives_completed_gold_silver_returns_and_exact_session_pairs(cloud, monkeypatch):
    store, _, _, _, _, uid = cloud
    days = sample_bars(22)
    at = datetime.fromisoformat(days[-1]["ts"]) + timedelta(hours=1)
    for module in (collectors, config, jobs, market, workspace):
        monkeypatch.setattr(module, "now", lambda: at)
    monkeypatch.setattr(db, "DATA", store.cfg.data)
    gold_returns = [0.01 if i % 4 == 0 else -0.005 + i / 10000 for i in range(1, 22)]
    gold, silver, stock = 500.0, 6000.0, 10.0
    daily = {"Au99.99": [], "Ag(T+D)": []}
    equity = []
    for i, bar in enumerate(days):
        day = bar["ts"][:10]
        store.execute("INSERT INTO calendar VALUES(?,?,?)", (day, "交易日历验收样本", stamp()))
        if i:
            gold *= math.exp(gold_returns[i - 1])
            silver *= math.exp(-gold_returns[i - 1])
            stock *= math.exp(2 * gold_returns[i - 1])
        daily["Au99.99"].append({"date": day, "close": gold})
        daily["Ag(T+D)"].append({"date": day, "close": silver})
        equity.append({**bar, "close": stock, "open": stock, "high": stock, "low": stock})
    collectors.save_bars("SH:601899", [row for i, row in enumerate(equity) if i != 8])
    for rows in daily.values():
        rows.append({"date": (at + timedelta(days=1)).date().isoformat(), "close": 1e6})
    monkeypatch.setattr(collectors, "call", lambda action, args, **kw: daily[args["symbol"]])
    assert all(collectors.sync_sge_history(ident)["status"] == "success" for ident in collectors.SGE_HISTORY)
    assert len(store.query("SELECT id FROM research_context WHERE category='metal_closes'")) == 2
    ordinary = workspace.facts(store, uid, "standard")
    assert "metal_study" not in ordinary

    result = run_job(cloud, kind="full", ai=False)
    report = workspace.report_detail(store, uid, result["report_id"])["payload"]
    study = report["metal_study"]
    gold_study, silver_study = study["series"]
    expected = round(statistics.stdev(gold_returns[-20:]) * math.sqrt(252) * 100, 4)
    assert study["as_of"] == at.date().isoformat()
    assert gold_study["unit"] == "元/克" and silver_study["unit"] == "元/千克"
    assert gold_study["sample_count"] == silver_study["sample_count"] == 20
    assert gold_study["annualized_volatility_pct"] == silver_study["annualized_volatility_pct"] == expected
    for series, coefficient in ((gold_study, 1.0), (silver_study, -1.0)):
        by_code = {r["instrument_id"]: r for r in series["linked"]}
        assert by_code["SH:601899"]["same_session"]["coefficient"] == coefficient
        assert by_code["SH:601899"]["same_session"]["sample_count"] == 18
        assert by_code["SH:518880"]["same_session"]["coefficient"] is None
        available = {sid for r in series["linked"] for sid in r["source_ids"]}
        assert available <= {source["id"] for source in report["sources"]}
    expected_lag = round(statistics.correlation(
        [gold_returns[i - 2] for i in range(2, 22) if i not in {8, 9}],
        [gold_returns[i - 1] for i in range(2, 22) if i not in {8, 9}]), 4)
    assert gold_study["linked"][0]["next_session"]["coefficient"] == expected_lag
    projected = models.project_input(report, {}, bounded=True)
    assert projected["metal_study"]["series"][1]["linked"][0]["same_session"]["coefficient"] == -1.0
    assert set(silver_study["linked"][0]["source_ids"]) <= {s["id"] for s in projected["sources"]}
    with TestClient(create_app(store.cfg)) as client:
        login(client)
        base = "/api/me/reports/" + result["report_id"]
        assert client.get(base).json()["metal_study"] == study
        for fmt in ("md", "html"):
            body = client.get(base + "/export", params={"format": fmt}).text
            assert all(term in body for term in ("金银波动率与矿业股联动", f"{expected:.4f}%", "Ag(T+D)", "金银领先一交易日"))
    store.execute("DELETE FROM research_context WHERE category='metal_closes'")
    empty = workspace.facts(store, uid, "full")
    assert all(r["annualized_volatility_pct"] is None for r in empty["metal_study"]["series"])
    assert workspace.report_detail(store, uid, result["report_id"])["payload"]["metal_study"] == study


def test_exchange_suspension_is_visible_in_watch_risk_and_report(cloud, monkeypatch):
    store, _, _, _, _, uid = cloud
    at = datetime(2025, 1, 3, 10, tzinfo=config.TZ)
    for module in (collectors, config, jobs, market, workspace):
        monkeypatch.setattr(module, "now", lambda: at)
    monkeypatch.setattr(db, "DATA", store.cfg.data)
    ident = "SH:601899"
    market.add_watch(store, uid, InstrumentInput(symbol="601899", market="SH", name="验收证券", asset_class="stock"))
    for day in ("2025-01-03", "2025-01-06"):
        store.execute("INSERT INTO calendar VALUES(?,?,?)", (day, "交易日历样本", stamp()))
    raw = {"productCode":"601899","type":"LXTP","controlType":"TR","stopTime":"","startStopDate":"20250103",
           "endStopDate":"20250103","stopReason":"重要公告","endStopReason":"重要公告"}
    collectors._store_sse_status([raw])
    item = market.instrument(store, ident)
    assert item["status"] == "suspended" and item["risk_flag"] == "unknown"
    result = run_job(cloud, ai=False)
    report = workspace.report_detail(store, uid, result["report_id"])["payload"]
    excluded = next(row for row in report["excluded"] if row["instrument_id"] == ident)
    assert "停牌" in excluded["reasons"]
    evidence = excluded["trading_status_evidence"]
    assert any(s["id"] == evidence["source_id"] and s["url"] == evidence["url"] for s in report["sources"])
    at = datetime(2025, 1, 6, 9, 29, tzinfo=config.TZ)
    assert market.instrument(store, ident)["status"] == "suspended"
    at += timedelta(minutes=1)
    resumed = market.instrument(store, ident)
    assert resumed["status"] == "active" and resumed["risk_flag"] == "unknown"
    assert market.risk_reasons(resumed, Preferences(), at)


def test_recovered_morning_uses_previous_close_and_closing_tracks_original(cloud, monkeypatch):
    store, keys, _, _, _, uid = cloud
    dates = [(datetime.fromisoformat(b["ts"]), b) for b in sample_bars(75)]
    trade_day = dates[-1][0] + timedelta(days=1)
    while trade_day.weekday() >= 5:
        trade_day += timedelta(days=1)
    at = trade_day + timedelta(hours=1)
    for module in (jobs, market, workspace, config, collectors, simulation):
        monkeypatch.setattr(module, "now", lambda: at)
    with store.tx(immediate=True) as c:
        c.executemany("INSERT INTO calendar VALUES(?,?,?)",
                      [(d.date().isoformat(), "验收日历", stamp()) for d, _ in dates] +
                      [(trade_day.date().isoformat(), "验收日历", stamp())])
    official = "https://www.sse.com.cn/test"
    symbols = ("603126", "601111")
    for offset, code in enumerate(symbols):
        ident = market.add_watch(store, uid, InstrumentInput(symbol=code, market="SH", name="验收证券"))["id"]
        item = market.instrument(store, ident)
        market.verify(store, ident, {"name": code, "status": "active", "risk_flag": "clear",
                      "rules": {**item["rules"], "verified": True, "source_url": official},
                      "evidence": {"listing_url": official, "status_url": official, "announcement_url": official,
                                   "review_note": "隔离验收"}})
        prices = [round(2 + i * (0.08 - 0.01 * offset), 2) for i in range(len(dates))] + [10.0 if offset == 0 else 7.30]
        bars = [{**b, "open": p, "close": p, "high": p + 0.10, "low": p - 0.10}
                for (_, b), p in zip([*dates, (trade_day, {**dates[-1][1], "ts": trade_day.isoformat()})], prices)]
        market.import_bars(store, ident, {"bars": bars})
    index = [{**b, "open": 3000 + i, "close": 3000 + i, "high": 3001 + i, "low": 2999 + i}
             for i, (_, b) in enumerate(dates)]
    market.import_bars(store, "SH:000001", {"bars": index})
    store.execute("INSERT INTO research_context VALUES(?,?,?,?,?,?,?,?)",
                  ("test-financials", "financials", "SH:603126", json_text([{"报告期": "2025-03-31", "净利润": "-0.50亿"}]),
                   "验收财务摘要", official, "2025-03-31", stamp()))
    at = at.replace(hour=10)
    account = simulation.create_account(store, uid, "隔离验收", 2000)
    rule = simulation.create_rule(store, uid, dict(account_id=account["id"], instrument_id="SH:601111",
                                  kind="close_at_or_below", trigger_price="7.40", limit_price="7.35", quantity=100))
    at = at.replace(hour=16)
    morning = run_job(cloud, automatic=True, ai=False)
    assert morning["candidate_count"] == 2
    original = workspace.owned(store, "personal_reports", uid, morning["report_id"])
    base = json.loads(original["payload_json"])
    selected = next(c for c in base["candidates"] if c["symbol"] == "603126")
    assert selected["quote_time"] == dates[-1][0].isoformat()
    detail = selected["score_details"]
    assert detail["relative_strength"]["instrument_id"] == "SH:000001"
    assert detail["risk"]["financials"]["profit"] == -50_000_000
    assert detail["round_trip_cost_pct"] > 0 and detail["scenario"]["net_downside_pct"] > 0
    assert detail["total"] == selected["score"] == round(sum(detail["components"].values()), 2)
    closing = run_job(cloud, kind="closing", ai=False)
    assert closing["candidate_count"] == 1
    row = workspace.owned(store, "personal_reports", uid, closing["report_id"])
    report = json.loads(row["payload_json"])
    followup = report["closing_followup"]
    assert followup["morning_report"]["report_id"] == original["id"]
    removed, retained = [next(c for c in followup["rows"] if c["symbol"] == symbol) for symbol in symbols]
    assert removed["status"] == "removed" and removed["closing"]["price"] == 10
    assert any("预算" in reason for reason in removed["reasons"])
    assert retained["status"] == "retained" and retained["closing"]["price"] == 7.30
    assert any(signal["price_rule_id"] == rule["id"] and signal["result"] == "created"
               and signal["order_status"] == "pending" for signal in followup["signals"])
    assert workspace.owned(store, "personal_reports", uid, original["id"])["payload_json"] == original["payload_json"]
    assert jobs.enqueue(store, uid, "closing")["id"] == row["job_id"]
    assert "收盘未入选" in Path(store.cfg.data / row["markdown_path"]).read_text(encoding="utf-8")


def test_cross_day_gap_is_persisted_once_for_previously_enabled_plans(cloud, monkeypatch):
    store, keys, _, _, admin, uid = cloud
    store.cfg = replace(store.cfg, desktop_mode=True)
    at = datetime(2025, 11, 3, 7, 59, tzinfo=config.TZ)
    for module in (jobs, market, workspace, config, collectors, simulation):
        monkeypatch.setattr(module, "now", lambda: at)
    day = at.date()
    with store.tx(immediate=True) as c:
        for offset in (0, 2):
            c.execute("INSERT INTO calendar VALUES(?,?,?)", ((day + timedelta(days=offset)).isoformat(), "验收日历", stamp()))
    save_preferences(store, uid, Preferences(morning_enabled=True, closing_enabled=True).model_dump())
    assert jobs.schedule(store) == 0
    at += timedelta(days=2)
    jobs.recover(store)
    assert jobs.schedule(store) == jobs.schedule(store) == 0
    missed = jobs.list_jobs(store, uid)["jobs"]
    assert {(row["target_date"], row["kind"], row["status"]) for row in missed} == {
        (day.isoformat(), "standard", "missed"),
        (day.isoformat(), "closing", "missed"),
        ((day + timedelta(days=1)).isoformat(), "standard", "missed"),
    }
    assert not jobs.list_jobs(store, admin)["jobs"]
    assert len(store.query("SELECT id FROM personal_notifications WHERE user_id=?", (uid,))) == 1
    assert not store.query("SELECT id FROM personal_reports")
    assert all(not row["can_retry"] and row["result"]["scheduled_time"] for row in missed)
    at = at.replace(hour=16, minute=1)
    assert jobs.schedule(store) == 1
    assert jobs.run(store, keys, jobs.claim(store, "recovered-morning"))["report_id"]
    assert jobs.schedule(store) == 1
    assert jobs.run(store, keys, jobs.claim(store, "recovered-close"))["report_id"]
    assert jobs.schedule(store) == 0


def test_task_failure_delivers_only_to_opted_in_verified_user(cloud, monkeypatch):
    store, keys, _, _, admin, uid = cloud
    store.cfg = replace(store.cfg, smtp_host="loopback-fixture", smtp_from="stockai@example.test")
    save_preferences(store, uid, Preferences(task_failure_email_enabled=True).model_dump())

    def unavailable(*args, **kwargs):
        raise RuntimeError("private fixture detail")

    monkeypatch.setattr(workspace, "facts", unavailable)
    jobs.enqueue(store, uid, ai=False)
    queued = jobs.claim(store, "failed-report")
    assert jobs.run(store, keys, queued)["error_code"] == "REPORT_GENERATION_FAILED"
    assert jobs.run(store, keys, queued)["error_code"] == "REPORT_GENERATION_FAILED"
    assert run_job(cloud, uid=admin, ai=False)["error_code"] == "REPORT_GENERATION_FAILED"
    notice = store.query("SELECT * FROM personal_notifications WHERE user_id=?", (uid,))
    outbox = store.query("SELECT id,state FROM mail_outbox")
    assert len(notice) == len(outbox) == 1 and "private fixture" not in notice[0]["message"]
    assert notice[0]["level"] == "error" and outbox[0]["state"] == "queued"
    sent = []
    assert mail.send_one(store, keys, sent.append)
    assert not mail.send_one(store, keys, sent.append)
    assert len(sent) == 1 and sent[0]["To"] == "user@example.test"
    assert "标准版早报" in sent[0].get_content() and "private fixture" not in sent[0].get_content()
    assert jobs.list_jobs(store, uid)["jobs"][0]["failure_mail_state"] == "sent"
    next_job = jobs.enqueue(store, uid, kind="full", ai=False)
    assert jobs.run(store, keys, jobs.claim(store, "opted-out"))["error_code"] == "REPORT_GENERATION_FAILED"
    save_preferences(store, uid, Preferences().model_dump())
    assert mail.send_one(store, keys, sent.append) and len(sent) == 1
    assert store.one("SELECT state FROM mail_outbox WHERE id=?", ("task-failure:" + next_job["id"],))["state"] == "cancelled"


def test_automatic_report_backoff_keeps_id_and_survives_recovery(cloud, monkeypatch):
    store, keys, _, _, admin, uid = cloud
    at = config.now().replace(hour=8, minute=1, second=0, microsecond=0)
    monkeypatch.setattr(jobs, "now", lambda: at)
    save_preferences(store, uid, Preferences(morning_enabled=True).model_dump())
    facts = workspace.facts
    failures = []

    def intermittent(*args, **kwargs):
        if not failures:
            failures.append(True)
            raise TimeoutError("临时数据不可达")
        return facts(*args, **kwargs)

    monkeypatch.setattr(workspace, "facts", intermittent)
    assert jobs.schedule(store, at) == 1
    first = jobs.claim(store, "attempt-1")
    pending = jobs.enqueue(store, uid, "full", ai=False)
    retry_at = jobs.run(store, keys, first)["next_attempt_at"]
    assert jobs.claim(store, "before-due") is None
    other = jobs.enqueue(store, admin, "standard", ai=False)
    assert jobs.claim(store, "other-user")["id"] == other["id"]
    assert jobs.schedule(store, at) == 0
    info = next(row for row in jobs.list_jobs(store, uid)["jobs"] if row["id"] == first["id"])
    assert info["next_attempt_at"] == retry_at and info["run_attempts"] == 1 and info["attempts"] == 0
    assert not store.query("SELECT id FROM personal_reports WHERE user_id=?", (uid,))
    jobs.recover(store)
    assert jobs.claim(store, "still-before-due") is not None  # 另一用户的中断任务可继续
    assert jobs.claim(store, "backoff-persists") is None
    at += timedelta(seconds=jobs.AUTOMATIC_RETRY_DELAYS[0])
    second = jobs.claim(store, "attempt-2")
    assert second["id"] == first["id"] and second["run_attempts"] == 2
    assert jobs.run(store, keys, second)["report_id"]
    assert jobs.schedule(store, at) == 0
    assert jobs.claim(store, "pending")["id"] == pending["id"]
    assert len(store.query("SELECT id FROM personal_reports WHERE user_id=?", (uid,))) == 1
    assert any("30 秒后自动重试" in row["message"] for row in jobs.list_jobs(store, uid)["logs"])


def test_automatic_report_backoff_exhausts_once_and_alerts(cloud, monkeypatch):
    store, keys, _, _, _, uid = cloud
    at = config.now().replace(hour=8, minute=0, second=0, microsecond=0)
    monkeypatch.setattr(jobs, "now", lambda: at)
    store.cfg = replace(store.cfg, smtp_host="loopback-fixture", smtp_from="stockai@example.test")
    save_preferences(store, uid, Preferences(task_failure_email_enabled=True).model_dump())

    def offline(*_args, **_kwargs):
        raise OSError("private transport detail")

    monkeypatch.setattr(workspace, "facts", offline)
    ident = jobs.enqueue(store, uid, automatic=True, ai=False)["id"]
    for attempt, delay in enumerate(jobs.AUTOMATIC_RETRY_DELAYS, 1):
        current = jobs.claim(store, f"attempt-{attempt}")
        assert current["id"] == ident
        assert jobs.run(store, keys, current)["next_attempt_at"]
        assert jobs.claim(store, "early") is None
        assert not store.query("SELECT * FROM mail_outbox")
        at += timedelta(seconds=delay)
    final = jobs.claim(store, "final")
    assert final["run_attempts"] == 3
    assert jobs.run(store, keys, final)["error_code"] == "REPORT_GENERATION_FAILED"
    assert jobs.claim(store, "exhausted") is None
    assert len(store.query("SELECT * FROM mail_outbox")) == 1
    assert len(store.query("SELECT * FROM personal_notifications WHERE level='error'")) == 1
    assert "private transport detail" not in json_text(jobs.list_jobs(store, uid))


def test_automatic_report_does_not_replay_a_model_call_after_publication_error(cloud, monkeypatch):
    store, keys, _, _, _, uid = cloud
    profile(cloud)
    save_preferences(store, uid, Preferences(automatic_ai=True).model_dump())

    def publication_error(*_args, **_kwargs):
        raise OSError("simulated error after model returned")

    monkeypatch.setattr(jobs, "publish", publication_error)
    jobs.enqueue(store, uid, automatic=True)
    job = jobs.claim(store, "model-request")
    assert jobs.run(store, keys, job, lambda *_args: (GOOD, {"total_tokens": 10}, "one-call"))["error_code"] == "REPORT_GENERATION_FAILED"
    assert jobs.claim(store, "no-replay") is None
    assert [r["phase"] for r in store.query("SELECT phase FROM model_calls WHERE job_id=?", (job["id"],))] == ["received"]
