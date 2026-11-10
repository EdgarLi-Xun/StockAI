# 验证价格规则只按启用后的已完成日线生成限价委托，不改写撮合时点。
from datetime import datetime

import pytest

from backend import collectors, db, simulation
from backend.config import STRATEGY_VERSION, TZ
from backend.price_rules import client_key, explain_pending, judge, prepare, rule_id_from_client_key, session_usable


RULES = {"price_tick": "0.01", "lot_size": 100, "quantity_step": 100, "limit_pct": "0.1", "verified": True, "settlement_days": 1, "source_url": "https://www.sse.com.cn/rules"}


def instrument():
    return {"id": "SH:603126", "market": "SH", "asset_class": "stock", "rules": RULES}


def bar(day, close, open_price=None):
    price = open_price if open_price is not None else close
    return {"ts": datetime.fromisoformat(day + "T15:00:00+08:00").isoformat(), "open": price, "high": price + 0.05, "low": price - 0.05, "close": close, "volume": 1000000, "adjustment": "none", "quality": "valid", "metadata": {}}


def rule(kind="close_at_or_below", armed="2025-01-02T10:00:00+08:00", **params):
    base = {"trigger_price": "6", "limit_price": "6", "quantity": 100}
    base.update(params)
    return {"id": "rule-1", "kind": kind, "armed_at": armed, "params": base}


def position(quantity=0, available=0, cost=0):
    return {"quantity": quantity, "available": available, "cost": cost}


# 15:00 之前启用可以用当天收盘，15:00 及之后只能用之后的交易日。
def test_arm_time_uses_same_day_only_before_close():
    assert session_usable("2025-01-02T14:59:59+08:00", "2025-01-02")
    assert not session_usable("2025-01-02T15:00:00+08:00", "2025-01-02")
    assert session_usable("2025-01-02T15:00:00+08:00", "2025-01-03")
    assert not session_usable("2025-01-03T10:00:00+08:00", "2025-01-02")


# 启用前的 K 线不产生判断结果。
def test_bar_before_arm_is_ignored():
    assert judge(rule(armed="2025-01-03T10:00:00+08:00"), bar("2025-01-02", 5), [], position(), set(), 10000, db.settings().fees, 1000, "stock", RULES) is None


# 想法原文里的数字不会变成触发价。
def test_idea_note_is_not_parsed_into_prices():
    saved = prepare({"kind": "close_at_or_below", "source_kind": "idea", "source_note": "进场 收盘价 10 元就买\n离场 跌破 8\n错在哪 放量", "trigger_price": "4.00", "limit_price": "5.00", "quantity": 100}, instrument(), STRATEGY_VERSION)
    assert saved["params"]["trigger_price"] == "4.00"
    assert "就买" in saved["source_note"]
    with pytest.raises(ValueError, match="触发价"):
        prepare({"kind": "close_at_or_below", "source_kind": "idea", "source_note": "收盘价 10 元", "limit_price": "5", "quantity": 100}, instrument(), STRATEGY_VERSION)


# 回测入口只能冻结均线参数，不能改成价格条件。
def test_backtest_source_stays_on_moving_average():
    with pytest.raises(ValueError, match="均线"):
        prepare({"kind": "close_at_or_below", "source_kind": "backtest", "trigger_price": "4", "limit_price": "5", "quantity": 100}, instrument(), STRATEGY_VERSION)
    saved = prepare({"kind": "ma_forward", "source_kind": "backtest", "fast": 5, "slow": 20, "stop_loss_pct": 0.05}, instrument(), STRATEGY_VERSION)
    assert saved["params"]["strategy_version"] == STRATEGY_VERSION
    assert saved["max_orders"] == 100


# 现金不足以买入一手时记拒绝，并且幂等键稳定。
def test_moving_average_rejects_when_cash_cannot_buy_one_lot():
    closes = [5] * 20
    current = bar("2025-02-01", 20)
    decision = judge({"id": "ma", "kind": "ma_forward", "armed_at": "2025-01-01T10:00:00+08:00", "params": {"fast": 2, "slow": 3, "stop_loss_pct": "0.05", "strategy_version": STRATEGY_VERSION}}, current, closes, position(), set(), 10, db.settings().fees, 1000, "stock", RULES)
    assert decision["result"] == "rejected" and decision["reason"] == "含费预算或现金不足"
    assert decision["client_key"] is None


