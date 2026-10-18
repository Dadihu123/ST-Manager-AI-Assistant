"""提示词模板：变量表、插值、消息组装。

纯函数层。对应设计文档 §5.3 / §5.4 / §5.5。

约定：
- 语法 `{{ ns.key }}` 为必填占位，`{{ ns.key? }}` 为可选占位（缺失渲染为空）。
- 模板中的变量名在**保存档案时**静态校验（未知变量即报错，见 `validate_template`）。
- 运行期缺失：必填 → 由调用方抛 `missing_variable`；可选 → 空串 + 计入 warnings。
- 转义：`{{{{` 输出字面 `{{`，`}}}}` 输出字面 `}}`。
"""

import re

from . import selector as selector_core
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
    'card.alternate_greetings': {'required': False, 'title': '备用开场白内容'},
    'card.local_note': {'required': False, 'title': '本地备注'},
    'card.regex_scripts': {'required': False, 'title': '正则脚本（摘要）'},
    'card.st_scripts': {'required': False, 'title': '酒馆助手脚本（摘要）'},

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

    'proposal.sample': {'required': False, 'title': '卡片采样清单'},
    'proposal.existing_context': {'required': False, 'title': '现有分类与标签'},

    'agent.catalog': {'required': False, 'title': '卡片库目录'},
    'agent.existing_context': {'required': False, 'title': '现有分类与标签'},
    'agent.budget_text': {'required': False, 'title': '读取预算说明'},
}

KNOWN_VARIABLES = frozenset(VARIABLE_SPECS)


# ------------------------------------------------------------------ 模板扫描


#: 条件块的开/闭标签（扫描时要一起识别，否则块里的变量名拼错也发现不了）
SECTION_TAG_PATTERN = re.compile(
    r'\{\{\s*([#/^])\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}'
)


def scan_template(template) -> list:
    """按出现顺序返回模板引用的变量名（去重）。

    同时识别变量插值 `{{ x }}`、可选插值 `{{ x? }}` 与条件块标签
    `{{# x }}` / `{{/ x }}` —— 否则条件块里拼错的变量名会被静默放过。
    """
    text = '' if template is None else str(template)
    found = []
    seen = set()

    # 条件块标签在前（`{{# x }}` 不会被 VARIABLE_PATTERN 匹配，故两者不重叠）
    for match in SECTION_TAG_PATTERN.finditer(text):
        name = match.group(2)
        if name in seen:
            continue
        seen.add(name)
        found.append(name)

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


#: 条件块：`{{# var }}` 值非空时输出；`{{^ var }}` 值空/缺失时输出。共用 `{{/ var }}` 收尾。
SECTION_PATTERN = re.compile(
    r'\{\{\s*([#^])\s*([A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z0-9_]+)*)\s*\}\}'
    r'([\s\S]*?)'
    r'\{\{\s*/\s*\2\s*\}\}'
)

#: 「只有标签、值被渲染成空」的行：`简介：` / `简介=` / `- 场景: ` 这类残句
_LABEL_ONLY_PATTERN = re.compile(r'^\s*(?:[-*•·]\s*)?[^=:：\n]{1,40}\s*[=:：]\s*$')

#: 条件块最多嵌套层数（超过即视为模板有误，避免死循环）
MAX_SECTION_DEPTH = 8


def _is_blank(value) -> bool:
    """变量值是否「等于没给」：空串、纯空白、空容器都算。"""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, dict, set)):
        return len(value) == 0
    return False


def _collapse_empty_label_lines(lines, template_line, rendered_line) -> bool:
    """判断这一行是否该整行丢弃。

    只丢两种情况同时成立的行：
    1. **模板那一行**引用了某个空值变量（说明这行的内容全靠它撑起来）；
    2. **渲染后**只剩一个标签（`简介：`），没有实际内容。

    这两个条件缺一不可 —— 只看第 2 条会误伤模板里本来就写死的 `注意：` 这类文字。
    """
    if not template_line or not rendered_line:
        return False
    if not VARIABLE_PATTERN.search(template_line):
        return False
    return bool(_LABEL_ONLY_PATTERN.match(rendered_line))


