# 采集交易日历和辅助市场资料，为研究报告提供可追溯的背景事实。
"""Traceable deep-research inputs. Missing context is never filled from model memory."""
from concurrent.futures import ThreadPoolExecutor

from . import collectors, db, instruments
from .collector_worker import BLS_CPI_URL
from .config import now, timestamp


def model_data(category, rows):
    if not isinstance(rows,list):
        return []
    if category=="fundamentals":
        fields={"行业","上市时间","总市值","流通市值","总股本","流通股"}
        return [{"item":r["item"],"value":str(r.get("value",""))[:100]} for r in rows if isinstance(r,dict) and r.get("item") in fields][:8]
    if category=="macro":
        fields=("日期","指标","单位","今值","前值","预测值")
    elif category=="financials":
        fields=("报告期","营业总收入","营业收入","净利润","归属于上市公司股东的净利润","净利润同比增长率","净资产收益率","基本每股收益")
    else:
        return []
    data=[{k:r[k] for k in fields if isinstance(r.get(k),(int,float)) or isinstance(r.get(k),str) and len(r[k])<=100}
          for r in rows[-4:] if isinstance(r,dict)]
    return [row for row in data if ("今值" in row if category=="macro" else len(row)>1)]


# 刷新可追溯的市场背景，并分别记录采集结果与失败原因。
def sync(log=lambda m:None, *, instrument_ids=None):
    tasks=[("macro_cpi_bls",None),("macro_cpi",None),("macro_pmi",None)]
    tasks.extend(("sge_history", ident) for ident in collectors.SGE_HISTORY)
    tasks.extend(("metal_equity", ident) for ident in instruments.METAL_LINKED_SECURITIES)
    if instrument_ids is None:
        instrument_ids=[r["id"] for r in db.query("SELECT id FROM instruments WHERE watched=1 AND asset_class='stock' AND market IN ('SH','SZ')")]
    tasks.extend((action,ident) for ident in instrument_ids for action in ("fundamentals","financials"))
    # 调用指定背景资料采集器并记录结果，单项失败不阻断其他资料。
    def collect(task):
        action,ident=task
        try:
            if action=="sge_history":
                return collectors.sync_sge_history(ident)
            if action=="metal_equity":
                known = collectors.bars_for(ident, limit=1)
                if known and not collectors.fresh_reasons(known[-1]):
                    return {"id": ident, "status": "success", "last_quote_time": known[-1]["ts"]}
                return collectors.sync_instrument(instruments.get(ident))
            rows=collectors.call(action,{"symbol":ident.split(":")[1]} if ident else {},timeout=25)
            asof=timestamp()
            if action=="fundamentals":
                category,source="fundamentals","AKShare / 东方财富证券概况"
                url=f"https://quote.eastmoney.com/{ident.lower().replace(':','')}.html"
            elif action=="financials":
                category,source="financials","AKShare / 同花顺财务摘要"
                url=f"https://basic.10jqka.com.cn/new/{ident.split(':')[1]}/finance.html"
            elif action=="macro_cpi_bls":
                category,source,url="macro","美国劳工统计局 · CPI-U",BLS_CPI_URL
            else:
                category,source="macro","AKShare / 金十经济数据"
                series="dc_usa_cpi" if action=="macro_cpi" else "dc_chinese_manufacturing_pmi"
                url=f"https://datacenter.jin10.com/reportType/{series}"
            date_field="日期" if category=="macro" else "报告期" if category=="financials" else None
            if date_field:
                today=now().date().isoformat()
                rows=sorted((r for r in rows if r.get(date_field) and str(r[date_field])[:10]<=today),key=lambda r:str(r[date_field]))
                if rows:
                    asof=str(rows[-1][date_field])[:10]
            if not any(model_data(category,rows)):
                raise ValueError("没有可读的指标数据")
            key=action+":"+(ident or "global")
            db.execute("INSERT INTO research_context VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,source=excluded.source,source_url=excluded.source_url,as_of=excluded.as_of,fetched_at=excluded.fetched_at",(key,category,ident,db.dumps(rows),source,url,asof,timestamp()))
            return {"id":key,"status":"success"}
        except Exception as exc:
            log(f"深度资料 {action} {ident or ''} 不可用：{str(exc)[:200]}")
            return {"id":action+":"+str(ident),"status":"unavailable","error":str(exc)[:300]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(collect,tasks))
