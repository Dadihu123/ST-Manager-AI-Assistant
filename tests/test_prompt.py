"""提示词模板：变量扫描、插值、消息组装与限制合并。"""

import pytest

from stm_ai_core import parsing, profiles, prompt
from stm_ai_core.profiles import builtin_profile


def test_scan_template_returns_unique_names_in_order():
    template = '{{ card.name }} 与 {{ card.description }}，再来一次 {{ card.name }}'
    assert prompt.scan_template(template) == ['card.name', 'card.description']


def test_scan_template_ignores_plain_braces_and_bad_names():
    assert prompt.scan_template('{ 不是变量 } {{bad name}}') == []


def test_validate_template_reports_unknown_variables():
    assert prompt.validate_template('{{ card.name }}') == []
    assert prompt.validate_template('{{ card.nickname }}') == ['card.nickname']


def test_render_template_substitutes_values():
    result = prompt.render_template('你好，{{ card.name }}', {'card.name': 'Alice'})
    assert result['text'] == '你好，Alice'
    assert result['missing_required'] == []


def test_render_template_reports_required_missing():
    result = prompt.render_template('{{ card.name }}', {})
    assert result['text'] == ''
    assert result['missing_required'] == ['card.name']


def test_render_template_keeps_optional_missing_silent():
    result = prompt.render_template('{{ user.instruction }}', {})
    assert result['missing_required'] == []
    assert result['missing_optional'] == ['user.instruction']


def test_render_template_question_mark_forces_optional():
    result = prompt.render_template('{{ card.name? }}', {})
    assert result['missing_required'] == []
    assert result['missing_optional'] == ['card.name']


def test_render_template_empty_policy_downgrades_required():
    result = prompt.render_template('{{ card.name }}', {}, missing_policy='empty')
    assert result['missing_required'] == []
    assert result['text'] == ''


def test_render_template_empty_string_value_is_not_missing():
    result = prompt.render_template('[{{ card.description }}]', {'card.description': ''})
    assert result['text'] == '[]'
    assert result['missing_required'] == []
    assert result['missing_optional'] == []


def test_render_template_escapes_double_braces():
    result = prompt.render_template('{{{{ card.name }}}}', {'card.name': 'X'})
    assert result['text'] == '{{ card.name }}'


def test_render_template_reports_and_blanks_unknown_variable():
    result = prompt.render_template('{{ card.nickname }}', {'card.nickname': 'X'})
    assert result['unknown'] == ['card.nickname']
    assert result['text'] == ''


def test_render_template_stringifies_bool_float_and_list():
    result = prompt.render_template(
        '{{ worldbook.truncated }}|{{ limits.confidence_min }}|{{ card.tags }}',
        {'worldbook.truncated': True, 'limits.confidence_min': 0.5, 'card.tags': ['a', 'b']},
    )
    assert result['text'] == '是|0.5|a、b'


def test_format_instruction_mentions_rules():
    """生成约束从 output_schema 读出（与后置校验同源）。"""
    text = prompt.format_instruction({
        'json_only': True,
        'required': ['summary'],
        'properties': {
            'summary': {'type': 'string', 'max_chars': 200},
            'tags': {'type': 'array', 'max_items': 5, 'min_confidence': 0.5,
                     'item': {'name': {'type': 'string'}}},
        },
    })
    assert 'JSON' in text
    assert 'summary' in text
    assert '200' in text
    assert '5' in text
    assert '0.5' in text


def test_format_instruction_empty_when_no_rules():
    assert prompt.format_instruction({'json_only': False}) == ''


def test_format_instruction_and_validation_share_one_schema():
    """同一份 schema 既生成要求也做校验：不会出现「要求 200 字、校验按 300 判」。"""
    schema_spec = {
        'json_only': True,
        'required': ['summary'],
        'properties': {'summary': {'type': 'string', 'max_chars': 50}},
    }
    instruction = prompt.format_instruction(schema_spec)
    assert '50' in instruction

    # 同一份声明下，60 字会被裁到 50
    checked = parsing.validate_result({'summary': '甲' * 60, 'tags': []}, schema_spec)
    assert len(checked['result']['summary']) <= 50