# 收盘落到触发价时给出买入意图；同方向待成交则不再发单。
def test_close_below_signals_once_per_pending_side():
    fees = db.settings().fees
    first = judge(rule(), bar("2025-01-02", 5), [], position(), set(), 10000, fees, 1000, "stock", RULES)
    assert first["result"] == "signal" and first["side"] == "buy" and first["client_key"] == client_key("rule-1", "2025-01-02", "buy")
    second = judge(rule(), bar("2025-01-02", 5), [], position(), {"buy"}, 10000, fees, 1000, "stock", RULES)
    assert second["result"] == "skipped" and second["reason"] == "已有同方向待成交委托"


# 从同步前缀里仍能还原规则编号。
def test_rule_id_survives_sync_prefix():
    assert rule_id_from_client_key("sync:abc:rule:rule-1:2025-01-02:buy") == "rule-1"


def _ready(valid_item, bars, monkeypatch):
    cfg = db.settings()
    cfg.fees.slippage_bps = 0
    cfg.fees.stock_transfer = 0
    db.save_settings(cfg)
    collectors.save_bars(valid_item["id"], bars[:4])
    last = bars[3]["ts"][:10]
    monkeypatch.setattr(collectors, "calendar_context", lambda *a, **k: {"calendar_verified": True, "last_completed_session": last, "date": last})
    monkeypatch.setattr(collectors, "fresh_reasons", lambda *a, **k: [])
    account = simulation.create_account("价格规则", 10000)
    return account


def _enable(item, account, armed, **extra):
    payload = {"account_id": account["id"], "instrument_id": item["id"], "kind": "close_at_or_below", "trigger_price": "9.00", "limit_price": "6.00", "quantity": 100, "source_kind": "manual"}
    payload.update(extra)
    saved = simulation.create_rule(payload)
    db.execute("UPDATE price_rules SET armed_at=? WHERE id=?", (armed, saved["id"]))
    return saved["id"]


# 启用后的收盘可以发信号，当天开盘不成交，下一根开盘才撮合。
def test_signal_waits_for_the_next_open(valid_item, bars, monkeypatch):
    account = _ready(valid_item, bars, monkeypatch)
    ident = _enable(valid_item, account, bars[0]["ts"][:10] + "T10:00:00+08:00")
    assert simulation.evaluate_rules() == [db.one("SELECT id FROM orders")["id"]]
    assert simulation.match_orders() == []
    db.execute("UPDATE orders SET created_at=? WHERE id=?", (bars[0]["ts"][:10] + "T10:00:00+08:00", db.one("SELECT id FROM orders")["id"]))
    fills = simulation.match_orders()
    assert len(fills) == 1 and db.one("SELECT source_ts FROM orders")["source_ts"] == bars[1]["ts"]
    assert simulation.evaluate_rules() == []
    assert db.one("SELECT COUNT(*) n FROM orders")["n"] == 1
    assert db.one("SELECT success_count FROM price_rules WHERE id=?", (ident,))["success_count"] == 1


# 15:00 之后启用，当天收盘不触发。
def test_arming_after_close_skips_that_session(valid_item, bars, monkeypatch):
    account = _ready(valid_item, bars, monkeypatch)
    _enable(valid_item, account, bars[0]["ts"][:10] + "T15:00:00+08:00")
    simulation.evaluate_rules()
    event = db.one("SELECT MIN(bar_date) day FROM price_rule_events WHERE result='created'")
    assert event["day"] == bars[1]["ts"][:10]


