"""提示词模板：变量表、插值、消息组装。

纯函数层。对应设计文档 §5.3 / §5.4 / §5.5。

约定：
- 语法 `{{ ns.key }}` 为必填占位，`{{ ns.key? }}` 为可选占位（缺失渲染为空）。
- 模板中的变量名在**保存档案时**静态校验（未知变量即报错，见 `validate_template`）。
- 运行期缺失：必填 → 由调用方抛 `missing_variable`；可选 → 空串 + 计入 warnings。
- 转义：`{{{{` 输出字面 `{{`，`}}}}` 输出字面 `}}`。
"""

import re

from .hashing import card_data_block, normalize_text

VARIABLE_PATTERN = re.compile(
    r'\{\{\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*(\?)?\s*\}\}'
)

_ESCAPE_OPEN = '\x00ESC_OPEN\x00'
_ESCAPE_CLOSE = '\x00ESC_CLOSE\x00'

# 变量表：required=True 表示「运行期必须由调用方提供」（而非「值必须非空」）
VARIABLE_SPECS = {
    'card.name': {'required': True, 'title': '卡片名称'},
    'card.description': {'required': False, 'title': '简介'},
    'card.personality': {'required': False, 'title': '性格'},
    'card.scenario': {'required': False, 'title': '场景'},
    'card.first_mes': {'required': False, 'title': '开场白'},
    'card.mes_example': {'required': False, 'title': '对话示例'},
    'card.creator': {'required': False, 'title': '作者'},
    'card.creator_notes': {'required': False, 'title': '作者备注'},
    'card.system_prompt': {'required': False, 'title': '系统提示词'},
    'card.post_history_instructions': {'required': False, 'title': '历史后置指令'},
    'card.tags': {'required': False, 'title': '现有标签'},
    'card.alternate_greetings_count': {'required': False, 'title': '备用开场白数量'},

    'worldbook.outline': {'required': False, 'title': '世界书条目索引'},
    'worldbook.excerpt': {'required': False, 'title': '世界书内容节选'},
    'worldbook.count': {'required': False, 'title': '条目数'},
    'worldbook.total_tokens': {'required': False, 'title': '世界书总 token'},
    'worldbook.excerpt_tokens': {'required': False, 'title': '节选 token'},
    'worldbook.truncated': {'required': False, 'title': '是否被裁剪'},

    'tags.library': {'required': False, 'title': '标签库'},
    'tags.taxonomy': {'required': False, 'title': '标签分类体系'},
    'tags.blacklist': {'required': False, 'title': '黑名单标签'},
    'tags.isolated': {'required': False, 'title': '隔离分类'},
    'tags.usage_top': {'required': False, 'title': '高频标签'},

    'limits.max_tags': {'required': True, 'title': '最大标签数'},
    'limits.summary_max_chars': {'required': True, 'title': '概览字数上限'},
    'limits.confidence_min': {'required': True, 'title': '置信度下限'},

    'model.name': {'required': True, 'title': '模型名'},
    'model.profile_id': {'required': True, 'title': '档案 id'},

    'user.instruction': {'required': False, 'title': '用户指令（改写/翻译）'},
    'text.content': {'required': False, 'title': '待处理正文'},
    'wi.entry_name': {'required': False, 'title': '条目名'},
    'wi.entry_keys': {'required': False, 'title': '条目关键词'},
    'wi.entry_content': {'required': False, 'title': '条目正文'},
    'wi.target_ratio': {'required': False, 'title': '目标压缩比'},
}

KNOWN_VARIABLES = frozenset(VARIABLE_SPECS)


# ------------------------------------------------------------------ 模板扫描


def scan_template(template) -> list:
    """按出现顺序返回模板引用的变量名（去重）。"""
    text = '' if template is None else str(template)
    found = []
    seen = set()
    for match in VARIABLE_PATTERN.finditer(text):
        name = match.group(1)
        if name in seen:
            continue
        seen.add(name)
        found.append(name)
    return found


