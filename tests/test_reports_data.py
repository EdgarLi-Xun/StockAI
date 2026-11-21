# 验证行情时效、跨市场时间、原子导入、规则降级和报告事实完整性。
import json
from datetime import datetime,timedelta

import pytest

from backend import ai, collectors, db, research
from backend.config import now
from conftest import sample_bars


# 验证过期行情会阻止候选通过时效检查。
def test_ac_f05_old_quote_blocks(bars):
    ctx={'calendar_verified':True,'last_completed_session':'2026-10-30'}
    assert collectors.fresh_reasons(bars[-1],ctx)


# 验证没有可信交易日历时不会猜测交易日或节假日。
def test_calendar_does_not_guess_holidays():
    ctx=collectors.calendar_context()
    assert ctx['is_trading_day'] is None and not ctx['calendar_verified']


# 验证美股日线收盘时间正确处理夏令时。
@pytest.mark.parametrize('date,expected',[('2026-10-30','2026-10-31T04:00:00+08:00'),('2026-01-02','2026-01-03T05:00:00+08:00')])
def test_us_daily_timestamp_respects_dst(date,expected):
    assert collectors.parse_ts(date,daily=True,market='US')==expected


# 验证港股日线采用港股收盘时点。
def test_hk_close_does_not_use_mainland_close():
    assert collectors.parse_ts('2026-10-30',daily=True,market='HK')=='2026-10-30T16:00:00+08:00'


# 验证基金净值只作为观察资料，不混入金银行情或买入候选。
def test_fund_nav_is_observation_and_not_metal_or_candidate(monkeypatch,client):
    from backend import instruments
    monkeypatch.setattr(collectors,'call',lambda *a,**k:[{'净值日期':'2025-01-02','单位净值':1.2345}])
    result=collectors.sync_instrument(instruments.get('FUND:009047'))
    assert result['status']=='success'
    rows=client.get('/api/watchlist').json()
    fund=next(x for x in rows if x['id']=='FUND:009047')
    assert fund['observation_only'] and fund['quote']['price']==1.2345 and fund['budget'] is None
    facts=research.build_facts('full')
    assert facts['metals']==[] and facts['candidates']==[]


# 验证不合法的 OHLC 或其他行情字段会被拒绝。
@pytest.mark.parametrize('change',[{'close':0},{'open':-1},{'adjustment':None},{'source_url':''},{'high':0},{'volume':-1}])
def test_ac_f05_reject_invalid_bars(bars,change):
    bar={**bars[0],**change}
    with pytest.raises((ValueError,KeyError)):
        collectors.save_bars('SH:603126',[bar])
    assert not collectors.bars_for('SH:603126')


# 验证一批行情校验失败时不会部分写入。
def test_import_is_atomic(bars):
    with pytest.raises(ValueError):
        collectors.save_bars('SH:603126',[bars[0],{**bars[1],'close':-1}])
    assert not collectors.bars_for('SH:603126')


# 验证尚未完成的交易日 K 线不会作为完整日线保存。
def test_incomplete_today_bar_not_saved():
    tomorrow=(now()+timedelta(days=1)).date().isoformat()
    rows=[{'日期':tomorrow,'开盘':5,'收盘':5,'最高':5,'最低':5,'成交量':100}]
    assert not collectors.normalize_bars(rows,'test','https://example.com')


# 验证模型离线时仍可生成带明确降级状态的规则报告。
def test_ac_f06_model_disconnected_rule_report(monkeypatch):
    monkeypatch.setattr(ai,'health',lambda:{'available':False,'message':'offline'})
    for mode in ('standard','full'):
        result=research.generate(mode+':test',mode)
        row=db.one('SELECT payload_json FROM reports WHERE id=?',(result['report_id'],))
        report=json.loads(row['payload_json'])
        assert report['candidates']==[] and report['summary']=='今日无合格模拟候选'
        assert report['ai_status']=='fallback' and report['model']=='rules-only'
        assert all(x['price'] is None for x in report['excluded'])
        assert len(report['snapshot_id'])==64


# 验证相同报告逻辑键不会产生重复归档。
def test_ac07_reports_idempotent(monkeypatch):
    monkeypatch.setattr(ai,'health',lambda:{'available':False,'message':'offline'})
    first=research.generate('standard:stable')
    second=research.generate('standard:stable')
    assert first['report_id']==second['report_id']
    assert db.one('SELECT COUNT(*) n FROM reports')['n']==1


