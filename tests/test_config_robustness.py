"""配置收敛的健壮性：坏值不能让插件不可用。

`normalize_config()` 直接吃宿主设置页写进来的原始值。这些值是**用户（或某次
JSON 往返）能随手写出来的**：`1e400` 是合法 JSON，`"Infinity"` / `"NaN"`
是合法字符串。而 `int(float('inf'))` 抛的是 `OverflowError` —— 它不属于
`TypeError/ValueError`，会穿透 `load_config()`（那里只兜 `api.config.get()`），
让所有 `_handle_errors` 包着的接口统一 500。

这里钉住三层：
1. 标量键（`max_tags` 等，上游就有）遇到非有限值回落默认；
2. `tag_category_rules`（本次新增、设置页可直接编辑）里的非有限值被丢弃；
3. 旧版全局 `confidence_min` 被迁移成「未设置分类的默认门槛」而不是静默丢掉。
"""

import json

import pytest

from conftest import load_backend_submodule

config_module = load_backend_submodule('config')

NON_FINITE = [1e400, float('inf'), float('-inf'), float('nan'), 'Infinity', '-Infinity', 'NaN', '1e400']


@pytest.mark.parametrize('value', NON_FINITE)
def test_scalar_keys_fall_back_instead_of_raising(value):
    """非有限值必须回落默认，不能抛 OverflowError。"""
    normalized = config_module.normalize_config({
        'max_tags': value,
        'summary_max_chars': value,
        'tag_pool_budget_tokens': value,
        'batch_size': value,
        'concurrency': value,
    })
    assert normalized['max_tags'] == config_module.DEFAULT_CONFIG['max_tags']
    assert normalized['summary_max_chars'] == config_module.DEFAULT_CONFIG['summary_max_chars']
    assert normalized['batch_size'] == config_module.DEFAULT_CONFIG['batch_size']
    assert normalized['concurrency'] == config_module.DEFAULT_CONFIG['concurrency']


@pytest.mark.parametrize('value', NON_FINITE)
def test_rule_quota_falls_back_instead_of_raising(value):
    """`tag_category_rules` 里的非有限配额被丢弃，其余规则照常生效。

    注意用 `json.dumps` 构造载荷：Python 的 json 既接受 `Infinity` / `NaN`
    字面量也接受 `"1e400"` 字符串，这两种都是用户或一次 JSON 往返能真实
    产生的内容（而不是我们手写出的非法 JSON）。
    """
    rules_json = json.dumps({
        '题材': {'max_tags': value},
        '场景': {'max_tags': 3},
    }, ensure_ascii=False)
    normalized = config_module.normalize_config({'tag_category_rules': rules_json})
    rules = normalized['tag_category_rules']
    assert '场景' in rules, '同一份配置里合法的规则不能被坏值连累'
    assert rules['场景']['max_tags'] == 3
    assert 'max_tags' not in rules.get('题材', {})


def test_numbers_are_still_accepted():
    """加固不能把正常数字也挡掉。"""
    normalized = config_module.normalize_config({
        'max_tags': '12', 'tag_pool_budget_tokens': 800,
        'tag_category_rules': '{"题材": {"max_tags": "2", "confidence_min": "0.7"}}',
    })
    assert normalized['max_tags'] == 12
    assert normalized['tag_pool']['budget_tokens'] == 800
    assert normalized['tag_category_rules']['题材'] == {'max_tags': 2, 'confidence_min': 0.7}


def test_legacy_global_confidence_is_migrated_not_dropped():
    """旧版 `confidence_min` 不该静默消失——那会把 0.8 悄悄放宽成 0.5。"""
    normalized = config_module.normalize_config({'confidence_min': 0.8})

    assert normalized['legacy_confidence_min'] == 0.8, '值得保留下来当默认门槛'
    assert normalized['migration_notices'], '要提示用户门槛的语义变了'
    assert '0.8' in normalized['migration_notices'][0]


def test_no_migration_notice_once_rules_exist():
    """已经按分类配过规则的用户不需要再被提示迁移。"""
    normalized = config_module.normalize_config({
        'confidence_min': 0.8,
        'tag_category_rules': '{"题材": {"confidence_min": 0.6}}',
    })
    assert normalized['migration_notices'] == []


def test_no_legacy_value_means_no_notice():
    normalized = config_module.normalize_config({})
    assert normalized['legacy_confidence_min'] is None
    assert normalized['migration_notices'] == []
