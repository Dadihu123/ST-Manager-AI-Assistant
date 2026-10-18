"""输出 schema（`backend/core/schema.py`）：一份声明驱动「要求」与「校验」。

覆盖重点是**两者同源**这件事 —— 改了 schema 的字数上限，
发给模型的要求与事后检查必须同时变，否则模型永远被判失败。
"""

import pytest

from conftest import load_backend_submodule

schema = load_backend_submodule('core.schema')
prompt = load_backend_submodule('core.prompt')
parsing = load_backend_submodule('core.parsing')

BASIC = {
    'json_only': True,
    'required': ['summary', 'tags'],
    'properties': {
        'summary': {'type': 'string', 'min_chars': 0, 'max_chars': 100},
        'tags': {
            'type': 'array', 'max_items': 3, 'min_confidence': 0.5,
            'order_by': 'confidence',
            'item': {'name': {'type': 'string'}, 'confidence': {'type': 'number'}},
        },
    },
}


# ------------------------------------------------------------------ 规范化


def test_normalize_schema_fills_defaults():
    spec = schema.normalize_schema({})
    assert spec['json_only'] is True
    assert spec['strip_think'] is True
    assert spec['forbidden_wrappers'] == []
    assert spec['required'] == []
    assert spec['properties'] == {}


def test_normalize_property_rejects_unknown_type():
    assert schema.normalize_property({'type': '量子态'})['type'] == 'string'
    assert schema.normalize_property(None)['type'] == 'string'


def test_normalize_property_clamps_item_limits():
    """坏 schema 不能把内存吃满。"""
    spec = schema.normalize_property({'type': 'array', 'max_items': 10 ** 9})
    assert spec['max_items'] == schema.MAX_ITEMS_LIMIT


def test_normalize_schema_keeps_declaration_order():
    spec = schema.normalize_schema({
        'properties': {'z': {'type': 'string'}, 'a': {'type': 'string'}},
    })
    assert list(spec['properties']) == ['z', 'a'], '顺序影响「输出要求」的排列'


# ------------------------------------------------------------------ 生成要求


def test_describe_constraints_covers_all_declared_limits():
    lines = schema.describe_constraints(BASIC)
    text = '\n'.join(lines)

    assert 'JSON' in text
    assert 'summary' in text and '100' in text
    assert 'tags' in text and '3' in text
    assert '0.5' in text


def test_describe_constraints_empty_when_nothing_declared():
    assert schema.describe_constraints({'json_only': False}) == []


def test_describe_constraints_mentions_forbidden_wrappers():
    lines = schema.describe_constraints({
        'json_only': True, 'forbidden_wrappers': ['```json', '```'],
    })
    assert any('不要使用' in line for line in lines)


def test_describe_constraints_lists_enum_values():
    lines = schema.describe_constraints({
        'properties': {'kind': {'type': 'enum', 'values': ['A', 'B']}},
    })
    assert any('A' in line and 'B' in line for line in lines)


# ------------------------------------------------------------------ 校验


def test_validate_reports_missing_required():
    out = schema.validate({'summary': '', 'tags': []}, BASIC)
    assert out['ok'] is False
    assert any('summary' in item for item in out['issues'])


def test_validate_treats_empty_array_as_provided():
    """空数组 = 「确实没找到标签」，不是「缺失」。"""
    out = schema.validate({'summary': 'ok', 'tags': []}, BASIC)
    assert out['ok'] is True
    assert out['issues'] == []


def test_validate_truncates_overlong_string_with_warning():
    out = schema.validate({'summary': '甲' * 150, 'tags': []}, BASIC)
    assert len(out['values']['summary']) <= 100
    assert any('已裁剪' in item for item in out['warnings'])


def test_validate_truncates_array_with_warning():
    tags = [{'name': f't{index}', 'confidence': 0.9} for index in range(10)]
    out = schema.validate({'summary': 'ok', 'tags': tags}, BASIC)
    assert len(out['values']['tags']) == 3
    assert any('已截断' in item for item in out['warnings'])


def test_validate_enum_rejects_out_of_range_value():
    spec = {
        'required': [],
        'properties': {'kind': {'type': 'enum', 'values': ['A', 'B']}},
    }
    out = schema.validate({'kind': 'C'}, spec)
    assert any('kind' in item for item in out['issues'])


