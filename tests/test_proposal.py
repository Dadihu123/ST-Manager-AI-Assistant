"""结构化提议（Agent 阶段一）：采样、提议收敛、作业链路。

这是**元任务**：输入是整个（采样后的）卡片库，输出是分类体系与标签池设计。
真实风险不在「AI 想不出来」，而在两个工程问题：

1. **上下文爆炸** —— 上千张卡不能整库塞进提示词，必须分层采样 + token 预算；
2. **提议没法安全落地** —— AI 产出的分类名可能是路径穿越，
   所以复用 `core.classify` 的路径校验，且只允许**创建**（目录 / 标签），不碰卡片。
"""

import json

import pytest

from conftest import load_backend_submodule

pytest.importorskip('flask', reason='装配层依赖 flask')

proposal = load_backend_submodule('core.proposal')
profiles = load_backend_submodule('core.profiles')
jobs = load_backend_submodule('jobs')

from fake_api import FakeApi, FakeCtx  # noqa: E402


# ------------------------------------------------------------------ 采样预算


def _row(card_id, category='', tags=None, name=None, excerpt=''):
    return {
        'card_id': card_id,
        'category': category,
        'tags': list(tags or []),
        'brief': {
            'name': name or card_id,
            'tags': list(tags or []),
            'excerpt': excerpt,
        },
    }


def test_sample_respects_size_limit():
    rows = [_row(f'c{i}.png', category=f'cat{i % 3}') for i in range(100)]
    sample = proposal.stratified_sample(rows, options={'sample_size': 10})
    assert sample['sampled'] == 10
    assert sample['total'] == 100


def test_sample_respects_token_budget():
    """预算是硬约束：宁可少采，也不能超。"""
    rows = [
        _row(f'c{i}.png', name='很长的卡片名字' * 10, excerpt='很长的简介内容' * 30)
        for i in range(100)
    ]
    sample = proposal.stratified_sample(
        rows, options={'sample_size': 100, 'sample_budget_tokens': 500},
    )
    assert sample['tokens'] <= 500
    assert sample['sampled'] < 100
    assert sample['truncated'] is True


def test_sample_is_stratified_across_categories():
    """分类数量悬殊时也要覆盖小类 —— 否则小类在方案里会消失。"""
    rows = [_row(f'big{i}.png', category='大类') for i in range(50)]
    rows += [_row(f'small{i}.png', category='小类') for i in range(2)]

    sample = proposal.stratified_sample(rows, options={'sample_size': 10})
    coverage = sample['category_coverage']
    assert coverage.get('小类', 0) >= 1, '小类必须被采样到'
    assert coverage.get('大类', 0) >= 1


def test_sample_handles_empty_library():
    sample = proposal.stratified_sample([], options=None)
    assert sample['items'] == []
    assert sample['sampled'] == 0


def test_sample_handles_garbage_input():
    for garbage in (None, 'x', 42):
        sample = proposal.stratified_sample(garbage, options=None)
        assert sample['sampled'] == 0


def test_normalize_options_clamps_ranges():
    opts = proposal.normalize_options({'sample_size': 9999, 'sample_budget_tokens': -5})
    assert opts['sample_size'] == 200
    assert opts['sample_budget_tokens'] == 200


def test_normalize_options_handles_garbage():
    for garbage in (None, 'x', 42):
        opts = proposal.normalize_options(garbage)
        assert opts['sample_size'] == proposal.DEFAULT_SAMPLE_SIZE


# ------------------------------------------------------------------ 提示词片段


def test_format_sample_is_compact_one_block_per_card():
    text = proposal.format_sample([
        _row('A.png', category='奇幻', tags=['魔法'], name='爱丽丝', excerpt='一个法师'),
    ])
    assert '1. 爱丽丝' in text
    assert '魔法' in text
    assert '奇幻' in text
    assert '一个法师' in text


def test_format_sample_empty_returns_empty_string():
    assert proposal.format_sample([]) == ''
    assert proposal.format_sample(None) == ''


