"""提示词模板：变量扫描、插值、消息组装与限制合并。"""

from stm_ai_core import prompt
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
    text = prompt.format_instruction(
        {'json_only': True, 'required_keys': ['summary']},
        {'summary_max_chars': 200, 'tags_max': 5, 'confidence_min': 0.5},
    )
    assert 'JSON' in text
    assert 'summary' in text
    assert '200' in text
    assert '5' in text


def test_format_instruction_empty_when_no_rules():
    assert prompt.format_instruction({'json_only': False}, {}) == ''


def test_render_messages_orders_prelude_then_format_then_suffix():
    profile = builtin_profile('builtin-gpt')
    variables = prompt.collect_variables(card={'name': 'A'}, model_name='gpt-4o',
                                         profile_id='builtin-gpt')
    result = prompt.render_messages(profile, variables)
    system = result['messages'][0]['content']

    assert result['messages'][0]['role'] == 'system'
    assert result['messages'][1]['role'] == 'user'
    assert system.index(profile['system']) < system.index('输出要求：')
    assert system.index('输出要求：') < system.index(profile['system_suffix'])


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


def test_build_limits_takes_stricter_side():
    limits = prompt.build_limits(
        {'max_tags': 5, 'summary_max_chars': 500, 'confidence_min': 0.3},
        {'length_rules': {'tags_max': 8, 'summary_max_chars': 200, 'confidence_min': 0.6}},
    )
    assert limits['max_tags'] == 5
    assert limits['summary_max_chars'] == 200
    assert limits['confidence_min'] == 0.6


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
                     'wi.entry_content', 'wi.target_ratio'}
    missing = set(prompt.KNOWN_VARIABLES) - set(variables)
    assert missing <= task_specific


def test_collect_variables_merges_extra():
    variables = prompt.collect_variables(card={'name': 'A'}, extra={'user.instruction': '改写'})
    assert variables['user.instruction'] == '改写'
