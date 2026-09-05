def asian_gate(final_decision, final_conf, three_way, entry_style, entry_price, sess_q,
               enabled=True, pen=0.45):
    """复刻 meta_agent.adjudicate 内「亚盘方向确认增强」逻辑，用于离线验证。"""
    if not (final_decision in ('BUY', 'SELL') and enabled):
        return final_conf
    if not sess_q:
        return final_conf
    if sess_q not in ('moderate', 'poor'):
        return final_conf
    strong = three_way or (final_conf >= 0.72)
    has_zone = (entry_style == 'limit' and entry_price is not None)
    if (not strong) and (not has_zone):
        return final_conf * pen
    return final_conf


OPEN = 0.50  # RISK_MIN_CONFIDENCE 默认开仓门槛
cases = [
    ('亚盘+弱共识SELL+无zone (复现三单错方向根因)', 'SELL', 0.66, False, 'market', None, 'moderate'),
    ('亚盘+弱共识SELL+无zone (另一错方向单)',       'SELL', 0.60, False, 'market', None, 'moderate'),
    ('亚盘+三脑强共识BUY+无zone (照常开)',         'BUY',  0.75, True,  'market', None, 'moderate'),
    ('亚盘+弱共识SELL+有zone(limit,AI要等回)',     'SELL', 0.66, False, 'limit',   4329.0, 'moderate'),
    ('欧美盘+弱共识SELL (不干预)',                 'SELL', 0.66, False, 'market', None, 'excellent'),
    ('凌晨清淡+弱共识BUY+无zone',                  'BUY',  0.60, False, 'market', None, 'poor'),
]
print('开仓门槛=%.2f (RISK_MIN_CONFIDENCE)' % OPEN)
for name, d, c, tw, es, ep, sq in cases:
    out = asian_gate(d, c, tw, es, ep, sq)
    verdict = '低于门槛→不市价追(✓止血)' if out < OPEN else '正常开(✓保交易笔数)'
    print('  [%-44s] conf %.2f→%.2f | %s' % (name, c, out, verdict))

# 断言：确保不误伤强信号、不漏拦弱信号
assert asian_gate('SELL', 0.66, False, 'market', None, 'moderate') < OPEN, '弱共识亚盘单应被降权拦住'
assert asian_gate('BUY', 0.75, True, 'market', None, 'moderate') >= 0.72, '强共识单不应被拦'
assert asian_gate('SELL', 0.66, False, 'limit', 4329.0, 'moderate') == 0.66, '有zone单应照常'
assert asian_gate('SELL', 0.66, False, 'market', None, 'excellent') == 0.66, '欧美盘不应干预'
print('\nALL_ASSERT_PASS')