def test_validate_enum_fallback_empty_clears_value():
    spec = {
        'required': [],
        'properties': {'kind': {'type': 'enum', 'values': ['A'], 'fallback': 'empty'}},
    }
    out = schema.validate({'kind': 'X'}, spec)
    assert out['values']['kind'] == ''


def test_validate_strict_promotes_soft_issue():
    spec = {
        'required': [],
        'properties': {'summary': {'type': 'string', 'min_chars': 50}},
    }
    lenient = schema.validate({'summary': '短'}, spec, strict=False)
    assert lenient['ok'] is True
    assert any('短于下限' in item for item in lenient['warnings'])

    strict = schema.validate({'summary': '短'}, spec, strict=True)
    assert strict['ok'] is False
    assert any('短于下限' in item for item in strict['issues'])


def test_validate_ignores_fields_not_in_schema():
    """schema 没声明的字段原样保留（task spec 可以只约束关心的部分）。"""
    out = schema.validate({'extra': 'keep', 'summary': 'ok', 'tags': []}, BASIC)
    assert out['values']['extra'] == 'keep'


# ------------------------------------------------------------------ 同源性


def test_instruction_and_validation_read_the_same_schema():
    """核心保证：改一处，两边同时生效。"""
    spec = {
        'json_only': True,
        'required': ['summary'],
        'properties': {'summary': {'type': 'string', 'max_chars': 42}},
    }

    instruction = prompt.format_instruction(spec)
    assert '42' in instruction, '生成要求里必须有 42'

    checked = parsing.validate_result({'summary': '甲' * 80, 'tags': []}, spec)
    assert len(checked['result']['summary']) <= 42, '校验也要按 42 裁'


def test_confidence_floor_and_item_limits_helpers():
    assert schema.confidence_floor(BASIC) == 0.5
    assert schema.item_limits(BASIC) == (0, 3)
    assert schema.confidence_floor({}) == 0.0


@pytest.mark.parametrize('kind', ['string', 'number', 'array', 'enum', 'object'])
def test_every_declared_type_normalizes(kind):
    spec = schema.normalize_property({'type': kind})
    assert spec['type'] == kind


def test_object_type_normalizes_nested_properties():
    spec = schema.normalize_property({
        'type': 'object',
        'item': {'a': {'type': 'string'}, 'b': {'type': 'number'}},
    })
    assert set(spec['item']) == {'a', 'b'}


# ------------------------------------------------------------------ 自定义字段


def test_custom_schema_field_survives_normalization():
    """schema 声明的自定义字段必须透传到校验阶段。

    回归用例：`normalize_result` 只产出固定的几个键，`validate_result` 又按那几个键
    重建 dict，于是 schema 里新增的字段（如 `kind`）在两步里都消失，
    枚举越界永远查不出来（`ok=True` 却带了个非法值）。
    """
    custom = {
        'required': ['summary'],
        'properties': {
            'summary': {'type': 'string'},
            'kind': {'type': 'enum', 'values': ['OC', '同人']},
        },
    }

    normalized = parsing.normalize_result({'summary': 'x', 'kind': '宇宙飞船'}, custom)
    assert normalized['kind'] == '宇宙飞船', '归一化阶段不能丢'

    checked = parsing.validate_result(normalized, custom)
    assert checked['ok'] is False, '枚举越界必须判失败'
    assert any('kind' in item for item in checked['issues'])


def test_custom_schema_field_valid_value_passes():
    custom = {
        'required': ['summary'],
        'properties': {
            'summary': {'type': 'string'},
            'kind': {'type': 'enum', 'values': ['OC', '同人']},
        },
    }
    checked = parsing.validate_result({'summary': 'x', 'kind': 'OC'}, custom)
    assert checked['ok'] is True
    assert checked['result']['kind'] == 'OC'


def test_declared_but_absent_field_is_checked_as_empty():
    """声明了必填、模型又没给出 → 必须报缺失（不能因为「键不存在」就跳过）。"""
    custom = {
        'required': ['new_content'],
        'properties': {'new_content': {'type': 'string'}},
    }
    checked = parsing.validate_result({'summary': 'x'}, custom)
    assert checked['ok'] is False
    assert any('new_content' in item for item in checked['issues'])
