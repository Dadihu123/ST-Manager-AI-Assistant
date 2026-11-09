"""标签池（`backend/core/tagpool.py`）：来源、预算、相关性、硬过滤、别名还原。

核心区分（容易搞错，专门有用例）：
**「不在候选集里」≠「被禁止」**。前者是陌生标签，该进建议队列；
后者是隔离/排除分类，必须挡下。把这两件事混起来会导致标签池一设预算
就把大量合法标签误判成违规。
"""

import time

import pytest

from conftest import load_backend_submodule

tagpool = load_backend_submodule('core.tagpool')

LIBRARY = {
    'tags': ['奇幻', '冒险', '校园', '恋爱', 'R18', '血腥'],
    'taxonomy': {
        'tag_to_category': {
            '奇幻': '题材', '冒险': '题材',
            '校园': '场景', '恋爱': '场景',
            'R18': '成人', '血腥': '成人',
        },
        'categories': {'题材': [], '场景': [], '成人': []},
    },
    'prefs': {'tag_blacklist': ['血腥']},
    'isolated_categories': ['成人'],
}


# ------------------------------------------------------------------ 规范化


def test_default_pool_matches_legacy_behaviour():
    pool = tagpool.default_pool()
    assert pool['source'] == tagpool.SOURCE_LIBRARY
    assert pool['respect_isolated'] is True
    assert pool['budget_tokens'] == tagpool.DEFAULT_BUDGET_TOKENS
    assert pool['allow_new_tags'] is True


def test_normalize_pool_rejects_garbage():
    pool = tagpool.normalize_pool({
        'source': '不存在的来源',
        'budget_tokens': -100,
        'max_new_tags': 999,
        'categories_include': ['a', 'a', ''],
    })
    assert pool['source'] == tagpool.SOURCE_LIBRARY
    assert pool['budget_tokens'] == 0
    assert pool['max_new_tags'] == 20
    assert pool['categories_include'] == ['a']


def test_profile_pool_prefers_profile_over_config():
    config = {'tag_pool': {'budget_tokens': 500}}
    profile = {'tag_pool': {'budget_tokens': 900}}
    assert tagpool.profile_pool(profile, config)['budget_tokens'] == 900


def test_profile_pool_falls_back_to_config():
    config = {'tag_pool': {'budget_tokens': 500}}
    assert tagpool.profile_pool({}, config)['budget_tokens'] == 500


# ------------------------------------------------------------------ 列表规范化
#
# `classify_tags()` 每卡要跑好几次，它第一件事就是把标签列表规范化（去重 +
# strip）。旧实现用 `text not in result`（list 线性查找）去重，3000 个标签时
# 单次 28.85ms —— 成本 ∝ N²，被每卡 4–5 次调用放大成整个作业的主要本地开销。
# 下面这组是**等价性钉子**：换成 set 去重后，这些输入必须一个字节都不变。


@pytest.mark.parametrize('raw', [[], (), set(), None, '不是列表', 12, {'a': 1}])
def test_tag_list_normalization_rejects_non_sequence(raw):
    """非 list/tuple/set 一律当空（`str`/`dict` 有自己的一套语义，不收）。"""
    assert tagpool.classify_tags({'tags': raw})['tags'] == []


@pytest.mark.parametrize('raw, expected', [
    (['奇幻', '奇幻', '冒险'], ['奇幻', '冒险']),
    ([' 奇幻 ', '奇幻', ' 冒险'], ['奇幻', '冒险']),
    ([None, '', '   ', '奇幻'], ['奇幻']),
    (('冒险', '奇幻', '冒险'), ['冒险', '奇幻']),
    # 0 / False 是假值，`str(item or '')` 之后是空串 —— 与 '0' 不是一回事
    (['0', 0, False], ['0']),
])
def test_tag_list_normalization_keeps_first_occurrence_order(raw, expected):
    """去重保留**首次出现**的顺序，且先 strip 再去重。"""
    assert tagpool.classify_tags({'tags': raw})['tags'] == expected


def test_tag_list_normalization_accepts_set_input():
    result = tagpool.classify_tags({'tags': {'奇幻', '冒险', '奇幻'}})['tags']
    assert sorted(result) == ['冒险', '奇幻']