def test_render_messages_orders_prelude_then_format_then_suffix():
    """顺序断言用合成档案：内置档案含条件块，原始模板字符串不再是渲染结果的子串。"""
    profile = {
        'system_prelude': 'PRELUDE_MARK',
        'system': 'SYSTEM_MARK',
        'system_suffix': 'SUFFIX_MARK',
        'user_template': 'U',
        'format_rules': {'json_only': True},
        'missing_policy': 'empty',
    }
    result = prompt.render_messages(profile, {})
    system = result['messages'][0]['content']

    assert result['messages'][0]['role'] == 'system'
    assert result['messages'][1]['role'] == 'user'
    assert system.index('PRELUDE_MARK') < system.index('SYSTEM_MARK')
    assert system.index('SYSTEM_MARK') < system.index('输出要求：')
    assert system.index('输出要求：') < system.index('SUFFIX_MARK')


def test_builtin_profiles_render_without_leftover_template_tags():
    """所有内置档案渲染后都不得残留 `{{`（条件块必须被消费掉）。"""
    empty_vars = prompt.collect_variables(
        card={'name': 'A'}, model_name='m', profile_id='p',
    )
    for profile in profiles.builtin_profiles():
        result = prompt.render_messages(profile, empty_vars)
        for message in result['messages']:
            assert '{{' not in message['content'], (
                f'{profile["id"]} 的 {message["role"]} 消息残留模板标签'
            )


def test_render_messages_appends_assistant_prefill():
    profile = builtin_profile('builtin-default')
    profile['assistant_prefill'] = '{'
    variables = prompt.collect_variables(card={'name': 'A'})
    result = prompt.render_messages(profile, variables)
    assert result['messages'][-1] == {'role': 'assistant', 'content': '{'}


def test_render_messages_surfaces_missing_variables():
    profile = builtin_profile('builtin-default')
    profile['user_template'] = '{{ user.instruction }}'
    result = prompt.render_messages(profile, {})
    assert result['missing_optional'] == ['user.instruction']


# ------------------------------------------------------------------ 空值清理


def test_empty_value_does_not_leave_dangling_label():
    """关闭的字段渲染成空串后，**不能**在提示词里留下 `简介=` 这种残句。

    这既是白费 token，也会让模型以为「简介本来就为空」而不是「没提供」。
    """
    out = prompt.render_template(
        '简介={{ card.description }}\n性格={{ card.personality }}',
        {'card.description': '', 'card.personality': '冷静'},
    )
    assert '简介' not in out['text'], '空值所在的标签行应整行消失'
    assert out['text'] == '性格=冷静'


def test_dangling_label_with_colon_and_bullet_is_removed():
    for line in ('场景：{{ card.scenario }}', '场景:{{ card.scenario }}', '- 场景：{{ card.scenario }}'):
        out = prompt.render_template(line, {'card.scenario': ''})
        assert out['text'] == '', f'未清理: {line!r}'


def test_hardcoded_label_line_is_kept():
    """模板里写死的说明文字不能被误吞（它没有引用变量）。"""
    out = prompt.render_template('注意：以下是卡片信息\n简介={{ card.description }}',
                                 {'card.description': 'x'})
    assert '注意：以下是卡片信息' in out['text']


def test_line_with_other_content_is_kept_even_if_value_empty():
    """行内还有别的实际内容时，不能整行丢掉。"""
    out = prompt.render_template('简介={{ card.description }}（共 3 行）',
                                 {'card.description': ''})
    assert out['text'] == '简介=（共 3 行）'


def test_consecutive_blank_lines_are_collapsed():
    out = prompt.render_template('A\n\n\n\nB', {})
    assert out['text'] == 'A\n\nB'


# ------------------------------------------------------------------ 条件块


def test_section_block_omits_whole_area_when_empty():
    template = (
        '卡片={{ card.name }}\n'
        '{{# card.description }}\n简介：{{ card.description }}\n{{/ card.description }}\n'
        '结束'
    )
    out = prompt.render_template(template, {'card.name': 'A', 'card.description': ''})
    assert '简介' not in out['text']
    assert out['omitted'] == ['card.description']
    assert out['text'].startswith('卡片=A')
    assert out['text'].endswith('结束')


