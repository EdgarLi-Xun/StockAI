"""Whitelisted owned business aggregates; receiving never runs jobs, inference or matching."""
from __future__ import annotations
import base64
from decimal import Decimal
import json
from pathlib import Path
import uuid
from backend.cloud.store import json_text
from backend.price_rules import client_key as price_rule_key
from .protocol import SyncError,digest,MAX_OBJECT

SECRET={'api_key','password','credential_id','envelope_json','key_hint','token','csrf_hash','authorization','smtp_password','allow_summary','allow_trades','allow_simulation_summary','allow_trade_details'}
REFS={'report_id':'personal_reports','source_report_id':'personal_reports','series_id':'report_series','parent_id':None,'account_id':'sim_accounts','order_id':'sim_orders','price_rule_id':'sim_price_rules','session_id':'research_objects','object_id':'research_objects','rule_id':'research_objects','request_id':'assistant_requests','regenerate_of':'assistant_requests','topic_id':'research_objects','result_id':'personal_backtests','profile_id':'model_profiles','experiment_id':'research_experiments'}
KINDS={'report':'personal_reports','account':'sim_accounts','backtest':'personal_backtests','experiment':'research_experiments','profile':'model_profiles','object':'research_objects','delivery':'alert_deliveries'}
ALLOWED={'user_watchlist','user_settings','model_profiles','model_profile_versions','model_context_limits','report_series','personal_reports','personal_candidates','sim_accounts','sim_orders','sim_lots','sim_ledger','sim_equity','sim_price_rules','sim_price_rule_events','portfolio_valuation_history','personal_backtests','research_experiments','research_objects','research_versions','journal_observation_history','alert_state','alert_events','assistant_requests','assistant_chunks'}
COMPOSITE={'user_watchlist','user_settings','model_profile_versions','model_context_limits','personal_candidates','sim_equity','research_versions','assistant_chunks'}
INTEGER={'sim_ledger'}
ALLOWED.update({'alert_deliveries','alert_delivery_events'});COMPOSITE.add('alert_delivery_events');REFS.update(event_id='alert_events',delivery_id='alert_deliveries')


def scrub(value):
    if isinstance(value,dict):return {k:scrub(v) for k,v in value.items() if k.lower() not in SECRET and k!='user_id'}
    if isinstance(value,list):return [scrub(v) for v in value]
    return value