def test_classify_tags_cost_stays_linear_in_tag_count():
    """性能守卫：分类成本必须随标签数线性增长。

    旧实现（list 上线性查找去重）3000 个标签单次 ~27ms、6000 个 ~107ms；
    而它在每张卡上被调用 4–5 次。阈值放到 25ms（本机修好后 ~1ms，36× 余量），
    只为拦住「谁又把它写回 O(N²)」，不做精确基准。
    """
    tags = [f'标签{index:05d}' for index in range(6000)]
    library = {'tags': tags, 'taxonomy': {}, 'prefs': {}}

    started = time.perf_counter()
    index = tagpool.classify_tags(library)
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert index['tags'] == tags
    assert elapsed_ms < 25, f'6000 标签分类耗时 {elapsed_ms:.1f}ms，疑似退回 O(N²) 去重'


# ------------------------------------------------------------------ 分类与隔离


def test_classify_tags_builds_indexes():
    index = tagpool.classify_tags(LIBRARY)
    assert index['tag_to_category']['奇幻'] == '题材'
    assert index['isolated'] == ['成人']
    assert index['blacklist'] == ['血腥']


def test_isolated_categories_become_hard_filter():
    """隔离分类必须真的挡住，而不是只当变量给 AI 看。"""
    result = tagpool.resolve_candidates(LIBRARY, tagpool.default_pool())

    assert 'R18' not in result['allowed']
    assert '血腥' not in result['allowed']
    assert 'R18' in result['blocked']
    assert '隔离分类' in result['excluded']['R18']


def test_respect_isolated_can_be_disabled():
    pool = dict(tagpool.default_pool(), respect_isolated=False)
    result = tagpool.resolve_candidates(LIBRARY, pool)
    assert 'R18' in result['allowed'], '关掉 respect_isolated 后不再挡'


def test_categories_include_limits_candidates():
    pool = dict(tagpool.default_pool(), categories_include=['场景'])
    result = tagpool.resolve_candidates(LIBRARY, pool)
    assert set(result['allowed']) == {'校园', '恋爱'}


def test_unmapped_tags_belong_to_default_category():
    """没有分类映射的标签归入 `default_category`（宿主界面里的「未整理」）。

    否则「未整理」只统计显式映射的那部分（实测宿主 287、插件 12），
    「排除 未整理」也挡不住它们。
    """
    library = {
        'tags': ['奇幻', '散装标签'],
        'taxonomy': {
            'default_category': '未整理',
            'tag_to_category': {'奇幻': '题材'},
            'categories': {'题材': []},
        },
        'prefs': {},
    }

    index = tagpool.classify_tags(library)
    assert index['tag_to_category']['散装标签'] == '未整理'
    assert index['default_category'] == '未整理'
    assert '未整理' in index['categories']

    pool = dict(tagpool.default_pool(), categories_exclude=['未整理'])
    result = tagpool.resolve_candidates(library, pool)
    assert result['allowed'] == ['奇幻']
    assert result['excluded']['散装标签'] == '排除分类 未整理'
    assert '散装标签' in tagpool.protected_tags(library, pool), '默认分类被排除时也算冻结'


# ------------------------------------------------------------------ 每分类规则
#
# 用户要的新系统：去掉全局置信度，改成「每个分类一个阈值 + 一个数量」，
# 外加一个最大总数量；未设置的分类默认阈值 0.5、数量不限；数量 0 = 不选。


def test_normalize_rules_accepts_dict_and_json():
    rules = tagpool.normalize_rules({
        '人设': {'confidence_min': 0.6, 'max_tags': 3},
        '关系': {'max_tags': 0},
        '未设置数量': {'confidence_min': 0.4},
        '坏的': 'x',
        '超范围': {'confidence_min': 3, 'max_tags': -5},
    })
    assert rules['人设'] == {'confidence_min': 0.6, 'max_tags': 3}
    assert rules['关系'] == {'max_tags': 0}, '0 = 不选，必须保留'
    assert rules['未设置数量'] == {'confidence_min': 0.4}, '数量缺省 = 不限'
    assert '坏的' not in rules
    assert rules['超范围'] == {'confidence_min': 1.0, 'max_tags': 0}, '越界值被钳位'

    assert tagpool.normalize_rules('{"人设": {"max_tags": 2}}') == {'人设': {'max_tags': 2}}
    assert tagpool.normalize_rules('不是 JSON') == {}
    assert tagpool.normalize_rules(None) == {}


