"""Additive synchronization schema; no existing historical payload is rewritten."""
SCHEMA="""
CREATE TABLE IF NOT EXISTS sync_bindings(user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,space_id TEXT NOT NULL,device_id TEXT NOT NULL,backend TEXT NOT NULL,version INTEGER NOT NULL,payload_json TEXT NOT NULL,secret_json TEXT NOT NULL,status TEXT NOT NULL,automatic INTEGER NOT NULL DEFAULT 0,updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sync_rows(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,table_name TEXT NOT NULL,local_id TEXT NOT NULL,global_id TEXT NOT NULL,PRIMARY KEY(user_id,table_name,local_id),UNIQUE(user_id,table_name,global_id));
CREATE TABLE IF NOT EXISTS sync_dirty(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,local_id TEXT NOT NULL,revision INTEGER NOT NULL DEFAULT 1,PRIMARY KEY(user_id,kind,local_id));
CREATE TABLE IF NOT EXISTS sync_entities(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,object_id TEXT NOT NULL,local_id TEXT NOT NULL,heads_json TEXT NOT NULL,payload_json TEXT,projection_hash TEXT,deleted INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(user_id,kind,object_id));
CREATE TABLE IF NOT EXISTS sync_operations(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,hash TEXT NOT NULL,op_id TEXT NOT NULL,object_id TEXT NOT NULL,kind TEXT NOT NULL,payload_json TEXT NOT NULL,cipher BLOB NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,PRIMARY KEY(user_id,hash),UNIQUE(user_id,op_id));
CREATE INDEX IF NOT EXISTS ix_sync_pending ON sync_operations(user_id,state,created_at);
CREATE TABLE IF NOT EXISTS sync_conflicts(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,object_id TEXT NOT NULL,base_json TEXT,local_json TEXT,remote_json TEXT NOT NULL,heads_json TEXT NOT NULL,state TEXT NOT NULL,created_at TEXT NOT NULL,resolution_json TEXT);
CREATE INDEX IF NOT EXISTS ix_sync_conflicts ON sync_conflicts(user_id,state);
CREATE TABLE IF NOT EXISTS sync_pending(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,hash TEXT NOT NULL,payload_json TEXT NOT NULL,reason TEXT NOT NULL,PRIMARY KEY(user_id,hash));
CREATE TABLE IF NOT EXISTS sync_inert_orders(order_id TEXT PRIMARY KEY REFERENCES sim_orders(id) ON DELETE CASCADE,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS sync_profile_descriptions(user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,profile_id TEXT NOT NULL,payload_json TEXT NOT NULL,PRIMARY KEY(user_id,profile_id));
"""

# source table -> root object; inherited references use a lookup in the same transaction.
ROOTS={
 'user_watchlist':('watch','instrument_id'), 'user_settings':('preferences','user_id'),
 'model_profiles':('profile','id'),'model_profile_versions':('profile','profile_id'),
 'personal_reports':('report','id'),'personal_candidates':('report','report_id'),
 'sim_accounts':('account','id'),'sim_orders':('account','account_id'),'sim_lots':('account','account_id'),'sim_ledger':('account','account_id'),'sim_equity':('account','account_id'),'portfolio_valuation_history':('account','account_id'),
 'sim_price_rules':('account','account_id'),'sim_price_rule_events':('account','account_id'),
 'personal_backtests':('backtest','id'),'research_experiments':('experiment','id'),
 'research_objects':('object','id'),'research_versions':('object','object_id'),
 'journal_observation_history':('object','object_id'),'alert_events':('object','rule_id'),
 'alert_state':('object','rule_id'),
 'assistant_requests':('object','session_id'),
 'alert_deliveries':('delivery','id'),'alert_delivery_events':('delivery','delivery_id'),
}


def install(c):
    c.executescript(SCHEMA)
    for table,(kind,column) in ROOTS.items():
        for action in ('INSERT','UPDATE','DELETE'):
            row='OLD' if action=='DELETE' else 'NEW'
            owner=f'(SELECT user_id FROM model_profiles WHERE id={row}.profile_id)' if table=='model_profile_versions' else f'(SELECT user_id FROM alert_deliveries WHERE id={row}.delivery_id)' if table=='alert_delivery_events' else f'{row}.user_id'
            c.execute(f"""CREATE TRIGGER IF NOT EXISTS sync_dirty_{table}_{action.lower()} AFTER {action} ON {table} BEGIN
                INSERT INTO sync_dirty(user_id,kind,local_id) SELECT {owner},'{kind}',CAST({row}.{column} AS TEXT) WHERE EXISTS(SELECT 1 FROM users WHERE id={owner})
                ON CONFLICT(user_id,kind,local_id) DO UPDATE SET revision=revision+1; END""")
    c.execute("INSERT OR IGNORE INTO cloud_meta VALUES('sync_schema_version','1')")


def protect(c):
    # 显式采用新协议后才启用；旧程序没有此函数时，SQLite 在写入前拒绝执行。
    for table in ROOTS:
        for action in ('INSERT','UPDATE','DELETE'):
            c.execute(f"CREATE TRIGGER IF NOT EXISTS stockai_capability_{table}_{action.lower()} BEFORE {action} ON {table} BEGIN SELECT CASE WHEN stockai_write_capability()<3 THEN RAISE(ABORT,'StockAI schema requires capability 3') END; END")
    c.execute("INSERT OR REPLACE INTO cloud_meta VALUES('minimum_write_capability','3')")