def render_template(template, variables, *, missing_policy: str = 'error',
                    known=KNOWN_VARIABLES) -> dict:
    """渲染单段模板。

    支持两种结构：

    - `{{ ns.key }}` / `{{ ns.key? }}` —— 变量插值；
    - `{{# ns.key }} … {{/ ns.key }}` —— **条件块**，变量为空/缺失时整段不输出。

    Args:
        template: 模板文本。
        variables: `{变量名: 值}`；键存在即视为「已提供」（空串合法）。
        missing_policy: `error` 时必填变量缺失计入 `missing_required`；`empty` 时一律按空串渲染。
        known: 已知变量集合（用于识别未知变量）。

    Returns:
        `{'text', 'missing_required', 'missing_optional', 'unknown', 'omitted'}`

    Raises:
        ValueError: 条件块未闭合或开闭标签不匹配。

    关于「空值」的清理：
        关闭的字段渲染成空串后，`简介={{ card.description }}` 会留下 `简介：` 这种
        只有标签的残句——既浪费 token，也会让模型以为「简介本来就为空」。
        因此渲染后会把这类行整行丢弃（判据见 `_collapse_empty_label_lines`）。
        想完全掌控时，请用条件块把标签一起包起来。
    """
    source = '' if template is None else str(template)
    values = variables if isinstance(variables, dict) else {}
    known_set = frozenset(known or ())
    policy = str(missing_policy or 'error').strip().lower()

    missing_required = []
    missing_optional = []
    unknown = []
    omitted = []

    def _resolve(name, optional_mark: bool) -> str:
        """解析单个变量：返回 `(文本, 是否为空)`。"""
        if name not in known_set:
            if name not in unknown:
                unknown.append(name)
            return ''

        if name in values:
            return _stringify(values.get(name))

        required = bool(VARIABLE_SPECS.get(name, {}).get('required')) and not optional_mark
        if required and policy == 'error':
            if name not in missing_required:
                missing_required.append(name)
        else:
            if name not in missing_optional:
                missing_optional.append(name)
        return ''

    # ---- 第 1 步：条件块（先于插值，避免块内变量被提前替换掉）----
    protected = _protect_escapes(source)

    def _render_section(match):
        marker = match.group(1)          # `#` 正向 / `^` 反向
        name = match.group(2).strip()
        body = match.group(3)
        inverted = marker == '^'

        # 未知变量：块内内容一律不输出，并把变量名记进 unknown
        if name not in known_set:
            if name not in unknown:
                unknown.append(name)
            return ''

        blank = name not in values or _is_blank(values.get(name))

        if inverted:
            # 反向块：值为空时才输出。此时不算「omitted」（那块本来就是备用文案）
            return body if blank else ''

        if blank:
            if name not in omitted:
                omitted.append(name)
            return ''
        return body

    # 检测未闭合：有 `{{# x }}` / `{{^ x }}` 但找不到 `{{/ x }}` → 报错
    for opening in re.finditer(
        r'\{\{\s*[#^]\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\}\}', protected,
    ):
        name = opening.group(1)
        # 用字符串拼接而非 f-string：正则里的 `{{` 与 f-string 的花括号会打架
        closing_pattern = r'\{\{\s*/\s*' + re.escape(name) + r'\s*\}\}'
        if not re.search(closing_pattern, protected):
            raise ValueError(f'条件块 {{{{# {name} }}}} 没有对应的 {{{{/ {name} }}}}')

    # 由内向外反复替换：`SECTION_PATTERN` 的非贪婪匹配会把「外层块的结束标签」
    # 当成本层结束，导致内层标签原样漏进输出。逐层收敛可正确处理嵌套
    # （内层先被替换掉，外层再匹配时块体已不含标签）。
    rendered = protected
    for _ in range(MAX_SECTION_DEPTH):
        if not SECTION_PATTERN.search(rendered):
            break
        rendered = SECTION_PATTERN.sub(_render_section, rendered)

    # ---- 第 2 步：变量插值（逐行做，以便判断「这行的值是不是空的」）----
    def _replace(match):
        return _resolve(match.group(1), bool(match.group(2)))

    out_lines = []
    for line in rendered.split('\n'):
        new_line = VARIABLE_PATTERN.sub(_replace, line)
        if _collapse_empty_label_lines(line, line, new_line):
            continue
        out_lines.append(new_line)

    # 条件块被整段丢弃后会留下多余空行：连续空行折叠成一个，
    # 并去掉首尾空行（否则 prompt 里会有一堆无意义的换行）。
    compacted = []
    for line in out_lines:
        if not line.strip() and (not compacted or not compacted[-1].strip()):
            continue
        compacted.append(line)
    while compacted and not compacted[-1].strip():
        compacted.pop()

    return {
        'text': _restore_escapes('\n'.join(compacted)),
        'missing_required': missing_required,
        'missing_optional': missing_optional,
        'unknown': unknown,
        'omitted': omitted,
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


def format_instruction(output_schema) -> str:
    """把输出 schema 渲染成中文指令段（生成约束）。

    同一份 schema 同时用于后置校验（`core.parsing.validate_result`），
    因此「要求模型这样答」与「检查模型这样答了没」不会漂移（设计文档 §8.5）。
    """
    from . import schema as schema_core  # noqa: PLC0415 - 循环导入

    lines = schema_core.describe_constraints(output_schema)
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

    format_text = format_instruction(spec.get('output_schema'))

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


def profile_selector(profile, config=None) -> dict:
    """解析最终生效的选择器。

    优先级：**档案 > 全局配置 > 内置默认**。
    档案是「这次分析要看什么」的直接表达，理应压过全局；全局只是新档案的初值。
    """
    spec = profile if isinstance(profile, dict) else {}
    conf = config if isinstance(config, dict) else {}

    base = conf.get('content_selector') if isinstance(conf.get('content_selector'), dict) else None
    if base is None:
        base = selector_core.default_selector()

    override = spec.get('content_selector')
    return selector_core.merge_selector(base, override if isinstance(override, dict) else {})


def profile_tag_pool(profile, config=None) -> dict:
    """解析最终生效的标签池（优先级同 `profile_selector`）。"""
    from . import tagpool  # noqa: PLC0415 - 局部导入

    return tagpool.profile_pool(profile, config)


def variable_usage(profile, variables=None) -> dict:
    """统计每个变量在档案模板里被引用了几次。

    Returns:
        `{'hits': {变量名: 次数}, 'referenced': [...], 'unused': [...]}`

    `unused` 是「选择器放行了、但模板根本没引用」的变量 —— 这些变量的内容
    会白白进入变量表却不进 prompt，是用户最容易困惑的地方（以为勾了就发了）。
    """
    spec = profile if isinstance(profile, dict) else {}
    templates = (
        spec.get('system'),
        spec.get('user_template'),
        spec.get('system_prelude'),
        spec.get('system_suffix'),
        spec.get('user_prelude'),
        spec.get('user_suffix'),
    )

    hits = {}
    for template in templates:
        text = '' if template is None else str(template)
        for match in VARIABLE_PATTERN.finditer(text):
            name = match.group(1)
            hits[name] = hits.get(name, 0) + 1

    referenced = sorted(hits)
    unused = []
    if isinstance(variables, dict):
        unused = sorted(
            name for name, value in variables.items()
            if name not in hits and str(value or '').strip()
        )

    return {'hits': hits, 'referenced': referenced, 'unused': unused}


def selector_gaps(selector, profile, variables=None) -> list:
    """找出选择器与模板之间的两类不一致，供预览层提示。

    1. 模板引用了某变量，但选择器把它的来源关掉了 → **会导致空值**（要警告）；
    2. 选择器放行了某变量，但模板没引用 → 白勾了（仅提示）。

    返回人类可读的中文提示列表（空列表表示一致）。
    """
    spec = selector_core.normalize_selector(selector)
    usage = variable_usage(profile, variables)
    hits = usage['hits']

    field_to_variable = {name: f'card.{name}' for name in selector_core.SELECTABLE_CARD_FIELDS}
    disabled_variables = {
        field_to_variable[name]: name
        for name in selector_core.SELECTABLE_CARD_FIELDS
        if name not in spec['card_fields']
    }

    if spec['alternate_greetings']['mode'] == selector_core.GREETINGS_NONE:
        disabled_variables['card.alternate_greetings'] = 'alternate_greetings'
    for extra, variable in (
        (selector_core.EXTRA_REGEX, 'card.regex_scripts'),
        (selector_core.EXTRA_ST_SCRIPTS, 'card.st_scripts'),
        (selector_core.EXTRA_LOCAL_NOTE, 'card.local_note'),
    ):
        if extra not in spec['extras']:
            disabled_variables[variable] = extra
    if spec['worldbook']['mode'] == selector_core.WI_NONE:
        disabled_variables['worldbook.excerpt'] = 'worldbook'
        disabled_variables['worldbook.outline'] = 'worldbook'

    gaps = []
    for variable, source in sorted(disabled_variables.items()):
        if variable in hits:
            gaps.append(
                f'模板引用了 {variable}，但选择器已关闭它的来源（{source}），'
                f'该处会渲染为空'
            )

    for variable in usage['unused']:
        if variable in selector_core.ALWAYS_AVAILABLE_VARIABLES:
            continue
        gaps.append(f'选择器放行了 {variable}，但模板没有引用它（这些内容不会进入提示词）')

    return gaps


# ------------------------------------------------------------------ 变量构建


def build_limits(config, profile) -> dict:
    """合并「全局配置」与「任务规格的 output_schema」，取更严格的一侧。

    配置是全局上限，schema 是该任务的具体值；这样 `limits.*` 变量、生成约束与
    后置校验三者始终同源（设计文档 §5.5 / §8.5）。

    输出仍是扁平 dict —— 它同时要喂给模板变量 `{{ limits.* }}` 与调用方，
    扁平比嵌套更好用。
    """
    from . import schema as schema_core  # noqa: PLC0415 - 循环导入

    conf = config if isinstance(config, dict) else {}
    spec = profile if isinstance(profile, dict) else {}
    output_schema = spec.get('output_schema')

    properties = schema_core.normalize_schema(output_schema)['properties']
    summary_prop = properties.get('summary') or {}
    tags_prop = properties.get('tags') or {}

    def _pick_int(config_key, schema_value, default):
        candidates = []
        for value in (conf.get(config_key), schema_value):
            if isinstance(value, bool) or value is None:
                continue
            try:
                candidates.append(int(value))
            except (TypeError, ValueError):
                continue
        if not candidates:
            return default
        return min(candidates)

    def _pick_float_max(config_key, schema_value, default):
        candidates = []
        for value in (conf.get(config_key), schema_value):
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
        'max_tags': _pick_int('max_tags', tags_prop.get('max_items'), 8),
        'summary_max_chars': _pick_int(
            'summary_max_chars', summary_prop.get('max_chars'), 300,
        ),
        'summary_min_chars': _pick_int(
            'summary_min_chars', summary_prop.get('min_chars'), 0,
        ),
        'tags_min': _pick_int('tags_min', tags_prop.get('min_items'), 0),
        'confidence_min': _pick_float_max(
            'confidence_min', tags_prop.get('min_confidence'), 0.5,
        ),
    }


