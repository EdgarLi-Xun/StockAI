"""Reviewable project actions. Inference can propose; only a manual API call can apply."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictFloat, StrictInt, StrictStr

from backend.instruments import InstrumentInput
from . import market
from .auth import AuthError
from .preferences import Preferences, get_preferences
from .research import fingerprint
from .store import json_text, stamp

Setting = Literal['budget', 'min_daily_amount', 'max_risk_age_hours', 'morning_enabled',
                  'closing_enabled', 'weekly_enabled', 'automatic_ai', 'morning_time',
                  'closing_time', 'weekly_time', 'fees.stock_commission', 'fees.stock_minimum',
                  'fees.etf_commission', 'fees.etf_minimum', 'fees.stock_transfer',
                  'fees.stock_stamp_sell', 'fees.slippage_bps']
LABELS = {'budget': '单只证券预算（元）', 'min_daily_amount': '最低日成交额（元）',
          'max_risk_age_hours': '风险核验有效期（小时）', 'morning_enabled': '标准早报计划',
          'closing_enabled': '收盘跟踪计划', 'weekly_enabled': '深度复盘计划',
          'automatic_ai': '自动调用个人模型', 'morning_time': '早报时间',
          'closing_time': '收盘跟踪时间', 'weekly_time': '复盘时间',
          'fees.stock_commission': '股票佣金率', 'fees.stock_minimum': '股票最低佣金（元）',
          'fees.etf_commission': 'ETF 佣金率', 'fees.etf_minimum': 'ETF 最低佣金（元）',
          'fees.stock_transfer': '股票过户费率', 'fees.stock_stamp_sell': '股票卖出印花税率',
          'fees.slippage_bps': '模拟滑点（基点）'}
ACTION_INTENT = re.compile(r'修改|调整|更改|改成|改为|设为|设置|添加|加入|新增|加到|加进|加一下|放入|放进|放到|记入|记到|录入|开启|关闭|启用|停用|持仓价|持仓价格|持仓数量')
HOLDING_GAP = re.compile(r'持仓(?:数据|价格|价|数量|记录)?.{0,12}(?:还是没有|仍然没有|没有|还未|还没|仍未|没|未).{0,8}(?:添加进去|加上|写入|保存|记录|填上|填进去|添加|进去)')
HOLDING_RETRY = re.compile(r'^(?:还是|仍然|依然|依旧)(?:没有|没|未)(?:添加|加上|写入|保存)(?:进去|上|了)?[啊呀吧呢。！!？?\s]*$')
TUTORIAL = re.compile(r'投资平台|登录您|以下步骤|帮助文档|联系客服')


class SettingChange(BaseModel):
    model_config = ConfigDict(extra='forbid')
    field: Setting
    value: StrictBool | StrictInt | StrictFloat | StrictStr


class PreferenceProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['preferences']
    changes: list[SettingChange] = Field(min_length=1, max_length=10)


class StockProposal(BaseModel):
    model_config = ConfigDict(extra='forbid')
    kind: Literal['add_stock']
    market: Literal['SH', 'SZ', 'HK', 'US']
    symbol: str = Field(min_length=1, max_length=24)
    group_name: str = Field(default='我的自选', min_length=1, max_length=40)
    cost_price: StrictFloat | StrictInt | StrictStr | None = None
    quantity: StrictInt | None = None


class ApplyInput(BaseModel):
    model_config = ConfigDict(extra='forbid')
    confirm: Literal[True]


def holding_followup(question):
    """用户在说持仓没写进去，而不是在问怎么操作。"""
    text = question.strip()
    return bool(HOLDING_GAP.search(text) or HOLDING_RETRY.fullmatch(text))


def stated_holding(text):
    """从用户原话提取成对的代码、持仓价和数量；缺任一项就不猜。"""
    symbol = re.search(r'(?<!\d)(\d{6})(?!\d)', text)
    cost = re.search(r'持仓价(?:格)?\s*(?:改为|设为|改成|调为|是|为)?\s*(\d+(?:\.\d{1,4})?)', text)
    quantity = re.search(r'持仓数量\s*(?:改为|设为|改成|调为|是|为)?\s*(\d{1,9})', text)
    if not quantity:
        quantity = re.search(r'持仓\s*(?:改为|设为|改成|调为|是|为)?\s*(\d{1,9})\s*股', text)
    if not symbol or not cost or not quantity:
        return None
    return {'kind': 'add_stock', 'market': 'SH', 'symbol': symbol.group(1), 'group_name': '我的自选',
            'cost_price': cost.group(1), 'quantity': int(quantity.group(1))}


def _complete_holding(item):
    quantity = item.get('quantity')
    return item.get('kind') == 'add_stock' and item.get('symbol') and item.get('cost_price') not in (None, '') and isinstance(quantity, int) and not isinstance(quantity, bool) and quantity > 0


def _proposal(item):
    return {'kind': 'add_stock', 'market': item.get('market') or 'SH', 'symbol': str(item['symbol']),
            'group_name': item.get('group_name') or '我的自选', 'cost_price': item['cost_price'], 'quantity': int(item['quantity'])}


def _saved(store, uid, proposal):
    value = stock_input(proposal)
    ident = value.market + ':' + value.symbol
    row = store.one('SELECT cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id=?', (uid, ident))
    if not row or row['cost_price'] in (None, ''):
        return False
    cost, quantity = market.normalize_holding(proposal.get('cost_price'), proposal.get('quantity'))
    return row['cost_price'] == cost and row['quantity'] == quantity


def recent_holdings(store, uid, session_id):
    """按时间从新到旧找回本会话说过的持仓，并标出是否已写入自选。"""
    rows = store.query("SELECT question,reply_json FROM assistant_requests WHERE user_id=? AND session_id=? AND status='success' ORDER BY created_at DESC LIMIT 10", (uid, session_id))
    found, seen = [], set()
    for row in rows:
        reply = json.loads(row['reply_json'] or '{}')
        items = [item for item in reply.get('actions') or [] if _complete_holding(item)]
        stated = stated_holding(row['question'])
        if stated:
            items.append(stated)
        for item in items:
            symbol = str(item['symbol'])
            if symbol in seen:
                continue
            proposal = _proposal(item)
            try:
                saved = _saved(store, uid, proposal)
            except ValueError:
                continue
            seen.add(symbol)
            found.append((proposal, saved))
            if len(found) == 3:
                return found
    return found


def pending_proposals(store, uid, session_id):
    return [proposal for proposal, saved in recent_holdings(store, uid, session_id) if not saved]


def action_history_note(reply):
    lines = []
    for action in reply.get('actions') or []:
        if action.get('kind') != 'add_stock':
            continue
        state = '已应用' if action.get('applied_at') else '未应用'
        lines.append('方案 ' + str(action.get('symbol')) + ' 持仓价' + str(action.get('cost_price')) + ' 数量' + str(action.get('quantity')) + ' ' + state)
    return '\n'.join(lines)


def restore_unsaved_holding(store, uid, session_id, question, answer):
    """持仓没写进去时，用本会话里已核对过的价和数量重做方案，不沿用操作步骤。"""
    if not holding_followup(question):
        return answer
    records = recent_holdings(store, uid, session_id)
    pending = [proposal for proposal, saved in records if not saved]
    saved = [proposal for proposal, saved in records if saved]
    if pending:
        try:
            actions = prepare(store, uid, pending)
        except ValueError:
            actions = []
        if actions:
            return {**answer, 'actions': actions, 'citations': [], 'answer': '持仓还没写入自选。请核对下面的方案，点击按钮后才会保存。'}
    kept = [item for item in answer.get('actions') or [] if _complete_holding(item)]
    if kept:
        if TUTORIAL.search(answer.get('answer') or ''):
            return {**answer, 'actions': kept, 'citations': [], 'answer': '持仓还没写入自选。请核对下面的方案，点击按钮后才会保存。'}
        return answer
    if saved:
        text = '、'.join(item['symbol'] + ' 持仓价 ' + str(item['cost_price']) + '，数量 ' + str(item['quantity']) for item in saved)
        return {**answer, 'actions': [], 'citations': [], 'answer': '这组持仓已经在自选里：' + text + '。可以在自选列表查看持仓价和数量。'}
    return {**answer, 'actions': [], 'citations': [], 'answer': '持仓价和持仓数量要成对填写后才能写入。请直接说股票代码、持仓价和持仓数量，我会准备方案，你点击后才会保存。'}


def configuration(store, uid):
    data = get_preferences(store, uid).model_dump()
    data['fees'] = {key: value for key, value in data['fees'].items() if 'fees.' + key in LABELS}
    return data


def changed_preferences(before, changes):
    after = json.loads(json_text(before)); seen = set()
    for change in changes:
        field, value = change['field'], change['value']
        if field not in LABELS or field in seen:
            raise ValueError('配置字段不在允许范围内或重复')
        seen.add(field)
        if field.startswith('fees.'):
            after['fees'][field.split('.')[1]] = value
        else:
            after[field] = value
    return Preferences.model_validate(after, strict=True).model_dump()


def stock_input(action):
    market, symbol = action['market'], str(action['symbol']).strip()
    if re.fullmatch(r'\d{6}', symbol):
        if symbol.startswith('6'):
            market = 'SH'
        elif symbol.startswith(('0', '3')):
            market = 'SZ'
    value = InstrumentInput(market=market, symbol=symbol, group_name=action['group_name'])
    if value.market in {'SH', 'SZ'} and not re.fullmatch(r'\d{6}', value.symbol):
        raise ValueError('境内股票代码须为六位数字')
    return value


def prepare(store, uid, proposals):
    """Generate server-validated previews without changing preferences or watchlist data."""
    actions = []; configuration_seen = False
    for proposal in proposals:
        if proposal['kind'] == 'preferences':
            if configuration_seen:
                raise ValueError('同一回答请合并为一份配置变更')
            configuration_seen = True
            before = get_preferences(store, uid).model_dump()
            after = changed_preferences(before, proposal['changes'])
            def value(data, key):
                return data['fees'][key.split('.')[1]] if key.startswith('fees.') else data[key]
            changes = [{'field': c['field'], 'label': LABELS[c['field']],
                        'before': value(before, c['field']), 'after': value(after, c['field'])}
                       for c in proposal['changes'] if value(before, c['field']) != value(after, c['field'])]
            if changes:
                actions.append({'kind': 'preferences', 'title': '个人研究配置变更', 'changes': changes,
                                'baseline': fingerprint(before), 'applied_at': None})
        elif proposal['kind'] == 'add_stock':
            value = stock_input(proposal); ident = value.market + ':' + value.symbol
            cost, qty = market.normalize_holding(proposal.get('cost_price'), proposal.get('quantity'))
            item = store.one('SELECT name,asset_class FROM instruments WHERE id=?', (ident,))
            if item and item['asset_class'] != 'stock':
                raise ValueError('该代码已有其他证券类型，请在自选页手动添加')
            existing = store.one('SELECT cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id=?', (uid, ident))
            actions.append({'kind': 'add_stock', 'title': '添加股票到本人自选', 'instrument_id': ident,
                            'market': value.market, 'symbol': value.symbol, 'group_name': value.group_name,
                            'name': item['name'] if item else '名称待核验', 'already_exists': bool(existing),
                            'cost_price': cost, 'quantity': qty,
                            'previous_cost_price': existing['cost_price'] if existing else None,
                            'previous_quantity': existing['quantity'] if existing else None,
                            'position': market.holding_view(cost, qty, market.bars_for(store, ident, limit=2)) if cost else None,
                            'applied_at': None})
        else:
            raise ValueError('助手不支持此操作')
    return actions


def apply(store, uid, request_id, index):
    """Apply an owned, successful preview after the user clicks its explicit apply button."""
    with store.user_lock(uid):
        row = store.one("SELECT reply_json FROM assistant_requests WHERE id=? AND user_id=? AND status='success'", (request_id, uid))
        if not row:
            raise AuthError('NOT_FOUND', '可应用的助手方案不存在', 404)
        reply = json.loads(row['reply_json']); actions = reply.get('actions', [])
        if not 0 <= index < len(actions):
            raise AuthError('NOT_FOUND', '方案不存在', 404)
        action = actions[index]
        if action.get('applied_at'):
            return {'kind': action['kind'], 'applied_at': action['applied_at'], 'reused': True}
        if action['kind'] == 'preferences':
            before = get_preferences(store, uid).model_dump()
            if fingerprint(before) != action['baseline']:
                raise AuthError('VERSION_CONFLICT', '配置已经变化，请重新生成方案再应用', 409)
            changes = [{'field': c['field'], 'value': c['after']} for c in action['changes']]
            after = changed_preferences(before, changes)
        elif action['kind'] == 'add_stock':
            value = stock_input(action)
            cost, qty = market.normalize_holding(action.get('cost_price'), action.get('quantity'))
        else:
            raise AuthError('ACTION_NOT_ALLOWED', '此操作只能在项目页面手动进行', 403)
        with store.tx(immediate=True) as c:
            if not store.active(uid, c=c):
                raise AuthError('ACCOUNT_DISABLED', '账号不可用', 403)
            if not c.execute('SELECT id FROM assistant_requests WHERE id=? AND user_id=?', (request_id, uid)).fetchone():
                raise AuthError('NOT_FOUND', '方案已删除', 404)
            if action['kind'] == 'preferences':
                c.execute('UPDATE user_settings SET payload_json=? WHERE user_id=?', (json_text(after), uid))
            elif action['kind'] == 'add_stock':
                current = c.execute('SELECT cost_price,quantity FROM user_watchlist WHERE user_id=? AND instrument_id=?', (uid, action['instrument_id'])).fetchone()
                previous = (action.get('previous_cost_price'), action.get('previous_quantity'))
                actual = (current['cost_price'], current['quantity']) if current else (None, None)
                if actual != previous:
                    raise AuthError('VERSION_CONFLICT', '持仓记录已经变化，请重新生成方案再应用', 409)
                if not current:
                    item = c.execute('SELECT asset_class FROM instruments WHERE id=?', (action['instrument_id'],)).fetchone()
                    if item and item['asset_class'] != 'stock':
                        raise AuthError('VERSION_CONFLICT', '证券资料已经变化，请在自选页核对', 409)
                    from backend.instruments import Rules
                    rules = Rules(price_tick='0.001' if value.asset_class == 'etf' else '0.01')
                    c.execute("INSERT OR IGNORE INTO instruments(id,symbol,market,name,asset_class,group_name,watched,rules_json) VALUES(?,?,?,?,?,'',0,?)", (action['instrument_id'], value.symbol, value.market, '名称待核验', 'stock', rules.model_dump_json()))
                    c.execute('INSERT INTO user_watchlist(user_id,instrument_id,group_name,pinned,created_at) VALUES(?,?,?,0,?)', (uid, action['instrument_id'], value.group_name, stamp()))
                    market.queue_names(store, c=c)
                if cost is not None:
                    c.execute('UPDATE user_watchlist SET cost_price=?,quantity=? WHERE user_id=? AND instrument_id=?', (cost, qty, uid, action['instrument_id']))
            action['applied_at'] = stamp()
            c.execute('UPDATE assistant_requests SET reply_json=?,updated_at=? WHERE id=? AND user_id=?', (json_text(reply), stamp(), request_id, uid))
            c.execute('UPDATE assistant_chunks SET payload_json=? WHERE request_id=? AND user_id=?', (json_text(reply), request_id, uid))
        return {'kind': action['kind'], 'applied_at': action['applied_at'], 'reused': False}