def test_format_existing_context_includes_categories_and_tags():
    text = proposal.format_existing_context(['奇幻', '科幻'], ['魔法', '太空'])
    assert '奇幻' in text
    assert '魔法' in text


def test_format_existing_context_empty_returns_empty_string():
    assert proposal.format_existing_context([], []) == ''


# ------------------------------------------------------------------ 提议收敛：分类


def test_category_proposal_is_validated_against_path_safety():
    """AI 产出的分类名绝不能直接当目录用。"""
    categories, warnings = proposal.normalize_category_proposal([
        {'name': '题材/奇幻'},
        {'name': '../etc'},
        {'name': 'notes'},
        {'name': 'a:b'},
        {'name': '根目录'},
    ])
    assert [item['path'] for item in categories] == ['题材/奇幻']
    assert len(warnings) == 4, '每个被丢弃的都要给原因'


def test_category_proposal_dedupes():
    categories, _warnings = proposal.normalize_category_proposal([
        {'name': '奇幻'}, {'name': '奇幻'},
    ])
    assert len(categories) == 1


def test_category_proposal_caps_count():
    raw = [{'name': f'c{i}'} for i in range(100)]
    categories, _warnings = proposal.normalize_category_proposal(raw)
    assert len(categories) == proposal.MAX_CATEGORIES


def test_category_proposal_marks_existing():
    categories, _warnings = proposal.normalize_category_proposal(
        [{'name': '奇幻'}, {'name': '新分类'}], existing_categories=['奇幻'],
    )
    by_path = {item['path']: item for item in categories}
    assert by_path['奇幻']['existing'] is True
    assert by_path['新分类']['existing'] is False


def test_category_proposal_truncates_examples_and_tags():
    categories, _warnings = proposal.normalize_category_proposal([{
        'name': '奇幻',
        'examples': [f'卡{i}' for i in range(30)],
        'suggested_tags': [f'标签{i}' for i in range(60)],
    }])
    assert len(categories[0]['examples']) == proposal.MAX_EXAMPLES
    assert len(categories[0]['suggested_tags']) == proposal.MAX_SUGGESTED_TAGS


# ------------------------------------------------------------------ 提议收敛：标签合并


def test_tag_merges_normalize():
    merges, warnings = proposal.normalize_tag_merges([
        {'from': ['学园', '学校'], 'to': '校园', 'reason': '近义'},
    ])
    assert merges[0]['to'] == '校园'
    assert set(merges[0]['from']) == {'学园', '学校'}
    assert warnings == []


def test_tag_merges_drop_self_reference():
    merges, _warnings = proposal.normalize_tag_merges([
        {'from': ['校园'], 'to': '校园'},
    ])
    assert merges == [], '自己映射到自己无意义'


def test_tag_merges_require_target_and_sources():
    merges, warnings = proposal.normalize_tag_merges([
        {'from': ['a']},              # 缺 to
        {'to': 'b'},                  # 缺 from
    ])
    assert merges == []
    assert len(warnings) == 2


def test_tag_merges_flag_new_target():
    """目标不在现有标签库时提示（不拦 —— AI 可能正是在提议新规范名）。"""
    merges, _warnings = proposal.normalize_tag_merges(
        [{'from': ['a'], 'to': '全新规范名'}], known_tags={'a', 'b'},
    )
    assert merges[0]['new_target'] is True


def test_tag_merges_cap_count():
    raw = [{'from': [f'x{i}'], 'to': f'y{i}'} for i in range(100)]
    merges, _warnings = proposal.normalize_tag_merges(raw)
    assert len(merges) == proposal.MAX_MERGES


# ------------------------------------------------------------------ 完整提议


