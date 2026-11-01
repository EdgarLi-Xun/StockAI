"""Traceable deep-research inputs. Missing context is never filled from model memory."""
from concurrent.futures import ThreadPoolExecutor

from . import collectors, db
from .config import now, timestamp


def sync(log=lambda m:None):
    tasks=[("macro_cpi",None),("macro_pmi",None)]
    tasks += [("fundamentals",r["id"]) for r in db.query("SELECT id FROM instruments WHERE watched=1 AND asset_class='stock' AND market IN ('SH','SZ')")]
    def collect(task):
        action,ident=task
        try:
            rows=collectors.call(action,{"symbol":ident.split(":")[1]} if ident else {},timeout=15)
            asof=timestamp()
            if not ident:
                rows=[r for r in rows if not r.get("日期") or str(r["日期"])[:10]<=now().date().isoformat()]
                dates=[str(r["日期"])[:10] for r in rows if r.get("日期")]
                if dates:
                    asof=max(dates)
            source="AKShare / 东方财富基本资料（非财报）" if ident else "AKShare / 金十经济数据（非官方核验）"
            url="https://quote.eastmoney.com/"+ident.lower().replace(":","")+".html" if ident else "https://datacenter.jin10.com/reportType/"+("dc_usa_cpi" if action=="macro_cpi" else "dc_chinese_manufacturing_pmi")
            key=action+":"+(ident or "global")
            db.execute("INSERT INTO research_context VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload_json=excluded.payload_json,source=excluded.source,source_url=excluded.source_url,as_of=excluded.as_of,fetched_at=excluded.fetched_at",(key,"fundamentals" if ident else "macro",ident,db.dumps(rows),source,url,asof,timestamp()))
            if ident:
                try:
                    collectors.announcements(ident)
                except Exception as exc:
                    log(f"{ident} 公告不可用：{str(exc)[:200]}")
            return {"id":key,"status":"success"}
        except Exception as exc:
            log(f"深度资料 {action} {ident or ''} 不可用：{str(exc)[:200]}")
            return {"id":action+":"+str(ident),"status":"unavailable","error":str(exc)[:300]}
    with ThreadPoolExecutor(max_workers=4) as pool:
        return list(pool.map(collect,tasks))
