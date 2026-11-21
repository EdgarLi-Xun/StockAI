from backend import collectors, db
from backend.cloud.market import watch_priority


def test_watch_priority_puts_recorded_holdings_before_pins():
    rows = [
        {"id": "plain", "pinned": False, "position": {"cost_price": None, "quantity": None}},
        {"id": "pin", "pinned": True, "position": {"cost_price": "", "quantity": None}},
        {"id": "held", "pinned": False, "position": {"cost_price": "8", "quantity": 100}},
        {"id": "both", "pinned": True, "position": {"cost_price": "9", "quantity": 200}},
        {"id": "held-later", "pinned": False, "position": {"cost_price": "7", "quantity": 1}},
    ]
    assert [row["id"] for row in sorted(rows, key=watch_priority)] == ["both", "held", "held-later", "pin", "plain"]


def test_watch_priority_ignores_a_boolean_quantity():
    assert watch_priority({"pinned": False, "position": {"cost_price": "1", "quantity": True}}) == 3


def test_local_dashboard_shows_spot_quote_before_daily_bars(client, valid_item, monkeypatch):
    ident = valid_item["id"]
    db.execute("UPDATE instruments SET watched=1 WHERE id=?", (ident,))
    monkeypatch.setattr(collectors, "_spot_targets", lambda: [valid_item])
    monkeypatch.setattr(collectors, "fetch_spots", lambda _: [
        {"f12": valid_item["symbol"], "f13": 1, "f2": 8.12, "f18": 8.0},
    ])
    assert collectors.sync_spots()["updated"] == 1
    assert collectors.bars_for(ident) == []

    result = client.get("/api/dashboard")
    assert result.status_code == 200
    row = next(item for item in result.json()["watchlist"] if item["id"] == ident)
    assert row["quote"]["price"] == 8.12
    assert row["quote"]["kind"] == "spot"
    assert row["budget"] is None
    assert "没有已确认行情" in row["exclusion_reasons"]
