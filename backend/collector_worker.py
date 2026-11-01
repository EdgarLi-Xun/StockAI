"""Run a third-party collector in a killable process; never import AKShare in API workers."""
from __future__ import annotations

import contextlib
import io
import json
import sys


def collect(action: str, args: dict):
    import akshare as ak

    if action == "calendar":
        return ak.tool_trade_date_hist_sina()
    if action == "master_sh":
        return ak.stock_info_sh_name_code(symbol="主板A股")
    if action == "master_sz":
        return ak.stock_info_sz_name_code(symbol="A股列表")
    if action == "etf_names":
        return ak.fund_etf_spot_em()
    if action == "daily":
        kind, sym, market = args["asset_class"], args["symbol"], args["market"]
        params = dict(symbol=sym, period="daily", start_date=args["start"], end_date=args["end"], adjust="")
        if kind == "index":
            return ak.stock_zh_index_daily_em(symbol=market.lower() + sym, start_date=args["start"], end_date=args["end"])
        if kind == "etf":
            return ak.fund_etf_hist_em(**params)
        if market == "HK":
            return ak.stock_hk_hist(**params)
        if market == "US":
            return ak.stock_us_daily(symbol=sym,adjust="")
        return ak.stock_zh_a_hist(**params)
    if action == "daily_backup":
        if args["asset_class"] == "etf":
            return ak.fund_etf_hist_sina(symbol=args["market"].lower()+args["symbol"])
        if args["asset_class"] == "index":
            return ak.stock_zh_index_daily(symbol=args["market"].lower()+args["symbol"])
        if args["market"] == "HK":
            return ak.stock_hk_daily(symbol=args["symbol"],adjust="")
        if args["asset_class"] != "stock" or args["market"] not in ("SH", "SZ"):
            raise ValueError("此资产暂无备用日线适配器")
        return ak.stock_zh_a_daily(symbol=args["market"].lower() + args["symbol"], start_date=args["start"], end_date=args["end"], adjust="")
    if action == "minute":
        function = ak.fund_etf_hist_min_em if args["asset_class"] == "etf" else ak.stock_zh_a_hist_min_em
        return function(symbol=args["symbol"], period=args["period"], adjust="")
    if action == "international_metals":
        return ak.futures_foreign_commodity_realtime(symbol=["XAU", "XAG"])
    if action == "domestic_metals":
        # Retain the quote date discarded by AKShare's current public table.
        import re
        import requests
        import pandas as pd
        r=requests.get("https://hq.sinajs.cn/?list=nf_AU0,nf_AG0",headers={"Referer":"https://finance.sina.com.cn/"},timeout=12)
        r.raise_for_status()
        out=[]
        for code,body in re.findall(r'hq_str_nf_(AU0|AG0)="([^\"]+)"',r.text):
            cells=body.split(",")
            if len(cells)<18 or not re.fullmatch(r"\d{6}",cells[1]) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}",cells[17]):
                raise ValueError("国内期货字段、报价日期或时间无效")
            quote_time=cells[1][:2]+":"+cells[1][2:4]+":"+cells[1][4:]
            out.append({"名称":cells[0],"最新价":float(cells[8]),"日期":cells[17],"时间":quote_time})
        return pd.DataFrame(out)
    if action == "sge":
        return ak.spot_hist_sge(symbol=args["symbol"])
    if action == "announcements":
        return ak.stock_zh_a_disclosure_report_cninfo(symbol=args["symbol"], market="沪深京", start_date=args["start"], end_date=args["end"])
    if action == "fundamentals":
        return ak.stock_individual_info_em(symbol=args["symbol"])
    if action == "fund_nav":
        return ak.fund_open_fund_info_em(symbol=args["symbol"],indicator="单位净值走势").tail(2)
    if action == "macro_cpi":
        return ak.macro_usa_cpi_monthly().tail(12)
    if action == "macro_pmi":
        return ak.macro_china_pmi_yearly().tail(12)
    raise ValueError("未知采集动作")


if __name__ == "__main__":
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = collect(sys.argv[1], json.loads(sys.argv[2]))
        # Pandas' JSON serializer normalizes NaN and datetime objects.
        print(result.to_json(orient="records", force_ascii=False, date_format="iso"))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:400]}"}, ensure_ascii=False))
        sys.exit(1)
