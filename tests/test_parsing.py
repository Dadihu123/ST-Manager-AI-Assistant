"""LLM 输出的容错解析与后置校验。"""

import pytest

from stm_ai_core import parsing

FORMAT = {'json_only': True, 'required_keys': ['summary', 'tags'], 'strip_think': True}
LIMITS = {'summary_max_chars': 100, 'summary_min_chars': 0, 'tags_max': 3, 'tags_min': 0,
          'confidence_min': 0.5}


# ------------------------------------------------------------------ 清洗


def test_strip_think_removes_closed_and_unclosed_blocks():
    assert parsing.strip_think('<think>想一下</think>结果') == '结果'
    assert parsing.strip_think('<thinking>a</thinking>b') == 'b'
    assert parsing.strip_think('<reasoning>x</reasoning>y') == 'y'
    assert parsing.strip_think('<think>没闭合') == ''


def test_strip_fences_reports_whether_stripped():
    text, stripped = parsing.strip_fences('```json\n{"a": 1}\n```')
    assert stripped is True
    assert text == '{"a": 1}'

    text, stripped = parsing.strip_fences('{"a": 1}')
    assert stripped is False
    assert text == '{"a": 1}'


def test_extract_balanced_handles_strings_and_escapes():
    payload = '{"a": "}"}'
    assert parsing.extract_balanced('前 { 后') == ''
    assert parsing.extract_balanced(f'noise {payload} tail') == payload

    escaped = '{"a": "\\"}"}'
    assert parsing.extract_balanced(escaped) == escaped


@pytest.mark.parametrize('text,strategy', [
    ('{"a": 1}', 'direct'),
    ('说明如下：{"a": 1} 完毕', 'brace'),
    ('[{"a": 1}]', 'direct'),
    ('结果：[{"a": 1}]', 'bracket'),
])
def test_loads_lenient_strategies(text, strategy):
    _data, used = parsing.loads_lenient(text)
    assert used == strategy


def test_loads_lenient_raises_on_garbage():
    with pytest.raises(ValueError):
        parsing.loads_lenient('完全不是 JSON')


# ------------------------------------------------------------------ 归一化


def test_normalize_result_accepts_string_tag_list():
    result = parsing.normalize_result({'summary': '概览', 'tags': ['战斗', '奇幻']})
    assert result['tags'] == [
        {'name': '战斗', 'confidence': None},
        {'name': '奇幻', 'confidence': None},
    ]


def test_normalize_result_accepts_results_wrapper():
    result = parsing.normalize_result({'results': [{'summary': 'S', 'tags': []}]})
    assert result['summary'] == 'S'
    assert any('多项结果' in item for item in result['_warnings'])


def test_normalize_result_accepts_bare_array():
    assert parsing.normalize_result([{'summary': 'S'}])['summary'] == 'S'


def test_normalize_result_accepts_tag_alias_fields():
    result = parsing.normalize_result({'tags': [{'tag': '战斗', 'score': 0.8}]})
    assert result['tags'] == [{'name': '战斗', 'confidence': 0.8}]


def test_normalize_result_dedupes_tags_and_keeps_max_confidence():
    result = parsing.normalize_result({'tags': [
        {'name': '战斗', 'confidence': 0.4},
        {'name': '战斗', 'confidence': 0.9},
    ]})
    assert result['tags'] == [{'name': '战斗', 'confidence': 0.9}]


def test_normalize_result_normalizes_percent_confidence():
    result = parsing.normalize_result({'tags': [{'name': 'a', 'confidence': 85}]})
    assert result['tags'][0]['confidence'] == 0.85


def test_normalize_result_parses_string_flags():
    result = parsing.normalize_result({'content_flags': 'NSFW, 长卡'})
    assert result['content_flags'] == ['NSFW', '长卡']


def test_normalize_result_truncates_tag_name():
    result = parsing.normalize_result({'tags': ['甲' * 100]})
    assert len(result['tags'][0]['name']) == 64


# ------------------------------------------------------------------ 校验


def test_validate_result_truncates_long_summary():
    outcome = parsing.validate_result({'summary': '甲' * 150, 'tags': []}, FORMAT, LIMITS)
    assert len(outcome['result']['summary']) <= 100
    assert any('裁剪' in item for item in outcome['warnings'])
    assert outcome['ok'] is True


def test_validate_result_prefers_sentence_boundary():
    text = '甲' * 60 + '。' + '乙' * 200
    outcome = parsing.validate_result({'summary': text, 'tags': []}, FORMAT, LIMITS)
    assert outcome['result']['summary'] == '甲' * 60 + '。'


