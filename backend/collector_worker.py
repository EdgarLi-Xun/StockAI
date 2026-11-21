# 在独立采集进程中调用行情源，隔离超时、依赖及外部接口故障。
"""Run a third-party collector in a killable process; never import AKShare in API workers."""
from __future__ import annotations

import contextlib
import html
import io
import json
import math
import re
import sys
from datetime import datetime

from .config import TZ
from .trading_status import CNINFO_NOTICE_URL, NOTICE_KEYWORD, SSE_STATUS_URL, document_events


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


def sse_suspensions(args):
    import requests
    headers = {"Referer": SSE_STATUS_URL, "User-Agent": "Mozilla/5.0"}
    params = {"isPagination": "true", "sqlId": "GW_PL_JYTS_TFPXX", "pageHelp.pageSize": 100,
              "startStopDate": args["start"], "endStopDate": args["end"], "productCode": "", "keyWords": ""}
    rows = []
    for page in range(1, 6):
        params.update({"pageHelp.pageNo": page, "pageHelp.beginPage": page, "pageHelp.endPage": page, "pageHelp.cacheSize": 1})
        response = requests.get("https://query.sse.com.cn/commonSoaQuery.do", params=params, headers=headers, timeout=(4, 8))
        response.raise_for_status()
        value = response.json()
        if value.get("actionErrors") or value.get("fieldErrors") or not isinstance(value.get("result"), list):
            raise ValueError("上交所停复牌查询返回格式有误")
        rows.extend(value["result"])
        if page >= int(value["pageHelp"]["pageCount"]):
            return rows
    raise ValueError("上交所停复牌查询分页未完成")


def _notice_pdf(session, url):
    from pypdf import PdfReader
    limit = 3 * 1024 * 1024
    with session.get(url, timeout=(3, 6), stream=True, allow_redirects=False) as response:
        if response.status_code != 200:
            raise ValueError("官方附件不可用")
        payload = bytearray()
        for chunk in response.iter_content(65536):
            payload.extend(chunk)
            if len(payload) > limit:
                raise ValueError("官方附件超过读取上限")
    if not payload.startswith(b"%PDF-"):
        raise ValueError("附件并非 PDF")
    reader = PdfReader(io.BytesIO(payload))
    return "\n".join(page.extract_text() for page in reader.pages[:2])


def cninfo_announcements(args):
    import pandas as pd
    import requests

    symbol = args["symbol"]
    if not re.fullmatch(r"\d{6}", symbol):
        raise ValueError("巨潮资讯要求六位证券代码")
    is_fund = args.get("asset_class") == "etf"
    base = "https://www.cninfo.com.cn"
    dates = [datetime.strptime(args[k], "%Y%m%d").date().isoformat() for k in ("start", "end")]
    with requests.Session() as session:
        session.headers.update({"Referer": CNINFO_NOTICE_URL, "User-Agent": "Mozilla/5.0"})
        index = session.get(base + "/new/data/" + ("fund_stock.json" if is_fund else "szse_stock.json"), timeout=(4, 8))
        index.raise_for_status()
        org = next((r["orgId"] for r in index.json()["stockList"] if str(r.get("code")) == symbol), None)
        if not org:
            raise ValueError("巨潮资讯未返回该证券的官方机构编号")
        form = {"pageSize": "30", "column": "fund" if is_fund else "szse", "tabName": "fulltext",
                "stock": f"{symbol},{org}", "seDate": "~".join(dates), "isHLtitle": "false",
                "plate": "", "searchkey": "", "secid": "", "category": "", "trade": "", "sortName": "", "sortType": ""}
        raw = []
        for page in range(1, 11):
            response = session.post(base + "/new/hisAnnouncement/query", data={**form, "pageNum": page}, timeout=(4, 8))
            response.raise_for_status()
            payload = response.json()
            total = int(payload["totalAnnouncement"])
            if not total:
                break
            rows = payload.get("announcements")
            if not isinstance(rows, list) or not rows:
                raise ValueError("巨潮资讯公告分页不完整")
            raw.extend(rows)
            if page * 30 >= total:
                break
        else:
            raise ValueError("巨潮资讯公告分页超过单次采集上限")

        result = []
        for row in raw:
            if str(row.get("secCode")) != symbol or str(row.get("orgId")) != str(org):
                continue
            title = html.unescape(str(row.get("announcementTitle") or ""))
            notice_id = str(row.get("announcementId") or "")
            if not title or not re.fullmatch(r"[0-9]+", notice_id):
                continue
            published = datetime.fromtimestamp(int(row["announcementTime"]) / 1000, tz=TZ).isoformat(sep=" ", timespec="seconds")[:19]
            url = f"{base}/new/disclosure/detail?stockCode={symbol}&announcementId={notice_id}&orgId={org}&announcementTime={published}"
            item = {"代码": symbol, "公告标题": title, "公告链接": url, "公告时间": published}
            attachment = str(row.get("adjunctUrl") or "").lstrip("/")
            if NOTICE_KEYWORD.search(title) and re.fullmatch(r"finalpage/\d{4}-\d{2}-\d{2}/[0-9]+\.[pP][dD][fF]", attachment):
                item["附件链接"] = "https://static.cninfo.com.cn/" + attachment
                try:
                    item["trading_events"] = document_events(symbol, _notice_pdf(session, item["附件链接"]))
                except Exception:
                    item["status_read_error"] = True
            result.append(item)
        return pd.DataFrame(result)


# 按请求选择数据源与采集动作，并将结果转换为可序列化记录。
def collect(action: str, args: dict):
    if action == "sse_suspensions":
        import pandas as pd
        return pd.DataFrame(sse_suspensions(args))
    if action == "announcements":
        return cninfo_announcements(args)
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