# 验证规则筛选最多保留三只合格模拟候选。
def test_ac05_max_three_candidates(valid_item,bars,monkeypatch):
    from backend import instruments
    cfg=db.settings(); cfg.min_daily_amount=0; db.save_settings(cfg)
    ctx={'calendar_verified':True,'last_completed_session':bars[-1]['ts'][:10],'date':now().date().isoformat(),'is_trading_day':False}
    monkeypatch.setattr(collectors,'calendar_context',lambda:ctx)
    monkeypatch.setattr(research.analytics,'score',lambda *a:90)
    for ident in ('SH:603126','SH:601111','SZ:000875','SH:600115'):
        db.execute("UPDATE instruments SET name='测试证券',status='active',risk_flag='clear',verified_at=?,rules_json=?,evidence_json=? WHERE id=?",(valid_item['verified_at'],db.dumps(valid_item['rules']),db.dumps(valid_item['evidence']),ident))
        collectors.save_bars(ident,bars)
    facts=research.build_facts()
    assert len(facts['candidates'])==3
    assert all(c['budget']['total']<=1000 for c in facts['candidates'])


# 验证模型解释不能覆盖原始价格等确定事实。
def test_ai_cannot_change_fact_values():
    facts={'candidates':[],'sources':[],'price':5.25}
    good=ai.ResearchText(summary='资料不足',market_context='未验证',candidates=[],risk_notes=[],missing_data=[],source_ids=[])
    assert ai.validate_output(good,facts)
    with pytest.raises(ValueError):
        ai.validate_output(good.model_copy(update={'summary':'价格为999.99'}),facts)
    with pytest.raises(ValueError):
        ai.validate_output(good.model_copy(update={'summary':'保证盈利'}),facts)


# 验证模型引用未提供来源时校验失败。
def test_ai_unknown_source_rejected():
    text=ai.ResearchText(summary='无候选',market_context='',candidates=[],risk_notes=[],missing_data=[],source_ids=['invented'])
    with pytest.raises(ValueError):
        ai.validate_output(text,{'candidates':[],'sources':[]})


def test_model_cannot_misstate_trading_status_or_borrow_another_security_source():
    facts={'candidates':[{'instrument_id':'SH:603126','source_ids':['s1']}],'sources':[{'id':'s1'},{'id':'s2'}]}
    candidate=ai.Explanation(instrument_id='SH:603126',rationale='短期趋势有支撑。',counterargument='仍需观察。',invalidation='波动加大时重新检查。',source_ids=['s1'])
    text=ai.ResearchText(summary='例行研究',market_context='以存档数据为准。',candidates=[candidate],risk_notes=[],missing_data=[],source_ids=[])
    assert ai.validate_output(text,facts)
    for change in ({'rationale':'公告确认该证券已经停 牌。'},{'source_ids':['s2']}):
        with pytest.raises(ValueError):
            ai.validate_output(text.model_copy(update={'candidates':[candidate.model_copy(update=change)]}),facts)


def test_exchange_delisting_is_sticky_until_newer_official_listing():
    from backend import instruments
    ident='SH:603126'
    row={'公司代码':'603126','公司简称':'原上市名称','暂停上市日期':'2022-01-10'}
    collectors._store_delisting('delist_sh',[row])
    collectors._store_delisting('delist_sh',[row])
    assert instruments.get(ident)['status']=='delisted'
    assert db.one('SELECT count(*) n FROM risk_events WHERE instrument_id=?',(ident,))['n']==1
    as_of='2022-05-01'
    with db.connection() as conn:
        collectors._store_listing(conn,'SH','603126','新上市名称','https://www.sse.com.cn/assortment/stock/list/share/',{'上市日期':as_of})
    collectors._store_delisting('delist_sh',[row])
    item=instruments.get(ident)
    assert item['status']=='unknown' and item['verified_at'] is None
    assert item['name']=='新上市名称' and item['evidence']['exchange_listing_date']==as_of


def test_official_disclosure_refresh_blocks_new_risk_without_clearing_unknown(monkeypatch,valid_item):
    from backend import instruments
    url='https://www.cninfo.com.cn/new/disclosure/detail?announcementId=example'
    other='SH:601111'
    monkeypatch.setattr(collectors,'call',lambda *args,**kwargs:[])
    assert not collectors.announcements(other)
    assert instruments.get(other)['status']=='unknown' and instruments.get(other)['risk_flag']=='unknown'
    monkeypatch.setattr(collectors,'call',lambda *args,**kwargs:[{'代码':valid_item['symbol'],'公告标题':'关于实施风险警示的公告','公告链接':url}])
    assert len(collectors.announcements(valid_item['id']))==1
    assert not collectors.announcements(valid_item['id'])
    item=instruments.get(valid_item['id'])
    assert item['risk_flag']=='warning' and item['evidence']['disclosure_check']['url'].startswith('https://www.cninfo.com.cn/')
    assert instruments.risk_reasons(item)