def test_rule_for_uses_default_confidence_and_unlimited_quota():
    rule = tagpool.rule_for({}, '人设')
    assert rule['confidence_min'] == 0.5
    assert rule['max_tags'] is None, '没设置 = 不限'

    rule = tagpool.rule_for({'人设': {'max_tags': 2}}, '人设')
    assert rule['confidence_min'] == 0.5
    assert rule['max_tags'] == 2


def test_rules_text_mentions_total_thresholds_and_quotas():
    text = tagpool.rules_text(
        {'人设': {'confidence_min': 0.6, 'max_tags': 3}, '关系': {'max_tags': 0}}, 8,
    )
    assert '最多 8 个' in text
    assert '人设' in text and '0.60' in text and '最多 3 个' in text
    assert '关系' in text and '不要输出任何标签' in text
    assert '其余分类' in text and '0.50' in text
    assert tagpool.rules_text({}, 0) == ''


def test_apply_rules_filters_by_category_threshold():
    tags = [
        {'name': '奇幻', 'confidence': 0.9},   # 题材
        {'name': '冒险', 'confidence': 0.6},   # 题材
        {'name': '校园', 'confidence': 0.4},   # 场景
    ]
    mapping = {'奇幻': '题材', '冒险': '题材', '校园': '场景'}
    result = tagpool.apply_rules(
        tags, tag_to_category=mapping,
        rules={'题材': {'confidence_min': 0.8}}, max_total=10,
    )
    assert [item['name'] for item in result['kept']] == ['奇幻']
    dropped = {item['name']: item['reason'] for item in result['dropped']}
    assert '0.60 < 0.80' in dropped['冒险']
    assert '0.40 < 0.50' in dropped['校园'], '未设置的分类用默认 0.5'


def test_apply_rules_quota_zero_disables_category_and_total_cap():
    tags = [
        {'name': '奇幻', 'confidence': 0.9}, {'name': '冒险', 'confidence': 0.8},
        {'name': '校园', 'confidence': 0.95}, {'name': '恋爱', 'confidence': 0.7},
    ]
    mapping = {'奇幻': '题材', '冒险': '题材', '校园': '场景', '恋爱': '场景'}

    # 题材限 1 个、场景 0 个（禁用）→ 只剩题材里置信度最高的
    result = tagpool.apply_rules(
        tags, tag_to_category=mapping,
        rules={'题材': {'max_tags': 1}, '场景': {'max_tags': 0}}, max_total=5,
    )
    assert [item['name'] for item in result['kept']] == ['奇幻']
    dropped = {item['name']: item['reason'] for item in result['dropped']}
    assert '超过 1 个' in dropped['冒险']
    assert '设为不选' in dropped['校园'] and '设为不选' in dropped['恋爱']

    # 总数上限再把结果砍到 1 个
    kept = tagpool.apply_rules(
        tags, tag_to_category=mapping, rules={}, max_total=1,
    )
    assert len(kept['kept']) == 1
    assert kept['kept'][0]['name'] == '校园', '按置信度从高到低截断'


def test_apply_rules_keeps_tags_without_confidence():
    """缺置信度的标签要**保留**（与原 validate_result 的行为一致）。

    模型偶尔漏写 `confidence`，或直接返回字符串数组（`{"tags":["A","B"]}`）。
    这种标签没有分数可比，丢掉等于因为一个字段格式偏差就清空全部标签；
    保留并让调用方告警，比静默清空安全。
    """
    result = tagpool.apply_rules(
        [{'name': '奇幻', 'confidence': None}],
        tag_to_category={'奇幻': '题材'}, rules={}, max_total=5,
    )
    assert [item['name'] for item in result['kept']] == ['奇幻']
    assert result['missing_confidence'] == ['奇幻'], '要回报给调用方去告警'
    assert result['dropped'] == []


