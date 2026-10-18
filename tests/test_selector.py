"""内容选择器与 token 明细。

覆盖三件事：
1. 选择器只**收敛**取值（关掉的字段变空串，但键仍存在 → 不误报 missing）；
2. 世界书的「指定条目 / 仅选定 / 不发」三种模式真的生效；
3. `by_block` 能把 token 归因到变量，并把剩余归为模板骨架。
"""

import pytest

from conftest import load_backend_submodule

selector = load_backend_submodule('core.selector')
prompt = load_backend_submodule('core.prompt')
trimming = load_backend_submodule('core.trimming')
tokens = load_backend_submodule('core.tokens')

CARD = {
    'data': {
        'name': '爱丽丝',
        'description': '一个流浪法师',
        'personality': '冷静、寡言',
        'scenario': '王都的雨夜',
        'first_mes': '「你也是来避雨的？」',
        'mes_example': '<START>\n{{user}}: 你好\n{{char}}: 嗯。',
        'creator': 'tester',
        'creator_notes': '测试用卡',
        'system_prompt': '保持角色',
        'post_history_instructions': '不要跳出角色',
        'tags': ['奇幻', '法师'],
        'alternate_greetings': ['第二条开场白内容', '第三条开场白内容'],
    },
}


# ------------------------------------------------------------------ 规范化


def test_default_selector_matches_legacy_behaviour():
    spec = selector.default_selector()
    assert spec['worldbook']['mode'] == selector.WI_AUTO
    assert spec['alternate_greetings']['mode'] == selector.GREETINGS_NONE
    assert spec['extras'] == []
    assert set(spec['card_fields']) == set(selector.SELECTABLE_CARD_FIELDS)


def test_normalize_selector_rejects_garbage():
    spec = selector.normalize_selector({
        'card_fields': ['description', '不存在的字段', 123],
        'extras': ['regex', 'nope'],
        'alternate_greetings': {'mode': '奇怪的模式', 'indexes': [1, 1, -1, 'x', 2]},
        'worldbook': {'mode': 'bad', 'entry_uids': ['a', 'a', 'b'], 'budget_tokens': -5},
    })
    assert spec['card_fields'] == ['description']
    assert spec['extras'] == ['regex']
    assert spec['alternate_greetings']['mode'] == selector.GREETINGS_NONE
    assert spec['alternate_greetings']['indexes'] == [1, 2]
    assert spec['worldbook']['mode'] == selector.WI_AUTO
    assert spec['worldbook']['entry_uids'] == ['a', 'b']
    assert spec['worldbook']['budget_tokens'] == 0


def test_empty_card_fields_means_only_name():
    spec = selector.normalize_selector({'card_fields': []})
    assert spec['card_fields'] == []
    assert selector.field_enabled(spec, 'name') is True
    assert selector.field_enabled(spec, 'description') is False


# ------------------------------------------------------------------ 收敛取值


def test_disabled_fields_become_empty_but_stay_present():
    """关掉的字段必须**给空串**而不是删键 —— 否则模板会误报 missing。"""
    variables = prompt.card_variables(
        CARD, {'card_fields': ['personality']},
    )
    assert variables['card.description'] == ''
    assert variables['card.personality'] == '冷静、寡言'
    # 键存在性：模板引用它不会触发 missing_required
    assert 'card.description' in variables
    # name 永远在
    assert variables['card.name'] == '爱丽丝'


def test_missing_name_is_omitted_entirely():
    """`card.name` 是真必填：没有就不进变量表，让渲染层报错。"""
    variables = prompt.card_variables({'data': {'description': 'x'}})
    assert 'card.name' not in variables


def test_alternate_greetings_modes():
    none = prompt.card_variables(CARD, {'alternate_greetings': {'mode': 'none'}})
    assert none['card.alternate_greetings'] == ''
    assert none['card.alternate_greetings_count'] == 2, '数量始终可读'

    all_of = prompt.card_variables(CARD, {'alternate_greetings': {'mode': 'all'}})
    assert '[第 1 条]' in all_of['card.alternate_greetings']
    assert '[第 2 条]' in all_of['card.alternate_greetings']

    picked = prompt.card_variables(
        CARD, {'alternate_greetings': {'mode': 'selected', 'indexes': [1]}},
    )
    assert '第三条开场白内容' in picked['card.alternate_greetings']
    assert '第二条开场白内容' not in picked['card.alternate_greetings']


