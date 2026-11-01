from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP, ROUND_FLOOR

from .config import Fees

CENT = Decimal("0.01")


def dec(value) -> Decimal:
    d = Decimal(str(value))
    if not d.is_finite():
        raise ValueError("金额必须是有限数值")
    return d


def money(value) -> Decimal:
    return dec(value).quantize(CENT, rounding=ROUND_HALF_UP)


def transaction_fees(price, quantity: int, asset: str, side: str, fees: Fees, multiplier=1) -> dict:
    gross = dec(price) * quantity
    is_etf = asset == "etf"
    commission = money(max(gross * dec(fees.etf_commission if is_etf else fees.stock_commission), dec(fees.etf_minimum if is_etf else fees.stock_minimum)) * dec(multiplier))
    transfer = money(gross * dec(fees.stock_transfer) * dec(multiplier)) if not is_etf else Decimal(0)
    stamp = money(gross * dec(fees.stock_stamp_sell) * dec(multiplier)) if side == "sell" and not is_etf else Decimal(0)
    return {"commission": float(commission), "transfer": float(transfer), "stamp_tax": float(stamp), "total": float(commission + transfer + stamp), "stamp_applicable": side == "sell" and not is_etf, "transfer_applicable": not is_etf, "assumption": not fees.confirmed}


def cost(price, quantity: int, asset: str, fees: Fees, multiplier=1) -> Decimal:
    return money(dec(price) * quantity + dec(transaction_fees(price, quantity, asset, "buy", fees, multiplier)["total"]))


def budget_check(price, rules: dict, asset: str, fees: Fees, budget=1000, quantity: int | None = None) -> dict:
    p = dec(price)
    cap = min(dec(budget), Decimal(1000))
    if p <= 0 or cap <= 0:
        raise ValueError("价格与预算须为正数")
    lot, step = int(rules["lot_size"]), int(rules["quantity_step"])
    tick = dec(rules["price_tick"])
    if p % tick != 0:
        raise ValueError("价格不符合该产品最小变动单位")
    if quantity is not None and (quantity < lot or (quantity - lot) % step != 0):
        raise ValueError("数量不符合该产品最小申报数量和阶梯")
    q = int((cap / p).to_integral_value(rounding=ROUND_FLOOR))
    q = lot + ((q - lot) // step) * step if q >= lot else 0
    while q >= lot and cost(p, q, asset, fees) > cap:
        q -= step
    q = max(q, 0)
    selected = quantity if quantity is not None else (q or lot)
    amount = cost(p, selected, asset, fees)
    return {"price": float(p), "quantity": selected, "max_quantity": q, "gross": float(money(p * selected)), "fees": transaction_fees(p, selected, asset, "buy", fees), "total": float(amount), "budget": float(cap), "within_budget": amount <= cap, "minimum_total": float(cost(p, lot, asset, fees)), "rules_verified": rules.get("verified", False), "fee_note": fees.note, "sensitivity": [{"minimum_commission": m, "total": float(cost(p, selected, asset, fees.model_copy(update={"etf_minimum" if asset == "etf" else "stock_minimum": m})))} for m in (0, 5, 10)]}