def test_apply_rules_keeps_missing_confidence_but_still_applies_quota():
    """保留不等于免死：分类配额与总数上限仍然管它。"""
    result = tagpool.apply_rules(
        [{'name': 'A', 'confidence': 0.9}, {'name': 'B', 'confidence': None}],
        tag_to_category={'A': '题材', 'B': '题材'},
        rules={'题材': {'max_tags': 1}}, max_total=5,
    )
    assert [item['name'] for item in result['kept']] == ['A'], '高置信度优先保留'
    assert result['missing_confidence'] == [], 'B 没进最终结果，不该再报缺置信度'


def test_apply_rules_profile_floor_overrides_category_threshold():
    """档案自己的 min_confidence 与分类门槛取**更严格者**。

    只认分类规则会让用户自建档案里写的门槛完全失效（README 承诺它仍生效）。
    """
    tags = [{'name': 'A', 'confidence': 0.6}, {'name': 'B', 'confidence': 0.95}]
    loose = dict(tagpool.default_pool(), categories_exclude=[])
    # 分类规则只要求 0.2，但档案要求 0.9 -> 应该按 0.9
    result = tagpool.apply_rules(
        tags, tag_to_category={'A': '题材', 'B': '题材'},
        rules={'题材': {'confidence_min': 0.2}}, max_total=5, profile_floor=0.9,
    )
    assert [item['name'] for item in result['kept']] == ['B']
    assert '0.90' in result['dropped'][0]['reason']


def test_protected_tags_only_covers_excluded_or_isolated_categories():
    """冻结标签 = 明确「排除」或「隔离」的分类下的标签。

    只收窄「只取」白名单不等于冻结：那只是「这次不参与挑词」，
    用户并没有说那些标签固定不变。
    """
    frozen = tagpool.protected_tags(
        LIBRARY,
        dict(tagpool.default_pool(), categories_exclude=['题材'], categories_include=['成人']),
    )
    assert frozen == {'奇幻', '冒险', 'R18', '血腥'}, '题材被排除 + 成人是隔离分类'
    assert '校园' not in frozen, '场景只是不在白名单，不算冻结'

    off = dict(tagpool.default_pool(), categories_exclude=['题材'], respect_isolated=False)
    assert tagpool.protected_tags(LIBRARY, off) == {'奇幻', '冒险'}, '关掉隔离后不算冻结'

    # 隔离分类即使没写进「排除」也算冻结（respect_isolated 默认开）
    assert tagpool.protected_tags(LIBRARY, tagpool.default_pool()) == {'R18', '血腥'}


def test_categories_include_also_drops_uncategorized_tags():
    """设了「只取这些分类」后，没有分类的标签也必须被排除。

    否则未分类标签会漏进候选集，用户的观感就是「设了跟没设一样」。
    """
    library = {
        'tags': ['奇幻', '散装标签'],
        'taxonomy': {'tag_to_category': {'奇幻': '题材'}, 'categories': {'题材': []}},
        'prefs': {},
    }
    pool = dict(tagpool.default_pool(), categories_include=['题材'])
    result = tagpool.resolve_candidates(library, pool)

    assert result['allowed'] == ['奇幻']
    assert result['excluded']['散装标签'] == '无分类'
    assert '散装标签' in result['blocked']


def test_categories_exclude_removes_candidates():
    pool = dict(tagpool.default_pool(), categories_exclude=['题材'])
    result = tagpool.resolve_candidates(LIBRARY, pool)
    assert '奇幻' not in result['allowed']
    assert '排除分类' in result['excluded']['奇幻']


def test_manual_tags_include_and_exclude():
    pool = dict(
        tagpool.default_pool(),
        tags_include=['外部标签'],
        tags_exclude=['校园'],
    )
    result = tagpool.resolve_candidates(LIBRARY, pool)
    assert '外部标签' in result['allowed'], '手动追加即使不在库里也要放行'
    assert '校园' in result['blocked']
    assert result['excluded']['校园'] == '手动排除'


def test_source_manual_only_uses_manual_list():
    pool = dict(
        tagpool.default_pool(),
        source=tagpool.SOURCE_MANUAL,
        tags_include=['只有这个'],
    )
    result = tagpool.resolve_candidates(LIBRARY, pool)
    assert result['allowed'] == ['只有这个']