def test_extensions_only_included_when_selected():
    extensions = {
        'regex_scripts': [{'scriptName': '去状态栏', 'findRegex': '/<状态>.*<\\/状态>/g'}],
        'tavern_helper': {
            'scripts': [{'name': '主逻辑', 'enabled': True, 'type': 'script'}],
            'variables': {'hp': 10, 'mp': 5},
        },
    }

    off = prompt.card_variables(CARD, {'extras': []}, extensions=extensions)
    assert off['card.regex_scripts'] == ''
    assert off['card.st_scripts'] == ''

    on = prompt.card_variables(CARD, {'extras': ['regex', 'st_scripts']}, extensions=extensions)
    assert '去状态栏' in on['card.regex_scripts']
    assert '主逻辑' in on['card.st_scripts']
    assert 'hp' in on['card.st_scripts']


def test_regex_summary_never_leaks_script_body():
    """摘要只含名称与匹配式，绝不能把 replaceString 正文塞进提示词。"""
    extensions = {'regex_scripts': [{
        'scriptName': 'R',
        'findRegex': '/a/g',
        'replaceString': '这是一大段绝不该出现在提示词里的替换正文',
    }]}
    text = prompt.regex_scripts_text(extensions)
    assert '替换正文' not in text


def test_local_note_only_included_when_selected():
    off = prompt.card_variables(CARD, {'extras': []}, local_note='本地写的备注')
    assert off['card.local_note'] == ''

    on = prompt.card_variables(CARD, {'extras': ['local_note']}, local_note='本地写的备注')
    assert on['card.local_note'] == '本地写的备注'


# ------------------------------------------------------------------ 世界书


ENTRIES = [
    {'uid': 1, 'comment': '世界观总纲', 'keys': ['世界'], 'content': '甲' * 300},
    {'uid': 2, 'comment': '无关条目', 'keys': ['杂'], 'content': '乙' * 300},
    {'uid': 3, 'comment': '停用条目', 'keys': ['停'], 'content': '丙' * 300, 'disable': True},
]


def test_worldbook_only_uids_limits_candidates():
    result = trimming.select_wi_excerpt(ENTRIES, 4000, only_uids=['2'])
    assert result['count'] == 1
    assert result['selected'] == ['无关条目']
    # 连索引里也不该出现别的条目
    assert '世界观总纲' not in result['outline']


def test_worldbook_pinned_entries_win_over_value_ranking():
    """勾选的条目必须**排在最前**，不能被「名字没命中关键词」挤到后面。"""
    entries = [
        {'uid': 1, 'comment': '世界观总纲', 'keys': ['世界'], 'content': '甲' * 200},
        {'uid': 2, 'comment': '我要的那条', 'keys': ['x'], 'content': '乙' * 200},
    ]
    # outline_ratio=0 去掉索引开销，预算只够装一条
    result = trimming.select_wi_excerpt(
        entries, 215, pinned_uids=['2'], outline_ratio=0.0,
    )
    assert result['selected'][0] == '我要的那条', 'pinned 必须排第一'
    assert result['pinned'] == ['我要的那条']
    # 预算不够的第二条被丢弃，而不是反过来
    assert '世界观总纲' in result['dropped']


def test_worldbook_reports_pinned_uid_that_does_not_exist():
    """勾选了但条目已删/停用 → 必须报出来，不能静默。"""
    result = trimming.select_wi_excerpt(ENTRIES, 4000, pinned_uids=['99'])
    assert result['pinned_missing'] == ['99']


def test_worldbook_pinned_disabled_entry_is_missing():
    result = trimming.select_wi_excerpt(ENTRIES, 4000, pinned_uids=['3'])
    assert '3' in result['pinned_missing']


# ------------------------------------------------------------------ 模板一致性


PROFILE = {
    'system': '你是助手。\n卡片={{ card.name }}\n简介={{ card.description }}',
    'user_template': '性格={{ card.personality }}',
}


def test_variable_usage_counts_references():
    usage = prompt.variable_usage(PROFILE)
    assert usage['hits'] == {'card.name': 1, 'card.description': 1, 'card.personality': 1}
    assert usage['referenced'] == ['card.description', 'card.name', 'card.personality']