def test_disclosure_timeout_keeps_coverage_unknown(monkeypatch):
    from subprocess import TimeoutExpired
    from backend import instruments

    def timeout(*args,**kwargs):
        raise TimeoutExpired('isolated-announcement-collector',25)

    item=instruments.get('SH:603126')
    monkeypatch.setattr(collectors,'call',timeout)
    errors=collectors.sync_announcements([item])
    assert errors[0]['instrument_id']==item['id']
    current=instruments.get(item['id'])
    assert 'disclosure_check' not in current['evidence']
    assert current["evidence"]["disclosure_failure"]["status"] == "unavailable"
    assert current['status']=='unknown' and current['risk_flag']=='unknown'


def test_unread_official_notice_requires_a_fresh_manual_or_source_check(monkeypatch, valid_item):
    from datetime import datetime, timedelta
    from backend import config, instruments

    at = datetime.fromisoformat(valid_item["verified_at"]) + timedelta(seconds=2)
    for module in (collectors, config, instruments):
        monkeypatch.setattr(module, "now", lambda: at)
    row = {"代码": valid_item["symbol"], "公告标题": "关于公司股票复牌的公告", "公告时间": at.isoformat(),
           "公告链接": "https://www.cninfo.com.cn/new/disclosure/detail?announcementId=1234567801", "status_read_error": True}
    monkeypatch.setattr(collectors, "call", lambda *a, **k: [row])
    assert collectors.sync_announcements([valid_item])
    item = instruments.get(valid_item["id"])
    assert item["risk_flag"] == "clear"
    assert item["evidence"]["disclosure_check"]["unread_status_notices"] == 1
    assert instruments.DISCLOSURE_UNVERIFIED in instruments.risk_reasons(item, at)
    at += timedelta(seconds=2)
    checked = instruments.verify(item["id"], {field: item[field] for field in ("name", "status", "risk_flag", "rules", "evidence")})
    assert checked["evidence"]["disclosure_failure"]["status"] == "partial"
    assert instruments.DISCLOSURE_UNVERIFIED not in instruments.risk_reasons(checked, at)


def test_official_notice_applies_effective_halt_and_resume_without_clearing_risk(monkeypatch, valid_item):
    from datetime import datetime, timedelta
    from backend import config, instruments, trading_status

    code, ident = valid_item["symbol"], valid_item["id"]
    at = datetime(2025, 2, 20, 8, tzinfo=config.TZ)
    for module in (collectors, instruments):
        monkeypatch.setattr(module, "now", lambda: at)
    source = "https://static.cninfo.com.cn/finalpage/2025-02-20/1234567800.PDF"
    official_body = f"证券代码：{code}。公司股票将于2025年2月20日（星期四）开市起停牌一天，并于2025年2月21日开市起复牌。"
    events = trading_status.document_events(code, official_body)
    assert [e["status"] for e in events] == ["suspended", "active"]
    assert not trading_status.document_events("600000", official_body)
    assert not trading_status.document_events(code, f"证券代码：{code}。公司股票预计于2025年2月21日开市起复牌。")
    short_clause = f"证券代码：{code}。公司股票将于2025年2月20日停牌1天，2025年2月21日开市起复牌。"
    assert [e["status"] for e in trading_status.document_events(code, short_clause)] == ["suspended", "active"]
    row = {"代码": code, "公告标题": "关于股票风险警示及停复牌的公告", "公告时间": "2025-02-20 07:30:00",
           "公告链接": "https://www.cninfo.com.cn/new/disclosure/detail?announcementId=1234567800",
           "附件链接": source, "trading_events": events}
    mismatch = {**row, "代码": "600000", "公告链接": row["公告链接"] + "01"}
    monkeypatch.setattr(collectors, "call", lambda *_args, **_kw: [mismatch, row])
    assert len(collectors.announcements(ident)) == 1
    pending = instruments.get(ident)
    assert pending["status"] == "active"
    assert "今日已有官方公告安排停牌" in instruments.risk_reasons(pending, at)
    at += timedelta(hours=2)
    assert not collectors.announcements(ident)
    current = instruments.get(ident)
    assert db.one("SELECT status FROM instruments WHERE id=?", (ident,))["status"] == "suspended"
    assert current["risk_flag"] == "warning" and current["verified_at"] == valid_item["verified_at"]
    assert current["evidence"]["exchange_trading_status"]["url"] == source
    assert "停牌" in instruments.risk_reasons(current, at)
    assert len(db.query("SELECT id FROM risk_events WHERE instrument_id=? AND event_type='trading_status'", (ident,))) == 2
    at += timedelta(days=1)
    assert not collectors.announcements(ident)
    resumed = instruments.get(ident)
    assert resumed["status"] == "active" and resumed["risk_flag"] == "warning"
    assert resumed["evidence"]["exchange_trading_status"]["effective_at"] == "2025-02-21T09:30:00+08:00"
    db.execute("UPDATE instruments SET status='delisted' WHERE id=?", (ident,))
    assert instruments.get(ident)["status"] == "delisted"


