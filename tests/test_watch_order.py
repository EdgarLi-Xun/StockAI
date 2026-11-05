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
