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
    assert set(entry) == {
        'id', 'name', 'model_match', 'priority', 'version', 'builtin', 'updated_at',
        # `task` 是 §4.3 新增的：前端 `candidateMatchesTask` 已经在读它，
        # 但以前这个字段整条链路都不存在，所以 17 个档案全列出来。
        'task',
    }


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


# ------------------------------------------------------------------ 任务归属（C6 / §4.3）
#
# 「按任务选档案」这个概念以前在代码里**根本不存在**：5 个内置档案的 `task`
# 全是 None，`normalize_profile` 是白名单会把用户的 `task` 丢掉，
# `select_profile` 签名里没有 task，前端 `candidateMatchesTask` 读到 undefined
# 就 `return true`。于是归类任务拿到了打标档案，实测 1/1 全失败。


def test_normalize_profile_keeps_task():
    data = profiles.normalize_profile({'id': 'x-1', 'task': 'classify'})
    assert data['task'] == 'classify'


def test_normalize_profile_normalizes_task_case():
    data = profiles.normalize_profile({'id': 'x-1', 'task': ' WI_REWRITE '})
    assert data['task'] == 'wi_rewrite'


def test_normalize_profile_defaults_task_to_generic():
    assert profiles.normalize_profile({'id': 'x-1'})['task'] == ''


def test_builtin_profiles_carry_their_task():
    expected = {
        'builtin-wi-rewrite': 'wi_rewrite',
        'builtin-classify': 'classify',
        'builtin-deepseek': 'analyze',
        'builtin-propose': 'propose',
        'builtin-agent': 'agent',
    }
    for profile_id, task in expected.items():
        assert profiles.builtin_profile(profile_id)['task'] == task


def test_generic_builtin_profiles_stay_generic():
    """模型家族档案（deepseek 除外）是通用档案，任何任务都能用。"""
    for profile_id in ('builtin-default', 'builtin-gpt', 'builtin-claude', 'builtin-gemini'):
        assert profiles.builtin_profile(profile_id)['task'] == ''


def test_validate_profile_rejects_unknown_task():
    errors = profiles.validate_profile({'id': 'x-1', 'task': 'nope'})
    assert any('task' in item for item in errors)


def test_select_profile_by_task_avoids_wrong_family():
    """T13：归类任务选到 `builtin-classify`，不是 `builtin-deepseek`。"""
    selected = profiles.select_profile(
        profiles.builtin_profiles(), 'deepseek-chat', task='classify',
    )
    assert selected['id'] == 'builtin-classify'


def test_select_profile_prefers_model_match_within_task():
    """模型命中仍然优先：claude 跑打标不该拿 deepseek 档案。"""
    selected = profiles.select_profile(
        profiles.builtin_profiles(), 'claude-sonnet-4', task='analyze',
    )
    assert selected['id'] == 'builtin-claude'


def test_select_profile_unknown_model_stays_generic():
    """未知模型 + analyze 不该落到 deepseek 档案（它有 model_match）。"""
    selected = profiles.select_profile(
        profiles.builtin_profiles(), 'some-model', task='analyze',
    )
    assert selected['id'] == 'builtin-default'


def test_select_profile_without_task_keeps_old_behaviour():
    """没指定任务时不按任务过滤，别把 deepseek 家族藏起来。"""
    assert profiles.select_profile(
        profiles.builtin_profiles(), 'deepseek-chat',
    )['id'] == 'builtin-deepseek'


def test_select_profile_keeps_generic_user_profile_for_any_task():
    """空 task 的用户档案任何任务都能用（不被任务过滤误伤）。"""
    candidates = profiles.builtin_profiles() + [
        profiles.normalize_profile(
            {'id': 'my-generic', 'model_match': 'deepseek-*', 'priority': 200},
        ),
    ]
    selected = profiles.select_profile(candidates, 'deepseek-chat', task='classify')
    assert selected['id'] == 'my-generic'