def test_expired_single_day_notice_leaves_unknown_until_positive_evidence(monkeypatch, valid_item):
    from datetime import datetime, timedelta
    from backend import config, instruments

    at = datetime(2025, 2, 20, 10, tzinfo=config.TZ)
    for module in (collectors, instruments):
        monkeypatch.setattr(module, "now", lambda: at)
    row = {"代码": valid_item["symbol"], "公告标题": "关于公司股票当日临时停牌的公告",
           "公告时间": "2025-02-20 09:55:00", "公告链接": "https://www.szse.cn/disclosure/fixture-one"}
    hypothetical = {**row, "公告标题": "关于公司拟申请停牌的风险提示", "公告链接": "https://www.szse.cn/disclosure/fixture-two"}
    monkeypatch.setattr(collectors, "call", lambda *_args, **_kw: [row, hypothetical])
    collectors.announcements(valid_item["id"])
    item = instruments.get(valid_item["id"])
    assert item["status"] == "suspended"
    assert len(item["evidence"]["trading_status_events"]) == 1
    at += timedelta(days=1)
    assert instruments.get(item["id"])["status"] == "unknown"


def test_startup_status_refresh_checks_listing_and_delisting_before_announcements(monkeypatch, valid_item):
    from datetime import datetime
    from backend import config, instruments

    seen = []
    monkeypatch.setattr(collectors, "now", lambda: datetime(2026, 11, 21, tzinfo=config.TZ))

    def fetch(action, _args=None, **_kwargs):
        seen.append(action)
        return {
            "calendar": [{"trade_date": "2026-12-01"}],
            "master_sh": [{"证券代码": valid_item["symbol"], "证券简称": "核验后的名称", "上市日期": "2007-05-01"}],
            "delist_sh": [{"公司代码": valid_item["symbol"], "公司简称": "核验后的名称", "暂停上市日期": "2025-03-06"}],
            "sse_suspensions": [],
        }[action]

    monkeypatch.setattr(collectors, "call", fetch)
    assert collectors.sync_trading_status([valid_item], refresh_identity=True)["status"] == "success"
    assert seen == ["calendar", "master_sh", "delist_sh", "sse_suspensions"]
    item = instruments.get(valid_item["id"])
    assert item["name"] == "核验后的名称" and item["status"] == "delisted"


def test_macro_fallback_accepts_only_the_verified_monthly_series():
    from backend.collector_worker import BLS_CPI_SERIES,bls_cpi_rows
    info={'status':'REQUEST_SUCCEEDED','Results':{'series':[{'seriesID':BLS_CPI_SERIES,'data':[
        {'year':'2026','period':'M02','value':'104.6'},
        {'year':'2026','period':'M01','value':'103.2'},
        {'year':'2026','period':'M13','value':'104'},
        {'year':'2026','period':'M03','value':'NaN'},
    ]}]}}
    assert [(r['日期'],r['今值']) for r in bls_cpi_rows(info)]==[('2026-01-01',103.2),('2026-02-01',104.6)]
    info['Results']['series'][0]['seriesID']='unrelated-series'
    with pytest.raises(ValueError):bls_cpi_rows(info)


# 验证报告 HTML 导出会转义证券名称等不可信文本。
def test_html_export_escapes_untrusted_text(monkeypatch):
    from backend.config import DATA
    db.execute("UPDATE instruments SET name='<script>alert(1)</script>' WHERE id='SH:603126'")
    monkeypatch.setattr(ai,'health',lambda:{'available':False,'message':'offline'})
    r=research.generate('xss:test')
    path=research.DATA/'reports'/(r['report_id']+'.html')
    assert '<script>' not in path.read_text(encoding='utf-8')
    assert '&lt;script&gt;' in path.read_text(encoding='utf-8')