def test_section_block_renders_when_value_present():
    template = '{{# card.description }}简介：{{ card.description }}{{/ card.description }}'
    out = prompt.render_template(template, {'card.description': '一个法师'})
    assert out['text'] == '简介：一个法师'
    assert out['omitted'] == []


def test_section_block_treats_empty_list_as_empty():
    template = '{{# tags.isolated }}隔离：{{ tags.isolated }}{{/ tags.isolated }}'
    out = prompt.render_template(template, {'tags.isolated': []})
    assert out['text'] == ''
    assert out['omitted'] == ['tags.isolated']


def test_unclosed_section_block_raises():
    with pytest.raises(ValueError, match='没有对应的'):
        prompt.render_template('{{# card.description }}没闭合', {'card.name': 'A'})


def test_unknown_variable_in_section_is_reported():
    out = prompt.render_template('{{# nope.thing }}x{{/ nope.thing }}', {})
    assert out['text'] == ''
    assert out['unknown'] == ['nope.thing']


def test_scan_template_finds_variables_inside_section_blocks():
    """条件块里的变量名也要被扫描到，否则拼错时“校验变量”会漏过。"""
    template = '{{# card.description }}简介：{{ card.description }}{{/ card.description }}'
    assert prompt.scan_template(template) == ['card.description']


def test_validate_template_rejects_unknown_name_in_section_tag():
    assert prompt.validate_template('{{# card.descripiton }}x{{/ card.descripiton }}') == [
        'card.descripiton',
    ]


def test_nested_sections_do_not_leak_tags():
    """嵌套条件块：内层标签绝不能原样漏进提示词。

    回归用例：单次 `SECTION_PATTERN.sub()` 的非贪婪匹配会把「外层块的结束标签」
    当成本层结束，于是内层的 `{{/ x }}` 残留成可见文本。
    """
    template = (
        '{{# tags.library }}<lib>{{# tags.taxonomy }}分类：{{ tags.taxonomy }}{{/ tags.taxonomy }}'
        '</lib>{{/ tags.library }}'
    )
    out = prompt.render_template(template, {'tags.library': ['a'], 'tags.taxonomy': ''})

    assert '{{' not in out['text'], '不能有残留标签'
    assert out['text'] == '<lib></lib>'
    assert out['omitted'] == ['tags.taxonomy']


def test_nested_sections_render_inner_when_present():
    template = '{{# tags.library }}<lib>{{# tags.taxonomy }}分类：{{ tags.taxonomy }}{{/ tags.taxonomy }}' \
               '</lib>{{/ tags.library }}'
    out = prompt.render_template(template, {'tags.library': ['a'], 'tags.taxonomy': '奇幻'})
    assert out['text'] == '<lib>分类：奇幻</lib>'


# ------------------------------------------------------------------ 反向条件块


def test_inverted_section_renders_only_when_empty():
    """`{{^ x }}` 是 `{{# x }}` 的补集：值为空时才输出。"""
    template = '{{# tags.library }}有库{{/ tags.library }}{{^ tags.library }}空库{{/ tags.library }}'

    assert prompt.render_template(template, {'tags.library': ['a']})['text'] == '有库'
    assert prompt.render_template(template, {'tags.library': []})['text'] == '空库'
    # 键根本不存在也算空
    assert prompt.render_template(template, {})['text'] == '空库'


def test_inverted_section_does_not_report_omitted():
    """反向块是「备用文案」，命中时不该被算作 omitted（那是正向块的概念）。"""
    out = prompt.render_template('{{^ tags.library }}空{{/ tags.library }}', {})
    assert out['text'] == '空'
    assert out['omitted'] == []


def test_inverted_section_unclosed_raises():
    with pytest.raises(ValueError, match='没有对应的'):
        prompt.render_template('{{^ tags.library }}没闭合', {})


def test_empty_tag_library_renders_no_placeholder_text():
    """空标签库不得渲染「（标签库为空）」这类占位文案（白费 token 且会被当成标签）。"""
    variables = prompt.tag_variables({'tags': []}, usage=None)
    assert variables['tags.library'] == ''
    assert variables['tags.taxonomy'] == ''
    assert variables['tags.blacklist'] == ''
    assert variables['tags.isolated'] == ''


