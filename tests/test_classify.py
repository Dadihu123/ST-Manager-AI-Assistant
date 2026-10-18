"""自动归类：分类路径安全、方案收敛、AI 输出校验、作业链路。

**安全是本模块的核心**：AI 生成的分类名绝不能直接当路径用。
主程序 `api.cards.create_category` / `move` 已有白名单，本模块再做一层
**保守预检**——与其让主程序静默返回 False，不如在提交前就把不合法的方案指出来。
"""

import pytest

from conftest import load_backend_submodule

pytest.importorskip('flask', reason='装配层依赖 flask')

classify = load_backend_submodule('core.classify')
profiles = load_backend_submodule('core.profiles')
jobs = load_backend_submodule('jobs')

from fake_api import FakeApi, FakeCtx  # noqa: E402

SCHEME = [
    {'name': '奇幻', 'description': '剑与魔法、异世界'},
    {'name': '科幻', 'description': '未来科技、太空'},
    {'name': '日常', 'description': '现实向、校园'},
]


# ------------------------------------------------------------------ 路径安全


def test_root_aliases_normalize_to_empty():
    for alias in ('', '根目录', '/', 'root', '.', None):
        path, error = classify.normalize_category_path(alias)
        assert path == '' and error == '', alias


def test_parent_traversal_is_rejected():
    for bad in ('..', '../etc', 'a/../../b', 'a/../..'):
        path, error = classify.normalize_category_path(bad)
        assert error, f'{bad} 应被拒'
        assert path == ''


def test_absolute_path_is_rejected():
    path, error = classify.normalize_category_path('/etc/passwd')
    assert error
    assert path == ''


def test_windows_illegal_chars_are_rejected():
    for bad in ('a:b', 'a*b', 'a?b', 'a"b', 'a<b', 'a>b', 'a|b', 'a\x00b'):
        _path, error = classify.normalize_category_path(bad)
        assert error, f'{bad} 应被拒'


def test_backslash_is_normalized_to_separator():
    path, error = classify.normalize_category_path('父级\\子级')
    assert error == ''
    assert path == '父级/子级'


def test_reserved_names_are_rejected():
    """与主程序保留目录冲突会让卡片混进系统目录。"""
    for bad in ('notes', 'backups', 'lorebooks', 'thumbnails', 'cards', 'trash'):
        _path, error = classify.normalize_category_path(bad)
        assert error, f'{bad} 是保留名，应被拒'


def test_overlong_name_is_rejected():
    _path, error = classify.normalize_category_path('长' * 70)
    assert error


def test_too_deep_path_is_rejected():
    _path, error = classify.normalize_category_path('/'.join(['a'] * 8))
    assert error


def test_normal_path_passes():
    path, error = classify.normalize_category_path('题材/奇幻')
    assert error == ''
    assert path == '题材/奇幻'


# ------------------------------------------------------------------ 方案


def test_normalize_scheme_accepts_strings_and_objects():
    scheme = classify.normalize_scheme(['奇幻', {'name': '科幻', 'description': '未来'}])
    assert [item['path'] for item in scheme] == ['奇幻', '科幻']
    assert scheme[1]['description'] == '未来'


def test_normalize_scheme_drops_illegal_and_duplicates():
    scheme = classify.normalize_scheme(['奇幻', '奇幻', '..', 'notes', '科幻'])
    assert [item['path'] for item in scheme] == ['奇幻', '科幻']


def test_normalize_scheme_is_idempotent_for_nested_paths():
    """二次规范化不得丢父级路径。

    回归用例：规范化后的项里 `name` 只剩最后一段（`题材/奇幻` → `奇幻`），
    若二次规范化优先读 `name`，方案会从 `题材/奇幻` 退化成 `奇幻`，
    于是 AI 选出的合法分类被判「不在方案内」——整批作业全部失败。
    """
    once = classify.normalize_scheme([{'name': '题材/奇幻'}])
    assert once[0]['path'] == '题材/奇幻'

    twice = classify.normalize_scheme(once)
    assert twice[0]['path'] == '题材/奇幻', '二次规范化不得丢父级'

    # 关键后果：方案内的分类必须仍能被接受
    path, error = classify.resolve_choice('题材/奇幻', twice)
    assert error == '', '幂等性破坏会导致所有归类失败'
    assert path == '题材/奇幻'


def test_normalize_scheme_handles_garbage():
    for garbage in (None, 'x', 42, {}):
        assert classify.normalize_scheme(garbage) == []


def test_format_scheme_empty_returns_empty_string():
    """空方案不输出占位文案 —— 否则会被模型当成一个真实分类。"""
    assert classify.format_scheme([]) == ''
    assert classify.format_scheme(None) == ''