def test_normalize_proposal_computes_new_items():
    result = proposal.normalize_proposal(
        {
            'categories': [
                {'name': '奇幻', 'suggested_tags': ['魔法', '剑']},
                {'name': '已有分类', 'suggested_tags': ['魔法']},
            ],
            'rationale': '按题材分',
        },
        existing_categories=['已有分类'],
        known_tags=['剑'],
    )

    assert len(result['categories']) == 2
    assert result['new_categories'] == ['奇幻']
    assert result['suggested_tags'] == ['魔法', '剑'], '跨分类去重'
    assert result['new_tags'] == ['魔法'], '排除已有标签'
    assert result['issues'] == []


def test_normalize_proposal_flags_empty_result():
    result = proposal.normalize_proposal({})
    assert result['issues'], '空提议要报出来，不能静默成功'


def test_normalize_proposal_handles_garbage():
    result = proposal.normalize_proposal('not a dict')
    assert result['categories'] == []
    assert result['issues']


# ------------------------------------------------------------------ 落地计划


def test_build_apply_plan_only_creates():
    plan = proposal.build_apply_plan({
        'categories': [
            {'path': '奇幻', 'existing': False},
            {'path': '已有', 'existing': True},
        ],
        'suggested_tags': ['魔法', '剑'],
        'new_tags': ['魔法'],
        'tag_merges': [{'from': ['a'], 'to': 'b'}],
    })

    assert plan['create_categories'] == ['奇幻']
    assert plan['existing_categories'] == ['已有']
    assert plan['create_tags'] == ['魔法']
    assert plan['known_tags'] == ['剑']
    # 合并建议会改卡片标签 → 必须人工确认
    assert plan['requires_manual_review'] is True


def test_apply_plan_without_merges_needs_no_review():
    plan = proposal.build_apply_plan({'categories': [], 'tag_merges': []})
    assert plan['requires_manual_review'] is False


# ------------------------------------------------------------------ 任务规格


def test_builtin_propose_profile_is_valid():
    profile = profiles.builtin_profile('builtin-propose')
    assert profile is not None
    assert profiles.validate_profile(profile) == []


def test_propose_profile_does_not_hijack_model_matching():
    profile = profiles.builtin_profile('builtin-propose')
    assert profile['model_match'] == ''
    assert profiles.select_profile(
        profiles.builtin_profiles(), 'some-model')['id'] == 'builtin-default'


def test_propose_profile_has_room_for_large_output():
    """提议要输出几十个分类 + 标签，额度不能小。"""
    profile = profiles.builtin_profile('builtin-propose')
    assert profile['max_tokens'] >= 2048
    assert 'categories' in profile['output_schema']['required']


# ------------------------------------------------------------------ 作业链路


@pytest.fixture
def env(tmp_path):
    store_module = load_backend_submodule('store')
    cards = {
        f'card{i}.png': {'data': {
            'name': f'卡{i}', 'tags': ['奇幻'] if i % 2 else ['科幻'],
            'description': f'第 {i} 张卡的简介',
        }}
        for i in range(6)
    }
    api = FakeApi(
        data_dir=str(tmp_path / 'data'),
        secrets={'api_key': 'sk-test'},
        cards=cards,
        tag_library={'tags': ['奇幻', '剑'], 'taxonomy': {}, 'prefs': {}},
    )
    api.cards.list = lambda **kwargs: {
        'items': [
            {'id': cid, 'char_name': data['data']['name'],
             'tags': data['data']['tags'], 'category': ''}
            for cid, data in cards.items()
        ],
        'total': len(cards),
        'index_ready': True,
    }
    api.cards.list_categories = lambda: ['已有分类']
    return api, store_module.PluginStore(api)


PROPOSAL_REPLY = {
    'categories': [
        {'name': '题材/奇幻', 'description': '剑与魔法', 'examples': ['卡1'],
         'suggested_tags': ['魔法', '剑'], 'card_count_estimate': 3},
        {'name': '题材/科幻', 'description': '未来科技', 'examples': ['卡0'],
         'suggested_tags': ['太空'], 'card_count_estimate': 3},
    ],
    'tag_merges': [{'from': ['学园', '学校'], 'to': '校园', 'reason': '近义'}],
    'rationale': '按题材分两类',
}