def test_source_categories_requires_category():
    """无分类的标签在 categories 来源下被排除。"""
    library = {'tags': ['无分类标签'], 'taxonomy': {}, 'prefs': {}}
    pool = dict(tagpool.default_pool(), source=tagpool.SOURCE_CATEGORIES)
    result = tagpool.resolve_candidates(library, pool)
    assert result['allowed'] == []
    assert result['excluded']['无分类标签'] == '无分类'


def test_source_categories_still_excludes_default_category_tags():
    """有 `default_category` 的标签库：回填进默认分类的仍算「未归类」。

    这是回归用例。`classify_tags()` 会把没有显式映射的标签回填成
    `default_category`，于是 `tag_to_category` 里人人都有分类，
    `source='categories'` 的 `if not category` 判断从此永远不成立 ——
    该模式静默退化成 `source='library'`，未归类标签全被放进来。
    """
    library = {
        'tags': ['奇幻', '散装标签'],
        'taxonomy': {
            'default_category': '未整理',
            'tag_to_category': {'奇幻': '题材'},
            'categories': {'题材': []},
        },
        'prefs': {},
    }
    categories_mode = tagpool.resolve_candidates(
        library, dict(tagpool.default_pool(), source=tagpool.SOURCE_CATEGORIES),
    )
    library_mode = tagpool.resolve_candidates(
        library, dict(tagpool.default_pool(), source=tagpool.SOURCE_LIBRARY),
    )

    assert categories_mode['allowed'] == ['奇幻'], '未归类的不能进「只取已归类」模式'
    assert categories_mode['excluded']['散装标签'] == '无分类'
    assert '散装标签' in library_mode['allowed'], 'library 模式仍然包含它'
    assert categories_mode['allowed'] != library_mode['allowed'], '两种来源不能退化成同一个'


def test_default_category_tags_are_still_protectable():
    """回填不影响「排除 未整理」——冻结保护必须挡住它们。"""
    library = {
        'tags': ['奇幻', '散装标签'],
        'taxonomy': {
            'default_category': '未整理',
            'tag_to_category': {'奇幻': '题材'},
            'categories': {'题材': []},
        },
        'prefs': {},
    }
    frozen = tagpool.protected_tags(
        library, dict(tagpool.default_pool(), categories_exclude=['未整理']),
    )
    assert frozen == {'散装标签'}


# ------------------------------------------------------------------ 预算


def test_budget_limits_listed_tags():
    """大预算列出全部，小预算只列一部分 —— 按 token 而不是条数。"""
    big = tagpool.build_pool_text(LIBRARY, dict(tagpool.default_pool(), budget_tokens=5000))
    small = tagpool.build_pool_text(LIBRARY, dict(tagpool.default_pool(), budget_tokens=20))

    assert len(big['listed']) >= len(small['listed'])
    assert small['dropped'], '小预算必须有被丢下的标签'
    assert small['tokens'] <= 20


def test_zero_budget_renders_nothing_but_keeps_candidates():
    result = tagpool.build_pool_text(LIBRARY, dict(tagpool.default_pool(), budget_tokens=0))
    assert result['text'] == ''
    assert result['listed'] == []
    # 关键：候选集与黑名单仍要给出，否则上层无法判断「哪些是被禁的」
    assert result['allowed']
    assert result['blocked']


def test_outline_layer_lists_categories_without_paying_for_tags():
    result = tagpool.build_pool_text(LIBRARY, tagpool.default_pool())
    assert '（分类概览）' in result['text']
    assert '题材' in result['outline']


def test_pool_text_mentions_dropped_count():
    result = tagpool.build_pool_text(LIBRARY, dict(tagpool.default_pool(), budget_tokens=30))
    if result['dropped']:
        assert '因预算未列出' in result['text']


def test_empty_library_yields_empty_text():
    result = tagpool.build_pool_text({'tags': []}, tagpool.default_pool())
    assert result['text'] == ''
    assert result['allowed'] == []


# ------------------------------------------------------------------ 相关性排序


def test_overlap_scoring_prefers_card_related_tags():
    card = {'data': {'name': '校园故事', 'description': '发生在校园里的恋爱故事'}}
    result = tagpool.build_pool_text(
        LIBRARY, dict(tagpool.default_pool(), budget_tokens=5000), card=card,
    )
    # 校园/恋爱与卡面文字重叠，应排在 冒险 之前
    listed = result['listed']
    assert listed.index('校园') < listed.index('冒险')