def validate_template(template, known=None) -> list:
    """返回模板中的未知变量名（空列表表示合法）。"""
    known_set = KNOWN_VARIABLES if known is None else frozenset(known)
    return [name for name in scan_template(template) if name not in known_set]


def validate_templates(templates, known=None) -> list:
    """批量校验多段模板（如 system + user_template），返回未知变量名。"""
    known_set = KNOWN_VARIABLES if known is None else frozenset(known)
    unknown = []
    seen = set()
    for template in templates or ():
        for name in validate_template(template, known_set):
            if name in seen:
                continue
            seen.add(name)
            unknown.append(name)
    return unknown


# ------------------------------------------------------------------ 渲染


def _protect_escapes(text: str) -> str:
    return text.replace('{{{{', _ESCAPE_OPEN).replace('}}}}', _ESCAPE_CLOSE)


def _restore_escapes(text: str) -> str:
    return text.replace(_ESCAPE_OPEN, '{{').replace(_ESCAPE_CLOSE, '}}')


def _stringify(value) -> str:
    if value is None:
        return ''
    if isinstance(value, bool):
        return '是' if value else '否'
    if isinstance(value, float):
        text = f'{value:.2f}'.rstrip('0').rstrip('.')
        return text or '0'
    if isinstance(value, (list, tuple)):
        return '、'.join(_stringify(item) for item in value if item is not None)
    if isinstance(value, dict):
        return '；'.join(f'{key}: {_stringify(item)}' for key, item in value.items())
    return str(value)


def render_template(template, variables, *, missing_policy: str = 'error',
                    known=KNOWN_VARIABLES) -> dict:
    """渲染单段模板。

    Args:
        template: 模板文本。
        variables: `{变量名: 值}`；键存在即视为「已提供」（空串合法）。
        missing_policy: `error` 时必填变量缺失计入 `missing_required`；`empty` 时一律按空串渲染。
        known: 已知变量集合（用于识别未知变量）。

    Returns:
        `{'text', 'missing_required', 'missing_optional', 'unknown'}`
    """
    source = '' if template is None else str(template)
    values = variables if isinstance(variables, dict) else {}
    known_set = frozenset(known or ())
    policy = str(missing_policy or 'error').strip().lower()

    missing_required = []
    missing_optional = []
    unknown = []

    def _replace(match):
        name = match.group(1)
        optional_mark = bool(match.group(2))
        spec = VARIABLE_SPECS.get(name)

        if name not in known_set:
            if name not in unknown:
                unknown.append(name)
            return ''

        if name in values:
            return _stringify(values.get(name))

        required = bool(spec and spec.get('required')) and not optional_mark
        if required and policy == 'error':
            if name not in missing_required:
                missing_required.append(name)
        else:
            if name not in missing_optional:
                missing_optional.append(name)
        return ''

    protected = _protect_escapes(source)
    rendered = VARIABLE_PATTERN.sub(_replace, protected)

    return {
        'text': _restore_escapes(rendered),
        'missing_required': missing_required,
        'missing_optional': missing_optional,
        'unknown': unknown,
    }


def join_nonempty(parts, sep: str = '\n\n') -> str:
    """用分隔符拼接非空片段（保留片段内部格式）。"""
    chunks = []
    for part in parts or ():
        text = '' if part is None else str(part).strip()
        if text:
            chunks.append(text)
    return sep.join(chunks)


# ------------------------------------------------------------------ 格式与长度要求