class Adapter:
    def __init__(self,store,uid,space,device):self.store,self.uid,self.space,self.device=store,uid,space,device;self.created_maps=[]

    def gid(self,table,ident,c=None):
        if ident is None:return None
        ident=str(ident)
        query=c.execute if c else None
        row=(query('SELECT global_id FROM sync_rows WHERE user_id=? AND table_name=? AND local_id=?',(self.uid,table,ident)).fetchone() if c else self.store.one('SELECT global_id FROM sync_rows WHERE user_id=? AND table_name=? AND local_id=?',(self.uid,table,ident)))
        if row:return row[0] if c else row['global_id']
        shared=table in ('user_watchlist','user_settings')
        gid=uuid.uuid5(uuid.NAMESPACE_URL,':'.join([self.space,'' if shared else self.device,table,ident if table!='user_settings' else 'preferences'])).hex
        if c:c.execute('INSERT INTO sync_rows VALUES(?,?,?,?)',(self.uid,table,ident,gid))
        else:self.store.execute('INSERT OR IGNORE INTO sync_rows VALUES(?,?,?,?)',(self.uid,table,ident,gid))
        self.created_maps.append({'table':table,'local':ident,'global':gid})
        return gid

    def local_id(self,table,gid,c,integer=False):
        row=c.execute('SELECT local_id FROM sync_rows WHERE user_id=? AND table_name=? AND global_id=?',(self.uid,table,gid)).fetchone()
        if row:return int(row[0]) if integer else row[0]
        if integer:
            maximum=c.execute('SELECT COALESCE(MAX(id),0) FROM '+table).fetchone()[0]
            other=c.execute('SELECT COALESCE(MAX(CAST(local_id AS INTEGER)),0) FROM sync_rows WHERE table_name=?',(table,)).fetchone()[0]
            local=str(max(maximum,other)+1)
        else:local=uuid.uuid5(uuid.NAMESPACE_URL,':'.join([self.store.site_id,self.uid,self.space,table,gid])).hex
        c.execute('INSERT INTO sync_rows VALUES(?,?,?,?)',(self.uid,table,local,gid))
        self.created_maps.append({'table':table,'local':local,'global':gid})
        return int(local) if integer else local

    def nested(self,value,outgoing,c,table=None):
        if isinstance(value,list):return [self.nested(v,outgoing,c,table) for v in value]
        if not isinstance(value,dict):return value
        result={}
        for key,item in value.items():
            if key.lower() in SECRET or key=='user_id':continue
            target=REFS.get(key)
            if key=='id' and value.get('kind') in KINDS:target=KINDS[value['kind']]
            if key=='parent_id':target='research_experiments' if table=='research_experiments' else 'personal_reports' if table=='personal_reports' else None
            if target and item and isinstance(item,str):
                result[key]=self.gid(target,item,c) if outgoing else self.local_id(target,item,c)
            elif key=='order_ids' and isinstance(item,list):result[key]=[self.gid('sim_orders',v,c) if outgoing else self.local_id('sim_orders',v,c) for v in item]
            else:result[key]=self.nested(item,outgoing,c,table)
        return result

    def normalize(self,table,row,c):
        value={k:v for k,v in dict(row).items() if k not in SECRET and k!='user_id'}
        if isinstance(value.get('client_key'),str) and value['client_key'].startswith('sync:'):value['client_key']=value['client_key'].split(':',2)[2]
        if table=='sim_orders':
            # 旧接收端的 key 仍含发送端规则编号，优先用事件的外键恢复关联。
            event=c.execute("SELECT price_rule_id,bar_date FROM sim_price_rule_events WHERE user_id=? AND account_id=? AND order_id=? AND result='created' ORDER BY bar_date,id LIMIT 1",(self.uid,row['account_id'],row['id'])).fetchone()
            parts=str(value.get('client_key') or '').split(':')
            rule=event['price_rule_id'] if event else parts[1] if len(parts)==4 and parts[0]=='rule' and parts[3]==row['side'] else None
            if rule and c.execute('SELECT id FROM sim_price_rules WHERE id=? AND user_id=? AND account_id=?',(rule,self.uid,row['account_id'])).fetchone():
                value['client_key']=price_rule_key(self.gid('sim_price_rules',rule,c),event['bar_date'] if event else parts[2],row['side'])
        if table=='user_settings':
            value.pop('default_profile_id',None)
            settings=json.loads(value['payload_json'])
            for k in ('automatic_ai','morning_enabled','closing_enabled','weekly_enabled'):settings.pop(k,None)
            value['payload_json']=json_text(settings)
        if table=='model_profiles':value.pop('active',None);value.pop('needs_reconnect',None)
        if table=='personal_reports':value.pop('markdown_path',None);value.pop('html_path',None);value['job_id']=None
        if table=='report_series' and value['kind'].startswith('synced:'):value['kind']=value['kind'].split(':',2)[1]
        if table=='research_experiments':value['job_id']=None
        if table=='assistant_requests':
            payload=json.loads(value.get('payload_json') or '{}')
            value['payload_json']=json_text({k:payload[k] for k in ('contexts','retrieved_contexts','lookup_contexts','lookup_sensitivity','read_preferences','read_watchlist') if k in payload})
            value['job_id']=None
        if table in ('assistant_requests','research_experiments') and value.get('status') in ('queued','running','validating'):
            value['status']='interrupted';value['error_code']='SYNC_RECEIVED_INTERRUPTED'
        for key in list(value):
            item=value[key]
            if table=='alert_deliveries' and key=='event_ids_json':value[key]=[self.gid('alert_events',v,c) for v in json.loads(item)];continue
            if key=='id' and table not in COMPOSITE:value[key]=self.gid(table,item,c)
            elif key in REFS and item:
                target=REFS[key] or ('research_experiments' if table=='research_experiments' else 'personal_reports')
                value[key]=self.gid(target,item,c)
            elif key.endswith('_json') and item:
                value[key]=self.nested(json.loads(item),True,c,table)
        return value

    def rows(self,c,table,where,args):
        return [self.normalize(table,row,c) for row in c.execute('SELECT * FROM '+table+' WHERE '+where+' ORDER BY rowid',args)]

    def export(self,kind,ident,c):
        tables={};attachments={}
        def owned(table,column='id',value=ident):
            rows=self.rows(c,table,'user_id=? AND '+column+'=?',(self.uid,value));tables[table]=rows;return rows
        if kind=='watch':
            root=owned('user_watchlist','instrument_id')
            if root:
                item=c.execute('SELECT id,symbol,market,name,asset_class FROM instruments WHERE id=?',(ident,)).fetchone();tables['instruments']=[dict(item)]
        elif kind=='preferences':root=owned('user_settings','user_id',self.uid)
        elif kind=='profile':
            root=owned('model_profiles')
            if root:
                tables['model_profile_versions']=self.rows(c,'model_profile_versions','profile_id=?',(ident,));tables['model_context_limits']=self.rows(c,'model_context_limits','profile_id=?',(ident,))
        elif kind=='report':
            root=owned('personal_reports')
            if root:
                original=c.execute('SELECT * FROM personal_reports WHERE id=? AND user_id=?',(ident,self.uid)).fetchone()
                owned('report_series','id',original['series_id']);owned('personal_candidates','report_id')
                for label in ('markdown','html'):
                    path=(self.store.cfg.data/original[label+'_path']).resolve()
                    if not path.is_relative_to((self.store.cfg.data/'reports'/self.uid).resolve()) or not path.is_file():raise SyncError('DEPENDENCY','归档报告文件缺失')
                    data=path.read_bytes()
                    if len(data)>MAX_OBJECT//2:raise SyncError('CORRUPT')
                    attachments[label]={'sha256':digest(data),'data':base64.b64encode(data).decode()}
        elif kind=='account':
            root=owned('sim_accounts')
            if root:
                for table in ('sim_orders','sim_lots','sim_ledger','sim_equity','portfolio_valuation_history','sim_price_rules','sim_price_rule_events'):owned(table,'account_id')
        elif kind=='backtest':root=owned('personal_backtests')
        elif kind=='experiment':root=owned('research_experiments')
        elif kind=='delivery':
            root=owned('alert_deliveries')
            if root:tables['alert_delivery_events']=self.rows(c,'alert_delivery_events','delivery_id=?',(ident,))
        elif kind=='object':
            root=owned('research_objects')
            if root:
                owned('research_versions','object_id')
                subtype=root[0]['kind']
                if subtype=='journal':owned('journal_observation_history','object_id')
                elif subtype=='alert':owned('alert_state','rule_id');owned('alert_events','rule_id')
                elif subtype=='chat_session':
                    owned('assistant_requests','session_id')
                    tables['assistant_chunks']=self.rows(c,'assistant_chunks','user_id=? AND request_id IN(SELECT id FROM assistant_requests WHERE user_id=? AND session_id=?)',(self.uid,self.uid,ident))
                elif subtype not in ('topic',):return None
        else:raise SyncError('INCOMPATIBLE')
        if not root:return None
        securities=set()
        def find(value):
            if isinstance(value,dict):
                if isinstance(value.get('instrument_id'),str):securities.add(value['instrument_id'])
                for v in value.values():find(v)
            elif isinstance(value,list):
                for v in value:find(v)
        find(tables)
        if securities:
            tables['instruments']=[dict(v) for ident in sorted(securities) for v in c.execute('SELECT id,symbol,market,name,asset_class FROM instruments WHERE id=?',(ident,))]
        return {'tables':tables,'attachments':attachments}

    def project(self,kind,gid,payload,c):
        table=KINDS.get(kind) or ('user_watchlist' if kind=='watch' else 'user_settings' if kind=='preferences' else None)
        if not table:raise SyncError('INCOMPATIBLE')
        ident=self.uid if kind=='preferences' else payload['tables']['user_watchlist'][0]['instrument_id'] if kind=='watch' else self.local_id(table,gid,c)
        tables=payload.get('tables',{});files=payload.get('attachments',{})
        if kind in ('report','backtest') and c.execute('SELECT id FROM '+table+' WHERE id=? AND user_id=?',(ident,self.uid)).fetchone():
            existing=self.export(kind,str(ident),c)
            if existing!={'tables':tables,'attachments':files}:raise SyncError('CORRUPT','不可变归档同标识内容不同，已保留原件并停止应用')
        if kind=='profile':
            current=c.execute('SELECT active,needs_reconnect FROM model_profiles WHERE id=? AND user_id=?',(ident,self.uid)).fetchone()
            if current and current['active'] and not current['needs_reconnect']:
                c.execute('INSERT OR REPLACE INTO sync_profile_descriptions VALUES(?,?,?)',(self.uid,ident,json_text(payload)))
                return str(ident)
        if set(tables)-ALLOWED-{'instruments'} or set(files)-{'markdown','html'}:raise SyncError('CORRUPT')
        if kind=='account':self.validate_account(tables)
        price_rule_ids={r['id'] for r in tables.get('sim_price_rules',[])}
        price_rule_events={e['order_id']:e for e in tables.get('sim_price_rule_events',[]) if e.get('order_id') and e.get('result')=='created'}
        # 所有全局 ID 预分配；外键延迟到整批应用末尾核验。
        for name,rows in tables.items():
            if not isinstance(rows,list) or len(rows)>100000:raise SyncError('CORRUPT')
            for row in rows:
                if name!='instruments' and name not in COMPOSITE and 'id' in row:self.local_id(name,row['id'],c,name in INTEGER)
        for item in tables.get('instruments',[]):
            from backend.instruments import Rules
            if item.get('id')!=item.get('market','')+':'+item.get('symbol',''):raise SyncError('CORRUPT')
            c.execute("INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,'',0,?)",(item['id'],item['symbol'],item['market'],item['name'],item['asset_class'],Rules().model_dump_json()))
        if kind=='account':
            for name in ('sim_price_rule_events','sim_lots','sim_ledger','sim_equity','portfolio_valuation_history','sim_orders','sim_price_rules'):
                c.execute('DELETE FROM '+name+' WHERE user_id=? AND account_id=?',(self.uid,ident))
        order=['user_watchlist','user_settings','model_profiles','model_profile_versions','model_context_limits','report_series','personal_reports','personal_candidates','sim_accounts','sim_price_rules','sim_orders','sim_price_rule_events','sim_lots','sim_ledger','sim_equity','portfolio_valuation_history','personal_backtests','research_objects','research_versions','research_experiments','journal_observation_history','alert_state','alert_events','assistant_requests','assistant_chunks','alert_deliveries','alert_delivery_events']
        for name in order:
            for original in tables.get(name,[]):
                row=dict(original);info=c.execute('PRAGMA table_info('+name+')').fetchall();columns={v['name'] for v in info}
                if set(row)-columns or 'user_id' in row or any(k.lower() in SECRET for k in row):raise SyncError('CORRUPT')
                for key,item in list(row.items()):
                    if name=='alert_deliveries' and key=='event_ids_json':row[key]=json_text([self.local_id('alert_events',v,c) for v in item])
                    elif key=='id' and name not in COMPOSITE:row[key]=self.local_id(name,item,c,name in INTEGER)
                    elif key in REFS and item:row[key]=self.local_id(REFS[key] or ('research_experiments' if name=='research_experiments' else 'personal_reports'),item,c)
                    elif key.endswith('_json') and item is not None:row[key]=json_text(self.nested(item,False,c,name))
                if 'user_id' in columns:row['user_id']=self.uid
                if row.get('client_key'):
                    parts=str(row['client_key']).split(':')
                    event=price_rule_events.get(original['id']) if name=='sim_orders' else None
                    rule=event['price_rule_id'] if event else parts[1] if name=='sim_orders' and len(parts)==4 and parts[0]=='rule' and parts[3]==row['side'] else None
                    if rule in price_rule_ids:
                        # 用接收端规则编号构成原生幂等键，同日重试仍命中原委托。
                        row['client_key']=price_rule_key(self.local_id('sim_price_rules',rule,c),event['bar_date'] if event else parts[2],row['side'])
                    else:row['client_key']='sync:'+str(gid)+':'+row['client_key']
                if name=='user_watchlist':
                    self.gid(name,row['instrument_id'],c)
                    if 'cost_price' in row or 'quantity' in row:
                        from backend.cloud.market import normalize_holding
                        try:normalize_holding(row.get('cost_price'),row.get('quantity'))
                        except ValueError:raise SyncError('CORRUPT','自选持仓价或数量无效') from None
                if name=='user_settings':
                    row['user_id']=self.uid;current=c.execute('SELECT payload_json,default_profile_id FROM user_settings WHERE user_id=?',(self.uid,)).fetchone()
                    saved=json.loads(current['payload_json']) if current else {};received=json.loads(row['payload_json'])
                    for flag in ('automatic_ai','morning_enabled','closing_enabled','weekly_enabled'):received.pop(flag,None)
                    saved.update(received);row['payload_json']=json_text(saved);row['default_profile_id']=current['default_profile_id'] if current else None
                if name=='research_objects' and row.get('kind')=='alert':
                    previous=c.execute('SELECT payload_json,status FROM research_objects WHERE id=? AND user_id=?',(row['id'],self.uid)).fetchone()
                    data=json.loads(row['payload_json']);local_flags=json.loads(previous['payload_json']) if previous else {}
                    data['enabled']=bool(local_flags.get('enabled',False));data['email']=bool(local_flags.get('email',False));row['payload_json']=json_text(data)
                    row['status']=previous['status'] if previous else 'disabled'
                if name=='model_profiles':row.update(active=0,needs_reconnect=1)
                if name=='model_profile_versions':row.update(credential_id=None,allow_summary=0,allow_trades=0)
                if name=='report_series':row['kind']='synced:'+row['kind']+':'+original['id']
                if name=='personal_reports':
                    for label in ('markdown','html'):
                        file=files.get(label)
                        if not file:raise SyncError('DEPENDENCY')
                        try:data=base64.b64decode(file['data'],validate=True)
                        except Exception:raise SyncError('CORRUPT') from None
                        if digest(data)!=file['sha256']:raise SyncError('CORRUPT')
                        target=self.store.cfg.data/'reports'/self.uid/(row['id']+('.md' if label=='markdown' else '.html'))
                        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(data);row[label+'_path']=str(target.relative_to(self.store.cfg.data))
                if name in ('assistant_requests','research_experiments'):
                    row['job_id']=None
                    if row.get('status') in ('queued','running','validating'):row.update(status='interrupted',error_code='SYNC_RECEIVED_INTERRUPTED')
                if name=='alert_events' and row.get('state') in ('pending','pending_dnd','ready'):row['state']='sync_received'
                if name=='alert_deliveries' and row.get('state') in ('queued','retry','sending'):row['state']='sync_received'
                primary=[v['name'] for v in sorted(info,key=lambda x:x['pk']) if v['pk']]
                fields=list(row);updates=[k for k in fields if k not in primary]
                sql='INSERT INTO '+name+'('+','.join(fields)+') VALUES('+','.join('?' for _ in fields)+') ON CONFLICT('+','.join(primary)+') DO '+('UPDATE SET '+','.join(k+'=excluded.'+k for k in updates) if updates else 'NOTHING')
                c.execute(sql,tuple(row[k] for k in fields))
                if name=='sim_orders' and row['status']=='pending':c.execute('INSERT OR IGNORE INTO sync_inert_orders VALUES(?,?)',(row['id'],self.uid))
        return str(ident)

    def remove(self,kind,ident,c):
        table=KINDS.get(kind)
        if kind=='watch':c.execute('DELETE FROM user_watchlist WHERE user_id=? AND instrument_id=?',(self.uid,ident))
        elif kind=='preferences':return
        elif table:
            if table=='model_profiles':c.execute('UPDATE model_profile_versions SET credential_id=NULL WHERE profile_id IN(SELECT id FROM model_profiles WHERE id=? AND user_id=?)',(ident,self.uid))
            c.execute('DELETE FROM '+table+' WHERE id=? AND user_id=?',(ident,self.uid))

    @staticmethod
    def validate_account(tables):
        try:
            root=tables['sim_accounts']
            if len(root)!=1:raise ValueError()
            cash=Decimal(root[0]['cash']);initial=Decimal(root[0]['initial_cash']);balance=Decimal(0)
            if not cash.is_finite() or cash<0 or not initial.is_finite() or initial<=0:raise ValueError()
            for row in tables.get('sim_ledger',[]):
                balance+=Decimal(row['delta'])
                if balance!=Decimal(row['balance']):raise ValueError()
            if balance!=cash:raise ValueError()
            if any(type(row['quantity']) is not int or row['quantity']<0 for row in tables.get('sim_lots',[])):raise ValueError()
            orders=[r['id'] for r in tables.get('sim_orders',[])]
            if len(orders)!=len(set(orders)):raise ValueError()
            root_id=root[0]['id']
            rules=tables.get('sim_price_rules',[])
            rule_ids={r['id'] for r in rules}
            kinds={'close_at_or_below','close_at_or_above','close_stop','ma_forward'}
            if any(r.get('account_id')!=root_id or r.get('kind') not in kinds or r.get('status') not in {'active','paused','stopped'} for r in rules):raise ValueError()
            if len(rule_ids)!=len(rules):raise ValueError()
            if any(e.get('account_id')!=root_id or e.get('price_rule_id') not in rule_ids or e.get('result') not in {'created','skipped','rejected'} for e in tables.get('sim_price_rule_events',[])):raise ValueError()
        except Exception:raise SyncError('CORRUPT','模拟账户账本或持仓校验失败，未应用半套状态') from None
