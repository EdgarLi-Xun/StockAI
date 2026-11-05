"""Bounded, read-only lookups of the caller's saved research and shared securities."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import market
from .auth import AuthError

ResearchKind = Literal['report', 'instrument', 'backtest', 'account', 'experiment']
TABLES = {'report': 'personal_reports', 'backtest': 'personal_backtests',
          'account': 'sim_accounts', 'experiment': 'research_experiments'}
MAX_LOOKUP_ROUNDS = 3


class DataQuery(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['search', 'read', 'latest']
    kind: ResearchKind | Literal['preferences', 'watchlist']
    id: str | None = Field(default=None, min_length=1, max_length=100)
    query: str = Field(default='', max_length=100)
    limit: int = Field(default=1, ge=1, le=5, strict=True)
    offset: int = Field(default=0, ge=0, le=10000, strict=True)
    instrument_id: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode='after')
    def valid_operation(self):
        if self.kind == 'preferences':
            if self.action != 'read' or self.id or self.query or self.offset or self.instrument_id or self.limit != 1:
                raise ValueError('preferences only supports reading the current user configuration')
            return self
        if self.kind == 'watchlist':
            if self.action != 'read' or self.id or self.query or self.offset or self.instrument_id or self.limit != 1:
                raise ValueError('watchlist only supports reading the current user recorded holdings')
            return self
        if self.action == 'read':
            if not self.id or self.query or self.offset or self.limit != 1:
                raise ValueError('read requires an ID and no search pagination')
        elif self.id or (self.action == 'latest' and (self.limit > 2 or self.offset)):
            raise ValueError('latest reads at most two results; search does not accept an ID')
        if self.instrument_id and self.kind != 'report':
            raise ValueError('instrument_id filters report facts only')
        return self


class Lookup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    queries: list[DataQuery] = Field(min_length=1, max_length=3)


def check_contexts(store, uid, contexts):
    """Resolve every ID within the caller's scope before projecting any facts."""
    for ctx in contexts:
        if ctx['kind'] == 'instrument':
            market.instrument(store, ctx['id'])
        else:
            table = TABLES[ctx['kind']]
            if not store.one(f'SELECT id FROM {table} WHERE id=? AND user_id=?', (ctx['id'], uid)):
                raise AuthError('NOT_FOUND', '资料不存在', 404)


def check_permission(query, fixed):
    if query.kind == 'account' and not (fixed.get('allow_summary') or fixed.get('allow_trades')):
        raise AuthError('SUMMARY_PERMISSION_REQUIRED', '此模型配置尚未授权模拟资金与持仓汇总', 403)


def search(store, uid, query: DataQuery):
    """Search allowlisted metadata with bound parameters, never raw result JSON or notes."""
    term = query.query.strip().lower()
    if query.kind == 'instrument':
        sql = ('SELECT id,name,symbol,market,asset_class,verified_at AS data_time FROM instruments '
               'WHERE (?=\'\' OR instr(lower(id),?) OR instr(lower(name),?) OR instr(lower(symbol),?)) '
               'ORDER BY id LIMIT ? OFFSET ?')
        params = (term, term, term, term, query.limit + 1, query.offset)
    else:
        table = TABLES[query.kind]
        if query.kind == 'report':
            columns = 'id,version,mode,report_date,generated_at AS data_time'
            matches = ['id', 'report_date', 'mode']
            order = 'generated_at DESC,version DESC,id DESC'
            # Search the archived instrument IDs/names, without exposing other JSON fields.
            extra = (" OR EXISTS(SELECT 1 FROM json_each(payload_json,'$.candidates') c "
                     "WHERE instr(lower(json_extract(c.value,'$.instrument_id')),?) "
                     "OR instr(lower(json_extract(c.value,'$.name')),?))"
                     " OR EXISTS(SELECT 1 FROM json_each(payload_json,'$.excluded') e "
                     "WHERE instr(lower(json_extract(e.value,'$.instrument_id')),?) "
                     "OR instr(lower(json_extract(e.value,'$.name')),?))")
        elif query.kind == 'account':
            # A user's free-form account name may be searched, but is never sent to a model.
            columns = 'id,created_at AS data_time'
            matches = ['id', 'name']; order = 'created_at DESC,id DESC'; extra = ''
        else:
            columns = "id,created_at AS data_time,json_extract(params_json,'$.instrument_id') AS instrument_id"
            matches = ['id', 'created_at', "json_extract(params_json,'$.instrument_id')"]
            if query.kind == 'experiment':
                columns += ',status,finished_at,error_code'; matches += ['status']
            order = 'created_at DESC,id DESC'; extra = ''
        clause = ' OR '.join('instr(lower(' + field + '),?)' for field in matches)
        sql = f'SELECT {columns} FROM {table} WHERE user_id=? AND (?=\'\' OR {clause}{extra}) ORDER BY {order} LIMIT ? OFFSET ?'
        params = (uid, term, *([term] * (len(matches) + (4 if extra else 0))), query.limit + 1, query.offset)
    rows = store.query(sql, params)
    return {'items': [{'kind': query.kind, **row} for row in rows[:query.limit]],
            'has_more': len(rows) > query.limit, 'offset': query.offset,
            'order': 'instrument_id' if query.kind == 'instrument' else 'newest_first'}


def compact_refs(refs, retrieved):
    """Keep automatic report reads bounded; preserve full cards for explicit selections."""
    for ref in refs:
        if ref['kind'] == 'comparison' and any(r['kind'] == 'report' for r in retrieved):
            omitted = {}
            for field in ('added', 'removed', 'changed', 'excluded_changes'):
                rows = ref['facts'].get(field, [])
                if len(rows) > 5:
                    omitted[field] = len(rows) - 5
                    ref['facts'][field] = rows[:5]
            if omitted:
                ref['omitted_items'] = omitted
            continue
        selector = next((r for r in retrieved if r['kind'] == ref['kind'] and r['id'] == ref.get('object_id')), None)
        if not selector or ref['kind'] != 'report':
            continue
        facts = ref['facts']; instrument = selector.get('instrument_id'); omitted = {}
        # Full watchlists and unrelated market panels are not needed to explain archived results.
        facts.pop('watchlist', None); facts.pop('market_summary', None)
        for field in ('candidates', 'excluded', 'sources', 'budget_calculations'):
            rows = facts.get(field, [])
            if instrument:
                rows = [r for r in rows if r.get('instrument_id') == instrument]
            limit = 6 if field == 'sources' else 5
            if len(rows) > limit:
                omitted[field] = len(rows) - limit
            facts[field] = rows[:limit]
        if instrument:
            ref['filtered_instrument_id'] = instrument
        if omitted:
            ref['omitted_items'] = omitted
    return refs
