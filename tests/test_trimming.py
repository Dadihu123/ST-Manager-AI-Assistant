"""世界书分层取样与文本裁剪。"""

from stm_ai_core import tokens, trimming


def test_entry_accessors_tolerate_both_field_styles():
    entry = {'uid': 7, 'comment': '世界观', 'key': '世界, 设定', 'content': 'X', 'disable': True}
    assert trimming.entry_uid(entry) == '7'
    assert trimming.entry_name(entry) == '世界观'
    assert trimming.entry_keys(entry) == ['世界', '设定']
    assert trimming.is_enabled(entry) is False

    other = {'id': 'a1', 'name': '地理', 'keys': ['北境'], 'content': 'Y'}
    assert trimming.entry_uid(other) == 'a1'
    assert trimming.entry_name(other) == '地理'
    assert trimming.entry_keys(other) == ['北境']
    assert trimming.is_enabled(other) is True


def test_entry_accessors_tolerate_garbage():
    assert trimming.entry_uid(None) == ''
    assert trimming.entry_name('not-a-dict') == ''
    assert trimming.entry_keys({'keys': 42}) == []
    assert trimming.entry_content({'content': None}) == ''
    assert trimming.is_enabled({}) is True


def test_select_wi_excerpt_skips_disabled_entries():
    entries = [
        {'comment': '启用', 'content': '内容' * 20},
        {'comment': '停用', 'content': '内容' * 20, 'disable': True},
    ]
    result = trimming.select_wi_excerpt(entries, 500)
    assert result['count'] == 1
    assert '停用' not in result['outline']


def test_select_wi_excerpt_with_zero_budget_returns_counts_only():
    result = trimming.select_wi_excerpt([{'comment': 'A', 'content': 'x' * 500}], 0)
    assert result['outline'] == ''
    assert result['excerpt'] == ''
    assert result['count'] == 1
    assert result['total_tokens'] > 0
    assert result['truncated'] is True


def test_select_wi_excerpt_prioritizes_constant_entries():
    entries = [
        {'comment': '普通', 'content': 'A' * 400},
        {'comment': '常驻', 'content': 'B' * 400, 'constant': True},
    ]
    result = trimming.select_wi_excerpt(entries, 400, outline_ratio=0.0)
    assert result['selected'][0] == '常驻'


def test_select_wi_excerpt_prioritizes_keyword_hits():
    entries = [
        {'comment': '角色甲', 'content': 'A' * 400},
        {'comment': '世界观设定', 'content': 'B' * 400},
    ]
    result = trimming.select_wi_excerpt(entries, 200, outline_ratio=0.0)
    assert result['selected'][0] == '世界观设定'


def test_select_wi_excerpt_marks_truncated_when_budget_short():
    entries = [{'comment': f'E{index}', 'content': '字' * 400} for index in range(10)]
    result = trimming.select_wi_excerpt(entries, 300)
    assert result['truncated'] is True
    assert result['dropped']


def test_select_wi_excerpt_stays_within_budget():
    entries = [{'comment': 'A', 'content': '字' * 100}]
    result = trimming.select_wi_excerpt(entries, 2000)
    assert result['excerpt_tokens'] <= 2000
    assert result['selected'] == ['A']
    assert result['excerpt'].startswith('（以下为节选）')


def test_select_wi_excerpt_keeps_outline_even_with_tiny_budget():
    entries = [{'comment': 'A', 'keys': ['k'], 'content': 'x' * 100}]
    result = trimming.select_wi_excerpt(entries, 1)
    assert result['outline']


def test_truncate_to_tokens_shortens_long_text():
    text = '字' * 1000
    out = trimming.truncate_to_tokens(text, 100)
    assert out.endswith('…')
    assert tokens.estimate(out) <= 130


def test_truncate_to_tokens_keeps_short_text():
    assert trimming.truncate_to_tokens('短文本', 100) == '短文本'
    assert trimming.truncate_to_tokens('任意', 0) == ''


def test_is_mostly_html():
    assert trimming.is_mostly_html('<div><span>a</span></div>') is True
    assert trimming.is_mostly_html('普通文本，偶尔 <b>加粗</b>') is False
    assert trimming.is_mostly_html('') is False
