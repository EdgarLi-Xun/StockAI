# 为个人研究对象提供统一的所有权、乐观版本控制、修订历史和删除语义。
from __future__ import annotations

import json
import uuid
from .auth import AuthError
from .store import stamp,json_text
from .research_control import control

KINDS={'alert','journal','topic','chat_session','usage_settings','notification_settings','readiness'}


def replay(store,uid,kind,client_key,request_fingerprint):
    if not client_key:return None
    row=store.one('SELECT * FROM research_objects WHERE user_id=? AND kind=? AND client_key=?',(uid,kind,client_key))
    if not row:return None
    if not store.active(uid):raise AuthError('ACCOUNT_DISABLED','账号不可用',403)
    payload=json.loads(row['payload_json'])
    if payload.get('creation_request_fingerprint')!=request_fingerprint:
        raise AuthError('REQUEST_CONFLICT','同一请求标识不能用于不同内容；旧请求缺少原始指纹时请使用新标识',409)
    return get(store,uid,kind,row['id'])


# 按用户、对象类型与标识读取资源，解析数据并核对归属。
def get(store,uid,kind,ident):
    if kind not in KINDS:raise ValueError('资源类型无效')
    row=store.one('SELECT * FROM research_objects WHERE id=? AND user_id=? AND kind=?',(ident,uid,kind))
    if not row:raise AuthError('NOT_FOUND','记录不存在',404)
    data=json.loads(row.pop('payload_json'))
    return {**row,'data':data}


# 在本人对象范围内筛选、搜索和分页，限制允许的页大小。
def listing(store,uid,kind,*,page=1,size=20,q='',status=''):
    if kind not in KINDS or size not in {20,50} or page<1:raise ValueError('分页或资源类型无效')
    args=[uid,kind];where='user_id=? AND kind=?'
    if status:where+=' AND status=?';args.append(status)
    if q:where+=' AND payload_json LIKE ?';args.append('%'+q[:100]+'%')
    total=store.one('SELECT COUNT(*) n FROM research_objects WHERE '+where,args)['n']
    rows=store.query('SELECT * FROM research_objects WHERE '+where+' ORDER BY updated_at DESC,id LIMIT ? OFFSET ?',[*args,size,(page-1)*size])
    for row in rows:row['data']=json.loads(row.pop('payload_json'))
    return {'items':rows,'total':total,'page':page,'size':size}


# 按客户端键幂等创建或按版本更新资源，同时追加不可覆盖的修订记录。
def save(store,uid,kind,data,*,ident=None,version=None,status='active',client_key=None):
    if kind not in KINDS:raise ValueError('资源类型无效')
    ident=ident or uuid.uuid4().hex;ts=stamp()
    with store.user_lock(uid),store.tx(immediate=True) as c:
        if not store.active(uid,c=c):raise AuthError('ACCOUNT_DISABLED','账号不可用',403)
        row=c.execute('SELECT * FROM research_objects WHERE id=? AND user_id=? AND kind=?',(ident,uid,kind)).fetchone()
        if version is not None:
            if not row:raise AuthError('NOT_FOUND','记录不存在',404)
            if version!=row['version']:raise AuthError('VERSION_CONFLICT','其他页面已保存新版本，请重新加载后编辑',409)
        elif row:
            raise AuthError('VERSION_CONFLICT','请携带当前版本号更新',409)
        if row:
            previous=json.loads(row['payload_json'])
            if previous.get('creation_request_fingerprint'):
                data={**data,'creation_request_fingerprint':previous['creation_request_fingerprint']}
        if not row and client_key:
            old=c.execute('SELECT * FROM research_objects WHERE user_id=? AND kind=? AND client_key=?',(uid,kind,client_key)).fetchone()
            if old:
                previous=json.loads(old['payload_json'])
                original=data.get('creation_request_fingerprint')
                same=(previous.get('creation_request_fingerprint')==original) if original else previous==data or bool(data.get('input_fingerprint') and previous.get('input_fingerprint')==data['input_fingerprint'])
                if not same:raise AuthError('REQUEST_CONFLICT','相同请求标识不能用于不同内容',409)
                return get(store,uid,kind,old['id'])
        new_version=row['version']+1 if row else 1
        c.execute('INSERT INTO research_objects VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET version=excluded.version,status=excluded.status,updated_at=excluded.updated_at,payload_json=excluded.payload_json',(ident,uid,kind,new_version,status,row['created_at'] if row else ts,ts,json_text(data),client_key))
        c.execute('INSERT INTO research_versions VALUES(?,?,?,?,?)',(ident,uid,new_version,ts,json_text(data)))
    sync=getattr(store,'sync_service',None)
    if kind=='usage_settings' and sync and sync.binding(uid):sync.request_limits(uid,{'daily':data.get('daily'),'monthly':data.get('monthly')})
    return get(store,uid,kind,ident)


# 先记录独立删除意图，再取消相关任务并删除本人对象。
def delete(store,uid,kind,ident):
    get(store,uid,kind,ident);stopped=[]
    with store.publish_lock,store.user_lock(uid):
        if kind=='topic' and store.one('SELECT id FROM research_experiments WHERE user_id=? AND topic_id=?',(uid,ident)):
            raise AuthError('TOPIC_HAS_EXPERIMENTS','课题包含独立实验，请归档课题以保留实验历史',409)
        control(store).delete(uid,kind,ident)
        with store.tx(immediate=True) as c:
            if kind=='chat_session':
                stopped=[r[0] for r in c.execute('SELECT job_id FROM assistant_requests WHERE user_id=? AND session_id=? AND job_id IS NOT NULL',(uid,ident))]
                for job in stopped:c.execute("UPDATE personal_jobs SET status='cancelled',error_code='SOURCE_DELETED',finished_at=?,snapshot_json='{}' WHERE id=? AND user_id=?",(stamp(),job,uid))
            if kind=='alert':
                for r in c.execute("SELECT * FROM alert_deliveries WHERE user_id=? AND state IN('queued','retry')",(uid,)).fetchall():
                    events=json.loads(r['event_ids_json'])
                    if c.execute('SELECT 1 FROM alert_events WHERE user_id=? AND rule_id=? AND id IN('+','.join('?' for _ in events)+')',(uid,ident,*events)).fetchone():
                        c.execute("UPDATE alert_deliveries SET state='cancelled',payload_json=NULL WHERE id=? AND user_id=?",(r['id'],uid))
            c.execute('DELETE FROM research_objects WHERE id=? AND user_id=? AND kind=?',(ident,uid,kind))
    for job in stopped:store.cancel_connection(job)
    return {'deleted':True,'id':ident}