def format_instruction(format_rules, length_rules) -> str:
    """把格式要求与字数要求渲染成中文指令段（生成约束）。

    同一份规则同时用于后置校验（`core/parsing.py`），保证「要求」与「校验」同源。
    """
    rules = format_rules if isinstance(format_rules, dict) else {}
    limits = length_rules if isinstance(length_rules, dict) else {}

    lines = []

    if rules.get('json_only', True):
        lines.append('只输出一个 JSON 对象，不要输出解释、前言、思考过程或 Markdown 代码块。')
        wrappers = rules.get('forbidden_wrappers') or []
        if wrappers:
            lines.append(f'不要使用 { "、".join(str(item) for item in wrappers) } 之类的包裹。')

    required_keys = rules.get('required_keys') or []
    if required_keys:
        lines.append(f'必需字段：{ "、".join(str(item) for item in required_keys) }。')

    summary_max = limits.get('summary_max_chars')
    summary_min = limits.get('summary_min_chars')
    if summary_max:
        if summary_min:
            lines.append(f'概览长度 {int(summary_min)}–{int(summary_max)} 字。')
        else:
            lines.append(f'概览不超过 {int(summary_max)} 字。')

    tags_max = limits.get('tags_max')
    tags_min = limits.get('tags_min')
    if tags_max:
        if tags_min:
            lines.append(f'标签 {int(tags_min)}–{int(tags_max)} 个。')
        else:
            lines.append(f'标签不超过 {int(tags_max)} 个。')

    confidence_min = limits.get('confidence_min')
    if confidence_min:
        lines.append(f'置信度低于 {confidence_min} 的标签不要输出。')

    if not lines:
        return ''

    return '输出要求：\n' + '\n'.join(f'{index}. {line}' for index, line in enumerate(lines, 1))


def render_messages(profile, variables, *, task: str = 'analyze') -> dict:
    """按档案组装 chat messages。

    Returns:
        `{'messages': [...], 'missing_required': [...], 'missing_optional': [...], 'unknown': [...]}`
    """
    spec = profile if isinstance(profile, dict) else {}
    policy = str(spec.get('missing_policy') or 'error')

    system_result = render_template(spec.get('system'), variables, missing_policy=policy)
    user_result = render_template(spec.get('user_template'), variables, missing_policy=policy)

    format_text = format_instruction(spec.get('format_rules'), spec.get('length_rules'))

    system_content = join_nonempty([
        spec.get('system_prelude'),
        system_result['text'],
        format_text,
        spec.get('system_suffix'),
    ])
    user_content = join_nonempty([
        spec.get('user_prelude'),
        user_result['text'],
        spec.get('user_suffix'),
    ])

    messages = []
    if system_content:
        messages.append({'role': 'system', 'content': system_content})
    messages.append({'role': 'user', 'content': user_content})

    prefill = str(spec.get('assistant_prefill') or '').strip()
    if prefill:
        messages.append({'role': 'assistant', 'content': prefill})

    missing_required = list(dict.fromkeys(
        system_result['missing_required'] + user_result['missing_required']
    ))
    missing_optional = list(dict.fromkeys(
        system_result['missing_optional'] + user_result['missing_optional']
    ))
    unknown = list(dict.fromkeys(system_result['unknown'] + user_result['unknown']))

    return {
        'messages': messages,
        'missing_required': missing_required,
        'missing_optional': missing_optional,
        'unknown': unknown,
        'task': str(task or 'analyze'),
    }


# ------------------------------------------------------------------ 变量构建