def test_run_propose_produces_reviewable_result(env, monkeypatch):
    api, store = env
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': json.dumps(PROPOSAL_REPLY, ensure_ascii=False),
        'usage': {'prompt_tokens': 900, 'completion_tokens': 300},
    })

    ctx = FakeCtx([{'key': 'proposal', 'payload': {}}],
                  payload={'provider_id': 'openai', 'options': {'sample_size': 5}})
    jobs.run_propose(ctx, api=api, store=store)

    assert ctx.summary['awaiting_review'] == 1
    assert ctx.summary['categories'] == 2
    assert ctx.summary['tag_merges'] == 1

    result = ctx.reports[0]['result']
    assert [c['path'] for c in result['categories']] == ['题材/奇幻', '题材/科幻']
    assert result['plan']['create_categories'] == ['题材/奇幻', '题材/科幻']
    assert result['plan']['create_tags'] == ['魔法', '太空'], '剑已在库里，不算新标签'
    assert result['plan']['requires_manual_review'] is True

    # 采样统计要回传（用户需要知道方案基于多少样本）
    assert result['sample_meta']['sampled'] <= 5
    assert result['sample_meta']['total'] == 6


def test_run_propose_rejects_unsafe_categories(env, monkeypatch):
    api, store = env
    reply = {
        'categories': [
            {'name': '../etc/passwd'},
            {'name': 'notes'},
            {'name': '合法分类'},
        ],
    }
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': json.dumps(reply, ensure_ascii=False), 'usage': {},
    })

    ctx = FakeCtx([{'key': 'proposal', 'payload': {}}],
                  payload={'provider_id': 'openai'})
    jobs.run_propose(ctx, api=api, store=store)

    result = ctx.reports[0]['result']
    assert [c['path'] for c in result['categories']] == ['合法分类']
    assert len(result['warnings']) >= 2, '被丢弃的分类要有原因'


def test_run_propose_reports_empty_library(env, monkeypatch):
    api, store = env
    api.cards.list = lambda **kwargs: {'items': [], 'total': 0, 'index_ready': True}
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {'content': '{}', 'usage': {}})

    ctx = FakeCtx([{'key': 'proposal', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_propose(ctx, api=api, store=store)

    assert ctx.summary['failed'] == 1
    assert ctx.reports[0]['error'] == 'empty_library'


def test_run_propose_reports_parse_failure(env, monkeypatch):
    api, store = env
    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': '完全不是 JSON', 'usage': {},
    })

    ctx = FakeCtx([{'key': 'proposal', 'payload': {}}], payload={'provider_id': 'openai'})
    with pytest.raises(Exception):
        jobs.run_propose(ctx, api=api, store=store)

    assert ctx.reports[0]['status'] == 'failed'


def test_run_propose_does_not_touch_cards(env, monkeypatch):
    """提议阶段只产出方案，绝不写卡片。"""
    api, store = env
    writes = []
    api.cards.apply_tags = lambda *a, **k: writes.append('tags') or {}
    api.cards.write_fields = lambda *a, **k: writes.append('fields') or True
    api.cards.move = lambda *a, **k: writes.append('move') or {'count': 0}

    monkeypatch.setattr(jobs.llm, 'chat', lambda *a, **k: {
        'content': json.dumps(PROPOSAL_REPLY, ensure_ascii=False), 'usage': {},
    })

    ctx = FakeCtx([{'key': 'proposal', 'payload': {}}], payload={'provider_id': 'openai'})
    jobs.run_propose(ctx, api=api, store=store)

    assert writes == [], '提议阶段不得写卡片'


def test_manifest_declares_propose_job():
    import os

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, 'manifest.json'), encoding='utf-8') as handle:
        manifest = json.load(handle)
    assert 'ai.propose' in manifest['backend']['jobs']
