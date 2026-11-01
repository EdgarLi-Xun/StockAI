import json
from datetime import datetime,timedelta

import pytest

from backend import ai, collectors, db, research
from backend.config import now
from conftest import sample_bars


def test_ac_f05_old_quote_blocks(bars):
    ctx={'calendar_verified':True,'last_completed_session':'2026-10-30'}
    assert collectors.fresh_reasons(bars[-1],ctx)


def test_calendar_does_not_guess_holidays():
    ctx=collectors.calendar_context()
    assert ctx['is_trading_day'] is None and not ctx['calendar_verified']


@pytest.mark.parametrize('date,expected',[('2026-10-30','2026-10-31T04:00:00+08:00'),('2026-01-02','2026-01-03T05:00:00+08:00')])
def test_us_daily_timestamp_respects_dst(date,expected):
    assert collectors.parse_ts(date,daily=True,market='US')==expected


def test_hk_close_does_not_use_mainland_close():
    assert collectors.parse_ts('2026-10-30',daily=True,market='HK')=='2026-10-30T16:00:00+08:00'


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


@pytest.mark.parametrize('change',[{'close':0},{'open':-1},{'adjustment':None},{'source_url':''},{'high':0},{'volume':-1}])
def test_ac_f05_reject_invalid_bars(bars,change):
    bar={**bars[0],**change}
    with pytest.raises((ValueError,KeyError)):
        collectors.save_bars('SH:603126',[bar])
    assert not collectors.bars_for('SH:603126')


def test_import_is_atomic(bars):
    with pytest.raises(ValueError):
        collectors.save_bars('SH:603126',[bars[0],{**bars[1],'close':-1}])
    assert not collectors.bars_for('SH:603126')


def test_incomplete_today_bar_not_saved():
    tomorrow=(now()+timedelta(days=1)).date().isoformat()
    rows=[{'日期':tomorrow,'开盘':5,'收盘':5,'最高':5,'最低':5,'成交量':100}]
    assert not collectors.normalize_bars(rows,'test','https://example.com')


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


def test_ac07_reports_idempotent(monkeypatch):
    monkeypatch.setattr(ai,'health',lambda:{'available':False,'message':'offline'})
    first=research.generate('standard:stable')
    second=research.generate('standard:stable')
    assert first['report_id']==second['report_id']
    assert db.one('SELECT COUNT(*) n FROM reports')['n']==1


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


def test_ai_cannot_change_fact_values():
    facts={'candidates':[],'sources':[],'price':5.25}
    good=ai.ResearchText(summary='资料不足',market_context='未验证',candidates=[],risk_notes=[],missing_data=[],source_ids=[])
    assert ai.validate_output(good,facts)
    with pytest.raises(ValueError):
        ai.validate_output(good.model_copy(update={'summary':'价格为999.99'}),facts)
    with pytest.raises(ValueError):
        ai.validate_output(good.model_copy(update={'summary':'保证盈利'}),facts)


def test_ai_unknown_source_rejected():
    text=ai.ResearchText(summary='无候选',market_context='',candidates=[],risk_notes=[],missing_data=[],source_ids=['invented'])
    with pytest.raises(ValueError):
        ai.validate_output(text,{'candidates':[],'sources':[]})


def test_html_export_escapes_untrusted_text(monkeypatch):
    from backend.config import DATA
    db.execute("UPDATE instruments SET name='<script>alert(1)</script>' WHERE id='SH:603126'")
    monkeypatch.setattr(ai,'health',lambda:{'available':False,'message':'offline'})
    r=research.generate('xss:test')
    path=research.DATA/'reports'/(r['report_id']+'.html')
    assert '<script>' not in path.read_text(encoding='utf-8')
    assert '&lt;script&gt;' in path.read_text(encoding='utf-8')
