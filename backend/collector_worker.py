# 在独立采集进程中调用行情源，隔离超时、依赖及外部接口故障。
"""Run a third-party collector in a killable process; never import AKShare in API workers."""
from __future__ import annotations

import contextlib
import io
import json
import math
import re
import sys


BLS_CPI_SERIES="CUUR0000SA0"
BLS_CPI_URL=f"https://api.bls.gov/publicAPI/v2/timeseries/data/{BLS_CPI_SERIES}"


def bls_cpi_rows(payload):
    series=payload.get("Results",{}).get("series",[])
    if payload.get("status")!="REQUEST_SUCCEEDED" or len(series)!=1 or series[0].get("seriesID")!=BLS_CPI_SERIES:
        raise ValueError("BLS 未返回预期 CPI 序列")
    rows=[]
    for item in series[0].get("data",[]):
        period=str(item.get("period",""))
        year=str(item.get("year",""))
        if not re.fullmatch(r"M(0[1-9]|1[0-2])",period) or not re.fullmatch(r"[0-9]{4}",year):
            continue
        try:
            value=float(item["value"])
        except (KeyError,ValueError,TypeError):
            continue
        if math.isfinite(value) and value>0:
            rows.append({"日期":f"{year}-{period[1:]}-01","指标":"美国 CPI-U 总指数（未季调）","单位":"指数点","今值":value})
    return sorted(rows,key=lambda row:row["日期"])[-12:]


# 按请求选择数据源与采集动作，并将结果转换为可序列化记录。
def collect(action: str, args: dict):
    if action=="macro_cpi_bls":
        import pandas as pd
        import requests
        response=requests.get(BLS_CPI_URL,timeout=10)
        response.raise_for_status()
        return pd.DataFrame(bls_cpi_rows(response.json()))
    import akshare as ak

    if action == "calendar":
        return ak.tool_trade_date_hist_sina()
    if action == "master_sh":
        import pandas as pd
        return pd.concat([ak.stock_info_sh_name_code(symbol=board) for board in ("主板A股","科创板")],ignore_index=True)
    if action == "master_sz":
        return ak.stock_info_sz_name_code(symbol="A股列表")
    if action == "delist_sh":
        return ak.stock_info_sh_delist(symbol="全部")
    if action == "delist_sz":
        return ak.stock_info_sz_delist(symbol="终止上市公司")
    if action == "etf_names":
        # 名称取自基金基础清单，避免实时行情限流阻断身份资料补全。
        return ak.fund_name_em().rename(columns={"基金代码":"代码","基金简称":"名称"})[["代码","名称"]]
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
        # 补回上游公开表转换时丢弃的报价日期，保留数据时点。
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
        market="基金" if args.get("asset_class")=="etf" else "沪深京"
        return ak.stock_zh_a_disclosure_report_cninfo(symbol=args["symbol"], market=market, start_date=args["start"], end_date=args["end"])
    if action == "fundamentals":
        return ak.stock_individual_info_em(symbol=args["symbol"])
    if action == "financials":
        return ak.stock_financial_abstract_ths(symbol=args["symbol"]).tail(4)
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
        # 使用 Pandas JSON 序列化统一处理 NaN 和日期对象。
        print(result.to_json(orient="records", force_ascii=False, date_format="iso"))
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {str(exc)[:400]}"}, ensure_ascii=False))
        sys.exit(1)