def alternate_greetings_text(block, selector=None) -> str:
    """按选择器把备用开场白拼成文本（编号列出，便于模型引用「第 N 条」）。"""
    greetings = block.get('alternate_greetings') if isinstance(block, dict) else None
    if not isinstance(greetings, list) or not greetings:
        return ''

    spec = selector_core.normalize_selector(selector)
    mode = spec['alternate_greetings']['mode']

    if mode == selector_core.GREETINGS_NONE:
        return ''

    items = [normalize_text(item) for item in greetings]
    if mode == selector_core.GREETINGS_SELECTED:
        indexes = spec['alternate_greetings']['indexes']
        picked = [(index, items[index]) for index in indexes if 0 <= index < len(items)]
    else:
        picked = list(enumerate(items))

    lines = []
    for index, text in picked:
        if not text:
            continue
        lines.append(f'[第 {index + 1} 条]\n{text}')
    return '\n\n'.join(lines)


def regex_scripts_text(extensions) -> str:
    """把卡片自带的正则脚本压成摘要（只要「做了什么」，不要脚本正文）。"""
    data = extensions if isinstance(extensions, dict) else {}
    scripts = data.get('regex_scripts')
    if not isinstance(scripts, list) or not scripts:
        return ''

    lines = []
    for script in scripts:
        if not isinstance(script, dict):
            continue
        name = str(script.get('scriptName') or '').strip() or '(未命名)'
        markers = []
        if script.get('disabled'):
            markers.append('已停用')
        if script.get('markdownOnly'):
            markers.append('仅显示')
        if script.get('promptOnly'):
            markers.append('仅提示词')
        depth = script.get('minDepth')
        if isinstance(depth, int):
            markers.append(f'depth≥{depth}')
        suffix = f'（{"、".join(markers)}）' if markers else ''
        find = str(script.get('findRegex') or '').strip()
        brief = f'匹配 {find[:60]}' if find else '无匹配式'
        lines.append(f'- {name}{suffix}：{brief}')

    return '\n'.join(lines)