def build_limits(config, profile) -> dict:
    """合并「全局配置」与「档案规则」，取更严格的一侧。

    配置是全局上限，档案是该模型的具体值；这样 `limits.*` 变量、生成约束与
    后置校验三者始终同源（设计文档 §5.5）。
    """
    conf = config if isinstance(config, dict) else {}
    spec = profile if isinstance(profile, dict) else {}
    length_rules = spec.get('length_rules') if isinstance(spec.get('length_rules'), dict) else {}

    def _pick_int(config_key, rule_key, default):
        candidates = []
        for value in (conf.get(config_key), length_rules.get(rule_key)):
            if isinstance(value, bool) or value is None:
                continue
            try:
                candidates.append(int(value))
            except (TypeError, ValueError):
                continue
        if not candidates:
            return default
        return min(candidates)

    def _pick_float_max(config_key, rule_key, default):
        candidates = []
        for value in (conf.get(config_key), length_rules.get(rule_key)):
            if isinstance(value, bool) or value is None:
                continue
            try:
                candidates.append(float(value))
            except (TypeError, ValueError):
                continue
        if not candidates:
            return default
        return max(candidates)

    return {
        'max_tags': _pick_int('max_tags', 'tags_max', 8),
        'summary_max_chars': _pick_int('summary_max_chars', 'summary_max_chars', 300),
        'summary_min_chars': _pick_int('summary_min_chars', 'summary_min_chars', 0),
        'tags_min': _pick_int('tags_min', 'tags_min', 0),
        'confidence_min': _pick_float_max('confidence_min', 'confidence_min', 0.5),
    }


def card_variables(card) -> dict:
    """从 `api.read_card()` 的返回构建 `card.*` 变量。

    唯一例外：`card.name` 为空时**不放进变量表**——它是必填变量，缺席会让渲染层
    报 `missing_variable`，比「用一个空名字去分析」更有意义。
    """
    block = card_data_block(card) if isinstance(card, dict) else {}

    tags = block.get('tags') or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(',') if part.strip()]
    tag_text = '、'.join(str(item) for item in tags if item)

    greetings = block.get('alternate_greetings') or []
    greeting_count = len(greetings) if isinstance(greetings, list) else 0

    variables = {
        'card.description': normalize_text(block.get('description')),
        'card.personality': normalize_text(block.get('personality')),
        'card.scenario': normalize_text(block.get('scenario')),
        'card.first_mes': normalize_text(block.get('first_mes')),
        'card.mes_example': normalize_text(block.get('mes_example')),
        'card.creator': normalize_text(block.get('creator')),
        'card.creator_notes': normalize_text(block.get('creator_notes')),
        'card.system_prompt': normalize_text(block.get('system_prompt')),
        'card.post_history_instructions': normalize_text(block.get('post_history_instructions')),
        'card.tags': tag_text,
        'card.alternate_greetings_count': greeting_count,
    }

    name = normalize_text(block.get('name'))
    if name:
        variables['card.name'] = name

    return variables


def worldbook_variables(excerpt) -> dict:
    """从 `core.trimming.select_wi_excerpt()` 的结果构建 `worldbook.*` 变量。"""
    data = excerpt if isinstance(excerpt, dict) else {}
    return {
        'worldbook.outline': data.get('outline', ''),
        'worldbook.excerpt': data.get('excerpt', ''),
        'worldbook.count': data.get('count', 0),
        'worldbook.total_tokens': data.get('total_tokens', 0),
        'worldbook.excerpt_tokens': data.get('excerpt_tokens', 0),
        'worldbook.truncated': bool(data.get('truncated', False)),
    }


def format_tag_library(tags, *, taxonomy=None, usage=None, limit: int = 200) -> str:
    """把标签库渲染成紧凑文本（可含分类与使用频次）。"""
    items = [str(item).strip() for item in (tags or []) if str(item).strip()]
    if not items:
        return '（标签库为空）'

    tag_to_category = {}
    if isinstance(taxonomy, dict) and isinstance(taxonomy.get('tag_to_category'), dict):
        tag_to_category = taxonomy['tag_to_category']

    usage_map = usage if isinstance(usage, dict) else {}

    def _sort_key(tag):
        return (-int(usage_map.get(tag) or 0), tag)

    ordered = sorted(set(items), key=_sort_key)
    size = max(1, int(limit or 200))
    shown = ordered[:size]

    lines = []
    for tag in shown:
        parts = [tag]
        category = tag_to_category.get(tag)
        if category:
            parts.append(f'分类:{category}')
        count = int(usage_map.get(tag) or 0)
        if count:
            parts.append(f'已用{count}次')
        lines.append(' · '.join(parts))

    text = '\n'.join(f'- {line}' for line in lines)
    if len(ordered) > len(shown):
        text += f'\n（共 {len(ordered)} 个标签，此处列出 {len(shown)} 个）'
    return text