# 现金或预算不足记拒绝，不增加成功次数。第二条买入被同一证券预算挡住。
def test_budget_rejection_does_not_count_success(valid_item, bars, monkeypatch):
    account = _ready(valid_item, bars, monkeypatch)
    armed = bars[0]["ts"][:10] + "T10:00:00+08:00"
    first = _enable(valid_item, account, armed)
    second = _enable(valid_item, account, armed)
    simulation.evaluate_rules()
    counts = [db.one("SELECT success_count n FROM price_rules WHERE id=?", (ident,))["n"] for ident in (first, second)]
    assert db.one("SELECT COUNT(*) n FROM orders")["n"] == 1
    assert sorted(counts) == [0, 1]
    rejected = second if counts[0] == 1 else first
    assert db.one("SELECT result FROM price_rule_events WHERE price_rule_id=? ORDER BY bar_date LIMIT 1", (rejected,))["result"] == "rejected"


# 暂停后不再发单，已有待成交委托仍可撮合。
def test_paused_rule_does_not_order_but_pending_can_fill(valid_item, bars, monkeypatch):
    account = _ready(valid_item, bars, monkeypatch)
    ident = _enable(valid_item, account, bars[0]["ts"][:10] + "T10:00:00+08:00")
    simulation.pause_rule(ident)
    assert simulation.evaluate_rules() == []
    order = simulation.order(simulation.OrderInput(client_key="manual-pending-1", account_id=account["id"], instrument_id=valid_item["id"], side="buy", quantity=100, limit_price=6))
    db.execute("UPDATE orders SET created_at=? WHERE id=?", (bars[0]["ts"][:10] + "T10:00:00+08:00", order["id"]))
    assert len(simulation.match_orders()) == 1


# 均线跟随不把启用前的历史收盘写成委托。
def test_moving_average_does_not_replay_history_before_arm(valid_item, bars, monkeypatch):
    account = _ready(valid_item, bars, monkeypatch)
    simulation.create_rule({"account_id": account["id"], "instrument_id": valid_item["id"], "kind": "ma_forward", "fast": 2, "slow": 3, "stop_loss_pct": 0.05, "source_kind": "backtest", "max_orders": 5})
    assert simulation.evaluate_rules() == []
    assert db.one("SELECT COUNT(*) n FROM orders")["n"] == 0


# 接口只在明确提交后创建规则，想法原文不会变成触发价。
def test_rule_route_waits_for_explicit_prices(client, valid_item):
    account = client.post("/api/accounts", json={"name": "页面账户", "initial_cash": 10000}).json()
    refused = client.post("/api/simulation/rules", json={"account_id": account["id"], "instrument_id": valid_item["id"], "kind": "close_at_or_below", "source_kind": "idea", "source_note": "收盘价 10 元就买", "quantity": 100})
    assert refused.status_code == 400
    assert client.get("/api/simulation").json()["rules"] == []
    saved = client.post("/api/simulation/rules", json={"account_id": account["id"], "instrument_id": valid_item["id"], "kind": "close_at_or_below", "source_kind": "idea", "source_note": "收盘价 10 元就买", "trigger_price": "4.00", "limit_price": "5.00", "quantity": 100}).json()
    assert saved["params"]["trigger_price"] == "4.00" and "就买" in saved["source_note"]
    assert client.post(f"/api/simulation/rules/{saved['id']}/pause", json={}).status_code == 200
    view = client.get("/api/simulation").json()
    assert view["rules"][0]["status"] == "paused"
    assert "reserved_buy" in view["accounts"][0] and "available_cash" in view["accounts"][0]


# 待成交说明来自读取时的计算，不写回委托。
def test_pending_note_is_not_stored(valid_item, bars, monkeypatch):
    account = _ready(valid_item, bars, monkeypatch)
    order = simulation.order(simulation.OrderInput(client_key="manual-note-1", account_id=account["id"], instrument_id=valid_item["id"], side="buy", quantity=100, limit_price=1))
    view = simulation.portfolio()
    assert view["orders"][0]["pending_note"]
    assert db.one("SELECT reason FROM orders WHERE id=?", (order["id"],))["reason"] is None
    assert view["accounts"][0]["reserved_buy"] > 0
    assert view["accounts"][0]["available_cash"] < float(account["cash"])
    note = explain_pending(db.one("SELECT * FROM orders WHERE id=?", (order["id"],)), collectors.bars_for(valid_item["id"], "1d"), valid_item["rules"], 0, {"calendar_verified": True, "last_completed_session": bars[3]["ts"][:10]})
    assert note