def test_format_scheme_includes_description_and_examples():
    text = classify.format_scheme([
        {'name': '奇幻', 'description': '剑与魔法', 'examples': ['勇者', '魔王']},
    ])
    assert '奇幻' in text
    assert '剑与魔法' in text
    assert '勇者' in text


# ------------------------------------------------------------------ AI 输出收敛


def test_choice_in_scheme_is_accepted():
    path, error = classify.resolve_choice('奇幻', SCHEME)
    assert error == ''
    assert path == '奇幻'


def test_choice_outside_scheme_is_rejected_by_default():
    """AI 顺手发明分类会让卡片散落到意外目录，默认不接受。"""
    path, error = classify.resolve_choice('赛博朋克', SCHEME)
    assert error
    assert path == ''


def test_choice_outside_scheme_accepted_when_allow_new():
    path, error = classify.resolve_choice('赛博朋克', SCHEME, allow_new=True)
    assert error == ''
    assert path == '赛博朋克'


def test_illegal_choice_is_rejected_even_with_allow_new():
    """安全校验不受 allow_new 影响 —— 路径穿越永远不许。"""
    path, error = classify.resolve_choice('../etc', SCHEME, allow_new=True)
    assert error
    assert path == ''


def test_empty_scheme_accepts_any_legal_category():
    path, error = classify.resolve_choice('任意分类', [])
    assert error == ''
    assert path == '任意分类'


def test_normalize_result_category_flags_low_confidence():
    decision = classify.normalize_result_category(
        {'category': '奇幻', 'confidence': 0.3}, SCHEME, confidence_min=0.6,
    )
    assert decision['category'] == '奇幻'
    assert decision['low_confidence'] is True
    assert any('低于' in item for item in decision['warnings'])


def test_normalize_result_category_rejects_unknown_category():
    decision = classify.normalize_result_category(
        {'category': '不存在', 'confidence': 0.9}, SCHEME,
    )
    assert decision['issues']
    assert decision['category'] == ''


def test_normalize_result_category_warns_on_missing_confidence():
    decision = classify.normalize_result_category({'category': '奇幻'}, SCHEME)
    assert decision['confidence'] is None
    assert any('未给出置信度' in item for item in decision['warnings'])


def test_normalize_result_category_handles_bad_confidence_type():
    decision = classify.normalize_result_category(
        {'category': '奇幻', 'confidence': '很高'}, SCHEME,
    )
    assert decision['confidence'] is None
    assert any('不是数字' in item for item in decision['warnings'])


def test_build_instruction_lists_scheme():
    text = classify.build_classify_instruction(SCHEME)
    assert '奇幻' in text
    assert '不要发明新分类' in text


def test_build_instruction_without_scheme_asks_for_free_form():
    text = classify.build_classify_instruction([])
    assert '不要发明新分类' not in text
    assert 'category' in text


def test_summarize_plan_groups_by_category():
    plan = classify.summarize_plan([
        {'card_id': 'A', 'category': '奇幻'},
        {'card_id': 'B', 'category': '奇幻'},
        {'card_id': 'C', 'category': '科幻', 'low_confidence': True},
    ])
    assert plan['total'] == 3
    assert plan['buckets'][0]['category'] == '奇幻'
    assert len(plan['buckets'][0]['card_ids']) == 2
    assert plan['buckets'][1]['low_confidence'] == 1


# ------------------------------------------------------------------ 内置任务规格


def test_builtin_classify_profile_is_valid():
    profile = profiles.builtin_profile('builtin-classify')
    assert profile is not None
    assert profiles.validate_profile(profile) == []


def test_classify_profile_does_not_hijack_model_matching():
    profile = profiles.builtin_profile('builtin-classify')
    assert profile['model_match'] == ''
    assert profiles.select_profile(
        profiles.builtin_profiles(), 'some-model')['id'] == 'builtin-default'


def test_classify_output_schema_is_small_and_focused():
    profile = profiles.builtin_profile('builtin-classify')
    assert set(profile['output_schema']['properties']) == {
        'category', 'confidence', 'reason',
    }
    assert profile['max_tokens'] == 256, '归类输出很短，不该给大额度'


# ------------------------------------------------------------------ 作业链路


@pytest.fixture
def env(tmp_path):
    store_module = load_backend_submodule('store')
    api = FakeApi(
        data_dir=str(tmp_path / 'data'),
        secrets={'api_key': 'sk-test'},
        cards={'A.png': {'data': {'name': 'A'}}, 'B.png': {'data': {'name': 'B'}}},
    )
    return api, store_module.PluginStore(api)


def _reply(category='奇幻', confidence=0.9):
    import json

    return json.dumps(
        {'category': category, 'confidence': confidence, 'reason': '有魔法设定'},
        ensure_ascii=False,
    )