def test_selector_gaps_warns_when_template_references_disabled_field():
    """模板要了简介、选择器却关了简介 → 会渲染成空，必须警告。"""
    spec = selector.normalize_selector({'card_fields': ['personality']})
    gaps = prompt.selector_gaps(spec, PROFILE)

    assert any('card.description' in item and '渲染为空' in item for item in gaps)


def test_selector_gaps_reports_selected_but_unreferenced():
    """选择器放行了角色卡字段、模板却没引用 → 白勾，仅提示。"""
    spec = selector.normalize_selector({'card_fields': ['personality', 'scenario']})
    variables = {'card.scenario': '王都的雨夜', 'card.personality': '冷静'}
    gaps = prompt.selector_gaps(spec, PROFILE, variables=variables)

    assert any('card.scenario' in item and '没有引用' in item for item in gaps)


def test_selector_gaps_clean_when_consistent():
    profile = {
        'system': '{{ card.name }} / {{ card.personality }}',
        'user_template': '无',
    }
    spec = selector.normalize_selector({'card_fields': ['personality']})
    gaps = prompt.selector_gaps(spec, profile, variables={'card.personality': '冷静'})
    assert gaps == []


def test_selector_gaps_ignores_always_available_metadata():
    """`alternate_greetings_count` 之类的元数据不算「白勾」——它们不占 token 也不受控。"""
    profile = {'system': '{{ card.name }}', 'user_template': 'x'}
    spec = selector.default_selector()
    variables = {
        'card.name': '爱丽丝',
        'card.alternate_greetings_count': 2,
        'worldbook.count': 1,
        'worldbook.total_tokens': 300,
    }
    gaps = prompt.selector_gaps(spec, profile, variables=variables)
    assert not any('alternate_greetings_count' in item for item in gaps)
    assert not any('worldbook.count' in item for item in gaps)


def test_profile_selector_prefers_profile_over_config():
    config = {'content_selector': {'card_fields': ['description']}}
    profile = {'content_selector': {'card_fields': ['personality']}}

    spec = prompt.profile_selector(profile, config)
    assert spec['card_fields'] == ['personality'], '档案应压过全局配置'


def test_profile_selector_falls_back_to_config_then_default():
    config = {'content_selector': {'card_fields': ['description']}}
    assert prompt.profile_selector({}, config)['card_fields'] == ['description']

    default = prompt.profile_selector({}, {})
    assert set(default['card_fields']) == set(selector.SELECTABLE_CARD_FIELDS)


# ------------------------------------------------------------------ token 明细


def test_estimate_blocks_attributes_tokens_to_variables():
    variables = {
        'card.name': '爱丽丝',
        'card.description': '一个流浪法师' * 20,
        'card.unused': '不该出现',
    }
    hits = {'card.name': 1, 'card.description': 1}

    blocks = tokens.estimate_blocks(hits, variables, total_tokens=500)

    names = [item['name'] for item in blocks['items']]
    assert names == ['card.description', 'card.name'], '按 token 降序'
    assert blocks['by_variable']['card.name'] == tokens.estimate('爱丽丝')
    assert 'card.unused' not in blocks['by_variable'], '未被模板引用就不计入'
    assert blocks['total'] == 500


def test_estimate_blocks_counts_repeated_variables():
    blocks = tokens.estimate_blocks({'card.name': 3}, {'card.name': '爱丽丝'})
    unit = tokens.estimate('爱丽丝')
    assert blocks['by_variable']['card.name'] == unit * 3


def test_estimate_blocks_separates_template_overhead():
    variables = {'card.name': 'A'}
    blocks = tokens.estimate_blocks({'card.name': 1}, variables, total_tokens=100)
    assert blocks['variables_total'] == tokens.estimate('A')
    assert blocks['template_overhead'] == 100 - tokens.estimate('A')


def test_estimate_blocks_marks_empty_variables():
    blocks = tokens.estimate_blocks({'card.scenario': 1}, {'card.scenario': ''})
    assert blocks['items'][0]['empty'] is True
    assert blocks['items'][0]['tokens'] == 0


def test_estimate_blocks_never_negative_overhead():
    """变量估算超过总数（误差）时，overhead 夹到 0 而不是负数。"""
    blocks = tokens.estimate_blocks({'card.name': 1}, {'card.name': '很长' * 100},
                                    total_tokens=1)
    assert blocks['template_overhead'] == 0
