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


def test_validate_profile_rejects_broken_length_rules():
    bad = profiles.builtin_profile('builtin-default')
    bad['length_rules'] = {'summary_min_chars': 500, 'summary_max_chars': 100,
                           'tags_min': 9, 'tags_max': 3, 'confidence_min': 3}
    errors = profiles.validate_profile(bad)
    assert any('summary_min_chars' in item for item in errors)
    assert any('tags_min' in item for item in errors)
    assert any('confidence_min' in item for item in errors)


def test_normalize_profile_fills_defaults():
    data = profiles.normalize_profile({'id': 'my-profile'})
    assert data['model_match'] == '*'
    assert data['missing_policy'] == 'error'
    assert data['format_rules']['json_only'] is True
    assert data['length_rules']['summary_max_chars'] == 300
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
