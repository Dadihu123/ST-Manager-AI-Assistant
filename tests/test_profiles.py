"""提示词档案：校验、模型匹配、索引与合并。"""

import pytest

from stm_ai_core import profiles


def test_builtin_profiles_pass_validation():
    for profile in profiles.builtin_profiles():
        assert profiles.validate_profile(profile) == [], profile['id']


def test_builtin_profiles_cover_common_model_families():
    ids = {profile['id'] for profile in profiles.builtin_profiles()}
    assert ids == {
        'builtin-default', 'builtin-gpt', 'builtin-claude',
        'builtin-deepseek', 'builtin-gemini',
        # 世界书改写与自动归类是 task spec 的复用示例，也是内置任务
        'builtin-wi-rewrite', 'builtin-classify', 'builtin-propose',
        'builtin-agent',
    }


def test_builtin_default_is_catch_all():
    fallback = profiles.builtin_profile('builtin-default')
    assert fallback['model_match'] == '*'
    assert fallback['priority'] == 0


@pytest.mark.parametrize('pattern,model,expected', [
    ('gpt-*', 'gpt-4o-mini', True),
    ('gpt-*', 'GPT-4O', True),
    ('deepseek', 'deepseek-chat', True),
    ('deepseek', 'deepseek', True),
    ('deepseek', 'deepseekx', True),
    ('claude-*', 'gpt-4o', False),
    ('*', 'anything', True),
    ('', 'anything', False),
])
def test_match_pattern(pattern, model, expected):
    assert profiles.match_pattern(pattern, model) is expected


def test_normalize_model_name_strips_prefix_and_tag():
    assert profiles.normalize_model_name('models/GPT-4o@2024-08') == 'gpt-4o'
    assert profiles.normalize_model_name('  DeepSeek-Chat  ') == 'deepseek-chat'


def test_select_profile_prefers_matching_family():
    candidates = profiles.builtin_profiles()
    assert profiles.select_profile(candidates, 'gpt-4o-mini')['id'] == 'builtin-gpt'
    assert profiles.select_profile(candidates, 'claude-sonnet-4')['id'] == 'builtin-claude'
    assert profiles.select_profile(candidates, 'deepseek-chat')['id'] == 'builtin-deepseek'


def test_select_profile_falls_back_to_catch_all():
    candidates = profiles.builtin_profiles()
    assert profiles.select_profile(candidates, 'some-unknown-model')['id'] == 'builtin-default'


def test_select_profile_falls_back_to_builtin_when_list_empty():
    assert profiles.select_profile([], 'gpt-4o')['id'] == 'builtin-default'


def test_select_profile_respects_priority_then_specificity():
    candidates = [
        {'id': 'low', 'model_match': 'gpt-4o', 'priority': 1},
        {'id': 'high', 'model_match': 'gpt-*', 'priority': 10},
        {'id': 'specific', 'model_match': 'gpt-4o-mini', 'priority': 10},
    ]
    assert profiles.select_profile(candidates, 'gpt-4o-mini')['id'] == 'specific'
    assert profiles.select_profile(candidates, 'gpt-4o')['id'] == 'high'


def test_validate_profile_rejects_unknown_variable():
    bad = profiles.builtin_profile('builtin-default')
    bad['user_template'] = '{{ card.nickname }}'
    errors = profiles.validate_profile(bad)
    assert any('未知变量' in item for item in errors)


def test_validate_profile_rejects_invalid_id_and_ranges():
    bad = profiles.builtin_profile('builtin-default')
    bad['id'] = 'Bad Id'
    bad['temperature'] = 5
    bad['max_tokens'] = 10
    errors = profiles.validate_profile(bad)
    assert any('id 不符合' in item for item in errors)
    assert any('temperature' in item for item in errors)
    assert any('max_tokens' in item for item in errors)


def test_validate_profile_requires_system_or_template():
    bad = profiles.normalize_profile({'id': 'x-1', 'system': '', 'user_template': ''})
    assert any('至少要有一个非空' in item for item in profiles.validate_profile(bad))


def test_validate_profile_rejects_broken_output_schema():
    """schema 自身的自洽性：下限不能大于上限。"""
    bad = profiles.builtin_profile('builtin-default')
    bad['output_schema'] = {
        'required': ['summary', 'nope'],
        'properties': {
            'summary': {'type': 'string', 'min_chars': 500, 'max_chars': 100},
            'tags': {'type': 'array', 'min_items': 9, 'max_items': 3},
        },
    }
    errors = profiles.validate_profile(bad)
    assert any('summary' in item and 'min_chars' in item for item in errors)
    assert any('tags' in item and 'min_items' in item for item in errors)
    assert any('nope' in item for item in errors), 'required 里的字段必须已声明'


def test_validate_profile_rejects_unknown_apply_action():
    bad = profiles.builtin_profile('builtin-default')
    bad['apply_actions'] = [{'from': 'summary', 'action': 'do_something_weird'}]
    errors = profiles.validate_profile(bad)
    assert any('未知的应用动作' in item for item in errors)


def test_validate_profile_rejects_action_with_undeclared_source():
    """动作的来源字段必须已在 output_schema 里声明，否则永远取不到值。"""
    bad = profiles.builtin_profile('builtin-default')
    bad['apply_actions'] = [{'from': 'nonexistent_field', 'action': 'write_card_note'}]
    errors = profiles.validate_profile(bad)
    assert any('nonexistent_field' in item for item in errors)


def test_normalize_profile_fills_defaults():
    data = profiles.normalize_profile({'id': 'my-profile'})
    assert data['model_match'] == '*'
    assert data['missing_policy'] == 'error'
    assert data['schema_version'] == 2, 'v2 一步到位，不做 v1 迁移'
    assert data['output_schema']['json_only'] is True
    assert data['output_schema']['properties']['summary']['max_chars'] == 300
    assert data['apply_actions'], '默认带三个动作'
    assert data['stop'] == []


def test_normalize_profile_drops_extra_stop_items():
    data = profiles.normalize_profile({'id': 'x-1', 'stop': ['a', 'b', 'c', 'd', 'e']})
    assert data['stop'] == ['a', 'b', 'c', 'd']


def test_profile_index_entry_is_compact():
    entry = profiles.profile_index_entry(profiles.builtin_profile('builtin-gpt'))
    assert set(entry) == {'id', 'name', 'model_match', 'priority', 'version', 'builtin', 'updated_at'}


def test_merge_profiles_overrides_builtin_by_id():
    override = profiles.builtin_profile('builtin-gpt')
    override['name'] = '我的 GPT 档案'
    merged = {item['id']: item for item in profiles.merge_profiles([override])}
    assert merged['builtin-gpt']['name'] == '我的 GPT 档案'
    assert 'builtin-claude' in merged


def test_builtin_profile_returns_independent_copy():
    first = profiles.builtin_profile('builtin-gpt')
    first['name'] = 'changed'
    assert profiles.builtin_profile('builtin-gpt')['name'] != 'changed'