def test_validate_result_falls_back_to_hard_truncate_when_break_is_too_early():
    text = '短。' + '乙' * 200
    outcome = parsing.validate_result({'summary': text, 'tags': []}, FORMAT, LIMITS)
    assert outcome['result']['summary'].endswith('…')
    assert len(outcome['result']['summary']) <= 100


def test_validate_result_drops_low_confidence_tags():
    outcome = parsing.validate_result({
        'summary': 'ok',
        'tags': [{'name': 'a', 'confidence': 0.2}, {'name': 'b', 'confidence': 0.9}],
    }, FORMAT, LIMITS)
    assert [item['name'] for item in outcome['result']['tags']] == ['b']


def test_validate_result_truncates_tags_by_confidence():
    tags = [{'name': f't{index}', 'confidence': 0.5 + index / 100} for index in range(6)]
    outcome = parsing.validate_result({'summary': 'ok', 'tags': tags}, FORMAT, LIMITS)
    assert len(outcome['result']['tags']) == 3
    assert outcome['result']['tags'][0]['name'] == 't5'


def test_validate_result_keeps_tags_without_confidence():
    outcome = parsing.validate_result({'summary': 'ok', 'tags': ['无置信度']}, FORMAT, LIMITS)
    assert [item['name'] for item in outcome['result']['tags']] == ['无置信度']
    assert any('缺少置信度' in item for item in outcome['warnings'])


def test_validate_result_reports_missing_required_keys():
    outcome = parsing.validate_result({'summary': '', 'tags': []}, FORMAT, LIMITS)
    assert outcome['ok'] is False
    assert any('summary' in item for item in outcome['issues'])


def test_validate_result_empty_tag_list_is_not_missing():
    outcome = parsing.validate_result({'summary': 'ok', 'tags': []}, FORMAT, LIMITS)
    assert outcome['ok'] is True


def test_validate_result_strict_flags_short_summary():
    strict_limits = dict(LIMITS, summary_min_chars=50)
    outcome = parsing.validate_result(
        {'summary': '太短', 'tags': [{'name': 'a', 'confidence': 0.9}]},
        FORMAT, strict_limits, strict=True,
    )
    assert outcome['ok'] is False
    assert any('短于下限' in item for item in outcome['issues'])


def test_validate_result_non_strict_warns_short_summary():
    lenient_limits = dict(LIMITS, summary_min_chars=50)
    outcome = parsing.validate_result({'summary': '太短', 'tags': []}, FORMAT, lenient_limits)
    assert outcome['ok'] is True
    assert any('短于下限' in item for item in outcome['warnings'])


def test_validate_result_drops_new_tags_duplicating_selected():
    outcome = parsing.validate_result({
        'summary': 'ok',
        'tags': [{'name': '战斗', 'confidence': 0.9}],
        'suggested_new_tags': [
            {'name': '战斗', 'reason': 'x'},
            {'name': '赛博朋克', 'reason': 'y'},
        ],
    }, FORMAT, LIMITS)
    assert [item['name'] for item in outcome['result']['suggested_new_tags']] == ['赛博朋克']


# ------------------------------------------------------------------ 端到端


def test_parse_llm_result_handles_fenced_noise():
    text = '好的，结果如下：\n```json\n{"summary": "一个骑士", "tags": ["战斗"]}\n```\n希望有帮助。'
    outcome = parsing.parse_llm_result(text, FORMAT, LIMITS)
    assert outcome['ok'] is True
    assert outcome['result']['summary'] == '一个骑士'
    assert outcome['result']['tags'][0]['name'] == '战斗'
    assert any('代码块' in item for item in outcome['warnings'])


def test_parse_llm_result_handles_think_block():
    outcome = parsing.parse_llm_result('<think>先想想</think>{"summary": "S", "tags": []}',
                                       FORMAT, LIMITS)
    assert outcome['ok'] is True
    assert outcome['strategy'] == 'direct'


def test_parse_llm_result_reports_failure_with_excerpt():
    outcome = parsing.parse_llm_result('对不起，我无法完成。', FORMAT, LIMITS)
    assert outcome['ok'] is False
    assert outcome['result'] is None
    assert outcome['raw_excerpt']
    assert any('无法解析' in item for item in outcome['issues'])


def test_parse_llm_result_keeps_excerpt_short():
    outcome = parsing.parse_llm_result('x' * 5000, FORMAT, LIMITS)
    assert len(outcome['raw_excerpt']) == 2048
