# 在公网数据目录内隔离运行旧采集器，仅更新共享市场资料。
"""Isolated legacy collectors touch shared tables only, in the public data directory."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

from backend.config import ROOT,now
from .store import json_text,stamp
from .auth import AuthError
from backend.processes import module_command,hidden_process


# 校验站点采集类型并按证券和周期构造幂等任务键。
def enqueue(store,kind="sync",instrument_id=None,timeframe="1d",*,c=None,retry_of=None):
    if kind not in {"sync","names","metals","instrument","announcements","context"}:
        raise ValueError("不支持的站点任务")
    if c is None:
        with store.tx(immediate=True) as conn:
            return enqueue(store,kind,instrument_id,timeframe,c=conn,retry_of=retry_of)
    logical=f"retry:{retry_of}" if retry_of else f"{kind}:{instrument_id or '*'}:{timeframe}"
    if retry_of:
        old=c.execute('SELECT id,status FROM site_jobs WHERE logical_key=?',(logical,)).fetchone()
        if old:return {**dict(old),'reused':True}
    # 名称采集进行中仍可合并排入一次后续任务，覆盖稍后加入的证券。
    old=c.execute("SELECT * FROM site_jobs WHERE logical_key=? AND (status='queued' OR(status='running' AND ?!='names'))",(logical,kind)).fetchone()
    if old:
        return {"id":old["id"],"status":old["status"],"reused":True}
    # 保留历史采集审计记录，同时允许用户稍后明确发起新刷新。
    c.execute("UPDATE site_jobs SET logical_key=logical_key||':'||id WHERE logical_key=?",(logical,))
    ident=uuid.uuid4().hex
    c.execute("INSERT INTO site_jobs(id,logical_key,kind,status,created_at,result_json) VALUES(?,?,?,'queued',?,?)",(ident,logical,kind,stamp(),json_text({"instrument_id":instrument_id,"timeframe":timeframe})))
    return {"id":ident,"status":"queued","reused":False}


# 添加、导入和启动时合并补全名称，不在接口事务内等待外部网络。
def queue_names(store,*,c=None):
    if c is None:
        with store.tx(immediate=True) as conn:
            return queue_names(store,c=conn)
    pending=c.execute("""SELECT 1 FROM instruments i
        JOIN user_watchlist w ON w.instrument_id=i.id JOIN users u ON u.id=w.user_id
        WHERE u.status='active' AND i.market IN('SH','SZ') AND i.asset_class IN('stock','etf')
        AND (i.name LIKE '%待核验%' OR trim(i.name)='') LIMIT 1""").fetchone()
    return enqueue(store,"names",c=c) if pending else None


# 领取一项站点任务，在受控子进程中采集并记录结果。
def run_one(store):
    with store.tx(immediate=True) as c:
        if c.execute("SELECT value FROM cloud_meta WHERE key='maintenance'").fetchone()[0]!='0':return None
        row=c.execute("SELECT * FROM site_jobs WHERE status='queued' AND kind!='backup' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return False
        c.execute("UPDATE site_jobs SET status='running',started_at=? WHERE id=?",(stamp(),row["id"]))
        row=dict(row)
    args=json.loads(row["result_json"] or "{}");env=os.environ.copy();env["STOCKAI_DATA_DIR"]=str(store.cfg.data)
    try:
        result=subprocess.run(module_command("backend.cloud.collector",row["kind"],json_text(args)),cwd=ROOT,env=env,capture_output=True,text=True,encoding="utf-8",timeout=900,**hidden_process())
        value=json.loads(result.stdout)
        if result.returncode or not isinstance(value,dict):
            raise ValueError()
        status=result_status(value)
        value['_request']=args
        store.execute("UPDATE site_jobs SET status=?,finished_at=?,result_json=?,error_code=? WHERE id=?",(status,stamp(),json_text(value),"SHARED_COLLECTION_PARTIAL" if status=='partial' else "SHARED_COLLECTION_FAILED" if status=='failed' else None,row["id"]))
    except Exception:
        store.execute("UPDATE site_jobs SET status='failed',finished_at=?,error_code='SHARED_COLLECTION_FAILED',result_json=? WHERE id=?",(stamp(),json_text({'_request':args}),row["id"]))
    return True


def result_status(value):
    status=value.get('status')
    if status in {'partial','unavailable','failed'}:return 'partial' if status=='partial' else 'failed'
    if status=='success':return 'success'
    results=value.get('result')
    if isinstance(results,list) and results:
        good=sum(isinstance(row,dict) and row.get('status')=='success' for row in results)
        return 'success' if good==len(results) else 'partial' if good else 'failed'
    return 'success'


def list_jobs(store):
    rows=store.query('SELECT * FROM site_jobs ORDER BY created_at DESC LIMIT 100')
    for row in rows:
        row['result']=json.loads(row.pop('result_json') or '{}')
        row['error']={'SHARED_COLLECTION_FAILED':'共享资料采集失败，请检查网络后重新运行','SHARED_COLLECTION_PARTIAL':'部分资料未能取得，保留已有缓存','PROCESS_RESTARTED':'服务重启中断了任务','ENCRYPTED_BACKUP_FAILED':'加密备份失败，请检查备份配置和磁盘空间'}.get(row['error_code'],row['error_code'])
        row['can_retry']=row['status'] in {'failed','partial'} and row['kind'] in {'sync','names','metals','context','instrument','announcements'} and (row['kind'] not in {'instrument','announcements'} or bool(row['result'].get('_request',{}).get('instrument_id')))
        row['retry_label']='重新采集'
        row['retry_reason']='请在原证券页面重新同步' if row['kind'] in {'instrument','announcements'} and not row['can_retry'] else ''
    return rows


def retry(store,ident):
    with store.tx(immediate=True) as c:
        row=c.execute('SELECT * FROM site_jobs WHERE id=?',(ident,)).fetchone()
        if not row:raise AuthError('NOT_FOUND','任务不存在',404)
        if row['status'] not in {'failed','partial'} or row['kind']=='backup':raise AuthError('JOB_NOT_RETRYABLE','此任务不能重试，请检查当前状态',409)
        args=json.loads(row['result_json'] or '{}').get('_request',{})
        if row['kind'] in {'instrument','announcements'} and not args.get('instrument_id'):raise AuthError('JOB_NOT_RETRYABLE','旧任务缺少采集参数，请在原证券页面重新同步',409)
        return enqueue(store,row['kind'],args.get('instrument_id'),args.get('timeframe','1d'),c=c,retry_of=ident)


# 在隔离进程内读取采集参数，并调用对应共享数据采集入口。
def main():
    from backend import db,collectors,instruments,context
    kind=sys.argv[1];args=json.loads(sys.argv[2])
    # 公网存储已完成建表，此处不再执行本地初始化、种子数据或本地设置逻辑。
    # 所有用户的自选并集只统一采集一次，避免每份个人报告重复采集。
    if kind=="sync":
        with db.connection() as c:
            c.execute("UPDATE instruments SET watched=0,group_name='',pinned=0")
            c.execute("UPDATE instruments SET watched=1 WHERE asset_class IN('index','metal','future') OR id IN(SELECT DISTINCT w.instrument_id FROM user_watchlist w JOIN users u ON u.id=w.user_id WHERE u.status='active')")
        value=collectors.sync_all()
        db.execute("UPDATE instruments SET watched=0,group_name='',pinned=0")
    elif kind=="names":
        value=collectors.sync_names()
    elif kind=="metals":
        value=collectors.sync_metals()
    elif kind=="instrument":
        value=collectors.sync_instrument(instruments.get(args["instrument_id"]),args["timeframe"])
    elif kind=="announcements":
        value={"announcements":collectors.announcements(args["instrument_id"])}
    elif kind=="context":
        value=context.sync()
    else:
        raise ValueError("未知站点任务")
    print(json_text(value if isinstance(value,dict) else {"result":value}))


if __name__=="__main__":
    main()