def test_run_classify_moves_cards_by_category(env, monkeypatch):
    api, store = env
    moves = []
    api.cards.move = lambda ids, target: (
        moves.append((list(ids), target)) or
        {'count': len(ids), 'moved': [], 'failed': [], 'category_counts': {}}
    )
    api.cards.create_category = lambda name, parent='': True

    monkeypatch.setattr(jobs.llm, 'chat', lambda messages, **kwargs: {
        'content': _reply('奇幻'), 'usage': {'prompt_tokens': 50, 'completion_tokens': 20},
    })

    ctx = FakeCtx(
        [{'key': 'A.png', 'payload': {'card_id': 'A.png'}},
         {'key': 'B.png', 'payload': {'card_id': 'B.png'}}],
        payload={'provider_id': 'openai', 'apply_mode': 'auto', 'scheme': SCHEME},
    )
    jobs.run_classify(ctx, api=api, store=store)

    assert ctx.summary['applied'] == 2
    assert ctx.summary['moved'] == 2
    # 批量：两张卡一次移动
    assert len(moves) == 1
    assert moves[0][1] == '奇幻'
    assert sorted(moves[0][0]) == ['A.png', 'B.png']


def test_run_classify_review_mode_does_not_move(env, monkeypatch):
    api, store = env
    moves = []
    api.cards.move = lambda ids, target: moves.append(target) or {'count': 0}

    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': _reply(), 'usage': {},
    })

    ctx = FakeCtx([{'key': 'A.png', 'payload': {'card_id': 'A.png'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'review',
                           'scheme': SCHEME})
    jobs.run_classify(ctx, api=api, store=store)

    assert ctx.summary['awaiting_review'] == 1
    assert moves == []


def test_run_classify_rejects_category_outside_scheme(env, monkeypatch):
    api, store = env
    moves = []
    api.cards.move = lambda ids, target: moves.append(target) or {'count': 0}

    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': _reply('模型自创的分类'), 'usage': {},
    })

    ctx = FakeCtx([{'key': 'A.png', 'payload': {'card_id': 'A.png'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto',
                           'scheme': SCHEME})
    jobs.run_classify(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1
    assert moves == [], '方案外的分类不得落地'
    assert any('不在方案内' in item for item in ctx.reports[0]['result']['issues'])


def test_run_classify_skips_when_already_in_target(env, monkeypatch):
    api, store = env
    moves = []
    api.cards.move = lambda ids, target: moves.append(target) or {'count': 0}

    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': _reply('奇幻'), 'usage': {},
    })

    ctx = FakeCtx([{'key': 'A.png', 'payload': {'card_id': 'A.png', 'category': '奇幻'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto', 'scheme': SCHEME})
    jobs.run_classify(ctx, api=api, store=store)

    assert ctx.reports[0]['result']['skipped'] == 'unchanged'
    assert moves == [], '幂等：已在目标分类不重复移动'


def test_run_classify_dry_run_does_not_move(env, monkeypatch):
    api, store = env
    moves = []
    api.cards.move = lambda ids, target: moves.append(target) or {'count': 0}

    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': _reply(), 'usage': {},
    })

    ctx = FakeCtx([{'key': 'A.png', 'payload': {'card_id': 'A.png'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto',
                           'dry_run': True, 'scheme': SCHEME})
    jobs.run_classify(ctx, api=api, store=store)

    assert moves == []
    assert ctx.reports[0]['result']['dry_run'] is True


def test_run_classify_reports_validation_failure(env, monkeypatch):
    api, store = env
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': '完全不是 JSON', 'usage': {},
    })

    ctx = FakeCtx([{'key': 'A.png', 'payload': {'card_id': 'A.png'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto', 'scheme': SCHEME})
    jobs.run_classify(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1


def test_run_classify_surfaces_move_failures(env, monkeypatch):
    api, store = env
    api.cards.create_category = lambda name, parent='': True
    api.cards.move = lambda ids, target: {
        'count': 0, 'moved': [],
        'failed': [{'old_id': 'A.png', 'msg': '目标已存在同名卡'}],
        'category_counts': {},
    }

    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': _reply(), 'usage': {},
    })

    ctx = FakeCtx([{'key': 'A.png', 'payload': {'card_id': 'A.png'}}],
                  payload={'provider_id': 'openai', 'apply_mode': 'auto', 'scheme': SCHEME})
    jobs.run_classify(ctx, api=api, store=store)

    assert ctx.summary['move_errors']
    assert any('A.png' in item for item in ctx.summary['move_errors'])


def test_manifest_declares_classify_job():
    import json
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'manifest.json'), encoding='utf-8') as handle:
        manifest = json.load(handle)
    assert 'ai.classify' in manifest['backend']['jobs']
