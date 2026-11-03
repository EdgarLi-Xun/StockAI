"""Signed, monotonic coordination state outside the restorable business database."""
import json
from backend.cloud.research_control import control
from backend.cloud.store import json_text,stamp


class Journal:
    def __init__(self,store,keys):
        self.store=store;self.control=control(store,keys)
        with self.control.tx() as c:
            c.execute("CREATE INDEX IF NOT EXISTS ix_coordination ON ledger(json_extract(payload,'$.domain'),json_extract(payload,'$.user_id'),json_extract(payload,'$.slot'))")

    def get(self,domain,uid,slot,default=None):
        with self.control.lock,self.control.tx() as c:
            row=c.execute("SELECT payload FROM ledger WHERE json_extract(payload,'$.type')='coordination' AND json_extract(payload,'$.domain')=? AND json_extract(payload,'$.user_id')=? AND json_extract(payload,'$.slot')=? ORDER BY seq DESC LIMIT 1",(domain,uid,slot)).fetchone()
        return json.loads(row[0])['value'] if row else default

    def put(self,domain,uid,slot,value):
        with self.control.lock:
            if self.get(domain,uid,slot)==value:return value
            with self.control.tx() as c:
                seq,sig=self.control.append(c,{'type':'coordination','domain':domain,'user_id':uid,'slot':slot,'value':value,'ts':stamp()})
            self.control.write_head(seq,sig)
        return value

    def all(self,domain,uid,prefix=''):
        result={}
        with self.control.lock,self.control.tx() as c:
            rows=c.execute("SELECT payload FROM ledger WHERE json_extract(payload,'$.type')='coordination' AND json_extract(payload,'$.domain')=? AND json_extract(payload,'$.user_id')=? ORDER BY seq",(domain,uid)).fetchall()
        for row in rows:
            item=json.loads(row[0])
            if item['slot'].startswith(prefix):result[item['slot']]=item['value']
        return result
