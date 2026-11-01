"""Isolated legacy collectors touch shared tables only, in the public data directory."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid

from backend.config import ROOT,now
from .store import json_text,stamp


def enqueue(store,kind="sync",instrument_id=None,timeframe="1d"):
    if kind not in {"sync","metals","instrument","announcements","context"}:
        raise ValueError("不支持的站点任务")
    logical=f"{kind}:{instrument_id or '*'}:{timeframe}"
    with store.tx(immediate=True) as c:
        old=c.execute("SELECT * FROM site_jobs WHERE logical_key=? AND status IN('queued','running')",(logical,)).fetchone()
        if old:
            return {"id":old["id"],"status":old["status"],"reused":True}
        # Keep an audit trail while allowing a later explicit refresh.
        c.execute("UPDATE site_jobs SET logical_key=logical_key||':'||id WHERE logical_key=?",(logical,))
        ident=uuid.uuid4().hex
        c.execute("INSERT INTO site_jobs(id,logical_key,kind,status,created_at,result_json) VALUES(?,?,?,'queued',?,?)",(ident,logical,kind,stamp(),json_text({"instrument_id":instrument_id,"timeframe":timeframe})))
    return {"id":ident,"status":"queued","reused":False}


def run_one(store):
    with store.tx(immediate=True) as c:
        row=c.execute("SELECT * FROM site_jobs WHERE status='queued' AND kind!='backup' ORDER BY created_at LIMIT 1").fetchone()
        if not row:
            return False
        c.execute("UPDATE site_jobs SET status='running',started_at=? WHERE id=?",(stamp(),row["id"]))
        row=dict(row)
    args=json.loads(row["result_json"] or "{}");env=os.environ.copy();env["STOCKAI_DATA_DIR"]=str(store.cfg.data)
    try:
        result=subprocess.run([sys.executable,"-X","utf8","-m","backend.cloud.collector",row["kind"],json_text(args)],cwd=ROOT,env=env,capture_output=True,text=True,encoding="utf-8",timeout=900)
        value=json.loads(result.stdout)
        if result.returncode or not isinstance(value,dict):
            raise ValueError()
        store.execute("UPDATE site_jobs SET status='success',finished_at=?,result_json=? WHERE id=?",(stamp(),json_text(value),row["id"]))
    except Exception:
        store.execute("UPDATE site_jobs SET status='failed',finished_at=?,error_code='SHARED_COLLECTION_FAILED',result_json=NULL WHERE id=?",(stamp(),row["id"]))
    return True


def main():
    from backend import db,collectors,instruments,context
    kind=sys.argv[1];args=json.loads(sys.argv[2])
    # Store.initialize already created the database; do not invoke db.initialize/seed/settings.
    # The union is refreshed once, never once per user's report.
    if kind=="sync":
        with db.connection() as c:
            c.execute("UPDATE instruments SET watched=0,group_name='',pinned=0")
            c.execute("UPDATE instruments SET watched=1 WHERE asset_class IN('index','metal','future') OR id IN(SELECT DISTINCT w.instrument_id FROM user_watchlist w JOIN users u ON u.id=w.user_id WHERE u.status='active')")
        value=collectors.sync_all()
        db.execute("UPDATE instruments SET watched=0,group_name='',pinned=0")
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