def st_scripts_text(extensions) -> str:
    """把酒馆助手脚本压成摘要（脚本正文通常几千行，绝不能整段发出去）。"""
    data = extensions if isinstance(extensions, dict) else {}
    helper = data.get('tavern_helper')
    if not isinstance(helper, dict):
        return ''

    lines = []
    scripts = helper.get('scripts')
    if isinstance(scripts, list):
        for script in scripts:
            if not isinstance(script, dict):
                continue
            name = str(script.get('name') or '').strip() or '(未命名)'
            enabled = '启用' if script.get('enabled', True) else '停用'
            kind = '脚本' if script.get('type') != 'folder' else '文件夹'
            lines.append(f'- {name}（{kind}，{enabled}）')

    variables = helper.get('variables')
    if isinstance(variables, dict) and variables:
        keys = sorted(str(key) for key in variables.keys())
        shown = '、'.join(keys[:20])
        more = f' 等 {len(keys)} 个' if len(keys) > 20 else ''
        lines.append(f'- 变量：{shown}{more}')

    return '\n'.join(lines)


def card_variables(card, selector=None, *, extensions=None, local_note=None) -> dict:
    """从 `api.cards.read()` 的返回构建 `card.*` 变量。

    选择器只**收敛**取值：未勾选的字段给空串（键仍存在，因此模板写它不会报
    missing），这样「不发」和「模板没写」在行为上一致，但预览层能分别提示。

    唯一例外：`card.name` 为空时**不放进变量表**——它是必填变量，缺席会让渲染层
    报 `missing_variable`，比「用一个空名字去分析」更有意义。

    Args:
        selector: 内容选择器（`core.selector.normalize_selector` 的输入）。
        extensions: `api.cards.read_extensions()` 的返回；仅在选择器要求时读取。
        local_note: `api.cards.read_note()` 的返回；仅在选择器要求时使用。
    """
    block = card_data_block(card) if isinstance(card, dict) else {}
    spec = selector_core.normalize_selector(selector)

    def _field(name):
        """按选择器决定该字段是否给值。"""
        if name in spec['card_fields']:
            return normalize_text(block.get(name))
        return ''

    tags = block.get('tags') or []
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.split(',') if part.strip()]
    tag_text = '、'.join(str(item) for item in tags if item)

    greetings = block.get('alternate_greetings') or []
    greeting_count = len(greetings) if isinstance(greetings, list) else 0

    variables = {
        'card.description': _field('description'),
        'card.personality': _field('personality'),
        'card.scenario': _field('scenario'),
        'card.first_mes': _field('first_mes'),
        'card.mes_example': _field('mes_example'),
        'card.creator': _field('creator'),
        'card.creator_notes': _field('creator_notes'),
        'card.system_prompt': _field('system_prompt'),
        'card.post_history_instructions': _field('post_history_instructions'),
        'card.tags': tag_text if 'tags' in spec['card_fields'] else '',
        'card.alternate_greetings_count': greeting_count,
        'card.alternate_greetings': alternate_greetings_text(block, spec),
        'card.local_note': (
            normalize_text(local_note)
            if selector_core.wants_extra(spec, selector_core.EXTRA_LOCAL_NOTE) else ''
        ),
        'card.regex_scripts': (
            regex_scripts_text(extensions)
            if selector_core.wants_extra(spec, selector_core.EXTRA_REGEX) else ''
        ),
        'card.st_scripts': (
            st_scripts_text(extensions)
            if selector_core.wants_extra(spec, selector_core.EXTRA_ST_SCRIPTS) else ''
        ),
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


def format_taxonomy(taxonomy, *, limit: int = 40) -> str:
    """把分类体系渲染成紧凑文本。

    **未配置时返回空串**：占位文案会被模板原样发进提示词，
    配合条件块 `{{# tags.taxonomy }}` 能让整段一起消失。
    """
    data = taxonomy if isinstance(taxonomy, dict) else {}
    order = data.get('category_tag_order') if isinstance(data.get('category_tag_order'), dict) else {}
    categories = data.get('categories') if isinstance(data.get('categories'), dict) else {}

    names = []
    for name in list(order.keys()) + list(categories.keys()):
        text = str(name or '').strip()
        if text and text not in names:
            names.append(text)

    if not names:
        return ''

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


def tag_variables(tag_library, *, usage=None, pool=None, card=None) -> dict:
    """从 `api.tags.library()` 的返回构建 `tags.*` 变量。

    标签池（`core.tagpool`）负责：来源过滤、隔离分类硬过滤、token 预算、相关性排序。
    未给 `pool` 时用默认池（全库、1200 token 预算），行为比改造前更省 token。
    """
    from . import tagpool  # noqa: PLC0415 - 局部导入，避免顶层依赖链变长

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

    spec = tagpool.normalize_pool(pool if pool is not None else tagpool.default_pool())
    pool_result = tagpool.build_pool_text(
        data, spec, card=card, usage=usage,
    )

    return {
        'tags.library': pool_result['text'],
        'tags.taxonomy': format_taxonomy(data.get('taxonomy')),
        # 空串而非「（无）」：占位文案会被发进提示词，且「（无）」可能被模型当成真实条目
        'tags.blacklist': '、'.join(str(item) for item in blacklist),
        'tags.isolated': '、'.join(str(item) for item in isolated),
        'tags.usage_top': format_usage_top(usage),
    }


def collect_variables(*, card=None, excerpt=None, tag_library=None, usage=None,
                      limits=None, model_name: str = '', profile_id: str = '',
                      selector=None, extensions=None, local_note=None,
                      tag_pool=None, extra=None) -> dict:
    """汇总全部变量；缺失的来源按空值填充（不抛错，由渲染层决定策略）。

    `selector` 只影响 `card.*` 的取值广度；`tag_pool` 只影响 `tags.library`
    的内容与预算。两者都不改变变量表结构。
    """
    variables = {}
    variables.update(card_variables(
        card, selector, extensions=extensions, local_note=local_note,
    ))
    variables.update(worldbook_variables(excerpt))
    variables.update(tag_variables(
        tag_library, usage=usage, pool=tag_pool, card=card,
    ))

    limits_data = limits if isinstance(limits, dict) else {}
    variables['limits.max_tags'] = limits_data.get('max_tags', 8)
    variables['limits.summary_max_chars'] = limits_data.get('summary_max_chars', 300)
    variables['limits.confidence_min'] = limits_data.get('confidence_min', 0.5)

    variables['model.name'] = str(model_name or '')
    variables['model.profile_id'] = str(profile_id or '')

    if isinstance(extra, dict):
        variables.update(extra)

    return variables