def format_taxonomy(taxonomy, *, limit: int = 40) -> str:
    """把分类体系渲染成紧凑文本。"""
    data = taxonomy if isinstance(taxonomy, dict) else {}
    order = data.get('category_tag_order') if isinstance(data.get('category_tag_order'), dict) else {}
    categories = data.get('categories') if isinstance(data.get('categories'), dict) else {}

    names = []
    for name in list(order.keys()) + list(categories.keys()):
        text = str(name or '').strip()
        if text and text not in names:
            names.append(text)

    if not names:
        return '（未配置分类）'

    lines = []
    for name in names[:max(1, int(limit or 40))]:
        tags = order.get(name) or []
        if isinstance(tags, (list, tuple)):
            tag_text = '、'.join(str(item) for item in tags if str(item).strip())
        else:
            tag_text = ''
        lines.append(f'- {name}：{tag_text or "（无标签）"}')

    default_category = data.get('default_category')
    header = f'默认分类：{default_category}\n' if default_category else ''
    return header + '\n'.join(lines)


def format_usage_top(usage, *, limit: int = 20) -> str:
    """把标签使用频次渲染成紧凑文本。"""
    data = usage if isinstance(usage, dict) else {}
    pairs = [(str(tag), int(count or 0)) for tag, count in data.items() if str(tag).strip()]
    if not pairs:
        return ''
    pairs.sort(key=lambda item: (-item[1], item[0]))
    return '、'.join(f'{tag}({count})' for tag, count in pairs[:max(1, int(limit or 20))])


def tag_variables(tag_library, *, usage=None) -> dict:
    """从 `api.read_tag_library()` 的返回构建 `tags.*` 变量。"""
    data = tag_library if isinstance(tag_library, dict) else {}
    prefs = data.get('prefs') if isinstance(data.get('prefs'), dict) else {}

    blacklist = prefs.get('tag_blacklist') or []
    if isinstance(blacklist, str):
        blacklist = [part.strip() for part in blacklist.split(',') if part.strip()]

    isolated = data.get('isolated_categories') or prefs.get('isolated_categories') or []
    if isinstance(isolated, dict):
        isolated = list(isolated.keys())
    if not isinstance(isolated, (list, tuple)):
        isolated = []

    return {
        'tags.library': format_tag_library(data.get('tags'), taxonomy=data.get('taxonomy'), usage=usage),
        'tags.taxonomy': format_taxonomy(data.get('taxonomy')),
        'tags.blacklist': '、'.join(str(item) for item in blacklist) or '（无）',
        'tags.isolated': '、'.join(str(item) for item in isolated) or '（无）',
        'tags.usage_top': format_usage_top(usage),
    }


def collect_variables(*, card=None, excerpt=None, tag_library=None, usage=None,
                      limits=None, model_name: str = '', profile_id: str = '',
                      extra=None) -> dict:
    """汇总全部变量；缺失的来源按空值填充（不抛错，由渲染层决定策略）。"""
    variables = {}
    variables.update(card_variables(card))
    variables.update(worldbook_variables(excerpt))
    variables.update(tag_variables(tag_library, usage=usage))

    limits_data = limits if isinstance(limits, dict) else {}
    variables['limits.max_tags'] = limits_data.get('max_tags', 8)
    variables['limits.summary_max_chars'] = limits_data.get('summary_max_chars', 300)
    variables['limits.confidence_min'] = limits_data.get('confidence_min', 0.5)

    variables['model.name'] = str(model_name or '')
    variables['model.profile_id'] = str(profile_id or '')

    if isinstance(extra, dict):
        variables.update(extra)

    return variables