def test_card_vocabulary_extracts_cjk_bigrams_and_words():
    vocabulary = tagpool.card_text_vocabulary({'data': {'name': '校园RPG'}})
    assert '校园' in vocabulary
    assert 'rpg' in vocabulary


def test_card_vocabulary_handles_missing_card():
    assert tagpool.card_text_vocabulary(None) == set()
    assert tagpool.card_text_vocabulary({}) == set()


def test_usage_frequency_outranks_overlap():
    """频次是第一优先级（现有行为要保住）。"""
    library = {
        'tags': ['校园', '冒险'],
        'taxonomy': {},
        'prefs': {},
        'usage': {'冒险': 100, '校园': 0},
    }
    card = {'data': {'description': '校园'}}
    result = tagpool.build_pool_text(library, tagpool.default_pool(), card=card)
    assert result['listed'].index('冒险') < result['listed'].index('校园')


# ------------------------------------------------------------------ 别名还原


def test_restore_aliases_maps_to_canonical():
    tags = [{'name': '情感纠葛', 'confidence': 0.9}]
    restored, info = tagpool.restore_aliases(
        tags, [{'alias': '情感纠葛', 'canonical': 'NTR'}],
    )
    assert restored[0]['name'] == 'NTR'
    assert restored[0]['confidence'] == 0.9
    assert info['applied'] == [{'from': '情感纠葛', 'to': 'NTR'}]


def test_restore_aliases_follows_chain():
    restored, _info = tagpool.restore_aliases(
        ['A'], [{'alias': 'A', 'canonical': 'B'}, {'alias': 'B', 'canonical': 'C'}],
    )
    assert restored == ['C']


def test_restore_aliases_detects_cycle_without_hanging():
    restored, _info = tagpool.restore_aliases(
        ['A'], [{'alias': 'A', 'canonical': 'B'}, {'alias': 'B', 'canonical': 'A'}],
    )
    assert restored in (['A'], ['B']), '成环时停下，不追下去'


def test_restore_aliases_accepts_arrow_text():
    restored, _info = tagpool.restore_aliases(['成人向'], ['成人向→R18'])
    assert restored == ['R18']


def test_restore_aliases_leaves_canonical_untouched():
    """模型自己写出真实词时，原样通过（不该被二次映射）。"""
    restored, info = tagpool.restore_aliases(
        ['NTR'], [{'alias': '情感纠葛', 'canonical': 'NTR'}],
    )
    assert restored == ['NTR']
    assert info['applied'] == []


def test_restore_aliases_merges_duplicates_keeping_higher_confidence():
    restored, _info = tagpool.restore_aliases(
        [{'name': '情感纠葛', 'confidence': 0.6}, {'name': 'NTR', 'confidence': 0.9}],
        [{'alias': '情感纠葛', 'canonical': 'NTR'}],
    )
    assert len(restored) == 1
    assert restored[0]['confidence'] == 0.9


def test_restore_aliases_no_table_is_passthrough():
    restored, info = tagpool.restore_aliases(['A', 'B'], [])
    assert restored == ['A', 'B']
    assert info['applied'] == []


# ------------------------------------------------------------------ 别名校验


def test_validate_aliases_rejects_ambiguous_alias():
    errors = tagpool.validate_aliases([
        {'alias': 'A', 'canonical': 'B'},
        {'alias': 'A', 'canonical': 'C'},
    ])
    assert any('歧义' in item for item in errors)


def test_validate_aliases_rejects_alias_colliding_with_real_tag():
    """别名恰好是真实标签 → 出站会误替换真词。"""
    errors = tagpool.validate_aliases(
        [{'alias': '校园', 'canonical': '学园'}], known_tags={'校园', '学园'},
    )
    assert any('同名' in item for item in errors)


def test_validate_aliases_allows_many_to_one():
    """多个别名映射到同一 canonical 是合法的合并语义。"""
    errors = tagpool.validate_aliases([
        {'alias': 'A', 'canonical': 'X'},
        {'alias': 'B', 'canonical': 'X'},
    ])
    assert errors == []


def test_validate_aliases_rejects_identity_mapping():
    errors = tagpool.validate_aliases([{'alias': 'A', 'canonical': 'A'}])
    assert any('无意义' in item for item in errors)
