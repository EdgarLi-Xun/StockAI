# 以增量建表方式扩展研究对象、提醒、观察、实验和助手会话，保留既有归档。
"""Additive public-workspace upgrade; existing archives are never rewritten."""
# 研究对象与修订表分开保存，保留判断、主题、会话的每次已提交版本。
# 提醒状态、事件和渠道投递分表管理，唯一约束用于去重及恢复。
# 观察、估值和实验保留事实指纹；助手请求及片段以会话和用户共同限定归属。
SCHEMA="""
CREATE TABLE IF NOT EXISTS model_context_limits(
 profile_id TEXT NOT NULL,version INTEGER NOT NULL,context_window INTEGER NOT NULL,output_limit INTEGER NOT NULL,
 PRIMARY KEY(profile_id,version),FOREIGN KEY(profile_id,version) REFERENCES model_profile_versions(profile_id,version) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS research_objects(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,kind TEXT NOT NULL,
 version INTEGER NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,payload_json TEXT NOT NULL,
 client_key TEXT,UNIQUE(id,user_id),UNIQUE(user_id,kind,client_key));
CREATE INDEX IF NOT EXISTS ix_research_list ON research_objects(user_id,kind,status,updated_at DESC);
CREATE TABLE IF NOT EXISTS research_versions(
 object_id TEXT NOT NULL,user_id TEXT NOT NULL,version INTEGER NOT NULL,created_at TEXT NOT NULL,payload_json TEXT NOT NULL,
 PRIMARY KEY(object_id,version),FOREIGN KEY(object_id,user_id) REFERENCES research_objects(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS alert_state(
 rule_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,rule_version INTEGER NOT NULL,last_valid INTEGER,last_fingerprint TEXT,
 last_trigger TEXT,last_evaluated TEXT,quality TEXT,details_json TEXT,
 FOREIGN KEY(rule_id,user_id) REFERENCES research_objects(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS alert_events(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,rule_id TEXT NOT NULL,rule_version INTEGER NOT NULL,event_key TEXT NOT NULL UNIQUE,
 triggered_at TEXT NOT NULL,state TEXT NOT NULL,facts_json TEXT NOT NULL,
 FOREIGN KEY(rule_id,user_id) REFERENCES research_objects(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS alert_deliveries(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,event_ids_json TEXT NOT NULL,
 channel TEXT NOT NULL,state TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,next_at TEXT NOT NULL,created_at TEXT NOT NULL,
 delivered_at TEXT,error_code TEXT,payload_json TEXT);
CREATE TABLE IF NOT EXISTS alert_delivery_events(event_id TEXT NOT NULL REFERENCES alert_events(id) ON DELETE CASCADE,
 channel TEXT NOT NULL,delivery_id TEXT NOT NULL REFERENCES alert_deliveries(id) ON DELETE CASCADE,PRIMARY KEY(event_id,channel));
CREATE TABLE IF NOT EXISTS journal_observations(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,object_id TEXT NOT NULL,judgement_version INTEGER NOT NULL,calculation_version INTEGER NOT NULL,
 created_at TEXT NOT NULL,data_hash TEXT NOT NULL,payload_json TEXT NOT NULL,UNIQUE(object_id,judgement_version,data_hash),
 FOREIGN KEY(object_id,user_id) REFERENCES research_objects(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS portfolio_valuations(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,account_id TEXT NOT NULL,created_at TEXT NOT NULL,data_hash TEXT NOT NULL,payload_json TEXT NOT NULL,
 UNIQUE(account_id,data_hash),FOREIGN KEY(account_id,user_id) REFERENCES sim_accounts(id,user_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS journal_observation_history(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,object_id TEXT NOT NULL,judgement_version INTEGER NOT NULL,calculation_version INTEGER NOT NULL,
 created_at TEXT NOT NULL,data_hash TEXT NOT NULL,payload_json TEXT NOT NULL,UNIQUE(object_id,judgement_version,calculation_version),
 FOREIGN KEY(object_id,user_id) REFERENCES research_objects(id,user_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS ix_journal_history ON journal_observation_history(user_id,object_id,judgement_version,calculation_version);
CREATE TABLE IF NOT EXISTS portfolio_valuation_history(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,account_id TEXT NOT NULL,revision INTEGER NOT NULL,created_at TEXT NOT NULL,
 data_hash TEXT NOT NULL,payload_json TEXT NOT NULL,origin TEXT NOT NULL,UNIQUE(account_id,revision),
 FOREIGN KEY(account_id,user_id) REFERENCES sim_accounts(id,user_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS ix_valuation_history ON portfolio_valuation_history(user_id,account_id,revision);
CREATE TABLE IF NOT EXISTS industry_classifications(
 instrument_id TEXT PRIMARY KEY REFERENCES instruments(id),industry TEXT NOT NULL,source_url TEXT NOT NULL,as_of TEXT NOT NULL,verified_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS market_sessions(
 market TEXT NOT NULL,day TEXT NOT NULL,open_at TEXT NOT NULL,close_at TEXT NOT NULL,source_url TEXT NOT NULL,fetched_at TEXT NOT NULL,
 PRIMARY KEY(market,day));
CREATE TABLE IF NOT EXISTS research_experiments(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,topic_id TEXT NOT NULL,client_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL,status TEXT NOT NULL,attempt_no INTEGER,result_id TEXT,job_id TEXT,params_json TEXT NOT NULL,
 benchmark_json TEXT NOT NULL,snapshot_json TEXT,created_at TEXT NOT NULL,started_at TEXT,finished_at TEXT,error_code TEXT,parent_id TEXT,
 UNIQUE(user_id,client_key),FOREIGN KEY(topic_id,user_id) REFERENCES research_objects(id,user_id));
CREATE INDEX IF NOT EXISTS ix_experiments ON research_experiments(user_id,created_at DESC);
CREATE TABLE IF NOT EXISTS assistant_requests(
 id TEXT PRIMARY KEY,user_id TEXT NOT NULL,session_id TEXT NOT NULL,client_key TEXT NOT NULL,fingerprint TEXT NOT NULL,
 job_id TEXT,question TEXT NOT NULL,payload_json TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL,updated_at TEXT NOT NULL,
 reply_json TEXT,error_code TEXT,checked_seq INTEGER NOT NULL DEFAULT 0,sensitivity_json TEXT NOT NULL,regenerate_of TEXT,
 UNIQUE(user_id,client_key),UNIQUE(id,user_id),FOREIGN KEY(session_id,user_id) REFERENCES research_objects(id,user_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS ix_assistant_history ON assistant_requests(user_id,session_id,created_at);
CREATE TABLE IF NOT EXISTS assistant_chunks(
 request_id TEXT NOT NULL,user_id TEXT NOT NULL,seq INTEGER NOT NULL,payload_json TEXT NOT NULL,created_at TEXT NOT NULL,
 PRIMARY KEY(request_id,seq),FOREIGN KEY(request_id,user_id) REFERENCES assistant_requests(id,user_id) ON DELETE CASCADE);
INSERT OR IGNORE INTO cloud_meta VALUES('shared_research_revision','0');
CREATE TRIGGER IF NOT EXISTS research_bar_insert AFTER INSERT ON bars BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_bar_update AFTER UPDATE ON bars BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_bar_delete AFTER DELETE ON bars BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_instrument_update AFTER UPDATE ON instruments BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_calendar_insert AFTER INSERT ON calendar BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_calendar_update AFTER UPDATE ON calendar BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_calendar_delete AFTER DELETE ON calendar BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_quote_insert AFTER INSERT ON market_quotes BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_quote_update AFTER UPDATE ON market_quotes BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
CREATE TRIGGER IF NOT EXISTS research_quote_delete AFTER DELETE ON market_quotes BEGIN UPDATE cloud_meta SET value=CAST(value AS INTEGER)+1 WHERE key='shared_research_revision'; END;
"""


VERSION=2


def _copy_legacy_history(connection):
    """Copy known snapshots verbatim; the ordinal only orders records that actually exist."""
    connection.execute('INSERT OR IGNORE INTO journal_observation_history SELECT * FROM journal_observations ORDER BY rowid')
    connection.execute("""INSERT OR IGNORE INTO portfolio_valuation_history
        SELECT id,user_id,account_id,ROW_NUMBER() OVER(PARTITION BY account_id ORDER BY rowid),
        created_at,data_hash,payload_json,'schema1' FROM portfolio_valuations ORDER BY rowid""")


def upgrade(connection):
    """The caller commits the copy and version marker together, or rolls both back."""
    current=connection.execute("SELECT value FROM cloud_meta WHERE key='research_schema_version'").fetchone()
    version=int(current[0]) if current else 0
    if version>VERSION:raise RuntimeError('研究数据版本较新，请使用匹配的应用版本')
    if version==VERSION:return
    if not connection.in_transaction:connection.execute('BEGIN IMMEDIATE')
    _copy_legacy_history(connection)
    connection.execute("INSERT INTO cloud_meta VALUES('research_schema_version',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(VERSION),))