def test_tag_placeholder_absent_from_builtin_prompt():
    """内置档案在空库时，整段 <tag_library> 都不该出现。"""
    profile = builtin_profile('builtin-default')
    variables = prompt.collect_variables(
        card={'name': 'A'}, tag_library={'tags': []},
        limits={'max_tags': 8, 'summary_max_chars': 300, 'confidence_min': 0.5},
        model_name='m', profile_id='p',
    )
    result = prompt.render_messages(profile, variables)
    user = result['messages'][1]['content']
    system = result['messages'][0]['content']

    assert '<tag_library>' not in user
    assert '<tag_taxonomy>' not in user
    assert '（标签库为空）' not in user
    assert '（未配置分类）' not in user
    # 措辞也要跟着变，不能还写着「从上面的标签库中挑选」
    assert '标签库为空' in user
    assert '<tag_library>' not in system


def test_build_limits_takes_stricter_side():
    """配置是全局上限、schema 是任务具体值，取更严格的一侧。"""
    limits = prompt.build_limits(
        {'max_tags': 5, 'summary_max_chars': 500, 'confidence_min': 0.3},
        {'output_schema': {'properties': {
            'summary': {'type': 'string', 'max_chars': 200},
            'tags': {'type': 'array', 'max_items': 8, 'min_confidence': 0.6},
        }}},
    )
    assert limits['max_tags'] == 5, '取两者更小者'
    assert limits['summary_max_chars'] == 200, '取两者更小者'
    assert limits['confidence_min'] == 0.6, '取两者更大者（更严格）'


def test_card_variables_support_data_block_and_string_tags():
    card = {'data': {'name': 'A', 'tags': 'x, y', 'alternate_greetings': ['a', 'b']}}
    variables = prompt.card_variables(card)
    assert variables['card.name'] == 'A'
    assert variables['card.tags'] == 'x、y'
    assert variables['card.alternate_greetings_count'] == 2


def test_tag_variables_include_library_taxonomy_and_blacklist():
    library = {
        'tags': ['战斗', '奇幻'],
        'taxonomy': {
            'default_category': '题材',
            'categories': {'题材': {'color': '#fff', 'opacity': 1}},
            'category_tag_order': {'题材': ['战斗']},
            'tag_to_category': {'战斗': '题材'},
        },
        'prefs': {'tag_blacklist': ['暴力']},
    }
    variables = prompt.tag_variables(library, usage={'战斗': 3})
    assert '战斗' in variables['tags.library']
    assert '已用3次' in variables['tags.library']
    assert variables['tags.blacklist'] == '暴力'
    assert '题材' in variables['tags.taxonomy']


def test_collect_variables_only_leaves_task_specific_names_out():
    variables = prompt.collect_variables(
        card={'name': 'A'}, excerpt={'outline': 'x'}, tag_library={'tags': []},
        limits={'max_tags': 3}, model_name='m', profile_id='p',
    )
    task_specific = {'user.instruction', 'text.content', 'wi.entry_name', 'wi.entry_keys',
                     'wi.entry_content', 'wi.target_ratio',
                     'proposal.sample', 'proposal.existing_context',
                     'agent.catalog', 'agent.existing_context', 'agent.budget_text'}
    missing = set(prompt.KNOWN_VARIABLES) - set(variables)
    assert missing <= task_specific


def test_card_name_is_omitted_when_empty():
    """必填变量 `card.name` 缺席时，渲染层才能报出 missing_variable。"""
    assert 'card.name' not in prompt.card_variables({'description': 'x'})
    assert prompt.card_variables({'name': 'A'})['card.name'] == 'A'

    result = prompt.render_template('{{ card.name }}', prompt.card_variables({'description': 'x'}))
    assert result['missing_required'] == ['card.name']


def test_collect_variables_merges_extra():
    variables = prompt.collect_variables(card={'name': 'A'}, extra={'user.instruction': '改写'})
    assert variables['user.instruction'] == '改写'
